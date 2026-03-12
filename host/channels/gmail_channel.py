"""Gmail channel implementation using Gmail API via google-api-python-client"""
import asyncio
import base64
import collections
import email as email_lib
import logging
import os
from email.mime.text import MIMEText
from typing import Callable, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import register_channel_class as register_channel
from .. import config
from ..env import read_env_file

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


class GmailChannel:
    name = "gmail"

    def __init__(self, on_message: Callable, on_chat_metadata: Callable, registered_groups: list):
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._registered_groups = registered_groups
        self._connected = False
        self._service = None
        self._poll_task: Optional[asyncio.Task] = None
        # Bounded LRU cache for seen message IDs — prevents unbounded memory growth
        # on long-running deployments. Oldest entries are evicted when full.
        self._seen_message_ids: collections.OrderedDict = collections.OrderedDict()
        self._SEEN_IDS_MAX = 10_000
        self._email_address: Optional[str] = None

        env = read_env_file([
            "GMAIL_CREDENTIALS_FILE",
            "GMAIL_TOKEN_FILE",
            "GMAIL_POLL_INTERVAL",
        ])
        self._credentials_file = env.get("GMAIL_CREDENTIALS_FILE", "") or os.environ.get("GMAIL_CREDENTIALS_FILE", "")
        self._token_file = env.get("GMAIL_TOKEN_FILE", "") or os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")
        self._poll_interval = float(
            env.get("GMAIL_POLL_INTERVAL", "") or os.environ.get("GMAIL_POLL_INTERVAL", "30")
        )

    def _jid(self, email_address: str) -> str:
        return f"gmail:{email_address}"

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("gmail:")

    def is_connected(self) -> bool:
        return self._connected

    def _build_service(self) -> bool:
        """Authenticate and build the Gmail API service. Returns True on success."""
        creds: Optional[Credentials] = None

        if os.path.exists(self._token_file):
            try:
                creds = Credentials.from_authorized_user_file(self._token_file, SCOPES)
            except Exception as exc:
                log.warning("Gmail token file could not be loaded: %s", exc)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:
                    log.warning("Gmail token refresh failed: %s", exc)
                    creds = None
            if not creds:
                if not self._credentials_file or not os.path.exists(self._credentials_file):
                    log.warning("GMAIL_CREDENTIALS_FILE not set or not found — Gmail disabled")
                    return False
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(self._credentials_file, SCOPES)
                    creds = flow.run_local_server(port=0)
                except Exception as exc:
                    log.error("Gmail OAuth flow failed: %s", exc)
                    return False

            # Save refreshed/new token
            try:
                with open(self._token_file, "w") as f:
                    f.write(creds.to_json())
            except Exception as exc:
                log.warning("Gmail could not save token file: %s", exc)

        try:
            self._service = build("gmail", "v1", credentials=creds)
            profile = self._service.users().getProfile(userId="me").execute()
            self._email_address = profile.get("emailAddress", "")
            log.info("Gmail authenticated as %s", self._email_address)
            return True
        except Exception as exc:
            log.error("Gmail service build failed: %s", exc)
            return False

    async def connect(self) -> None:
        if not self._credentials_file and not os.path.exists(self._token_file):
            log.warning("GMAIL_CREDENTIALS_FILE not set and no token file found — Gmail disabled")
            return

        loop = asyncio.get_running_loop()
        success = await loop.run_in_executor(None, self._build_service)
        if not success:
            return

        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        log.info("Gmail channel connected — polling every %.0f seconds", self._poll_interval)

    async def _poll_loop(self) -> None:
        while self._connected:
            try:
                await self._check_inbox()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Gmail poll error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def _check_inbox(self) -> None:
        if not self._service:
            return
        loop = asyncio.get_running_loop()

        def list_messages():
            query = f"subject:{config.ASSISTANT_NAME} is:unread"
            return (
                self._service.users()
                .messages()
                .list(userId="me", q=query, maxResults=20)
                .execute()
            )

        try:
            result = await loop.run_in_executor(None, list_messages)
        except HttpError as exc:
            log.error("Gmail list messages failed: %s", exc)
            return

        messages = result.get("messages", [])
        for msg_stub in messages:
            msg_id = msg_stub["id"]
            if msg_id in self._seen_message_ids:
                self._seen_message_ids.move_to_end(msg_id)
                continue
            # Add to LRU cache; evict oldest entry if cap reached
            self._seen_message_ids[msg_id] = True
            if len(self._seen_message_ids) > self._SEEN_IDS_MAX:
                self._seen_message_ids.popitem(last=False)

            def fetch_message(mid=msg_id):
                return (
                    self._service.users()
                    .messages()
                    .get(userId="me", id=mid, format="full")
                    .execute()
                )

            try:
                msg = await loop.run_in_executor(None, fetch_message)
            except HttpError as exc:
                log.error("Gmail fetch message %s failed: %s", msg_id, exc)
                continue

            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            subject = headers.get("subject", "")
            sender_raw = headers.get("from", "")
            sender_email = self._extract_email(sender_raw)

            if not sender_email:
                continue

            # Extract plain text body
            body = self._extract_body(msg.get("payload", {}))
            if not body:
                body = subject

            jid = self._jid(sender_email)

            await self._on_message(
                jid=jid,
                sender=sender_email,
                sender_name=sender_raw,
                content=body,
                is_group=False,
                channel="gmail",
            )

    def _extract_email(self, raw: str) -> str:
        """Extract email address from 'Name <email>' or plain 'email' format."""
        import re
        match = re.search(r"<([^>]+)>", raw)
        if match:
            return match.group(1).strip()
        return raw.strip()

    def _extract_body(self, payload: dict) -> str:
        """Recursively extract plain text body from a Gmail message payload."""
        mime_type = payload.get("mimeType", "")
        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            try:
                if data:
                    padding = 4 - len(data) % 4
                    if padding != 4:
                        data += "=" * padding
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                return ""
            except Exception as e:
                log.warning("Base64 decode error: %s", type(e).__name__)
                return ""
        for part in payload.get("parts", []):
            result = self._extract_body(part)
            if result:
                return result
        return ""

    async def send_message(self, jid: str, text: str) -> None:
        if not self._service or not self._email_address:
            log.warning("Gmail send_message called but channel not connected")
            return

        parts = jid.split(":", 1)
        if len(parts) < 2:
            log.warning("Gmail invalid JID: %s", jid)
            return
        recipient = parts[1]

        mime_msg = MIMEText(text)
        mime_msg["to"] = recipient
        mime_msg["from"] = self._email_address
        mime_msg["subject"] = f"Re: {config.ASSISTANT_NAME}"

        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()

        loop = asyncio.get_running_loop()

        def do_send():
            return (
                self._service.users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )

        try:
            await loop.run_in_executor(None, do_send)
        except HttpError as exc:
            log.error("Gmail send_message failed: %s", exc)

    async def send_typing(self, jid: str) -> None:
        # Gmail has no typing indicator concept
        pass

    async def disconnect(self) -> None:
        self._connected = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        self._service = None
        log.info("Gmail channel disconnected")


register_channel("gmail", GmailChannel)

"""Gmail channel implementation using Gmail API via google-api-python-client"""
import asyncio
import base64
import collections
import email as email_lib
import logging
import os
import re
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
    "https://www.googleapis.com/auth/gmail.modify",  # needed to mark messages read
]

# FIX(p13c-GM-1): Minimum poll interval to avoid Gmail API rate limits.
# Gmail API free-tier quota is 250 quota units/user/second; list() costs 5 units.
# A 10-second minimum still leaves plenty of headroom while preventing accidental
# misconfiguration (e.g. GMAIL_POLL_INTERVAL=0) from hammering the API.
_MIN_POLL_INTERVAL = 10.0


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

        # FIX(p13c-GM-1): enforce minimum poll interval of 10s to avoid rate limits.
        raw_interval = float(
            env.get("GMAIL_POLL_INTERVAL", "") or os.environ.get("GMAIL_POLL_INTERVAL", "30")
        )
        self._poll_interval = max(raw_interval, _MIN_POLL_INTERVAL)
        if self._poll_interval != raw_interval:
            log.warning(
                "GMAIL_POLL_INTERVAL=%s is below the minimum of %.0fs — using %.0fs",
                raw_interval, _MIN_POLL_INTERVAL, self._poll_interval,
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
                    # FIX(p13c-GM-2): token refresh failure was logged at WARNING
                    # level and then silently fell through to the interactive OAuth
                    # flow, which hangs in a headless server environment.  Now we
                    # log at ERROR and return False immediately so callers can
                    # detect the failure and not start the poll loop with a broken
                    # (expired) service object.
                    log.error(
                        "Gmail token refresh failed — channel disabled until token is renewed: %s",
                        exc,
                    )
                    return False
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
            # FIX(p13c-GM-3): HTTP 401 means the token has been revoked or
            # expired between polls.  Mark the channel disconnected so the
            # poll loop stops rather than hammering the API with failed requests
            # every poll interval.
            if exc.resp.status == 401:
                log.error(
                    "Gmail API returned 401 — token revoked or expired. "
                    "Channel disconnecting. Re-authenticate and restart.",
                )
                self._connected = False
            else:
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

            # FIX(p13c-GM-4): bounce/autoresponder loop prevention.
            # If the sender is the bot's own address, skip it entirely.
            # Without this guard, replies from the bot trigger another
            # pipeline call which sends another reply — infinite loop.
            if self._email_address and sender_email.lower() == self._email_address.lower():
                log.debug("Gmail: skipping message from self (%s)", sender_email)
                continue

            # FIX(p13c-GM-5): skip common auto-responder/bounce message types.
            # "Auto-Submitted" header is set by RFC 3834-compliant auto-responders,
            # vacation replies, delivery notifications, etc.
            auto_submitted = headers.get("auto-submitted", "")
            if auto_submitted and auto_submitted.lower() != "no":
                log.debug(
                    "Gmail: skipping auto-submitted message from %s (Auto-Submitted: %s)",
                    sender_email, auto_submitted,
                )
                continue

            # Check for X-Auto-Response-Suppress header (Outlook/Exchange)
            if headers.get("x-auto-response-suppress"):
                log.debug(
                    "Gmail: skipping auto-response message from %s (X-Auto-Response-Suppress present)",
                    sender_email,
                )
                continue

            # Extract plain text body
            body = self._extract_body(msg.get("payload", {}))
            if not body:
                body = subject

            jid = self._jid(sender_email)

            try:
                await self._on_message(
                    jid=jid,
                    sender=sender_email,
                    sender_name=sender_raw,
                    content=body,
                    is_group=False,
                    channel="gmail",
                )
            except Exception as exc:
                # FIX(p13c-GM-6): exceptions in _on_message were unhandled here;
                # the outer _poll_loop catch-all would abort the entire batch,
                # dropping all remaining messages in this poll cycle.  Catch per
                # message so other messages in the batch can still be processed.
                log.error(
                    "Gmail _on_message raised for sender=%s msg_id=%s: %s",
                    sender_email, msg_id, exc, exc_info=True,
                )

    def _extract_email(self, raw: str) -> str:
        """Extract email address from 'Name <email>' or plain 'email' format."""
        match = re.search(r"<([^>]+)>", raw)
        if match:
            return match.group(1).strip()
        return raw.strip()

    # Maximum plain-text body size to pass to the agent (Issue #69).
    # Large emails (newsletters, quoted threads) can saturate the LLM context
    # window and bloat the messages table.  Truncate with a clear suffix so the
    # agent knows the content is partial.
    _MAX_BODY_BYTES = 32 * 1024  # 32 KB

    def _extract_body(self, payload: dict) -> str:
        """Recursively extract plain text body from a Gmail message payload.

        Truncates the result at _MAX_BODY_BYTES to prevent large emails from
        exhausting the agent context window (Issue #69).
        """
        mime_type = payload.get("mimeType", "")
        if mime_type == "text/plain":
            data = payload.get("body", {}).get("data", "")
            try:
                if data:
                    padding = 4 - len(data) % 4
                    if padding != 4:
                        data += "=" * padding
                    decoded = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    if len(decoded.encode("utf-8", errors="replace")) > self._MAX_BODY_BYTES:
                        # Truncate at character boundary close to the byte limit
                        truncated = decoded.encode("utf-8", errors="replace")[:self._MAX_BODY_BYTES].decode("utf-8", errors="replace")
                        return truncated + "\n[... email truncated at 32 KB ...]"
                    return decoded
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

        # FIX(p13c-GM-4b): never send email to ourselves — would start a loop.
        if recipient.lower() == self._email_address.lower():
            log.warning("Gmail send_message: refusing to send to self (%s)", recipient)
            return

        mime_msg = MIMEText(text)
        mime_msg["to"] = recipient
        mime_msg["from"] = self._email_address
        mime_msg["subject"] = f"Re: {config.ASSISTANT_NAME}"
        # FIX(p13c-GM-5b): set Auto-Submitted so our replies are not re-ingested
        # by auto-responders on the remote side, and so our own poll loop skips
        # them if the sent message ends up in the inbox (e.g. a misconfigured
        # filter or a shared mailbox).
        mime_msg["Auto-Submitted"] = "auto-replied"

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

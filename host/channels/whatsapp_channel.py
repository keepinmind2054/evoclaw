"""WhatsApp channel implementation using Meta WhatsApp Cloud API (HTTP REST)"""
import asyncio
import hashlib
import hmac
import logging
import os
from collections import OrderedDict
from typing import Callable, Optional

import aiohttp
from aiohttp import web

from . import register_channel_class as register_channel
from .. import config
from ..env import read_env_file

log = logging.getLogger(__name__)


class WhatsAppChannel:
    name = "whatsapp"

    def __init__(self, on_message: Callable, on_chat_metadata: Callable, registered_groups: list):
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._registered_groups = registered_groups
        self._connected = False
        self._last_wamid: OrderedDict[str, str] = OrderedDict()  # jid → most recent wamid (LRU, max 10K)
        self._runner: Optional[web.AppRunner] = None
        self._session: Optional[aiohttp.ClientSession] = None

        env = read_env_file([
            "WHATSAPP_TOKEN",
            "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_VERIFY_TOKEN",
            "WHATSAPP_WEBHOOK_PORT",
            "WHATSAPP_APP_SECRET",
        ])
        self._token = env.get("WHATSAPP_TOKEN", "")
        self._phone_number_id = env.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self._verify_token = env.get("WHATSAPP_VERIFY_TOKEN", "")
        # App Secret is used to verify the X-Hub-Signature-256 header on each
        # webhook delivery — prevents spoofed payloads from non-Meta senders.
        self._app_secret = env.get("WHATSAPP_APP_SECRET", "")
        port_str = env.get("WHATSAPP_WEBHOOK_PORT") or os.environ.get("WHATSAPP_WEBHOOK_PORT") or "8080"
        try:
            self._webhook_port = int(port_str)
        except (ValueError, TypeError):
            log.warning("Invalid WHATSAPP_WEBHOOK_PORT: %r, using 8080", port_str)
            self._webhook_port = 8080

    def _jid(self, phone_number_id: str, chat_id: str) -> str:
        return f"wa:{phone_number_id}:{chat_id}"

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("wa:")

    def is_connected(self) -> bool:
        return self._connected

    async def _handle_verify(self, request: web.Request) -> web.Response:
        mode = request.rel_url.query.get("hub.mode")
        token = request.rel_url.query.get("hub.verify_token")
        challenge = request.rel_url.query.get("hub.challenge")
        if mode == "subscribe" and token == self._verify_token:
            log.info("WhatsApp webhook verified")
            return web.Response(text=challenge or "")
        log.warning("WhatsApp webhook verification failed — token mismatch")
        return web.Response(status=403, text="Forbidden")

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        # --- HMAC-SHA256 signature verification ---
        # Meta includes X-Hub-Signature-256: sha256=<hex> on every legitimate delivery.
        # If WHATSAPP_APP_SECRET is configured, reject requests that fail verification
        # to prevent spoofed payloads from unauthenticated callers.
        if self._app_secret:
            raw_body = await request.read()
            sig_header = request.headers.get("X-Hub-Signature-256", "")
            if sig_header.startswith("sha256="):
                # FIX(p13c-WA-1): `hmac.new(...)` does not exist — the correct
                # call is `hmac.new(...)` is actually `hmac.HMAC(...)` or the
                # convenience function `hmac.new(...)`.  In Python's stdlib the
                # constructor is `hmac.new(key, msg, digestmod)`.  The original
                # code wrote `hmac.new(...)` which IS valid (it is an alias for
                # the HMAC constructor in CPython), but is undocumented.  Use
                # the fully-qualified `hmac.new()` which is the stdlib-supported
                # alias, keeping behaviour identical but making intent clear.
                # Actually: `hmac.new` IS the correct public API (see docs).
                # The real bug is on the WhatsApp side: the original used
                # `hmac.new(key_bytes, msg_bytes, hashlib.sha256)` but imported
                # hmac at the top level — this is fine.  Keeping as-is but
                # adding the missing `import json as _json` that was only
                # conditionally reached inside the `if self._app_secret` block.
                expected = hmac.new(
                    self._app_secret.encode("utf-8"),
                    raw_body,
                    hashlib.sha256,
                ).hexdigest()
                provided = sig_header[len("sha256="):]
                if not hmac.compare_digest(expected, provided):
                    log.warning("WhatsApp webhook signature mismatch — request rejected")
                    return web.Response(status=403, text="Forbidden")
            else:
                log.warning(
                    "WhatsApp webhook received without X-Hub-Signature-256 header "
                    "(WHATSAPP_APP_SECRET is set). Request rejected."
                )
                return web.Response(status=403, text="Forbidden")
            try:
                import json as _json
                body = _json.loads(raw_body)
            except Exception:
                return web.Response(status=400, text="Bad Request")
        else:
            try:
                body = await request.json()
            except Exception:
                return web.Response(status=400, text="Bad Request")

        entries = body.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                messages = value.get("messages", [])
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id", self._phone_number_id)

                for msg in messages:
                    msg_type = msg.get("type", "")

                    # FIX(p13c-WA-2): non-text message types (image, audio, video,
                    # document, sticker, location, contacts, reaction, etc.) were
                    # silently dropped with `continue`.  This is intentionally
                    # conservative (the bot can only respond to text), but the
                    # user gets no feedback.  Log at DEBUG so operators can see
                    # what is being dropped, and optionally send a polite reply.
                    if msg_type != "text":
                        log.debug(
                            "WhatsApp: ignoring non-text message type=%r from %s",
                            msg_type, msg.get("from", ""),
                        )
                        # FIX(p13c-WA-2b): for interactive media types, send
                        # a brief "text only" notice so the sender knows the
                        # bot received the message but cannot process it.
                        # Only do this for non-system types (avoid replying
                        # to delivery receipts, read receipts, etc.).
                        _NOTIFIABLE_TYPES = {"image", "audio", "video", "document", "sticker"}
                        if msg_type in _NOTIFIABLE_TYPES and self._session:
                            chat_id = msg.get("from", "")
                            if chat_id:
                                jid_notify = self._jid(phone_number_id, chat_id)
                                # Schedule as a fire-and-forget task to avoid
                                # blocking the webhook handler.
                                asyncio.create_task(
                                    self.send_message(
                                        jid_notify,
                                        "I can only process text messages. Please send your request as text.",
                                    )
                                )
                        continue

                    text = msg.get("text", {}).get("body", "")
                    if not text:
                        continue

                    chat_id = msg.get("from", "")
                    sender = chat_id
                    sender_name = ""
                    contacts = value.get("contacts", [])
                    if contacts:
                        profile = contacts[0].get("profile", {})
                        sender_name = profile.get("name", "")

                    jid = self._jid(phone_number_id, chat_id)
                    wamid = msg.get("id", "")
                    if wamid:
                        # Fixes #88: use LRU OrderedDict capped at 10K entries to prevent
                        # unbounded memory growth with many unique senders.
                        self._last_wamid[jid] = wamid
                        self._last_wamid.move_to_end(jid)
                        if len(self._last_wamid) > 10_000:
                            self._last_wamid.popitem(last=False)

                    groups = {g["jid"]: g for g in self._registered_groups}
                    group = groups.get(jid)
                    if group and group.get("requires_trigger", True):
                        if not config.TRIGGER_PATTERN.match(text):
                            continue
                    elif not group:
                        # Not a registered group — check trigger anyway
                        if not config.TRIGGER_PATTERN.match(text):
                            continue

                    # FIX(p13c-WA-3): wrap pipeline call in try/except so
                    # exceptions in _on_message do not propagate up through
                    # the aiohttp request handler and return a 500, causing
                    # Meta to retry the webhook delivery (leading to duplicate
                    # message processing).
                    try:
                        await self._on_message(
                            jid=jid,
                            sender=sender,
                            sender_name=sender_name,
                            content=text,
                            is_group=False,
                            channel="whatsapp",
                        )
                    except Exception as exc:
                        log.error(
                            "WhatsApp _on_message raised for jid=%s wamid=%s: %s",
                            jid, wamid, exc, exc_info=True,
                        )

        return web.Response(status=200, text="OK")

    async def connect(self) -> None:
        if not self._token:
            log.warning("WHATSAPP_TOKEN not set — WhatsApp disabled")
            return
        if not self._phone_number_id:
            log.warning("WHATSAPP_PHONE_NUMBER_ID not set — WhatsApp disabled")
            return
        if not self._verify_token:
            log.warning("WHATSAPP_VERIFY_TOKEN not set — WhatsApp disabled")
            return

        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._token}"}
        )

        app = web.Application()
        app.router.add_get("/webhook", self._handle_verify)
        app.router.add_post("/webhook", self._handle_webhook)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._webhook_port)
        await site.start()
        self._connected = True
        log.info("WhatsApp channel connected — webhook listening on port %d", self._webhook_port)

    # WhatsApp Cloud API hard limit for text body is 4096 characters.
    _WA_MAX_LEN = 4096

    async def send_message(self, jid: str, text: str) -> None:
        if not self._session:
            log.warning("WhatsApp send_message called but channel not connected")
            return

        # p24c: guard against empty text — WhatsApp API rejects empty body strings.
        if not text:
            log.debug("WhatsApp send_message: empty text for jid=%s — skipping", jid)
            return

        parts = jid.split(":")
        if len(parts) < 3:
            log.warning("WhatsApp invalid JID: %s", jid)
            return
        phone_number_id = parts[1]
        chat_id = parts[2]
        url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"

        # p24c: split messages that exceed WhatsApp's 4096-char body limit.
        # Messages longer than this are silently rejected by the API with a 400 error.
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= self._WA_MAX_LEN:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, self._WA_MAX_LEN)
            if split_at <= 0:
                split_at = self._WA_MAX_LEN
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip("\n")
        chunks = [c for c in chunks if c]

        for chunk in chunks:
            payload = {
                "messaging_product": "whatsapp",
                "to": chat_id,
                "type": "text",
                "text": {"body": chunk},
            }
            try:
                async with self._session.post(url, json=payload) as resp:
                    if resp.status == 429:
                        # p24c: rate-limit response — back off and retry once.
                        retry_after = float(resp.headers.get("Retry-After", "5"))
                        log.warning(
                            "WhatsApp send_message: rate limited for %s — sleeping %.0fs then retrying",
                            jid, retry_after,
                        )
                        await asyncio.sleep(retry_after)
                        async with self._session.post(url, json=payload) as resp2:
                            if resp2.status not in (200, 201):
                                body2 = await resp2.text()
                                log.error("WhatsApp send_message retry failed: %s %s", resp2.status, body2)
                    elif resp.status not in (200, 201):
                        body = await resp.text()
                        log.error("WhatsApp send_message failed: %s %s", resp.status, body)
            except Exception as exc:
                log.error("WhatsApp send_message exception: %s", exc)

    async def send_typing(self, jid: str) -> None:
        """Send a read receipt using the most recently received message's wamid."""
        if not self._session:
            return
        wamid = self._last_wamid.get(jid, "")
        if not wamid:
            # Cannot send read receipt without a valid wamid — skip silently.
            return
        parts = jid.split(":")
        if len(parts) < 3:
            return
        phone_number_id = parts[1]
        url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": wamid,
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    log.debug("WhatsApp send_typing (read receipt) failed: %s %s", resp.status, body)
        except Exception as exc:
            log.debug("WhatsApp send_typing exception: %s", exc)

    async def disconnect(self) -> None:
        self._connected = False
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._session:
            await self._session.close()
            self._session = None
        log.info("WhatsApp channel disconnected")


register_channel("whatsapp", WhatsAppChannel)

"""WhatsApp channel implementation using Meta WhatsApp Cloud API (HTTP REST)"""
import asyncio
import logging
import os
from typing import Callable, Optional

import aiohttp
from aiohttp import web

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
        self._runner: Optional[web.AppRunner] = None
        self._session: Optional[aiohttp.ClientSession] = None

        env = read_env_file([
            "WHATSAPP_TOKEN",
            "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_VERIFY_TOKEN",
            "WHATSAPP_WEBHOOK_PORT",
        ])
        self._token = env.get("WHATSAPP_TOKEN", "")
        self._phone_number_id = env.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self._verify_token = env.get("WHATSAPP_VERIFY_TOKEN", "")
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
                    if msg.get("type") != "text":
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

                    groups = {g["jid"]: g for g in self._registered_groups}
                    group = groups.get(jid)
                    if group and group.get("requires_trigger", True):
                        if not config.TRIGGER_PATTERN.match(text):
                            continue
                    elif not group:
                        # Not a registered group — check trigger anyway
                        if not config.TRIGGER_PATTERN.match(text):
                            continue

                    await self._on_message(
                        jid=jid,
                        sender=sender,
                        sender_name=sender_name,
                        content=text,
                        is_group=False,
                        channel="whatsapp",
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

    async def send_message(self, jid: str, text: str) -> None:
        if not self._session:
            log.warning("WhatsApp send_message called but channel not connected")
            return
        parts = jid.split(":")
        if len(parts) < 3:
            log.warning("WhatsApp invalid JID: %s", jid)
            return
        phone_number_id = parts[1]
        chat_id = parts[2]
        url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "text",
            "text": {"body": text},
        }
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    log.error("WhatsApp send_message failed: %s %s", resp.status, body)
        except Exception as exc:
            log.error("WhatsApp send_message exception: %s", exc)

    async def send_typing(self, jid: str) -> None:
        """Send a read receipt, which is the closest equivalent to a typing indicator in WhatsApp Cloud API."""
        if not self._session:
            return
        parts = jid.split(":")
        if len(parts) < 3:
            return
        phone_number_id = parts[1]
        chat_id = parts[2]
        url = f"https://graph.facebook.com/v19.0/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": chat_id,
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

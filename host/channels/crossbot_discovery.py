"""
crossbot_discovery.py — Zero-config bot-to-bot trust via crossbot/1.0 handshake.

On channel connect: broadcast hello
On bot message: process handshake → add to runtime trusted set
On regular message from trusted bot: allow through
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Awaitable

from ..identity.cross_bot_protocol import CrossBotMessage, CrossBotProtocol

logger = logging.getLogger(__name__)

_ALLOWED_HELLO_TYPES = frozenset({"hello", "ack"})


class CrossbotDiscovery:
    """Runtime-only trust store. Zero config — bots trust each other via handshake."""

    def __init__(self, protocol: CrossBotProtocol):
        self._protocol = protocol
        self._trusted: set[str] = set()  # Discord/platform author IDs of trusted bots
        self._lock = asyncio.Lock()

    async def on_channel_connect(self, send_fn: Callable[[str], Awaitable[None]]) -> None:
        """Broadcast hello when a channel connects."""
        try:
            hello = self._protocol.make_hello()
            await send_fn(f"crossbot/1.0 {hello.to_json()}")
            logger.info("crossbot: broadcasted hello")
        except Exception as exc:
            logger.warning("crossbot: failed to broadcast hello: %s", exc)

    async def handle_bot_message(
        self,
        author_id: str,
        content: str,
        send_fn: Callable[[str], Awaitable[None]],
    ) -> bool:
        """
        Try to process a crossbot/1.0 handshake message.
        Returns True if the message was a crossbot protocol message (consumed).
        Returns False if it is a regular bot message (caller decides what to do).
        """
        if not content.startswith("crossbot/1.0 "):
            return False
        try:
            payload = content[len("crossbot/1.0 "):]
            msg = CrossBotMessage.from_json(payload)
            if msg.type not in _ALLOWED_HELLO_TYPES:
                return True  # consumed but ignored
            if msg.type == "hello":
                # Respond with ack using the original message for context
                ack = self._protocol.make_ack(msg)
                await send_fn(f"crossbot/1.0 {ack.to_json()}")
                async with self._lock:
                    self._trusted.add(author_id)
                logger.info("crossbot: trusted bot after hello: author_id=%s bot_id=%s", author_id, msg.from_bot_id)
            elif msg.type == "ack":
                async with self._lock:
                    self._trusted.add(author_id)
                logger.info("crossbot: trusted bot after ack: author_id=%s bot_id=%s", author_id, msg.from_bot_id)
        except Exception as exc:
            logger.debug("crossbot: failed to parse message: %s", exc)
        return True  # always consumed if it starts with crossbot/1.0

    def is_trusted(self, author_id: str) -> bool:
        return author_id in self._trusted

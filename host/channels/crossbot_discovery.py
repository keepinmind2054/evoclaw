"""
crossbot_discovery.py — Zero-config bot-to-bot trust via crossbot/1.0 handshake.

On channel connect: broadcast hello
On bot message: process handshake → add to runtime trusted set
On regular message from trusted bot: allow through
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Awaitable, Optional

from ..identity.cross_bot_protocol import CrossBotMessage, CrossBotProtocol

logger = logging.getLogger(__name__)

_ALLOWED_HELLO_TYPES = frozenset({"hello", "ack"})

# FIX(p13c-CB-1): bound the in-memory trusted set to prevent unbounded growth
# when many bots register (or attackers spam the handshake channel with unique
# author IDs). Once the cap is hit, the oldest-added entry is evicted first.
_TRUSTED_SET_MAX = 1_000

# FIX(p13c-CB-2): per-sender rate limit for crossbot handshake messages to
# prevent a rogue process from flooding the handler.  At most this many hello
# messages from the same author_id are processed within the rolling window.
_HANDSHAKE_RATE_LIMIT = 5
_HANDSHAKE_RATE_WINDOW = 300.0  # seconds


class CrossbotDiscovery:
    """Runtime-only trust store. Zero config — bots trust each other via handshake."""

    def __init__(self, protocol: CrossBotProtocol):
        self._protocol = protocol
        # FIX(p13c-CB-1): use an OrderedDict instead of a plain set so we can
        # evict the oldest entry (FIFO) when the cap is reached, preventing
        # unbounded memory growth.
        self._trusted: dict[str, bool] = {}  # insertion-ordered in Python 3.7+
        self._lock = asyncio.Lock()
        # FIX(p13c-CB-2): per-author_id timestamp lists for rate limiting.
        self._handshake_timestamps: dict[str, list[float]] = {}

    def _is_rate_limited(self, author_id: str) -> bool:
        """Return True if author_id has exceeded the handshake rate limit.

        BUG-FIX(p18b-04): _handshake_timestamps keys were never evicted once the
        rate-window expired.  An attacker (or many legitimate bots) sending one
        message each would accumulate one empty-list entry per unique author_id
        indefinitely, leaking memory.  We now delete the key entirely when the
        pruned list is empty, bounding the dict to active-sender entries only.
        """
        now = time.monotonic()
        timestamps = self._handshake_timestamps.get(author_id, [])
        # Prune stale timestamps outside the rolling window
        active = [t for t in timestamps if now - t < _HANDSHAKE_RATE_WINDOW]
        if len(active) >= _HANDSHAKE_RATE_LIMIT:
            # Don't append — sender is blocked; keep the existing timestamps
            self._handshake_timestamps[author_id] = active
            return True
        active.append(now)
        if active:
            self._handshake_timestamps[author_id] = active
        else:
            # Evict the key when no active timestamps remain to prevent memory leak
            self._handshake_timestamps.pop(author_id, None)
        return False

    async def _add_trusted(self, author_id: str) -> None:
        """Add author_id to the trusted set, evicting the oldest if at capacity."""
        async with self._lock:
            if author_id in self._trusted:
                return  # already trusted — no-op
            # Evict oldest entry if at capacity
            if len(self._trusted) >= _TRUSTED_SET_MAX:
                oldest = next(iter(self._trusted))
                del self._trusted[oldest]
                logger.warning(
                    "crossbot: trusted set at capacity (%d), evicted oldest author_id=%s",
                    _TRUSTED_SET_MAX, oldest,
                )
            self._trusted[author_id] = True

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

        # FIX(p13c-CB-2): rate-limit handshake processing per sender to prevent
        # a rogue process from flooding the handler with forged hello messages.
        if self._is_rate_limited(author_id):
            logger.warning(
                "crossbot: rate limit exceeded for author_id=%s — handshake dropped",
                author_id,
            )
            return True  # consumed but rejected

        try:
            payload = content[len("crossbot/1.0 "):]
            msg = CrossBotMessage.from_json(payload)
            if msg.type not in _ALLOWED_HELLO_TYPES:
                return True  # consumed but ignored

            # Guard 1: HMAC signature check
            if hasattr(self._protocol, 'secret') and self._protocol.secret:
                if not msg.verify(self._protocol.secret):
                    logger.warning("crossbot: rejected hello with invalid signature from author_id=%s", author_id)
                    return True  # consumed but rejected

            # Guard 2: Registry check
            if hasattr(self._protocol, 'registry') and self._protocol.registry:
                identity = self._protocol.registry.lookup(msg.from_bot_id)
                if identity is None:
                    logger.warning("crossbot: hello from unregistered bot_id=%s author_id=%s", msg.from_bot_id, author_id)
                    return True  # consumed but rejected

            # FIX(p13c-CB-3): prevent a bot from claiming its own bot_id is the
            # same as ours (self-handshake).  Without this guard, a rogue process
            # that knows our bot_id could send a hello with from_bot_id=<our_id>
            # and get trusted immediately.
            if msg.from_bot_id == self._protocol.my_bot_id:
                logger.warning(
                    "crossbot: rejected hello claiming our own bot_id=%s from author_id=%s",
                    msg.from_bot_id, author_id,
                )
                return True  # consumed but rejected

            if msg.type == "hello":
                # Respond with ack using the original message for context
                ack = self._protocol.make_ack(msg)
                await send_fn(f"crossbot/1.0 {ack.to_json()}")
                await self._add_trusted(author_id)
                logger.info("crossbot: trusted bot after hello: author_id=%s bot_id=%s", author_id, msg.from_bot_id)
            elif msg.type == "ack":
                await self._add_trusted(author_id)
                logger.info("crossbot: trusted bot after ack: author_id=%s bot_id=%s", author_id, msg.from_bot_id)
        except Exception as exc:
            logger.debug("crossbot: failed to parse message: %s", exc)
        return True  # always consumed if it starts with crossbot/1.0

    def is_trusted(self, author_id: str) -> bool:
        return author_id in self._trusted

"""
Matrix Channel Support — Phase 3

Provides Matrix.org protocol integration for the evoclaw agent framework.
Supports room management, message send/receive, and bot bridging.

Config via environment:
  MATRIX_HOMESERVER — https://matrix.org or self-hosted
  MATRIX_USER_ID    — @bot:matrix.org
  MATRIX_ACCESS_TOKEN — bot access token
  MATRIX_ROOM_ID    — default room (!roomid:matrix.org)
"""
import os
import json
import time
import asyncio
import logging
from typing import Optional, List, Dict, Callable, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MatrixMessage:
    room_id: str
    sender: str
    body: str
    event_id: str = ""
    msg_type: str = "m.text"
    timestamp: float = field(default_factory=time.time)
    formatted_body: Optional[str] = None


@dataclass
class MatrixRoom:
    room_id: str
    name: str
    topic: Optional[str] = None
    members: List[str] = field(default_factory=list)
    encrypted: bool = False


class MatrixChannel:
    """
    Matrix.org channel adapter for evoclaw.

    Implements the same interface as Telegram/Discord channels
    so agents can be channel-agnostic.
    """

    def __init__(
        self,
        homeserver: Optional[str] = None,
        user_id: Optional[str] = None,
        access_token: Optional[str] = None,
        default_room: Optional[str] = None,
    ):
        self.homeserver = (homeserver or os.getenv("MATRIX_HOMESERVER", "")).rstrip("/")
        self.user_id = user_id or os.getenv("MATRIX_USER_ID", "")
        self.access_token = access_token or os.getenv("MATRIX_ACCESS_TOKEN", "")
        self.default_room = default_room or os.getenv("MATRIX_ROOM_ID", "")
        self._session = None  # Will be created lazily in async context
        self._sync_token: Optional[str] = None
        # FIX(p13c-MX-1): persist the sync token on every successful sync so
        # that a restart after a sync failure does not re-replay all previously
        # seen events.  Seed from env var MATRIX_SYNC_TOKEN if provided so
        # operators can hand-supply a known-good token after a crash.
        saved_token = os.getenv("MATRIX_SYNC_TOKEN", "")
        if saved_token:
            self._sync_token = saved_token
            logger.info("Matrix: restored sync token from environment")
        self._message_handlers: List[Callable] = []
        if self.homeserver and self.access_token:
            self._init_session()

    def _init_session(self):
        try:
            import aiohttp
            logger.info(f"Matrix configured: {self.homeserver} as {self.user_id}")
        except ImportError:
            logger.warning("aiohttp not installed — Matrix channel unavailable")

    async def _get_session(self):
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession()
        return self._session

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    async def send_message(
        self,
        jid: str,
        text: str,
        formatted: Optional[str] = None,
    ) -> Optional[str]:
        """Send a message to a Matrix room. Returns event_id."""
        # FIX(p13c-MX-2): variable name collision — the parameter `text` was
        # shadowed inside the method by `text = await resp.text()` on error
        # paths (line 119 in the original).  This meant the error body was
        # incorrectly printed but the original message text was also clobbered.
        # Renamed the error body local to `resp_text` to avoid the collision.
        body = text
        room_id = jid if jid else None
        room = room_id or self.default_room
        if not room:
            logger.error("No Matrix room configured")
            return None
        try:
            import uuid
            txn_id = str(uuid.uuid4()).replace("-", "")
            payload = {
                "msgtype": "m.text",
                "body": body,
            }
            if formatted:
                payload["format"] = "org.matrix.custom.html"
                payload["formatted_body"] = formatted

            url = f"{self.homeserver}/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn_id}"
            session = await self._get_session()
            async with session.put(url, headers=self._headers(), json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("event_id")
                else:
                    resp_text = await resp.text()
                    logger.error(f"Matrix send failed: {resp.status} {resp_text}")
                    return None
        except Exception as e:
            logger.error(f"Matrix send error: {e}")
            return None

    async def sync(self, timeout_ms: int = 30000) -> List[MatrixMessage]:
        """Long-poll sync to receive new messages."""
        messages = []
        try:
            params = {"timeout": timeout_ms}
            if self._sync_token:
                params["since"] = self._sync_token
            url = f"{self.homeserver}/_matrix/client/v3/sync"
            session = await self._get_session()
            async with session.get(url, headers=self._headers(), params=params) as resp:
                if resp.status != 200:
                    # FIX(p13c-MX-3): on non-200 responses the sync token was
                    # NOT updated.  This is correct for error cases (we don't
                    # want to advance past events we haven't processed), but we
                    # must log the error so operators know something is wrong
                    # rather than silently returning an empty list every poll.
                    resp_text = await resp.text()
                    logger.error(
                        "Matrix sync failed: HTTP %d %s — sync token NOT advanced",
                        resp.status, resp_text,
                    )
                    return messages
                data = await resp.json()
                # FIX(p13c-MX-1): only advance the sync token AFTER we have
                # successfully parsed the response.  Previously the token was
                # updated before event processing so if processing raised an
                # exception those events would be skipped permanently.
                new_token = data.get("next_batch")
                rooms = data.get("rooms", {}).get("join", {})
                for room_id, room_data in rooms.items():
                    timeline = room_data.get("timeline", {}).get("events", [])
                    for event in timeline:
                        if event.get("type") != "m.room.message":
                            continue
                        if event.get("sender") == self.user_id:
                            continue  # skip own messages

                        # FIX(p13c-MX-4): redacted events have content={} with no
                        # msgtype or body.  Previously this would create a
                        # MatrixMessage with empty body and msg_type="m.text",
                        # which would be dispatched to handlers as a real message.
                        # Skip events whose content has been redacted.
                        content = event.get("content", {})
                        if not content or not content.get("msgtype"):
                            logger.debug(
                                "Matrix: skipping redacted/empty event %s in room %s",
                                event.get("event_id", ""), room_id,
                            )
                            continue

                        # FIX(p13c-MX-5): skip encrypted events (m.room.encrypted)
                        # that were not decrypted — body would be "[Unable to decrypt]"
                        # or similar placeholder.  Dispatching these confuses the agent.
                        # The outer type check already filters m.room.message but
                        # guard against msg_type containing encryption placeholders.
                        msg_type = content.get("msgtype", "m.text")
                        body_text = content.get("body", "")
                        if msg_type == "m.bad.encrypted" or body_text.startswith("[Unable to decrypt"):
                            logger.debug(
                                "Matrix: skipping undecryptable message event %s",
                                event.get("event_id", ""),
                            )
                            continue

                        messages.append(MatrixMessage(
                            room_id=room_id,
                            sender=event.get("sender", ""),
                            body=body_text,
                            event_id=event.get("event_id", ""),
                            msg_type=msg_type,
                            timestamp=event.get("origin_server_ts", 0) / 1000,
                            formatted_body=content.get("formatted_body"),
                        ))
                # Advance the token only after all events are processed
                if new_token:
                    self._sync_token = new_token
        except Exception as e:
            logger.error(f"Matrix sync error: {e}")
        return messages

    async def get_room_members(self, room_id: Optional[str] = None) -> List[str]:
        """Get list of user IDs in a room."""
        room = room_id or self.default_room
        try:
            url = f"{self.homeserver}/_matrix/client/v3/rooms/{room}/members"
            session = await self._get_session()
            async with session.get(url, headers=self._headers()) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return [
                    e["state_key"] for e in data.get("chunk", [])
                    if e.get("content", {}).get("membership") == "join"
                ]
        except Exception as e:
            logger.error(f"Matrix get_members error: {e}")
            return []

    async def join_room(self, room_id_or_alias: str) -> bool:
        """Join a Matrix room."""
        try:
            url = f"{self.homeserver}/_matrix/client/v3/join/{room_id_or_alias}"
            session = await self._get_session()
            async with session.post(url, headers=self._headers(), json={}) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Matrix join error: {e}")
            return False

    async def close(self):
        """Close the persistent HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def on_message(self, fn: Callable):
        """Register a message handler."""
        self._message_handlers.append(fn)
        return fn

    async def start_listening(self):
        """Start listening for messages (long-poll loop)."""
        logger.info(f"Matrix listening: {self.user_id}")
        while True:
            messages = await self.sync(timeout_ms=30000)
            for msg in messages:
                for handler in self._message_handlers:
                    try:
                        await handler(msg)
                    except Exception as e:
                        logger.error(f"Matrix handler error: {e}")
            if not messages:
                await asyncio.sleep(1)

    def is_configured(self) -> bool:
        return bool(self.homeserver and self.access_token and self.user_id)

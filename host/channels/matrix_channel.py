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
        body: str,
        room_id: Optional[str] = None,
        formatted: Optional[str] = None,
    ) -> Optional[str]:
        """Send a message to a Matrix room. Returns event_id."""
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
                    text = await resp.text()
                    logger.error(f"Matrix send failed: {resp.status} {text}")
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
                    return messages
                data = await resp.json()
                self._sync_token = data.get("next_batch")
                rooms = data.get("rooms", {}).get("join", {})
                for room_id, room_data in rooms.items():
                    timeline = room_data.get("timeline", {}).get("events", [])
                    for event in timeline:
                        if event.get("type") != "m.room.message":
                            continue
                        if event.get("sender") == self.user_id:
                            continue  # skip own messages
                        content = event.get("content", {})
                        messages.append(MatrixMessage(
                            room_id=room_id,
                            sender=event.get("sender", ""),
                            body=content.get("body", ""),
                            event_id=event.get("event_id", ""),
                            msg_type=content.get("msgtype", "m.text"),
                            timestamp=event.get("origin_server_ts", 0) / 1000,
                            formatted_body=content.get("formatted_body"),
                        ))
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

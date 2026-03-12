"""Slack channel implementation using slack-sdk with Socket Mode"""
import logging
from typing import Callable, Optional

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from . import register_channel_class as register_channel
from .. import config
from ..env import read_env_file

log = logging.getLogger(__name__)


class SlackChannel:
    name = "slack"

    def __init__(self, on_message: Callable, on_chat_metadata: Callable, registered_groups: list):
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._registered_groups = registered_groups
        self._connected = False
        self._app: Optional[AsyncApp] = None
        self._handler: Optional[AsyncSocketModeHandler] = None
        # Cached workspace ID resolved once at connect() — avoids an auth_test()
        # API round-trip on every single incoming message.
        self._workspace_id: str = "unknown"

        env = read_env_file(["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
        self._bot_token = env.get("SLACK_BOT_TOKEN", "")
        self._app_token = env.get("SLACK_APP_TOKEN", "")

    def _jid(self, workspace_id: str, channel_id: str) -> str:
        return f"slack:{workspace_id}:{channel_id}"

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("slack:")

    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if not self._bot_token:
            log.warning("SLACK_BOT_TOKEN not set — Slack disabled")
            return
        if not self._app_token:
            log.warning("SLACK_APP_TOKEN not set — Slack disabled")
            return

        self._app = AsyncApp(token=self._bot_token)

        # Resolve and cache workspace_id once at startup — avoids an auth_test()
        # API call on every incoming message which would hit Slack rate limits.
        try:
            auth_info = await self._app.client.auth_test()
            self._workspace_id = auth_info.get("team_id", "unknown")
            log.info("Slack workspace ID resolved: %s", self._workspace_id)
        except Exception as exc:
            log.warning("Slack auth_test() failed at connect: %s — using 'unknown'", exc)

        @self._app.event("message")
        async def handle_message(event, client, say):
            subtype = event.get("subtype")
            if subtype in ("bot_message", "message_changed", "message_deleted"):
                return

            text = event.get("text", "")
            if not text:
                return

            channel_id = event.get("channel", "")
            user_id = event.get("user", "")

            # Use cached workspace_id resolved once at connect()
            workspace_id = self._workspace_id

            jid = self._jid(workspace_id, channel_id)

            groups = {g["jid"]: g for g in self._registered_groups}
            group = groups.get(jid)
            if group and group.get("requires_trigger", True):
                if not config.TRIGGER_PATTERN.match(text):
                    return
            elif not group:
                if not config.TRIGGER_PATTERN.match(text):
                    return

            # Try to get display name
            sender_name = user_id
            try:
                user_info = await client.users_info(user=user_id)
                profile = user_info.get("user", {}).get("profile", {})
                sender_name = profile.get("real_name", "") or profile.get("display_name", user_id)
            except Exception:
                pass

            await self._on_message(
                jid=jid,
                sender=user_id,
                sender_name=sender_name,
                content=text,
                is_group=True,
                channel="slack",
            )

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._handler.start_async()
        self._connected = True
        log.info("Slack channel connected via Socket Mode")

    async def send_message(self, jid: str, text: str) -> None:
        if not self._app:
            log.warning("Slack send_message called but channel not connected")
            return
        parts = jid.split(":")
        if len(parts) < 3:
            log.warning("Slack invalid JID: %s", jid)
            return
        channel_id = parts[2]
        try:
            await self._app.client.chat_postMessage(channel=channel_id, text=text)
        except Exception as exc:
            log.error("Slack send_message exception: %s", exc)

    async def send_typing(self, jid: str) -> None:
        # Slack does not expose a typing indicator API for bots
        pass

    async def disconnect(self) -> None:
        self._connected = False
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception as exc:
                log.debug("Slack disconnect error: %s", exc)
            self._handler = None
        self._app = None
        log.info("Slack channel disconnected")


register_channel("slack", SlackChannel)

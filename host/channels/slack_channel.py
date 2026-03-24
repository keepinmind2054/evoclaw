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
        # FIX(p13c-SL-1): cache the bot's own user ID at connect() so we can
        # reliably detect and ignore messages the bot itself posts.  Without
        # this, the "subtype=bot_message" guard misses messages posted by the
        # bot via certain Slack SDK paths that do not set the subtype field.
        self._bot_user_id: Optional[str] = None

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

        # Resolve and cache workspace_id and bot user_id once at startup.
        try:
            auth_info = await self._app.client.auth_test()
            self._workspace_id = auth_info.get("team_id", "unknown")
            # FIX(p13c-SL-1): store the bot's own user_id for self-message detection.
            self._bot_user_id = auth_info.get("user_id")
            log.info(
                "Slack workspace ID resolved: %s, bot user_id: %s",
                self._workspace_id, self._bot_user_id,
            )
        except Exception as exc:
            log.warning("Slack auth_test() failed at connect: %s — using 'unknown'", exc)

        @self._app.event("message")
        async def handle_message(event, client, say):
            subtype = event.get("subtype")
            if subtype in ("bot_message", "message_changed", "message_deleted"):
                return

            # FIX(p13c-SL-1): also skip messages from the bot's own user ID
            # regardless of subtype, preventing bot-message feedback loops when
            # the Slack SDK does not set the subtype for bot-originated posts.
            user_id = event.get("user", "")
            if self._bot_user_id and user_id == self._bot_user_id:
                return

            text = event.get("text", "")
            if not text:
                return

            # FIX(p13c-SL-2): strip Slack mrkdwn @mention tags (e.g. <@U12345>)
            # from the start of the message before trigger matching, mirroring
            # Discord's normalization logic.  Without this, a Slack @mention
            # of the bot arrives as "<@U12345> hello" which never matches
            # TRIGGER_PATTERN (anchored at "^@AssistantName\b").
            import re as _re
            # Normalize bot @mention to the configured trigger word
            if self._bot_user_id and f"<@{self._bot_user_id}>" in text:
                normalized = _re.sub(rf"<@{_re.escape(self._bot_user_id)}>", "", text).strip()
                # Strip any remaining mention tags
                normalized = _re.sub(r"<@[A-Z0-9]+>", "", normalized).strip()
                text = f"@{config.ASSISTANT_NAME} {normalized}".strip()
            else:
                # Strip leading mention syntax that could break TRIGGER_PATTERN.match()
                text = _re.sub(r"^(<@[A-Z0-9]+>\s*)+", "", text).strip()

            channel_id = event.get("channel", "")

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

            # FIX(p13c-SL-3): wrap pipeline call in try/except to prevent
            # unhandled exceptions from propagating into slack-bolt's event
            # dispatcher and silently dropping the message.
            try:
                await self._on_message(
                    jid=jid,
                    sender=user_id,
                    sender_name=sender_name,
                    content=text,
                    is_group=True,
                    channel="slack",
                )
            except Exception as exc:
                log.error(
                    "Slack handle_message: unhandled exception in _on_message for jid=%s: %s",
                    jid, exc, exc_info=True,
                )

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        await self._handler.start_async()
        self._connected = True
        log.info("Slack channel connected via Socket Mode")

    # Slack chat.postMessage supports up to 40,000 characters, but blocks with
    # very long plain-text messages may be truncated by some clients.  Use a
    # conservative 3900-char split to stay well under any client limit.
    _SLACK_MAX_LEN = 3900

    async def send_message(self, jid: str, text: str) -> None:
        if not self._app:
            log.warning("Slack send_message called but channel not connected")
            return
        parts = jid.split(":")
        if len(parts) < 3:
            log.warning("Slack invalid JID: %s", jid)
            return
        channel_id = parts[2]

        # FIX(p13c-SL-4): Slack's text message API rejects empty strings with a
        # "no_text" error.  Guard against empty text to avoid spurious API errors.
        if not text:
            log.debug("Slack send_message: empty text for jid=%s — skipping", jid)
            return

        # p24c: split messages that exceed the conservative per-message character limit.
        # Slack's API allows up to 40,000 chars, but very long messages may be
        # truncated by clients.  Split on newlines where possible.
        import asyncio as _asyncio
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= self._SLACK_MAX_LEN:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, self._SLACK_MAX_LEN)
            if split_at <= 0:
                split_at = self._SLACK_MAX_LEN
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip("\n")
        chunks = [c for c in chunks if c]

        for chunk in chunks:
            try:
                await self._app.client.chat_postMessage(channel=channel_id, text=chunk)
            except Exception as exc:
                exc_str = str(exc).lower()
                # p24c: retry once on Slack rate-limit errors (status 429 / "ratelimited").
                if "ratelimited" in exc_str or "429" in exc_str:
                    log.warning(
                        "Slack send_message: rate limited for %s — sleeping 5s then retrying",
                        jid,
                    )
                    await _asyncio.sleep(5.0)
                    try:
                        await self._app.client.chat_postMessage(channel=channel_id, text=chunk)
                    except Exception as exc2:
                        log.error("Slack send_message retry failed: %s", exc2)
                else:
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

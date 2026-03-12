"""Discord channel implementation using discord.py"""
import asyncio
import logging
import threading
from typing import Callable, Optional

import discord

from . import register_channel_class as register_channel
from .. import config
from ..env import read_env_file

log = logging.getLogger(__name__)


class DiscordChannel:
    name = "discord"

    def __init__(self, on_message: Callable, on_chat_metadata: Callable, registered_groups: list):
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._registered_groups = registered_groups
        self._client: Optional[discord.Client] = None
        self._connected = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        env = read_env_file(["DISCORD_BOT_TOKEN"])
        self._token = env.get("DISCORD_BOT_TOKEN", "")

    def _jid_for_message(self, message: discord.Message) -> str:
        if isinstance(message.channel, discord.DMChannel):
            return f"dc:dm:{message.author.id}"
        guild_id = message.guild.id if message.guild else "unknown"
        channel_id = message.channel.id
        return f"dc:{guild_id}:{channel_id}"

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("dc:")

    def is_connected(self) -> bool:
        return self._connected and self._client is not None and not self._client.is_closed()

    async def connect(self) -> None:
        if not self._token:
            log.warning("DISCORD_BOT_TOKEN not set — Discord disabled")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        intents.guild_messages = True
        intents.dm_messages = True

        self._client = discord.Client(intents=intents)
        on_message_callback = self._on_message
        registered_groups = self._registered_groups

        @self._client.event
        async def on_ready():
            self._connected = True
            log.info("Discord channel connected as %s", self._client.user)

        @self._client.event
        async def on_message(message: discord.Message):
            if message.author.bot:
                return

            text = message.content
            if not text:
                return

            jid = self._jid_for_message(message)
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_group = not is_dm

            groups = {g["jid"]: g for g in registered_groups}
            group = groups.get(jid)
            if group and group.get("requires_trigger", True):
                if not config.TRIGGER_PATTERN.match(text):
                    return
            elif not group and not is_dm:
                if not config.TRIGGER_PATTERN.match(text):
                    return

            sender = str(message.author.id)
            sender_name = message.author.display_name or message.author.name

            await on_message_callback(
                jid=jid,
                sender=sender,
                sender_name=sender_name,
                content=text,
                is_group=is_group,
                channel="discord",
            )

        # Run the discord client in a background thread with its own event loop
        self._loop = asyncio.new_event_loop()

        def run_client():
            self._loop.run_until_complete(self._client.start(self._token))

        self._thread = threading.Thread(target=run_client, daemon=True, name="discord-client")
        self._thread.start()
        log.info("Discord channel starting in background thread")

    async def _get_channel(self, jid: str) -> Optional[discord.abc.Messageable]:
        if not self._client:
            return None
        parts = jid.split(":")
        if len(parts) < 3:
            log.warning("Discord invalid JID: %s", jid)
            return None
        if parts[1] == "dm":
            try:
                user_id = int(parts[2])
            except (ValueError, IndexError):
                log.error("Invalid Discord JID: %s", jid)
                return None
            try:
                user = await self._client.fetch_user(user_id)
                return await user.create_dm()
            except Exception as exc:
                log.error("Discord could not open DM to user %d: %s", user_id, exc)
                return None
        else:
            try:
                channel_id = int(parts[2])
            except (ValueError, IndexError):
                log.error("Invalid Discord JID: %s", jid)
                return None
            channel = self._client.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self._client.fetch_channel(channel_id)
                except Exception as exc:
                    log.error("Discord could not fetch channel %d: %s", channel_id, exc)
                    return None
            return channel

    async def _run_in_discord_loop(self, coro):
        """Schedule a coroutine on the Discord client's event loop and await its result.

        The Discord client runs in a background thread with its own event loop
        (self._loop).  Awaiting discord.py coroutines directly from the main
        asyncio event loop raises RuntimeError or silently hangs.  This helper
        bridges the two loops safely using asyncio.run_coroutine_threadsafe().
        """
        if self._loop is None or self._loop.is_closed():
            raise RuntimeError("Discord event loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        # Await the concurrent.futures.Future from the main event loop
        return await asyncio.get_event_loop().run_in_executor(None, future.result, 30)

    async def send_message(self, jid: str, text: str) -> None:
        if not self.is_connected():
            log.warning("Discord send_message called but channel not connected")
            return
        try:
            async def _send():
                channel = await self._get_channel(jid)
                if channel is not None:
                    await channel.send(text)
            await self._run_in_discord_loop(_send())
        except Exception as exc:
            log.error("Discord send_message exception: %s", exc)

    async def send_typing(self, jid: str) -> None:
        if not self.is_connected():
            return
        try:
            async def _type():
                channel = await self._get_channel(jid)
                if channel is not None:
                    async with channel.typing():
                        await asyncio.sleep(0.5)
            await self._run_in_discord_loop(_type())
        except Exception as exc:
            log.debug("Discord send_typing exception: %s", exc)

    async def disconnect(self) -> None:
        self._connected = False
        if self._client:
            try:
                await self._client.close()
            except Exception as exc:
                log.debug("Discord disconnect error: %s", exc)
            self._client = None
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        log.info("Discord channel disconnected")


register_channel("discord", DiscordChannel)

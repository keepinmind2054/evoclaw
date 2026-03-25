"""Discord channel implementation using discord.py"""
import asyncio
import concurrent.futures
import logging
import re
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
        self._discord_thread: Optional[threading.Thread] = None
        self._ready_event = asyncio.Event()

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

    def _start_discord_thread(self) -> threading.Thread:
        """Start (or restart) the Discord client in a background daemon thread.

        Stores the thread as ``self._discord_thread`` so the watchdog in
        ``is_connected()`` can check ``is_alive()`` and trigger a restart when
        the thread dies unexpectedly (e.g. due to an unhandled exception in the
        discord.py event loop).
        """
        def run_client():
            try:
                self._loop.run_until_complete(self._client.start(self._token))
            except discord.LoginFailure as auth_exc:
                # p28b: Invalid or revoked token.  Retrying will not help.
                # Log CRITICAL so the operator rotates the token in the
                # Discord developer portal and updates .env.
                log.critical(
                    "Discord: LoginFailure — DISCORD_BOT_TOKEN is invalid or revoked. "
                    "Obtain a new token from the Discord Developer Portal and update .env, "
                    "then restart. Error: %s",
                    auth_exc,
                )
            except Exception as exc:
                log.error("Discord client thread exited with error: %s", exc)

        t = threading.Thread(target=run_client, daemon=True, name="discord-client")
        t.start()
        return t

    def is_connected(self) -> bool:
        # Watchdog: if the daemon thread has died, attempt to restart it so
        # Discord does not silently stop working after an unhandled exception.
        # p24c BUG-FIX: the previous watchdog created a new discord.Client() but
        # did NOT re-register the on_ready / on_message event handlers (those are
        # closures registered on the *original* client in connect()).  The new
        # bare client would start but never set self._connected = True and never
        # deliver any messages — silently broken.  Log the failure and mark the
        # channel as disconnected so the calling code can attempt a full reconnect
        # via connect() rather than leaving a broken unhandled client running.
        if (
            hasattr(self, "_discord_thread")
            and self._discord_thread is not None
            and not self._discord_thread.is_alive()
            and self._client is not None
        ):
            log.error(
                "Discord daemon thread died unexpectedly — marking disconnected. "
                "A full reconnect via connect() is required to restore event handlers."
            )
            self._connected = False
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
            # Signal wait_connected() that the handshake is complete.
            # run_coroutine_threadsafe because on_ready fires on the Discord
            # background loop, but _ready_event lives on the main loop.
            _main_loop.call_soon_threadsafe(self._ready_event.set)

        # Capture the main event loop NOW (before the background thread starts)
        # so the on_message handler can forward callbacks to it.
        # p17c BUG-FIX (CRITICAL): the Discord client runs its own event loop in
        # a background thread (self._loop).  When discord.py calls the on_message
        # handler it runs on self._loop.  If on_message_callback (_on_message in
        # main.py) is awaited directly on self._loop, every asyncio.create_task()
        # call inside it schedules work on the Discord loop — not the main
        # application loop.  GroupQueue tasks, dedup checks, route_outbound, and
        # all other coroutines in the pipeline then execute on the wrong loop,
        # breaking per-group serialization and causing RuntimeError when tasks try
        # to access main-loop-bound asyncio primitives (Locks, Events, etc.).
        # Fix: capture the main event loop here (connect() is always called from
        # the main coroutine) and forward the callback to it via
        # run_coroutine_threadsafe().  The Discord loop only does the lightweight
        # message parsing; all application logic runs on the correct main loop.
        _main_loop = asyncio.get_running_loop()

        @self._client.event
        async def on_message(message: discord.Message):
            if message.author.bot:
                # Allow messages from trusted bot IDs (e.g. NanoClaw/Andy).
                # Set DISCORD_TRUSTED_BOT_IDS=id1,id2 in .env to whitelist specific bots.
                # This enables cross-bot interaction between EvoClaw (Eve) and NanoClaw (Andy).
                trusted_ids_raw = os.environ.get("DISCORD_TRUSTED_BOT_IDS", "")
                trusted_ids = {s.strip() for s in trusted_ids_raw.split(",") if s.strip()}
                if str(message.author.id) not in trusted_ids:
                    return

            text = message.content or ""

            # Append attachment descriptions so the agent can see uploaded files
            if message.attachments:
                attachment_lines = [
                    f"[Attachment: {a.filename} | {a.content_type or 'unknown'} | {a.size}B | {a.url}]"
                    for a in message.attachments
                ]
                text = (text + "\n" + "\n".join(attachment_lines)).strip()

            if not text:
                return

            jid = self._jid_for_message(message)
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_group = not is_dm

            # Normalize Discord @mention of the bot (e.g. <@1234567890> or <@!1234567890>)
            # to the configured trigger word so that tagging the bot works naturally.
            # e.g.  "<@1483370646770810881> 哈囉"  →  "@Eve 哈囉"
            # Also strip any other user/role @mentions (e.g. <@!999>) that appear in the
            # message so that TRIGGER_PATTERN.match() (anchored at start) is not confused
            # by leading mention tags for other users.
            if self._client.user and self._client.user in message.mentions:
                # Remove the bot's own mention and prepend the trigger word
                normalized = re.sub(rf"<@!?{self._client.user.id}>", "", text).strip()
                # Strip any remaining user/role mentions from the normalized text
                normalized = re.sub(r"<@[!&]?\d+>", "", normalized).strip()
                text = f"@{config.ASSISTANT_NAME} {normalized}".strip()
            else:
                # Even without a bot mention, strip raw Discord mention syntax that could
                # appear at the start of the message and break TRIGGER_PATTERN.match().
                text = re.sub(r"^(<@[!&]?\d+>\s*)+", "", text).strip()

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

            # p17c BUG-FIX (CRITICAL): forward the callback to the main event
            # loop via run_coroutine_threadsafe() so all downstream coroutines
            # (GroupQueue.enqueue_message_check, asyncio.create_task, Locks, etc.)
            # execute on the main loop where they were created.
            # We do NOT await the future here because discord.py's on_message
            # must return promptly; the actual processing is decoupled.
            try:
                asyncio.run_coroutine_threadsafe(
                    on_message_callback(
                        jid=jid,
                        sender=sender,
                        sender_name=sender_name,
                        content=text,
                        is_group=is_group,
                        channel="discord",
                    ),
                    _main_loop,
                )
            except Exception as _exc:
                log.error(
                    "Discord on_message: failed to forward callback for jid=%s: %s",
                    jid, _exc, exc_info=True,
                )

        # Run the discord client in a background thread with its own event loop
        self._loop = asyncio.new_event_loop()
        self._discord_thread = self._start_discord_thread()
        log.info("Discord channel starting in background thread")

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        """Wait until on_ready fires or timeout expires. Returns True if connected."""
        if self._connected:
            return True
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return self._connected
        except asyncio.TimeoutError:
            log.warning("Discord did not connect within %.0fs", timeout)
            return False

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

        # Await the concurrent.futures.Future from the main event loop.
        # Fixes #87: future.result(30) was not catching TimeoutError, which propagated
        # uncaught and crashed the send_message() coroutine.
        def _get_result():
            try:
                result = future.result(30)
                return result
            except concurrent.futures.TimeoutError:
                log.warning("Discord loop call timed out after 30s")
                return None
            except Exception as exc:
                log.error("Discord loop call failed: %s", exc)
                return None

        # p17c BUG-FIX (MEDIUM): asyncio.get_event_loop() is deprecated in Python
        # 3.10+ when called from a coroutine.  Use get_running_loop() which always
        # returns the loop the current coroutine is executing on.
        return await asyncio.get_running_loop().run_in_executor(None, _get_result)

    async def send_message(self, jid: str, text: str) -> None:
        if not self.is_connected():
            log.warning("Discord send_message called but channel not connected")
            return
        try:
            # Discord enforces a hard 2000-character limit per message.
            # Split long messages into chunks rather than silently truncating so
            # the user sees the full response.
            # Fix(p12a): the previous `range(0, max(len(text), 1), …)` passed
            # `range(0, 1, 2000)` when text was empty, producing chunks=[""].
            # Sending an empty string to Discord raises a 400 Bad Request.
            # Guard against empty text before building the chunk list.
            if not text:
                log.debug("Discord send_message: empty text for jid=%s — skipping", jid)
                return
            _DISCORD_LIMIT = 2000
            chunks = [text[i:i + _DISCORD_LIMIT] for i in range(0, len(text), _DISCORD_LIMIT)]

            async def _send():
                channel = await self._get_channel(jid)
                if channel is None:
                    return
                for chunk in chunks:
                    try:
                        await channel.send(chunk)
                    except discord.Forbidden as forbidden_exc:
                        # p28b: Bot lacks permissions in this channel or was removed.
                        # Log at WARNING — this is a normal operational event (e.g. admin
                        # restricted the bot's write permissions) and does not indicate
                        # token revocation.  Stop sending remaining chunks.
                        log.warning(
                            "Discord send_message: Forbidden for jid=%s — "
                            "bot may lack permissions or was removed from the channel. "
                            "Error: %s",
                            jid, forbidden_exc,
                        )
                        return
                    except discord.HTTPException as http_exc:
                        # p28b: Handle unexpected HTTP status codes from Discord.
                        # 429 (RateLimit) is handled by discord.py automatically.
                        # 401 means the token was revoked — log CRITICAL.
                        # 5xx codes are transient; log ERROR but continue (discord.py
                        # may retry internally; this is a last-resort guard).
                        if http_exc.status == 401:
                            log.critical(
                                "Discord send_message: HTTP 401 Unauthorized — "
                                "DISCORD_BOT_TOKEN may have been revoked. "
                                "Obtain a new token from the Discord Developer Portal "
                                "and update .env, then restart. Error: %s",
                                http_exc,
                            )
                            return  # Token is unusable; abort remaining chunks
                        log.error(
                            "Discord send_message: HTTP %d for jid=%s: %s",
                            http_exc.status, jid, http_exc,
                        )

            await self._run_in_discord_loop(_send())
        except Exception as exc:
            log.error("Discord send_message exception: %s", exc)

    async def send_file(self, jid: str, file_path: str, caption: str = "") -> None:
        """Upload a file to a Discord channel, with an optional caption."""
        if not self.is_connected():
            log.warning("Discord send_file called but channel not connected")
            return
        try:
            import os as _os
            filename = _os.path.basename(file_path)

            async def _upload():
                channel = await self._get_channel(jid)
                if channel is None:
                    return
                with open(file_path, "rb") as fp:
                    await channel.send(
                        content=caption or None,
                        file=discord.File(fp, filename=filename),
                    )
            await self._run_in_discord_loop(_upload())
            log.info("Discord file sent: jid=%s file=%s", jid, filename)
        except Exception as exc:
            log.error("Discord send_file exception: %s", exc)

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
        """Disconnect the Discord client cleanly (Issue #67).

        discord.py's client.close() must be awaited on the Discord event loop
        (self._loop), not on the main asyncio event loop.  Awaiting it from the
        wrong loop causes a deadlock.  We schedule it via run_coroutine_threadsafe()
        and then join the background thread with a timeout to ensure the Discord
        loop drains before the process exits.
        """
        self._connected = False
        if self._client and self._loop and not self._loop.is_closed():
            try:
                future = asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
                # Wait up to 10 s for clean close; ignore timeout — we are shutting down anyway
                try:
                    future.result(timeout=10)
                except Exception as exc:
                    log.debug("Discord client.close() error: %s", exc)
            except Exception as exc:
                log.debug("Discord disconnect scheduling error: %s", exc)
        elif self._client:
            # Loop already gone — best effort
            try:
                await self._client.close()
            except Exception as exc:
                log.debug("Discord disconnect error: %s", exc)
        self._client = None
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        # Join whichever thread reference is set (legacy self._thread or new self._discord_thread)
        _thread_to_join = self._discord_thread or self._thread
        if _thread_to_join and _thread_to_join.is_alive():
            _thread_to_join.join(timeout=5)
            if _thread_to_join.is_alive():
                log.warning("Discord background thread did not exit within 5s")
        log.info("Discord channel disconnected")


register_channel("discord", DiscordChannel)

"""Telegram channel implementation using python-telegram-bot"""
import asyncio
import logging
import os
import time
from typing import Callable, Awaitable, Optional
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from .. import config
from ..env import read_env_file

log = logging.getLogger(__name__)

# p22c: Silence watchdog threshold.  If no update (message, command, or
# non-text event) is received for this many seconds, the watchdog will log a
# warning and attempt to reconnect.  Distinct from the startup retry logic —
# this catches cases where polling is running but Telegram stops delivering
# updates (e.g. a silent TCP connection hang).
_POLL_SILENCE_THRESHOLD_S = 300  # 5 minutes


class TelegramChannel:
    name = "telegram"

    def __init__(self, on_message: Callable, on_chat_metadata: Callable, registered_groups: list, on_setup_command: Optional[Callable] = None):
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._registered_groups = registered_groups
        self._on_setup_command = on_setup_command
        self._app: Optional[Application] = None
        # p22c: staleness watchdog state
        self._last_poll_activity: float = time.time()
        self._watchdog_task: Optional[asyncio.Task] = None
        env = read_env_file(["TELEGRAM_BOT_TOKEN", "TELEGRAM_UPLOAD_TIMEOUT", "TELEGRAM_PROXY"])
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        self._token = token

        # Fixes #91: make upload timeout configurable via env var (default 300s).
        # The previous hardcoded 120s is insufficient for files near the 45 MB limit
        # on slow networks.
        timeout_str = env.get("TELEGRAM_UPLOAD_TIMEOUT") or os.environ.get("TELEGRAM_UPLOAD_TIMEOUT") or "300"
        try:
            self._upload_timeout = int(timeout_str)
        except (ValueError, TypeError):
            self._upload_timeout = 300

        # Fixes #207: support HTTP/SOCKS5 proxy for networks where api.telegram.org
        # is unreachable directly (e.g. certain VPS regions, firewalled corporate nets).
        # Set TELEGRAM_PROXY=http://host:port  or  socks5://user:pass@host:port
        self._proxy = env.get("TELEGRAM_PROXY") or os.environ.get("TELEGRAM_PROXY") or ""

    def _jid(self, chat_id: int) -> str:
        return f"tg:{chat_id}"

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("tg:")

    def is_connected(self) -> bool:
        return self._app is not None and self._app.running

    async def connect(self) -> None:
        if not self._token:
            log.warning("TELEGRAM_BOT_TOKEN not set — Telegram disabled")
            return

        import asyncio
        MAX_RETRIES = 5  # increased from 3 — transient network blips need more attempts
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                builder = Application.builder().token(self._token)
                if self._proxy:
                    # Route all Telegram API calls through the configured proxy.
                    # Supports HTTP (http://host:port) and SOCKS5 (socks5://user:pass@host:port).
                    from telegram.request import HTTPXRequest
                    req = HTTPXRequest(
                        proxy=self._proxy,
                        connect_timeout=30.0,
                        read_timeout=30.0,
                    )
                    builder = builder.request(req)
                    log.info("Telegram using proxy: %s", self._proxy)
                self._app = builder.build()

                async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
                    if not update.message or not update.message.text:
                        return
                    # p22c: record activity so the staleness watchdog knows polling is alive.
                    self._last_poll_activity = time.time()
                    jid = self._jid(update.effective_chat.id)
                    sender = str(update.effective_user.id) if update.effective_user else "unknown"
                    sender_name = update.effective_user.full_name if update.effective_user else "Unknown"
                    text = update.message.text

                    groups = {g["jid"]: g for g in self._registered_groups}
                    group = groups.get(jid)
                    if group and group.get("requires_trigger", True):
                        # Fix(p12a): use TRIGGER_PATTERN (regex with \b) for consistency
                        # with Discord and _process_group_messages; the previous
                        # str.startswith() check had no word-boundary so "@Eveline"
                        # would incorrectly pass when ASSISTANT_NAME="Eve".
                        if not config.TRIGGER_PATTERN.match(text):
                            return

                    # Fix(p12a): wrap pipeline call in try/except so that any
                    # exception raised inside _on_message (RBAC, DB, immune check,
                    # dedup) does not propagate back into python-telegram-bot's
                    # dispatcher and crash the update handler, which would cause
                    # that update to be silently dropped and logged as an unhandled
                    # application error.
                    try:
                        await self._on_message(
                            jid=jid,
                            sender=sender,
                            sender_name=sender_name,
                            content=text,
                            is_group=update.effective_chat.type in ("group", "supergroup"),
                            channel="telegram",
                        )
                    except Exception as _exc:
                        log.error(
                            "Telegram handle: unhandled exception in _on_message for jid=%s: %s",
                            jid, _exc, exc_info=True,
                        )

                self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

                # ── /monitor command: one-step monitor group setup ──────────
                # Sending /monitor in any group adds it as the monitor group
                # without needing to edit .env manually.
                _on_setup = self._on_setup_command

                async def handle_monitor_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
                    if not update.effective_chat or not update.effective_user:
                        return
                    chat_id = update.effective_chat.id
                    jid = self._jid(chat_id)
                    sender_name = update.effective_user.full_name or "unknown"
                    log.info("/monitor command received from jid=%s by %s", jid, sender_name)
                    if _on_setup:
                        try:
                            result = await _on_setup(jid, "monitor")
                            reply = result or f"✅ 監控群組已設定完成！\n\nJID: {jid}\n\nEvoClaw 的錯誤通知將自動發送到這個群組。"
                        except Exception as exc:
                            reply = f"❌ 設定失敗：{exc}"
                    else:
                        reply = "⚠️ 設定功能尚未啟用，請在 .env 中手動設定 MONITOR_JID。"
                    try:
                        await self._app.bot.send_message(chat_id=chat_id, text=reply)
                    except Exception:
                        pass

                self._app.add_handler(CommandHandler("monitor", handle_monitor_cmd))

                async def handle_non_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
                    """Notify users who send non-text messages (Issue #70)."""
                    if not update.effective_chat:
                        return
                    # p22c: non-text updates also count as poll activity.
                    self._last_poll_activity = time.time()
                    chat_id = update.effective_chat.id
                    try:
                        await self._app.bot.send_message(
                            chat_id=chat_id,
                            text="I can only process text messages at the moment. Please send your request as text.",
                        )
                    except Exception:
                        pass

                self._app.add_handler(
                    MessageHandler(
                        (filters.PHOTO | filters.VOICE | filters.VIDEO | filters.AUDIO |
                         filters.Document.ALL | filters.Sticker.ALL | filters.LOCATION |
                         filters.CONTACT) & ~filters.COMMAND,
                        handle_non_text,
                    )
                )
                await self._app.initialize()
                await self._app.start()
                # drop_pending_updates=True: flush all messages that accumulated
                # while the bot was offline or blocked (e.g. during RBAC lockout,
                # restart, maintenance). Without this, every restart re-processes
                # all queued messages from Telegram's server-side buffer.
                await self._app.updater.start_polling(drop_pending_updates=True)
                # p22c: Reset activity timestamp and start staleness watchdog.
                self._last_poll_activity = time.time()
                if self._watchdog_task is None or self._watchdog_task.done():
                    self._watchdog_task = asyncio.create_task(
                        self._poll_watchdog(), name="telegram-poll-watchdog"
                    )
                log.info("Telegram channel connected (pending updates dropped)")
                return  # success
            except Exception as e:
                err_str = str(e).lower()
                if "conflict" in err_str:
                    log.error(
                        "Telegram: Conflict detected — another bot instance is already running. "
                        "Stop the other instance and restart."
                    )
                    raise  # Conflict is unrecoverable, re-raise immediately

                # p28b: Detect token revocation / authorization failures at connect time.
                # Forbidden (covers 401/403) from Telegram means the bot
                # token is invalid or has been revoked.  Retrying will not help and
                # clutters logs — raise immediately with a clear CRITICAL message so
                # the operator knows to rotate the token.
                if any(kw in err_str for kw in ("unauthorized", "invalid token", "forbidden", "bot was kicked")):
                    log.critical(
                        "Telegram: Authorization failure (%s: %s) — "
                        "TELEGRAM_BOT_TOKEN may be invalid or revoked. "
                        "Rotate the token in BotFather and update .env, then restart.",
                        type(e).__name__, e,
                    )
                    raise  # Unrecoverable — re-raise immediately

                if attempt < MAX_RETRIES:
                    wait = min(2 ** attempt, 30)  # 2s, 4s, 8s, 16s (capped at 30s)
                    log.warning(
                        f"Telegram connect attempt {attempt}/{MAX_RETRIES} failed "
                        f"({type(e).__name__}: {e}) — retrying in {wait}s"
                    )
                    # Clean up failed app before retry
                    try:
                        if self._app:
                            await self._app.shutdown()
                    except Exception:
                        pass
                    self._app = None
                    await asyncio.sleep(wait)
                else:
                    log.error(
                        f"Telegram connect failed after {MAX_RETRIES} attempts: {type(e).__name__}: {e}"
                    )
                    raise

    async def _poll_watchdog(self) -> None:
        """p22c: Staleness watchdog for the Telegram long-poll connection.

        Runs as a background task alongside the python-telegram-bot updater.
        Every 60 seconds it checks whether any update has been received within
        the last _POLL_SILENCE_THRESHOLD_S seconds.  If not, it logs a warning
        and attempts to reconnect by calling disconnect() then connect().

        This is separate from the startup retry logic — it catches silent TCP
        hangs where the updater loop is running but Telegram has stopped
        delivering updates.  In a healthy group, messages arrive regularly; in
        quiet groups the watchdog is a safety net that only fires after 5 full
        minutes of silence.
        """
        log.debug("Telegram poll watchdog started (threshold=%ds)", _POLL_SILENCE_THRESHOLD_S)
        while self._app is not None and self._app.running:
            await asyncio.sleep(60)
            if self._app is None or not self._app.running:
                break
            silence = time.time() - self._last_poll_activity
            if silence > _POLL_SILENCE_THRESHOLD_S:
                log.warning(
                    "Telegram: no poll activity for %.0fs (threshold=%ds) — "
                    "attempting reconnect to recover from silent stale connection",
                    silence, _POLL_SILENCE_THRESHOLD_S,
                )
                try:
                    await self.disconnect()
                except Exception as _disc_exc:
                    log.warning("Telegram watchdog: disconnect failed: %s", _disc_exc)
                try:
                    await self.connect()
                except Exception as _conn_exc:
                    log.error("Telegram watchdog: reconnect failed: %s", _conn_exc)
                # connect() starts a new watchdog task; this one exits.
                return
        log.debug("Telegram poll watchdog exiting (app stopped)")

    # Telegram hard limit is 4096 UTF-16 code units; use 4000 for a safety margin.
    _TELEGRAM_MAX_LEN = 4000

    async def send_message(self, jid: str, text: str) -> None:
        if not self._app:
            return
        # p24c: guard against empty text — sending empty string causes a Telegram 400 error.
        if not text:
            log.debug("send_message: empty text for jid=%s — skipping", jid)
            return
        try:
            chat_id = int(jid.replace("tg:", ""))
        except ValueError:
            log.error("send_message: malformed Telegram JID %r — cannot parse chat_id", jid)
            return

        # p24c: split messages that exceed Telegram's 4096-char limit.
        # The router already splits at TELEGRAM_MAX_LEN but this guards against
        # messages that arrive via other code paths (e.g. /monitor reply, test shims).
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= self._TELEGRAM_MAX_LEN:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, self._TELEGRAM_MAX_LEN)
            if split_at <= 0:
                split_at = self._TELEGRAM_MAX_LEN
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip("\n")
        chunks = [c for c in chunks if c]

        for chunk in chunks:
            try:
                # p24c: handle Telegram FloodWait (429 RetryAfter) rate-limit errors.
                # When Telegram tells us to back off, wait the specified number of seconds
                # and retry once before giving up.
                # p28b: also handle token revocation (Forbidden) and other
                # unexpected HTTP status codes (402, 503, etc.) explicitly.
                from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden
                try:
                    await self._app.bot.send_message(chat_id=chat_id, text=chunk)
                except RetryAfter as flood_exc:
                    wait_secs = max(float(getattr(flood_exc, "retry_after", 5)), 5.0)
                    log.warning(
                        "send_message: Telegram FloodWait for %s — sleeping %.0fs then retrying",
                        jid, wait_secs,
                    )
                    await asyncio.sleep(wait_secs)
                    await self._app.bot.send_message(chat_id=chat_id, text=chunk)
                except Forbidden as forbidden_exc:
                    # p28b: Token revoked (401) or bot kicked/blocked (403).
                    # In python-telegram-bot v20+, both are raised as Forbidden.
                    log.critical(
                        "send_message: Telegram auth error for %s: %s — "
                        "check TELEGRAM_BOT_TOKEN and whether bot is still in group.",
                        jid, forbidden_exc,
                    )
                    return  # Abort remaining chunks
                except (TimedOut, NetworkError) as transient_exc:
                    # p24c: retry once on transient network errors (timeouts, brief disconnects).
                    log.warning(
                        "send_message: transient error for %s (%s) — retrying once",
                        jid, transient_exc,
                    )
                    await asyncio.sleep(2.0)
                    await self._app.bot.send_message(chat_id=chat_id, text=chunk)
            except Exception as exc:
                # p28b: Log the exception type so unexpected HTTP codes (402, 503, etc.)
                # are identifiable in logs without requiring a second investigation pass.
                log.error("send_message: failed to deliver to %s (%s): %s", jid, type(exc).__name__, exc)

    async def send_file(self, jid: str, file_path: str, caption: str = "") -> None:
        """Send a document/file to a Telegram chat.

        Improvements over the previous implementation:
        - Streams the file via an open file object instead of loading the entire
          content into memory with f.read() — avoids large memory spikes for
          multi-megabyte files.
        - Wraps the upload in asyncio.wait_for() with a 120-second timeout so a
          slow connection cannot stall the GroupQueue slot indefinitely.
        - Notifies the user on failure rather than silently swallowing the error.
        """
        import asyncio
        import pathlib

        if not self._app:
            return

        p = pathlib.Path(file_path)
        if not p.exists():
            log.warning("send_file: file not found: %s", file_path)
            await self.send_message(jid, f"[File not found: {p.name}]")
            return

        try:
            chat_id = int(jid.replace("tg:", ""))
        except ValueError:
            log.error("send_file: malformed Telegram JID %r — cannot parse chat_id", jid)
            return

        try:
            # Stream the file — do NOT read() the whole thing into memory.
            with open(p, "rb") as fh:
                await asyncio.wait_for(
                    self._app.bot.send_document(
                        chat_id=chat_id,
                        document=fh,
                        filename=p.name,
                        caption=caption or p.name,
                    ),
                    timeout=self._upload_timeout,
                )
            log.info("send_file: sent %s (%d bytes) to %s", p.name, p.stat().st_size, jid)
        except asyncio.TimeoutError:
            log.error("send_file: upload of %s timed out after %ds", p.name, self._upload_timeout)
            try:
                await self.send_message(jid, f"[File upload timed out: {p.name}]")
            except Exception:
                pass
        except Exception as exc:
            log.error("send_file: failed to send %s to %s: %s", p.name, jid, exc, exc_info=True)
            try:
                await self.send_message(jid, f"[Failed to send file '{p.name}': {exc}]")
            except Exception:
                pass

    async def send_typing(self, jid: str) -> None:
        if not self._app:
            return
        try:
            from telegram.constants import ChatAction
            chat_id = int(jid.replace("tg:", ""))
            await self._app.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception as e:
            log.debug(f"Typing indicator failed: {e}")

    async def disconnect(self) -> None:
        # p22c: cancel the staleness watchdog before tearing down the app so it
        # doesn't try to reconnect during a deliberate shutdown.
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if not self._app:
            return
        try:
            await self._app.updater.stop()
        except Exception:
            pass
        try:
            await self._app.stop()
        except asyncio.CancelledError:
            pass  # update_fetcher already cancelled — expected during shutdown
        except Exception:
            pass
        try:
            await self._app.shutdown()
        except Exception:
            pass
        self._app = None

"""Telegram channel implementation using python-telegram-bot"""
import logging
import os
from typing import Callable, Awaitable, Optional
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from .. import config
from ..env import read_env_file

log = logging.getLogger(__name__)

class TelegramChannel:
    name = "telegram"

    def __init__(self, on_message: Callable, on_chat_metadata: Callable, registered_groups: list, on_setup_command: Optional[Callable] = None):
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._registered_groups = registered_groups
        self._on_setup_command = on_setup_command
        self._app: Optional[Application] = None
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
                    jid = self._jid(update.effective_chat.id)
                    sender = str(update.effective_user.id) if update.effective_user else "unknown"
                    sender_name = update.effective_user.full_name if update.effective_user else "Unknown"
                    text = update.message.text

                    groups = {g["jid"]: g for g in self._registered_groups}
                    group = groups.get(jid)
                    if group and group.get("requires_trigger", True):
                        if not text.lower().startswith(f"@{config.ASSISTANT_NAME.lower()}"):
                            return

                    await self._on_message(
                        jid=jid,
                        sender=sender,
                        sender_name=sender_name,
                        content=text,
                        is_group=update.effective_chat.type in ("group", "supergroup"),
                        channel="telegram",
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
                await self._app.updater.start_polling()
                log.info("Telegram channel connected")
                return  # success
            except Exception as e:
                err_str = str(e).lower()
                if "conflict" in err_str:
                    log.error(
                        "Telegram: Conflict detected — another bot instance is already running. "
                        "Stop the other instance and restart."
                    )
                    raise  # Conflict is unrecoverable, re-raise immediately

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

    async def send_message(self, jid: str, text: str) -> None:
        if not self._app:
            return
        chat_id = int(jid.replace("tg:", ""))
        await self._app.bot.send_message(chat_id=chat_id, text=text)

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

        chat_id = int(jid.replace("tg:", ""))

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

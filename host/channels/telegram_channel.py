"""Telegram channel implementation using python-telegram-bot"""
import logging
from typing import Callable, Awaitable, Optional
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from .. import config
from ..env import read_env_file

log = logging.getLogger(__name__)

class TelegramChannel:
    name = "telegram"

    def __init__(self, on_message: Callable, on_chat_metadata: Callable, registered_groups: list):
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._registered_groups = registered_groups
        self._app: Optional[Application] = None
        token = read_env_file(["TELEGRAM_BOT_TOKEN"]).get("TELEGRAM_BOT_TOKEN", "")
        self._token = token

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

        self._app = Application.builder().token(self._token).build()

        async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not update.message or not update.message.text:
                return
            jid = self._jid(update.effective_chat.id)
            sender = str(update.effective_user.id) if update.effective_user else "unknown"
            sender_name = update.effective_user.full_name if update.effective_user else "Unknown"
            text = update.message.text

            # Prepend trigger if @mention used via Telegram mention
            groups = {g["jid"]: g for g in self._registered_groups}
            group = groups.get(jid)
            if group and group.get("requires_trigger", True):
                # Add @AssistantName prefix if message starts with mention
                if not text.lower().startswith(f"@{config.ASSISTANT_NAME.lower()}"):
                    return  # ignore non-triggered messages

            await self._on_message(
                jid=jid,
                sender=sender,
                sender_name=sender_name,
                content=text,
                is_group=update.effective_chat.type in ("group", "supergroup"),
                channel="telegram",
            )

        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        log.info("Telegram channel connected")

    async def send_message(self, jid: str, text: str) -> None:
        if not self._app:
            return
        chat_id = int(jid.replace("tg:", ""))
        await self._app.bot.send_message(chat_id=chat_id, text=text)

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
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

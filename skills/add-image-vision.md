# Skill: Add Image Vision

Enables EvoClaw to receive and analyze images using Gemini's vision capability.

## How it works

Gemini 2.0 Flash natively supports images. When an image is received, convert it to base64 and include it in the prompt to Gemini.

## Changes needed in container/agent-runner/agent.py

In the `run_agent()` function, update the initial user message to support image parts:

```python
from google.genai import types
import base64

def make_user_content(text: str, image_path: str = None) -> types.Content:
    parts = []
    if image_path:
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode()
        parts.append(types.Part(
            inline_data=types.Blob(
                mime_type="image/jpeg",
                data=image_data,
            )
        ))
    parts.append(types.Part(text=text))
    return types.Content(role="user", parts=parts)
```

## Channel-side (e.g. Telegram)

In `host/channels/telegram_channel.py`, handle photo messages:

```python
from telegram.ext import MessageHandler, filters

async def handle_photo(update, ctx):
    photo = update.message.photo[-1]  # largest size
    file = await ctx.bot.get_file(photo.file_id)
    path = f"/tmp/evoclaw_photo_{photo.file_id}.jpg"
    await file.download_to_drive(path)
    jid = self._jid(update.effective_chat.id)
    caption = update.message.caption or "What is in this image?"
    await self._on_message(jid=jid, sender=..., content=caption, image_path=path, ...)

self._app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
```

No extra dependencies needed — Gemini already supports vision!

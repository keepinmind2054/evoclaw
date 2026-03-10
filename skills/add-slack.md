# Skill: Add Slack Channel

Adds Slack support to EvoClaw using Slack Bolt for Python.

## Steps

### 1. Create a Slack app
- Go to api.slack.com/apps → Create New App → From Scratch
- Under "OAuth & Permissions", add scopes: `chat:write`, `channels:read`, `channels:history`, `im:history`, `im:write`
- Install app to workspace, copy Bot Token (`xoxb-...`)
- Under "Event Subscriptions", enable and subscribe to: `message.channels`, `message.im`
- Copy Signing Secret from Basic Information

### 2. Add to .env
```
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_SIGNING_SECRET=your-signing-secret
```

### 3. Create host/channels/slack_channel.py
```python
"""Slack channel using slack_bolt"""
import asyncio, logging
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from . import register_channel_class as register_channel
from ..env import read_env_file

log = logging.getLogger(__name__)

class SlackChannel:
    name = "slack"

    def __init__(self, on_message, on_chat_metadata, registered_groups):
        self._on_message = on_message
        env = read_env_file(["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET"])
        self._bot_token = env.get("SLACK_BOT_TOKEN", "")
        self._app_token = env.get("SLACK_APP_TOKEN", "")
        self._app = None

    def owns_jid(self, jid): return jid.startswith("slack:")
    def is_connected(self): return self._app is not None

    async def connect(self):
        if not self._bot_token:
            log.warning("SLACK_BOT_TOKEN not set — Slack disabled")
            return
        self._app = AsyncApp(token=self._bot_token)

        @self._app.event("message")
        async def handle(event, say):
            jid = f"slack:{event.get('channel')}"
            await self._on_message(
                jid=jid,
                sender=event.get("user", "unknown"),
                sender_name=event.get("user", "unknown"),
                content=event.get("text", ""),
                is_group=True,
                channel="slack",
            )

        handler = AsyncSocketModeHandler(self._app, self._app_token)
        asyncio.create_task(handler.start_async())
        log.info("Slack channel connected")

    async def send_message(self, jid, text):
        channel = jid.replace("slack:", "")
        await self._app.client.chat_postMessage(channel=channel, text=text)

    async def disconnect(self):
        pass

register_channel("slack", SlackChannel)
```

### 4. Install slack_bolt
```bash
pip install slack_bolt
```
Add `slack_bolt>=1.18` to `host/requirements.txt`.

### 5. Import and register (same as other channels)

# Skill: Add Discord Channel

Adds Discord bot support to EvoClaw.

## Steps

### 1. Create a Discord bot
- Go to discord.com/developers/applications
- New Application → Bot → Copy Token
- Enable "Message Content Intent" under Bot settings
- Invite bot to server with `bot` scope + `Send Messages` + `Read Messages` permissions

### 2. Add token to .env
```
DISCORD_BOT_TOKEN=your_token_here
```

### 3. Create host/channels/discord_channel.py
```python
"""Discord channel using discord.py"""
import logging
import discord
from . import register_channel
from ..env import read_env_file

log = logging.getLogger(__name__)

class DiscordChannel:
    name = "discord"

    def __init__(self, on_message, on_chat_metadata, registered_groups):
        self._on_message = on_message
        self._registered_groups = registered_groups
        self._client = None
        self._token = read_env_file(["DISCORD_BOT_TOKEN"]).get("DISCORD_BOT_TOKEN", "")

    def owns_jid(self, jid): return jid.startswith("dc:")
    def is_connected(self): return self._client and not self._client.is_closed()

    async def connect(self):
        if not self._token:
            log.warning("DISCORD_BOT_TOKEN not set — Discord disabled")
            return
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_message(message):
            if message.author.bot:
                return
            jid = f"dc:{message.channel.id}"
            await self._on_message(
                jid=jid,
                sender=str(message.author.id),
                sender_name=message.author.display_name,
                content=message.content,
                is_group=isinstance(message.channel, discord.TextChannel),
                channel="discord",
            )

        import asyncio
        asyncio.create_task(self._client.start(self._token))
        log.info("Discord channel connecting...")

    async def send_message(self, jid, text):
        channel_id = int(jid.replace("dc:", ""))
        channel = self._client.get_channel(channel_id)
        if channel:
            await channel.send(text[:2000])

    async def disconnect(self):
        if self._client:
            await self._client.close()

register_channel("discord", DiscordChannel)
```

### 4. Install discord.py
```bash
pip install discord.py
```
Add `discord.py>=2.0` to `host/requirements.txt`.

### 5. Import in host/channels/__init__.py
Add: `from .discord_channel import DiscordChannel`

### 6. Register your Discord channel in the DB
```python
python -c "
from host import db, config
db.init_database(config.STORE_DIR / 'messages.db')
db.set_registered_group(
    jid='dc:YOUR_CHANNEL_ID',
    name='My Discord',
    folder='discord_my-server',
    trigger_pattern=None,
    container_config=None,
    requires_trigger=True,
    is_main=False,
)
"
```

### 7. Restart and test
```bash
python run.py
```

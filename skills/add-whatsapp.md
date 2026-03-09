# Skill: Add WhatsApp Channel

Adds WhatsApp support to EvoClaw via a WhatsApp Web client.

## Option: Use whatsapp-web.js bridge

### 1. Install the WhatsApp bridge
```bash
pip install -r host/requirements.txt
npm install whatsapp-web.js qrcode-terminal
```

### 2. Create host/channels/whatsapp_channel.py

```python
"""WhatsApp channel via whatsapp-web.js subprocess bridge"""
import asyncio, json, logging, subprocess
from . import register_channel

log = logging.getLogger(__name__)

class WhatsAppChannel:
    name = "whatsapp"

    def __init__(self, on_message, on_chat_metadata, registered_groups):
        self._on_message = on_message
        self._registered_groups = registered_groups
        self._proc = None

    def owns_jid(self, jid): return "@c.us" in jid or "@g.us" in jid
    def is_connected(self): return self._proc is not None

    async def connect(self):
        # Start whatsapp-web.js bridge process
        self._proc = await asyncio.create_subprocess_exec(
            "node", "host/channels/wa_bridge.js",
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )
        asyncio.create_task(self._read_loop())
        log.info("WhatsApp bridge started")

    async def _read_loop(self):
        async for line in self._proc.stdout:
            try:
                msg = json.loads(line)
                if msg.get("type") == "message":
                    await self._on_message(**msg)
            except Exception as e:
                log.error(f"WA bridge error: {e}")

    async def send_message(self, jid, text):
        cmd = json.dumps({"type": "send", "jid": jid, "text": text}) + "\n"
        self._proc.stdin.write(cmd.encode())
        await self._proc.stdin.drain()

    async def disconnect(self):
        if self._proc:
            self._proc.terminate()

register_channel("whatsapp", WhatsAppChannel)
```

### 3. Import in host/channels/__init__.py
Add: `from .whatsapp_channel import WhatsAppChannel`

### 4. Scan QR code on first run
```bash
python run.py
```
Scan the QR code with WhatsApp on your phone.

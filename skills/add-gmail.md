# Skill: Add Gmail Channel

Adds Gmail support to EvoClaw — reads emails and replies via Gmail API.

## Steps

### 1. Enable Gmail API
- Go to console.cloud.google.com
- Create project → Enable Gmail API
- Create OAuth 2.0 credentials → Download as `credentials.json`
- Place `credentials.json` in project root

### 2. Install dependencies
```bash
pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client
```
Add to `host/requirements.txt`:
```
google-auth-oauthlib>=1.0
google-api-python-client>=2.0
```

### 3. Create host/channels/gmail_channel.py
```python
"""Gmail channel using Gmail API"""
import asyncio, base64, logging, time
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from . import register_channel

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
log = logging.getLogger(__name__)

class GmailChannel:
    name = "gmail"

    def __init__(self, on_message, on_chat_metadata, registered_groups):
        self._on_message = on_message
        self._service = None
        self._last_check = int(time.time())

    def owns_jid(self, jid): return jid.startswith("gmail:")
    def is_connected(self): return self._service is not None

    async def connect(self):
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
        self._service = build("gmail", "v1", credentials=creds)
        asyncio.create_task(self._poll_loop())
        log.info("Gmail channel connected")

    async def _poll_loop(self):
        while True:
            try:
                results = self._service.users().messages().list(
                    userId="me", q=f"after:{self._last_check} is:unread"
                ).execute()
                for msg in results.get("messages", []):
                    full = self._service.users().messages().get(userId="me", id=msg["id"]).execute()
                    headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
                    sender = headers.get("From", "unknown")
                    subject = headers.get("Subject", "")
                    body = self._get_body(full)
                    jid = f"gmail:{sender}"
                    await self._on_message(jid=jid, sender=sender, sender_name=sender,
                                           content=f"{subject}\n{body}", is_group=False, channel="gmail")
                    self._service.users().messages().modify(userId="me", id=msg["id"],
                        body={"removeLabelIds": ["UNREAD"]}).execute()
                self._last_check = int(time.time())
            except Exception as e:
                log.error(f"Gmail poll error: {e}")
            await asyncio.sleep(30)

    def _get_body(self, msg):
        if "parts" in msg["payload"]:
            for p in msg["payload"]["parts"]:
                if p["mimeType"] == "text/plain":
                    return base64.urlsafe_b64decode(p["body"]["data"]).decode()
        return ""

    async def send_message(self, jid, text):
        to = jid.replace("gmail:", "")
        mime = MIMEText(text)
        mime["to"] = to
        mime["subject"] = "EvoClaw"
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        self._service.users().messages().send(userId="me", body={"raw": raw}).execute()

    async def disconnect(self): pass

register_channel("gmail", GmailChannel)
```

### 4. Import in host/channels/__init__.py
Add: `from .gmail_channel import GmailChannel`

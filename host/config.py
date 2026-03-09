"""EvoClaw Host Configuration"""
import os
import platform
import re
from pathlib import Path
from .env import read_env_file

# Base paths
BASE_DIR = Path(__file__).parent.parent

if platform.system() == "Windows":
    _base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    DATA_DIR = Path(os.environ.get("DATA_DIR", str(_base / "evoclaw" / "data")))
    STORE_DIR = Path(os.environ.get("STORE_DIR", str(_base / "evoclaw" / "store")))
    CONFIG_DIR = _base / "evoclaw" / "config"
else:
    DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
    STORE_DIR = Path(os.environ.get("STORE_DIR", str(BASE_DIR / "store")))
    CONFIG_DIR = Path.home() / ".config" / "evoclaw"

GROUPS_DIR = BASE_DIR / "groups"
MOUNT_ALLOWLIST_FILE = CONFIG_DIR / "mount-allowlist.json"
SENDER_ALLOWLIST_FILE = CONFIG_DIR / "sender-allowlist.json"


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        import logging
        logging.getLogger(__name__).warning("Invalid value for %s, using default %d", key, default)
        return default


# Assistant
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Andy")
TRIGGER_PATTERN = re.compile(rf"^@{re.escape(ASSISTANT_NAME)}\b", re.IGNORECASE)

# Polling
POLL_INTERVAL = _env_int("POLL_INTERVAL", 2000) / 1000  # seconds
SCHEDULER_POLL_INTERVAL = _env_int("SCHEDULER_POLL_INTERVAL", 60000) / 1000
IPC_POLL_INTERVAL = float(os.environ.get("IPC_POLL_INTERVAL", "1000")) / 1000

# Container
CONTAINER_IMAGE = os.environ.get("CONTAINER_IMAGE", "evoclaw-agent:latest")
CONTAINER_TIMEOUT = _env_int("CONTAINER_TIMEOUT", 30 * 60 * 1000) / 1000
IDLE_TIMEOUT = _env_int("IDLE_TIMEOUT", 30 * 60 * 1000) / 1000
MAX_CONCURRENT_CONTAINERS = _env_int("MAX_CONCURRENT_CONTAINERS", 5)

# Timezone
TIMEZONE = os.environ.get("TZ", os.environ.get("TIMEZONE", "UTC"))

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Gmail
GMAIL_POLL_INTERVAL = _env_int("GMAIL_POLL_INTERVAL", 30)

# WhatsApp
WHATSAPP_WEBHOOK_PORT = _env_int("WHATSAPP_WEBHOOK_PORT", 8080)

# Channels to load (comma-separated, default: telegram)
ENABLED_CHANNELS = [c.strip() for c in os.environ.get("ENABLED_CHANNELS", "telegram").split(",")]

def get_secrets() -> dict:
    return read_env_file([
        "GOOGLE_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "WHATSAPP_TOKEN",
        "SLACK_BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
        "GMAIL_CREDENTIALS_FILE",
    ])

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
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Eve")
TRIGGER_PATTERN = re.compile(rf"^@{re.escape(ASSISTANT_NAME)}\b", re.IGNORECASE)

# Polling
POLL_INTERVAL = _env_int("POLL_INTERVAL", 2000) / 1000  # seconds
SCHEDULER_POLL_INTERVAL = _env_int("SCHEDULER_POLL_INTERVAL", 60000) / 1000
IPC_POLL_INTERVAL = _env_int("IPC_POLL_INTERVAL", 1000) / 1000

# Container
CONTAINER_IMAGE = os.environ.get("CONTAINER_IMAGE", "evoclaw-agent:1.10.23")
CONTAINER_TIMEOUT = _env_int("CONTAINER_TIMEOUT", 30 * 60 * 1000) / 1000
IDLE_TIMEOUT = _env_int("IDLE_TIMEOUT", 30 * 60 * 1000) / 1000
MAX_CONCURRENT_CONTAINERS = _env_int("MAX_CONCURRENT_CONTAINERS", 5)
# Per-container resource limits (Issue #61): prevent runaway agents from OOM-killing the host.
# Set to empty string "" to disable the limit (e.g. CONTAINER_MEMORY="" CONTAINER_CPUS="").
CONTAINER_MEMORY = os.environ.get("CONTAINER_MEMORY", "512m")
CONTAINER_CPUS = os.environ.get("CONTAINER_CPUS", "1.0")

# Timezone
TIMEZONE = os.environ.get("TZ", os.environ.get("TIMEZONE", "UTC"))

# Logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
# Set LOG_FORMAT=json to emit newline-delimited JSON logs compatible with Loki/Datadog.
# Default is "text" (human-readable).
LOG_FORMAT = os.environ.get("LOG_FORMAT", "text").lower()

# Per-group message rate limiting (sliding window)
# A group that sends more than RATE_LIMIT_MAX_MSGS within RATE_LIMIT_WINDOW_SECS
# will have excess messages dropped to protect system fairness.
RATE_LIMIT_MAX_MSGS = _env_int("RATE_LIMIT_MAX_MSGS", 20)
RATE_LIMIT_WINDOW_SECS = _env_int("RATE_LIMIT_WINDOW_SECS", 60)

# Gmail
GMAIL_POLL_INTERVAL = _env_int("GMAIL_POLL_INTERVAL", 30)

# WhatsApp
WHATSAPP_WEBHOOK_PORT = _env_int("WHATSAPP_WEBHOOK_PORT", 8080)

# Dashboard
DASHBOARD_HOST: str = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = _env_int("DASHBOARD_PORT", 8765)
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")  # If set, enables HTTP Basic Auth
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
WEBPORTAL_ENABLED = os.environ.get("WEBPORTAL_ENABLED", "false").lower() == "true"
WEBPORTAL_PORT = _env_int("WEBPORTAL_PORT", 8766)
WEBPORTAL_HOST = os.environ.get("WEBPORTAL_HOST", "127.0.0.1")
HEALTH_PORT = _env_int("HEALTH_PORT", 8767)

# Channels to load (comma-separated, default: telegram)
ENABLED_CHANNELS = [c.strip() for c in os.environ.get("ENABLED_CHANNELS", "telegram").split(",")]

# Keys that can be modified via the dashboard /api/env endpoint
EDITABLE_ENV_KEYS: frozenset = frozenset({
    "CLAUDE_API_KEY",
    "TELEGRAM_TOKEN",
    "WHATSAPP_TOKEN",
    "DISCORD_TOKEN",
    "SLACK_TOKEN",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
    "DASHBOARD_PASSWORD",
    "DASHBOARD_HOST",
    "DASHBOARD_PORT",
    "WEBPORTAL_PORT",
    "POLL_INTERVAL",
    "IPC_POLL_INTERVAL",
    "CONTAINER_IMAGE",
    "MAX_CONCURRENT_CONTAINERS",
    "ASSISTANT_NAME",
    "EVOLUTION_ENABLED",
})


def get_secrets() -> dict:
    return read_env_file([
        "GOOGLE_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "WHATSAPP_TOKEN",
        "SLACK_BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
        "GMAIL_CREDENTIALS_FILE",
    ])

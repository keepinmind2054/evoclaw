"""EvoClaw Host Configuration"""
import os
import platform
import re
import socket
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


def _env_int(key: str, default: int, minimum: int | None = None) -> int:
    """Parse an integer env var, falling back to *default* on bad input.

    BUG-CFG-01 / BUG-CFG-02 FIX: Added optional *minimum* parameter.  When
    provided, values below the minimum are rejected and the default is used
    instead.  This prevents zero/negative values for settings like
    MAX_CONCURRENT_CONTAINERS (which would deadlock the queue) or poll
    intervals (which would create tight CPU-burning loops).
    """
    try:
        val = int(os.environ.get(key, default))
    except (ValueError, TypeError):
        import logging
        logging.getLogger(__name__).warning("Invalid value for %s, using default %d", key, default)
        return default
    if minimum is not None and val < minimum:
        import logging
        logging.getLogger(__name__).warning(
            "Value %d for %s is below minimum %d, using default %d", val, key, minimum, default
        )
        return default
    return val


# p21c: ANTHROPIC_API_KEY alias — promote to CLAUDE_API_KEY at the process level
# so that users who follow the README_en.md Quick Start (which showed
# ANTHROPIC_API_KEY) get Claude rather than silently falling back to Gemini.
# This runs once at import time and is transparent to all downstream consumers.
if os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("CLAUDE_API_KEY"):
    os.environ["CLAUDE_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]

# Assistant
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "Eve") or "Eve"
# p24c: guard against empty ASSISTANT_NAME (e.g. ASSISTANT_NAME="" in .env).
# An empty name produces TRIGGER_PATTERN = "^@\b" which never matches any input,
# silently disabling the trigger and causing the bot to ignore all messages.
if not ASSISTANT_NAME:
    ASSISTANT_NAME = "Eve"
TRIGGER_PATTERN = re.compile(rf"^@{re.escape(ASSISTANT_NAME)}\b", re.IGNORECASE)

# Polling
# BUG-CFG-02 FIX: enforce minimum of 100 ms (0.1 s) for all poll intervals.
# A value of 0 (or negative) produces a tight busy-loop that pegs the CPU.
POLL_INTERVAL = _env_int("POLL_INTERVAL", 2000, minimum=100) / 1000  # seconds
SCHEDULER_POLL_INTERVAL = _env_int("SCHEDULER_POLL_INTERVAL", 60000, minimum=100) / 1000
IPC_POLL_INTERVAL = _env_int("IPC_POLL_INTERVAL", 1000, minimum=100) / 1000

# Container
CONTAINER_IMAGE = os.environ.get("CONTAINER_IMAGE", "evoclaw-agent:latest")
# CONTAINER_TIMEOUT: maximum wall-clock seconds a single container run may take before
# it is force-killed.  Configured as milliseconds in the env var for consistency with
# other interval vars (e.g. POLL_INTERVAL), then divided by 1000 for runtime use.
# Default: 1 800 000 ms = 1800 s = 30 minutes.
# Override via env: CONTAINER_TIMEOUT=60000  (60 s, useful for fast-response groups)
CONTAINER_TIMEOUT = _env_int("CONTAINER_TIMEOUT", 30 * 60 * 1000) / 1000
# Startup sanity check: if CONTAINER_TIMEOUT looks like it was set in seconds
# (i.e. < 1000) instead of milliseconds, warn the operator.  A value of 30
# means 30 ms — almost certainly a misconfiguration (should be 30000 for 30 s).
_raw_container_timeout = _env_int("CONTAINER_TIMEOUT", 30 * 60 * 1000)
if _raw_container_timeout < 1000 and os.environ.get("CONTAINER_TIMEOUT"):
    import warnings as _warnings
    _warnings.warn(
        f"CONTAINER_TIMEOUT={_raw_container_timeout} looks like it may be set in seconds. "
        f"EvoClaw expects milliseconds (e.g., 30000 for 30 seconds). "
        f"Current effective timeout: {CONTAINER_TIMEOUT:.1f}s",
        UserWarning,
        stacklevel=2,
    )
IDLE_TIMEOUT = _env_int("IDLE_TIMEOUT", 30 * 60 * 1000) / 1000
# BUG-CFG-01 FIX: enforce minimum of 1.  A value of 0 or negative makes the
# concurrency check (self._active_count >= MAX_CONCURRENT_CONTAINERS) always
# True so no container ever runs and all work is queued forever.
MAX_CONCURRENT_CONTAINERS = _env_int("MAX_CONCURRENT_CONTAINERS", 5, minimum=1)
# Per-container resource limits (Issue #61): prevent runaway agents from OOM-killing the host.
# Set to empty string "" to disable the limit (e.g. CONTAINER_MEMORY="" CONTAINER_CPUS="").
CONTAINER_MEMORY = os.environ.get("CONTAINER_MEMORY", "512m")
CONTAINER_CPUS = os.environ.get("CONTAINER_CPUS", "1.0")
# CONTAINER_PIDS_LIMIT: maximum number of processes the container may spawn.
# Prevents fork bombs inside an untrusted agent container.
# Set to -1 to disable (not recommended for production).
CONTAINER_PIDS_LIMIT: int = _env_int("CONTAINER_PIDS_LIMIT", 256)
# CONTAINER_LOG_MAX_SIZE / CONTAINER_LOG_MAX_FILES (BUG-19B-01):
# Caps the Docker json-file log size per container so a chatty or looping agent
# cannot fill the host disk through the Docker log driver.
# Format: "<N>m" for megabytes (e.g. "10m").
CONTAINER_LOG_MAX_SIZE: str = os.environ.get("CONTAINER_LOG_MAX_SIZE", "10m")
CONTAINER_LOG_MAX_FILES: str = os.environ.get("CONTAINER_LOG_MAX_FILES", "2")
# CONTAINER_TMPFS_SIZE (BUG-19B-02):
# Size of the tmpfs mounted at /tmp inside each container.  Bounds the amount
# of host memory a container may consume via temporary files (including the
# /tmp/input.json written by entrypoint.sh).  Default: 64m.
CONTAINER_TMPFS_SIZE: str = os.environ.get("CONTAINER_TMPFS_SIZE", "64m")
# CONTAINER_STOP_GRACE_SECS (BUG-19B-03):
# Seconds to wait for SIGTERM to cleanly stop a container before Docker issues
# SIGKILL.  Gives the agent time to flush open file writes and close IPC files
# before being force-killed.  Intentionally short (5 s) to avoid delaying
# overall shutdown.
CONTAINER_STOP_GRACE_SECS: int = _env_int("CONTAINER_STOP_GRACE_SECS", 5, minimum=1)
# CONTAINER_NETWORK: Docker network mode for agent containers.
# Default "bridge" allows agents to call LLM APIs (NIM, OpenAI, Gemini) directly.
# Set to "none" to fully isolate the container from the network (most secure,
# but only works if LLM calls are routed through the host proxy instead).
CONTAINER_NETWORK: str = os.environ.get("CONTAINER_NETWORK", "bridge")

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
# p12b fix: defer the DASHBOARD_PASSWORD warning to a function so it can be called
# after logging is fully configured (previously fired at import time before handlers
# were set up, causing the warning to be emitted by the root logger's default handler
# with inconsistent formatting or, in some configurations, lost entirely).
def warn_dashboard_no_password() -> None:
    """Emit a warning if the dashboard has no authentication configured.

    Call this once from main() after _setup_logging() has run.
    """
    if not DASHBOARD_PASSWORD:
        import logging as _log_cfg
        _log_cfg.getLogger(__name__).warning(
            "DASHBOARD_PASSWORD is not set — dashboard has NO authentication. "
            "Set DASHBOARD_PASSWORD in .env to enable HTTP Basic Auth."
        )
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
WEBPORTAL_ENABLED = os.environ.get("WEBPORTAL_ENABLED", "false").lower() == "true"
WEBPORTAL_PORT = _env_int("WEBPORTAL_PORT", 8766)
WEBPORTAL_HOST = os.environ.get("WEBPORTAL_HOST", "127.0.0.1")
HEALTH_PORT = _env_int("HEALTH_PORT", 8769)

# Channels to load (comma-separated, default: telegram)
# env var takes priority; fall back to .env file so operators can set it there
_env_file_channels = read_env_file(["ENABLED_CHANNELS"]).get("ENABLED_CHANNELS", "")
ENABLED_CHANNELS = [
    c.strip()
    for c in (os.environ.get("ENABLED_CHANNELS") or _env_file_channels or "telegram").split(",")
    if c.strip()
]

# Keys that can be modified via the dashboard /api/env endpoint
# NOTE: POLL_INTERVAL, IPC_POLL_INTERVAL, SCHEDULER_POLL_INTERVAL and
# CONTAINER_TIMEOUT are all specified in MILLISECONDS in the environment
# (e.g. POLL_INTERVAL=2000 means 2 seconds).  Operators editing these via
# the dashboard must supply millisecond values, not second values.
EDITABLE_ENV_KEYS: frozenset = frozenset({
    "CLAUDE_API_KEY",
    "TELEGRAM_BOT_TOKEN",   # p12b fix: was TELEGRAM_TOKEN — channel code reads TELEGRAM_BOT_TOKEN
    "WHATSAPP_TOKEN",
    "DISCORD_BOT_TOKEN",    # p12b fix: was DISCORD_TOKEN — channel code reads DISCORD_BOT_TOKEN
    "SLACK_BOT_TOKEN",      # p12b fix: was SLACK_TOKEN — channel code reads SLACK_BOT_TOKEN
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
    # DASHBOARD_PASSWORD intentionally excluded — password changes require env restart (Fix #191)
    "DASHBOARD_HOST",
    "DASHBOARD_PORT",
    "WEBPORTAL_PORT",
    "POLL_INTERVAL",        # milliseconds — e.g. 2000 = 2 s
    "IPC_POLL_INTERVAL",    # milliseconds — e.g. 1000 = 1 s
    "CONTAINER_IMAGE",
    "MAX_CONCURRENT_CONTAINERS",
    "ASSISTANT_NAME",
    "EVOLUTION_ENABLED",
})


# Database (optional — defaults to SQLite)
DATABASE_URL: str = os.environ.get("DATABASE_URL", "")  # e.g. postgresql://user:pass@host:5432/dbname

# Multi-instance Leader Election
LEADER_ELECTION_ENABLED: bool = os.environ.get("LEADER_ELECTION_ENABLED", "false").lower() == "true"
LEADER_HEARTBEAT_INTERVAL: int = _env_int("LEADER_HEARTBEAT_INTERVAL", 10)  # p12b fix: use _env_int for safe coercion
LEADER_LEASE_TIMEOUT: int = _env_int("LEADER_LEASE_TIMEOUT", 30)            # p12b fix: use _env_int for safe coercion
INSTANCE_ID: str = os.environ.get("INSTANCE_ID", f"{socket.gethostname()}:{os.getpid()}")


def get_secrets() -> dict:
    return read_env_file([
        "GOOGLE_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "WHATSAPP_TOKEN",
        "SLACK_BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
        "GMAIL_CREDENTIALS_FILE",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    ])

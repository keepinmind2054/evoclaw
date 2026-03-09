"""Constants for the Evoclaw skills engine."""

EVOCLAW_DIR = ".evoclaw"
STATE_FILE = "state.yaml"
BASE_DIR = ".evoclaw/base"
BACKUP_DIR = ".evoclaw/backup"
LOCK_FILE = ".evoclaw/lock"
CUSTOM_DIR = ".evoclaw/custom"
SKILLS_SCHEMA_VERSION = "0.1.0"

# Top-level paths to include in base snapshot and upstream extraction.
# Add new entries here when new root-level directories/files need tracking.
BASE_INCLUDES = [
    "host/",
    "container/",
    "run.py",
    ".env.example",
]

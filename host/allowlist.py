"""Sender and mount allowlist management."""
import json
import logging
from pathlib import Path
from . import config

log = logging.getLogger(__name__)

def load_sender_allowlist() -> set[str]:
    """Load sender allowlist from config. Empty set = allow all."""
    path = config.SENDER_ALLOWLIST_FILE
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        senders = set(data.get("senders", []))
        log.info(f"Sender allowlist loaded: {len(senders)} entries")
        return senders
    except Exception as e:
        log.warning(f"Failed to load sender allowlist: {e}")
        return set()

def is_sender_allowed(sender_id: str, allowlist: set[str]) -> bool:
    """Return True if sender is allowed. Empty allowlist = allow all."""
    if not allowlist:
        return True
    return sender_id in allowlist

def load_mount_allowlist() -> list[str]:
    """Load mount allowlist from config."""
    path = config.MOUNT_ALLOWLIST_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("mounts", [])
    except Exception as e:
        log.warning(f"Failed to load mount allowlist: {e}")
        return []

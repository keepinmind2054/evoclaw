"""Sender and mount allowlist management."""
import json
import logging
from pathlib import Path
from . import config

log = logging.getLogger(__name__)

# BUG-AL-1: The original code used "empty allowlist = allow all" semantics.
# This is dangerous: if the allowlist file is accidentally deleted or corrupt,
# the system silently opens up to all senders.  The correct secure default is
# to DENY all when the allowlist cannot be loaded, unless the operator has
# explicitly opted into open mode via SENDER_ALLOWLIST_ALLOW_ALL_IF_MISSING=true.
#
# The opt-in env var is checked at module load time so that the decision is
# logged once during startup, not silently on every message.
import os as _os
_ALLOW_ALL_IF_MISSING: bool = (
    _os.environ.get("SENDER_ALLOWLIST_ALLOW_ALL_IF_MISSING", "false").lower() == "true"
)
if _ALLOW_ALL_IF_MISSING:
    log.warning(
        "allowlist: SENDER_ALLOWLIST_ALLOW_ALL_IF_MISSING=true — "
        "a missing or unreadable allowlist file will permit ALL senders. "
        "This is insecure for production deployments."
    )


def load_sender_allowlist() -> set[str]:
    """Load sender allowlist from config.

    Returns a non-empty set of allowed sender IDs on success.

    BUG-AL-1 FIX: On failure (file missing, unreadable, or parse error) the
    return value depends on SENDER_ALLOWLIST_ALLOW_ALL_IF_MISSING:
      - false (default): returns the sentinel value {""} (a set containing an
        empty string which no real sender_id will ever match) so that
        is_sender_allowed() safely denies all senders rather than allowing all.
      - true: returns empty set() to preserve the original "allow all" behaviour
        for operators who explicitly opt into it.

    Callers can distinguish "allow-all" mode (empty set) from "deny-all due to
    load failure" ({""}) by checking ``not allowlist`` vs ``allowlist == {""}``,
    but most callers just pass the result directly to is_sender_allowed() which
    handles both cases correctly.
    """
    path = config.SENDER_ALLOWLIST_FILE
    if not path.exists():
        if _ALLOW_ALL_IF_MISSING:
            return set()  # explicit opt-in: allow all
        log.warning(
            "allowlist: sender allowlist file not found at %s — "
            "denying all senders (set SENDER_ALLOWLIST_ALLOW_ALL_IF_MISSING=true to allow all)",
            path,
        )
        return {""}  # BUG-AL-1 FIX: sentinel deny-all set
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        senders = set(data.get("senders", []))
        if not senders:
            # Empty senders list in file — treat as explicit allow-all (file exists,
            # operator intentionally left it empty).
            log.info("Sender allowlist loaded: 0 entries — allowing all senders (file present but empty)")
            return set()
        log.info(f"Sender allowlist loaded: {len(senders)} entries")
        return senders
    except Exception as e:
        log.warning(f"Failed to load sender allowlist: {e}")
        if _ALLOW_ALL_IF_MISSING:
            return set()
        return {""}  # BUG-AL-1 FIX: sentinel deny-all set on parse error


def is_sender_allowed(sender_id: str, allowlist: set[str]) -> bool:
    """Return True if sender is allowed.

    Semantics:
    - Empty set (set()): allow all (explicit opt-in or empty allowlist file).
    - Non-empty set: only allow senders in the set.
    - {""}  sentinel: deny all (allowlist could not be loaded and
      SENDER_ALLOWLIST_ALLOW_ALL_IF_MISSING is false).

    BUG-AL-2 FIX: Normalise sender_id to strip whitespace before lookup to
    prevent trivial bypasses via e.g. sender_id=" 123456" vs "123456".
    """
    if not allowlist:
        return True  # empty set = allow-all mode
    return sender_id.strip() in allowlist


def load_mount_allowlist() -> list[str]:
    """Load mount allowlist from config."""
    path = config.MOUNT_ALLOWLIST_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("mounts", [])
    except Exception as e:
        log.warning(f"Failed to load mount allowlist: {e}")
        return []

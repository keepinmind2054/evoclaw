"""Hot memory management — MEMORY.md per group (8KB limit)."""
from __future__ import annotations
import logging
import time
from .. import db

log = logging.getLogger(__name__)

HOT_MEMORY_MAX_BYTES = 8 * 1024  # 8KB


def _safe_truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes without splitting a
    multi-byte character.

    Python's ``bytes[:n].decode("utf-8", errors="ignore")`` silently drops
    any partial multi-byte sequence that straddles the cut point, which is
    correct but easy to miss.  This helper makes the intent explicit and
    ensures we never exceed the byte limit.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Walk back from the cut point until we land on a valid UTF-8 boundary.
    # UTF-8 continuation bytes have the form 10xxxxxx (0x80–0xBF).
    cut = max_bytes
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    return encoded[:cut].decode("utf-8", errors="ignore")


def get_hot_memory(jid: str) -> str:
    """Return the hot memory content for a group, or empty string."""
    row = db.get_hot_memory(jid)
    return row or ""


def update_hot_memory(jid: str, content: str) -> None:
    """Update hot memory for a group, enforcing 8KB limit.

    Bug fixed (p14b-1): previous code used
    ``encoded[:MAX].decode("utf-8", errors="ignore")`` which silently
    discards partial multi-byte characters at the boundary.  We now walk
    back to a clean UTF-8 boundary before decoding.
    """
    encoded = content.encode("utf-8")
    if len(encoded) > HOT_MEMORY_MAX_BYTES:
        content = _safe_truncate_utf8(content, HOT_MEMORY_MAX_BYTES)
        log.warning("hot_memory: content truncated to 8KB for jid=%s", jid)
    db.set_hot_memory(jid, content)
    log.debug("hot_memory: updated for jid=%s (%d bytes)", jid, len(content.encode()))

"""Hot memory management — MEMORY.md per group (8KB limit)."""
from __future__ import annotations
import logging
import time
from .. import db

log = logging.getLogger(__name__)

HOT_MEMORY_MAX_BYTES = 8 * 1024  # 8KB


def get_hot_memory(jid: str) -> str:
    """Return the hot memory content for a group, or empty string."""
    row = db.get_hot_memory(jid)
    return row or ""


def update_hot_memory(jid: str, content: str) -> None:
    """Update hot memory for a group, enforcing 8KB limit."""
    encoded = content.encode("utf-8")
    if len(encoded) > HOT_MEMORY_MAX_BYTES:
        # Truncate to 8KB boundary
        content = encoded[:HOT_MEMORY_MAX_BYTES].decode("utf-8", errors="ignore")
        log.warning("hot_memory: content truncated to 8KB for jid=%s", jid)
    db.set_hot_memory(jid, content)
    log.debug("hot_memory: updated for jid=%s (%d bytes)", jid, len(content.encode()))

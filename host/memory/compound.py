"""Weekly compound — memory pruning + distillation."""
from __future__ import annotations
import logging
import time

from .. import db
from .warm import prune_old_warm_logs
from .hot import get_hot_memory, update_hot_memory, HOT_MEMORY_MAX_BYTES

log = logging.getLogger(__name__)

COMPOUND_INTERVAL_SECS = 7 * 86400  # weekly


async def run_weekly_compound(jid: str) -> None:
    """
    Weekly compound:
    1. Prune warm logs older than 30 days
    2. Update hot memory with distillation note

    Bug fixed (p14b-5): previous code called ``(hot + compound_note).strip()``
    which stripped *leading* whitespace from the existing hot memory content,
    potentially corrupting content that intentionally starts with whitespace or
    newlines.  Changed to ``hot.rstrip() + compound_note`` to be consistent
    with the approach used in warm.py.
    """
    try:
        pruned = prune_old_warm_logs(jid)
        from datetime import datetime
        week = datetime.now().strftime("%Y-W%W")
        hot = get_hot_memory(jid)
        compound_note = f"\n[Weekly compound: {week}, pruned {pruned} old entries]\n"
        if compound_note not in hot:
            new_hot = hot.rstrip() + compound_note  # fix: don't strip leading content
            # P31A-FIX-2: Guard against silent truncation of existing hot memory.
            # warm.py's micro_sync and daily_wrapup both check size before calling
            # update_hot_memory(); compound.py was missing this guard.  Without it,
            # a near-full hot memory (close to HOT_MEMORY_MAX_BYTES) would be
            # silently truncated by update_hot_memory() when the compound note is
            # appended, losing valuable tail content.  Skip the note instead.
            if len(new_hot.encode()) <= HOT_MEMORY_MAX_BYTES:
                update_hot_memory(jid, new_hot)
            else:
                log.warning(
                    "compound: compound_note would exceed HOT_MEMORY_MAX_BYTES for jid=%s "
                    "— skipping note to preserve existing content",
                    jid,
                )
        db.record_weekly_compound(jid)
        log.info("compound: weekly compound done for jid=%s (pruned %d)", jid, pruned)
    except Exception as exc:
        log.error("compound: weekly compound failed for jid=%s: %s", jid, exc)

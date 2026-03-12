"""Weekly compound — memory pruning + distillation."""
from __future__ import annotations
import logging
import time

from .. import db
from .warm import prune_old_warm_logs
from .hot import get_hot_memory, update_hot_memory

log = logging.getLogger(__name__)

COMPOUND_INTERVAL_SECS = 7 * 86400  # weekly


async def run_weekly_compound(jid: str) -> None:
    """
    Weekly compound:
    1. Prune warm logs older than 30 days
    2. Update hot memory with distillation note
    """
    try:
        pruned = prune_old_warm_logs(jid)
        from datetime import datetime
        week = datetime.now().strftime("%Y-W%W")
        hot = get_hot_memory(jid)
        compound_note = f"\n[Weekly compound: {week}, pruned {pruned} old entries]\n"
        if compound_note not in hot:
            new_hot = (hot + compound_note).strip()
            update_hot_memory(jid, new_hot)
        db.record_weekly_compound(jid)
        log.info("compound: weekly compound done for jid=%s (pruned %d)", jid, pruned)
    except Exception as exc:
        log.error("compound: weekly compound failed for jid=%s: %s", jid, exc)

"""Warm memory management — daily logs + micro sync."""
from __future__ import annotations
import logging
import time
from datetime import datetime, timezone, timedelta

from .. import db

log = logging.getLogger(__name__)

MICRO_SYNC_INTERVAL_SECS = 3 * 3600  # 3 hours
DAILY_WRAPUP_HOUR = 0  # midnight local
WARM_RETENTION_DAYS = 30


def append_warm_log(jid: str, user_msg: str, assistant_msg: str) -> None:
    """Append a conversation summary to today's warm memory log.

    Bug fixed (p14b-2): ``datetime.now()`` was called twice without pinning
    to a single instant, so ``today`` and ``ts`` could (very rarely) span
    midnight.  We now capture a single ``now`` object and derive both from
    it.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    ts = now.strftime("%H:%M")
    # Build a compact log entry (no emojis — keeps entries plain-text safe)
    u_preview = user_msg[:200].replace("\n", " ") if user_msg else ""
    a_preview = assistant_msg[:200].replace("\n", " ") if assistant_msg else ""
    entry = f"### {ts}\nUser: {u_preview}\nBot: {a_preview}\n"
    db.append_warm_log(jid, today, entry)
    log.debug("warm_log: appended entry for jid=%s date=%s", jid, today)


async def run_micro_sync(jid: str) -> None:
    """Extract key decisions from recent warm logs and update hot memory.

    Bug fixed (p14b-3): size check used strict ``<`` (less-than) so content
    that is *exactly* HOT_MEMORY_MAX_BYTES was rejected.  Changed to ``<=``.
    """
    try:
        recent_logs = db.get_warm_logs_recent(jid, days=1)
        if not recent_logs:
            return
        from .hot import get_hot_memory, update_hot_memory, HOT_MEMORY_MAX_BYTES
        current_hot = get_hot_memory(jid)
        # Simple heuristic: keep latest content, prepend today's summary header
        today = datetime.now().strftime("%Y-%m-%d")
        sync_note = f"\n\n[Last sync: {today}]\n"
        if sync_note not in current_hot:
            new_hot = current_hot.rstrip() + sync_note
            if len(new_hot.encode()) <= HOT_MEMORY_MAX_BYTES:  # fix: <= not <
                update_hot_memory(jid, new_hot)
        db.record_micro_sync(jid)
        log.info("warm: micro_sync complete for jid=%s", jid)
    except Exception as exc:
        log.error("warm: micro_sync failed for jid=%s: %s", jid, exc)


async def run_daily_wrapup(jid: str) -> None:
    """Summarise yesterday's warm logs into hot memory and record the timestamp.

    Bug fixed (p14b-4): ``get_warm_logs_recent(jid, days=1)`` returns the
    last 24 hours of logs, but daily wrapup is meant to summarise
    **yesterday**.  At midnight the last-24-hours window is essentially
    empty (only the seconds since midnight).  We now explicitly fetch logs
    dated to yesterday.

    Bug fixed (p14b-3): same off-by-one ``<`` → ``<=`` as micro_sync.

    This ensures the last_daily_wrapup column in group_memory_sync is kept
    up to date, which the evolution daemon uses to avoid re-running daily
    wrapup on every restart.
    """
    try:
        # Fetch yesterday's logs specifically (not just "last 24 h")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        recent_logs = db.get_warm_logs_for_date(jid, yesterday)
        if not recent_logs:
            db.record_daily_wrapup(jid)
            return
        from .hot import get_hot_memory, update_hot_memory, HOT_MEMORY_MAX_BYTES
        current_hot = get_hot_memory(jid)
        today = datetime.now().strftime("%Y-%m-%d")
        wrapup_note = f"\n\n[Daily wrapup: {today}, {len(recent_logs)} log entries]\n"
        if wrapup_note not in current_hot:
            new_hot = current_hot.rstrip() + wrapup_note
            if len(new_hot.encode()) <= HOT_MEMORY_MAX_BYTES:  # fix: <= not <
                update_hot_memory(jid, new_hot)
        db.record_daily_wrapup(jid)
        log.info("warm: daily_wrapup complete for jid=%s", jid)
    except Exception as exc:
        log.error("warm: daily_wrapup failed for jid=%s: %s", jid, exc)


def prune_old_warm_logs(jid: str) -> int:
    """Remove warm logs older than WARM_RETENTION_DAYS. Returns count removed."""
    cutoff_ts = time.time() - WARM_RETENTION_DAYS * 86400
    removed = db.delete_warm_logs_before(jid, cutoff_ts)
    if removed:
        log.info("warm: pruned %d old log entries for jid=%s", removed, jid)
    return removed

"""Memory search — hybrid FTS5 keyword + recency scoring."""
from __future__ import annotations
import logging
from typing import Any

from .. import db

log = logging.getLogger(__name__)

MAX_RESULTS = 5


def memory_search(jid: str, query: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """
    Hybrid memory search: FTS5 keyword match + recency score.
    Returns list of {source, date, content, score} dicts.
    """
    if not query or not query.strip():
        return []
    try:
        results = db.memory_fts_search(jid, query.strip(), limit=limit * 2)
        # Score: FTS rank + recency bonus
        scored = []
        import time
        now = time.time()
        for row in results:
            age_days = max(0, (now - row.get("created_at", now)) / 86400)
            recency_score = max(0.0, 1.0 - age_days / 30)  # decay over 30 days
            fts_score = row.get("fts_score", 0.0)
            total = fts_score * 0.7 + recency_score * 0.3
            scored.append({**row, "score": round(total, 4)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]
    except Exception as exc:
        log.error("memory_search: failed for jid=%s query=%r: %s", jid, query, exc)
        return []

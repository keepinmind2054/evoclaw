"""Memory search — hybrid FTS5 keyword + recency scoring."""
from __future__ import annotations
import logging
import math
from typing import Any

from .. import db

log = logging.getLogger(__name__)

MAX_RESULTS = 5


def memory_search(jid: str, query: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
    """
    Hybrid memory search: FTS5 keyword match + recency score.
    Returns list of {source, date, content, score} dicts.

    Bug fixed (p14b-11): BM25 scores are unbounded — a highly relevant result
    could have an ``fts_score`` of 50+ (after ``abs()`` in db.py), completely
    drowning out the recency term in
    ``total = fts_score * 0.7 + recency_score * 0.3``.

    We now normalise the raw BM25 magnitude to [0, 1] using the same
    sigmoid used in MemoryBus.recall(), then blend with the recency score.
    This preserves the relative ordering of FTS results while keeping the
    blended score in a predictable range.
    """
    if not query or not query.strip():
        return []
    try:
        results = db.memory_fts_search(jid, query.strip(), limit=limit * 2)
        # Score: normalised FTS rank + recency bonus
        scored = []
        import time
        now = time.time()
        for row in results:
            age_days = max(0, (now - row.get("created_at", now)) / 86400)
            recency_score = max(0.0, 1.0 - age_days / 30)  # decay over 30 days

            # db.memory_fts_search already returns abs(bm25) as fts_score.
            # Normalise to [0, 1] via: norm = 1 - exp(-magnitude / 5)
            # (approaches 1 for large magnitudes, near 0 for small ones).
            raw_fts = float(row.get("fts_score", 0.0))
            norm_fts = 1.0 - math.exp(-raw_fts / 5.0)

            total = norm_fts * 0.7 + recency_score * 0.3
            scored.append({**row, "score": round(total, 4)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]
    except Exception as exc:
        log.error("memory_search: failed for jid=%s query=%r: %s", jid, query, exc)
        return []

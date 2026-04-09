"""
PalaceStore — namespace/topic-aware memory retrieval.
Inspired by mempalace wing/room hierarchy.
Adds two-level metadata filtering on top of shared_memories FTS5.
"""
import logging
import sqlite3
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Keyword classifier (reuse logic from warm.py _MEMORY_KEYWORDS)
_NAMESPACE_KEYWORDS = {
    "technical": ["code", "bug", "error", "function", "class", "api", "database", "sql", "python", "git", "docker"],
    "planning": ["plan", "task", "deadline", "milestone", "sprint", "goal", "roadmap", "priority"],
    "personal": ["prefer", "like", "dislike", "feel", "want", "need", "habit", "style"],
    "project": ["project", "feature", "requirement", "deploy", "release", "version"],
}
_TOPIC_KEYWORDS = {
    "decisions": ["decided", "decision", "chose", "switched", "agreed", "will use"],
    "preferences": ["prefer", "like", "always", "usually", "typically", "favorite"],
    "problems": ["error", "bug", "issue", "broken", "fail", "problem", "crash"],
    "facts": ["is", "are", "was", "means", "defined as", "equals"],
    "milestones": ["completed", "done", "finished", "shipped", "deployed", "merged"],
    "tasks": ["todo", "need to", "should", "must", "will", "going to"],
}


class PalaceStore:
    """
    Two-level (namespace/topic_tag) hierarchical memory store built on top of
    the shared_memories table.

    namespace  — broad domain bucket (e.g. "technical", "planning", "personal")
    topic_tag  — fine-grained category within a namespace (e.g. "decisions",
                 "problems", "milestones")

    Both levels are populated automatically by classify() using keyword scoring
    and can also be set explicitly by callers via MemoryBus.remember().
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_topics(self, namespace: str = "") -> list[str]:
        """
        Return distinct topic_tag values stored in shared_memories.

        If *namespace* is provided, filter to that namespace only.
        Results are ordered alphabetically.
        """
        try:
            with self._lock:
                if namespace:
                    rows = self._conn.execute(
                        """SELECT DISTINCT topic_tag
                           FROM shared_memories
                           WHERE namespace = ?
                             AND topic_tag != ''
                           ORDER BY topic_tag""",
                        (namespace,),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        """SELECT DISTINCT topic_tag
                           FROM shared_memories
                           WHERE topic_tag != ''
                           ORDER BY topic_tag""",
                    ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.Error as exc:
            logger.warning("PalaceStore.list_topics error: %s", exc)
            return []

    def topic_summary(self, topic_tag: str, namespace: str = "", k: int = 10) -> list[dict]:
        """
        Return the *k* most important memories for a given topic_tag.

        Optionally restrict to a specific namespace.  Results are ordered by
        importance DESC, then created_at DESC (most important and most recent
        first).
        """
        try:
            with self._lock:
                if namespace:
                    rows = self._conn.execute(
                        """SELECT id, content, agent_id, scope, namespace,
                                  topic_tag, importance, created_at
                           FROM shared_memories
                           WHERE topic_tag = ?
                             AND namespace = ?
                           ORDER BY importance DESC, created_at DESC
                           LIMIT ?""",
                        (topic_tag, namespace, k),
                    ).fetchall()
                else:
                    rows = self._conn.execute(
                        """SELECT id, content, agent_id, scope, namespace,
                                  topic_tag, importance, created_at
                           FROM shared_memories
                           WHERE topic_tag = ?
                           ORDER BY importance DESC, created_at DESC
                           LIMIT ?""",
                        (topic_tag, k),
                    ).fetchall()
            return [
                {
                    "id": r[0],
                    "content": r[1],
                    "agent_id": r[2],
                    "scope": r[3],
                    "namespace": r[4],
                    "topic_tag": r[5],
                    "importance": r[6],
                    "created_at": r[7],
                }
                for r in rows
            ]
        except sqlite3.Error as exc:
            logger.warning("PalaceStore.topic_summary error: %s", exc)
            return []

    def search(
        self,
        query: str,
        namespace: str = "",
        topic_tag: str = "",
        k: int = 5,
    ) -> list[dict]:
        """
        FTS5 search on shared_memories filtered by namespace and/or topic_tag.

        At least one of *namespace* or *topic_tag* should be non-empty for
        useful hierarchical filtering; if both are empty this degrades to a
        plain FTS5 search without metadata filters (equivalent to
        SharedMemoryStore.search but without the scope check).

        Results include all accessible memories (scope is not restricted here;
        callers should use MemoryBus.recall() for scope-aware retrieval).
        """
        try:
            with self._lock:
                # Build WHERE clause dynamically to avoid unnecessary ANDs.
                extra_clauses = []
                params: list = [query]
                if namespace:
                    extra_clauses.append("sm.namespace = ?")
                    params.append(namespace)
                if topic_tag:
                    extra_clauses.append("sm.topic_tag = ?")
                    params.append(topic_tag)
                params.append(k)

                extra_where = ""
                if extra_clauses:
                    extra_where = " AND " + " AND ".join(extra_clauses)

                rows = self._conn.execute(
                    f"""SELECT sm.id, sm.content, sm.agent_id, sm.scope,
                               sm.namespace, sm.topic_tag,
                               sm.importance, sm.created_at,
                               rank AS fts_rank
                        FROM shared_memories_fts fts
                        JOIN shared_memories sm ON sm.rowid = fts.rowid
                        WHERE shared_memories_fts MATCH ?
                          {extra_where}
                        ORDER BY fts_rank, sm.importance DESC
                        LIMIT ?""",
                    params,
                ).fetchall()
            return [
                {
                    "id": r[0],
                    "content": r[1],
                    "agent_id": r[2],
                    "scope": r[3],
                    "namespace": r[4],
                    "topic_tag": r[5],
                    "importance": r[6],
                    "created_at": r[7],
                    "fts_rank": r[8],
                }
                for r in rows
            ]
        except sqlite3.Error as exc:
            logger.warning("PalaceStore.search error: %s", exc)
            return []

    def classify(self, content: str) -> tuple[str, str]:
        """
        Return (namespace, topic_tag) derived from keyword scoring of *content*.

        Each candidate label accumulates a score equal to the number of its
        keywords that appear (case-insensitively) in *content*.  The label
        with the highest score wins; ties are broken by dict insertion order
        (i.e. the first label defined in the keyword tables).  If no keyword
        matches at all, both values default to empty string.
        """
        lower = content.lower()

        # Score namespaces
        ns_scores: dict[str, int] = {ns: 0 for ns in _NAMESPACE_KEYWORDS}
        for ns, keywords in _NAMESPACE_KEYWORDS.items():
            for kw in keywords:
                if kw in lower:
                    ns_scores[ns] += 1

        # Score topic tags
        topic_scores: dict[str, int] = {t: 0 for t in _TOPIC_KEYWORDS}
        for topic, keywords in _TOPIC_KEYWORDS.items():
            for kw in keywords:
                if kw in lower:
                    topic_scores[topic] += 1

        best_ns = max(ns_scores, key=lambda k: ns_scores[k])
        best_topic = max(topic_scores, key=lambda k: topic_scores[k])

        namespace = best_ns if ns_scores[best_ns] > 0 else ""
        topic_tag = best_topic if topic_scores[best_topic] > 0 else ""

        return namespace, topic_tag

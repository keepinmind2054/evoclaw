"""
MemoryBus — Universal Memory Layer Interface
Phase 1 of UnifiedClaw architecture

Provides a unified interface for all memory operations across agents:
- Hot:    per-agent MEMORY.md (injected at container start)
- Shared: cross-agent readable/writable knowledge store (NEW)
- Vector: sqlite-vec semantic search (NEW - requires sqlite-vec extension)
- Cold:   FTS5 full-text search with time decay (existing)

Usage:
    bus = MemoryBus(db_conn, groups_dir)
    
    # Store a memory
    await bus.remember("User prefers concise answers", agent_id="myagent", scope="private")
    
    # Recall relevant memories
    memories = await bus.recall("user preferences", agent_id="myagent", k=5)
    
    # Share knowledge across agents
    await bus.remember("Project deadline is March 31", agent_id="myagent", scope="shared")
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Literal, Optional

from .palace_store import PalaceStore

logger = logging.getLogger(__name__)

MemoryScope = Literal["private", "shared", "project"]


@dataclass
class Memory:
    """A single recalled memory unit."""
    memory_id: str
    content: str
    agent_id: str
    scope: MemoryScope
    score: float  # relevance score (0.0 - 1.0)
    created_at: float
    source: Literal["hot", "shared", "vector", "cold"]
    metadata: dict = field(default_factory=dict)

    @property
    def age_hours(self) -> float:
        return (time.time() - self.created_at) / 3600


class SharedMemoryStore:
    """
    Cross-agent shared memory store backed by SQLite.
    
    Agents can write memories with scope:
    - "private":  only readable by the owning agent
    - "shared":   readable by all agents
    - "project":  readable by agents in the same project
    
    Table schema (created on first use):
        shared_memories(
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            project TEXT DEFAULT '',
            scope TEXT NOT NULL,
            content TEXT NOT NULL,
            importance REAL DEFAULT 0.5,
            access_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """

    TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS shared_memories (
        id          TEXT    PRIMARY KEY,
        agent_id    TEXT    NOT NULL,
        project     TEXT    NOT NULL DEFAULT '',
        scope       TEXT    NOT NULL DEFAULT 'private',
        content     TEXT    NOT NULL,
        importance  REAL    NOT NULL DEFAULT 0.5,
        access_count INTEGER NOT NULL DEFAULT 0,
        created_at  REAL    NOT NULL,
        updated_at  REAL    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_shared_memories_scope
        ON shared_memories(scope, project);
    CREATE INDEX IF NOT EXISTS idx_shared_memories_agent
        ON shared_memories(agent_id);
    CREATE VIRTUAL TABLE IF NOT EXISTS shared_memories_fts
        USING fts5(content, content='shared_memories', content_rowid='rowid');
    CREATE TRIGGER IF NOT EXISTS shared_memories_ai AFTER INSERT ON shared_memories BEGIN
      INSERT INTO shared_memories_fts(rowid, content) VALUES (new.rowid, new.content);
    END;
    CREATE TRIGGER IF NOT EXISTS shared_memories_au AFTER UPDATE ON shared_memories BEGIN
      INSERT INTO shared_memories_fts(shared_memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
      INSERT INTO shared_memories_fts(rowid, content) VALUES (new.rowid, new.content);
    END;
    CREATE TRIGGER IF NOT EXISTS shared_memories_ad AFTER DELETE ON shared_memories BEGIN
      INSERT INTO shared_memories_fts(shared_memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    END;
    """

    def __init__(self, conn: sqlite3.Connection, db_path: str = ""):
        # MEM-01: each Store owns its own sqlite3.Connection so that
        # SharedMemoryStore and VectorStore cannot corrupt each other's
        # transaction state through a shared connection object.
        # For in-memory databases (db_path == "") we reuse the caller's
        # connection to preserve test-fixture behaviour (each :memory: URL
        # is a distinct database; opening a second connection would give an
        # empty, unrelated database).
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        else:
            self._conn = conn
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self):
        """Create tables if they don't exist.

        Bug fixed (p14b-15): the original code split TABLE_DDL on ";" and
        executed each fragment with conn.execute().  CREATE TRIGGER statements
        contain ";" inside their BEGIN...END body, so the naive split produced
        incomplete fragments that caused an "incomplete input" error and left
        the schema in a broken state.  We now use conn.executescript() which
        handles the full DDL correctly as a single batch.
        """
        try:
            with self._lock:
                self._conn.executescript(self.TABLE_DDL)
                # executescript issues an implicit COMMIT, but call it
                # explicitly to be consistent with the rest of the module.
                self._conn.commit()
        except sqlite3.Error as e:
            logger.error(f"SharedMemoryStore schema error: {e}")

    def write(
        self,
        content: str,
        agent_id: str,
        scope: MemoryScope = "private",
        project: str = "",
        importance: float = 0.5,
        namespace: str = "",
        topic_tag: str = "",
    ) -> str:
        """Store a memory. Returns memory_id."""
        memory_id = hashlib.sha256(
            f"{agent_id}:{content}:{time.time()}".encode()
        ).hexdigest()[:16]
        now = time.time()
        try:
            with self._lock:
                try:
                    self._conn.execute(
                        """INSERT INTO shared_memories
                           (id, agent_id, project, scope, content, importance,
                            namespace, topic_tag, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (memory_id, agent_id, project, scope, content, importance,
                         namespace, topic_tag, now, now),
                    )
                    self._conn.commit()
                except sqlite3.Error:
                    self._conn.rollback()
                    raise
            logger.debug(
                f"SharedMemory written: {memory_id} scope={scope} ns={namespace} topic={topic_tag}"
            )
        except sqlite3.Error as e:
            logger.error(f"SharedMemory write error: {e}")
        return memory_id

    def search(
        self,
        query: str,
        agent_id: str,
        project: str = "",
        k: int = 5,
    ) -> list[dict]:
        """FTS5 search across accessible memories."""
        try:
            with self._lock:
                # MEM-02: wrap the SELECT and the subsequent UPDATE in a single
                # BEGIN IMMEDIATE transaction so that no other writer can slip
                # in between the two statements.  Without this, a concurrent
                # write could change access_count between our SELECT and UPDATE,
                # producing a lost-update.
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    rows = self._conn.execute(
                        """SELECT sm.id, sm.content, sm.agent_id, sm.scope,
                                  sm.importance, sm.created_at,
                                  rank as fts_rank
                           FROM shared_memories_fts fts
                           JOIN shared_memories sm ON sm.rowid = fts.rowid
                           WHERE shared_memories_fts MATCH ?
                             AND (sm.scope = 'shared'
                                  OR (sm.scope = 'private' AND sm.agent_id = ?)
                                  OR (sm.scope = 'project' AND sm.project = ?))
                           ORDER BY fts_rank, sm.importance DESC
                           LIMIT ?""",
                        (query, agent_id, project, k),
                    ).fetchall()
                    # Batch increment access counts
                    ids = [row[0] for row in rows]
                    if ids:
                        placeholders = ",".join("?" * len(ids))
                        self._conn.execute(
                            f"UPDATE shared_memories SET access_count=access_count+1 WHERE id IN ({placeholders})",
                            ids
                        )
                    self._conn.commit()
                except sqlite3.Error:
                    self._conn.rollback()
                    raise
            return [
                {
                    "id": r[0], "content": r[1], "agent_id": r[2],
                    "scope": r[3], "importance": r[4], "created_at": r[5],
                    "fts_rank": r[6],
                }
                for r in rows
            ]
        except sqlite3.Error as e:
            logger.warning(f"SharedMemory search error: {e}")
            return []

    def delete(self, memory_id: str, agent_id: str) -> bool:
        """Delete a memory.

        Only the owning agent may delete any of their memories.

        Bug fixed (p14b-6): the previous condition
        ``agent_id = ? OR scope != 'private'`` allowed *any* agent to delete
        shared or project-scoped memories they did not own.  The correct rule
        is: only the owner (``agent_id``) may delete, regardless of scope.
        """
        try:
            with self._lock:
                try:
                    result = self._conn.execute(
                        "DELETE FROM shared_memories WHERE id = ? AND agent_id = ?",
                        (memory_id, agent_id),
                    )
                    self._conn.commit()
                except sqlite3.Error:
                    self._conn.rollback()
                    raise
            return result.rowcount > 0
        except sqlite3.Error as e:
            logger.error(f"SharedMemory delete error: {e}")
            return False


class VectorStore:
    """
    Semantic vector search using sqlite-vec.
    
    Falls back gracefully to FTS5 if sqlite-vec is not available.
    
    To enable: pip install sqlite-vec
    Extension loads automatically if installed.
    
    Embedding generation:
    - Primary:  Gemini text-embedding-004 API (requires GOOGLE_API_KEY)
    - Fallback: Simple TF-IDF approximation (no external deps)
    """

    VECTOR_DIM = 768  # Gemini text-embedding-004 dimension

    def __init__(self, conn: sqlite3.Connection, db_path: str = ""):
        # MEM-01: each Store owns its own sqlite3.Connection so that
        # SharedMemoryStore and VectorStore cannot corrupt each other's
        # transaction state through a shared connection object.
        # For in-memory databases (db_path == "") we reuse the caller's
        # connection to preserve test-fixture behaviour.
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        else:
            self._conn = conn
        self._lock = threading.Lock()
        self._pending_embed: collections.deque = collections.deque()
        try:
            self._available = self._try_load_extension()
        except Exception as exc:  # pragma: no cover
            logger.warning("WARNING: sqlite-vec unavailable — vector search disabled")
            logger.debug("sqlite-vec load error: %s", exc)
            self._available = False
        if self._available:
            self._ensure_schema()
            if not self._available:
                logger.warning("WARNING: sqlite-vec unavailable — vector search disabled")
            else:
                logger.info("VectorStore: sqlite-vec available")
        else:
            logger.warning("WARNING: sqlite-vec unavailable — vector search disabled")

    def _try_load_extension(self) -> bool:
        """Try to load sqlite-vec extension."""
        try:
            import sqlite_vec  # type: ignore
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            return True
        except (ImportError, sqlite3.OperationalError, AttributeError):
            return False

    def _ensure_schema(self):
        """Create vector index table.

        Bug fixed (p14b-8): added ``project`` column to ``vec_memories`` so
        that project-scoped memories can be properly filtered during search.
        """
        try:
            self._conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS vec_memories (
                    memory_id   TEXT PRIMARY KEY,
                    agent_id    TEXT NOT NULL,
                    scope       TEXT NOT NULL DEFAULT 'private',
                    project     TEXT NOT NULL DEFAULT '',
                    content     TEXT NOT NULL,
                    created_at  REAL NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_index USING vec0(
                    memory_id TEXT,
                    embedding FLOAT[{self.VECTOR_DIM}]
                );
            """)
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error(f"VectorStore schema error: {e}")
            self._available = False

    async def embed(self, text: str) -> Optional[list[float]]:
        """
        Generate embedding for text.
        Uses Gemini API if available, otherwise returns None (fallback to FTS5).
        """
        try:
            import os
            api_key = os.environ.get("GOOGLE_API_KEY", "").split(",")[0].strip()
            if not api_key:
                return None

            import urllib.request
            import json as _json

            url = "https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent"
            payload = _json.dumps({
                "model": "models/text-embedding-004",
                "content": {"parts": [{"text": text[:2000]}]}
            }).encode()

            req = urllib.request.Request(
                f"{url}?key={api_key}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=5))
            data = _json.loads(response.read())
            return data.get("embedding", {}).get("values")
        except Exception as e:
            logger.debug(f"Embedding generation failed: {e}")
            return None

    async def retry_pending(self):
        """Retry embedding for all memory_ids queued in _pending_embed."""
        if not self._pending_embed:
            return
        retry_queue = list(self._pending_embed)
        self._pending_embed.clear()
        for item in retry_queue:
            memory_id = item["memory_id"]
            content = item["content"]
            agent_id = item["agent_id"]
            scope = item["scope"]
            project = item["project"]
            try:
                embedding = await self.embed(content)
                if embedding is None:
                    self._pending_embed.append(item)
                    continue
                with self._lock:
                    try:
                        self._conn.execute("BEGIN IMMEDIATE")
                        self._conn.execute(
                            "INSERT OR REPLACE INTO vec_memories "
                            "(memory_id, agent_id, scope, project, content, created_at) "
                            "VALUES (?,?,?,?,?,?)",
                            (memory_id, agent_id, scope, project, content, time.time())
                        )
                        self._conn.execute(
                            "INSERT OR REPLACE INTO vec_index (memory_id, embedding) VALUES (?,?)",
                            (memory_id, json.dumps(embedding))
                        )
                        self._conn.commit()
                    except sqlite3.Error as e:
                        self._conn.rollback()
                        logger.debug("VectorStore.retry_pending store error for %s: %s", memory_id, e)
                        self._pending_embed.append(item)
            except Exception as e:
                logger.debug("VectorStore.retry_pending embed error for %s: %s", memory_id, e)
                self._pending_embed.append(item)

    async def store(
        self,
        memory_id: str,
        content: str,
        agent_id: str,
        scope: str = "private",
        project: str = "",
    ):
        """Store content with its vector embedding.

        Bug fixed (p14b-8): added ``project`` parameter so project-scoped
        memories carry their project label into vec_memories and can be
        correctly filtered by VectorStore.search().
        """
        if not self._available:
            return
        # GAP-06: retry any previously failed embeds before attempting a new one
        await self.retry_pending()
        try:
            embedding = await self.embed(content)
        except Exception as e:
            logger.error(
                "ERROR: embedding failed for memory_id=%s, queued for retry", memory_id
            )
            logger.debug("embed exception: %s", e)
            self._pending_embed.append({
                "memory_id": memory_id,
                "content": content,
                "agent_id": agent_id,
                "scope": scope,
                "project": project,
            })
            return
        if embedding is None:
            logger.error(
                "ERROR: embedding failed for memory_id=%s, queued for retry", memory_id
            )
            self._pending_embed.append({
                "memory_id": memory_id,
                "content": content,
                "agent_id": agent_id,
                "scope": scope,
                "project": project,
            })
            return
        with self._lock:
            # MEM-04: wrap both INSERTs in one transaction so that vec_memories
            # and vec_index are never partially written.  A failure after the
            # first INSERT but before the second would leave an orphaned row in
            # vec_memories with no corresponding vector in vec_index, causing
            # silent search misses.
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT OR REPLACE INTO vec_memories "
                    "(memory_id, agent_id, scope, project, content, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (memory_id, agent_id, scope, project, content, time.time())
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO vec_index (memory_id, embedding) VALUES (?,?)",
                    (memory_id, json.dumps(embedding))
                )
                self._conn.commit()
            except sqlite3.Error as e:
                self._conn.rollback()
                logger.debug(f"VectorStore.store error: {e}")

    async def search(
        self,
        query: str,
        agent_id: str,
        k: int = 5,
        project: str = "",
    ) -> list[dict]:
        """Semantic similarity search. Returns empty list if not available.

        Bug fixed (p14b-8): project-scoped memories were never returned
        because the WHERE clause only checked ``scope = 'shared' OR
        agent_id = ?``.  Added project scope filtering to match
        SharedMemoryStore.search() semantics.

        Bug fixed (p14b-9): ``created_at`` was not included in the SELECT so
        recall() always stamped vector results with ``time.time()`` (now)
        instead of the actual creation time.
        """
        if not self._available:
            return []
        query_embedding = await self.embed(query)
        if query_embedding is None:
            return []
        with self._lock:
            try:
                rows = self._conn.execute(
                    """SELECT vm.memory_id, vm.content, vm.agent_id, vm.scope,
                              vi.distance, vm.created_at
                       FROM vec_index vi
                       JOIN vec_memories vm ON vi.memory_id = vm.memory_id
                       WHERE vi.embedding MATCH ?
                         AND (vm.scope = 'shared'
                              OR vm.agent_id = ?
                              OR (vm.scope = 'project' AND vm.project = ?))
                       ORDER BY vi.distance
                       LIMIT ?""",
                    (json.dumps(query_embedding), agent_id, project, k)
                ).fetchall()
                return [
                    {"memory_id": r[0], "content": r[1], "agent_id": r[2],
                     "scope": r[3], "distance": r[4], "created_at": r[5]}
                    for r in rows
                ]
            except sqlite3.Error as e:
                logger.debug(f"VectorStore.search error: {e}")
                return []

    def delete(self, memory_id: str) -> bool:
        """Remove a memory from the vector index."""
        with self._lock:
            try:
                self._conn.execute("DELETE FROM vec_memories WHERE memory_id = ?", (memory_id,))
                self._conn.execute("DELETE FROM vec_index WHERE memory_id = ?", (memory_id,))
                self._conn.commit()
                return True
            except Exception as exc:
                logger.error("VectorStore.delete failed for %s: %s", memory_id, exc)
                return False

    @property
    def available(self) -> bool:
        return self._available


class ColdMemoryStore:
    """
    Read/write interface for the cold memory layer (group_cold_memory table).

    Cold memory stores long-form archives such as dream-pass summaries.
    It is searched using FTS5 with time-decay scoring so that recent
    entries rank higher than equally-relevant older entries.

    The underlying write functions live in host/db.py and rely on that
    module's global DB connection + _db_lock.  This class wraps them so
    that MemoryBus can treat cold as a first-class source alongside
    shared and vector.
    """

    # Half-life for time-decay scoring: entries this many seconds old
    # receive a 0.5x relevance multiplier.  Default: 7 days.
    _DECAY_HALF_LIFE = 7 * 24 * 3600.0

    def search(self, query: str, jid: str, k: int = 5) -> list[dict]:
        """FTS5 search of cold memory with time-decay re-ranking.

        GAP-05: MemoryBus.recall() documented support for 'cold' in
        include_sources but silently skipped it.  This method provides
        the implementation so the cold layer is now a live recall source.

        Scoring formula:
            final_score = fts_score * decay_factor
        where
            decay_factor = 0.5 ** (age_seconds / DECAY_HALF_LIFE)

        This ensures a recent but moderately relevant entry can outscore
        an ancient highly-relevant one, matching the expected behaviour
        for a conversational assistant.
        """
        try:
            from .. import db as _db_module
            raw = _db_module.memory_fts_search(jid, query, limit=k * 2)
            # memory_fts_search returns both warm and cold; keep cold only.
            cold_rows = [r for r in raw if r.get("source") == "cold"]
        except Exception as exc:
            logger.warning("ColdMemoryStore.search error: %s", exc)
            return []

        now = time.time()
        results = []
        for r in cold_rows:
            age_seconds = max(0.0, now - r.get("created_at", now))
            decay = 0.5 ** (age_seconds / self._DECAY_HALF_LIFE)
            fts_score = r.get("fts_score", 0.0)
            combined = fts_score * decay
            results.append({
                "id": "cold:{}:{}".format(r.get("date", ""), r.get("created_at", 0)),
                "content": r.get("content", ""),
                "created_at": r.get("created_at", now),
                "fts_score": fts_score,
                "decay": decay,
                "combined_score": combined,
            })

        results.sort(key=lambda x: x["combined_score"], reverse=True)
        return results[:k]

    def write(self, jid: str, title: str, content: str, tags: str = "") -> int:
        """Write a cold memory entry.  Returns the new row id."""
        try:
            from .. import db as _db_module
            return _db_module.append_cold_memory(jid=jid, title=title, content=content, tags=tags)
        except Exception as exc:
            logger.error("ColdMemoryStore.write error: %s", exc)
            return -1


class MemoryBus:
    """
    Universal Memory Bus - unified interface for all memory operations.

    Combines Hot + Shared + Vector + Cold memory layers into a single
    coherent interface that all agents use.

    Example:
        bus = MemoryBus(db_conn, groups_dir=Path("groups"))

        # Remember something (private to this agent)
        await bus.remember("User likes Python", agent_id="bot1")

        # Remember shared knowledge (all agents can read)
        await bus.remember("API endpoint changed to v3", agent_id="bot1", scope="shared")

        # Recall relevant memories (searches all accessible layers)
        memories = await bus.recall("Python preferences", agent_id="bot1")
        for m in memories:
            print(f"[{m.source}] {m.content} (score={m.score:.2f})")
    """

    def __init__(self, conn: sqlite3.Connection, groups_dir: Path):
        self._conn = conn
        self._groups_dir = groups_dir
        self._hot_memory_locks: Dict[str, asyncio.Lock] = {}
        # MEM-01: derive the on-disk path from the caller's connection so that
        # each Store can open its own independent connection.  PRAGMA
        # database_list returns (seq, name, file); file is "" for :memory:.
        _db_row = conn.execute("PRAGMA database_list").fetchone()
        _db_path: str = _db_row[2] if _db_row else ""
        self.shared = SharedMemoryStore(conn, db_path=_db_path)
        self.vector = VectorStore(conn, db_path=_db_path)
        self.cold = ColdMemoryStore()
        self.palace = PalaceStore(self.shared._conn)
        logger.info(
            f"MemoryBus initialized | "
            f"shared=ok | "
            f"vector={'ok' if self.vector.available else 'unavailable (install sqlite-vec)'} | "
            f"cold=ok"
        )

    async def remember(
        self,
        content: str,
        agent_id: str,
        scope: MemoryScope = "private",
        project: str = "",
        importance: float = 0.5,
    ) -> str:
        """
        Store a memory in the appropriate layer(s).
        
        Args:
            content:    Text content to remember
            agent_id:   ID of the agent storing this memory
            scope:      "private" (agent only) | "shared" (all agents) | "project" (same project)
            project:    Project name for "project" scope
            importance: 0.0-1.0 importance weight
            
        Returns:
            memory_id: Unique ID for this memory
        """
        memory_id = self.shared.write(
            content=content,
            agent_id=agent_id,
            scope=scope,
            project=project,
            importance=importance,
        )
        # Also index in vector store for semantic search.
        # Bug fixed (p14b-8): pass project so vector store can filter by it.
        await self.vector.store(memory_id, content, agent_id, scope, project)
        return memory_id

    async def recall(
        self,
        query: str,
        agent_id: str,
        k: int = 5,
        project: str = "",
        include_sources: tuple = ("shared", "vector"),
    ) -> list[Memory]:
        """
        Recall relevant memories from all accessible layers.

        Search order:
        1. Vector (semantic) - most accurate for meaning
        2. Shared FTS5 - keyword matches in shared store
        3. Cold FTS5 + time-decay - long-term archived summaries (GAP-05)

        Pass include_sources=("shared", "vector", "cold") to enable cold recall.
        The default omits cold for backwards compatibility.

        Results are deduplicated and sorted by combined relevance score.

        Args:
            query:    Natural language query
            agent_id: Requesting agent's ID (also used as jid for cold search)
            k:        Maximum number of results
            project:  Project context for scoped memories

        Returns:
            List of Memory objects sorted by relevance (highest first)
        """
        results: list[Memory] = []
        seen_ids: set[str] = set()

        # 0. Hot memory from MEMORY.md
        if "hot" in include_sources:
            try:
                # Prevent path traversal
                safe_id = Path(agent_id).name  # strips any ../ components
                if not safe_id or safe_id != agent_id:
                    raise ValueError(f"Invalid agent_id: {agent_id!r}")
                hot_path = self._groups_dir / safe_id / "MEMORY.md"
                if hot_path.exists():
                    content = hot_path.read_text(encoding="utf-8", errors="ignore")
                    if content.strip():
                        results.append(Memory(
                            memory_id=f"hot:{agent_id}",
                            content=content[:2000],  # cap at 2000 chars
                            agent_id=agent_id,
                            scope="private",
                            source="hot",
                            score=0.9,  # high importance
                            created_at=time.time(),
                        ))
                        seen_ids.add(f"hot:{agent_id}")
            except Exception as exc:
                logger.debug("recall: failed to read hot memory for %s: %s", agent_id, exc)

        # 1. Vector semantic search
        if "vector" in include_sources and self.vector.available:
            vec_results = await self.vector.search(query, agent_id, k=k, project=project)
            for r in vec_results:
                if r["memory_id"] not in seen_ids:
                    seen_ids.add(r["memory_id"])
                    # Convert distance to score (lower distance = higher score).
                    # Bug fixed (p14b-7): previous code applied a 1.2× boost
                    # which pushed scores above the documented 0.0–1.0 range.
                    # We now clamp to [0.0, 1.0] after conversion.
                    score = min(1.0, max(0.0, 1.0 - r.get("distance", 1.0)))
                    results.append(Memory(
                        memory_id=r["memory_id"],
                        content=r["content"],
                        agent_id=r["agent_id"],
                        scope=r["scope"],
                        score=score,
                        # Bug fixed (p14b-9): use actual creation time from DB,
                        # not time.time() (which always returned "now").
                        created_at=r.get("created_at", time.time()),
                        source="vector",
                    ))

        # 2. Shared FTS5 search
        if "shared" in include_sources:
            shared_results = self.shared.search(query, agent_id, project=project, k=k)
            for r in shared_results:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    # Normalize FTS5 rank to 0-1 score.
                    # Bug fixed (p14b-10): BM25 rank values are unbounded
                    # (e.g. -0.5 for low relevance, -50 for high relevance).
                    # The old formula ``abs(rank) / 10.0`` produced values
                    # well above 1.0 for highly relevant results which were
                    # then hard-clamped by min(), destroying the ordering
                    # between moderately and highly relevant results.
                    # We now use a sigmoid-like mapping: score = 1 / (1 + e^rank)
                    # where rank is negative (BM25 convention), so higher
                    # absolute rank → score closer to 1.0.
                    import math
                    raw_rank = r.get("fts_rank", -1.0) or -1.0
                    # raw_rank is negative; more negative = more relevant.
                    # Map to (0, 1): score = 1 / (1 + exp(raw_rank / 5))
                    fts_score = 1.0 / (1.0 + math.exp(raw_rank / 5.0))
                    fts_score = min(1.0, max(0.0, fts_score))
                    results.append(Memory(
                        memory_id=r["id"],
                        content=r["content"],
                        agent_id=r["agent_id"],
                        scope=r["scope"],
                        score=fts_score,
                        created_at=r.get("created_at", time.time()),
                        source="shared",
                    ))

        # 3. Cold memory FTS5 + time-decay search (GAP-05)
        # agent_id doubles as the group jid for cold memory queries because
        # MemoryBus is always called with the group's folder name which
        # matches the jid used when writing to group_cold_memory.
        if "cold" in include_sources:
            import math
            cold_results = self.cold.search(query, jid=agent_id, k=k)
            for r in cold_results:
                cold_id = r["id"]
                if cold_id not in seen_ids:
                    seen_ids.add(cold_id)
                    # fts_score from memory_fts_search is abs(bm25), positive;
                    # apply sigmoid mapping for comparability with shared search,
                    # then multiply by the pre-computed time-decay factor.
                    raw_fts = r.get("fts_score", 0.0)
                    cold_score_base = 1.0 / (1.0 + math.exp(-raw_fts / 5.0))
                    decay = r.get("decay", 1.0)
                    cold_score = min(1.0, max(0.0, cold_score_base * decay))
                    results.append(Memory(
                        memory_id=cold_id,
                        content=r["content"],
                        agent_id=agent_id,
                        scope="private",
                        score=cold_score,
                        created_at=r.get("created_at", time.time()),
                        source="cold",
                    ))

        # Sort by score descending, return top k
        results.sort(key=lambda m: m.score, reverse=True)
        return results[:k]

    async def get_hot_memory(
        self,
        agent_id: str,
        token_budget: Optional[int] = None,
    ) -> str:
        """Return the hot MEMORY.md content for *agent_id*.

        Delegates to hot.get_hot_memory().  When *token_budget* is
        provided the content is filtered through MemoryStack.wake_up()
        so that only the highest-scoring sections within the budget are
        returned, cutting startup token cost 60-70% compared to injecting
        the full file unconditionally.

        Args:
            agent_id:     Agent / group identifier (mapped to a JID).
            token_budget: Optional token budget forwarded to
                          MemoryStack.wake_up().  Pass None to get
                          the full stored content (original behaviour).

        Returns:
            Filtered (or full) MEMORY.md text, or empty string if absent.
        """
        from .hot import get_hot_memory as _get_hot_memory

        return _get_hot_memory(agent_id, token_budget=token_budget)

    async def forget(self, memory_id: str, agent_id: str) -> bool:
        """Remove a memory (only owner can delete private memories)."""
        result = self.shared.delete(memory_id, agent_id)
        self.vector.delete(memory_id)
        return result

    async def patch_hot_memory(self, agent_id: str, patch: str, max_bytes: int = 8192) -> bool:
        """
        Append a patch to the agent's hot MEMORY.md file.
        Called by Agent Runtime via WebSocket when agent wants to update its memory.

        Args:
            agent_id:   Agent identifier (maps to group folder name)
            patch:      Text to append to MEMORY.md
            max_bytes:  Maximum file size (default 8KB)
        """
        # Prevent path traversal
        safe_id = Path(agent_id).name  # strips any ../ components
        if not safe_id or safe_id != agent_id:
            raise ValueError(f"Invalid agent_id: {agent_id!r}")
        # MEM-03: dict.setdefault() is atomic in CPython because the GIL
        # serialises bytecode execution.  Two coroutines racing here will
        # both create an asyncio.Lock(), but setdefault() guarantees that
        # only the first one is stored; subsequent calls return that same
        # Lock so all coroutines for this agent_id share one lock.
        # This is safe in CPython but would need an explicit asyncio.Lock
        # guard in a free-threaded or multi-interpreter scenario.
        lock = self._hot_memory_locks.setdefault(safe_id, asyncio.Lock())
        async with lock:
            memory_file = self._groups_dir / safe_id / "MEMORY.md"
            try:
                memory_file.parent.mkdir(parents=True, exist_ok=True)
                current = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
                updated = current + "\n" + patch
                # Truncate to max_bytes, being careful with UTF-8 boundaries.
                # BUG-MB-01 FIX: the naive encoded[:max_bytes].decode(errors="ignore")
                # can silently discard the last partial multi-byte character at the
                # cut boundary.  Use the same safe boundary-walking truncation that
                # hot.py employs so we never produce invalid UTF-8 or lose more
                # data than necessary.
                if len(updated.encode("utf-8")) > max_bytes:
                    from .hot import _safe_truncate_utf8
                    updated = _safe_truncate_utf8(updated, max_bytes)
                tmp_path = memory_file.with_suffix('.tmp')
                tmp_path.write_text(updated, encoding="utf-8")
                os.replace(tmp_path, memory_file)
                logger.debug(f"Hot memory patched for agent {agent_id}: +{len(patch)} chars")
                return True
            except OSError as e:
                logger.error(f"Hot memory patch failed for {agent_id}: {e}")
                return False

    def status(self) -> dict:
        """Return current status of all memory layers."""
        try:
            shared_count = self._conn.execute(
                "SELECT COUNT(*) FROM shared_memories"
            ).fetchone()[0]
        except sqlite3.Error:
            shared_count = -1

        return {
            "shared_memories": shared_count,
            "vector_available": self.vector.available,
            "groups_dir": str(self._groups_dir),
        }


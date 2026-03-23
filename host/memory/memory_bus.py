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

    def __init__(self, conn: sqlite3.Connection):
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
                           (id, agent_id, project, scope, content, importance, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (memory_id, agent_id, project, scope, content, importance, now, now),
                    )
                    self._conn.commit()
                except sqlite3.Error:
                    self._conn.rollback()
                    raise
            logger.debug(f"SharedMemory written: {memory_id} scope={scope}")
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

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()
        self._available = self._try_load_extension()
        if self._available:
            self._ensure_schema()
            logger.info("VectorStore: sqlite-vec available")
        else:
            logger.info("VectorStore: sqlite-vec not available, falling back to FTS5")

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
        embedding = await self.embed(content)
        if embedding is None:
            return
        with self._lock:
            try:
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
        self.shared = SharedMemoryStore(conn)
        self.vector = VectorStore(conn)
        logger.info(
            f"MemoryBus initialized | "
            f"shared=ok | "
            f"vector={'ok' if self.vector.available else 'unavailable (install sqlite-vec)'}"
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

        Note: "cold" (FTS5 full conversation history) is not yet integrated into
        MemoryBus recall.  Pass include_sources=("shared", "vector") explicitly or
        rely on the default; do NOT pass "cold" — it is silently ignored.

        Results are deduplicated and sorted by combined relevance score.

        Args:
            query:    Natural language query
            agent_id: Requesting agent's ID
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

        # Sort by score descending, return top k
        results.sort(key=lambda m: m.score, reverse=True)
        return results[:k]

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
        lock = self._hot_memory_locks.setdefault(safe_id, asyncio.Lock())
        async with lock:
            memory_file = self._groups_dir / safe_id / "MEMORY.md"
            try:
                memory_file.parent.mkdir(parents=True, exist_ok=True)
                current = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
                updated = current + "\n" + patch
                # Truncate to max_bytes, being careful with UTF-8 boundaries
                if len(updated.encode("utf-8")) > max_bytes:
                    encoded = updated.encode("utf-8")[:max_bytes]
                    updated = encoded.decode("utf-8", errors="ignore")
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


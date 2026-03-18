"""
Memory subsystem — Three-tier memory + Universal Memory Bus

Layers:
  hot.py        — Hot memory (MEMORY.md per group, 8KB limit)
  warm.py       — Warm memory (30-day daily logs)
  search.py     — Cold memory (FTS5 full-text search + time decay)
  compound.py   — Compound queries across hot+warm+cold layers
  memory_bus.py — Universal Memory Bus (Phase 1 NEW)
  summarizer.py — Memory auto-summarizer (Phase 2 NEW)

Phase 1 adds:
  - SharedMemoryStore: cross-agent readable/writable SQLite store
  - VectorStore: sqlite-vec semantic search (graceful fallback to FTS5)
  - MemoryBus: unified interface wrapping all layers
"""

# ── Original exports (backward compatible) ────────────────────────────────────
from .hot import get_hot_memory, update_hot_memory  # noqa: F401
from .warm import append_warm_log, run_micro_sync, prune_old_warm_logs  # noqa: F401
from .search import memory_search  # noqa: F401
from .compound import run_weekly_compound  # noqa: F401

# ── Phase 1 (UnifiedClaw): Universal Memory Bus ───────────────────────────────
try:
    from .memory_bus import MemoryBus, Memory, MemoryScope  # noqa: F401
except ImportError:
    pass  # memory_bus.py may not be present in older installs

# ── Phase 2 (UnifiedClaw): Memory Summarizer ─────────────────────────────────
try:
    from .summarizer import MemorySummarizer  # noqa: F401
except ImportError:
    pass  # summarizer.py may not be present in older installs

__all__ = [
    # ── Original ──────────────────────────────────────────────────────────────
    "get_hot_memory", "update_hot_memory",
    "append_warm_log", "run_micro_sync", "prune_old_warm_logs",
    "memory_search",
    "run_weekly_compound",
    # ── Phase 1 ───────────────────────────────────────────────────────────────
    "MemoryBus", "Memory", "MemoryScope",
    # ── Phase 2 ───────────────────────────────────────────────────────────────
    "MemorySummarizer",
]

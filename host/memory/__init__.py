"""
Memory subsystem - Three-tier + Universal Memory Bus

Layers:
  hot.py        - Hot memory (MEMORY.md per group, 8KB limit)
  warm.py       - Warm memory (30-day daily logs)
  search.py     - Cold memory (FTS5 full-text search + time decay)
  compound.py   - Compound queries across hot+warm+cold layers
  memory_bus.py - Universal Memory Bus (hot+shared+vector+cold unified)  [Phase 1 NEW]

Phase 1 adds:
  - SharedMemoryStore: cross-agent readable/writable SQLite store
  - VectorStore: sqlite-vec semantic search (graceful fallback to FTS5)
  - MemoryBus: unified interface wrapping all layers
"""
from .memory_bus import MemoryBus, Memory, MemoryScope  # noqa: F401

__all__ = ["MemoryBus", "Memory", "MemoryScope"]

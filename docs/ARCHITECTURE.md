# EvoClaw Architecture

## Vision: Toward UnifiedClaw

EvoClaw is evolving toward a unified multi-agent framework that combines:
- **Multi-channel breadth** (Telegram/WhatsApp/Discord/Teams/Matrix/Signal)
- **Self-evolution** (Genome-based adaptive behavior)
- **Enterprise tools** (LDAP/Jira/HPC/Workflow — from MinionDesk lineage)
- **Universal Memory Layer** (cross-agent knowledge sharing)

---

## Current Architecture (v1.x)

```
[Channels]
Telegram / Discord / Slack / Gmail / WhatsApp
     │
     ▼
[Python] host/ — Gateway + Orchestrator (single asyncio process)
  ├── channels/          Channel adapters
  ├── main.py            Message loop + IPC + scheduling
  ├── db.py              SQLite (13 tables, WAL mode, FTS5)
  ├── container_runner.py Docker lifecycle management
  ├── group_queue.py     Per-group queue + concurrency control
  ├── task_scheduler.py  cron/interval/once scheduler
  ├── ipc_watcher.py     File-based IPC watcher (1s polling)
  ├── evolution/         Evolution Engine
  │   ├── genome.py      Per-group behavior genome
  │   ├── fitness.py     Response quality scoring
  │   ├── adaptive.py    Load/time-aware adaptation
  │   └── immune.py      Prompt injection detection
  ├── memory/            Three-tier memory
  │   ├── hot.py         MEMORY.md (8KB per group)
  │   ├── warm.py        30-day daily logs
  │   ├── search.py      FTS5 full-text search
  │   └── compound.py    Cross-layer query
  ├── dev_engine.py      7-stage LLM dev pipeline
  ├── dashboard.py       Web dashboard (port 8765)
  └── webportal.py       Web chat portal (port 8766)
     │
     │ File-based IPC (JSON files, 1s polling)
     ▼
[Python] container/ — Agent Runtime (Docker, non-root UID 1000)
  ├── agent.py           Multi-provider LLM agent
  │   ├── Gemini 2.0 Flash (default, free tier)
  │   ├── Claude (Anthropic)
  │   └── OpenAI-compatible (NVIDIA NIM, Groq)
  ├── soul.md            Core ethical principles
  └── tools/             Agent capabilities
      ├── bash           Shell execution (300s timeout)
      ├── web_fetch      URL fetching (12KB limit)
      ├── file_read/write Filesystem operations
      └── github_cli     gh CLI integration
```

---

## Target Architecture (v2.x — UnifiedClaw)

```
[Channels]
Telegram / WhatsApp / Discord / Teams / Signal / iMessage / Matrix
     │
     ▼
[Python] Gateway + Orchestrator (single asyncio process)
  ├── channels/          Multi-channel adapters
  ├── memory/
  │   └── memory_bus.py  ← NEW: Universal Memory Bus
  │       ├── Hot         per-agent MEMORY.md
  │       ├── Shared      cross-agent knowledge (scope: private/shared/project)
  │       ├── Vector      sqlite-vec semantic search (NEW)
  │       └── Cold        FTS5 + time decay (existing)
  ├── identity/           ← NEW: Agent Identity Layer
  │   └── agent_id → profile, skills, history
  ├── evolution/          Evolution Engine (enhanced)
  │   └── cross_agent.py  ← NEW: cross-agent genome collaboration
  ├── ws_server.py        ← NEW: WebSocket API (port 8767)
  │   ├── /ws/agent       Agent Runtime connection
  │   ├── /ws/sdk         External SDK connection
  │   └── /ws/monitor     Monitoring tools
  ├── task_scheduler.py
  ├── group_queue.py
  ├── dashboard.py        (port 8765)
  └── webportal.py        (port 8766)
     │
     │ WebSocket (replaces file IPC) ← NEW
     ▼
[Python] Agent Runtime (Docker, non-root UID 1000)
  ├── agent.py           Multi-provider LLM
  │   ├── Claude / Gemini / OpenAI / Ollama / vLLM
  ├── tools/
  │   ├── base/          Existing tools (bash/web/file/github)
  │   └── enterprise/    ← NEW: MinionDesk tools
  │       ├── ldap.py    LDAP/AD queries
  │       ├── jira.py    Jira ticket operations
  │       ├── hpc.py     LSF/Slurm HPC job management
  │       └── workflow.py Approval workflow engine
  ├── soul.md
  └── fitness_reporter.py ← NEW: sends fitness back to Gateway
```

---

## Universal Memory Bus Design

```python
class MemoryBus:
    # Unified interface for all memory operations across agents.
    
    async def recall(
        self,
        query: str,
        agent_id: str,
        k: int = 5,
        scope: str = "all"  # "private" | "shared" | "project" | "all"
    ) -> list:
        # Simultaneously queries:
        # 1. Vector store (sqlite-vec semantic similarity)
        # 2. FTS5 full-text search
        # Merges and re-ranks results by relevance + recency.
        pass
    
    async def remember(
        self,
        content: str,
        agent_id: str,
        scope: str = "private",  # "private" | "shared" | "project"
        importance: float = 0.5   # 0.0 - 1.0
    ) -> str:  # Returns memory_id
        # Store memory with automatic embedding generation.
        pass
    
    async def forget(self, memory_id: str, agent_id: str): ...
    async def summarize(self, agent_id: str) -> str: ...
```

---

## Agent Identity System

```python
class AgentIdentity:
    agent_id: str       # Stable: hash(name + project + channel)
    name: str           # Human-readable name
    skills: list        # Accumulated skill tags
    profile: dict       # Free-form profile data
    history_summary: str  # Compressed conversation history
    genome_ref: str     # Link to evolution genome
    last_active: datetime
    created_at: datetime
```

Identity persists across container restarts via the `agent_identities` SQLite table.

---

## IPC Evolution: File Polling → WebSocket

### Current (v1.x)
```
Agent → writes JSON to /ipc/output/*.json
Host  → polls every 1 second, reads + processes
```
**Limitations**: 1s max latency, non-atomic writes, no bidirectional feedback

### Target (v2.x)
```
Agent ←──── task_payload ──────── Gateway
      ──── memory_patch ─────────→
      ──── fitness_update ───────→
      ──── evolution_hints ──────→ (bidirectional)
```
**Benefits**: <100ms latency, bidirectional, atomic, supports streaming

---

## Technology Choices

| Component | Current | Target | Reason |
|-----------|---------|--------|--------|
| Vector Search | None | sqlite-vec | Zero dependency, embedded |
| Embedding | None | Gemini text-embedding-004 | No local model needed |
| IPC | File polling | WebSocket | Bidirectional, low latency |
| Channel | 5 channels | 7+ channels | +Matrix, +Signal (Phase 3) |
| Enterprise Tools | None | MinionDesk port | LDAP/Jira/HPC/Workflow |

---

## Development Roadmap

### Phase 1 — Integration Foundation (Near-term)
- [ ] sqlite-vec integration into db.py
- [ ] MemoryBus abstract interface
- [ ] WebSocket IPC replacing file polling
- [ ] Agent fitness feedback to Gateway
- [ ] Basic Shared Memory table

### Phase 2 — Universal Memory Layer (Mid-term)
- [ ] Full Universal Memory Bus implementation
- [ ] Agent Identity Layer
- [ ] Cross-project knowledge sharing
- [ ] WebSocket SDK API
- [ ] Auto memory summarization

### Phase 3 — Enterprise Tools + RBAC (Mid-long term)
- [ ] MinionDesk enterprise tool suite port
- [ ] RBAC per agent role/permission
- [ ] Matrix channel support
- [ ] Multi-tenant support

### Phase 4 — Autonomous Evolution (Long-term)
- [ ] Cross-agent genome collaboration
- [ ] Agent self-discovers and composes tools
- [ ] Formal multi-agent swarm (prototype exists)
- [ ] Collective learning and knowledge distillation

---

## Key Design Principles

1. **Transparency**: Any developer should understand the codebase in half a day
2. **Security through isolation**: Agent code runs in Docker, host secrets never exposed
3. **Fork-friendly**: Customize by editing code directly, not config files
4. **Zero external dependencies**: SQLite for everything (no Redis, no Postgres, no Chroma)
5. **Graceful degradation**: System continues operating even if subsystems fail

---

*NanoClaw → EvoClaw → UnifiedClaw lineage*

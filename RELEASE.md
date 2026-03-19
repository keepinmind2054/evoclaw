# Release Notes

## EvoClaw v1.12.0 — UnifiedClaw Phase 1 Preview (Upcoming)

### Overview
This upcoming release begins the transition toward the **UnifiedClaw** unified framework, introducing the foundational components for cross-agent memory sharing and improved Agent↔Gateway communication.

### Planned Features

#### Universal Memory Bus (Phase 1)
- `sqlite-vec` integration for semantic/vector search
- `MemoryBus` unified interface (`recall()`, `remember()`, `forget()`)
- Basic `shared` memory scope (cross-agent readable/writable)

#### WebSocket IPC
- Replace 1-second file polling with WebSocket bidirectional communication
- Agent fitness feedback flows back to Gateway in real-time
- Memory patches sent directly from Agent Runtime to Gateway

#### Agent Identity (Foundation)
- `agent_identities` SQLite table
- Stable `agent_id` = hash(name + project + channel)
- Profile persistence across container restarts

### Architecture Evolution

```
v1.x (Current)                    v2.x (UnifiedClaw Target)
-----------------                 --------------------------
File IPC (1s polling)      ->     WebSocket (bidirectional)
Isolated group memory      ->     Universal Memory Bus
No vector search           ->     sqlite-vec semantic search
No agent identity          ->     Persistent Agent Identity
5 channels                 ->     7+ channels (+ Matrix/Signal)
Basic tools                ->     Enterprise tools (LDAP/HPC/Jira)
```

### Issues Addressed
See [GitHub Issues](https://github.com/KeithKeepGoing/evoclaw/issues) — 13 architecture roadmap issues created.

---

## EvoClaw v1.11.42 — 2026-03-17

### Summary
Stability release with security fixes and documentation improvements.

### Changes
- **Security**: Added SECURITY.md with vulnerability reporting policy
- **Security**: Fixed path traversal in `dev_engine._deploy_files()`
- **Fix**: Memory leak in long-running container sessions
- **Fix**: Evolution daemon timestamp handling
- **Docs**: Improved README with architecture diagram, badges, TOC
- **Docs**: Added ARCHITECTURE.md with UnifiedClaw roadmap
- **Maintenance**: Updated .gitignore to exclude Python cache files
- **Tracking**: 22 security/architecture issues created and tracked

### Security Notes
3 CRITICAL issues identified — see Issues #214, #215, #216 for remediation status.

---

## EvoClaw v1.11.34 — 2026-03-17

### Summary
Multiple stability improvements across message handling and evolution engine.

---

## EvoClaw v1.11.27 — 2026-03-16

### Summary
RELEASE.md coverage extended, documentation improvements.

---

## EvoClaw v1.10.8 — 2026-03-10

### Summary
Web portal authentication added, improved channel stability.

---

*EvoClaw → UnifiedClaw*

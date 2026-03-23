# EvoClaw

[![Version](https://img.shields.io/badge/version-v1.11.43-blue)](https://github.com/KeithKeepGoing/evoclaw/blob/main/docs/CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-required-blue)](https://www.docker.com/)
[![Security Issues](https://img.shields.io/badge/security%20issues-tracked-orange)](https://github.com/KeithKeepGoing/evoclaw/issues?q=label%3Asecurity)

A lightweight, Python-based multi-model AI agent framework designed for personal use. Built with transparency and security in mind — you can understand the entire codebase in half a day.

> **NanoClaw -> EvoClaw**: EvoClaw was developed from NanoClaw and powers the 小Evo AI assistant.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Features](#features)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Memory System](#memory-system)
- [Evolution Engine](#evolution-engine)
- [Channels](#channels)
- [Web Dashboard](#web-dashboard)
- [Security](#security)
- [Known Issues & Roadmap](#known-issues--roadmap)
- [Recent Changes](#recent-changes)
- [Contributing](#contributing)

---

## Overview

EvoClaw is a single-process AI agent framework (~42 Python files) that:

- Runs AI agent code in **isolated Docker containers** for security
- Supports **multiple LLM providers** (Gemini, Claude, OpenAI-compatible)
- Maintains **persistent memory** across conversations
- Adapts behavior through a **bio-inspired evolution engine**
- Integrates with **Telegram, Discord, Slack, Gmail, WhatsApp**

**Design Philosophy**: Customize by editing the code directly. Fork it and make it yours.

---

## Architecture

```
+-------------------------------------------------------------+
|                    HOST PROCESS (Python asyncio)            |
|                                                             |
|  +----------+  +----------+  +----------+  +-----------+   |
|  | Telegram |  | Discord  |  |  Slack   |  |  Gmail    |   |
|  | Channel  |  | Channel  |  | Channel  |  |  Channel  |   |
|  +----+-----+  +----+-----+  +----+-----+  +-----+-----+   |
|       +-------------------+-------------------+             |
|                           |                                 |
|                    +------v------+                          |
|                    |  main.py    |                          |
|                    | (msg loop)  |                          |
|                    +------+------+                          |
|          +----------------+-----------------+               |
|   +------v------+  +------v------+  +------v------+        |
|   |   db.py     |  |group_queue  |  |  evolution/ |        |
|   |  (SQLite)   |  |   .py       |  |  engine     |        |
|   +-------------+  +------+------+  +-------------+        |
|                            |                                |
|                    +-------v--------+                       |
|                    |container_runner|                       |
|                    |     .py        |                       |
|                    +-------+--------+                       |
+----------------------------+--------------------------------+
                             | Docker API (IPC via files)
+----------------------------v--------------------------------+
|              ISOLATED DOCKER CONTAINER                      |
|                                                             |
|   +-----------------------------------------------------+   |
|   |                  agent.py                           |   |
|   |   (Gemini / Claude / OpenAI-compatible)             |   |
|   |                                                     |   |
|   |   Tools: bash, web_fetch, file_read, file_write,   |   |
|   |          web_search, github_cli, ...                |   |
|   +-----------------------------------------------------+   |
|   Non-root user (UID 1000) | No host filesystem access      |
+-------------------------------------------------------------+
```

**Key Security Property**: Agent code runs in an isolated Linux container. Even if the agent is compromised via prompt injection, host secrets (Telegram token, GitHub token) remain protected.

---

## Features

### Three-Tier Memory System

| Layer | Storage | Capacity | Purpose |
|-------|---------|----------|---------|
| Hot | `MEMORY.md` per group | 8KB | Injected at container start |
| Warm | Daily log files | 30 days | Recent conversation history |
| Cold | SQLite FTS5 | Unlimited | Full-text search with time decay |

### Evolution Engine

Bio-inspired adaptive behavior system:
- **Genome**: Per-group behavior customization (style, formality, technical depth)
- **Fitness**: Tracks response quality metrics
- **Adaptive**: Adjusts to system load and time of day
- **Immune**: Detects 22+ injection attack patterns

### DevEngine

7-stage LLM-driven development pipeline:
1. **Analyze** -> 2. **Design** -> 3. **Implement** -> 4. **Test** -> 5. **Review** -> 6. **Document** -> 7. **Deploy**

Supports both `auto` and `interactive` modes.

### Web Dashboard (Port 8765)

10 monitoring pages including:
- Real-time container queue and agent status
- Live SSE log streaming
- DevEngine progress tracking
- Message history with group filtering
- Evolution statistics and genome tracking

### Multi-Channel Support

| Channel | Status |
|---------|--------|
| Telegram | Full support |
| Discord | Full support |
| Slack | Full support |
| Gmail | Full support |
| WhatsApp | Requires WHATSAPP_APP_SECRET |

### Agent Swarms

Coordinate specialized agent teams for complex tasks using the built-in swarm orchestration.

### Scheduled Tasks

Support for `cron`, `interval`, and one-time (`once`) execution via NanoClaw's scheduler integration.

---

## Quick Start

### Requirements

- Python 3.11+
- Docker
- At least one LLM provider API key

### Installation

```bash
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw
python setup/setup.py
```

The setup wizard will guide you through API keys, Docker configuration, and channel registration.

### Run

```bash
python run.py
```

- **Dashboard**: http://localhost:8765
- **Web Portal (Chat)**: http://localhost:8766

---

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Key settings:

| Variable | Description | Required |
|----------|-------------|----------|
| `GOOGLE_API_KEY` | Gemini API key (supports `key1,key2,key3` rotation) | Recommended |
| `ANTHROPIC_API_KEY` | Claude API key | Optional |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | For Telegram |
| `DISCORD_BOT_TOKEN` | Discord bot token | For Discord |
| `WHATSAPP_APP_SECRET` | Required for secure WhatsApp webhooks | For WhatsApp |
| `DASHBOARD_PASSWORD` | Web dashboard password | Recommended |

> **Security Note**: Always set `WHATSAPP_APP_SECRET` when using WhatsApp. Without it, the webhook endpoint accepts payloads from any caller.

---

## Memory System

Each group has its own memory stack:

```
groups/
+-- {group_name}/
    +-- MEMORY.md        <- Hot memory (8KB, injected at start)
    +-- logs/
    |   +-- 2026-03-17.md <- Warm memory (daily logs)
    +-- ...
```

To update a group's memory, simply edit its `MEMORY.md` file. The next container run will pick up the changes automatically.

---

## Evolution Engine

The evolution engine runs every 24 hours and adapts each group's behavior based on:

- Response time metrics
- User satisfaction signals
- System load patterns
- Time-of-day patterns

Genome parameters per group:
- `style`: response style (concise/detailed/technical)
- `formality`: tone level (casual/formal)
- `technical_depth`: explanation depth (beginner/expert)

---

## Channels

### Adding a New Group

Use the setup wizard or manually register via the web portal:

```bash
python setup/setup.py --add-group
```

### Allowlist Management

Groups are allowlisted in `host/allowlist.py`. Only registered groups receive responses.

---

## Web Dashboard

Access at http://localhost:8765

Available pages:
1. **Queue Monitor** - Real-time container status
2. **Log Stream** - Live SSE log viewer
3. **DevEngine** - Development pipeline progress
4. **Messages** - Full message history
5. **Evolution** - Genome and fitness statistics
6. **Health** - System resource monitoring
7. **Container Logs** - Per-container log inspection
8. **Groups** - Group management
9. **Tasks** - Scheduled task management
10. **Skills** - Installed skill packages

---

## Security

> See [SECURITY.md](SECURITY.md) for vulnerability reporting policy.

### Security Architecture

- **Container isolation**: Agent code runs as non-root in isolated Docker containers
- **Host/Container separation**: Host secrets never exposed to agent containers
- **Circuit breaker**: 3 consecutive Docker failures trigger automatic recovery
- **Immune system**: 22+ injection attack pattern detection
- **Message deduplication**: SHA-256 fingerprinting prevents replay attacks
- **Rate limiting**: Per-group message rate limits

### Known Security Considerations

- `self_update` IPC command enables agent-triggered `git pull` + restart (requires human confirmation in future)
- `tool_web_fetch()` currently has no private IP blocklist (SSRF risk being addressed)
- WhatsApp webhook verification requires manual `WHATSAPP_APP_SECRET` configuration

**22 security and architecture issues are actively tracked** in [GitHub Issues](https://github.com/KeithKeepGoing/evoclaw/issues?q=label%3Asecurity).

---

## Known Issues & Roadmap

All tracked issues: [GitHub Issues](https://github.com/KeithKeepGoing/evoclaw/issues)

### Critical (Fix Immediately)
- [ ] [#214] WhatsApp HMAC validation should be mandatory, not optional
- [ ] [#215] `self_update` command needs out-of-band human confirmation
- [ ] [#216] `_deploy_files()` path validation needs strict whitelist

### High Priority
- [ ] [#217] Immune system: change fail-open to fail-closed strategy
- [ ] [#218] Rate limit exceeded: notify user instead of silent drop
- [ ] [#219] Global `_db_lock` is a performance bottleneck
- [ ] [#220] `tool_web_fetch()` needs private IP blocklist (SSRF)

### See All Issues
-> [Full issue list on GitHub](https://github.com/KeithKeepGoing/evoclaw/issues)

---

## Recent Changes

See [CHANGELOG.md](docs/CHANGELOG.md) for full version history.

| Version | Date | Highlights |
|---------|------|-----------|
| v1.11.42 | 2026-03-17 | Path traversal fix, memory leak fix, evolution daemon timestamp fix |
| v1.11.34 | 2026-03-17 | Multiple stability improvements |
| v1.11.27 | 2026-03-16 | RELEASE.md coverage extended |
| v1.10.8 | 2026-03-10 | Web portal authentication added |

---

## Contributing

EvoClaw welcomes contributions! The codebase is intentionally small (~42 Python files) to keep it accessible.

### Setup for Development

```bash
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw
pip install -e ".[dev]"
python -m pytest tests/
```

### Key Contribution Areas

1. **Skills** (`skills_engine/`) - New capability packages
2. **Channels** (`host/channels/`) - New platform integrations
3. **Dynamic Tools** (`dynamic_tools/`) - Hot-swappable container tools
4. **Evolution** (`host/evolution/`) - Adaptive behavior improvements
5. **Security** - Fixing issues in the [security issue list](https://github.com/KeithKeepGoing/evoclaw/issues?q=label%3Asecurity)

### Design Philosophy

> *"The right way to customize EvoClaw is to fork it and edit the code directly."*

- Keep it under 50 Python files
- No microservices -- single process
- Transparency over magic: any developer should understand the codebase in half a day
- Security through OS-level isolation, not application-level controls

### Pull Request Guidelines

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/your-feature`)
3. Write tests for new functionality
4. Ensure `python -m pytest tests/` passes
5. Submit a PR with a clear description

See [CONTRIBUTING.md](CONTRIBUTING.md) for detailed guidelines.

---

## License

[MIT](LICENSE) - Keith / KeithKeepGoing

---

*Built with care by NanoClaw -> EvoClaw lineage*

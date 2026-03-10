# EvoClaw — AI Agent System

## Architecture Overview

EvoClaw is a Python-based AI assistant that routes messages through Google Gemini 2.0 Flash agents running in isolated Docker containers. All code is Python (asyncio).

## Core Components

### Host Process (`host/`)
Single Python asyncio process (`host/main.py`):
- **Message Loop** (every 2s): Polls SQLite for new messages, dispatches to GroupQueue
- **IPC Watcher**: Monitors `/data/ipc/{folder}/` for JSON commands from containers
- **Task Scheduler** (every 60s): Checks and runs scheduled tasks (cron/interval/once)
- **Evolution Loop** (every 24h): Adjusts group behavior genomes based on usage

### GroupQueue (`host/group_queue.py`)
Concurrency control:
- One container per group max (serialized per JID)
- Global limit: `MAX_CONCURRENT_CONTAINERS` (default 5)
- Tasks take priority over messages
- Exponential backoff on failure (5s→10s→20s→40s, max 5 retries)

### Container Runner (`host/container_runner.py`)
- Spawns `docker run evoclaw-agent:latest` per request
- Input: JSON via stdin (messages, secrets, genome hints, adaptive hints)
- Output: JSON between `---EVOCLAW_OUTPUT_START---` / `---EVOCLAW_OUTPUT_END---` markers
- Volume mounts:
  - Main group: full project :ro + own folder :rw
  - Regular groups: own folder :rw + global :ro only

### Container Agent (`container/agent-runner/agent.py`)
- **Model**: Google Gemini 2.0 Flash
- **Tools**: bash, read, write, edit, send_message, schedule_task
- **Working dir**: `/workspace/group`
- **IPC**: Writes JSON to `/workspace/ipc/messages/` and `/workspace/ipc/tasks/`
- Session memory persisted at `/data/sessions/{folder}/.claude`

### Database (`host/db.py`)
SQLite at `{STORE_DIR}/messages.db`:
- `messages` — chat history (chat_jid, sender, content, timestamp)
- `scheduled_tasks` — cron/interval/once tasks
- `registered_groups` — active groups (jid, folder, requires_trigger, is_main)
- `chats` — channel metadata
- `router_state` — k-v store (lastTimestamp cursor)

### Evolution Engine (`host/evolution/`)
- `immune.py` — Blocks prompt injection + spam before storing messages
- `fitness.py` — Tracks execution metrics (duration, success, retries) per group
- `adaptive.py` — Appends environment hints (system load, time of day) to prompts
- `genome.py` — Per-group style genes (formality, verbosity, technical depth)
- `daemon.py` — Every 24h, updates genomes based on fitness scores

### Channels (`host/channels/`)
Supported: Telegram, WhatsApp, Discord, Slack, Gmail
Each implements: `connect()`, `send_message(jid, text)`, `send_typing(jid)`
JID format: `tg:<id>`, `wa:<phone_id>:<chat_id>`, `dc:<channel_id>`, etc.

### Router (`host/router.py`)
- `format_messages()` — Converts DB messages to XML prompt for agent
- `route_outbound()` — Strips `<internal>` tags, sends via channel

## Message Flow

```
User → Channel → host._on_message()
  ├─ immune_check() — block injection/spam
  ├─ is_allowed() — allowlist check
  └─ db.store_message()
       ↓ (next 2s poll)
  GroupQueue.enqueue()
       ↓
  docker run evoclaw-agent (stdin: JSON, stdout: JSON)
       ↓
  route_outbound() → channel.send_message()
       ↓
  advance_cursor() — mark messages processed
```

## IPC Protocol (Container → Host)

JSON files written to `/workspace/ipc/{type}/`:
```json
{"type": "message", "chat_jid": "tg:123", "text": "Hello"}
{"type": "schedule_task", "prompt": "...", "schedule_type": "cron", "schedule_value": "0 9 * * *"}
{"type": "register_group", "jid": "tg:-100...", "name": "My Group", "folder": "telegram_my-group"}
```

## Configuration

**`.env` file:**
```
GOOGLE_API_KEY=...          # Required (Gemini)
TELEGRAM_BOT_TOKEN=...
WHATSAPP_TOKEN=...
ENABLED_CHANNELS=telegram,whatsapp
TIMEZONE=Asia/Taipei
MAX_CONCURRENT_CONTAINERS=5
```

**Register a group:**
```bash
python scripts/register_group.py --jid "tg:123456789" --name "Main" --folder "telegram_main" --main
```

## Key Design Decisions

1. **Secrets via stdin** (not env vars) — harder to leak via `/proc/environ`
2. **Cursor rollback safety** — `lastTimestamp` only advances after successful reply
3. **Immune system runs first** — prompt injection blocked before any DB write
4. **Tasks before messages** — scheduled tasks always take priority in queue
5. **SQLite only** — no external DB dependency; WAL mode for concurrency

## Development Commands

```bash
# Run host
python -m host.main

# Build agent container
docker build -t evoclaw-agent container/

# Register group
python scripts/register_group.py --jid "tg:YOUR_ID" --name "Me" --folder "telegram_me" --main

# Run setup wizard
python setup/setup.py
```

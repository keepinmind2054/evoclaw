<p align="center">
  <img src="assets/evoclaw-logo.svg" alt="EvoClaw" width="600">
</p>

<p align="center">
  A multi-LLM AI assistant that runs agents securely in their own containers.<br>
  Lightweight, 100% Python, built to be easily understood and completely customized.
</p>

<p align="center">
  <a href="README_zh.md">繁體中文</a>&nbsp; • &nbsp;
  <a href="https://github.com/KeithKeepGoing/evoclaw">GitHub</a>
</p>

An AI assistant framework built in Python. Supports Gemini, OpenAI-compatible APIs, and Claude.
Ships with a built-in **evolution engine** that makes the assistant adapt and improve over time.

---

## Philosophy

**Small enough to understand.** One process, ~42 Python files, no microservices. You can read the entire codebase in an afternoon. If you want to understand how something works, just read the source.

**Secure by isolation.** Agents run in Linux containers (Docker). They can only see what's explicitly mounted. Bash access is safe because commands run inside the container, not on your host machine. Security is at the OS level, not the application level.

**Built for the individual user.** EvoClaw isn't a monolithic framework — it's software that fits your exact needs. Make your own fork and modify it. The codebase is small enough that changes are safe and understandable.

**Customization = code changes.** No configuration sprawl. Want different behavior? Modify the code directly. There's no dashboard, no config wizard, no bloat.

**AI-native.**
- No installation wizard — `python setup/setup.py` handles everything
- No monitoring dashboard — ask the agent what's happening
- No debugging tools — describe the problem and the agent fixes it

**Skills over features.** Add capabilities via the `skills_engine/` system rather than hardcoding everything. Keep the base clean and composable.

**Evolves, not just runs.** The built-in evolution engine (`host/evolution/`) lets the assistant adapt like a living organism — automatically tuning response style per group, detecting threats, and sensing system load. No manual tuning required.

---

## What It Does

- Talk to your AI assistant from **Telegram, Discord, Slack, Gmail** or **WhatsApp** (optional skill)
- Every agent session runs in an **isolated Docker container** (secure by design)
- **Multi-LLM**: Gemini 2.0 Flash (default), any OpenAI-compatible API (NVIDIA NIM, Groq, etc.), or Claude
- **Scheduled tasks** — cron, interval, one-time
- **Native multi-turn conversation history** — agent remembers recent context across turns
- **Per-group memory** via `MEMORY.md` files in each group folder
- **Agent Swarms** — spin up teams of specialized agents that collaborate on complex tasks
- Available tools: Bash, Read, Write, Edit, send_message, schedule_task, list_tasks, cancel_task, and more
- **100% Python** — no Node.js, no TypeScript, no compilation step
- 🧬 **Evolution Engine** — AI behavior auto-optimizes with use (see below)

---

## Quick Start

```bash
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw
python setup/setup.py
```

The setup wizard handles everything: API keys, Docker, channel registration.

---

## Requirements

- Python 3.11+
- Docker
- An API key for your chosen LLM (auto-detected — set whichever key you have):
  - **Gemini** (default, free tier): [aistudio.google.com](https://aistudio.google.com) → `GOOGLE_API_KEY`
  - **NVIDIA NIM**: [build.nvidia.com](https://build.nvidia.com) → `NIM_API_KEY`
  - **OpenAI-compatible** (Groq, etc.): `OPENAI_API_KEY` + `OPENAI_BASE_URL`
  - **Claude**: [console.anthropic.com](https://console.anthropic.com) → `CLAUDE_API_KEY`

---

## Manual Setup

```bash
# 1. Clone
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw

# 2. Configure
cp .env.example .env
# Edit .env with your GOOGLE_API_KEY and channel tokens

# 3. Install Python dependencies
pip install -r host/requirements.txt

# 4. Build the Docker container
cd container && docker build -t evoclaw-agent . && cd ..

# 5. Start
python run.py
```

---

## Getting an API Key

**Gemini (default, free tier):**
1. Go to [aistudio.google.com](https://aistudio.google.com)
2. Sign in with Google → **Get API key** → **Create API key**
3. Add to `.env`: `GOOGLE_API_KEY=...`

> Free tier has generous limits. Separate from Gemini Advanced.

**NVIDIA NIM:**
1. Go to [build.nvidia.com](https://build.nvidia.com) and get an API key
2. Add to `.env`: `NIM_API_KEY=nvapi-...` (optionally set `NIM_MODEL`)

**Claude:**
1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an API key
3. Add to `.env`: `CLAUDE_API_KEY=...` (optionally set `CLAUDE_MODEL`)

---

## Usage

Talk to your assistant using the trigger word (default: `@Andy`):

```
@Andy summarize the sales pipeline every weekday morning at 9am
@Andy review the git history every Friday and update the README if there's drift
@Andy every Monday at 8am, compile AI news from Hacker News and message me a briefing
@Andy what files changed in the last 3 commits?
@Andy spin up a team of agents to research and write a market analysis report
```

### Main Channel

Your private self-chat is the **main channel** — your admin console. From here:

```
@Andy list all scheduled tasks across all groups
@Andy pause the Monday briefing task
@Andy register the "team-chat" group with jid dc:1234567890:9876543210
@Andy what's in the recent error logs?
```

Every other group is fully isolated from the main channel and from each other.

---

## Channels

Enable channels by setting `ENABLED_CHANNELS` in `.env`:

```bash
ENABLED_CHANNELS=telegram,discord,slack
```

| Channel | Required Env Vars | Notes |
|---------|------------------|-------|
| Telegram | `TELEGRAM_BOT_TOKEN` | Built-in |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | Built-in |
| Discord | `DISCORD_BOT_TOKEN` | Built-in |
| Gmail | `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE` | Built-in |
| WhatsApp | `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN` | Optional skill — run `/add-whatsapp` |

See `.env.example` for all available options.

---

## Customizing

EvoClaw has no configuration files. To change behavior, modify the code directly:

- "Change the trigger word to @Eve"
- "Make responses shorter and more direct"
- "Add a greeting when someone says good morning"
- "Store conversation summaries weekly to each group's memory file"

The codebase is only ~42 Python files — safe and easy to change.

---

## Project Structure

```
evoclaw/
├── run.py                        ← Start here: python run.py
├── host/                         ← Python host orchestrator
│   ├── main.py                   ← Entry point (message loop, IPC, scheduler)
│   ├── config.py                 ← Configuration from environment
│   ├── db.py                     ← SQLite database
│   ├── router.py                 ← Message routing
│   ├── group_queue.py            ← Per-group queue + concurrency control
│   ├── container_runner.py       ← Docker container management
│   ├── ipc_watcher.py            ← Agent↔Host IPC
│   ├── task_scheduler.py         ← Scheduled tasks
│   ├── allowlist.py              ← Sender/mount allowlists
│   ├── requirements.txt          ← Python dependencies
│   ├── evolution/                ← 🧬 Evolution Engine
│   │   ├── fitness.py            ←   Fitness tracking (natural selection)
│   │   ├── adaptive.py           ←   Epigenetic hints (environment sensing)
│   │   ├── genome.py             ←   Group genome (speciation)
│   │   ├── immune.py             ←   Immune system (threat detection)
│   │   └── daemon.py             ←   Evolution Daemon (24h cycle)
│   └── channels/
│       ├── telegram_channel.py   ← Telegram (long polling)
│       ├── whatsapp_channel.py   ← WhatsApp (Meta Cloud API + webhook)
│       ├── slack_channel.py      ← Slack (Socket Mode)
│       ├── discord_channel.py    ← Discord (discord.py)
│       └── gmail_channel.py      ← Gmail (OAuth2 polling)
├── container/
│   └── agent-runner/
│       ├── agent.py              ← Multi-LLM agent (Gemini / OpenAI-compatible / Claude)
│       └── requirements.txt      ← google-genai, openai, anthropic
├── skills_engine/                ← Plugin system (apply/uninstall skills)
├── scripts/                      ← CLI utility scripts
└── groups/
    └── {group-name}/
        └── MEMORY.md             ← Per-group memory file
```

---

## Evolution Engine

EvoClaw ships with a bio-inspired self-adaptation system. The assistant automatically improves over time without manual tuning.

### 🧬 Four Mechanisms

**① Fitness Tracking (Natural Selection)**
Every AI response records performance metrics (response time, success rate, retry count), computing a 0.0–1.0 fitness score as the basis for all evolutionary decisions.

**② Epigenetic Adaptation**
The environment shapes behavior without touching your `MEMORY.md`:
- High system load → AI automatically gives shorter answers
- Late night (0–6am) → switches to casual tone
- Weekend → more relaxed conversation style

**③ Group Genome (Speciation)**
Each group has its own behavioral genome (response style, formality, technical depth).
An Evolution Daemon runs every 24 hours, analyzing usage data and adjusting each group's genome independently — technical groups grow more technical, casual groups grow more relaxed.

**④ Immune System**
Automatically detects prompt injection attacks ("ignore previous instructions") and spam flooding. Builds persistent immune memory — accumulated threats trigger automatic sender blocking with no human intervention needed.

```
Message received
    ↓ Immune check (injection / spam detection)
Stored to DB
    ↓ Epigenetic hints computed from environment
Container starts (with evolution hints injected)
    ↓ AI responds
Fitness score recorded
    ↓ Every 24h
Evolution Daemon adjusts group genome
```

---

## Architecture

```
Telegram / WhatsApp / Discord / Slack / Gmail
                    ↓
           Host (Python, single process)
           ├── Message loop (polls SQLite)
           ├── Immune System (injection / spam blocking)
           ├── GroupQueue (one container per group, global concurrency limit)
           ├── IPC watcher (agent → host messages)
           ├── Task scheduler (cron / interval / once)
           └── Evolution Daemon (24h evolution cycle)
                    ↓ spawns (with evolution hints)
           Docker Container (isolated per group)
                    ↓ runs
           agent.py + Gemini / OpenAI-compatible / Claude
           + tools (Bash, Read, Write, Edit, send_message, schedule_task, list_tasks, cancel_task, ...)
                    ↓
           Fitness recorded → Response routed back to the right channel
```

- Each group has its own isolated container, workspace, and memory (`MEMORY.md`)
- GroupQueue ensures one container per group — messages queue up if the agent is busy
- Global concurrency limit (`MAX_CONCURRENT_CONTAINERS`) prevents resource exhaustion
- Cursor rollback: cursor only advances after successful agent output — no missed messages
- Evolution Engine: fitness tracking + epigenetic hints + group genome + immune system

For full architecture details see [docs/SPEC.md](docs/SPEC.md).

---

## Debugging

### Test the agent container directly

**Linux / macOS:**
```bash
echo '{"prompt":"hello"}' | docker run -i --rm evoclaw-agent
```

**Windows (PowerShell) — simple:**
```powershell
'{"prompt":"hello"}' | docker run -i --rm evoclaw-agent
```
**Windows (PowerShell) — full parameters:**
```powershell
$json = '{"prompt":"Say hello","secrets":{"GOOGLE_API_KEY":"your-api-key"},"groupFolder":"test","chatJid":"tg:123","isMain":false,"isScheduledTask":false,"assistantName":"Evo","evolutionHints":""}'
$json | docker run -i --rm evoclaw-agent
```

Expected output:
```
---EVOCLAW_OUTPUT_START---
{"status": "ok", "result": "Hello! ...", "error": null}
---EVOCLAW_OUTPUT_END---
```

| Error | Cause | Fix |
|-------|-------|-----|
| `Invalid JSON input` | stdin encoding issue | Rebuild image after `git pull` |
| `GOOGLE_API_KEY not set` | Missing API key | Add `GOOGLE_API_KEY` to `.env` |
| `No such image: evoclaw-agent` | Image not built | Run `docker build -t evoclaw-agent container/` |

---

## Security

- Agents run in Linux containers, not behind application-level permission checks
- Each container only sees explicitly mounted directories
- Bash access is safe because commands run inside the container, not on your host
- Sender allowlist: restrict which users can invoke the agent (`~/.config/evoclaw/sender-allowlist.json`)
- Mount allowlist: restrict which directories containers can access (`~/.config/evoclaw/mount-allowlist.json`)
- **Immune System**: automatically detects prompt injection attacks, builds persistent threat memory, auto-blocks malicious senders

See [docs/SECURITY.md](docs/SECURITY.md) for the full security model.

---

## FAQ

**Why Docker?**

Docker provides cross-platform support (macOS, Linux, Windows via WSL2) and a mature ecosystem.

**Can I run this on Linux?**

Yes. Docker works on both macOS and Linux.

**Can I use a different Gemini model?**

Yes. Set `GEMINI_MODEL` in your `.env`:
```bash
GEMINI_MODEL=gemini-2.0-flash-exp
```

**Can I use Claude or another LLM instead of Gemini?**

Yes. The agent auto-detects the backend from whichever key is set:
- **Gemini** (default) — set `GOOGLE_API_KEY`
- **NVIDIA NIM** — set `NIM_API_KEY` (optionally `NIM_MODEL`, `NIM_BASE_URL`)
- **OpenAI-compatible** (Groq, etc.) — set `OPENAI_API_KEY` + `OPENAI_BASE_URL`
- **Claude** — set `CLAUDE_API_KEY` (optionally `CLAUDE_MODEL`)

**How do I debug issues?**

Ask the agent directly in your main channel: "Why isn't the scheduler running?" "What's in the recent logs?" "Why did this message not get a response?"

---

## Credits

- [Google Gemini](https://ai.google.dev/) API
- [Anthropic Claude](https://www.anthropic.com/) API
- [OpenAI](https://openai.com/) compatible APIs (NVIDIA NIM, Groq, etc.)

## License

MIT

# EvoClaw

> This English README is a summary. For complete and current documentation, see [README.md](README.md) (Traditional Chinese).
> Current version: v1.26.0

[![Version](https://img.shields.io/badge/version-v1.26.0-blue)](https://github.com/KeithKeepGoing/evoclaw/blob/main/CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-required-blue)](https://www.docker.com/)

A lightweight, 100% Python multi-LLM AI agent framework. Agents run securely in isolated Docker containers. ~42 Python files — readable in an afternoon.

**Fork origin**: Built on [NanoClaw](https://github.com/qwibitai/nanoclaw-discord). EvoClaw extends the foundation with a Python AI agent layer, multi-layer memory, skill system, workflow engine, and enterprise connectors.

## Quick Start

```bash
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw
python setup/setup.py   # handles API keys, Docker, channel registration
python run.py
```

- Dashboard: http://localhost:8765
- Web chat: http://localhost:8766

## Key Features

- **Multi-channel**: Telegram, Discord, Slack, Gmail, WhatsApp (optional skill)
- **Isolated agents**: Each session runs in its own Docker container (non-root, no host filesystem access)
- **Multi-LLM**: Gemini (default, free tier), Claude, OpenAI-compatible APIs (NVIDIA NIM, Groq, etc.)
- **Evolution Engine**: Bio-inspired self-adaptation — auto-tunes response style per group, detects threats
- **Skills 2.0**: Hot-swap Python tools into containers without rebuilding the Docker image
- **DevEngine**: 7-stage LLM-driven dev pipeline (Analyze → Design → Implement → Test → Review → Document → Deploy)
- **3-layer memory**: Hot (`MEMORY.md`, 8KB), Warm (daily logs, 30 days), Cold (SQLite FTS5, unlimited)
- **Agent Swarms**: Coordinate teams of specialized agents for complex tasks
- **Scheduled tasks**: cron, interval, and one-time execution

## Requirements

- Python 3.11+
- Docker
- One LLM API key: `GOOGLE_API_KEY` (Gemini), `CLAUDE_API_KEY` (Claude, also accepted as `ANTHROPIC_API_KEY`), `NIM_API_KEY`, or `OPENAI_API_KEY` + `OPENAI_BASE_URL`

## Configuration

```bash
cp .env.example .env
# Set GOOGLE_API_KEY and your channel tokens
```

Key env vars: `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN`, `SLACK_BOT_TOKEN`, `DASHBOARD_PASSWORD`.

> See [README.md](README.md) for the full configuration reference and all documentation.

## License

[MIT](LICENSE) — Keith / KeithKeepGoing

---
*EvoClaw is built on the foundation of [NanoClaw](https://github.com/qwibitai/nanoclaw-discord).*

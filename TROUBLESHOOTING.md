# EvoClaw Troubleshooting Guide

## Diagnostic tools

```bash
# View live logs
python run.py 2>&1 | grep -E "ERROR|WARNING|CRITICAL"

# Check Docker status
docker ps | grep evoclaw

# Validate your environment (checks Python, Docker, API keys, image)
python scripts/validate_env.py

# Test Google Gemini API key
curl -H "x-goog-api-key: $GOOGLE_API_KEY" \
  "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"

# Test OpenAI / NIM API key
curl -H "Authorization: Bearer $OPENAI_API_KEY" \
  "${OPENAI_BASE_URL:-https://api.openai.com}/v1/models" | head -5
```

---

## Common problems

### Bot not responding

**Checklist:**
1. Confirm Docker is running: `docker ps`
2. Confirm bot token is correct: check with @BotFather on Telegram
3. View error logs: `python run.py 2>&1 | tail -50`
4. Run environment check: `python scripts/validate_env.py`

**Common causes:**
- `TELEGRAM_BOT_TOKEN` has wrong format (should be `123456:ABC-DEF...`)
- Docker image not built yet: run `make build`
- LLM API key invalid or expired: confirm the key and check your quota
- The group is not registered: send `/monitor` from the group or run `python scripts/register_group.py`

---

### `pip install` fails / ModuleNotFoundError on startup

The host dependencies live in `host/requirements.txt`, not a root-level file.

```bash
pip install -r host/requirements.txt
```

If you see `ModuleNotFoundError` for a specific package, install it directly:

```bash
pip install python-telegram-bot aiohttp websockets
```

---

### No LLM API key / wrong key name

EvoClaw supports these LLM providers and their environment variable names:

| Provider | Environment variable | Notes |
|----------|---------------------|-------|
| Google Gemini | `GOOGLE_API_KEY` | Free tier at aistudio.google.com |
| NVIDIA NIM | `NIM_API_KEY` | Supports Qwen, Llama, Mistral, and many others |
| OpenAI | `OPENAI_API_KEY` | GPT-4 series |
| Anthropic Claude | `CLAUDE_API_KEY` | Most reliable |

> **Important:** There is no `QWEN_API_KEY`. To use Qwen models, set `NIM_API_KEY`
> (NVIDIA NIM) or use `OPENAI_API_KEY` + `OPENAI_BASE_URL` pointing at a Qwen-compatible
> OpenAI-API endpoint.

Set at least one of these in `.env`:
```
GOOGLE_API_KEY=your-gemini-key
```

---

### OWNER_IDS not set — lost admin access

If no one was granted admin and you cannot reach the dashboard, set `OWNER_IDS` in `.env`:

```
# Telegram user IDs (comma-separated). Use /userinfobot to find yours.
OWNER_IDS=123456789,987654321
```

When `OWNER_IDS` is set, those users are automatically granted admin on every startup.

When `OWNER_IDS` is empty and no grants have been made, EvoClaw operates in **fail-open**
mode: all users can submit messages. This is intentional for fresh installs so you are
not locked out. Set `OWNER_IDS` to activate RBAC enforcement.

---

### Docker not installed or not running

```bash
# Check if Docker is available
docker info
```

If Docker is missing:
- **macOS / Windows:** Install Docker Desktop from https://www.docker.com/products/docker-desktop
- **Linux (Debian/Ubuntu):** `sudo apt-get install docker.io` then `sudo systemctl start docker`
- **Linux (RHEL/Fedora):** `sudo dnf install docker` then `sudo systemctl start docker`

If your user cannot run Docker without `sudo`:
```bash
sudo usermod -aG docker $USER
# Then log out and back in
```

---

### `evoclaw-agent` Docker image not found

The image must be built before first run. Run:

```bash
make build
# or directly:
docker build -t evoclaw-agent:latest container/
```

This takes 5-10 minutes on first build (downloads ~1 GB of layers). Subsequent builds
are cached and take ~30 seconds.

---

### Docker build failed

```bash
# Clear stale build cache and retry
docker builder prune -f
make build
```

If apt package downloads fail (network/proxy issue):

```bash
export DOCKER_BUILDKIT=1
export HTTP_PROXY=http://your-proxy:port
make build
```

---

### Slow responses (over 30 seconds)

**Normal:** First Docker container cold-start takes 8-15 seconds.

**Abnormal (>30 s):**
- Check machine resources: need RAM > 4 GB, Docker Desktop allocated > 2 GB
- Check if the LLM API is slow: try switching to Gemini (`GOOGLE_API_KEY`) which is faster
- Check Docker image size: `docker images | grep evoclaw`

---

### Circuit breaker open

Docker failed 3 times in a row; the system pauses to protect stability.

**Fix:**
1. Wait 60 seconds for automatic recovery
2. Or restart: `Ctrl+C` then `python run.py`
3. View the failure reason: `docker logs evoclaw-agent` (last few lines)
4. Or send SIGUSR1 to reset without restart: `kill -USR1 $(pgrep -f "python.*run.py")`

---

### Out-of-memory (OOM)

Each Docker container uses ~512 MB RAM. 5 concurrent containers = ~2.5 GB needed.

**Fix:**

```bash
# In .env — reduce concurrency or per-container memory:
MAX_CONCURRENT_CONTAINERS=2
CONTAINER_MEMORY=256m
```

---

### Wrong Telegram bot token — bot loops or crashes

A wrong token causes Telegram to return `401 Unauthorized`. EvoClaw retries
5 times with exponential back-off (2, 4, 8, 16, 30 s), then logs an error and
stops the Telegram channel. The process continues running but Telegram is inactive.

Check the logs for: `Telegram connect failed after 5 attempts`

Fix: correct `TELEGRAM_BOT_TOKEN` in `.env` and restart.

---

### Bot conflicts (`Conflict: terminated by other getUpdates request`)

Two instances of EvoClaw are running with the same bot token.
Stop all running instances and start only one.

---

### Logs filling up disk

EvoClaw prunes its internal SQLite log tables at startup (keeps last 30 days by default).

For process stdout/stderr logs, use log rotation:

**With systemd (recommended):**
journald rotates logs automatically. No action needed.

**Without systemd — set up logrotate:**

```bash
# /etc/logrotate.d/evoclaw
/path/to/evoclaw/logs/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

---

### Running EvoClaw continuously (auto-restart on crash)

`python run.py` does not auto-restart on crash. Use a process supervisor:

**systemd (Linux):**

```bash
sudo cp scripts/evoclaw.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now evoclaw
sudo journalctl -u evoclaw -f
```

**launchd (macOS):**

```bash
bash scripts/install_launchd.sh
launchctl list | grep evoclaw
```

**Manual restart loop (quick test only — not recommended for production):**

```bash
while true; do python run.py; echo "Crashed, restarting in 5s..."; sleep 5; done
```

---

### Updating EvoClaw

```bash
git pull
pip install -r host/requirements.txt   # pick up new deps
make build                              # only if container/Dockerfile changed
# Then restart EvoClaw (systemctl restart evoclaw, or Ctrl+C + python run.py)
```

---

## Log symbol reference

| Symbol | Meaning |
|--------|---------|
| CRITICAL | Fatal startup error (Docker unreachable, no API keys, etc.) |
| ERROR | Container failure, channel error |
| WARNING | Non-fatal issue (rate limit, duplicate message, missing optional config) |
| INFO | Normal operation |
| DEBUG | Verbose tracing (set `LOG_LEVEL=DEBUG` in `.env`) |

---

## Getting help

- GitHub Issues: https://github.com/KeithKeepGoing/evoclaw/issues
- When filing an issue, include: error message, `.env` content (mask your keys), `docker version` output, `python --version`

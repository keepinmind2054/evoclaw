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
5. Confirm `ENABLED_CHANNELS` is set in `.env` and matches your channel (e.g. `telegram`)

**Common causes:**
- `TELEGRAM_BOT_TOKEN` has wrong format (should be `123456:ABC-DEF...`)
- `ENABLED_CHANNELS` is missing or set to wrong value — bot starts with no active channels
- Docker image not built yet: run `make build`
- LLM API key invalid or expired: confirm the key and check your quota
- The group is not registered: run `python scripts/register_group.py`

**What the user sees while processing:**
EvoClaw sends a typing indicator ("...") while the container runs (15-60 seconds). The
indicator is renewed every 4 seconds so it does not disappear mid-processing. If you
see no typing indicator, the message may have been filtered before reaching the container.

---

### RBAC blocking messages ("您目前沒有使用此機器人的權限")

The user received: `⚠️ 您目前沒有使用此機器人的權限，請聯繫管理員申請開通。`

**Cause:** RBAC is active and the sender has not been granted `task:submit` permission.

**Fix:**
- Set `OWNER_IDS` in `.env` with your own user ID — owners are granted admin automatically
- Or grant permission via the dashboard: Settings → RBAC → grant the user `task:submit`
- If you want all users allowed, leave `OWNER_IDS` empty (fail-open mode)

```
OWNER_IDS=123456789,987654321
```

---

### Rate limiting ("速度太快" or "訊息量已達上限")

**Per-sender rate limit:** `⚠️ 您傳送訊息的速度太快，請稍等片刻再試。`

A single user may send at most 5 messages per 60 seconds. This prevents one person from
flooding the queue.

**Per-group rate limit:** `⚠️ 此群組的訊息量已達上限，請稍等片刻後再試。`

The entire group may send at most 20 messages per 60 seconds.

**Tuning (in `.env`):**
```
SENDER_RATE_LIMIT_MAX=5          # messages per sender per window
SENDER_RATE_LIMIT_WINDOW_SECS=60
RATE_LIMIT_MAX_MSGS=20           # messages per group per window
RATE_LIMIT_WINDOW_SECS=60
```

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

**Normal:** First Docker container cold-start takes 8-15 seconds. EvoClaw shows a
typing indicator while processing so you know the request was received.

**Abnormal (>30 s):**
- Check machine resources: need RAM > 4 GB, Docker Desktop allocated > 2 GB
- Check if the LLM API is slow: try switching to Gemini (`GOOGLE_API_KEY`) which is faster
- Check Docker image size: `docker images | grep evoclaw`

---

### Circuit breaker open ("Docker 暫時受阻")

The user received: `⚠️ 此群組 Docker 暫時受阻（連續失敗 3 次）…`

Docker failed 3 times in a row; the system pauses to protect stability.

**Fix:**
1. Wait 60 seconds for automatic recovery (the message tells you the exact wait time)
2. Or restart: `Ctrl+C` then `python run.py`
3. View the failure reason: `docker logs evoclaw-agent` (last few lines)
4. Or send SIGUSR1 to reset without restart: `kill -USR1 $(pgrep -f "python.*run.py")`

---

### Out-of-memory (OOM) — "記憶體不足"

The user received: `⚠️ AI 執行時記憶體不足（已被系統終止）…`

This means the Docker container was killed with exit code 137 (OOM kill). Each container
is limited by `CONTAINER_MEMORY` (default 512m).

**Fix:**

```bash
# In .env — reduce concurrency or raise per-container memory:
MAX_CONCURRENT_CONTAINERS=2
CONTAINER_MEMORY=1g
```

Or simplify the user's request (shorter history, smaller attachments).

---

### Container timeout ("請求逾時")

The user received: `⏱️ 這個請求超過 X 分鐘仍未完成（逾時）…`

The container ran longer than `CONTAINER_TIMEOUT` (default 30 minutes). The system
will retry automatically.

**If timeouts are frequent:**
- Check LLM API response times
- Reduce `CONTAINER_TIMEOUT` in `.env` to fail faster: `CONTAINER_TIMEOUT=120000` (2 minutes)
- Simplify requests

---

### Queue depth — "正在處理上一則訊息"

The user received: `⏳ 正在處理上一則訊息，您的請求已加入佇列，請稍候…`

This is normal. EvoClaw processes one message per group at a time. The queued message
will be processed as soon as the current container finishes.

If the queue is consistently full:
- Raise `MAX_CONCURRENT_CONTAINERS` (default 5) — needs more RAM
- Or use multiple registered groups to parallelize work

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

## User-facing error message reference

| Message | Meaning | Action |
|---------|---------|--------|
| `⚠️ 您目前沒有使用此機器人的權限` | RBAC blocked — sender lacks permission | Admin grants permission via dashboard or OWNER_IDS |
| `⚠️ 您傳送訊息的速度太快` | Per-sender rate limit hit | Wait 60 seconds |
| `⚠️ 此群組的訊息量已達上限` | Per-group rate limit hit | Wait 60 seconds |
| `⚠️ 此群組 Docker 暫時受阻` | Circuit breaker open after 3 failures | Wait stated seconds; check Docker |
| `⚠️ AI 執行時記憶體不足` | Container OOM-killed (exit 137) | Raise CONTAINER_MEMORY or simplify request |
| `⏱️ 這個請求超過 X 仍未完成` | Container timeout | System retries; check LLM latency |
| `⚠️ 系統暫時發生問題，請稍後再傳訊息` | Generic container error | System retries; check logs |
| `⚠️ AI 回應格式異常` | JSON parse error in container output | System retries automatically |
| `⏳ 正在處理上一則訊息` | Queue depth — your message is waiting | Normal; will process soon |

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

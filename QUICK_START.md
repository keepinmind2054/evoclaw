# EvoClaw Quick Start

Get EvoClaw running in under 10 minutes.

## Requirements

- Docker (installed and running)
- Python 3.11+
- At least one LLM API key (Gemini / NIM / OpenAI / Claude — pick one)
- At least one chat channel token (Telegram is easiest)

## Steps

### 1. Install

```bash
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw
pip install -r host/requirements.txt
```

> **Note (Ubuntu 23.04+ / Debian 12+):** If you see `error: externally-managed-environment`, use a
> virtual environment instead:
> ```bash
> python3 -m venv .venv
> source .venv/bin/activate
> pip install -r host/requirements.txt
> ```

### 2. Configure

Copy the minimal config template:

```bash
cp .env.minimal .env
```

Edit `.env` and fill in your keys (only the ones you have):

```bash
# LLM — pick ONE of the following:
GOOGLE_API_KEY=your-gemini-key        # Recommended: free tier at aistudio.google.com
# NIM_API_KEY=your-nim-key            # NVIDIA NIM (supports Qwen, Llama, Mistral, etc.)
# OPENAI_API_KEY=your-openai-key      # GPT-4 series
# CLAUDE_API_KEY=your-claude-key      # Most reliable, but costs more

# Channel — pick ONE and set ENABLED_CHANNELS to match:
ENABLED_CHANNELS=telegram             # Must match the channel you configure below
TELEGRAM_BOT_TOKEN=your-telegram-token  # Easiest to obtain via @BotFather
# DISCORD_BOT_TOKEN=your-discord-token  # Also set ENABLED_CHANNELS=discord

# Owner — strongly recommended:
# Your Telegram user ID. Use /userinfobot to find it.
# These users get automatic admin rights.
OWNER_IDS=123456789
```

> **Note on Qwen:** Qwen models are accessed via `NIM_API_KEY` (NVIDIA NIM) or `OPENAI_API_KEY`
> (with `OPENAI_BASE_URL` pointed at the Qwen endpoint). There is no separate `QWEN_API_KEY`.

> **Note on ENABLED_CHANNELS:** This field is required. Without it the bot starts with no active
> channels. Set it to `telegram`, `discord`, or a comma-separated list if you use both.

### 3. Build the Docker image and start

```bash
# Build the agent container (first time takes 5-10 minutes)
make build

# Verify your environment
python scripts/validate_env.py

# Start EvoClaw
python run.py
```

### 4. Register a group

EvoClaw must know which group to respond in. There are two ways:

**Option A — Interactive script (recommended for first setup):**

```bash
python scripts/register_group.py
```

**Option B — Send a message to your bot on Telegram:**
Start a conversation with your bot. On first contact the bot sends a welcome message
explaining how to trigger it (use `@BotName your question`).

**Optional — Set up a monitor group (for error alerts):**
Send `/monitor` from a dedicated Telegram group to register it as the error-alert
destination. This is separate from your main working group and is optional.

### 5. Test

Find your bot on Telegram and send:

```
@YourBotName hello
```

If the bot replies (may take 15–30 seconds on cold start), setup is complete.

---

## Common problems

| Problem | Fix |
|---------|-----|
| `pip install` fails | Make sure you are running `pip install -r host/requirements.txt` (not a root-level file) |
| Docker not running | Start Docker Desktop or run `sudo systemctl start docker` |
| No reply from bot | Check `TROUBLESHOOTING.md` |
| Wrong API key format | Check `.env` — no spaces around the `=` sign, no quotes |
| Slow response (15-30 s) | Normal — first Docker container cold-start takes time; typing indicator shows while processing |
| `evoclaw-agent image not found` | Run `make build` first |
| No admin access | Set `OWNER_IDS` in `.env` with your Telegram user ID |
| Bot doesn't respond in group | Set `ENABLED_CHANNELS=telegram` in `.env`; make sure the group is registered |
| Rate limited ("速度太快") | You're sending messages too fast — wait 60 seconds |

---

## Running continuously (production)

### Linux (systemd)

```bash
# Install the systemd service
sudo cp scripts/evoclaw.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable evoclaw
sudo systemctl start evoclaw

# View logs
sudo journalctl -u evoclaw -f
```

### macOS (launchd)

```bash
# Install the launchd plist (fills in PROJECT_ROOT and HOME automatically)
bash scripts/install_launchd.sh
```

### Updating EvoClaw

```bash
git pull
pip install -r host/requirements.txt
make build        # only needed if container changed
sudo systemctl restart evoclaw   # or restart however you run it
```

---

For detailed configuration see [README.md](README.md).
For troubleshooting see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

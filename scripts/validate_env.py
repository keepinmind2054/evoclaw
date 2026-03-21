#!/usr/bin/env python3
"""Validate EvoClaw environment and configuration"""
import sys, subprocess
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from host.env import read_env_file

def check(label, ok, detail=""):
    icon = "✓" if ok else "✗"
    print(f"  {icon} {label}" + (f": {detail}" if detail else ""))
    return ok

def main():
    print("EvoClaw Environment Check\n")
    all_ok = True

    # Python version
    v = sys.version_info
    ok = v >= (3, 11)
    all_ok &= check("Python 3.11+", ok, f"found {v.major}.{v.minor}")

    # Docker installed and running
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        all_ok &= check("Docker running", r.returncode == 0,
                        "install Docker or start Docker Desktop" if r.returncode != 0 else "")
    except FileNotFoundError:
        all_ok &= check("Docker running", False,
                        "Docker not found — install from https://www.docker.com/products/docker-desktop")
    except Exception as e:
        all_ok &= check("Docker running", False, str(e))

    # LLM API keys — at least one must be set
    LLM_KEYS = {
        "GOOGLE_API_KEY": "Google Gemini",
        "NIM_API_KEY": "NVIDIA NIM (Qwen, Llama, Mistral, etc.)",
        "OPENAI_API_KEY": "OpenAI",
        "CLAUDE_API_KEY": "Anthropic Claude",
    }
    env = read_env_file(list(LLM_KEYS.keys()) + [
        "TELEGRAM_BOT_TOKEN",
        "DISCORD_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
        "OWNER_IDS",
    ])

    has_llm = any(env.get(k, "").strip() for k in LLM_KEYS)
    all_ok &= check(
        "At least one LLM API key set",
        has_llm,
        "set one of: " + ", ".join(LLM_KEYS.keys()) + " in .env" if not has_llm else "",
    )
    for k, label in LLM_KEYS.items():
        if env.get(k, "").strip():
            check(f"  {k} ({label})", True)

    # Channel token — at least one (required: without a token the bot cannot receive messages)
    # p12b fix: promote from non-fatal to fatal (all_ok &=) because a bot with no channel
    # token starts "successfully" but is completely deaf — worse than failing at startup.
    has_channel = any(env.get(k, "").strip() for k in ["TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN"])
    all_ok &= check(
        "At least one channel token set",
        has_channel,
        "set TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN, or SLACK_BOT_TOKEN in .env" if not has_channel else "",
    )

    # OWNER_IDS — warn if missing (fail-open without it)
    owner_ids = env.get("OWNER_IDS", "").strip()
    if owner_ids:
        check("OWNER_IDS set", True, owner_ids)
    else:
        # Not a fatal error — just a warning
        print("  ! OWNER_IDS not set — RBAC is in fail-open mode (all users allowed).")
        print("    Set OWNER_IDS=<your Telegram user ID> in .env to enable access control.")

    # Container image
    try:
        r = subprocess.run(["docker", "image", "inspect", "evoclaw-agent"],
                          capture_output=True, timeout=10)
        all_ok &= check("evoclaw-agent image built", r.returncode == 0,
                        "run: make build" if r.returncode != 0 else "")
    except Exception:
        all_ok &= check("evoclaw-agent image built", False, "run: make build")

    # host/requirements.txt installed
    try:
        import telegram  # noqa: F401
        check("python-telegram-bot installed", True)
    except ImportError:
        all_ok &= check("python-telegram-bot installed", False,
                        "run: pip install -r host/requirements.txt")

    print()
    if all_ok:
        print("All checks passed! Run: python run.py")
    else:
        print("Some checks failed. Fix the issues above, then run: python run.py")
        sys.exit(1)

if __name__ == "__main__":
    main()

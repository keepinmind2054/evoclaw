#!/usr/bin/env python3
"""EvoClaw entry point — run with: python run.py"""
import asyncio
import sys


def _preflight_check() -> None:
    """p12b: Lightweight pre-flight check that runs before the asyncio event loop.

    Validates the most common configuration mistakes (missing LLM key, missing
    channel token) and prints a clear, actionable error message before any
    complex initialisation happens.  This catches the worst failure modes early
    so operators see "TELEGRAM_BOT_TOKEN is not set" instead of a cryptic
    asyncio traceback buried in startup logs.

    Intentionally avoids importing host.main (which triggers heavy imports) so
    errors here are always readable even when optional dependencies are missing.
    """
    from pathlib import Path

    # Only attempt preflight when a .env file exists — if there is no .env at
    # all, the startup sequence itself will catch the problem with better context.
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        print(
            "WARNING: .env file not found. "
            "Copy .env.minimal to .env and fill in your keys before starting.",
            file=sys.stderr,
        )
        return

    # Use the same reader as the rest of the codebase.
    try:
        from host.env import read_env_file
    except ImportError:
        return  # host package not importable yet — skip preflight

    LLM_KEYS = ["GOOGLE_API_KEY", "NIM_API_KEY", "OPENAI_API_KEY", "CLAUDE_API_KEY"]
    CHANNEL_KEYS = ["TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN"]
    secrets = read_env_file(LLM_KEYS + CHANNEL_KEYS)

    missing = []
    if not any(secrets.get(k, "").strip() for k in LLM_KEYS):
        missing.append(
            f"  No LLM API key set. Add one of: {', '.join(LLM_KEYS)}"
        )
    if not any(secrets.get(k, "").strip() for k in CHANNEL_KEYS):
        missing.append(
            f"  No channel token set. Add one of: {', '.join(CHANNEL_KEYS)}"
        )

    if missing:
        print("EvoClaw pre-flight check FAILED:", file=sys.stderr)
        for m in missing:
            print(m, file=sys.stderr)
        print(
            "\nEdit .env (or copy .env.minimal to .env) and add the missing values,",
            file=sys.stderr,
        )
        print("then re-run: python run.py", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    # p15d BUG-FIX (LOW): set WindowsProactorEventLoopPolicy before asyncio.run()
    # so asyncio subprocesses work correctly on Windows.  Without this, Docker
    # subprocess spawning raises NotImplementedError on Python 3.8+ Windows because
    # the default SelectorEventLoop does not support subprocess pipes.
    # host/main.py's __main__ block already did this but run.py (the actual entry
    # point) did not, so the policy was never applied when using `python run.py`.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    _preflight_check()
    from host.main import main
    asyncio.run(main())

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

    import logging as _log_run
    LLM_KEYS = ["GOOGLE_API_KEY", "NIM_API_KEY", "OPENAI_API_KEY", "CLAUDE_API_KEY", "ANTHROPIC_API_KEY"]
    CHANNEL_KEYS = ["TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN"]
    secrets = read_env_file(LLM_KEYS + CHANNEL_KEYS)

    # p21c: Warn if the user set ANTHROPIC_API_KEY but not CLAUDE_API_KEY.
    # EvoClaw reads CLAUDE_API_KEY; without this alias awareness the user would
    # silently fall back to Gemini even with a valid Anthropic key in .env.
    import os as _os_run
    if (_os_run.environ.get("ANTHROPIC_API_KEY") or secrets.get("ANTHROPIC_API_KEY")) and \
            not (_os_run.environ.get("CLAUDE_API_KEY") or secrets.get("CLAUDE_API_KEY")):
        _log_run.getLogger(__name__).warning(
            "ANTHROPIC_API_KEY detected but EvoClaw uses CLAUDE_API_KEY. "
            "Using ANTHROPIC_API_KEY as fallback. Consider renaming to CLAUDE_API_KEY."
        )

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


def _parse_args() -> None:
    """Parse command-line arguments before anything else is imported.

    run.py is intentionally minimal — most configuration comes from .env.
    The only CLI flags currently supported are:

      --version   Print the package version and exit (0).
      --help / -h Handled by argparse automatically.

    Unknown arguments are rejected so operators get a clear error instead of
    having stray flags silently ignored and causing confusing startup failures.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="evoclaw",
        description="EvoClaw — Gemini-powered personal AI assistant",
        add_help=True,
    )
    parser.add_argument(
        "--version",
        action="version",
        # BUG-RUN-01 FIX: There was no --version flag at all.  Read the
        # canonical version from pyproject.toml metadata so it stays in sync
        # automatically.  Fall back gracefully if the package is not installed
        # (e.g. during development via `python run.py` from the repo root).
        version=_get_version(),
        help="Show the EvoClaw version and exit.",
    )
    # Parse known args only — if someone passes an unknown flag we want a clear
    # error, not a silent ignore.
    _args, unknown = parser.parse_known_args()
    if unknown:
        parser.error(f"Unrecognised argument(s): {' '.join(unknown)}")


def _get_version() -> str:
    """Return the package version string from installed metadata or pyproject.toml."""
    try:
        from importlib.metadata import version as _meta_version
        return f"EvoClaw %(prog)s {_meta_version('evoclaw')}"
    except Exception:
        pass
    # Fallback: parse pyproject.toml directly (works during development)
    try:
        from pathlib import Path
        import re
        toml_path = Path(__file__).parent / "pyproject.toml"
        if toml_path.exists():
            text = toml_path.read_text(encoding="utf-8")
            m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
            if m:
                return f"EvoClaw %(prog)s {m.group(1)}"
    except Exception:
        pass
    return "EvoClaw %(prog)s (unknown version)"


if __name__ == "__main__":
    # p15d BUG-FIX (LOW): set WindowsProactorEventLoopPolicy before asyncio.run()
    # so asyncio subprocesses work correctly on Windows.  Without this, Docker
    # subprocess spawning raises NotImplementedError on Python 3.8+ Windows because
    # the default SelectorEventLoop does not support subprocess pipes.
    # host/main.py's __main__ block already did this but run.py (the actual entry
    # point) did not, so the policy was never applied when using `python run.py`.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    # BUG-RUN-01 FIX: Parse CLI arguments (including --version) before any
    # heavy imports or the preflight check, so `python run.py --version` works
    # even when the .env is missing or the host package has import errors.
    _parse_args()
    _preflight_check()
    from host.main import main
    asyncio.run(main())

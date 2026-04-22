"""Summarizer subsystem (Issue #548).

Inspired by Claude Code's WebFetchTool — instead of dumping raw fetched
content to the main model, run it through a cheap secondary model with the
user's prompt and return only the prompt-applied result.

This module is intentionally **provider-agnostic** but does NOT require
the larger refactor in #549.  Internally it dispatches to the appropriate
SDK based on env config; #549 will collapse this dispatch into a unified
LLMClient interface.

Configuration via env vars (operator sets these in .env):

    SUMMARIZER_PROVIDER   = "openai-compat" | "gemini" | "claude" | "" (disabled)
    SUMMARIZER_MODEL      = e.g. "meta/llama-3.1-8b-instruct"
    SUMMARIZER_API_KEY_REUSE = name of another env var to read the key from
                               (e.g. "NIM_API_KEY") — useful when the
                               summarizer shares a key with the main backend
    SUMMARIZER_API_KEY    = direct key (used if _REUSE not set)
    SUMMARIZER_BASE_URL   = e.g. "https://integrate.api.nvidia.com/v1"
    SUMMARIZER_MAX_TOKENS = default 1500
    SUMMARIZER_TIMEOUT_S  = default 30

If SUMMARIZER_PROVIDER is empty/unset, all summarize calls return None and
callers fall back to truncation.
"""
from __future__ import annotations

import os
import time
import threading
from typing import Optional

from _utils import _log


# ── In-memory cache (5 min TTL) ────────────────────────────────────────────────
# Same (url, prompt) within 5 minutes → cached.  Prevents the model-loop case
# observed 2026-04-20 where the LLM kept calling WebFetch on the same URL.
_CACHE_TTL_SECS = 300
_cache: dict = {}  # key = (url, prompt) → (timestamp, result)
_cache_lock = threading.Lock()


def _cache_get(key: tuple) -> Optional[str]:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        ts, val = entry
        if time.time() - ts > _CACHE_TTL_SECS:
            _cache.pop(key, None)
            return None
        return val


def _cache_set(key: tuple, value: str) -> None:
    with _cache_lock:
        # Evict oldest 25% if cache grows past 100 entries
        if len(_cache) >= 100:
            sorted_items = sorted(_cache.items(), key=lambda x: x[1][0])
            for k, _ in sorted_items[: 25]:
                _cache.pop(k, None)
        _cache[key] = (time.time(), value)


# ── Config ─────────────────────────────────────────────────────────────────────
def _get_config() -> dict:
    """Read summarizer config from env each call (cheap, allows hot reload)."""
    provider = (os.environ.get("SUMMARIZER_PROVIDER", "") or "").strip().lower()
    model = os.environ.get("SUMMARIZER_MODEL", "").strip()
    if not provider or not model:
        return {"enabled": False}

    key_reuse = os.environ.get("SUMMARIZER_API_KEY_REUSE", "").strip()
    api_key = os.environ.get(key_reuse, "") if key_reuse else os.environ.get("SUMMARIZER_API_KEY", "")
    if not api_key:
        return {"enabled": False, "reason": "no_api_key"}

    try:
        max_tokens = int(os.environ.get("SUMMARIZER_MAX_TOKENS", "1500"))
    except ValueError:
        max_tokens = 1500
    try:
        timeout_s = float(os.environ.get("SUMMARIZER_TIMEOUT_S", "30"))
    except ValueError:
        timeout_s = 30.0

    return {
        "enabled": True,
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "base_url": os.environ.get("SUMMARIZER_BASE_URL", "").strip(),
        "max_tokens": max_tokens,
        "timeout_s": timeout_s,
    }


def is_enabled() -> bool:
    return _get_config().get("enabled", False)


# ── Public entry ───────────────────────────────────────────────────────────────
_DEFAULT_SYSTEM = (
    "You are a precise content processor.  Apply the user's prompt to the "
    "provided content.  Return ONLY the requested result — no preamble, no "
    "meta-commentary, no markdown fences unless the prompt asks for code."
)


def summarize(content: str, prompt: str, *, cache_key: Optional[tuple] = None) -> Optional[str]:
    """Apply prompt to content via the configured summarizer model.

    Returns the model's text output, or None if:
        * summarizer not configured
        * provider unknown
        * call failed (logged + returns None so caller can fall back)

    cache_key, if provided, enables 5-min in-memory caching keyed by it.
    """
    if not content:
        return None
    cfg = _get_config()
    if not cfg.get("enabled"):
        return None

    if cache_key is not None:
        cached = _cache_get(cache_key)
        if cached is not None:
            _log("📋 SUM-CACHE", f"hit key={cache_key[0][:60]!r}")
            return cached

    # Issue #549: dispatch via the unified LLMClient interface.
    from _llm_client import make_client
    try:
        client = make_client(
            provider=cfg["provider"],
            model=cfg["model"],
            api_key=cfg["api_key"],
            base_url=cfg.get("base_url", ""),
            timeout_s=cfg["timeout_s"],
        )
    except ValueError as cfg_err:
        _log("⚠️ SUM-CFG", str(cfg_err))
        return None

    user = f"<content>\n{content}\n</content>\n\n<task>\n{prompt}\n</task>"
    t0 = time.time()
    try:
        result = client.complete(_DEFAULT_SYSTEM, user, max_tokens=cfg["max_tokens"]).text
    except Exception as exc:
        _log("⚠️ SUM-FAIL", f"provider={cfg['provider']} model={cfg['model']} err={type(exc).__name__}: {exc}")
        return None
    dur_ms = int((time.time() - t0) * 1000)
    _log(
        "✨ SUM-OK",
        f"provider={cfg['provider']} model={cfg['model']} "
        f"in={len(content)}B out={len(result)}B dur={dur_ms}ms",
    )

    if cache_key is not None:
        _cache_set(cache_key, result)
    return result

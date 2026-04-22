"""LLMClient — unified interface over Gemini / OpenAI-compat / Claude SDKs.

Issue #549. Eliminates the per-provider dispatching duplicated between
`_summarizer.py` and (eventually) `_loop_*.py`.

Scope of THIS PR
----------------
This PR ships **simple completion** support — one prompt in, text out.
Streaming + tool-calls (needed by the agentic loops in `_loop_openai.py`,
`_loop_gemini.py`, `_loop_claude.py`) is a follow-up.

That ordering is deliberate:
1. summarizer is the lowest-risk consumer — failures degrade gracefully
2. proves the interface works before we touch the OOM-prone main loop
3. lets us iterate the interface based on real usage before locking it in

Design
------
LLMClient is a Protocol — no inheritance, no base class. Adapters are
plain dataclasses so adding a backend is `make_client()` + a 30-line
adapter and nothing else.

Adapters do **lazy SDK imports** (matching the pattern in `agent.py`)
so that loading the openai-compat adapter does NOT pull google-genai
into memory.

Configuration
-------------
make_client() takes provider/model/api_key/base_url/timeout. Callers
that read env vars (e.g. _summarizer._get_config()) hand the values in;
the adapter doesn't touch os.environ.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class CompletionResult:
    """Shape returned by every adapter's complete() call."""
    text: str
    # raw provider response, for callers that want extra metadata.  Avoid
    # depending on this — keep all logic against `text`.
    raw: object = None


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, temperature: float = 0.2) -> CompletionResult:
        """Run a single-turn completion. system + user → text."""
        ...


# ── Adapters ──────────────────────────────────────────────────────────────────
@dataclass
class _OpenAICompatClient:
    provider: str
    model: str
    api_key: str
    base_url: str
    timeout_s: float

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, temperature: float = 0.2) -> CompletionResult:
        from openai import OpenAI
        import httpx
        timeout = httpx.Timeout(connect=15.0, read=self.timeout_s, write=15.0, pool=10.0)
        base = self.base_url or "https://api.openai.com/v1"
        client = OpenAI(base_url=base, api_key=self.api_key, timeout=timeout)
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = (resp.choices[0].message.content or "").strip()
        return CompletionResult(text=text, raw=resp)


@dataclass
class _GeminiClient:
    provider: str
    model: str
    api_key: str
    base_url: str       # unused for Gemini (kept for interface symmetry)
    timeout_s: float    # not directly enforceable on google-genai sync calls

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, temperature: float = 0.2) -> CompletionResult:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.api_key)
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        resp = client.models.generate_content(model=self.model, contents=user, config=cfg)
        text = (resp.text or "").strip()
        return CompletionResult(text=text, raw=resp)


@dataclass
class _ClaudeClient:
    provider: str
    model: str
    api_key: str
    base_url: str       # passed to anthropic.Anthropic if non-empty
    timeout_s: float

    def complete(self, system: str, user: str, *, max_tokens: int = 1500, temperature: float = 0.2) -> CompletionResult:
        import anthropic
        kwargs = {"api_key": self.api_key, "timeout": self.timeout_s}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = anthropic.Anthropic(**kwargs)
        resp = client.messages.create(
            model=self.model,
            system=system,
            messages=[{"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        parts = []
        for block in resp.content:
            t = getattr(block, "text", "")
            if t:
                parts.append(t)
        return CompletionResult(text="".join(parts).strip(), raw=resp)


# ── Factory ───────────────────────────────────────────────────────────────────
# Aliases let operators write provider names that match common conventions.
_PROVIDER_ALIASES = {
    "openai-compat": _OpenAICompatClient,
    "openai": _OpenAICompatClient,
    "nim": _OpenAICompatClient,
    "groq": _OpenAICompatClient,
    "qwen": _OpenAICompatClient,
    "gemini": _GeminiClient,
    "google": _GeminiClient,
    "claude": _ClaudeClient,
    "anthropic": _ClaudeClient,
}


def make_client(provider: str, model: str, api_key: str, *, base_url: str = "", timeout_s: float = 30.0) -> LLMClient:
    """Construct an adapter for the named provider.

    Raises ValueError on unknown provider; callers should catch this if
    they want to log + skip rather than abort.
    """
    cls = _PROVIDER_ALIASES.get(provider.lower())
    if cls is None:
        raise ValueError(f"Unknown LLM provider {provider!r}. Known: {sorted(set(_PROVIDER_ALIASES.keys()))}")
    return cls(provider=provider, model=model, api_key=api_key, base_url=base_url, timeout_s=timeout_s)


def known_providers() -> list[str]:
    """For diagnostics + config validation."""
    return sorted(set(_PROVIDER_ALIASES.keys()))

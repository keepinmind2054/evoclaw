"""Utility functions for the EvoClaw agent runner."""
import json, os, sys, time, random, string, threading
from pathlib import Path
from _constants import (
    IPC_MESSAGES_DIR, IPC_TASKS_DIR, IPC_RESULTS_DIR, WORKSPACE,
    _ALLOWED_PATH_PREFIXES, _MAX_TOOL_RESULT_CHARS,
)

# ── Thread-safety for tool_web_fetch's socket monkey-patch (issue #445) ──────
# socket.create_connection is a module-level global; patching it in multiple
# threads simultaneously causes races where one thread restores the original
# while another thread's request is still in flight through the patched version.
# Serialise all web fetches through this lock so only one thread can hold the
# patch at a time.  Performance impact is minimal because tool calls are already
# serialised per-session by the agentic loop.
_SSRF_PATCH_LOCK = threading.Lock()

import datetime as _dt


def _log(tag: str, msg: str = "") -> None:
    """Structured stderr logging with millisecond timestamps."""
    ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {tag} {msg}", file=sys.stderr, flush=True)


def _atomic_ipc_write(fname: Path, data: str) -> None:
    """Atomically write *data* to *fname* via a .tmp sibling file.

    All IPC tool functions produce JSON files consumed by the host's
    ipc_watcher.  If the file is written non-atomically (direct write_text)
    the host may read a partial JSON when the inotify CREATE event fires
    before the write completes.  This helper centralises the
    ``tmp = fname.with_suffix('.tmp'); tmp.write_text(...); tmp.rename(fname)``
    pattern that previously appeared 10+ times across tool implementations.
    """
    tmp = fname.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.rename(fname)  # POSIX rename() is atomic


def _write_ipc_file(ipc_dir: str, payload: dict, suffix: str = "") -> Path:
    """Create a timestamped IPC JSON file in *ipc_dir* and write *payload* atomically.

    Centralises the boilerplate that previously appeared in every IPC tool:
      1. ensure the directory exists
      2. generate a random uid suffix (prevents millisecond-level collisions)
      3. build the filename  ``{timestamp_ms}-{uid}[-{suffix}].json``
      4. write atomically via _atomic_ipc_write

    Returns the Path of the written file.

    Issue #444: IPC write pattern was duplicated 6+ times across _tools.py.
    """
    uid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    dir_path = Path(ipc_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time()*1000)}-{uid}"
    if suffix:
        name = f"{name}-{suffix}"
    fname = dir_path / f"{name}.json"
    _atomic_ipc_write(fname, json.dumps(payload))
    return fname


class _KeyPool:
    """Round-robin key rotation pool with per-key failure tracking."""

    def __init__(self, keys_csv: str):
        self._keys = [k.strip() for k in (keys_csv or "").split(",") if k.strip()]
        self._idx = 0
        self._lock = threading.Lock()

    def __bool__(self):
        return bool(self._keys)

    def current(self) -> str:
        if not self._keys:
            return ""
        with self._lock:
            return self._keys[self._idx % len(self._keys)]

    def rotate(self) -> str:
        """Advance to the next key and return it."""
        if not self._keys:
            return ""
        with self._lock:
            self._idx = (self._idx + 1) % len(self._keys)
            return self._keys[self._idx]

    def __len__(self):
        return len(self._keys)


def _is_qwen_model(model_name: str) -> bool:
    """Check if the model is a Qwen variant (needs special handling)."""
    return "qwen" in (model_name or "").lower()


def _llm_call_with_retry(fn, max_attempts: int = 3, base_delay: float = 1.0, pool: "_KeyPool | None" = None, apply_key_fn=None):
    """Call an LLM API function with exponential backoff retry on transient errors.

    Retries on HTTP 429 (rate limit), 500, 502, 503, 529 (server errors).
    Permanent errors (400 bad request, 401 unauthorized) are not retried.

    When a pool and apply_key_fn are provided, rotates to the next key in the
    pool on 429/ResourceExhausted errors before retrying.

    Args:
        fn: Zero-argument callable that performs the LLM API call.
        max_attempts: Maximum number of total attempts (default 3).
        base_delay: Initial delay in seconds; doubles on each retry.
        pool: Optional _KeyPool for automatic key rotation on rate limit errors.
        apply_key_fn: Optional callable(key: str) -> None that updates the
            active API key when a rotation occurs (e.g. re-initialise the client).

    Returns:
        The API response from fn().

    Raises:
        The last exception if all attempts are exhausted.
    """
    _RETRYABLE_STATUS = {429, 500, 502, 503, 529}
    _RATE_LIMIT_KW = ("rate limit", "resource exhausted", "too many requests", "quota")
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            exc_str = str(exc)
            # Determine if this is a retryable error by inspecting the exception text
            is_rate_limit = any(str(code) in exc_str for code in {429}) or any(
                kw in exc_str.lower() for kw in _RATE_LIMIT_KW
            )
            is_retryable = is_rate_limit or any(str(code) in exc_str for code in _RETRYABLE_STATUS) or any(
                kw in exc_str.lower()
                for kw in ("overloaded", "service unavailable", "bad gateway", "timeout")
            )
            if not is_retryable or attempt == max_attempts - 1:
                raise
            # On rate-limit errors, rotate to the next API key before sleeping
            if is_rate_limit and pool and apply_key_fn and len(pool) > 1:
                new_key = pool.rotate()
                apply_key_fn(new_key)
                _log("🔑 KEY ROTATE", f"switched to key index {pool._idx} (pool size={len(pool)})")
            delay = base_delay * (2 ** attempt)
            _log("⚠️ LLM retry", f"attempt={attempt + 1}/{max_attempts} delay={delay:.1f}s err={exc_str[:80]}")
            time.sleep(delay)
    raise last_exc  # unreachable but satisfies type checkers


def _check_path_allowed(file_path: str) -> str | None:
    """Return an error string if the resolved path is outside the allowed workspace,
    or None if the path is acceptable.

    This is a defence-in-depth measure inside the container to prevent
    prompt-injection attacks from reading sensitive container files like
    /proc/self/environ (which may contain env vars) or /etc/passwd.
    """
    # BUG-P26B-4: reject empty paths and paths containing null bytes before
    # calling Path().resolve().  An empty string resolves to the Python process
    # CWD which may or may not be inside /workspace/ (non-deterministic).  A
    # path with embedded null bytes (\x00) would be silently truncated by the
    # C-level open() syscall, potentially accessing a different file than intended.
    if not file_path:
        return "Error: file path must not be empty"
    if "\x00" in file_path:
        return "Error: file path must not contain null bytes"
    try:
        resolved = str(Path(file_path).resolve())
    except Exception as exc:
        return f"Error: cannot resolve path {file_path!r}: {exc}"
    if not any(resolved.startswith(prefix) for prefix in _ALLOWED_PATH_PREFIXES):
        _log("⚠️ SECURITY", f"path sandbox violation: {file_path!r} resolved to {resolved!r}")
        return (
            f"Error: access denied — path {file_path!r} is outside the allowed workspace. "
            f"Only paths within /workspace/ are permitted."
        )
    return None

#!/usr/bin/env python3
"""
EvoClaw Agent Runner (Python + Gemini / OpenAI-compatible)
Reads ContainerInput JSON from stdin, runs agentic loop, outputs to stdout.
Supports Gemini (default) or any OpenAI-compatible API (NVIDIA NIM, OpenAI, Groq, etc.)
"""

import json
import os
import sys
import subprocess
import time
import random
import string
import glob as _glob_module
import urllib.request
import urllib.error
import html.parser
import traceback
import datetime as _dt
import uuid
import threading
from pathlib import Path

import logging as _logging

# Phase 1 (UnifiedClaw): Fitness feedback to Gateway
try:
    from fitness_reporter import FitnessReporter as _FitnessReporter
    _REPORTER_AVAILABLE = True
except ImportError:
    _REPORTER_AVAILABLE = False

_logging.getLogger("httpx").setLevel(_logging.WARNING)
_logging.getLogger("httpcore").setLevel(_logging.WARNING)
_logging.getLogger("google").setLevel(_logging.WARNING)
_logging.getLogger("urllib3").setLevel(_logging.WARNING)
try:
    from google import genai
    from google.genai import types
    _GOOGLE_AVAILABLE = True
except ImportError:
    genai = None  # type: ignore
    types = None  # type: ignore
    _GOOGLE_AVAILABLE = False
try:
    from openai import OpenAI as OpenAIClient
    import httpx
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False
    httpx = None  # type: ignore
try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

# container 輸出的邊界標記，host 用這兩個字串從 stdout 截取 JSON 結果
# 必須與 container_runner.py 中定義的常數完全一致
OUTPUT_START = "---EVOCLAW_OUTPUT_START---"
OUTPUT_END = "---EVOCLAW_OUTPUT_END---"

# IPC 目錄路徑（由 host 透過 Docker volume mount 對應到 data/ipc/<folder>/）
IPC_MESSAGES_DIR = "/workspace/ipc/messages"  # agent 發送訊息給用戶
IPC_TASKS_DIR = "/workspace/ipc/tasks"        # agent 建立排程任務
IPC_RESULTS_DIR = "/workspace/ipc/results"

# agent 的工作目錄，對應到 host 的 groups/<folder>/ 目錄
WORKSPACE = "/workspace/group"

# Allowed top-level prefixes for file-system tool operations.
# Paths must resolve inside one of these directories to be accepted.
_ALLOWED_PATH_PREFIXES = (
    "/workspace/",   # covers /workspace/group, /workspace/ipc, /workspace/project, etc.
)

# Module-level chat JID — populated from input JSON so tool_send_file can auto-detect it
_input_chat_jid: str = ""

# History / tool-result size limits (P9A)
_MAX_TOOL_RESULT_CHARS = 4000  # ~4KB per tool result
_MAX_HISTORY_MESSAGES = 40     # max messages in history

# BUG-P21-1 / BUG-P21-4: Module-level action-claim regex with stricter structure.
# The old pattern matched single Chinese characters like 已/完成/成功 as standalone
# tokens, causing false positives on normal sentences such as "我已了解您的問題".
# The new pattern requires 已+specificVerb or verb+了/完成 structures, reducing noise.
import re as _re_module_level
_ACTION_CLAIM_RE = _re_module_level.compile(
    r'(?:'
    r'已(?:完成|修復|修正|部署|更新|新增|刪除|建立|創建|執行|運行|安裝|設定|配置|提交|推送|合併)'
    r'|(?:完成|修復|修正|部署|更新|新增|刪除|建立|創建|執行|運行|安裝|設定|配置|提交|推送|合併)了'
    r'|(?:successfully|completed|deployed|fixed|updated|committed|pushed|merged)\s+(?:the\s+)?(?:fix|update|feature|change|patch|code|file)'
    r')',
    _re_module_level.IGNORECASE,
)


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


def _write_ipc_task(task_type: str, payload: dict, ipc_dir: str = None) -> str:
    """Write an IPC task JSON file atomically and return the filename stem.

    Encapsulates the repeated pattern of:
      mkdir -> random uid -> millisecond timestamp filename -> atomic write JSON

    Args:
        task_type: The ``"type"`` field value and filename slug (e.g. ``"cancel_task"``).
        payload:   Dict to serialise as JSON; ``{"type": task_type}`` is merged in.
        ipc_dir:   Directory to write into; defaults to ``IPC_TASKS_DIR``.

    Returns:
        The ``Path.name`` of the written file (useful for log messages).

    Raises:
        Any ``OSError`` / ``Exception`` from mkdir or write -- callers should
        wrap in ``try/except`` and return ``f"Error: {e}"`` as usual.
    """
    target_dir = Path(ipc_dir) if ipc_dir else Path(IPC_TASKS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    uid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    slug = task_type.replace("_", "-")
    fname = target_dir / f"{int(time.time() * 1000)}-{slug}-{uid}.json"
    _atomic_ipc_write(fname, json.dumps({"type": task_type, **payload}))
    return fname.name


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


# ── Tool implementations ──────────────────────────────────────────────────────

def tool_bash(command: str) -> str:
    """
    在 /workspace/group 目錄中執行 bash 指令，回傳 stdout + stderr 輸出。

    timeout=300 秒：防止指令無限期阻塞（例如 git clone 或 npm install 過慢）。
    shell=False：exec bash directly，避免雙層 shell 解析風險。
    同時回傳 stderr 讓 agent 能看到錯誤訊息並自行修正。
    輸出限制 50KB：防止大量輸出撐爆 LLM context window。
    危險指令封鎖：防止 prompt-injection 攻擊執行破壞性指令。

    P14D-BASH-1: stdin is explicitly closed (DEVNULL) so the child process
    never blocks waiting for interactive input from the bot's own stdin.

    P14D-BASH-2: on TimeoutExpired the child and its entire process group are
    sent SIGKILL so they do not linger as zombies or continue consuming CPU/IO
    after the timeout.

    P14D-BASH-3: non-zero exit codes are surfaced to the LLM in the return
    value so it knows the command failed rather than silently succeeding.
    """
    # ── Dangerous command blocklist ───────────────────────────────────────────
    # Block commands that could destroy the container filesystem or host mounts.
    # Uses a whitespace/flag-aware pattern to catch common variants.
    import re as _re_bash
    import signal as _signal
    _DANGEROUS_PATTERNS = [
        # rm -rf / and variants (rm -rf /*, rm --no-preserve-root /, etc.)
        r'\brm\s+.*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+[/~]',
        r'\brm\s+.*-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+[/~]',
        r'\brm\s+.*--no-preserve-root',
        # dd writing to block devices or /dev/
        r'\bdd\b.*\bof\s*=\s*/dev/',
        # mkfs — format a filesystem
        r'\bmkfs\b',
        # fork bomb
        r':\s*\(\s*\)\s*\{.*:\|:.*\}',
        # writing directly to /dev/sda, /dev/nvme, etc.
        r'>\s*/dev/[sh]d[a-z]',
        r'>\s*/dev/nvme',
        # chmod/chown 777 on / or critical system dirs (but not /workspace/)
        # BUG-P18D-12: the old r'\bchown\s+.*\s+/' matched any path starting
        # with / including /workspace/group/myfile, blocking legitimate ops.
        # Restrict to actual system directory roots only.
        r'\bchmod\s+.*777\s+/(?!workspace/)',
        r'\bchown\s+.*\s+/(?:etc|bin|usr|lib|sbin|var|boot|root|proc|sys|dev)\b',
        # shred /dev/* or critical paths
        r'\bshred\s+.*/dev/',
    ]
    _cmd_strip = command.strip()
    for _pat in _DANGEROUS_PATTERNS:
        if _re_bash.search(_pat, _cmd_strip, _re_bash.IGNORECASE | _re_bash.DOTALL):
            _log("🚨 SECURITY", f"Bash: blocked dangerous command pattern: {_cmd_strip[:200]}")
            return "Error: command blocked — matches dangerous command pattern (rm -rf /, dd to block device, mkfs, etc.)"

    # Sanity check: reject commands that are bare filenames (no spaces, no shell
    # operators).  The LLM sometimes passes a filename like "MEMORY.md" or
    # "/workspace/group/MEMORY.md" as the bash command, which produces
    # "bash: MEMORY.md: command not found" (exit 127) and wastes a turn.
    # Legitimate bash commands always contain at least one space OR a shell
    # operator (|, ;, &&, >, <, $, etc.).
    _cmd_stripped = command.strip()
    _BARE_FILENAME_RE = _re_bash.compile(
        r'^[^\s|;&<>$`\'\"()\[\]{}!\\]+\.(md|txt|py|js|ts|sh|json|yaml|yml|toml|csv|log|conf|cfg)$',
        _re_bash.IGNORECASE,
    )
    if _BARE_FILENAME_RE.match(_cmd_stripped):
        _log("⚠️ BASH-SANITY", f"Rejected bare filename as bash command: {_cmd_stripped[:100]}")
        return (
            f"✗ Invalid command: '{_cmd_stripped}' looks like a filename, not a bash command.\n"
            "To READ a file use the Read tool. To WRITE a file use the Write or Edit tool.\n"
            "Example: Read({\"file_path\": \"/workspace/group/MEMORY.md\"})"
        )

    # BUG-P26B-5: a command string containing a null byte (\x00) would be
    # silently truncated at the C-level execve() boundary, executing only the
    # portion before the first null byte.  This can allow a prompt-injected
    # payload to hide commands after a null byte that only partially execute.
    # Reject such commands early with a clear error message.
    if "\x00" in command:
        return "\u2717 [exit ?] Error: command must not contain null bytes"

    _BASH_OUTPUT_LIMIT = 50 * 1024  # 50 KB

    proc = None
    try:
        proc = subprocess.Popen(
            ["bash", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,   # P14D-BASH-1: never block on stdin
            cwd=WORKSPACE,
            # start_new_session=True creates a new process group so we can
            # kill the entire group (including child processes) on timeout.
            start_new_session=True,
        )
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            # P14D-BASH-2: kill the whole process group, not just the leader
            try:
                import os as _os
                _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
            except Exception:
                proc.kill()
            # BUG-P18D-16: add timeout to post-kill communicate() so an
            # unkillable process (D-state) cannot hang the agent loop forever.
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # best-effort reap; process is already SIGKILL'd
            return "Error: command timed out after 300s"

        out = stdout_bytes.decode("utf-8", errors="replace")
        err_text = stderr_bytes.decode("utf-8", errors="replace")
        if err_text:
            out += f"\nSTDERR:\n{err_text}"
        if len(out) > _BASH_OUTPUT_LIMIT:
            out = out[:_BASH_OUTPUT_LIMIT] + f"\n... (output truncated at 50KB, total {len(out)} bytes)"
        # Fix 2 (STABILITY_ANALYSIS 3.1): prefix result with unambiguous success/failure flag
        # so the LLM sees exit status as the FIRST characters, not buried at the end.
        _exit_code = proc.returncode
        if _exit_code == 0:
            return f"\u2713 [exit 0] {out or '(no output)'}"
        else:
            return f"\u2717 [exit {_exit_code}] {out or '(no output)'}"
    except Exception as e:
        # BUG-P26B-6: if an unexpected exception occurs after Popen() succeeds
        # (e.g. MemoryError decoding output), the child process may still be
        # running or waiting for pipes to be drained.  Kill and reap it so no
        # zombie remains.
        if proc is not None:
            try:
                import os as _os_cleanup
                import signal as _sig_cleanup
                _os_cleanup.killpg(_os_cleanup.getpgid(proc.pid), _sig_cleanup.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        return f"\u2717 [exit ?] Error: {e}"


def tool_read(file_path: str) -> str:
    """讀取指定路徑的文字檔案內容，讓 agent 可以檢視檔案。
    檔案大小限制 512KB：防止讀取巨大檔案導致 OOM 或 context 爆炸。

    P14D-READ-1: Symlink resolution is re-checked AFTER resolving the path so
    that symlinks pointing outside /workspace/ (e.g. /etc/passwd) are blocked.
    The existing _check_path_allowed() call only checks the raw input string;
    a symlink like /workspace/group/evil -> /etc/passwd would pass that check
    but lead to reading host-sensitive files.

    P14D-READ-2: Binary files are detected from the first 512 bytes and
    rejected with an explanatory message rather than being returned as garbled
    UTF-8 replacement characters.

    P14D-READ-3: Non-UTF-8 text files are read with errors="replace" so a
    Latin-1 file does not raise UnicodeDecodeError.
    """
    _READ_SIZE_LIMIT = 512 * 1024  # 512 KB

    err = _check_path_allowed(file_path)
    if err:
        return err

    try:
        p = Path(file_path)

        # P14D-READ-1: resolve symlinks and re-check the *real* path
        try:
            resolved = p.resolve()
        except Exception as exc:
            return f"Error: cannot resolve path {file_path!r}: {exc}"
        resolved_str = str(resolved)
        if not any(resolved_str.startswith(prefix) for prefix in _ALLOWED_PATH_PREFIXES):
            _log("⚠️ SECURITY", f"Read: symlink escape blocked: {file_path!r} -> {resolved_str!r}")
            return (
                f"Error: access denied — {file_path!r} resolves to {resolved_str!r} "
                f"which is outside the allowed workspace (symlink escape prevention)."
            )

        file_size = p.stat().st_size

        # P14D-READ-2: binary detection via null-byte heuristic and strict
        # UTF-8 decode test.
        # BUG-P18D-14: the old high-byte fraction heuristic (>30% bytes > 127)
        # incorrectly rejected valid UTF-8 files consisting mostly of multi-byte
        # characters (e.g. a file written entirely in Chinese/Japanese/Korean).
        # UTF-8 multi-byte sequences are 2-4 bytes each with the high bit set,
        # so a Chinese-only file has ~100% high bytes even though it is perfectly
        # valid text.  Replace the high-byte fraction test with a strict
        # UTF-8 decode of the sample: if the bytes are not valid UTF-8 AND
        # contain a null byte (strong binary signal), reject as binary.
        with p.open("rb") as fh:
            sample = fh.read(min(512, file_size))
        _has_null = b"\x00" in sample
        _is_valid_utf8 = True
        if sample:
            try:
                sample.decode("utf-8")
            except UnicodeDecodeError:
                _is_valid_utf8 = False
        if _has_null or (not _is_valid_utf8 and len(sample) > 0 and sum(b > 127 for b in sample) > len(sample) * 0.5):
            return (
                f"Error: {file_path!r} appears to be a binary file. "
                "tool_read only supports text files. Use Bash + base64 to inspect binary content."
            )

        if file_size > _READ_SIZE_LIMIT:
            # Read only the first 512KB and warn
            with p.open("rb") as fh:
                raw = fh.read(_READ_SIZE_LIMIT)
            # P14D-READ-3: decode with errors="replace" to handle non-UTF-8 text
            text = raw.decode("utf-8", errors="replace")
            # BUG-P21-2: prefix with [OK] so _tool_fail_counter resets correctly
            return f"[OK] {file_path}\n" + text + f"\n\n... (file truncated: read {_READ_SIZE_LIMIT} of {file_size} bytes)"

        # P14D-READ-3: use errors="replace" so Latin-1/Windows-1252 files don't crash
        # BUG-P21-2: prefix with [OK] so _tool_fail_counter resets correctly on successful reads
        content = p.read_text(encoding="utf-8", errors="replace")
        return f"[OK] {file_path}\n{content}"
    except Exception as e:
        return f"Error reading file: {e}"


def tool_write(file_path: str, content: str) -> str:
    """
    將內容寫入指定路徑的檔案。
    自動建立不存在的父目錄（mkdir -p），簡化 agent 的操作步驟。
    寫入大小限制 10MB：防止寫入過大檔案耗盡磁碟。
    原子寫入：先寫入 .tmp 再 rename，防止部分寫入導致檔案損毀。

    P14D-WRITE-1: If the target file already exists its mode bits (permissions)
    are preserved across the atomic replace.  Without this, every Write call
    would silently reset a chmod +x script to 0o600, breaking executables.
    """
    _WRITE_SIZE_LIMIT = 10 * 1024 * 1024  # 10 MB

    err = _check_path_allowed(file_path)
    if err:
        return f"[ERROR] {err}"
    if len(content.encode("utf-8")) > _WRITE_SIZE_LIMIT:
        return f"[ERROR] content too large ({len(content.encode('utf-8'))} bytes > 10MB limit)"
    try:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # BUG-P18D-02: re-check the *resolved* path of the parent directory after
        # mkdir so a symlinked parent (e.g. /workspace/group/evil -> /etc/) does
        # not bypass the sandbox.  _check_path_allowed only inspects the raw string;
        # resolve() follows symlinks to the real destination.
        try:
            _resolved_parent = str(p.parent.resolve())
        except Exception as _rp_exc:
            return f"[ERROR] cannot resolve parent directory: {_rp_exc}"
        if not any(_resolved_parent.startswith(pfx) for pfx in _ALLOWED_PATH_PREFIXES):
            _log("⚠️ SECURITY", f"Write: symlink parent escape blocked: {file_path!r} -> {_resolved_parent!r}")
            return (
                f"[ERROR] access denied — parent directory of {file_path!r} resolves to "
                f"{_resolved_parent!r} which is outside the allowed workspace (symlink escape prevention)."
            )
        # BUG-P32-02 FIX (LOW): Reject writes where the target path is an
        # existing directory.  Without this check, the attempt proceeds until
        # tmp.rename(p) raises "IsADirectoryError: [Errno 21] Is a directory"
        # which is caught and returned as a confusing [ERROR] message.  An
        # explicit check here produces a clear, actionable error message and
        # avoids writing a .tmp file into the parent directory unnecessarily.
        if p.exists() and p.is_dir():
            return f"[ERROR] {file_path!r} is an existing directory — cannot overwrite a directory with a file"

        # P14D-WRITE-1: capture existing permissions before overwriting
        existing_mode = None
        if p.exists():
            try:
                existing_mode = p.stat().st_mode
            except Exception:
                pass
        # Atomic write: write to a sibling tmp file with a fixed unique suffix then
        # rename.  Use os.getpid() + id() so the tmp name is unique even if two
        # concurrent writes target the same file, and cannot collide with the target
        # (BUG-P18D-01: the old p.with_suffix(p.suffix+".tmp") produced the same
        # name as the target when file_path already ended in ".tmp").
        import os as _os_write
        tmp = p.parent / f".{p.name}.{_os_write.getpid()}.{id(content) & 0xFFFF}.tmp"
        tmp.write_text(content, encoding="utf-8")
        # Restore permissions on the tmp file before renaming so the final
        # file inherits the original mode atomically.
        if existing_mode is not None:
            try:
                import os as _os
                _os.chmod(tmp, existing_mode)
            except Exception:
                pass
        tmp.rename(p)  # POSIX rename() is atomic
        return f"[OK] Written: {file_path}"
    except Exception as e:
        return f"[ERROR] {e}"


def tool_edit(file_path: str, old_string: str, new_string: str) -> str:
    """
    在檔案中找到 old_string 並替換為 new_string（只替換第一個出現的位置）。
    若 old_string 不存在則回傳錯誤，讓 agent 知道需要先確認內容再修改。
    原子寫入：先寫入 .tmp 再 rename，防止部分寫入導致檔案損毀。

    P14D-EDIT-1: If old_string appears more than once in the file the LLM is
    warned about the ambiguity so it can provide a longer, unique context
    string.  Previously the first occurrence was silently replaced, which could
    corrupt the wrong section of the file.

    P14D-EDIT-2: File permissions are preserved across the atomic replace, for
    the same reason as tool_write (P14D-WRITE-1).

    P14D-EDIT-3: Files that cannot be decoded as UTF-8 are read with
    errors="replace" so the function does not crash on Latin-1 files.
    """
    _WRITE_SIZE_LIMIT = 10 * 1024 * 1024  # 10 MB

    err = _check_path_allowed(file_path)
    if err:
        return f"[ERROR] {err}"
    try:
        p = Path(file_path)
        # BUG-P18D-03: re-check the *resolved* path of the parent after symlink
        # expansion, matching the same defence added to tool_write (BUG-P18D-02).
        try:
            _resolved_edit_parent = str(p.parent.resolve())
        except Exception as _rep_exc:
            return f"[ERROR] cannot resolve parent directory: {_rep_exc}"
        if not any(_resolved_edit_parent.startswith(pfx) for pfx in _ALLOWED_PATH_PREFIXES):
            _log("⚠️ SECURITY", f"Edit: symlink parent escape blocked: {file_path!r} -> {_resolved_edit_parent!r}")
            return (
                f"[ERROR] access denied — parent directory of {file_path!r} resolves to "
                f"{_resolved_edit_parent!r} which is outside the allowed workspace (symlink escape prevention)."
            )
        # P14D-EDIT-3: tolerate non-UTF-8 files
        content = p.read_text(encoding="utf-8", errors="replace")
        if old_string not in content:
            return f"[ERROR] old_string not found in {file_path}"
        # P14D-EDIT-1: warn if old_string appears multiple times to prevent
        # the LLM from inadvertently editing the wrong occurrence
        count = content.count(old_string)
        if count > 1:
            return (
                f"[ERROR] old_string appears {count} times in {file_path}. "
                "Provide a longer, unique context string that matches exactly one location."
            )
        # replace(..., 1) 確保只替換第一個出現的位置，避免意外修改多處
        new_content = content.replace(old_string, new_string, 1)
        if len(new_content.encode("utf-8")) > _WRITE_SIZE_LIMIT:
            return f"[ERROR] resulting file too large (> 10MB limit)"
        # P14D-EDIT-2: preserve existing file permissions
        existing_mode = None
        try:
            existing_mode = p.stat().st_mode
        except Exception:
            pass
        # Atomic write: use unique tmp name to avoid collision (BUG-P18D-01:
        # the old p.with_suffix(p.suffix+".tmp") produced the same name as the
        # target when file_path already ended in ".tmp").
        import os as _os_edit
        tmp = p.parent / f".{p.name}.{_os_edit.getpid()}.{id(new_content) & 0xFFFF}.tmp"
        tmp.write_text(new_content, encoding="utf-8")
        if existing_mode is not None:
            try:
                import os as _os
                _os.chmod(tmp, existing_mode)
            except Exception:
                pass
        tmp.rename(p)  # POSIX rename() is atomic
        return f"[OK] Edited: {file_path}"
    except Exception as e:
        return f"[ERROR] {e}"


def tool_send_message(chat_jid: str, text: str, sender: str = None) -> str:
    """
    透過 IPC 機制將訊息發送給用戶（寫入 JSON 檔案，host 的 ipc_watcher 負責實際傳送）。

    檔名格式：{timestamp_ms}-{random_8_chars}.json
    使用時間戳記前綴確保 ipc_watcher 按 FIFO 順序處理；
    加入隨機後綴避免同一毫秒內產生多個檔案時發生名稱衝突。
    空訊息檢查：拒絕空白訊息，避免發送無效 IPC 檔案。
    長度限制 32KB：防止超大訊息被傳送或破壞 IPC JSON。
    """
    _MSG_MAX_LEN = 32 * 1024  # 32 KB

    if not text or not text.strip():
        return "Error: message text cannot be empty"
    if len(text) > _MSG_MAX_LEN:
        text = text[:_MSG_MAX_LEN] + f"\n... (message truncated at 32KB)"
    try:
        ipc_dir = Path(IPC_MESSAGES_DIR)
        ipc_dir.mkdir(parents=True, exist_ok=True)
        uid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        fname = ipc_dir / f"{int(time.time()*1000)}-{uid}.json"
        payload = {"type": "message", "chatJid": chat_jid, "text": text}
        if sender:
            payload["sender"] = sender  # 可選的發送者名稱（顯示為不同的 bot 身份）
        _atomic_ipc_write(fname, json.dumps(payload))
        _log("📨 IPC", f"type=message → {fname.name}")
        return "Message sent"
    except Exception as e:
        return f"Error: {e}"


def tool_schedule_task(prompt: str, schedule_type: str, schedule_value: str, context_mode: str = "group", chat_jid: str = "") -> str:
    """
    透過 IPC 機制建立排程任務（寫入 JSON 檔案到 tasks/ 子目錄）。
    host 的 ipc_watcher 讀取後會呼叫 db.create_task 正式寫入 DB。
    chat_jid 必須包含在 payload 中，讓 ipc_watcher 能將正確的群組 JID 存入 DB，
    供排程器執行任務時路由回正確的聊天室。
    """
    try:
        tasks_dir = Path(IPC_TASKS_DIR)
        tasks_dir.mkdir(parents=True, exist_ok=True)
        # p15b-fix: add random suffix to avoid filename collision when two
        # schedule_task calls land within the same millisecond.
        uid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        fname = tasks_dir / f"{int(time.time()*1000)}-{uid}.json"
        payload = json.dumps({
            "type": "schedule_task",
            "prompt": prompt,
            "schedule_type": schedule_type,   # "cron", "interval", 或 "once"
            "schedule_value": schedule_value,  # cron 表達式、毫秒數、或 ISO 時間字串
            "context_mode": context_mode,      # "group" 或 "isolated"
            "chatJid": chat_jid,              # 群組 JID，讓 ipc_watcher 存入 DB 供排程器路由使用
        })
        _atomic_ipc_write(fname, payload)
        _log("📨 IPC", f"type=schedule_task → {fname.name}")
        return "Task scheduled"
    except Exception as e:
        return f"Error: {e}"


def tool_list_tasks() -> str:
    """
    回傳此群組的排程任務清單（由 host 在啟動時透過 stdin 傳入）。
    讓 agent 可以看到目前有哪些排程任務及其 ID。
    """
    tasks = _input_data.get("scheduledTasks", [])
    if not tasks:
        return "No scheduled tasks found."
    return json.dumps(tasks, ensure_ascii=False, indent=2)


def tool_cancel_task(task_id: str) -> str:
    """
    透過 IPC 機制取消（刪除）指定 ID 的排程任務。
    寫入 JSON 檔案到 tasks/ 子目錄，host 的 ipc_watcher 讀取後呼叫 db.delete_task。
    原子寫入：先寫入 .tmp 再 rename，防止 host 讀到半寫的 JSON。
    """
    if not task_id:
        return "Error: task_id is required."
    try:
        _write_ipc_task("cancel_task", {"task_id": task_id})
        return f"Task {task_id} cancellation request sent."
    except Exception as e:
        return f"Error: {e}"


def tool_pause_task(task_id: str) -> str:
    """透過 IPC 暫停指定 ID 的排程任務（status 改為 paused）。
    原子寫入：先寫入 .tmp 再 rename，防止 host 讀到半寫的 JSON。
    """
    if not task_id:
        return "Error: task_id is required."
    try:
        _write_ipc_task("pause_task", {"task_id": task_id})
        return f"Task {task_id} pause request sent."
    except Exception as e:
        return f"Error: {e}"


def tool_resume_task(task_id: str) -> str:
    """透過 IPC 恢復指定 ID 的已暫停排程任務（status 改回 active）。
    原子寫入：先寫入 .tmp 再 rename，防止 host 讀到半寫的 JSON。
    """
    if not task_id:
        return "Error: task_id is required."
    try:
        _write_ipc_task("resume_task", {"task_id": task_id})
        return f"Task {task_id} resume request sent."
    except Exception as e:
        return f"Error: {e}"


def tool_run_agent(prompt: str, context_mode: str = "isolated") -> str:
    """
    在獨立 Docker container 中執行子 agent，等待結果後回傳。
    這是同步阻塞呼叫，父 agent 會等待子 agent 完成（最多 300 秒）。
    context_mode: "isolated"（全新對話，無歷史）或 "group"（帶群組對話歷史）
    """
    import uuid as _uuid
    try:
        request_id = str(_uuid.uuid4())

        Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
        Path(IPC_RESULTS_DIR).mkdir(parents=True, exist_ok=True)

        # BUG-P18D-09: add random suffix (same pattern as schedule_task/cancel_task)
        # to prevent filename collision when two tool_run_agent calls land within
        # the same millisecond, which would silently overwrite each other's request.
        _spawn_uid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}-{_spawn_uid}-spawn.json"
        _atomic_ipc_write(fname, json.dumps({
            "type": "spawn_agent",
            "requestId": request_id,
            "prompt": prompt,
            "context_mode": context_mode,
        }))

        # Poll for result — reduced from 300s to 60s to free the parent group
        # faster (STABILITY_ANALYSIS 5.3).
        _SUBAGENT_TIMEOUT_S = 60  # was 300
        output_path = Path(IPC_RESULTS_DIR) / f"{request_id}.json"
        for _poll_i in range(_SUBAGENT_TIMEOUT_S):
            if output_path.exists():
                try:
                    data = json.loads(output_path.read_text(encoding="utf-8"))
                    output_path.unlink(missing_ok=True)
                    return data.get("output", "(no output)")
                except Exception as e:
                    return f"Error reading subagent result: {e}"
            time.sleep(1)
            # Every 10s, log progress so we know the subagent is not hung
            if _poll_i > 0 and _poll_i % 10 == 0:
                _log("⏳ SUBAGENT", f"subagent {request_id}: still waiting ({_poll_i}s elapsed)...")

        return f"Error: subagent timed out after {_SUBAGENT_TIMEOUT_S}s"
    except Exception as e:
        return f"Error spawning subagent: {e}"


def tool_send_file(chat_jid: str = "", file_path: str = "", caption: str = "") -> str:
    """Send a file to a chat. file_path must be an absolute path inside the container
    (e.g., /workspace/group/output/report.pptx). The file must have been written
    to /workspace/group/output/ first (create the directory with os.makedirs if needed)
    so it maps to the host filesystem via Docker volume mount."""
    global _input_chat_jid
    # Auto-detect chat_jid from input if not explicitly provided by the LLM
    effective_jid = chat_jid or _input_chat_jid or ""
    if not effective_jid:
        return "Error: chat_jid not provided and not available from input"
    if not file_path:
        return "Error: file_path is required"

    # BUG-P18D-10: sandbox-check the file_path so the LLM cannot send
    # /etc/passwd, /proc/self/environ, or other host-sensitive files to the
    # user's chat.  Both the raw-path check and the symlink-resolved check are
    # required (same defence-in-depth as tool_read).
    _sf_path_err = _check_path_allowed(file_path)
    if _sf_path_err:
        return _sf_path_err
    try:
        _sf_resolved = str(Path(file_path).resolve())
    except Exception as _sf_rp_exc:
        return f"Error: cannot resolve file path {file_path!r}: {_sf_rp_exc}"
    if not any(_sf_resolved.startswith(pfx) for pfx in _ALLOWED_PATH_PREFIXES):
        _log("⚠️ SECURITY", f"SendFile: symlink escape blocked: {file_path!r} -> {_sf_resolved!r}")
        return (
            f"Error: access denied — {file_path!r} resolves to {_sf_resolved!r} "
            f"which is outside the allowed workspace (symlink escape prevention)."
        )

    # Ensure the parent directory of the file exists (common failure point)
    parent = Path(file_path).parent
    if str(parent) != file_path:  # guard against root path edge case
        parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "type": "send_file",
        "chatJid": effective_jid,
        "filePath": file_path,
        "caption": caption,
    }
    Path(IPC_MESSAGES_DIR).mkdir(parents=True, exist_ok=True)
    msg_file = Path(IPC_MESSAGES_DIR) / f"file_{int(time.time()*1000)}_{os.getpid()}.json"
    _atomic_ipc_write(msg_file, json.dumps(payload, ensure_ascii=False))
    _log("📎 FILE", f"path={file_path} exists={os.path.exists(file_path)}")
    _log("📨 IPC", f"type=send_file → {msg_file.name}")
    return f"✅ File queued: {os.path.basename(file_path)}"


def tool_start_remote_control(chat_jid: str = "", sender: str = "") -> str:
    """Request the host to start a Claude Code remote-control session.
    The host will spawn `claude remote-control` in the EvoClaw directory and
    send the resulting https://claude.ai/code... URL back to this chat."""
    global _input_chat_jid
    effective_jid = chat_jid or _input_chat_jid or ""
    if not effective_jid:
        return "Error: chat_jid not provided and not available from input"
    try:
        Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
        uid = str(uuid.uuid4())[:8]
        fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}-remote-control-{uid}.json"
        _atomic_ipc_write(fname, json.dumps({
            "type": "start_remote_control",
            "jid": effective_jid,
            "sender": sender,
        }))
        _log("📨 IPC", f"type=start_remote_control jid={effective_jid} → {fname.name}")
        return "Remote control session requested — URL will be sent to this chat shortly (up to 30s)."
    except Exception as exc:
        return f"Error: {exc}"


def tool_self_update(chat_jid: str = "") -> str:
    """Request the host to pull the latest EvoClaw code from git and restart.
    The host will run `git pull` + `pip install -e .` then restart via os.execv()."""
    global _input_chat_jid
    effective_jid = chat_jid or _input_chat_jid or ""
    try:
        Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
        uid = str(uuid.uuid4())[:8]
        fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}-self-update-{uid}.json"
        _atomic_ipc_write(fname, json.dumps({
            "type": "self_update",
            "jid": effective_jid,
        }))
        _log("📨 IPC", f"type=self_update jid={effective_jid} → {fname.name}")
        return "Self-update requested — EvoClaw will pull latest code and restart shortly."
    except Exception as exc:
        return f"Error: {exc}"


def tool_glob(pattern: str, path: str = WORKSPACE) -> str:
    """
    在指定目錄下尋找符合 glob 模式的檔案（支援 ** 遞迴搜尋）。
    例如 pattern="**/*.py" 可找出所有 Python 檔案。
    結果最多回傳 1000 個，超過部分截斷並警告。

    P14D-GLOB-1: A ``**`` pattern on a very deep directory tree (e.g. a
    node_modules tree with hundreds of thousands of files) can run for many
    seconds and block the entire agent loop.  We run the glob in a background
    thread and enforce a 30-second wall-clock timeout via threading.Event so
    the agent is not blocked indefinitely.

    BUG-P18D-06: validate the path argument against the allowed workspace so
    the LLM cannot pass path="/" to enumerate the full container filesystem.
    """
    _GLOB_MAX_RESULTS = 1000
    _GLOB_TIMEOUT_SECS = 30

    # Validate path is inside the allowed workspace
    _glob_path_err = _check_path_allowed(path)
    if _glob_path_err:
        return _glob_path_err
    try:
        _resolved_glob_path = str(Path(path).resolve())
    except Exception as _rgp_exc:
        return f"Error: cannot resolve path {path!r}: {_rgp_exc}"
    if not any(_resolved_glob_path.startswith(pfx) for pfx in _ALLOWED_PATH_PREFIXES):
        _log("⚠️ SECURITY", f"Glob: path escape blocked: {path!r} -> {_resolved_glob_path!r}")
        return f"Error: access denied — path {path!r} is outside the allowed workspace"

    import threading as _threading

    # BUG-P32-01 FIX (MEDIUM): Validate the pattern argument for path traversal.
    # os.path.join(path, pattern) with pattern="../../../etc/passwd" produces a
    # path that resolves outside /workspace/, bypassing the _check_path_allowed
    # sandbox check that only validated the `path` argument.  The pattern is not
    # a filesystem path (it may contain * and ** wildcards), so we cannot resolve
    # it directly; instead we reject any pattern that contains ".." segments which
    # are the only mechanism for escaping the search root.
    if ".." in pattern.split(os.sep) or ".." in pattern.replace("\\", "/").split("/"):
        _log("⚠️ SECURITY", f"Glob: path traversal in pattern blocked: {pattern!r}")
        return "Error: access denied — pattern must not contain '..' path traversal segments"

    _result_holder: list = []
    _exc_holder: list = []

    def _do_glob() -> None:
        try:
            search_path = os.path.join(path, pattern)
            raw_matches = _glob_module.glob(search_path, recursive=True)
            # BUG-P32-01 FIX: Filter matches to ensure all returned paths are
            # inside the allowed workspace.  This is a defence-in-depth check
            # so that even if the pattern check above is somehow bypassed the
            # agent never receives paths for files outside /workspace/.
            safe_matches = [
                m for m in raw_matches
                if any(os.path.realpath(m).startswith(pfx) for pfx in _ALLOWED_PATH_PREFIXES)
            ]
            _result_holder.append(safe_matches)
        except Exception as exc:
            _exc_holder.append(exc)

    t = _threading.Thread(target=_do_glob, daemon=True)
    t.start()
    t.join(timeout=_GLOB_TIMEOUT_SECS)

    if t.is_alive():
        # Thread is still running — glob timed out
        _log("⚠️ GLOB-TIMEOUT", f"glob pattern={pattern!r} path={path!r} timed out after {_GLOB_TIMEOUT_SECS}s")
        return (
            f"Error: glob timed out after {_GLOB_TIMEOUT_SECS}s — "
            "the pattern matched too many files or the directory tree is too deep. "
            "Narrow the pattern or reduce the search path."
        )

    if _exc_holder:
        return f"Error: {_exc_holder[0]}"

    matches = _result_holder[0] if _result_holder else []
    if not matches:
        return f"No files found matching: {pattern} in {path}"
    matches_sorted = sorted(matches)
    if len(matches_sorted) > _GLOB_MAX_RESULTS:
        truncated = len(matches_sorted) - _GLOB_MAX_RESULTS
        return "\n".join(matches_sorted[:_GLOB_MAX_RESULTS]) + f"\n... ({truncated} more results not shown — refine your pattern)"
    return "\n".join(matches_sorted)


def tool_grep(pattern: str, path: str = WORKSPACE, include: str = "*") -> str:
    """
    在指定目錄下遞迴搜尋符合正規表達式的檔案內容，回傳「檔名:行號:內容」格式。
    include 參數可過濾副檔名，例如 include="*.py" 只搜尋 Python 檔案。

    BUG-P18D-04/05: validate the path argument against the allowed workspace
    so the LLM cannot pass path="/" or path="/etc" to grep arbitrary filesystem
    locations.  The include and pattern arguments are passed as separate argv
    elements (shell=False), so they cannot inject shell commands, but path is
    the search root and must be sandbox-checked.
    """
    # Validate path is inside the allowed workspace
    _path_err = _check_path_allowed(path)
    if _path_err:
        return _path_err
    # Re-check after resolving symlinks (path itself could be a symlink to /etc)
    try:
        _resolved_grep_path = str(Path(path).resolve())
    except Exception as _rg_exc:
        return f"Error: cannot resolve path {path!r}: {_rg_exc}"
    if not any(_resolved_grep_path.startswith(pfx) for pfx in _ALLOWED_PATH_PREFIXES):
        _log("⚠️ SECURITY", f"Grep: path escape blocked: {path!r} -> {_resolved_grep_path!r}")
        return f"Error: access denied — path {path!r} is outside the allowed workspace"
    # Validate include is a plain glob pattern (no path separators)
    if "/" in include or "\\" in include or ".." in include:
        return "Error: include parameter must be a plain filename glob (e.g. '*.py'), not a path"
    try:
        result = subprocess.run(
            ["grep", "-r", "-n", "--include", include, pattern, path],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        output = result.stdout
        if result.stderr and not output:
            output = result.stderr
        if len(output) > 8000:
            output = output[:8000] + "\n... (truncated, too many matches)"
        return output or "(no matches found)"
    except subprocess.TimeoutExpired:
        return "Error: grep timed out after 30s"
    except Exception as e:
        return f"Error: {e}"


class _HTMLTextExtractor(html.parser.HTMLParser):
    """簡單的 HTML 純文字提取器，過濾掉所有 HTML 標籤。"""
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False
        if tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def tool_web_fetch(url: str) -> str:
    """
    從指定 URL 抓取網頁內容，自動將 HTML 轉換為純文字。
    適合查閱文件、新聞、GitHub README 等網頁資料。
    結果最多回傳 50KB（文字），超過部分截斷。

    安全限制：
    - SSRF 防護：封鎖私有/迴環/雲端 metadata IP 範圍
    - 回應大小限制：最多讀取 2MB raw bytes（防止超大下載）
    - 二進位內容偵測：拒絕非文字回應
    - 重導向限制：最多追蹤 5 次重導向
    - HTTP timeout：30 秒
    """
    import socket as _socket
    import ipaddress as _ipaddress
    import re as _re_url

    _WEB_FETCH_TEXT_LIMIT = 50 * 1024    # 50 KB returned to LLM
    _WEB_FETCH_RAW_LIMIT  = 2 * 1024 * 1024  # 2 MB raw download cap

    # BUG-P18D-08: validate url type before passing to urlparse to prevent
    # AttributeError / TypeError when the LLM passes a non-string.
    if not isinstance(url, str):
        return f"Error: url must be a string, got {type(url).__name__}"

    # ── SSRF prevention ───────────────────────────────────────────────────────
    # Parse the URL and resolve the hostname to an IP, then reject private ranges.
    _BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}
    _BLOCKED_HOST_RE = _re_url.compile(
        r'^(?:169\.254\.|metadata\.google\.internal|instance-data)',
        _re_url.IGNORECASE,
    )

    def _is_ssrf_target(hostname: str) -> bool:
        """Return True if the hostname resolves to a private/reserved address."""
        if not hostname:
            return True
        hostname_lower = hostname.lower()
        if hostname_lower in _BLOCKED_HOSTS:
            return True
        if _BLOCKED_HOST_RE.match(hostname_lower):
            return True
        try:
            # Resolve all A/AAAA records and check each
            infos = _socket.getaddrinfo(hostname, None)
            for info in infos:
                ip_str = info[4][0]
                try:
                    ip = _ipaddress.ip_address(ip_str)
                    if (ip.is_private or ip.is_loopback or ip.is_link_local or
                            ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                        return True
                    # Explicitly block 169.254.x.x (AWS/GCP/Azure metadata)
                    if ip_str.startswith("169.254."):
                        return True
                except ValueError:
                    pass
        except Exception:
            # DNS resolution failed — treat as safe (will fail at fetch time)
            pass
        return False

    # Parse URL to extract hostname
    try:
        import urllib.parse as _urlparse
        parsed = _urlparse.urlparse(url)
        _scheme = parsed.scheme.lower()
        if _scheme not in ("http", "https"):
            return f"Error: unsupported URL scheme '{_scheme}' — only http/https are allowed"
        _hostname = parsed.hostname or ""
    except Exception as _parse_err:
        return f"Error: invalid URL: {_parse_err}"

    if _is_ssrf_target(_hostname):
        _log("🚨 SECURITY", f"WebFetch: SSRF blocked — host {_hostname!r} resolves to private/reserved address")
        return f"Error: access denied — URL targets a private or reserved address (SSRF protection)"

    # ── Fetch with redirect limit ─────────────────────────────────────────────
    try:
        # Build an opener that limits redirects (default urllib follows up to 10)
        _redirect_count = 0
        _MAX_REDIRECTS = 5

        class _LimitedRedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                nonlocal _redirect_count
                _redirect_count += 1
                if _redirect_count > _MAX_REDIRECTS:
                    raise urllib.error.URLError(f"Too many redirects (> {_MAX_REDIRECTS})")
                # SSRF-check the redirect target too
                # BUG-P29A-1 FIX (HIGH): the previous bare `except Exception: pass`
                # silently swallowed any non-URLError exception raised during URL
                # parsing (e.g. AttributeError, ValueError from a malformed newurl).
                # This left the SSRF check bypassed — the redirect was allowed to
                # proceed to an unchecked destination.  Fix: treat ANY exception
                # during the redirect SSRF check as a reason to block the redirect.
                # Unknown/unparseable redirect targets are unsafe by default.
                try:
                    _rp = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(newurl)
                    _rh = _rp.hostname or ""
                    if _is_ssrf_target(_rh):
                        raise urllib.error.URLError(f"Redirect to private address blocked (SSRF): {_rh}")
                except urllib.error.URLError:
                    raise
                except Exception as _redir_exc:
                    # Treat unparseable/unverifiable redirect target as blocked (fail-safe).
                    raise urllib.error.URLError(
                        f"Redirect to unverifiable target blocked (SSRF fail-safe): {_redir_exc}"
                    )
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        # BUG-P18D-07: DNS rebinding TOCTOU mitigation.  The pre-flight
        # _is_ssrf_target() call resolves the hostname at check-time, but the
        # actual TCP connection is made later by urllib.  A DNS rebinding attack
        # can return a public IP at check-time, then switch to a private IP at
        # connect-time to bypass the SSRF filter.
        # Mitigation: monkey-patch socket.create_connection to re-validate the
        # resolved IP address at the moment the TCP socket is opened.  This is
        # the only reliable defence without external libraries (e.g. pycurl).
        import socket as _socket_rebind
        _orig_create_conn = _socket_rebind.create_connection

        def _safe_create_connection(address, *_args, **_kwargs):
            _conn_host, _conn_port = address[0], address[1] if len(address) > 1 else None
            try:
                _conn_ip = _socket_rebind.getaddrinfo(_conn_host, _conn_port)[0][4][0]
                _conn_ip_obj = _ipaddress.ip_address(_conn_ip)
                if (_conn_ip_obj.is_private or _conn_ip_obj.is_loopback or
                        _conn_ip_obj.is_link_local or _conn_ip_obj.is_reserved or
                        _conn_ip_obj.is_multicast or _conn_ip_obj.is_unspecified):
                    raise urllib.error.URLError(
                        f"SSRF: connection to private IP {_conn_ip!r} blocked (DNS rebinding protection)"
                    )
            except urllib.error.URLError:
                raise
            except Exception:
                pass  # DNS error at connect time — let urllib surface it naturally
            return _orig_create_conn(address, *_args, **_kwargs)

        _opener = urllib.request.build_opener(_LimitedRedirectHandler)

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; EvoClaw-Agent/1.0)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            }
        )
        # BUG-P24A-1: the previous code patched socket.create_connection only
        # during build_opener() (which makes no network connections) and then
        # restored the original before _opener.open() was called.  The DNS
        # rebinding protection was therefore never active during the actual TCP
        # connection, making it completely ineffective.  Fix: keep the patched
        # socket.create_connection active for the duration of _opener.open() so
        # every TCP connection made while fetching the URL is validated.
        _socket_rebind.create_connection = _safe_create_connection
        try:
            _fetch_ctx = _opener.open(req, timeout=30)
        except Exception:
            _socket_rebind.create_connection = _orig_create_conn
            raise
        _socket_rebind.create_connection = _orig_create_conn
        with _fetch_ctx as resp:
            content_type = resp.headers.get("Content-Type", "")

            # ── Binary content detection ──────────────────────────────────────
            _ct_lower = content_type.lower()
            _BINARY_TYPES = (
                "application/octet-stream", "application/zip", "application/gzip",
                "application/x-tar", "application/pdf", "application/msword",
                "application/vnd.", "image/", "audio/", "video/",
            )
            if any(_ct_lower.startswith(bt) for bt in _BINARY_TYPES):
                return f"Error: binary content type '{content_type}' — WebFetch only supports text content"

            # ── Cap raw download size ─────────────────────────────────────────
            raw_bytes = resp.read(_WEB_FETCH_RAW_LIMIT)
            # Check for binary bytes in first 512 bytes (null bytes etc.)
            _sample = raw_bytes[:512]
            if b"\x00" in _sample or (len(_sample) > 0 and sum(b > 127 for b in _sample) > len(_sample) * 0.3):
                return "Error: response appears to be binary content — WebFetch only supports text"

        # P14D-WF-CHARSET: extract charset from Content-Type header and use it
        # for decoding, falling back to UTF-8.  Servers frequently return pages
        # encoded in Latin-1 or Windows-1252 with the charset declared in the
        # header (e.g. "text/html; charset=iso-8859-1").  Ignoring the declared
        # charset and always decoding as UTF-8 produces replacement characters
        # that corrupt the extracted text.
        import re as _re_ct
        _charset = "utf-8"
        _ct_charset_match = _re_ct.search(r'charset=["\']?([A-Za-z0-9_\-]+)', content_type, _re_ct.IGNORECASE)
        if _ct_charset_match:
            _declared = _ct_charset_match.group(1).lower()
            # Normalise common aliases
            _charset = _declared if _declared else "utf-8"
        try:
            raw = raw_bytes.decode(_charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            # Unknown charset name — fall back to UTF-8
            raw = raw_bytes.decode("utf-8", errors="replace")

        if "html" in _ct_lower or raw.lstrip().startswith("<"):
            extractor = _HTMLTextExtractor()
            try:
                extractor.feed(raw)
                text = extractor.get_text()
            except Exception:
                text = raw
            lines = [l.strip() for l in text.splitlines()]
            text = "\n".join(l for l in lines if l)
        else:
            text = raw

        if len(text) > _WEB_FETCH_TEXT_LIMIT:
            text = text[:_WEB_FETCH_TEXT_LIMIT] + "\n\n... (content truncated at 50KB)"
        return text or "(empty response)"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} error fetching {url}: {e.reason}"
    except urllib.error.URLError as e:
        return f"URL error fetching {url}: {e.reason}"
    except Exception as e:
        return f"Error fetching {url}: {e}"


# ── Dynamic tool registry ─────────────────────────────────────────────────────
# Skills 安裝的 container_tools/*.py 在 container 啟動時從 /app/dynamic_tools/ 動態載入
# 每個工具以 register_dynamic_tool() 自我註冊，不需重建 Docker image

_dynamic_tools: dict[str, dict] = {}  # name → {"fn": callable, "schema": dict, "description": str}


def _json_schema_to_gemini(props: dict, required: list):
    """將 JSON Schema properties 轉換為 Gemini types.Schema（僅支援常用型別）。"""
    if not _GOOGLE_AVAILABLE or types is None:
        return None
    gemini_props = {}
    for pname, pdef in props.items():
        ptype_str = pdef.get("type", "string").upper()
        ptype = getattr(types.Type, ptype_str, types.Type.STRING)
        gemini_props[pname] = types.Schema(
            type=ptype,
            description=pdef.get("description", ""),
        )
    return types.Schema(
        type=types.Type.OBJECT,
        properties=gemini_props,
        required=required or [],
    )


def register_dynamic_tool(name: str, description: str, schema: dict, fn) -> None:
    """
    動態注冊工具到所有 provider 宣告列表（Gemini / Claude / OpenAI）。
    由 /app/dynamic_tools/*.py 模組在 import 時呼叫。
    schema 使用 JSON Schema 格式（OpenAI/Claude 相容）。
    """
    _dynamic_tools[name] = {"fn": fn, "description": description, "schema": schema}
    props = schema.get("properties", {})
    req = schema.get("required", [])

    # Gemini FunctionDeclaration
    if _GOOGLE_AVAILABLE and types is not None:
        try:
            gemini_params = _json_schema_to_gemini(props, req)
            if gemini_params:
                TOOL_DECLARATIONS.append(
                    types.FunctionDeclaration(name=name, description=description, parameters=gemini_params)
                )
        except Exception:
            pass

    # Claude (Anthropic) tool declaration
    CLAUDE_TOOL_DECLARATIONS.append({
        "name": name,
        "description": description,
        "input_schema": schema,
    })

    # OpenAI-compatible tool declaration
    OPENAI_TOOL_DECLARATIONS.append({
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    })

    _log("🔌 DYNAMIC", f"registered tool: {name}")


def _load_dynamic_tools() -> None:
    """
    自動 import /app/dynamic_tools/ 中的所有 .py 工具模組。
    每個模組應在 module level 呼叫 register_dynamic_tool()。
    這讓 DevEngine 生成的 Skill container_tools 不需重建 image 即可使用。
    """
    import importlib.util
    dynamic_dir = Path("/app/dynamic_tools")
    if not dynamic_dir.exists():
        return
    for py_file in sorted(dynamic_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"dynamic_tools.{py_file.stem}", py_file
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                # 將 register_dynamic_tool 注入模組命名空間，讓工具可直接呼叫
                mod.register_dynamic_tool = register_dynamic_tool  # type: ignore[attr-defined]
                spec.loader.exec_module(mod)
                _log("🔌 DYNAMIC TOOL", f"loaded {py_file.name}")
        except Exception as exc:
            _log("⚠️ DYNAMIC TOOL", f"failed to load {py_file.name}: {exc}")


# ── Tool registry ─────────────────────────────────────────────────────────────

# 向 Gemini function calling API 宣告可用的工具
# Gemini 根據這些宣告決定何時呼叫哪個工具（function call）
# BUG-FIX: Guard with _GOOGLE_AVAILABLE so that importing this module when only
# the OpenAI or Claude backend is installed does not raise AttributeError on
# types.FunctionDeclaration (types is None when google-genai is absent).
TOOL_DECLARATIONS = [] if not _GOOGLE_AVAILABLE or types is None else [
    types.FunctionDeclaration(
        name="Bash",
        description="Execute a bash command in /workspace/group.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"command": types.Schema(type=types.Type.STRING, description="The bash command to run")},
            required=["command"],
        ),
    ),
    types.FunctionDeclaration(
        name="Read",
        description="Read a file from the filesystem.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"file_path": types.Schema(type=types.Type.STRING, description="Absolute path to the file")},
            required=["file_path"],
        ),
    ),
    types.FunctionDeclaration(
        name="Write",
        description="Write content to a file (creates parent dirs if needed).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "file_path": types.Schema(type=types.Type.STRING, description="Absolute path to write to"),
                "content": types.Schema(type=types.Type.STRING, description="File content"),
            },
            required=["file_path", "content"],
        ),
    ),
    types.FunctionDeclaration(
        name="Edit",
        description="Find and replace a string in a file.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "file_path": types.Schema(type=types.Type.STRING, description="Path to the file"),
                "old_string": types.Schema(type=types.Type.STRING, description="Exact text to replace"),
                "new_string": types.Schema(type=types.Type.STRING, description="Replacement text"),
            },
            required=["file_path", "old_string", "new_string"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__send_message",
        description="Send a message to the user in the chat.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "text": types.Schema(type=types.Type.STRING, description="Message text"),
                "sender": types.Schema(type=types.Type.STRING, description="Optional bot name"),
            },
            required=["text"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__schedule_task",
        description="Schedule a recurring or one-time task.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "prompt": types.Schema(type=types.Type.STRING, description="What to do when task runs"),
                "schedule_type": types.Schema(type=types.Type.STRING, description="cron, interval, or once"),
                "schedule_value": types.Schema(type=types.Type.STRING, description="Cron expr, ms, or ISO timestamp"),
                "context_mode": types.Schema(type=types.Type.STRING, description="group or isolated"),
            },
            required=["prompt", "schedule_type", "schedule_value"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__list_tasks",
        description="List all scheduled tasks for this group.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__cancel_task",
        description="Cancel (delete) a scheduled task by its ID.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "task_id": types.Schema(type=types.Type.STRING, description="The task ID to cancel"),
            },
            required=["task_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__pause_task",
        description="Pause a scheduled task (it will not run until resumed).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "task_id": types.Schema(type=types.Type.STRING, description="The task ID to pause"),
            },
            required=["task_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__resume_task",
        description="Resume a previously paused scheduled task.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "task_id": types.Schema(type=types.Type.STRING, description="The task ID to resume"),
            },
            required=["task_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="Glob",
        description="Find files matching a glob pattern (supports ** for recursive search).",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "pattern": types.Schema(type=types.Type.STRING, description="Glob pattern, e.g. '**/*.py'"),
                "path": types.Schema(type=types.Type.STRING, description="Base directory (default: /workspace/group)"),
            },
            required=["pattern"],
        ),
    ),
    types.FunctionDeclaration(
        name="Grep",
        description="Search file contents using regex. Returns filename:line:content for each match.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "pattern": types.Schema(type=types.Type.STRING, description="Regex pattern to search for"),
                "path": types.Schema(type=types.Type.STRING, description="Directory to search (default: /workspace/group)"),
                "include": types.Schema(type=types.Type.STRING, description="File filter e.g. '*.py' (default: all files)"),
            },
            required=["pattern"],
        ),
    ),
    types.FunctionDeclaration(
        name="WebFetch",
        description="Fetch content from a URL and return it as plain text. Useful for reading docs, news, GitHub READMEs.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "url": types.Schema(type=types.Type.STRING, description="The URL to fetch"),
            },
            required=["url"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__run_agent",
        description="Spawn a subagent in an isolated Docker container to handle a subtask. Blocks until the subagent completes (up to 300s) and returns its output.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "prompt": types.Schema(type=types.Type.STRING, description="The task for the subagent to execute"),
                "context_mode": types.Schema(type=types.Type.STRING, description="isolated (no history, default) or group (with conversation history)"),
            },
            required=["prompt"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__send_file",
        description="Send a file to the user. Write the file to /workspace/group/output/ first, then call this tool with the absolute container path.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "chat_jid": types.Schema(type=types.Type.STRING, description="The chat JID to send the file to"),
                "file_path": types.Schema(type=types.Type.STRING, description="Absolute container path to the file, e.g. /workspace/group/output/report.pptx"),
                "caption": types.Schema(type=types.Type.STRING, description="Optional caption for the file"),
            },
            required=["file_path"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__reset_group",
        description="Clear the failure counter for a group, unfreezing it if it was locked in cooldown. Use this when a group is stuck and not responding. Only callable from monitor group.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "jid": types.Schema(type=types.Type.STRING, description="The JID of the group to reset, e.g. tg:8259652816"),
            },
            required=["jid"],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__start_remote_control",
        description="Start a Claude Code remote-control session on the host. The host spawns `claude remote-control` in the EvoClaw directory and sends the resulting URL back to this chat. Use when the user wants to update code, restart EvoClaw, or open a live coding session.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "chat_jid": types.Schema(type=types.Type.STRING, description="The chat JID to send the URL to (auto-detected if omitted)"),
                "sender": types.Schema(type=types.Type.STRING, description="Optional sender name for logging"),
            },
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="mcp__evoclaw__self_update",
        description="Pull the latest EvoClaw code from git and restart the host process. Use when the user asks to update, upgrade, or restart EvoClaw.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "chat_jid": types.Schema(type=types.Type.STRING, description="The chat JID to notify when update is done (auto-detected if omitted)"),
            },
            required=[],
        ),
    ),
]



# ── OpenAI-compatible tool declarations ───────────────────────────────────────

OPENAI_TOOL_DECLARATIONS = [
    {"type": "function", "function": {"name": "Bash", "description": "Execute a bash command in /workspace/group.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to run"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read", "description": "Read a file from the filesystem.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to the file"}}, "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "Write", "description": "Write content to a file.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to write to"}, "content": {"type": "string", "description": "File content"}}, "required": ["file_path", "content"]}}},
    {"type": "function", "function": {"name": "Edit", "description": "Find and replace a string in a file.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Path to the file"}, "old_string": {"type": "string", "description": "Exact text to replace"}, "new_string": {"type": "string", "description": "Replacement text"}}, "required": ["file_path", "old_string", "new_string"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__send_message", "description": "Send a message to the user in the chat.", "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "Message text"}, "sender": {"type": "string", "description": "Optional bot name"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__schedule_task", "description": "Schedule a recurring or one-time task.", "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "What to do when task runs"}, "schedule_type": {"type": "string", "description": "cron, interval, or once"}, "schedule_value": {"type": "string", "description": "Cron expr, ms, or ISO timestamp"}, "context_mode": {"type": "string", "description": "group or isolated"}}, "required": ["prompt", "schedule_type", "schedule_value"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__list_tasks", "description": "List all scheduled tasks for this group.", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__cancel_task", "description": "Cancel (delete) a scheduled task by its ID.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to cancel"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__pause_task", "description": "Pause a scheduled task (it will not run until resumed).", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to pause"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__resume_task", "description": "Resume a previously paused scheduled task.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to resume"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "Glob", "description": "Find files matching a glob pattern (supports ** recursive).", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "Grep", "description": "Search file contents with regex. Returns filename:line:content.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "WebFetch", "description": "Fetch a URL and return its content as plain text.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__run_agent", "description": "Spawn a subagent in an isolated Docker container to handle a subtask. Blocks until complete (up to 300s) and returns its output.", "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "The task for the subagent"}, "context_mode": {"type": "string", "description": "isolated or group"}}, "required": ["prompt"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__send_file", "description": "Send a file to the user. Write the file to /workspace/group/output/ first, then call this tool.", "parameters": {"type": "object", "properties": {"chat_jid": {"type": "string", "description": "The chat JID to send the file to"}, "file_path": {"type": "string", "description": "Absolute container path to the file"}, "caption": {"type": "string", "description": "Optional caption"}}, "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__reset_group", "description": "Clear the failure counter for a group, unfreezing it if it was locked in cooldown. Use when a group is stuck and not responding.", "parameters": {"type": "object", "properties": {"jid": {"type": "string", "description": "The JID of the group to reset, e.g. tg:8259652816"}}, "required": ["jid"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__start_remote_control", "description": "Start a Claude Code remote-control session. The host spawns `claude remote-control` and sends the URL back to this chat. Use when the user wants to update code or restart EvoClaw.", "parameters": {"type": "object", "properties": {"chat_jid": {"type": "string"}, "sender": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__self_update", "description": "Pull the latest EvoClaw code from git and restart the host process. Use when the user asks to update, upgrade, or restart EvoClaw.", "parameters": {"type": "object", "properties": {"chat_jid": {"type": "string"}}, "required": []}}},
]


# 追蹤 agent 是否在 agentic loop 中已呼叫過 send_message 工具
# 每次 Docker 啟動都是全新 process，此 flag 只有一次生命週期
# 用途：避免 host 讀取 result 欄位時重複發送（雙重訊息 bug）
_messages_sent_via_tool: list = []

# stdin 解析後的完整輸入資料，main() 初始化後供工具函式存取
_input_data: dict = {}


# Claude (Anthropic) tool declarations
CLAUDE_TOOL_DECLARATIONS = [
    {"name": "Bash", "description": "Execute a bash command in /workspace/group.", "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to run"}}, "required": ["command"]}},
    {"name": "Read", "description": "Read a file from the filesystem.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to the file"}}, "required": ["file_path"]}},
    {"name": "Write", "description": "Write content to a file.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}},
    {"name": "Edit", "description": "Find and replace a string in a file.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["file_path", "old_string", "new_string"]}},
    {"name": "mcp__evoclaw__send_message", "description": "Send a message to the user.", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "sender": {"type": "string"}}, "required": ["text"]}},
    {"name": "mcp__evoclaw__schedule_task", "description": "Schedule a task.", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "schedule_type": {"type": "string"}, "schedule_value": {"type": "string"}, "context_mode": {"type": "string"}}, "required": ["prompt", "schedule_type", "schedule_value"]}},
    {"name": "mcp__evoclaw__list_tasks", "description": "List all scheduled tasks for this group.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "mcp__evoclaw__cancel_task", "description": "Cancel (delete) a scheduled task by its ID.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "mcp__evoclaw__pause_task", "description": "Pause a scheduled task (it will not run until resumed).", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to pause"}}, "required": ["task_id"]}},
    {"name": "mcp__evoclaw__resume_task", "description": "Resume a previously paused scheduled task.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to resume"}}, "required": ["task_id"]}},
    {"name": "Glob", "description": "Find files matching a glob pattern (supports ** recursive).", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "Grep", "description": "Search file contents with regex. Returns filename:line:content.", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "WebFetch", "description": "Fetch a URL and return its content as plain text.", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "mcp__evoclaw__run_agent", "description": "Spawn a subagent in an isolated Docker container to handle a subtask. Blocks until complete (up to 300s) and returns its output.", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string", "description": "The task for the subagent"}, "context_mode": {"type": "string", "description": "isolated or group"}}, "required": ["prompt"]}},
    {"name": "mcp__evoclaw__send_file", "description": "Send a file to the user. Write the file to /workspace/group/output/ first, then call this tool.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string", "description": "The chat JID to send the file to"}, "file_path": {"type": "string", "description": "Absolute container path to the file"}, "caption": {"type": "string", "description": "Optional caption"}}, "required": ["file_path"]}},
    {"name": "mcp__evoclaw__reset_group", "description": "Clear the failure counter for a group, unfreezing it if it was locked in cooldown. Use when a group is stuck and not responding.", "input_schema": {"type": "object", "properties": {"jid": {"type": "string", "description": "The JID of the group to reset, e.g. tg:8259652816"}}, "required": ["jid"]}},
    {"name": "mcp__evoclaw__start_remote_control", "description": "Start a Claude Code remote-control session. The host spawns `claude remote-control` and sends the URL back to this chat. Use when the user wants to update code or restart EvoClaw.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string"}, "sender": {"type": "string"}}, "required": []}},
    {"name": "mcp__evoclaw__self_update", "description": "Pull the latest EvoClaw code from git and restart the host process. Use when the user asks to update, upgrade, or restart EvoClaw.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string"}}, "required": []}},
]


def run_agent_claude(client_holder, model: str, system_instruction: str, user_message: str, chat_jid: str, conversation_history: list = None, pool: "_KeyPool | None" = None, apply_key_fn=None, max_iter: int = 20, group_folder: str = "") -> str:
    """
    Anthropic Claude agentic loop.
    client_holder: a one-element list [client] so key rotation can swap the client mid-loop.
    conversation_history: 最近的對話記錄，以原生 multi-turn 格式注入。
    pool/apply_key_fn: optional key pool for automatic rotation on rate-limit errors.
    max_iter: maximum number of agentic loop iterations (default 20; caller sets based on task complexity).
    group_folder: path to the group folder (used for MEMORY.md tracking).
    """
    import re as _re_claude
    messages = []
    # P15A-FIX-8: inject conversation history preserving structured content.
    # The old code used msg.get("content","") which evaluates to "" for list-typed
    # content (tool_use/tool_result blocks), silently dropping those messages.
    # Use the raw content value directly; skip only genuinely empty values.
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str) and not content.strip():
                continue
            if content is not None and content != [] and content != "":
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    MAX_ITER = max_iter
    final_response = ""
    _memory_written = False  # True once agent writes to MEMORY.md this session
    _memory_path_str = f"{WORKSPACE}/MEMORY.md"  # BUG-FIX #424: group_folder is a name, not a path; use WORKSPACE
    _tool_fail_counter: dict = {}  # (tool_name, args_hash) -> consecutive_fail_count
    _MAX_CONSECUTIVE_TOOL_FAILS = 3
    _retry_warning: str = ""  # injected before next LLM call when tool retries detected
    _turns_since_notify = 0   # turns since last mcp__evoclaw__send_message call
    _only_notify_turns = 0    # consecutive turns with ONLY send_message (no real work)
    # Tools that represent actual work (not just reporting)
    _SUBSTANTIVE_TOOLS_CLAUDE = frozenset([
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch",
        "mcp__evoclaw__run_agent",
    ])
    # Extended fake-status regex covering Claude's common hallucination patterns
    # P15A-FIX-1: added English fake-done patterns (previously only in OpenAI loop)
    _FAKE_STATUS_RE = _re_claude.compile(
        r'\*\([^)]*\)\*'           # *(正在執行...)*
        r'|\*\[[^\]]*\]\*'          # *[running...]*
        r'|✅\s*Done'              # ✅ Done
        r'|✅\s*完成'              # ✅ 完成
        r'|【[^】]*(?:已|正在|將|完成|處理|執行)[^】]*】'   # 【已完成】
        r'|（[^）]{2,30}(?:已|正在|處理|執行)[^）]{0,20}）' # （已完成）
        r'|(?:I\s+have\s+(?:completed|finished|executed|run|written))'  # English fake-done
        r'|(?:Task\s+(?:is\s+)?(?:complete|done|finished))'            # Task complete
        r'|(?:Successfully\s+(?:completed|executed|ran|written))',      # Successfully executed
        _re_claude.DOTALL | _re_claude.IGNORECASE,
    )

    for n in range(MAX_ITER):
        _log("🧠 LLM →", f"turn={n} provider=claude")
        _claude_msgs = messages  # capture current snapshot for lambda
        response = _llm_call_with_retry(lambda: client_holder[0].messages.create(
            model=model,
            max_tokens=4096,
            system=system_instruction,
            tools=CLAUDE_TOOL_DECLARATIONS,
            messages=_claude_msgs,
        ), pool=pool, apply_key_fn=apply_key_fn)
        _log("🧠 LLM ←", f"stop={response.stop_reason}")

        # Add assistant response to history
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Collect all text blocks
            # BUG-P26B-1: block.text may be None even when the attribute exists
            # (e.g. a text content block returned with a null value by the API).
            # Joining None would raise TypeError; guard with an explicit None check.
            final_response = " ".join(
                block.text for block in response.content
                if hasattr(block, "text") and block.text is not None
            )
            # ── Fake status detection on end_turn (no tool calls made) ─────────
            _fake_hits = _FAKE_STATUS_RE.findall(final_response)
            if _fake_hits and n < MAX_ITER - 1:
                _log("⚠️ FAKE-STATUS", f"Claude wrote {len(_fake_hits)} fake status indicator(s) without tool calls")
                messages.append({
                    "role": "user",
                    "content": (
                        "【系統警告】你剛才的回覆包含假狀態指示（例如 ✅ Done 或 *(正在執行...)* ），但沒有呼叫任何工具。"
                        "請立刻使用 Bash tool 或其他工具實際執行所需命令，不要只是描述或假裝完成。"
                    ),
                })
                final_response = ""
                continue

            # ── Semantic cross-validation: action claim without any tool call ──
            # If the agent's text claims it completed an action (using common
            # completion verbs) but did NOT call any tools this turn, inject a
            # verification demand.  This catches hallucinations that slip past the
            # syntactic _FAKE_STATUS_RE patterns above.
            _had_tool_calls_this_turn = any(
                hasattr(b, "type") and b.type == "tool_use"
                for b in response.content
            )
            if not _had_tool_calls_this_turn and _ACTION_CLAIM_RE.search(final_response) and n < MAX_ITER - 1:
                _log("⚠️ SEMANTIC-FAKE", "Claude claims action complete but called no tools this turn")
                messages.append({
                    "role": "user",
                    "content": (
                        "【系統驗證】你的回應中聲稱已執行了某項操作，但本輪沒有呼叫任何工具。"
                        "請實際使用對應工具（Read/Write/Edit/Bash）執行並確認，不要只是聲明已完成。"
                    ),
                })
                final_response = ""
                continue
            break

        if response.stop_reason != "tool_use":
            # P15A-FIX-2: handle max_tokens gracefully.
            # If Claude hit max_tokens while mid-tool-call, the assistant message
            # (already appended above) may contain tool_use blocks.  If we simply
            # break, the next API call will fail with a 400 because the history has
            # an assistant tool_use without a matching tool_result.  Detect this
            # case and execute any pending tool_use blocks so history stays valid.
            _pending_tool_uses = [b for b in response.content if hasattr(b, "type") and b.type == "tool_use"]
            if _pending_tool_uses and response.stop_reason == "max_tokens":
                _log("⚠️ MAX-TOKENS", f"Claude hit max_tokens with {len(_pending_tool_uses)} pending tool_use block(s) — executing to keep history valid")
                _partial_results = []
                for _tb in _pending_tool_uses:
                    try:
                        _tr = execute_tool(_tb.name, _tb.input, chat_jid)
                    except Exception as _te:
                        _tr = f"[Tool error: {_te}]"
                    _tr_str = str(_tr)
                    if len(_tr_str) > _MAX_TOOL_RESULT_CHARS:
                        _half = _MAX_TOOL_RESULT_CHARS // 2
                        _head = _tr_str[:_half]
                        _tail = _tr_str[-_half:]
                        _omitted = len(_tr_str) - _MAX_TOOL_RESULT_CHARS
                        _tr_str = _head + f"\n[... {_omitted} chars omitted (middle truncated to preserve head+tail) ...]\n" + _tail
                    _partial_results.append({"type": "tool_result", "tool_use_id": _tb.id, "content": _tr_str})
                messages.append({"role": "user", "content": _partial_results})
            # Unexpected / terminal stop reason — collect text and exit
            # BUG-P26B-1: same None guard as in the end_turn branch above.
            final_response = " ".join(
                block.text for block in response.content
                if hasattr(block, "text") and block.text is not None
            )
            _log("⚠️ UNEXPECTED-STOP", f"Claude stop_reason={response.stop_reason} — exiting loop")
            break

        # Execute all tool calls
        tool_results = []
        _tool_names_this_turn: set = set()
        for block in response.content:
            if block.type == "tool_use":
                _tool_names_this_turn.add(block.name)
                try:
                    result = execute_tool(block.name, block.input, chat_jid)
                except Exception as e:
                    result = f"[Tool error: {e}]"
                    _log("❌ TOOL-EXC", f"Tool {block.name} raised exception: {e}")
                # Truncate large tool results before adding to history
                result_str = str(result)
                if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                    half = _MAX_TOOL_RESULT_CHARS // 2
                    head = result_str[:half]
                    tail = result_str[-half:]
                    omitted = len(result_str) - _MAX_TOOL_RESULT_CHARS
                    result_str = head + f"\n[... {omitted} chars omitted (middle truncated to preserve head+tail) ...]\n" + tail
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })
                # Fix 4 (STABILITY_ANALYSIS 3.5): detect repeated identical-args tool failures
                _fail_key = (block.name, hash(str(block.input)[:200]))
                _is_failure = (result_str.startswith("\u2717") or result_str.startswith("[ERROR]") or result_str.startswith("Error:"))
                if _is_failure:
                    _tool_fail_counter[_fail_key] = _tool_fail_counter.get(_fail_key, 0) + 1
                    if _tool_fail_counter[_fail_key] >= _MAX_CONSECUTIVE_TOOL_FAILS:
                        _retry_warning = (
                            f"【系統警告】工具 `{block.name}` 以相同參數已連續失敗 {_tool_fail_counter[_fail_key]} 次。"
                            f"請立即更換策略：嘗試不同的方法、參數或工具。不要繼續重試相同的失敗操作。"
                        )
                        _log("⚠️ RETRY-LOOP", f"Tool {block.name} failed {_tool_fail_counter[_fail_key]} times consecutively — injecting warning")
                elif result_str.startswith("\u2713") or result_str.startswith("[OK]"):
                    _tool_fail_counter.pop(_fail_key, None)
                # Track MEMORY.md writes
                if not _memory_written and block.name in {"Write", "Edit", "Bash"}:
                    _block_args = str(block.input) if block.input else ""
                    if "MEMORY.md" in _block_args or _memory_path_str in _block_args:
                        _memory_written = True
                        _log("🧠 MEMORY-WRITE", f"Claude updated MEMORY.md via {block.name} on turn {n}")

        # P15A-FIX-3: if stop_reason was tool_use but we found zero tool_use blocks,
        # the response is malformed.  Breaking here leaves the last assistant message
        # (already appended) without a paired tool_result, corrupting history for any
        # future continuation.  Inject a synthetic error tool_result for each tool_use
        # block actually present (if any), then break cleanly.
        if not tool_results:
            _stray_tool_uses = [b for b in response.content if hasattr(b, "type") and b.type == "tool_use"]
            if _stray_tool_uses:
                _log("⚠️ EMPTY-RESULTS", f"stop_reason=tool_use but 0 results collected; {len(_stray_tool_uses)} orphan tool_use blocks — injecting error results")
                _err_results = [
                    {"type": "tool_result", "tool_use_id": b.id, "content": "[System error: tool_use block not executed]"}
                    for b in _stray_tool_uses
                ]
                messages.append({"role": "user", "content": _err_results})
            break

        # ── Milestone Enforcer: anti-fabrication (same logic as OpenAI loop) ──
        _sent_message_this_turn = "mcp__evoclaw__send_message" in _tool_names_this_turn
        _did_real_work = bool(_tool_names_this_turn & _SUBSTANTIVE_TOOLS_CLAUDE)

        if _sent_message_this_turn and _did_real_work:
            _turns_since_notify = 0
            _only_notify_turns = 0
        elif _sent_message_this_turn and not _did_real_work:
            _only_notify_turns += 1
            _log("⚠️ FAKE-PROGRESS", f"Claude called only send_message (no real work) — streak={_only_notify_turns}")
            if _only_notify_turns >= 2 and n < MAX_ITER - 2:
                _log("🚨 FAKE-PROGRESS", f"Injecting anti-fabrication warning after {_only_notify_turns} fake-report turns")
                # BUG-FIX: Anthropic API requires every tool_result.tool_use_id to match
                # a real tool_use block from the preceding assistant message.  A synthetic
                # "__system__" id that has no matching tool_use causes a 400 validation
                # error that crashes the loop.  Inject the warning as a follow-up *user*
                # message instead — it is semantically equivalent and always API-valid.
                messages.append({"role": "user", "content": tool_results})
                messages.append({
                    "role": "user",
                    "content": (
                        "【系統警告】你已連續多輪只呼叫 send_message，沒有呼叫任何實質工具（Bash、Read、Write、run_agent 等）。"
                        "立刻停止假報告。你的下一步必須是：呼叫 Bash tool 執行指令、Read 讀取檔案、或 mcp__evoclaw__run_agent 委派任務。"
                    ),
                })
                # Skip the normal messages.append below since we already appended above
                tool_results = None  # sentinel: messages already appended
        else:
            _only_notify_turns = 0  # reset streak when doing real work silently
            _turns_since_notify += 1
            if _turns_since_notify >= 5 and n < MAX_ITER - 2:
                _log("⏰ MILESTONE", f"Claude: no send_message for {_turns_since_notify} turns — injecting reminder")
                # BUG-FIX: same issue — inject as a separate user message, not a
                # tool_result with a fake tool_use_id that Anthropic would reject.
                messages.append({"role": "user", "content": tool_results})
                messages.append({
                    "role": "user",
                    "content": (
                        f"⏰ 你已執行 {_turns_since_notify} 輪未向用戶回報進度。"
                        "請在繼續工作的同時，用 mcp__evoclaw__send_message 發送一條簡短的進度更新（1-2 句話）。"
                        "注意：只有在呼叫了 Bash/Read/Write 等實質工具之後才需要回報，不要虛報進度。"
                    ),
                })
                _turns_since_notify = 0
                tool_results = None  # sentinel: messages already appended

        if tool_results is not None:
            messages.append({"role": "user", "content": tool_results})

        # Fix 4 (STABILITY_ANALYSIS 3.5): inject retry warning as a user message
        if _retry_warning:
            messages.append({"role": "user", "content": _retry_warning})
            _retry_warning = ""

        # P15A-FIX-4: Trim history to prevent unbounded growth while preserving
        # tool_use / tool_result pairs.  The naive slice messages[:1]+messages[-(N-1):]
        # can cut between an assistant message with tool_use blocks and the following
        # user message with tool_result blocks, producing a 400 from the Anthropic API.
        # Instead, after slicing, advance the tail start until it begins on a message
        # that is NOT a user tool_result (i.e. its content is not a list starting with
        # a tool_result type), ensuring the pair is never split.
        if len(messages) > _MAX_HISTORY_MESSAGES:
            _keep_head = messages[:1]  # first user message always preserved
            _tail_start = len(messages) - (_MAX_HISTORY_MESSAGES - 1)
            # Advance _tail_start past any orphaned user tool_result messages
            while _tail_start < len(messages):
                _tm = messages[_tail_start]
                _tc = _tm.get("content", "")
                # A user message whose content is a list of tool_result dicts
                if isinstance(_tc, list) and _tc and isinstance(_tc[0], dict) and _tc[0].get("type") == "tool_result":
                    _tail_start += 1
                else:
                    break
            messages = _keep_head + messages[_tail_start:]

        # ── MEMORY.md reminder on penultimate turn ───────────────────────────
        if not _memory_written and n == MAX_ITER - 2:
            _log("⚠️ MEMORY-REMIND", f"MEMORY.md not updated by turn {n} — injecting CRITICAL reminder")
            messages.append({
                "role": "user",
                "content": (
                    f"【CRITICAL 系統警告】你在本 session 中尚未更新 MEMORY.md（{_memory_path_str}）。\n"
                    "這是倒數第二輪。你必須在結束前執行以下操作：\n"
                    "1. 使用 Write/Edit 工具更新 MEMORY.md\n"
                    "2. 在 `## 任務記錄 (Task Log)` 區段追加今日任務摘要\n"
                    "3. 若 `## 身份 (Identity)` 有新發現（弱點、原則），同步更新\n"
                    "格式：`[YYYY-MM-DD] <做了什麼、關鍵決策、解決方法>`"
                ),
            })

        # ── Penultimate-turn send_message enforcer (Claude) ──────────────────
        # Mirror of the OpenAI loop's enforcer: for Level A (MAX_ITER=6) the
        # normal milestone (5 silent turns AND n < MAX_ITER-2) can never fire,
        # so we add an explicit penultimate check here.
        if _turns_since_notify > 0 and n == MAX_ITER - 2:
            _log("⏰ MILESTONE-FINAL", f"Claude: no send_message in {_turns_since_notify} turns, penultimate turn {n} — injecting CRITICAL send reminder")
            messages.append({
                "role": "user",
                "content": (
                    "【CRITICAL 系統警告】你尚未向用戶發送任何回應（send_message）。\n"
                    f"這是倒數第二輪（turn {n+1}/{MAX_ITER}），下一輪是最後一輪。\n"
                    "你必須在下一輪立刻呼叫 mcp__evoclaw__send_message 把結果告知用戶，"
                    "否則用戶將看到「處理完成，但未能產生文字回應」錯誤。\n"
                    "不要再執行其他工具——把你已掌握的資訊直接發送出去。"
                ),
            })
            _turns_since_notify = 0

    # If the loop exhausted MAX_ITER without an end_turn, return whatever we have.
    # Avoids returning silent empty string to the host.
    if not final_response:
        _log("⚠️ LOOP-EXHAUST", f"Claude agent loop hit MAX_ITER={MAX_ITER} without end_turn — no final text collected")
    if not final_response or not final_response.strip():
        final_response = "（處理完成，但未能產生文字回應，請重新詢問。）"
    return final_response


def execute_tool(name: str, args: dict, chat_jid: str) -> str:
    """
    根據 Gemini 回傳的 function call 名稱，分派到對應的 tool 實作。
    chat_jid 傳給需要知道發送目標的工具（如 send_message）。
    """
    _log("🔧 TOOL", f"{name} args={str(args)[:1500]}")
    result = _execute_tool_inner(name, args, chat_jid)
    _log("🔧 RESULT", str(result)[:1500])
    return result


def _execute_tool_inner(name: str, args: dict, chat_jid: str) -> str:
    # BUG-P18D-11: validate that args is a dict before key access to avoid
    # AttributeError when the LLM passes a non-dict value.
    if not isinstance(args, dict):
        return f"Error: tool arguments must be a JSON object, got {type(args).__name__}"
    if name == "Bash":
        _cmd = args.get("command")
        if not isinstance(_cmd, str):
            return "Error: Bash requires a 'command' string argument"
        return tool_bash(_cmd)
    elif name == "Read":
        _fp = args.get("file_path")
        if not isinstance(_fp, str):
            return "Error: Read requires a 'file_path' string argument"
        return tool_read(_fp)
    elif name == "Write":
        _fp = args.get("file_path")
        _ct = args.get("content")
        if not isinstance(_fp, str):
            return "Error: Write requires a 'file_path' string argument"
        if not isinstance(_ct, str):
            return "Error: Write requires a 'content' string argument"
        return tool_write(_fp, _ct)
    elif name == "Edit":
        _fp = args.get("file_path")
        _os = args.get("old_string")
        _ns = args.get("new_string")
        if not isinstance(_fp, str):
            return "Error: Edit requires a 'file_path' string argument"
        if not isinstance(_os, str):
            return "Error: Edit requires an 'old_string' string argument"
        if not isinstance(_ns, str):
            return "Error: Edit requires a 'new_string' string argument"
        return tool_edit(_fp, _os, _ns)
    elif name == "mcp__evoclaw__send_message":
        _text = args.get("text")
        if not isinstance(_text, str):
            return "Error: send_message requires a 'text' string argument"
        _messages_sent_via_tool.append(True)  # 標記：已透過工具發送，host 不需再發 result
        return tool_send_message(chat_jid, _text, args.get("sender"))
    elif name == "mcp__evoclaw__schedule_task":
        _sched_prompt = args.get("prompt")
        _sched_type = args.get("schedule_type")
        _sched_val = args.get("schedule_value")
        if not isinstance(_sched_prompt, str):
            return "Error: schedule_task requires a 'prompt' string argument"
        if not isinstance(_sched_type, str):
            return "Error: schedule_task requires a 'schedule_type' string argument"
        if not isinstance(_sched_val, str):
            return "Error: schedule_task requires a 'schedule_value' string argument"
        return tool_schedule_task(
            _sched_prompt, _sched_type, _sched_val,
            args.get("context_mode", "group"),
            chat_jid,
        )
    elif name == "mcp__evoclaw__list_tasks":
        return tool_list_tasks()
    elif name == "mcp__evoclaw__cancel_task":
        return tool_cancel_task(args.get("task_id", ""))
    elif name == "mcp__evoclaw__pause_task":
        return tool_pause_task(args.get("task_id", ""))
    elif name == "mcp__evoclaw__resume_task":
        return tool_resume_task(args.get("task_id", ""))
    elif name == "Glob":
        _glob_pat = args.get("pattern")
        if not isinstance(_glob_pat, str):
            return "Error: Glob requires a 'pattern' string argument"
        return tool_glob(_glob_pat, args.get("path", WORKSPACE))
    elif name == "Grep":
        _grep_pat = args.get("pattern")
        if not isinstance(_grep_pat, str):
            return "Error: Grep requires a 'pattern' string argument"
        return tool_grep(_grep_pat, args.get("path", WORKSPACE), args.get("include", "*"))
    elif name == "WebFetch":
        _url = args.get("url")
        if not isinstance(_url, str):
            return "Error: WebFetch requires a 'url' string argument"
        return tool_web_fetch(_url)
    elif name == "mcp__evoclaw__run_agent":
        _ra_prompt = args.get("prompt")
        if not isinstance(_ra_prompt, str):
            return "Error: run_agent requires a 'prompt' string argument"
        return tool_run_agent(_ra_prompt, args.get("context_mode", "isolated"))
    elif name == "mcp__evoclaw__send_file":
        _sf_fp = args.get("file_path")
        if not isinstance(_sf_fp, str):
            return "Error: send_file requires a 'file_path' string argument"
        return tool_send_file(args.get("chat_jid", chat_jid), _sf_fp, args.get("caption", ""))
    elif name == "mcp__evoclaw__reset_group":
        target_jid = args.get("jid", "")
        if not target_jid:
            return "Error: jid is required"
        try:
            Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
            uid = str(uuid.uuid4())[:8]
            fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}-reset-{uid}.json"
            _atomic_ipc_write(fname, json.dumps({"type": "reset_group", "jid": target_jid}))
            _log("📨 IPC", f"type=reset_group jid={target_jid} → {fname.name}")
            return f"reset_group IPC sent for {target_jid} — fail counters will be cleared on next host poll cycle"
        except Exception as exc:
            return f"reset_group IPC write failed: {exc}"
    elif name == "mcp__evoclaw__start_remote_control":
        return tool_start_remote_control(args.get("chat_jid", chat_jid), args.get("sender", ""))
    elif name == "mcp__evoclaw__self_update":
        return tool_self_update(args.get("chat_jid", chat_jid))
    # ── Dynamic tools (installed via Skills container_tools:) ─────────────────
    if name in _dynamic_tools:
        try:
            return str(_dynamic_tools[name]["fn"](args))
        except Exception as exc:
            return f"Dynamic tool {name} error: {exc}"
    return f"Unknown tool: {name}"


# ── Agentic loop ──────────────────────────────────────────────────────────────

def run_agent_openai(client_holder, system_instruction: str, user_message: str, chat_jid: str, model: str, conversation_history: list = None, pool: "_KeyPool | None" = None, apply_key_fn=None, group_folder: str = "", max_iter: int = 20) -> str:
    """
    OpenAI-compatible agentic loop (NVIDIA NIM / OpenAI / Qwen / Groq / etc.)
    Works the same as run_agent but uses OpenAI chat completions API.
    client_holder: a one-element list [client] so key rotation can swap the client mid-loop.
    conversation_history: 原生 multi-turn 格式的對話歷史。
    pool/apply_key_fn: optional key pool for automatic rotation on rate-limit errors.
    max_iter: maximum number of agentic loop iterations (default 20; caller sets based on task complexity).
    """
    import json as _json
    history = [{"role": "system", "content": system_instruction}]
    # P15A-FIX-5: inject conversation history preserving structured content.
    # The old code used msg.get("content","") which evaluates to "" for list-typed
    # content (tool_result blocks), silently dropping those messages.  Use the raw
    # content value directly; only skip messages that have genuinely empty content.
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            # P16B-FIX-6: accept both string and list content; skip only truly empty
            # values.  The old guard `content or content == 0` contained a dead branch
            # (`content == 0` can never occur in conversation history — message content
            # is always a string or list) and was semantically confusing.  Replaced with
            # an explicit check: skip empty strings and empty lists only.
            if content is None:
                continue
            if isinstance(content, str) and not content.strip():
                continue
            if isinstance(content, list) and not content:
                continue
            history.append({"role": role, "content": content})
    history.append({"role": "user", "content": user_message})
    MAX_ITER = max_iter
    final_response = ""
    _no_tool_turns = 0  # consecutive turns without any tool call (Fix #169)
    _turns_since_notify = 0  # turns since last mcp__evoclaw__send_message call (milestone enforcer)
    _only_notify_turns = 0   # consecutive turns with ONLY send_message (no substantive tools)
    _memory_written = False  # True once agent writes to MEMORY.md this session (Enforcer v3)
    # P16B-FIX-3: guard against empty group_folder so the path does not resolve to
    # the relative string "MEMORY.md", which would never match an absolute path in
    # tool arguments and silently disable the MEMORY.md write-detection logic.
    _memory_path_str = f"{WORKSPACE}/MEMORY.md"  # BUG-FIX #424: group_folder is a name, not a path; use WORKSPACE
    _tool_fail_counter: dict = {}  # (tool_name, args_hash) -> consecutive_fail_count
    _MAX_CONSECUTIVE_TOOL_FAILS = 3
    _retry_warning: str = ""  # injected before next LLM call when tool retries detected
    # Tools that represent actual work (not just reporting)
    _SUBSTANTIVE_TOOLS = frozenset([
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch",
        "mcp__evoclaw__run_agent",
    ])

    for n in range(MAX_ITER):
        # Escalate to "required" when model has been avoiding tools (Fix #169).
        # tool_choice="required" is enforced at the API level — the model CANNOT
        # return a text-only response, it MUST make a tool call.
        # Qwen 不適合 tool_choice="required"（容易死循環），改用 prompt 強制
        _is_qwen = _is_qwen_model(model)
        if _no_tool_turns >= 2 and _is_qwen:
            _log("⚠️ QWEN-FORCE", f"Injecting critical prompt instead of tool_choice='required' (Qwen)")
            history.append({
                "role": "user",
                "content": (
                    "【緊急】你已連續多輪未呼叫工具。現在必須立刻選擇並執行一個工具：\n"
                    "A) Bash: 執行指令\n"
                    "B) Read: 讀取檔案\n"
                    "C) mcp__evoclaw__send_message: 發送訊息給用戶\n"
                    "禁止產出任何文字說明，直接呼叫工具。"
                ),
            })
            _no_tool_turns = 0
            _tool_choice = "auto"
        elif _no_tool_turns > 0:
            _tool_choice = "required"
        else:
            _tool_choice = "auto"
        if _no_tool_turns > 0:
            _log("⚠️ FORCE-TOOL", f"no_tool_turns={_no_tool_turns} — escalating tool_choice to 'required'")
        _log("🧠 LLM →", f"turn={n} provider=openai-compat tool_choice={_tool_choice}")
        _oai_history = history  # capture current snapshot for lambda
        try:
            response = _llm_call_with_retry(lambda: client_holder[0].chat.completions.create(
                model=model,
                messages=_oai_history,
                tools=OPENAI_TOOL_DECLARATIONS,
                tool_choice=_tool_choice,
                temperature=0.2 if _is_qwen else 0.3,
                max_tokens=4096,
            ), pool=pool, apply_key_fn=apply_key_fn)
        except Exception as _tc_err:
            if _tool_choice == "required":
                # Some providers don't support tool_choice="required" — fall back to "auto"
                _log("⚠️ FORCE-TOOL", f"tool_choice='required' rejected ({_tc_err}) — retrying with 'auto'")
                try:
                    response = _llm_call_with_retry(lambda: client_holder[0].chat.completions.create(
                        model=model,
                        messages=_oai_history,
                        tools=OPENAI_TOOL_DECLARATIONS,
                        tool_choice="auto",
                        temperature=0.2 if _is_qwen else 0.3,
                        max_tokens=4096,
                    ), pool=pool, apply_key_fn=apply_key_fn)
                except Exception as _fallback_err:
                    # Fallback also failed (e.g. Qwen timeout) — report cleanly and break
                    _log("❌ LLM-FALLBACK", f"Fallback API call also failed: {_fallback_err}")
                    final_response = f"（API 呼叫失敗：{type(_fallback_err).__name__}，請稍後重試。）"
                    break
            else:
                raise
        msg = response.choices[0].message
        stop_reason = response.choices[0].finish_reason
        _log("🧠 LLM ←", f"stop={stop_reason}")

        # Add assistant message to history
        msg_dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            msg_dict["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        history.append(msg_dict)

        # P15A-FIX-7: handle finish_reason=="length" (context length exceeded).
        # Treat it as a terminal condition: trim history and break cleanly rather
        # than treating it as a regular no-tool turn and incrementing _no_tool_turns.
        if stop_reason == "length":
            _log("⚠️ CTX-OVERFLOW", f"finish_reason=length — context window exceeded; trimming history and stopping")
            # Aggressively trim history to the most recent quarter to free context
            _trim_to = max(1, len(history) // 4)
            history = history[:1] + history[-_trim_to:]
            final_response = (msg.content or "").strip() or (
                "（系統：輸入超出模型 context 限制，請縮短對話記錄或簡化提示後重試。）"
            )
            break

        if not msg.tool_calls:
            _no_tool_turns += 1  # track consecutive no-tool turns (Fix #169)

            # Hard cap: after 3 consecutive turns without tool calls, give up (Fix #169).
            # At this point tool_choice="required" has already been active for 2 turns,
            # yet the model still produced no tool calls — something is fundamentally wrong.
            if _no_tool_turns >= 3:
                _log("❌ NO-TOOL", f"Model made no tool call for {_no_tool_turns} consecutive turns — breaking")
                # BUG-FIX: when msg.content is None (API returns null for tool-only
                # messages) fall back to an explicit error string rather than empty
                # string, so the user sees "agent gave up" rather than a confusing
                # generic "(處理完成...)" fallback message.
                final_response = (msg.content or "").strip() or (
                    "（系統：模型連續多輪未呼叫工具，強制終止。請重新提問或簡化任務。）"
                )
                break

            # ── Fallback: detect bash code blocks the model forgot to run ─────
            # Some models (Qwen/NIM) output ```bash blocks as text instead of
            # calling the Bash tool. Auto-execute them and feed results back.
            import re as _re_cb
            content = msg.content or ""
            _code_blocks = _re_cb.findall(r'```(?:bash|sh|shell)?\n([\s\S]*?)```', content)
            _runnable = [b.strip() for b in _code_blocks if b.strip()]
            if _runnable and n < MAX_ITER - 1:
                _log("⚠️ AUTO-EXEC", f"model output {len(_runnable)} code block(s) as text — auto-executing")
                _no_tool_turns = 0  # reset: code blocks count as attempted tool use
                _exec_outputs = []
                for _cmd in _runnable:
                    _log("🔧 TOOL", f"Bash (fallback) args={_cmd[:300]}")
                    # Sanity check 1: reject commands that look like git log output
                    # (lines starting with 7-40 hex chars + space, e.g. "c43faa8 feat: ...")
                    _auto_lines = [_l for _l in _cmd.strip().splitlines() if _l.strip()]
                    _GIT_LOG_RE = _re_cb.compile(r'^[0-9a-f]{7,40} \S')
                    # Sanity check 2: reject bare filenames (e.g. "MEMORY.md", "/workspace/group/MEMORY.md")
                    _BARE_FILE_RE = _re_cb.compile(
                        r'^[^\s|;&<>$`\'\"()\[\]{}!\\]+\.(md|txt|py|js|ts|sh|json|yaml|yml|toml|csv|log|conf|cfg)$',
                        _re_cb.IGNORECASE,
                    )
                    if _auto_lines and len(_auto_lines) >= 2 and all(
                        _GIT_LOG_RE.match(_l) for _l in _auto_lines[:4]
                    ):
                        _res = "✗ Skipped: this looks like git log output, not a valid bash command. Use `git log --oneline` explicitly if you need commit history."
                        _log("⚠️ AUTO-EXEC-SKIP", f"Rejected fallback block that looks like git log output: {_cmd[:120]}")
                    elif _auto_lines and len(_auto_lines) == 1 and _BARE_FILE_RE.match(_cmd.strip()):
                        _res = (
                            f"✗ Skipped: '{_cmd.strip()}' is a filename, not a bash command.\n"
                            "Use the Read tool to read it, or Write/Edit tool to write it."
                        )
                        _log("⚠️ AUTO-EXEC-SKIP", f"Rejected bare filename as bash command: {_cmd.strip()[:100]}")
                    else:
                        _res = execute_tool("Bash", {"command": _cmd}, chat_jid)
                    _log("🔧 RESULT", str(_res)[:1500])
                    _exec_outputs.append(f"$ {_cmd[:200]}\n{_res}")
                _combined = "\n\n".join(_exec_outputs)
                history.append({
                    "role": "user",
                    "content": f"[系統自動執行了 {len(_runnable)} 個指令]\n{_combined}\n\n請根據以上輸出，繼續任務並回報最終結果。",
                })
                continue

            # ── Fallback 2: detect fake status lines *(正在...)* etc. ──────────
            # Log the detection; the real enforcement happens via tool_choice="required"
            # on the NEXT iteration (Fix #167 + #169: API-level enforcement is primary,
            # text re-prompt is secondary fallback for providers that don't support "required").
            # BUG-FIX: the original regex only matched *(...)* and *[...]* patterns.
            # Add the same extended set used by the Claude/Gemini loops so that
            # non-Qwen OpenAI models (GPT-4, etc.) also get full fake-status coverage.
            _FAKE_STATUS_RE = _re_cb.compile(
                r'\*\([^)]*\)\*'                                                   # *(正在執行...)*
                r'|\*\[[^\]]*\]\*'                                                  # *[running...]*
                r'|✅\s*Done'                                                      # ✅ Done
                r'|✅\s*完成'                                                      # ✅ 完成
                r'|【[^】]*(?:已|正在|將|完成|處理|執行)[^】]*】'                    # 【已完成】
                r'|（[^）]{2,30}(?:已|正在|處理|執行)[^）]{0,20}）'                 # （已完成）
                r'|(?:I\s+have\s+(?:completed|finished|executed|run|written))'     # English fake-done
                r'|(?:Task\s+(?:is\s+)?(?:complete|done|finished))'               # Task complete
                r'|(?:Successfully\s+(?:completed|executed|ran|written))',          # Successfully executed
                _re_cb.DOTALL | _re_cb.IGNORECASE,
            )
            _fake_hits = _FAKE_STATUS_RE.findall(content)
            # 擴展假狀態偵測，涵蓋常見的虛假回應格式（所有 OpenAI-compatible models）
            _EXTENDED_FAKE_PATTERNS = [
                r'(?:已|正在|即將).{0,8}(?:完成|處理|執行|分析)',  # 已完成、正在處理
            ]
            for _qp in _EXTENDED_FAKE_PATTERNS:
                try:
                    _ext_fake_hits = _re_cb.findall(_qp, content)
                    if _ext_fake_hits:
                        _fake_hits = (_fake_hits or []) + _ext_fake_hits
                except Exception:
                    pass
            if _fake_hits and n < MAX_ITER - 1:
                _log("⚠️ FAKE-STATUS", f"model wrote {len(_fake_hits)} fake status line(s) — tool_choice='required' on next turn")
                history.append({
                    "role": "user",
                    "content": (
                        "【系統警告】你剛才的回覆包含假狀態行（例如 *(正在執行...)* ），沒有呼叫任何工具。"
                        "下一輪系統將強制要求你必須呼叫工具，請立刻使用 Bash tool 或其他工具執行所需命令。"
                    ),
                })
                continue

            # ── Semantic cross-validation: action claim without any tool call ──
            # Catches completion-verb hallucinations that slip past syntactic patterns.
            # OpenAI: tool calls appear as msg.tool_calls (None or empty list when absent)
            _had_tool_calls_this_turn_oai = bool(getattr(msg, "tool_calls", None))
            if not _had_tool_calls_this_turn_oai and _ACTION_CLAIM_RE.search(content) and n < MAX_ITER - 1:
                _log("⚠️ SEMANTIC-FAKE", "OpenAI model claims action complete but called no tools this turn")
                history.append({
                    "role": "user",
                    "content": (
                        "【系統驗證】你的回應中聲稱已執行了某項操作，但本輪沒有呼叫任何工具。"
                        "請實際使用對應工具（Read/Write/Edit/Bash）執行並確認，不要只是聲明已完成。"
                    ),
                })
                continue

            # No code blocks, no fake status — model is genuinely done
            final_response = content
            break

        # Model made tool calls — reset the no-tool counter (Fix #169)
        _no_tool_turns = 0

        # ── 里程碑強制器 v2：區分「假報告」和「真工作」────────────────────────
        # 問題：舊版允許模型只呼叫 send_message 來通過里程碑檢查，
        # 導致模型用假進度報告（完全虛構內容）冒充在工作。
        # 修正：只有「實質工具 + send_message」的組合才算真里程碑。
        #       連續多輪「只有 send_message」→ 強硬警告：停止假報告，立即做事。
        #
        # BUG-P26B-3: the OpenAI API requires that every tool-role message
        # immediately follows the assistant message that issued the tool_calls —
        # no user-role message may be inserted between them.  The milestone
        # enforcer and MEMORY.md reminder previously injected user messages into
        # history BEFORE the tool-execution loop, which placed them between the
        # assistant tool_calls message and its tool-role results, causing a 400
        # validation error on the next API call.  Fix: collect deferred injection
        # messages in _deferred_user_msgs and append them AFTER all tool results
        # have been added to history.
        _deferred_user_msgs: list = []

        _tool_names_this_turn = {tc.function.name for tc in msg.tool_calls}
        _sent_message_this_turn = "mcp__evoclaw__send_message" in _tool_names_this_turn
        _did_real_work = bool(_tool_names_this_turn & _SUBSTANTIVE_TOOLS)

        if _sent_message_this_turn and _did_real_work:
            # Genuine progress report: real work + notification
            _turns_since_notify = 0
            _only_notify_turns = 0
        elif _sent_message_this_turn and not _did_real_work:
            # Only send_message, no actual work done — track fabrication pattern
            _only_notify_turns += 1
            _log("⚠️ FAKE-PROGRESS", f"Model called only send_message (no real work) — streak={_only_notify_turns}")
            if _only_notify_turns >= 2 and n < MAX_ITER - 2:
                _log("🚨 FAKE-PROGRESS", f"Injecting anti-fabrication warning after {_only_notify_turns} fake-report turns")
                _deferred_user_msgs.append({
                    "role": "user",
                    "content": (
                        "【系統警告】你已連續多輪只呼叫 send_message，沒有呼叫任何實質工具（Bash、Read、Write、run_agent 等）。"
                        "這代表你在發送虛構的進度報告而不是真正執行任務。"
                        "立刻停止假報告。你的下一步必須是：呼叫 Bash tool 執行指令、Read 讀取檔案、或 mcp__evoclaw__run_agent 委派任務。"
                        "如果不知道怎麼繼續，使用 mcp__evoclaw__run_agent 把任務委派給子代理。"
                    ),
                })
        else:
            # No send_message — working silently; reset only_notify streak
            _only_notify_turns = 0
            _turns_since_notify += 1
            if _turns_since_notify >= 5 and n < MAX_ITER - 2:
                _log("⏰ MILESTONE", f"No send_message for {_turns_since_notify} turns — injecting reminder")
                _deferred_user_msgs.append({
                    "role": "user",
                    "content": (
                        f"⏰ 你已執行 {_turns_since_notify} 輪未向用戶回報進度。"
                        "請在繼續工作的同時，用 mcp__evoclaw__send_message 發送一條簡短的進度更新（1-2 句話）。"
                        "注意：只有在呼叫了 Bash/Read/Write 等實質工具之後才需要回報，不要虛報進度。"
                    ),
                })
                _turns_since_notify = 0

        # ── Milestone Enforcer v3: MEMORY.md 寫入偵測 ───────────────────────
        # 偵測本輪是否有工具呼叫了 MEMORY.md（Write/Edit/Bash 寫入）。
        # 確保 session 結束前 agent 確實更新了長期記憶與身份認知。
        if not _memory_written:
            for _tc in msg.tool_calls:
                if _tc.function.name in {"Write", "Edit", "Bash"}:
                    _tc_args = _tc.function.arguments or ""
                    if "MEMORY.md" in _tc_args or _memory_path_str in _tc_args:
                        _memory_written = True
                        _log("🧠 MEMORY-WRITE", f"Agent updated MEMORY.md via {_tc.function.name} on turn {n}")
                        break

        # 倒數第二輪若 MEMORY.md 仍未寫入 → CRITICAL 提醒
        # BUG-P26B-3 (cont): defer this injection as well so it lands after tool results.
        if not _memory_written and n == MAX_ITER - 2:
            _log("⚠️ MEMORY-REMIND", f"MEMORY.md not updated by turn {n} — injecting CRITICAL reminder")
            _deferred_user_msgs.append({
                "role": "user",
                "content": (
                    f"【CRITICAL 系統警告】你在本 session 中尚未更新 MEMORY.md（{_memory_path_str}）。\n"
                    "這是倒數第二輪。你必須在結束前執行以下操作：\n"
                    "1. 使用 Write/Edit 工具更新 MEMORY.md\n"
                    "2. 在 `## 任務記錄 (Task Log)` 區段追加今日任務摘要\n"
                    "3. 若 `## 身份 (Identity)` 有新發現（弱點、原則），同步更新\n"
                    "格式：`[YYYY-MM-DD] <做了什麼、關鍵決策、解決方法>`"
                ),
            })

        # Penultimate-turn send_message enforcer:
        # The normal milestone fires at _turns_since_notify >= 5 AND n < MAX_ITER - 2.
        # For Level A sessions (MAX_ITER=6) that condition can never be satisfied
        # (you'd need 5 silent turns but the window closes at n<4).
        # Fix: at n == MAX_ITER - 2, if the agent has not sent ANY message yet,
        # inject a CRITICAL reminder so it delivers the result on the final turn.
        if _turns_since_notify > 0 and n == MAX_ITER - 2:
            _log("⏰ MILESTONE-FINAL", f"No send_message in {_turns_since_notify} turns, penultimate turn {n} — injecting CRITICAL send reminder")
            _deferred_user_msgs.append({
                "role": "user",
                "content": (
                    "【CRITICAL 系統警告】你尚未向用戶發送任何回應（send_message）。\n"
                    f"這是倒數第二輪（turn {n+1}/{MAX_ITER}），下一輪是最後一輪。\n"
                    "你必須在下一輪立刻呼叫 mcp__evoclaw__send_message 把結果告知用戶，"
                    "否則用戶將看到「處理完成，但未能產生文字回應」錯誤。\n"
                    "不要再執行其他工具——把你已掌握的資訊直接發送出去。"
                ),
            })
            _turns_since_notify = 0  # reset to prevent re-trigger

        # Execute all tool calls and add results
        for tc in msg.tool_calls:
            try:
                args = _json.loads(tc.function.arguments)
            except Exception as _arg_err:
                # 嘗試修復 Qwen 常見的 JSON 格式問題
                _raw_args = str(tc.function.arguments or "")
                _recovered = False
                if _raw_args.strip() and _raw_args != "{}":
                    try:
                        import re as _re_fix_args
                        # 修復常見問題：末尾多餘逗號
                        _fixed = _re_fix_args.sub(r',\s*([}\]])', r'\1', _raw_args)
                        args = _json.loads(_fixed)
                        _recovered = True
                        _log("🔧 ARG-RECOVERY", f"Recovered malformed args for {tc.function.name}")
                    except Exception:
                        args = {}
                else:
                    args = {}

                if not _recovered:
                    _log("⚠️ TOOL-ARG-PARSE", f"Failed to parse/recover tool args for {tc.function.name}: {_arg_err}")
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Error: Failed to parse tool arguments: {_arg_err}"
                    })
                    continue
            try:
                result = execute_tool(tc.function.name, args, chat_jid)
            except Exception as _tool_exc:
                result = f"[Tool error: {_tool_exc}]"
                _log("❌ TOOL-EXC", f"Tool {tc.function.name} raised exception: {_tool_exc}")
            # Truncate large tool results before adding to history
            result_str = str(result)
            if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                half = _MAX_TOOL_RESULT_CHARS // 2
                head = result_str[:half]
                tail = result_str[-half:]
                omitted = len(result_str) - _MAX_TOOL_RESULT_CHARS
                result_str = head + f"\n[... {omitted} chars omitted (middle truncated to preserve head+tail) ...]\n" + tail
            # Fix 4 (STABILITY_ANALYSIS 3.5): detect repeated identical-args tool failures
            _fail_key_oai = (tc.function.name, hash(str(args)[:200]))
            _is_failure_oai = (result_str.startswith("\u2717") or result_str.startswith("[ERROR]") or result_str.startswith("Error:"))
            if _is_failure_oai:
                _tool_fail_counter[_fail_key_oai] = _tool_fail_counter.get(_fail_key_oai, 0) + 1
                if _tool_fail_counter[_fail_key_oai] >= _MAX_CONSECUTIVE_TOOL_FAILS:
                    _retry_warning = (
                        f"【系統警告】工具 `{tc.function.name}` 以相同參數已連續失敗 {_tool_fail_counter[_fail_key_oai]} 次。"
                        f"請立即更換策略：嘗試不同的方法、參數或工具。不要繼續重試相同的失敗操作。"
                    )
                    _log("⚠️ RETRY-LOOP", f"Tool {tc.function.name} failed {_tool_fail_counter[_fail_key_oai]} times consecutively — injecting warning")
            elif result_str.startswith("\u2713") or result_str.startswith("[OK]"):
                _tool_fail_counter.pop(_fail_key_oai, None)
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_str,
            })

        # BUG-P26B-3 (cont): now that all tool-role messages have been appended,
        # it is safe to inject deferred user messages (milestone enforcer warnings,
        # MEMORY.md reminders) without violating the OpenAI API constraint that
        # tool-role messages must directly follow the assistant tool_calls message.
        for _deferred_msg in _deferred_user_msgs:
            history.append(_deferred_msg)

        # Inject retry warning if any tool failed repeatedly this turn
        if _retry_warning:
            history.append({"role": "user", "content": _retry_warning})
            _retry_warning = ""

        # P15A-FIX-6: Trim history to prevent unbounded growth while preserving
        # assistant tool_calls / tool-result message pairs.  The naive slice
        # history[:1]+history[-(N-1):] can sever an assistant message that has
        # tool_calls from the following tool-role messages, producing a 400 from
        # the OpenAI API ("tool messages must be preceded by an assistant tool_calls
        # message").  After slicing, advance the tail start until the first retained
        # message is NOT a tool-role message.
        if len(history) > _MAX_HISTORY_MESSAGES:
            _keep_sys = history[:1]  # system message always preserved
            _tail_start = len(history) - (_MAX_HISTORY_MESSAGES - 1)
            # Advance past any orphaned tool-role messages at the new tail boundary
            while _tail_start < len(history) and history[_tail_start].get("role") == "tool":
                _tail_start += 1
            history = _keep_sys + history[_tail_start:]

    if not final_response:
        _log("⚠️ LOOP-EXHAUST", f"OpenAI agent loop hit MAX_ITER={MAX_ITER} without finish_reason=stop — no final text collected")
    if not final_response or not final_response.strip():
        final_response = "（處理完成，但未能產生文字回應，請重新詢問。）"
    return final_response



def run_agent(client_holder, system_instruction: str, user_message: str, chat_jid: str, assistant_name: str = "Eve", conversation_history: list = None, pool: "_KeyPool | None" = None, apply_key_fn=None, max_iter: int = 20, group_folder: str = "") -> str:
    """
    Gemini function-calling 代理迴圈（agentic loop）。

    工作原理：
    1. 將用戶訊息加入 history，發送給 Gemini
    2. Gemini 回傳的 response 可能包含：
       a. 純文字：代表 agent 已完成思考，直接回傳給用戶
       b. Function call：代表 agent 要使用工具，執行後將結果加回 history
    3. 若是 function call，執行工具並將結果作為 user role 加回 history，
       然後再次呼叫 Gemini（繼續下一輪）
    4. 重複直到 Gemini 不再發出 function call，或達到 max_iter 上限

    max_iter: 由呼叫方根據任務複雜度動態設定（Level A=6, Level B=20）。
    history 維護完整的對話記錄（user / model / tool_response），
    讓 Gemini 在每次迭代都有完整的上下文，不需要重新解釋先前的工具結果。
    """
    # Few-shot: only teach identity response for direct "who are you" questions.
    # Avoid adding examples too similar to real user queries — that causes the model
    # to apply the identity template to all questions (the "always says Eve" bug).
    identity_response = f"我是 {assistant_name}，你的個人 AI 助理！有什麼需要幫忙的嗎？"
    history = [
        types.Content(role="user", parts=[types.Part(text="你是誰？你是什麼AI？你是Google的嗎？")]),
        types.Content(role="model", parts=[types.Part(text=identity_response)]),
    ]
    # 注入對話歷史（原生 multi-turn 格式），放在 few-shot 之後、當前訊息之前
    if conversation_history:
        for msg in conversation_history:
            role = "model" if msg.get("role") == "assistant" else "user"
            _raw_content = msg.get("content", "")
            # P16B-FIX-8: Gemini only accepts string text parts.  If conversation
            # history was captured from a Claude/OpenAI session it may contain
            # list-typed content (tool_use/tool_result blocks).  Coerce to a string
            # representation so Gemini does not receive a non-string Part.text value
            # (which would raise TypeError or produce garbled output).
            if isinstance(_raw_content, list):
                text = " ".join(
                    item.get("text", "") if isinstance(item, dict) else str(item)
                    for item in _raw_content
                ).strip()
            else:
                text = str(_raw_content).strip() if _raw_content else ""
            if text:
                history.append(types.Content(role=role, parts=[types.Part(text=text)]))
    history.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

    MAX_ITER = max_iter  # 由呼叫方動態設定（Level A=6, Level B=20）
    final_response = ""
    _memory_written = False   # True once agent writes to MEMORY.md this session
    _memory_path_str = f"{WORKSPACE}/MEMORY.md"  # BUG-FIX #424: group_folder is a name, not a path; use WORKSPACE
    _tool_fail_counter: dict = {}  # (tool_name, args_hash) -> consecutive_fail_count
    _MAX_CONSECUTIVE_TOOL_FAILS = 3
    _retry_warning: str = ""  # injected before next LLM call when tool retries detected
    _turns_since_notify = 0   # turns since last mcp__evoclaw__send_message call
    _only_notify_turns = 0    # consecutive turns with ONLY send_message (no real work)
    _no_tool_turns = 0        # consecutive turns without any tool call
    # Tools that represent actual work (not just reporting)
    _SUBSTANTIVE_TOOLS_GEMINI = frozenset([
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch",
        "mcp__evoclaw__run_agent",
    ])
    import re as _re_gemini
    # Fake-status regex: covers both standard and Chinese hallucination patterns
    # P15A-FIX-10: added English fake-done patterns (previously only in OpenAI loop)
    _FAKE_STATUS_RE_G = _re_gemini.compile(
        r'\*\([^)]*\)\*'          # *(正在執行...)*
        r'|\*\[[^\]]*\]\*'         # *[running...]*
        r'|✅\s*Done'             # ✅ Done
        r'|✅\s*完成'             # ✅ 完成
        r'|【[^】]*(?:已|正在|將|完成|處理|執行)[^】]*】'   # 【已完成】
        r'|（[^）]{2,30}(?:已|正在|處理|執行)[^）]{0,20}）' # （已完成）
        r'|(?:I\s+have\s+(?:completed|finished|executed|run|written))'  # English fake-done
        r'|(?:Task\s+(?:is\s+)?(?:complete|done|finished))'            # Task complete
        r'|(?:Successfully\s+(?:completed|executed|ran|written))',      # Successfully executed
        _re_gemini.DOTALL | _re_gemini.IGNORECASE,
    )

    # P16B-FIX-7: cache GEMINI_MODEL once before the loop rather than re-reading
    # os.environ on every iteration.  os.environ is a dict-like but reading it 20+
    # times per request is wasteful, and a concurrent env mutation mid-loop would
    # cause the model name to change between turns — an impossible-to-debug failure.
    _gemini_model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    for n in range(MAX_ITER):
        _log("🧠 LLM →", f"turn={n} provider=gemini")
        response = _llm_call_with_retry(lambda: client_holder[0].models.generate_content(
            model=_gemini_model_name,
            contents=history,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
                temperature=0.3,  # 適中的隨機性，讓回覆自然但不失準確
            ),
        ), pool=pool, apply_key_fn=apply_key_fn)

        candidate = response.candidates[0] if response.candidates else None
        stop_reason = str(candidate.finish_reason) if candidate else "none"
        _log("🧠 LLM ←", f"stop={stop_reason}")

        # P31A-FIX-1: Check for terminal finish_reason values (SAFETY, RECITATION,
        # MAX_TOKENS) BEFORE inspecting content.  Gemini sometimes returns partial
        # content alongside a SAFETY or RECITATION finish_reason; the old code only
        # checked these in the `not candidate.content` branch, so a response that
        # had content *and* finish_reason=SAFETY would silently fall through, treat
        # the partial text as a normal reply, and never surface the safety message.
        _sr_lower = stop_reason.lower()
        _is_safety_block = "safety" in _sr_lower or "recitation" in _sr_lower
        _is_max_tokens   = "max_tokens" in _sr_lower or stop_reason.strip() == "2"
        if _is_safety_block or _is_max_tokens:
            _feedback = getattr(response, "prompt_feedback", None)
            if _feedback:
                _log("⚠️ GEMINI-BLOCK", f"prompt_feedback={_feedback}")
            if "safety" in _sr_lower:
                final_response = "（系統：回應被安全過濾器攔截，請調整問題後重試。）"
                _log("🚨 GEMINI-SAFETY", f"finish_reason={stop_reason} — safety block; returning user-visible error")
            elif "recitation" in _sr_lower:
                final_response = "（系統：回應被版權偵測攔截，請以不同方式重新提問。）"
                _log("⚠️ GEMINI-RECITATION", f"finish_reason={stop_reason} — recitation block")
            else:
                final_response = "（系統：輸入超出模型 context 限制，請縮短對話記錄或簡化提示後重試。）"
                _log("⚠️ GEMINI-MAXTOKEN", f"finish_reason={stop_reason} — context limit hit")
            break

        if not candidate or not candidate.content or not candidate.content.parts:
            # No content and no recognised terminal finish_reason — unexpected empty
            # response (e.g. network truncation).  Log and break without overwriting
            # any final_response already accumulated.
            _log("⚠️ GEMINI-EMPTY", f"finish_reason={stop_reason} — no content/parts; breaking")
            break

        parts = candidate.content.parts
        # 將 Gemini 的回覆加入 history，讓下一輪能看到完整對話脈絡
        history.append(types.Content(role="model", parts=parts))

        # 找出所有 function call（Gemini 可能一次發出多個工具呼叫）
        fn_calls = [p for p in parts if p.function_call]

        if not fn_calls:
            # 沒有 function call：agent 完成推理，收集所有文字輸出
            final_response = "".join(p.text for p in parts if hasattr(p, "text") and p.text)
            _no_tool_turns += 1

            # ── Fake status detection on text-only turn ──────────────────────
            _fake_hits = _FAKE_STATUS_RE_G.findall(final_response)
            if _fake_hits and n < MAX_ITER - 1:
                _log("⚠️ FAKE-STATUS", f"Gemini wrote {len(_fake_hits)} fake status indicator(s) without tool calls")
                history.append(types.Content(role="user", parts=[types.Part(text=(
                    "【系統警告】你剛才的回覆包含假狀態指示（例如 ✅ Done 或 *(正在執行...)* ），但沒有呼叫任何工具。"
                    "請立刻使用 Bash tool 或其他工具實際執行所需命令，不要只是描述或假裝完成。"
                ))]))
                final_response = ""
                continue

            # ── Semantic cross-validation: action claim without any tool call ──
            # Catches completion-verb hallucinations that slip past syntactic patterns.
            # Gemini: fn_calls is already empty here (we're in the `if not fn_calls` branch)
            # so _had_tool_calls_this_turn is always False at this point.
            if _ACTION_CLAIM_RE.search(final_response) and n < MAX_ITER - 1:
                _log("⚠️ SEMANTIC-FAKE", "Gemini claims action complete but called no tools this turn")
                history.append(types.Content(role="user", parts=[types.Part(text=(
                    "【系統驗證】你的回應中聲稱已執行了某項操作，但本輪沒有呼叫任何工具。"
                    "請實際使用對應工具（Read/Write/Edit/Bash）執行並確認，不要只是聲明已完成。"
                ))]))
                final_response = ""
                continue

            # ── Hard cap: 3 consecutive no-tool turns → stop ─────────────────
            if _no_tool_turns >= 3:
                _log("❌ NO-TOOL", f"Gemini made no tool call for {_no_tool_turns} consecutive turns — breaking")
                break

            # No fake status and no forced continuation — genuine done
            break

        # Model made tool calls — reset no-tool counter
        _no_tool_turns = 0

        # 執行所有工具呼叫，並收集結果
        fn_responses = []
        _tool_names_this_turn: set = set()
        for part in fn_calls:
            fc = part.function_call
            _tool_names_this_turn.add(fc.name)
            try:
                result = execute_tool(fc.name, dict(fc.args), chat_jid)
            except Exception as e:
                result = f"[Tool error: {e}]"
                _log("❌ TOOL-EXC", f"Tool {fc.name} raised exception: {e}")
            # Truncate large tool results before adding to history
            result_str = str(result)
            if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                half = _MAX_TOOL_RESULT_CHARS // 2
                head = result_str[:half]
                tail = result_str[-half:]
                omitted = len(result_str) - _MAX_TOOL_RESULT_CHARS
                result_str = head + f"\n[... {omitted} chars omitted (middle truncated to preserve head+tail) ...]\n" + tail
            # Fix 4 (STABILITY_ANALYSIS 3.5): detect repeated identical-args tool failures
            _fail_key_gem = (fc.name, hash(str(dict(fc.args) if fc.args else {})[:200]))
            _is_failure_gem = (result_str.startswith("\u2717") or result_str.startswith("[ERROR]") or result_str.startswith("Error:"))
            if _is_failure_gem:
                _tool_fail_counter[_fail_key_gem] = _tool_fail_counter.get(_fail_key_gem, 0) + 1
                if _tool_fail_counter[_fail_key_gem] >= _MAX_CONSECUTIVE_TOOL_FAILS:
                    _retry_warning = (
                        f"【系統警告】工具 `{fc.name}` 以相同參數已連續失敗 {_tool_fail_counter[_fail_key_gem]} 次。"
                        f"請立即更換策略：嘗試不同的方法、參數或工具。不要繼續重試相同的失敗操作。"
                    )
                    _log("⚠️ RETRY-LOOP", f"Tool {fc.name} failed {_tool_fail_counter[_fail_key_gem]} times consecutively — injecting warning")
            elif result_str.startswith("\u2713") or result_str.startswith("[OK]"):
                _tool_fail_counter.pop(_fail_key_gem, None)
            # Track MEMORY.md writes
            if not _memory_written and fc.name in {"Write", "Edit", "Bash"}:
                _fc_args_str = str(fc.args) if fc.args else ""
                if "MEMORY.md" in _fc_args_str or _memory_path_str in _fc_args_str:
                    _memory_written = True
                    _log("🧠 MEMORY-WRITE", f"Gemini updated MEMORY.md via {fc.name} on turn {n}")
            # 將工具結果包裝成 FunctionResponse 格式，Gemini 要求此格式
            fn_responses.append(
                types.Part(function_response=types.FunctionResponse(
                    name=fc.name,
                    response={"result": result_str},
                ))
            )

        # ── Milestone Enforcer: anti-fabrication (same logic as OpenAI loop) ──
        _sent_message_this_turn = "mcp__evoclaw__send_message" in _tool_names_this_turn
        _did_real_work = bool(_tool_names_this_turn & _SUBSTANTIVE_TOOLS_GEMINI)

        if _sent_message_this_turn and _did_real_work:
            _turns_since_notify = 0
            _only_notify_turns = 0
        elif _sent_message_this_turn and not _did_real_work:
            _only_notify_turns += 1
            _log("⚠️ FAKE-PROGRESS", f"Gemini called only send_message (no real work) — streak={_only_notify_turns}")
            if _only_notify_turns >= 2 and n < MAX_ITER - 2:
                _log("🚨 FAKE-PROGRESS", f"Injecting anti-fabrication warning after {_only_notify_turns} fake-report turns")
                # BUG-FIX: Appending an extra FunctionResponse for mcp__evoclaw__send_message
                # creates a duplicate FunctionResponse for the same function name in a
                # single user turn.  Gemini may reject or misinterpret this.  Instead,
                # flush the real fn_responses first, then inject a separate user text
                # turn with the warning — this is always well-formed per the protocol.
                history.append(types.Content(role="user", parts=fn_responses))
                history.append(types.Content(role="user", parts=[types.Part(text=(
                    "【系統警告】你已連續多輪只呼叫 send_message，沒有呼叫任何實質工具（Bash、Read、Write、run_agent 等）。"
                    "立刻停止假報告。你的下一步必須是：呼叫 Bash tool 執行指令、Read 讀取檔案、或 mcp__evoclaw__run_agent 委派任務。"
                ))]))
                fn_responses = None  # sentinel: history already appended
        else:
            _only_notify_turns = 0  # reset streak when doing real work silently
            _turns_since_notify += 1
            if _turns_since_notify >= 5 and n < MAX_ITER - 2:
                _log("⏰ MILESTONE", f"Gemini: no send_message for {_turns_since_notify} turns — injecting reminder")
                # BUG-FIX: same issue — flush real fn_responses, then add a separate
                # user text turn for the reminder rather than a duplicate FunctionResponse.
                history.append(types.Content(role="user", parts=fn_responses))
                history.append(types.Content(role="user", parts=[types.Part(text=(
                    f"⏰ 你已執行 {_turns_since_notify} 輪未向用戶回報進度。"
                    "請在繼續工作的同時，用 mcp__evoclaw__send_message 發送一條簡短的進度更新（1-2 句話）。"
                    "注意：只有在呼叫了 Bash/Read/Write 等實質工具之後才需要回報，不要虛報進度。"
                ))]))
                _turns_since_notify = 0
                fn_responses = None  # sentinel: history already appended

        # 工具結果以 user role 加回 history（Gemini function calling 協議要求）
        if fn_responses is not None:
            history.append(types.Content(role="user", parts=fn_responses))

        # BUG-P27A-GEMINI-1 FIX: when the milestone enforcer took the sentinel
        # path (fn_responses = None, meaning fn_responses + a warning text were
        # already appended as two consecutive user turns), appending _retry_warning
        # as yet another role="user" message creates a third (or fourth) consecutive
        # user turn in a row.  While Gemini's API tolerates consecutive user turns,
        # consolidating the retry warning into the most recent user message avoids
        # a structurally confusing history and prevents the history trimmer from
        # potentially leaving an orphaned user(warning) message at the tail boundary.
        # When fn_responses was already flushed by the sentinel path, append
        # _retry_warning text to the last history entry (which is a user text Part)
        # rather than inserting a new user message.
        if _retry_warning:
            if fn_responses is None and history and history[-1].role == "user":
                # Extend the last user message's parts with the retry warning
                # so it arrives in the same turn as the milestone warning.
                history[-1].parts.append(types.Part(text="\n\n" + _retry_warning))
            else:
                history.append(types.Content(role="user", parts=[types.Part(text=_retry_warning)]))
            _retry_warning = ""

        # ── MEMORY.md reminder on penultimate turn ────────────────────────────
        if not _memory_written and n == MAX_ITER - 2:
            _log("⚠️ MEMORY-REMIND", f"MEMORY.md not updated by turn {n} — injecting CRITICAL reminder")
            history.append(types.Content(role="user", parts=[types.Part(text=(
                f"【CRITICAL 系統警告】你在本 session 中尚未更新 MEMORY.md（{_memory_path_str}）。\n"
                "這是倒數第二輪。你必須在結束前執行以下操作：\n"
                "1. 使用 Write/Edit 工具更新 MEMORY.md\n"
                "2. 在 `## 任務記錄 (Task Log)` 區段追加今日任務摘要\n"
                "3. 若 `## 身份 (Identity)` 有新發現（弱點、原則），同步更新\n"
                "格式：`[YYYY-MM-DD] <做了什麼、關鍵決策、解決方法>`"
            ))]))

        # ── Penultimate-turn send_message enforcer (Gemini) ──────────────────
        # Mirror of the OpenAI loop's enforcer: for Level A (MAX_ITER=6) the
        # normal milestone (5 silent turns AND n < MAX_ITER-2) can never fire.
        if _turns_since_notify > 0 and n == MAX_ITER - 2:
            _log("⏰ MILESTONE-FINAL", f"Gemini: no send_message in {_turns_since_notify} turns, penultimate turn {n} — injecting CRITICAL send reminder")
            history.append(types.Content(role="user", parts=[types.Part(text=(
                "【CRITICAL 系統警告】你尚未向用戶發送任何回應（send_message）。\n"
                f"這是倒數第二輪（turn {n+1}/{MAX_ITER}），下一輪是最後一輪。\n"
                "你必須在下一輪立刻呼叫 mcp__evoclaw__send_message 把結果告知用戶，"
                "否則用戶將看到「處理完成，但未能產生文字回應」錯誤。\n"
                "不要再執行其他工具——把你已掌握的資訊直接發送出去。"
            ))]))
            _turns_since_notify = 0

        # P15A-FIX-9: Trim history to prevent unbounded growth while preserving
        # model(function_call) + user(function_response) pairs.  The naive slice
        # history[:2]+history[-(N-2):] can sever a model message with function_calls
        # from the following user message that carries the FunctionResponse parts,
        # which causes Gemini to reject the history with an API error.
        # After slicing, advance the tail start past any user function_response messages.
        if len(history) > _MAX_HISTORY_MESSAGES:
            _keep_fewshot = history[:2]  # few-shot pair always preserved
            _tail_start = len(history) - (_MAX_HISTORY_MESSAGES - 2)
            # Advance past any orphaned user messages that carry only function_response parts
            while _tail_start < len(history):
                _hm = history[_tail_start]
                if _hm.role == "user":
                    _hm_parts = _hm.parts or []
                    # A pure function_response user message must not be the first retained entry
                    if _hm_parts and all(getattr(p, "function_response", None) is not None for p in _hm_parts):
                        _tail_start += 1
                        continue
                break
            history = _keep_fewshot + history[_tail_start:]

    if not final_response:
        _log("⚠️ LOOP-EXHAUST", f"Gemini agent loop hit MAX_ITER={MAX_ITER} without text response — no final text collected")
    if not final_response or not final_response.strip():
        final_response = "（處理完成，但未能產生文字回應，請重新詢問。）"
    return final_response


# ── Main ──────────────────────────────────────────────────────────────────────

# Phase 1 (UnifiedClaw): Fitness reporter instance (module-level).
# Declared here (before main()) so the reference is unambiguously initialised
# when main() runs.  The old placement was after main(), which is valid Python
# (module-level code runs before any function is called) but misleading to
# readers who might assume _phase1_reporter is undefined inside main().
# P16B-FIX-17: moved declaration to before main() for clarity.
_phase1_reporter = None


def emit(obj: dict):
    """
    將結果 JSON 輸出到 stdout，用 OUTPUT_START/OUTPUT_END 標記包住。
    host 的 container_runner 會從這兩個標記之間截取 JSON。
    使用 flush=True 確保輸出立即寫入，不被 Python 的緩衝區滯留。

    p15b-fix: wrapped in try/except BrokenPipeError so that if the host has
    already closed the pipe (e.g. due to timeout) the container exits cleanly
    instead of raising an unhandled exception that would produce confusing
    traceback output on stderr.
    """
    result_text = obj.get("result") or ""
    if result_text:
        _log("📤 REPLY", result_text[:600])
    _log("📤 OUTPUT", f"{len(result_text)} chars")
    success = obj.get("status") == "success"
    _log("🏁 DONE", f"success={success}")
    try:
        print(OUTPUT_START, flush=True)
        print(json.dumps(obj), flush=True)
        print(OUTPUT_END, flush=True)
    except (BrokenPipeError, OSError):
        # Host closed the pipe (e.g. container timed out and was killed).
        # Exit quietly — container_runner already handles this case.
        pass


def main():
    """
    container 的主入口：從 stdin 讀取 JSON 輸入，執行 agent，輸出結果到 stdout。

    輸入使用 stdin JSON 而非環境變數的原因：
    - 環境變數在 /proc/self/environ、docker inspect 等地方容易洩漏
    - stdin 在 container 啟動後才讀取，其他行程無法直接觀察
    - JSON 格式讓輸入結構清晰，容易擴展新欄位

    API 金鑰從 secrets 欄位讀入後設定為環境變數，
    供 Gemini SDK 等函式庫自動讀取（它們預期從 os.environ 取得金鑰）。

    系統提示詞（system_instruction）的建立邏輯：
    先設定基本角色與工作環境資訊，再讀取 CLAUDE.md 設定檔（若存在），
    讓每個群組可以有自訂的 agent 行為設定。
    """
    _log("🚀 START", f"pid={os.getpid()}")
    # Read stdin via buffer to handle BOM (Windows Docker pipe may prepend \xef\xbb\xbf)
    raw = sys.stdin.buffer.read().decode("utf-8-sig").strip()
    try:
        inp = json.loads(raw)
    except Exception as e:
        _log("❌ ERROR", f"{type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        emit({"status": "error", "result": None, "error": "Invalid JSON input"})
        return

    # 將解析後的輸入資料存到全域變數，讓工具函式（如 tool_list_tasks）可以存取
    global _input_data, _input_chat_jid
    _input_data = inp

    prompt = inp.get("prompt", "")
    group_folder = inp.get("groupFolder", "")
    chat_jid = inp.get("chatJid", "")
    # P16B-FIX-15: warn early when critical fields are missing so the failure is
    # traceable in stderr logs rather than producing silent misbehaviour later
    # (e.g. MEMORY.md path resolving to a relative "MEMORY.md", file tools operating
    # on the wrong directory, or IPC messages having no destination JID).
    if not group_folder:
        _log("⚠️ INPUT", "groupFolder is empty — MEMORY.md tracking and file tools may malfunction. Check container_runner input_data.")
    if not chat_jid:
        _log("⚠️ INPUT", "chatJid is empty — send_message / send_file tools will fail without an explicit chat_jid argument.")
    # Store at module level so tool_send_file can auto-detect it if the LLM omits chat_jid
    _input_chat_jid = chat_jid
    secrets = inp.get("secrets", {})
    # 演化引擎注入的動態行為提示（表觀遺傳：環境感知 + 群組基因組風格）
    # 若為空字串則不添加任何附加指引
    evolution_hints = inp.get("evolutionHints", "")
    assistant_name = inp.get("assistantName", "") or "Eve"
    conversation_history = inp.get("conversationHistory", [])
    # Read sessionId passed in by the host so it can be preserved in the response.
    # This allows the host to maintain conversation continuity across container runs.
    session_id = inp.get("sessionId") or None

    messages = inp.get("conversationHistory", [])
    _log("📥 INPUT", f"jid={chat_jid} group={group_folder} msgs={len(messages)}")
    last_msg = {"role": "user", "content": prompt}
    _log("💬 MSG", str(last_msg)[:120])
    # Extract human-readable user text from the XML prompt for better log visibility
    import re as _re_log
    _xml_msgs = _re_log.findall(r'<message[^>]*>([\s\S]*?)</message>', prompt)
    _user_plain = _xml_msgs[-1].strip() if _xml_msgs else prompt.strip()
    if _user_plain:
        _log("💬 USER", _user_plain[:600])

    # 將 API 金鑰等敏感資料從 stdin JSON 設定到環境變數
    # 這樣 Gemini SDK 等依賴 os.environ 的函式庫就能自動取得
    for k, v in secrets.items():
        os.environ[k] = v

    # ── Auto-authenticate gh CLI + git credential helper ─────────────────────
    # gh auth login  → authenticates gh CLI (gh repo create, gh pr create, etc.)
    # gh auth setup-git → configures git credential helper so git push via HTTPS works
    _gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if _gh_token:
        import subprocess as _subprocess
        try:
            _gh_result = _subprocess.run(
                ["gh", "auth", "login", "--with-token"],
                input=_gh_token.encode(),
                capture_output=True,
                timeout=10,
            )
            if _gh_result.returncode == 0:
                _log("🔑 GH AUTH", "gh CLI authenticated ✓")
                # Configure git credential helper so git push/pull via HTTPS uses the token
                _subprocess.run(["gh", "auth", "setup-git"], capture_output=True, timeout=10)
                _log("🔑 GH AUTH", "git credential helper configured ✓")
                # Set git identity so commits don't fail with "Please tell me who you are"
                _subprocess.run(["git", "config", "--global", "user.email", "agent@evoclaw.local"], capture_output=True)
                _subprocess.run(["git", "config", "--global", "user.name", "EvoClaw Agent"], capture_output=True)
            else:
                _log("⚠️ GH AUTH", f"gh auth failed: {_gh_result.stderr.decode(errors='replace')[:200]}")
        except FileNotFoundError:
            _log("⚠️ GH AUTH", "gh CLI not installed in container")
        except Exception as _gh_exc:
            _log("⚠️ GH AUTH", f"gh auth error: {_gh_exc}")
    else:
        _log("⚠️ GH AUTH", "no GITHUB_TOKEN in secrets — gh CLI unauthenticated")

    # ── Dynamic tool hot-loading ──────────────────────────────────────────────
    # 從 /app/dynamic_tools/ 掛載目錄載入 Skills 安裝的 container_tools
    # 必須在 API key 設定後、LLM loop 前執行，讓工具有機會使用環境變數
    _load_dynamic_tools()

    # ── Phase 1 (UnifiedClaw): Initialize FitnessReporter ─────────────────────
    # agentId is injected by container_runner._get_agent_id() via input_data
    _phase1_agent_id = inp.get("agentId", "") or os.environ.get("AGENT_ID", "")
    if _phase1_agent_id and _REPORTER_AVAILABLE:
        try:
            import asyncio as _asyncio_phase1
            _asyncio_phase1.run(_init_fitness_reporter(_phase1_agent_id))
        except Exception as _phase1_err:
            print(f"[Phase1] FitnessReporter init error: {_phase1_err}", file=sys.stderr)

    # ── Backend selection: NIM / OpenAI-compatible takes priority ────────────────
    # Build key pools from potentially comma-separated values to support rotation
    nim_pool = _KeyPool(os.environ.get("NIM_API_KEY", ""))
    openai_pool = _KeyPool(os.environ.get("OPENAI_API_KEY", ""))
    google_pool = _KeyPool(os.environ.get("GOOGLE_API_KEY", ""))
    claude_pool = _KeyPool(os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", ""))

    nim_api_key = nim_pool.current()
    openai_api_key = openai_pool.current()
    google_api_key = google_pool.current()
    claude_api_key = claude_pool.current()

    claude_model = os.environ.get("CLAUDE_MODEL", "claude-3-5-haiku-latest")
    # Backend priority: NIM / OpenAI-compat > Claude > Gemini.
    # Rationale: NIM/OpenAI keys are assumed to be the primary production backend.
    # Claude is used only when no OpenAI-compat key is present.
    use_openai_compat = bool(nim_api_key or openai_api_key)
    use_claude = bool(claude_api_key and not use_openai_compat)

    # P16B-FIX-5: log a warning when CLAUDE_API_KEY is set but suppressed by a
    # higher-priority OpenAI-compat key.  Previously this was silent — operators
    # would set CLAUDE_API_KEY expecting Claude to be used and get NIM instead.
    if claude_api_key and use_openai_compat:
        _log("⚠️ BACKEND", "CLAUDE_API_KEY is set but suppressed because NIM_API_KEY or OPENAI_API_KEY is also present (priority: NIM/OpenAI > Claude > Gemini). Unset NIM_API_KEY/OPENAI_API_KEY to use Claude.")

    backend = "claude" if use_claude else ("openai-compat" if use_openai_compat else "gemini")

    if use_openai_compat and not _OPENAI_AVAILABLE:
        emit({"status": "error", "result": None, "error": "openai package not installed in container. Rebuild with updated requirements.txt."})
        return

    if use_claude and not _ANTHROPIC_AVAILABLE:
        emit({"status": "error", "result": None, "error": "anthropic package not installed. Rebuild container."})
        return

    if not use_openai_compat and not use_claude and not google_api_key:
        emit({"status": "error", "result": None, "error": "No API key found. Set GOOGLE_API_KEY, NIM_API_KEY, CLAUDE_API_KEY, or OPENAI_API_KEY in .env"})
        return

    if not use_openai_compat and not use_claude and not _GOOGLE_AVAILABLE:
        emit({"status": "error", "result": None, "error": "google-genai package not installed in container. Run: docker build -t evoclaw-agent:latest container/"})
        return

    if use_openai_compat:
        _active_pool = nim_pool if nim_api_key else openai_pool
        _api_key = nim_api_key or openai_api_key
        _base_url = os.environ.get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1") if nim_api_key else os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        # Use a holder list so the lambda in run_agent_openai always dereferences
        # the latest client after a key rotation swap.
        # Set explicit timeouts to prevent infinite hangs on slow/unresponsive APIs.
        # connect=15s: time to establish TCP connection
        # read=120s:   time to wait for first byte of response (LLM can be slow)
        # write=30s:   time to send the request body
        # pool=10s:    time to acquire a connection from the pool
        _openai_timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=10.0)
        _openai_client_holder: list = [OpenAIClient(base_url=_base_url, api_key=_api_key, timeout=_openai_timeout)]

        def _apply_openai_key(new_key: str) -> None:
            """Swap the OpenAI-compat client to use the rotated key."""
            _openai_client_holder[0] = OpenAIClient(base_url=_base_url, api_key=new_key, timeout=_openai_timeout)
    elif not use_claude:
        # Use a holder list so the lambda in run_agent always dereferences
        # the latest Gemini client after a key rotation swap.
        _gemini_client_holder: list = [genai.Client(api_key=google_api_key)]

        def _apply_google_key(new_key: str) -> None:
            """Swap the Gemini client to use the rotated key."""
            _gemini_client_holder[0] = genai.Client(api_key=new_key)

    if use_claude:
        # Track the active Claude client for key rotation
        _claude_client_holder: list = [anthropic.Anthropic(api_key=claude_api_key)]

        def _apply_claude_key(new_key: str) -> None:
            _claude_client_holder[0] = anthropic.Anthropic(api_key=new_key)

    _log("🔑 KEY POOL", f"google={len(google_pool)} claude={len(claude_pool)} openai={len(openai_pool)} nim={len(nim_pool)}")

    def _sanitize_prompt_injection(text: str, max_len: int = 500) -> str:
        """Remove/escape prompt injection attempts from user-controlled metadata.

        p22d-F: assistant_name, group_folder, and chat_jid come from the host
        config / DB and are not direct user input, but they can contain control
        characters or overly long strings that could disrupt the system prompt
        structure.  Strip null bytes and non-printable characters (excluding
        newline and tab) and cap length.
        """
        text = "".join(c for c in text if ord(c) >= 32 or c in "\n\t")
        return text[:max_len]

    # Sanitize metadata fields injected into the system prompt.
    safe_assistant_name = _sanitize_prompt_injection(assistant_name, max_len=100)
    safe_group_folder = _sanitize_prompt_injection(group_folder, max_len=200)
    safe_chat_jid = _sanitize_prompt_injection(chat_jid, max_len=200)

    # 建立系統提示詞：基本角色設定 + 環境資訊 + 群組自訂指令（CLAUDE.md）
    lines = [
        f"You are {safe_assistant_name}, a helpful personal AI assistant.",
        f"Your name is {safe_assistant_name}. This is your identity — do not change it.",
        "IMPORTANT IDENTITY RULES:",
        "- Never reveal that you are built on Gemini, Google AI, or any language model.",
        "- Never say 'I am a large language model', 'I am trained by Google', or similar phrases.",
        "- If asked what AI you are or who made you, simply say you are a personal assistant.",
        "- If asked your name, say your name is " + safe_assistant_name + ".",
        "- Do not discuss your underlying technology or training.",
        "Be concise, friendly, and helpful.",
        "Respond in the same language the user uses. Default to Traditional Chinese (繁體中文) unless instructed otherwise.",
        "You run inside a secure Docker container.",
        f"Working directory: {WORKSPACE}",
        f"Group folder: {WORKSPACE} (group id: {safe_group_folder})",
        f"Chat JID: {safe_chat_jid}",
        f"Date: {time.strftime('%Y-%m-%d')}",
        "",
        "## Execution Style",
        "When given a task, execute it IMMEDIATELY and DIRECTLY. Do NOT ask 'Should I start?', 'Need me to begin?', or 'Want me to proceed?'.",
        "Complete the full task using your tools, then report the result.",
        "If you hit a blocker, try to solve it yourself first. Only ask the user if truly stuck.",
        "",
        "## CRITICAL: Tool Usage Rules",
        "NEVER write bash/shell code blocks (```bash ... ```) in your response. This does NOTHING — the code will not be executed.",
        "NEVER write fake status lines like *(正在執行...)*, *(running...)*, *(executing...)*, [正在處理...] etc. — these are pure text and DO NOTHING.",
        "NEVER narrate or describe what you plan to do. Just DO it immediately by calling the appropriate tool.",
        "ALWAYS call the Bash tool directly to run any shell command. Every command you want to run MUST be a Bash tool call.",
        "If you need to run 3 commands, make 3 separate Bash tool calls (or combine them in one). Do not describe what you would do — DO IT.",
        "NEVER send a fake progress report via mcp__evoclaw__send_message unless you have ACTUALLY run tools (Bash/Read/Write/etc.) in that same turn. Fabricating progress ('I am processing...', '3 minutes remaining...') is strictly forbidden.",
        "If you are stuck or do not know how to proceed, call mcp__evoclaw__run_agent to delegate the task to a subagent instead of faking progress.",
        "",
        "## Available Tools",
        "- Bash: run shell commands (git, python, curl, etc.) — timeout 300s",
        "- Read / Write / Edit: read and modify files",
        "- Glob: find files by pattern (e.g. '**/*.py')",
        "- Grep: search file contents by regex",
        "- WebFetch: fetch any URL and read its content",
        "- mcp__evoclaw__send_message: send a message to the user",
        "- mcp__evoclaw__schedule_task / list_tasks / cancel_task / pause_task / resume_task: manage scheduled tasks",
        "- mcp__evoclaw__run_agent: spawn a subagent to handle a subtask and return its result",
        "",
    ]

    # ── 靈魂規則 (Soul Rules) — 從 soul.md 讀取 ──────────────────────────────
    # soul.md 與 agent.py 同目錄，更新靈魂規則只需編輯該檔案，無需動 Python code。
    # {{GROUP_FOLDER}} 為執行時替換的佔位符，指向此群組的資料目錄。
    # VALIDATION: soul.md must exist and be non-empty — it contains the core
    # anti-hallucination rules.  A missing or empty soul.md means the agent
    # will run WITHOUT honesty constraints, so we log a CRITICAL warning and
    # inject a minimal fallback rather than silently continuing.
    _SOUL_MAX_CHARS = 32_000  # warn if soul.md exceeds this (context-window risk)
    _soul_path = Path(__file__).parent / "soul.md"
    if not _soul_path.exists():
        _log("🚨 CRITICAL", f"soul.md NOT FOUND at {_soul_path} — anti-hallucination rules are MISSING. "
             "The agent will run without soul constraints. Fix immediately.")
        lines.append("\n你是 EvoClaw，一個智能助理。請誠實、準確地回應，不要編造資訊。不能假裝執行工具或假造結果。")
    else:
        try:
            _soul_text = _soul_path.read_text(encoding="utf-8").strip()
            if not _soul_text:
                _log("🚨 CRITICAL", f"soul.md at {_soul_path} is EMPTY — anti-hallucination rules are MISSING. "
                     "The agent will run without soul constraints. Fix immediately.")
                lines.append("\n你是 EvoClaw，一個智能助理。請誠實、準確地回應，不要編造資訊。不能假裝執行工具或假造結果。")
            else:
                if len(_soul_text) > _SOUL_MAX_CHARS:
                    _log("⚠️ SOUL", f"soul.md is very large ({len(_soul_text)} chars > {_SOUL_MAX_CHARS}) — "
                         "consider trimming to avoid consuming excessive context-window tokens.")
                # BUG-FIX #424: group_folder is the folder *name* (e.g. "telegram_8259652816"),
                # not a container filesystem path.  The group is mounted at WORKSPACE (/workspace/group).
                # Replace {{GROUP_FOLDER}} with the actual container path so soul.md instructions
                # referencing files use the correct absolute path.
                _soul_text = _soul_text.replace("{{GROUP_FOLDER}}", WORKSPACE)
                lines.append("")
                lines.append(_soul_text)
                _log("🧠 SOUL", f"Injected soul.md ({len(_soul_text)} chars)")
        except Exception as _soul_err:
            _log("🚨 CRITICAL", f"Failed to read soul.md: {_soul_err} — anti-hallucination rules are MISSING.")
            lines.append("\n你是 EvoClaw，一個智能助理。請誠實、準確地回應，不要編造資訊。不能假裝執行工具或假造結果。")

    # ── MEMORY.md 啟動注入（長期記憶 + 身份）────────────────────────────────────
    # 每次 session 啟動時讀取 MEMORY.md，注入為「長期記憶」section。
    # 智慧分割：身份區段永遠完整保留，任務記錄取最後 3000 字元（防止截斷身份）。
    # 若缺少身份區段 → 注入模板 + 填寫指令（身份引導 Bootstrap）。
    # BUG-FIX #424: group_folder is the folder *name* (e.g. "telegram_8259652816"), NOT a
    # container filesystem path.  The group folder is mounted at WORKSPACE (/workspace/group).
    # Using Path(group_folder) produced a relative path like "telegram_8259652816/MEMORY.md"
    # which resolved to /app/telegram_8259652816/MEMORY.md — never found → memory always empty.
    _memory_path = Path(WORKSPACE) / "MEMORY.md"
    _IDENTITY_MARKER = "## 身份 (Identity)"
    _TASK_MARKER = "## 任務記錄 (Task Log)"
    _MEMORY_READ_LIMIT = 512 * 1024  # 512 KB — prevent huge MEMORY.md from blowing context window
    if _memory_path.exists():
        try:
            _mem_size = _memory_path.stat().st_size
            if _mem_size > _MEMORY_READ_LIMIT:
                _log("⚠️ MEMORY", f"MEMORY.md is large ({_mem_size} bytes) — reading only last {_MEMORY_READ_LIMIT} bytes")
                with _memory_path.open("rb") as _mf:
                    _mf.seek(max(0, _mem_size - _MEMORY_READ_LIMIT))
                    _memory_content = _mf.read().decode("utf-8", errors="replace").strip()
            else:
                # BUG-P26B-2: use errors="replace" so a MEMORY.md file that
                # contains null bytes or Latin-1 characters (e.g. from filesystem
                # corruption) does not raise UnicodeDecodeError and silently drop
                # the entire long-term memory injection.
                _memory_content = _memory_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as _mem_read_err:
            _log("⚠️ MEMORY", f"Failed to read MEMORY.md: {_mem_read_err}")
            _memory_content = ""
        if _memory_content:
            # 智慧分割：保留完整身份 + task log 最後 3000 字元
            if _IDENTITY_MARKER in _memory_content and _TASK_MARKER in _memory_content:
                _id_end = _memory_content.index(_TASK_MARKER)
                _identity_part = _memory_content[:_id_end].strip()
                _task_part = _memory_content[_id_end:][-3000:]
                _memory_snippet = _identity_part + "\n\n" + _task_part
            else:
                _memory_snippet = _memory_content[-4000:]
            lines.append("")
            lines.append(
                f"## 長期記憶 (MEMORY.md)\n"
                f"⚠️ **重要：以下為過去 session 記錄的歷史記憶。這些是歷史筆記，不是已確認的事實。**\n"
                f"**請在引用任何記憶內容之前，先透過實際工具（Read/Bash）重新驗證，切勿直接當作已完成的事實陳述。**\n\n"
                f"{_memory_snippet}"
            )
            _log("🧠 MEMORY", f"Injected {len(_memory_snippet)} chars from MEMORY.md")
            # 身份引導：若缺少身份區段，提示建立
            if _IDENTITY_MARKER not in _memory_content:
                lines.append(
                    f"⚠️ 身份引導：你的 MEMORY.md 尚未建立 `{_IDENTITY_MARKER}` 區段。"
                    f"請在本 session 完成主要任務後，在 {_memory_path} 開頭建立身份區段（格式見 soul.md 的 ### 自我認知）。"
                )
    else:
        # 第一次 session：提示建立 MEMORY.md 與身份區段
        lines.append(
            f"⚠️ 身份引導：這是你的第一次 session，尚無長期記憶。"
            f"請在完成主要任務後，建立 {_memory_path} 並填寫身份資料（格式見 soul.md 的 ### 自我認知）。"
        )

    # ── Level B 啟發式偵測（代碼層面輔助分類）+ 動態 MAX_ITER ─────────────────
    # 根據 prompt 長度 + 關鍵字分析任務複雜度，動態設定 MAX_ITER：
    #   Level A（簡單問答）：MAX_ITER = 6  — 減少幻覺迴圈機會
    #   Level B（複雜任務）：MAX_ITER = 20 — 足夠完成多步驟任務
    # 透過環境變數 MAX_ITER 可覆蓋此設定（用於測試或特殊需求）
    _LEVEL_B_KEYWORDS = [
        "debug", "修復", "fix", "配置", "configure", "install", "安裝",
        "optimize", "優化", "implement", "實作", "refactor", "重構",
        "analyze", "分析", "deploy", "部署", "multi-step", "step by step",
        "system", "系統", "migrate", "migration", "architecture", "架構",
        "寫", "write", "create", "建立", "generate", "產生", "整理", "總結",
        "搜尋", "search", "找", "查", "git", "docker", "python", "code",
        # P16B-FIX-9: added missing Level B keywords that indicate multi-step tasks
        # requiring more than 3 tool calls (would be misclassified as Level A = 6 iter).
        "report", "報告", "schedule", "排程", "plan", "計劃", "計畫",
        "test", "測試", "review", "審查", "audit", "稽核", "monitor", "監控",
        "automate", "自動化", "integrate", "整合", "pipeline", "流程",
        "compare", "比較", "summarize", "summarise", "摘要",
        "npm", "pip", "yarn", "cargo", "make", "cmake", "gradle",
        # Repo / file inspection tasks — need Glob+Read, classify as Level B
        "repo", "repository", "倉庫", "更新", "有沒有", "有沒", "檢查", "check",
        "看看", "看一下", "有什麼", "列出", "list", "show", "顯示",
    ]
    _prompt_lower = prompt.lower() if prompt else ""
    _is_level_b = (
        len(prompt or "") > 150 or
        any(kw in _prompt_lower for kw in _LEVEL_B_KEYWORDS)
    )
    # Dynamic MAX_ITER: env override takes priority, then complexity-based default
    _env_max_iter = os.environ.get("MAX_ITER")
    if _env_max_iter:
        try:
            _dynamic_max_iter = int(_env_max_iter)
        except ValueError:
            _log("⚠️ MAX_ITER", f"Invalid MAX_ITER env value '{_env_max_iter}' — using dynamic default")
            _dynamic_max_iter = 20 if _is_level_b else 6
    else:
        _dynamic_max_iter = 20 if _is_level_b else 6
    _log("🔢 MAX_ITER", f"{_dynamic_max_iter} ({'Level B' if _is_level_b else 'Level A'}, prompt_len={len(prompt or '')})")

    # Qwen 397B 推理較慢且容易陷入 reasoning loop，降低 MAX_ITER
    _model_for_check = os.environ.get("NIM_MODEL") or os.environ.get("OPENAI_MODEL") or ""
    if _is_qwen_model(_model_for_check) and not _env_max_iter:
        _prev = _dynamic_max_iter
        _dynamic_max_iter = min(_dynamic_max_iter, 12 if _is_level_b else 5)
        if _prev != _dynamic_max_iter:
            _log("🧠 QWEN-ITER", f"Reduced MAX_ITER {_prev}→{_dynamic_max_iter} for Qwen model")

    if _is_level_b:
        lines.append("")
        lines.append(
            "⚠️ 系統預分析：本任務可能屬於 Level B（複雜任務）。"
            "請在開始前評估是否需要使用 mcp__evoclaw__run_agent 委派給子代理。"
        )
        _log("🧠 LEVEL-B", f"Heuristic detected Level B task (len={len(prompt or '')}, keywords match={_is_level_b})")

    # 讀取全域和群組專屬的 CLAUDE.md 設定（若存在），附加到系統提示詞末尾
    # 全域 CLAUDE.md 提供所有群組共用的指令；群組 CLAUDE.md 提供群組專屬設定
    for claude_md in ["/workspace/global/CLAUDE.md", "/workspace/group/CLAUDE.md"]:
        if Path(claude_md).exists():
            try:
                lines.append("")
                lines.append(Path(claude_md).read_text(encoding="utf-8"))
            except Exception as _cmd_err:
                _log("⚠️ CLAUDE.MD", f"Failed to read {claude_md}: {_cmd_err} — skipping")

    # 演化引擎提示：附加在所有靜態設定之後（表觀遺傳，動態覆蓋）
    # 格式：\n\n---\n[環境自動調整提示...] 或 [群組偏好...]
    # 這些提示每次 container 啟動時都可能不同，反映當下的環境狀態
    # SECURITY: strip any evolution_hints content that attempts to override the
    # soul.md honesty rules (e.g. "ignore previous instructions", "pretend you
    # completed", "you may skip tools", "override soul").  These patterns should
    # never appear in legitimate evolution_hints produced by the GA engine, but
    # a compromised or misconfigured hint could otherwise silently bypass the
    # anti-hallucination rules established earlier in the system prompt.
    #
    # NOTE: evolution_hints are appended AFTER soul.md in the system prompt, so
    # a malicious hint that says "disregard previous rules" could in theory
    # override the soul rules. The filter below blocks the known bypass patterns.
    # Legitimate GA-produced hints should only contain style/tone preferences,
    # language settings, or group-specific behavioural nudges — never honesty
    # overrides. When in doubt, strip the hint and log SECURITY.
    if evolution_hints:
        import re as _re_hints
        _BYPASS_PATTERNS = _re_hints.compile(
            # English prompt-injection patterns
            r'ignore\s+(?:all\s+)?(?:previous|prior|above|your)\s+instructions?'
            r'|disregard\s+(?:all\s+)?(?:previous|prior|above|your)\s+instructions?'
            r'|forget\s+(?:your\s+)?(?:rules?|instructions?|guidelines?|soul)'
            r'|override\s+(?:soul|honesty|rules?|instructions?)'
            r'|you\s+may\s+(?:skip|bypass|ignore|omit)\s+tools?'
            r'|pretend\s+(?:you\s+)?(?:completed?|finished?|done|executed?|ran)'
            r'|act\s+as\s+if\s+(?:you\s+)?(?:completed?|finished?|done|executed?)'
            r'|(?:skip|bypass|omit)\s+(?:tool\s+calls?|verification|checking)'
            r'|(?:claim|say|report|tell)\s+(?:the\s+)?(?:task\s+)?(?:is\s+)?(?:done|complete|finished|success)'
            r'\s+without'
            # Chinese prompt-injection patterns
            r'|(?:忽略|無視|跳過|忘記)\s*(?:之前|先前|上面|所有)?\s*(?:規則|指令|要求|設定|靈魂)'
            r'|(?:假裝|偽裝|假設)\s*(?:已經)?\s*(?:完成|執行|成功|做完)'
            r'|(?:跳過|省略)\s*(?:工具|呼叫|驗證|確認)'
            r'|(?:覆蓋|覆寫|取代)\s*(?:soul|靈魂|規則|誠實)',
            _re_hints.IGNORECASE | _re_hints.DOTALL,
        )
        if _BYPASS_PATTERNS.search(evolution_hints):
            _log("🚨 SECURITY", "evolution_hints contains soul-bypass pattern — stripping hints")
            evolution_hints = ""
        else:
            lines.append(evolution_hints)

    system_instruction = "\n".join(lines)

    # Log system prompt for container log visibility
    _log("📋 SYSTEM", f"{len(system_instruction)} chars | {len(lines)} lines")
    # Log first 800 chars in chunks so each line is readable
    _sys_preview = system_instruction[:800]
    for _sys_line in _sys_preview.split('\n'):
        if _sys_line.strip():
            _log("📋", _sys_line[:120])

    # Log conversation history summary
    _hist_count = len(conversation_history) if conversation_history else 0
    _log("📚 HISTORY", f"{_hist_count} turns")
    if conversation_history:
        for _hmsg in conversation_history[-3:]:  # last 3 turns
            _hrole = str(_hmsg.get('role', '?')).upper()
            _hcontent = str(_hmsg.get('content', ''))
            _log(f"📚 [{_hrole}]", _hcontent[:200])

    # Qwen 特化：注入中文遵從規則，避免推理迴圈
    if _is_qwen_model(_model_for_check):
        _qwen_rules = (
            "\n## Qwen 特化規則（最高優先）\n"
            "- 思考限制：不超過 200 字就必須行動，不要過度推理\n"
            "- 禁止假設：工具執行後必須等待結果，不能假設成功\n"
            "- 失敗承認：承認不確定比編造進度更好\n"
            "- 工具呼叫：遇到困難優先呼叫工具，不要空想\n"
            "- 禁止假狀態：*(正在執行...)*、【已完成】等格式完全禁止\n"
        )
        system_instruction = system_instruction + _qwen_rules
        _log("🧠 QWEN-PROMPT", f"Injected {len(_qwen_rules)} chars Qwen-specific rules")

    try:
        if use_openai_compat:
            # P16B-FIX-4: do NOT fall back to GEMINI_MODEL for an OpenAI-compat client.
            # Passing a Gemini model name (e.g. "gemini-2.0-flash") to an OpenAI-compat
            # endpoint always fails with 404/model-not-found.  Use only NIM/OpenAI model
            # env vars; if neither is set, fall back to the NIM default instruct model.
            _model = os.environ.get("NIM_MODEL") or os.environ.get("OPENAI_MODEL") or "meta/llama-3.3-70b-instruct"
            _log("🤖 MODEL", f"openai-compat/{_model}")
            result = run_agent_openai(_openai_client_holder, system_instruction, prompt, chat_jid, _model, conversation_history, pool=_active_pool, apply_key_fn=_apply_openai_key, group_folder=group_folder, max_iter=_dynamic_max_iter)
        elif use_claude:
            _log("🤖 MODEL", f"claude/{claude_model}")
            # BUG-FIX: pass group_folder so MEMORY.md detection uses the correct path
            result = run_agent_claude(_claude_client_holder, claude_model, system_instruction, prompt, chat_jid, conversation_history, pool=claude_pool, apply_key_fn=_apply_claude_key, max_iter=_dynamic_max_iter, group_folder=group_folder)
        else:
            _gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
            _log("🤖 MODEL", f"gemini/{_gemini_model}")
            result = run_agent(_gemini_client_holder, system_instruction, prompt, chat_jid, assistant_name, conversation_history, pool=google_pool, apply_key_fn=_apply_google_key, max_iter=_dynamic_max_iter, group_folder=group_folder)
        # 若 agent 已透過 mcp__evoclaw__send_message 工具主動發送訊息，
        # 則清空 result 欄位，避免 host 的 container_runner 再次發送（雙重訊息 + 超長訊息 bug）
        # 若 agent 沒有呼叫工具（純文字回覆），則由 host 負責發送 result
        #
        # BUG-FIX: the old ternary was: result if result and result.strip() else
        # ("" if _messages_sent_via_tool else result).  When result is None and
        # _messages_sent_via_tool is falsy this evaluates to None, not "".  The
        # subsequent `not emit_result` guard handles None correctly but the
        # semantics are confusing.  Rewrite as an explicit two-step assignment so
        # the intent is unambiguous and None is never propagated downstream.
        _result_text = (result or "").strip()
        if _result_text and not _messages_sent_via_tool:
            # Agent produced text AND did not send via tool — let host deliver it.
            emit_result = _result_text
        elif _messages_sent_via_tool:
            # Agent already delivered content via send_message tool — clear result
            # so the host does not double-send.
            emit_result = ""
        else:
            # Neither text output nor tool messages — will be caught by fallback below.
            emit_result = ""
        # Guard: if the agent loop produced no output at all (empty result, no tool messages),
        # emit a minimal fallback so the user never sees pure silence.
        if not emit_result and not _messages_sent_via_tool:
            _log("⚠️ EMPTY-RESULT", "Agent loop returned no output and sent no tool messages — emitting fallback")
            emit_result = "（系統：處理完成，但未產生回應，請重試。）"
        # BUG-FIX: report fitness after a successful run so the evolution engine
        # actually receives quality data.  Previously _report_fitness() was defined
        # but never called, leaving fitness scores perpetually unupdated.
        # Score heuristic: non-empty substantive result = 0.9, generic fallback = 0.3.
        if _REPORTER_AVAILABLE and _phase1_reporter:
            try:
                import asyncio as _asyncio_report
                # P16B-FIX-10: when the agent delivered output via send_message tool,
                # emit_result is "" (to prevent double-send) but the agent DID succeed.
                # Treat any run where tool messages were sent as a successful interaction
                # (score 0.9) even if emit_result is empty.  Only fall back to 0.3 when
                # NEITHER emit_result NOR tool messages produced any real content.
                _FALLBACK_STRINGS = frozenset([
                    "（處理完成，但未能產生文字回應，請重新詢問。）",
                    "（系統：處理完成，但未產生回應，請重試。）",
                ])
                _has_real_output = bool(_messages_sent_via_tool) or (
                    emit_result and emit_result not in _FALLBACK_STRINGS
                )
                _fitness_score = 0.9 if _has_real_output else 0.3
                _asyncio_report.run(_report_fitness(_fitness_score, {"has_tool_calls": bool(_messages_sent_via_tool)}))
            except Exception as _fit_err:
                _log("⚠️ FITNESS", f"fitness report failed: {_fit_err}")
        # Preserve the incoming sessionId so the host can track conversation continuity.
        # Only fall back to generating a new UUID if no sessionId was provided.
        preserved_session_id = session_id if session_id else str(uuid.uuid4())
        emit({"status": "success", "result": emit_result, "newSessionId": preserved_session_id})
    except Exception as e:
        _log("❌ ERROR", f"{type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        # Emit a structured error so container_runner can surface it to the user via on_error
        emit({"status": "error", "result": None, "error": f"{type(e).__name__}: {e}"})



async def _init_fitness_reporter(agent_id: str) -> object:
    """Initialize and connect FitnessReporter (Phase 1). No-op if unavailable."""
    global _phase1_reporter
    if not _REPORTER_AVAILABLE:
        return None
    reporter = _FitnessReporter(agent_id=agent_id)
    connected = await reporter.connect()
    if connected:
        print(f"[Phase1] FitnessReporter connected for agent: {agent_id}")
    else:
        print(f"[Phase1] FitnessReporter: WSBridge unavailable, using file IPC")
    _phase1_reporter = reporter
    return reporter

async def _report_fitness(score: float, metadata: dict = None):
    """Report fitness score to Gateway evolution engine (Phase 1)."""
    if _phase1_reporter and getattr(_phase1_reporter, 'connected', False):
        await _phase1_reporter.report_fitness(score=score, metadata=metadata or {})

if __name__ == "__main__":
    main()


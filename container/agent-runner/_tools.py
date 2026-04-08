"""Tool implementations for the EvoClaw agent runner."""
import json, os, sys, subprocess, time, random, string, glob as _glob_module
import urllib.request, urllib.error, html.parser, traceback, uuid
from pathlib import Path

from _constants import (
    IPC_MESSAGES_DIR, IPC_TASKS_DIR, IPC_RESULTS_DIR, WORKSPACE,
    _ALLOWED_PATH_PREFIXES, _MAX_TOOL_RESULT_CHARS,
)
from _utils import _log, _atomic_ipc_write, _check_path_allowed, _write_ipc_file, _SSRF_PATCH_LOCK

import _constants

# 追蹤 agent 是否在 agentic loop 中已呼叫過 send_message 工具
# 每次 Docker 啟動都是全新 process，此 flag 只有一次生命週期
# 用途：避免 host 讀取 result 欄位時重複發送（雙重訊息 bug）
_messages_sent_via_tool: list = []

# stdin 解析後的完整輸入資料，main() 初始化後供工具函式存取
_input_data: dict = {}


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
        # Issue #444: use _write_ipc_file helper (deduplicates mkdir+uid+atomic-write pattern)
        fname = _write_ipc_file(IPC_TASKS_DIR, {"type": "cancel_task", "task_id": task_id}, suffix="cancel")
        _log("📨 IPC", f"type=cancel_task → {fname.name}")
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
        # Issue #444: use _write_ipc_file helper
        fname = _write_ipc_file(IPC_TASKS_DIR, {"type": "pause_task", "task_id": task_id}, suffix="pause")
        _log("📨 IPC", f"type=pause_task → {fname.name}")
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
        # Issue #444: use _write_ipc_file helper
        fname = _write_ipc_file(IPC_TASKS_DIR, {"type": "resume_task", "task_id": task_id}, suffix="resume")
        _log("📨 IPC", f"type=resume_task → {fname.name}")
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

        Path(IPC_RESULTS_DIR).mkdir(parents=True, exist_ok=True)

        # Issue #444: use _write_ipc_file helper (deduplicates mkdir+uid+atomic-write)
        fname = _write_ipc_file(IPC_TASKS_DIR, {
            "type": "spawn_agent",
            "requestId": request_id,
            "prompt": prompt,
            "context_mode": context_mode,
        }, suffix="spawn")
        _log("📨 IPC", f"type=spawn_agent → {fname.name}")

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
    # Auto-detect chat_jid from input if not explicitly provided by the LLM
    effective_jid = chat_jid or _constants._input_chat_jid or ""
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
    effective_jid = chat_jid or _constants._input_chat_jid or ""
    if not effective_jid:
        return "Error: chat_jid not provided and not available from input"
    try:
        # Issue #444: use _write_ipc_file helper
        fname = _write_ipc_file(IPC_TASKS_DIR, {
            "type": "start_remote_control",
            "jid": effective_jid,
            "sender": sender,
        }, suffix="remote-control")
        _log("📨 IPC", f"type=start_remote_control jid={effective_jid} → {fname.name}")
        return "Remote control session requested — URL will be sent to this chat shortly (up to 30s)."
    except Exception as exc:
        return f"Error: {exc}"


def tool_self_update(chat_jid: str = "") -> str:
    """Request the host to pull the latest EvoClaw code from git and restart.
    The host will run `git pull` + `pip install -e .` then restart via os.execv()."""
    effective_jid = chat_jid or _constants._input_chat_jid or ""
    try:
        # Issue #444: use _write_ipc_file helper
        fname = _write_ipc_file(IPC_TASKS_DIR, {
            "type": "self_update",
            "jid": effective_jid,
        }, suffix="self-update")
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
        #
        # Issue #445 (thread-safety): socket.create_connection is a module-level
        # global.  Without a lock, two concurrent tool_web_fetch calls could race:
        # thread A sets the patched fn, thread B restores the original, then thread
        # A's fetch runs without SSRF protection.  Serialise via _SSRF_PATCH_LOCK
        # (defined in _utils.py) so only one fetch holds the patch at a time.
        with _SSRF_PATCH_LOCK:
            _socket_rebind.create_connection = _safe_create_connection
            try:
                _fetch_ctx = _opener.open(req, timeout=30)
            finally:
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

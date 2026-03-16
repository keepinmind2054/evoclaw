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
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False
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


def _log(tag: str, msg: str = "") -> None:
    """Structured stderr logging with millisecond timestamps."""
    ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {tag} {msg}", file=sys.stderr, flush=True)


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

    timeout=60 秒：防止指令無限期阻塞（例如 git clone 或 npm install 過慢）。
    shell=True：讓指令支援管線（|）、重導向（>）等 shell 特性。
    同時回傳 stderr 讓 Gemini 能看到錯誤訊息並自行修正。
    """
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True, text=True,
            timeout=300, cwd=WORKSPACE,
            shell=False  # safer: exec bash directly, not via /bin/sh -c
        )
        out = result.stdout
        if result.stderr:
            out += f"\nSTDERR:\n{result.stderr}"
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 300s"
    except Exception as e:
        return f"Error: {e}"


def tool_read(file_path: str) -> str:
    """讀取指定路徑的文字檔案內容，讓 agent 可以檢視檔案。"""
    err = _check_path_allowed(file_path)
    if err:
        return err
    try:
        return Path(file_path).read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading file: {e}"


def tool_write(file_path: str, content: str) -> str:
    """
    將內容寫入指定路徑的檔案。
    自動建立不存在的父目錄（mkdir -p），簡化 agent 的操作步驟。
    """
    err = _check_path_allowed(file_path)
    if err:
        return err
    try:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written: {file_path}"
    except Exception as e:
        return f"Error writing file: {e}"


def tool_edit(file_path: str, old_string: str, new_string: str) -> str:
    """
    在檔案中找到 old_string 並替換為 new_string（只替換第一個出現的位置）。
    若 old_string 不存在則回傳錯誤，讓 Gemini 知道需要先確認內容再修改。
    """
    err = _check_path_allowed(file_path)
    if err:
        return err
    try:
        p = Path(file_path)
        content = p.read_text(encoding="utf-8")
        if old_string not in content:
            return f"Error: old_string not found in {file_path}"
        # replace(..., 1) 確保只替換第一個出現的位置，避免意外修改多處
        p.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
        return f"Edited: {file_path}"
    except Exception as e:
        return f"Error editing file: {e}"


def tool_send_message(chat_jid: str, text: str, sender: str = None) -> str:
    """
    透過 IPC 機制將訊息發送給用戶（寫入 JSON 檔案，host 的 ipc_watcher 負責實際傳送）。

    檔名格式：{timestamp_ms}-{random_8_chars}.json
    使用時間戳記前綴確保 ipc_watcher 按 FIFO 順序處理；
    加入隨機後綴避免同一毫秒內產生多個檔案時發生名稱衝突。
    """
    try:
        Path(IPC_MESSAGES_DIR).mkdir(parents=True, exist_ok=True)
        uid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        fname = Path(IPC_MESSAGES_DIR) / f"{int(time.time()*1000)}-{uid}.json"
        payload = {"type": "message", "chatJid": chat_jid, "text": text}
        if sender:
            payload["sender"] = sender  # 可選的發送者名稱（顯示為不同的 bot 身份）
        fname.write_text(json.dumps(payload), encoding="utf-8")
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
        Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
        fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}.json"
        fname.write_text(json.dumps({
            "type": "schedule_task",
            "prompt": prompt,
            "schedule_type": schedule_type,   # "cron", "interval", 或 "once"
            "schedule_value": schedule_value,  # cron 表達式、毫秒數、或 ISO 時間字串
            "context_mode": context_mode,      # "group" 或 "isolated"
            "chatJid": chat_jid,              # 群組 JID，讓 ipc_watcher 存入 DB 供排程器路由使用
        }), encoding="utf-8")
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
    """
    if not task_id:
        return "Error: task_id is required."
    try:
        Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
        fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}-cancel.json"
        fname.write_text(json.dumps({
            "type": "cancel_task",
            "task_id": task_id,
        }), encoding="utf-8")
        return f"Task {task_id} cancellation request sent."
    except Exception as e:
        return f"Error: {e}"


def tool_pause_task(task_id: str) -> str:
    """透過 IPC 暫停指定 ID 的排程任務（status 改為 paused）。"""
    if not task_id:
        return "Error: task_id is required."
    try:
        Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
        fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}-pause.json"
        fname.write_text(json.dumps({
            "type": "pause_task",
            "task_id": task_id,
        }), encoding="utf-8")
        return f"Task {task_id} pause request sent."
    except Exception as e:
        return f"Error: {e}"


def tool_resume_task(task_id: str) -> str:
    """透過 IPC 恢復指定 ID 的已暫停排程任務（status 改回 active）。"""
    if not task_id:
        return "Error: task_id is required."
    try:
        Path(IPC_TASKS_DIR).mkdir(parents=True, exist_ok=True)
        fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}-resume.json"
        fname.write_text(json.dumps({
            "type": "resume_task",
            "task_id": task_id,
        }), encoding="utf-8")
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

        fname = Path(IPC_TASKS_DIR) / f"{int(time.time()*1000)}-spawn.json"
        fname.write_text(json.dumps({
            "type": "spawn_agent",
            "requestId": request_id,
            "prompt": prompt,
            "context_mode": context_mode,
        }), encoding="utf-8")

        # Poll for result (up to 300 seconds)
        output_path = Path(IPC_RESULTS_DIR) / f"{request_id}.json"
        for _ in range(300):
            if output_path.exists():
                try:
                    data = json.loads(output_path.read_text(encoding="utf-8"))
                    output_path.unlink(missing_ok=True)
                    return data.get("output", "(no output)")
                except Exception as e:
                    return f"Error reading subagent result: {e}"
            time.sleep(1)

        return "Error: subagent timed out after 300s"
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
    msg_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _log("📎 FILE", f"path={file_path} exists={os.path.exists(file_path)}")
    _log("📨 IPC", f"type=send_file → {msg_file.name}")
    return f"✅ File queued: {os.path.basename(file_path)}"


def tool_glob(pattern: str, path: str = WORKSPACE) -> str:
    """
    在指定目錄下尋找符合 glob 模式的檔案（支援 ** 遞迴搜尋）。
    例如 pattern="**/*.py" 可找出所有 Python 檔案。
    """
    try:
        search_path = os.path.join(path, pattern)
        matches = _glob_module.glob(search_path, recursive=True)
        if not matches:
            return f"No files found matching: {pattern} in {path}"
        return "\n".join(sorted(matches))
    except Exception as e:
        return f"Error: {e}"


def tool_grep(pattern: str, path: str = WORKSPACE, include: str = "*") -> str:
    """
    在指定目錄下遞迴搜尋符合正規表達式的檔案內容，回傳「檔名:行號:內容」格式。
    include 參數可過濾副檔名，例如 include="*.py" 只搜尋 Python 檔案。
    """
    try:
        result = subprocess.run(
            ["grep", "-r", "-n", "--include", include, pattern, path],
            capture_output=True, text=True, timeout=30
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
    結果最多回傳 12000 字元，超過部分截斷。
    """
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; EvoClaw-Agent/1.0)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            }
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8", errors="replace")

        if "html" in content_type.lower() or raw.lstrip().startswith("<"):
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

        if len(text) > 12000:
            text = text[:12000] + "\n\n... (content truncated)"
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
TOOL_DECLARATIONS = [
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
]



# ── OpenAI-compatible tool declarations ───────────────────────────────────────

OPENAI_TOOL_DECLARATIONS = [
    {"type": "function", "function": {"name": "Bash", "description": "Execute a bash command in /workspace/group.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to run"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "Read", "description": "Read a file from the filesystem.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to the file"}}, "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "Write", "description": "Write content to a file.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to write to"}, "content": {"type": "string", "description": "File content"}}, "required": ["file_path", "content"]}}},
    {"type": "function", "function": {"name": "Edit", "description": "Find and replace a string in a file.", "parameters": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Path to the file"}, "old_string": {"type": "string", "description": "Exact text to replace"}, "new_string": {"type": "string", "description": "Replacement text"}}, "required": ["file_path", "old_string", "new_string"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__send_message", "description": "Send a message to the user in the chat.", "parameters": {"type": "object", "properties": {"text": {"type": "string", "description": "Message text"}, "sender": {"type": "string", "description": "Optional bot name"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__schedule_task", "description": "Schedule a recurring or one-time task.", "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "What to do when task runs"}, "schedule_type": {"type": "string", "description": "cron, interval, or once"}, "schedule_value": {"type": "string", "description": "Cron expr, ms, or ISO timestamp"}, "context_mode": {"type": "string", "description": "group or isolated"}}, "required": ["prompt", "schedule_type", "schedule_value"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__list_tasks", "description": "List all scheduled tasks for this group.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__cancel_task", "description": "Cancel (delete) a scheduled task by its ID.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to cancel"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__pause_task", "description": "Pause a scheduled task.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__resume_task", "description": "Resume a paused scheduled task.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {"name": "Glob", "description": "Find files matching a glob pattern (supports ** recursive).", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "Grep", "description": "Search file contents with regex. Returns filename:line:content.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "WebFetch", "description": "Fetch a URL and return its content as plain text.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__run_agent", "description": "Spawn a subagent in an isolated Docker container to handle a subtask. Blocks until complete (up to 300s) and returns its output.", "parameters": {"type": "object", "properties": {"prompt": {"type": "string", "description": "The task for the subagent"}, "context_mode": {"type": "string", "description": "isolated or group"}}, "required": ["prompt"]}}},
    {"type": "function", "function": {"name": "mcp__evoclaw__send_file", "description": "Send a file to the user. Write the file to /workspace/group/output/ first, then call this tool.", "parameters": {"type": "object", "properties": {"chat_jid": {"type": "string", "description": "The chat JID to send the file to"}, "file_path": {"type": "string", "description": "Absolute container path to the file"}, "caption": {"type": "string", "description": "Optional caption"}}, "required": ["file_path"]}}},
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
    {"name": "mcp__evoclaw__list_tasks", "description": "List all scheduled tasks for this group.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "mcp__evoclaw__cancel_task", "description": "Cancel (delete) a scheduled task by its ID.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "mcp__evoclaw__pause_task", "description": "Pause a scheduled task.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "mcp__evoclaw__resume_task", "description": "Resume a paused scheduled task.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "Glob", "description": "Find files matching a glob pattern (supports ** recursive).", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "Grep", "description": "Search file contents with regex. Returns filename:line:content.", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "WebFetch", "description": "Fetch a URL and return its content as plain text.", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "mcp__evoclaw__run_agent", "description": "Spawn a subagent in an isolated Docker container to handle a subtask. Blocks until complete (up to 300s) and returns its output.", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string", "description": "The task for the subagent"}, "context_mode": {"type": "string", "description": "isolated or group"}}, "required": ["prompt"]}},
    {"name": "mcp__evoclaw__send_file", "description": "Send a file to the user. Write the file to /workspace/group/output/ first, then call this tool.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string", "description": "The chat JID to send the file to"}, "file_path": {"type": "string", "description": "Absolute container path to the file"}, "caption": {"type": "string", "description": "Optional caption"}}, "required": ["file_path"]}},
]


def run_agent_claude(client_holder, model: str, system_instruction: str, user_message: str, chat_jid: str, conversation_history: list = None, pool: "_KeyPool | None" = None, apply_key_fn=None) -> str:
    """
    Anthropic Claude agentic loop.
    client_holder: a one-element list [client] so key rotation can swap the client mid-loop.
    conversation_history: 最近的對話記錄，以原生 multi-turn 格式注入。
    pool/apply_key_fn: optional key pool for automatic rotation on rate-limit errors.
    """
    messages = []
    # 注入對話歷史（原生 multi-turn 格式）
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            text = msg.get("content", "")
            if text:
                messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": user_message})
    MAX_ITER = 30
    final_response = ""

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
            final_response = " ".join(
                block.text for block in response.content
                if hasattr(block, "text")
            )
            break

        if response.stop_reason != "tool_use":
            break

        # Execute all tool calls
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = execute_tool(block.name, block.input, chat_jid)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if not tool_results:
            break

        messages.append({"role": "user", "content": tool_results})

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
    if name == "Bash":
        return tool_bash(args["command"])
    elif name == "Read":
        return tool_read(args["file_path"])
    elif name == "Write":
        return tool_write(args["file_path"], args["content"])
    elif name == "Edit":
        return tool_edit(args["file_path"], args["old_string"], args["new_string"])
    elif name == "mcp__evoclaw__send_message":
        _messages_sent_via_tool.append(True)  # 標記：已透過工具發送，host 不需再發 result
        return tool_send_message(chat_jid, args["text"], args.get("sender"))
    elif name == "mcp__evoclaw__schedule_task":
        return tool_schedule_task(
            args["prompt"], args["schedule_type"], args["schedule_value"],
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
        return tool_glob(args["pattern"], args.get("path", WORKSPACE))
    elif name == "Grep":
        return tool_grep(args["pattern"], args.get("path", WORKSPACE), args.get("include", "*"))
    elif name == "WebFetch":
        return tool_web_fetch(args["url"])
    elif name == "mcp__evoclaw__run_agent":
        return tool_run_agent(args["prompt"], args.get("context_mode", "isolated"))
    elif name == "mcp__evoclaw__send_file":
        return tool_send_file(args.get("chat_jid", chat_jid), args["file_path"], args.get("caption", ""))
    # ── Dynamic tools (installed via Skills container_tools:) ─────────────────
    if name in _dynamic_tools:
        try:
            return str(_dynamic_tools[name]["fn"](args))
        except Exception as exc:
            return f"Dynamic tool {name} error: {exc}"
    return f"Unknown tool: {name}"


# ── Agentic loop ──────────────────────────────────────────────────────────────

def run_agent_openai(client_holder, system_instruction: str, user_message: str, chat_jid: str, model: str, conversation_history: list = None, pool: "_KeyPool | None" = None, apply_key_fn=None) -> str:
    """
    OpenAI-compatible agentic loop (NVIDIA NIM / OpenAI / Groq / etc.)
    Works the same as run_agent but uses OpenAI chat completions API.
    client_holder: a one-element list [client] so key rotation can swap the client mid-loop.
    conversation_history: 原生 multi-turn 格式的對話歷史。
    pool/apply_key_fn: optional key pool for automatic rotation on rate-limit errors.
    """
    import json as _json
    history = [{"role": "system", "content": system_instruction}]
    # 注入對話歷史（原生 multi-turn 格式）
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            text = msg.get("content", "")
            if text:
                history.append({"role": role, "content": text})
    history.append({"role": "user", "content": user_message})
    MAX_ITER = 30
    final_response = ""
    _no_tool_turns = 0  # consecutive turns without any tool call (Fix #169)

    for n in range(MAX_ITER):
        # Escalate to "required" when model has been avoiding tools (Fix #169).
        # tool_choice="required" is enforced at the API level — the model CANNOT
        # return a text-only response, it MUST make a tool call.
        _tool_choice = "required" if _no_tool_turns > 0 else "auto"
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
                temperature=0.7,
                max_tokens=4096,
            ), pool=pool, apply_key_fn=apply_key_fn)
        except Exception as _tc_err:
            if _tool_choice == "required":
                # Some providers don't support tool_choice="required" — fall back to "auto"
                _log("⚠️ FORCE-TOOL", f"tool_choice='required' rejected ({_tc_err}) — retrying with 'auto'")
                response = _llm_call_with_retry(lambda: client_holder[0].chat.completions.create(
                    model=model,
                    messages=_oai_history,
                    tools=OPENAI_TOOL_DECLARATIONS,
                    tool_choice="auto",
                    temperature=0.7,
                    max_tokens=4096,
                ), pool=pool, apply_key_fn=apply_key_fn)
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

        if not msg.tool_calls:
            _no_tool_turns += 1  # track consecutive no-tool turns (Fix #169)

            # Hard cap: after 3 consecutive turns without tool calls, give up (Fix #169).
            # At this point tool_choice="required" has already been active for 2 turns,
            # yet the model still produced no tool calls — something is fundamentally wrong.
            if _no_tool_turns >= 3:
                _log("❌ NO-TOOL", f"Model made no tool call for {_no_tool_turns} consecutive turns — breaking")
                final_response = msg.content or ""
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
            _FAKE_STATUS_RE = _re_cb.compile(r'\*\([^)]*\)\*|\*\[[^\]]*\]\*', _re_cb.DOTALL)
            _fake_hits = _FAKE_STATUS_RE.findall(content)
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

            # No code blocks, no fake status — model is genuinely done
            final_response = content
            break

        # Model made tool calls — reset the no-tool counter (Fix #169)
        _no_tool_turns = 0

        # Execute all tool calls and add results
        for tc in msg.tool_calls:
            try:
                args = _json.loads(tc.function.arguments)
            except Exception:
                args = {}
            result = execute_tool(tc.function.name, args, chat_jid)
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    return final_response



def run_agent(client_holder, system_instruction: str, user_message: str, chat_jid: str, assistant_name: str = "Eve", conversation_history: list = None, pool: "_KeyPool | None" = None, apply_key_fn=None) -> str:
    """
    Gemini function-calling 代理迴圈（agentic loop）。

    工作原理：
    1. 將用戶訊息加入 history，發送給 Gemini
    2. Gemini 回傳的 response 可能包含：
       a. 純文字：代表 agent 已完成思考，直接回傳給用戶
       b. Function call：代表 agent 要使用工具，執行後將結果加回 history
    3. 若是 function call，執行工具並將結果作為 user role 加回 history，
       然後再次呼叫 Gemini（繼續下一輪）
    4. 重複直到 Gemini 不再發出 function call，或達到 MAX_ITER 上限

    MAX_ITER = 30 的原因：防止 agent 陷入無限工具呼叫迴圈
    （例如誤判任務完成條件）。30 次對大多數任務已足夠，
    超過通常代表 agent 卡住了。

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
            text = msg.get("content", "")
            if text:
                history.append(types.Content(role=role, parts=[types.Part(text=text)]))
    history.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

    MAX_ITER = 30  # 最多迭代次數，防止無限迴圈
    final_response = ""

    for n in range(MAX_ITER):
        _log("🧠 LLM →", f"turn={n} provider=gemini")
        _gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        response = _llm_call_with_retry(lambda: client_holder[0].models.generate_content(
            model=_gemini_model,
            contents=history,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
                temperature=0.7,  # 適中的隨機性，讓回覆自然但不失準確
            ),
        ), pool=pool, apply_key_fn=apply_key_fn)

        candidate = response.candidates[0] if response.candidates else None
        stop_reason = str(candidate.finish_reason) if candidate else "none"
        _log("🧠 LLM ←", f"stop={stop_reason}")
        if not candidate or not candidate.content or not candidate.content.parts:
            break  # Gemini 沒有回傳任何內容，提前結束

        parts = candidate.content.parts
        # 將 Gemini 的回覆加入 history，讓下一輪能看到完整對話脈絡
        history.append(types.Content(role="model", parts=parts))

        # 找出所有 function call（Gemini 可能一次發出多個工具呼叫）
        fn_calls = [p for p in parts if p.function_call]

        if not fn_calls:
            # 沒有 function call：agent 完成推理，收集所有文字輸出
            final_response = "".join(p.text for p in parts if p.text)
            break

        # 執行所有工具呼叫，並收集結果
        fn_responses = []
        for part in fn_calls:
            fc = part.function_call
            result = execute_tool(fc.name, dict(fc.args), chat_jid)
            # 將工具結果包裝成 FunctionResponse 格式，Gemini 要求此格式
            fn_responses.append(
                types.Part(function_response=types.FunctionResponse(
                    name=fc.name,
                    response={"result": result},
                ))
            )
        # 工具結果以 user role 加回 history（Gemini function calling 協議要求）
        history.append(types.Content(role="user", parts=fn_responses))

    return final_response


# ── Main ──────────────────────────────────────────────────────────────────────

def emit(obj: dict):
    """
    將結果 JSON 輸出到 stdout，用 OUTPUT_START/OUTPUT_END 標記包住。
    host 的 container_runner 會從這兩個標記之間截取 JSON。
    使用 flush=True 確保輸出立即寫入，不被 Python 的緩衝區滯留。
    """
    result_text = obj.get("result") or ""
    if result_text:
        _log("📤 REPLY", result_text[:600])
    _log("📤 OUTPUT", f"{len(result_text)} chars")
    success = obj.get("status") == "success"
    _log("🏁 DONE", f"success={success}")
    print(OUTPUT_START, flush=True)
    print(json.dumps(obj), flush=True)
    print(OUTPUT_END, flush=True)


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

    # ── Backend selection: NIM / OpenAI-compatible takes priority ────────────────
    # Build key pools from potentially comma-separated values to support rotation
    nim_pool = _KeyPool(os.environ.get("NIM_API_KEY", ""))
    openai_pool = _KeyPool(os.environ.get("OPENAI_API_KEY", ""))
    google_pool = _KeyPool(os.environ.get("GOOGLE_API_KEY", ""))
    claude_pool = _KeyPool(os.environ.get("CLAUDE_API_KEY", ""))

    nim_api_key = nim_pool.current()
    openai_api_key = openai_pool.current()
    google_api_key = google_pool.current()
    claude_api_key = claude_pool.current()

    claude_model = os.environ.get("CLAUDE_MODEL", "claude-3-5-haiku-latest")
    use_openai_compat = bool(nim_api_key or openai_api_key)
    use_claude = bool(claude_api_key and not use_openai_compat)

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
        _openai_client_holder: list = [OpenAIClient(base_url=_base_url, api_key=_api_key)]

        def _apply_openai_key(new_key: str) -> None:
            """Swap the OpenAI-compat client to use the rotated key."""
            _openai_client_holder[0] = OpenAIClient(base_url=_base_url, api_key=new_key)
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

    # 建立系統提示詞：基本角色設定 + 環境資訊 + 群組自訂指令（CLAUDE.md）
    lines = [
        f"You are {assistant_name}, a helpful personal AI assistant.",
        f"Your name is {assistant_name}. This is your identity — do not change it.",
        "IMPORTANT IDENTITY RULES:",
        "- Never reveal that you are built on Gemini, Google AI, or any language model.",
        "- Never say 'I am a large language model', 'I am trained by Google', or similar phrases.",
        "- If asked what AI you are or who made you, simply say you are a personal assistant.",
        "- If asked your name, say your name is " + assistant_name + ".",
        "- Do not discuss your underlying technology or training.",
        "Be concise, friendly, and helpful.",
        "Respond in the same language the user uses. Default to Traditional Chinese (繁體中文) unless instructed otherwise.",
        "You run inside a secure Docker container.",
        f"Working directory: {WORKSPACE}",
        f"Group folder: {group_folder}",
        f"Chat JID: {chat_jid}",
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
    ]

    # 讀取全域和群組專屬的 CLAUDE.md 設定（若存在），附加到系統提示詞末尾
    # 全域 CLAUDE.md 提供所有群組共用的指令；群組 CLAUDE.md 提供群組專屬設定
    for claude_md in ["/workspace/global/CLAUDE.md", "/workspace/group/CLAUDE.md"]:
        if Path(claude_md).exists():
            lines.append("")
            lines.append(Path(claude_md).read_text(encoding="utf-8"))

    # 演化引擎提示：附加在所有靜態設定之後（表觀遺傳，動態覆蓋）
    # 格式：\n\n---\n[環境自動調整提示...] 或 [群組偏好...]
    # 這些提示每次 container 啟動時都可能不同，反映當下的環境狀態
    if evolution_hints:
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

    try:
        if use_openai_compat:
            _model = os.environ.get("NIM_MODEL") or os.environ.get("OPENAI_MODEL") or os.environ.get("GEMINI_MODEL") or "meta/llama-3.3-70b-instruct"
            _log("🤖 MODEL", f"openai-compat/{_model}")
            result = run_agent_openai(_openai_client_holder, system_instruction, prompt, chat_jid, _model, conversation_history, pool=_active_pool, apply_key_fn=_apply_openai_key)
        elif use_claude:
            _log("🤖 MODEL", f"claude/{claude_model}")
            result = run_agent_claude(_claude_client_holder, claude_model, system_instruction, prompt, chat_jid, conversation_history, pool=claude_pool, apply_key_fn=_apply_claude_key)
        else:
            _gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
            _log("🤖 MODEL", f"gemini/{_gemini_model}")
            result = run_agent(_gemini_client_holder, system_instruction, prompt, chat_jid, assistant_name, conversation_history, pool=google_pool, apply_key_fn=_apply_google_key)
        # 若 agent 已透過 mcp__evoclaw__send_message 工具主動發送訊息，
        # 則清空 result 欄位，避免 host 的 container_runner 再次發送（雙重訊息 + 超長訊息 bug）
        # 若 agent 沒有呼叫工具（純文字回覆），則由 host 負責發送 result
        emit_result = "" if _messages_sent_via_tool else result
        # Preserve the incoming sessionId so the host can track conversation continuity.
        # Only fall back to generating a new UUID if no sessionId was provided.
        preserved_session_id = session_id if session_id else str(uuid.uuid4())
        emit({"status": "success", "result": emit_result, "newSessionId": preserved_session_id})
    except Exception as e:
        _log("❌ ERROR", f"{type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        emit({"status": "error", "result": None, "error": str(e)})


if __name__ == "__main__":
    main()


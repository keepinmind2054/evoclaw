"""Spawns and manages agent execution in Docker containers"""
import asyncio
import atexit
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional, Callable, Awaitable

import threading as _threading

from . import config, db
from .env import read_env_file
from .evolution import record_run, get_adaptive_hints, get_genome_style_hints
from .memory import get_hot_memory, update_hot_memory

# ── Secret redaction for container stderr logs (Fix #110) ─────────────────────
# Applied to every stderr line before logging to prevent API keys and credentials
# from appearing in host log files or the dashboard log stream.
_SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|token|password|secret|bearer|authorization)\s*[=:]\s*\S+'),
    re.compile(r'sk-[A-Za-z0-9]{20,}'),       # OpenAI-style keys
    re.compile(r'ghp_[A-Za-z0-9]{36}'),         # GitHub tokens
    re.compile(r'AIza[A-Za-z0-9_-]{35}'),        # Google API keys
]


def _redact_secrets(text: str) -> str:
    """Replace secret values in a log line with [REDACTED] before emitting."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub(
            lambda m: m.group(0).split('=')[0] + '=[REDACTED]' if '=' in m.group(0) else '[REDACTED]',
            text,
        )
    return text


def _is_windows() -> bool:
    return sys.platform == "win32"

log = logging.getLogger(__name__)

# ── Container image version pin warning ───────────────────────────────────────
# Log a warning at import time when the image tag uses the mutable ':latest' tag.
# Operators should pin to a specific version or digest to prevent silent behavioral
# regressions when the image is rebuilt (e.g. CONTAINER_IMAGE=evoclaw-agent:1.10.12).
def _warn_if_latest_image() -> None:
    img = config.CONTAINER_IMAGE
    if img.endswith(":latest") or ":" not in img:
        log.warning(
            "CONTAINER_IMAGE is using an unpinned tag (%r). "
            "A 'docker pull' or rebuild can silently change agent behavior. "
            "Consider pinning to a specific version tag, e.g. evoclaw-agent:1.10.22",
            img,
        )

_warn_if_latest_image()

# ── Named constants for thresholds (Fix #193) ─────────────────────────────────
# Stderr length threshold: above this, we assume the container ran (Docker OK)
# but the agent process crashed before emitting output markers.
_STDERR_DOCKER_OK_THRESHOLD = 200
# Maximum size of container output JSON before we reject it (2 MB)
_MAX_OUTPUT_SIZE = 2 * 1024 * 1024
# Stderr readline timeout in seconds (per-line, not total)
_STDERR_READLINE_TIMEOUT = 30.0
# Default history lookback window in hours (can be overridden per-group)
_DEFAULT_HISTORY_LOOKBACK_HOURS = 4

# ── Active container tracking (for dashboard) ─────────────────────────────────
_active_containers: dict[str, dict] = {}  # container_name → info dict
_active_lock = asyncio.Lock()  # asyncio.Lock for use in async coroutines

# ── Docker circuit breaker ─────────────────────────────────────────────────────
# Per-group circuit breaker（每個群組獨立追蹤失敗）
_docker_failures: dict = {}       # group_folder → int
_docker_failure_time: dict = {}   # group_folder → float
_docker_failure_lock = _threading.Lock()
_DOCKER_CIRCUIT_THRESHOLD = 3   # open circuit after this many consecutive failures
_DOCKER_HALF_OPEN_SECS = 60     # try ONE request after 60s of open circuit (half-open state)

# ── Portable empty file for .env shadow mount ──────────────────────────────────
_EMPTY_ENV_FILE: str | None = None
_EMPTY_ENV_FILE_LOCK = _threading.Lock()  # guard lazy-init (Issue #55)


def _get_empty_env_file() -> str | None:
    """Get path to an empty file for shadowing .env in containers.

    Returns None if temp file creation fails (e.g., on some Windows Docker configs).
    A threading.Lock prevents two concurrent callers from each creating a separate
    temp file during the first call, which would leave one orphaned (Issue #55).
    """
    global _EMPTY_ENV_FILE
    # Fast path: already initialised (no lock needed — str assignment is atomic on CPython)
    if _EMPTY_ENV_FILE is not None:
        return _EMPTY_ENV_FILE
    with _EMPTY_ENV_FILE_LOCK:
        # Double-checked locking: re-check inside the lock
        if _EMPTY_ENV_FILE is not None:
            return _EMPTY_ENV_FILE
        try:
            fd, path = tempfile.mkstemp(prefix="evoclaw_empty_env_", suffix=".env")
            os.close(fd)
            atexit.register(lambda p=path: os.unlink(p) if os.path.exists(p) else None)
            _EMPTY_ENV_FILE = path
            return path
        except Exception as exc:
            log.warning("Cannot create shadow .env temp file: %s", exc)
            return None


def _record_docker_success(group_folder: str = "_global") -> None:
    global _docker_failures, _docker_failure_time
    with _docker_failure_lock:
        if group_folder in _docker_failures:
            del _docker_failures[group_folder]
        if group_folder in _docker_failure_time:
            del _docker_failure_time[group_folder]


def _record_docker_failure(group_folder: str = "_global") -> None:
    global _docker_failures, _docker_failure_time
    with _docker_failure_lock:
        _docker_failures[group_folder] = _docker_failures.get(group_folder, 0) + 1
        _docker_failure_time[group_folder] = time.time()
        log.warning("[%s] Docker failure recorded (count=%d)", group_folder, _docker_failures[group_folder])


def _docker_circuit_open(group_folder: str = "_global") -> bool:
    """Per-group circuit breaker：每個群組獨立追蹤，互不干擾。"""
    global _docker_failures, _docker_failure_time
    with _docker_failure_lock:
        failures = _docker_failures.get(group_folder, 0)
        if failures < _DOCKER_CIRCUIT_THRESHOLD:
            return False
        last_failure = _docker_failure_time.get(group_folder, 0.0)
        elapsed = time.time() - last_failure
        if elapsed >= _DOCKER_HALF_OPEN_SECS:
            _docker_failures[group_folder] = 0  # Reset counter when half-open
            log.info("[%s] Docker circuit half-open after %.0fs", group_folder, elapsed)
            return False
        log.warning("[%s] Docker circuit OPEN (failures=%d, retry in %.0fs)",
                    group_folder, failures, _DOCKER_HALF_OPEN_SECS - elapsed)
        return True


def get_active_containers() -> list[dict]:
    """Thread-safe snapshot for dashboard thread.
    Since asyncio is single-threaded, a shallow copy without lock is safe
    for reading from another thread (GIL protects dict iteration on CPython).
    """
    return list(_active_containers.values())


def _docker_path(p) -> str:
    """Convert path to Docker-compatible forward-slash format."""
    return str(p).replace("\\", "/")


# container 輸出的邊界標記，用於從 stdout 中精確截取 JSON 結果
# 使用不常見的字串避免與 agent 的正常輸出衝突
OUTPUT_START = "---EVOCLAW_OUTPUT_START---"
OUTPUT_END = "---EVOCLAW_OUTPUT_END---"

def _read_secrets() -> dict:
    """從 .env 檔案讀取敏感金鑰（API key 等），以字典形式回傳給 container。

    Only LLM-related keys are passed to the container. Channel tokens
    (TELEGRAM_BOT_TOKEN, etc.) and SCM tokens (GITHUB_TOKEN, GH_TOKEN)
    are intentionally excluded to limit blast radius if a container is
    compromised (Fix #187).
    """
    return read_env_file([
        "GOOGLE_API_KEY", "GEMINI_MODEL",
        "NIM_API_KEY", "NIM_MODEL", "NIM_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
        "CLAUDE_API_KEY", "CLAUDE_MODEL",
        "ASSISTANT_NAME",
    ])

def _validate_secrets(secrets: dict) -> None:
    """Validate that at least one LLM API key is present; warn on startup for missing keys."""
    llm_keys = ["GOOGLE_API_KEY", "NIM_API_KEY", "OPENAI_API_KEY", "CLAUDE_API_KEY"]
    has_any = any(secrets.get(k, "").strip() for k in llm_keys)
    if not has_any:
        log.warning(
            "Secret validation: none of the LLM API keys are set (%s). "
            "Container agent will fail to call any LLM.",
            ", ".join(llm_keys)
        )
    for key in llm_keys:
        val = secrets.get(key, "").strip()
        if key == "GOOGLE_API_KEY" and not val:
            log.warning("Secret %s is missing or empty", key)

def _build_volume_mounts(group: dict) -> list[str]:
    """
    根據群組設定建立 Docker volume mount 參數清單。

    主群組（is_main=True）與一般群組的掛載方式不同：
    - 主群組：掛載整個 project 目錄（唯讀 :ro），讓 agent 可以讀取程式碼庫，
              同時掛載自己的 group 目錄（可讀寫 :rw）
    - 一般群組：不掛載 project（避免存取原始碼），改掛載 global 共享目錄（唯讀），
               讓 agent 可以讀取全域 CLAUDE.md 設定但不能修改

    所有群組都掛載：
    - sessions/.claude：持久化 Claude session 資料（對話記憶）
    - ipc/：IPC 目錄，container 透過寫入 JSON 檔案與 host 通訊
    """
    folder = group["folder"]
    groups_dir = config.GROUPS_DIR
    data_dir = config.DATA_DIR
    base_dir = config.BASE_DIR
    is_main = bool(group.get("is_main"))

    mounts = []

    if is_main:
        # 主群組可以讀取整個 project 原始碼（唯讀），用於 code review、開發協助等
        mounts += [
            f"{_docker_path(base_dir)}:/workspace/project:ro",
            f"{_docker_path(groups_dir)}/{folder}:/workspace/group:rw",
        ]
        # Security: shadow .env to prevent container access to host secrets.
        # The project dir is mounted :ro above, so .env is readable inside the container
        # unless we shadow it. Use a portable empty temp file instead of /dev/null
        # because /dev/null cannot be bind-mounted on macOS Docker Desktop.
        env_file = base_dir / ".env"
        if env_file.exists():
            empty_env = _get_empty_env_file()
            if empty_env and not _is_windows():
                log.warning(
                    "SECURITY: .env file found in project root (%s). "
                    "Shadowing with empty file to prevent container access to host secrets.",
                    env_file
                )
                mounts.append(
                    f"{_docker_path(empty_env)}:/workspace/project/.env:ro"
                )
            else:
                log.warning(
                    "SECURITY: .env file found in project root (%s). "
                    "On Windows, shadow mount is skipped. Consider moving .env outside the project directory.",
                    env_file
                )
    else:
        # 一般群組只能存取自己的資料夾與全域共享設定，無法觸碰原始碼
        mounts += [
            f"{_docker_path(groups_dir)}/{folder}:/workspace/group:rw",
            f"{_docker_path(groups_dir)}/global:/workspace/global:ro",
        ]

    # Sessions：持久化 Claude 的對話 session，讓 agent 記得之前的對話脈絡
    session_dir = data_dir / "sessions" / folder / ".claude"
    session_dir.mkdir(parents=True, exist_ok=True)
    mounts.append(f"{_docker_path(session_dir)}:/home/node/.claude:rw")

    # IPC：container 寫入 JSON 檔案，host 的 ipc_watcher 讀取並執行對應動作
    # messages/ 子目錄：傳送訊息給用戶
    # tasks/ 子目錄：建立或管理排程任務
    # input/ 子目錄：host 傳給 container 的資料（目前備用）
    ipc_dir = data_dir / "ipc" / folder
    for sub in ["messages", "tasks", "input", "results"]:
        (ipc_dir / sub).mkdir(parents=True, exist_ok=True)
    mounts.append(f"{_docker_path(ipc_dir)}:/workspace/ipc:rw")

    # Dynamic tools：Skills 安裝的 container_tools 熱插拔目錄
    # 不需重建 image — 安裝 skill 後下次 container 啟動即自動 import
    dynamic_tools_dir = data_dir / "dynamic_tools"
    dynamic_tools_dir.mkdir(parents=True, exist_ok=True)
    mounts.append(f"{_docker_path(dynamic_tools_dir)}:/app/dynamic_tools:ro")

    return mounts

def _safe_name(folder: str) -> str:
    """將 folder 名稱轉換為合法的 Docker container 名稱（底線換連字號，截斷過長部分）。"""
    return folder.replace("_", "-")[:40]

async def update_container_activity(container_name: str, activity: str) -> None:
    """Update the current_activity field for a running container (called from stderr stream)."""
    async with _active_lock:
        if container_name in _active_containers:
            _active_containers[container_name]["current_activity"] = activity



def _get_agent_id(group_name: str, project: str = "", channel: str = "") -> str:
    """Phase 2 (UnifiedClaw): Generate stable agent_id for a group.
    
    Produces a deterministic 16-char hex ID from group name + project + channel.
    This is the same algorithm used by AgentIdentityStore.get_or_create().
    Passed as AGENT_ID env var to containers so FitnessReporter can self-identify.
    """
    import hashlib
    raw = f"{group_name.lower()}:{project.lower()}:{channel.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

async def run_container_agent(
    group: dict,
    prompt: str,
    on_output: Optional[Callable[[str], Awaitable[None]]] = None,
    session_id: Optional[str] = None,
    is_scheduled_task: bool = False,
    on_success: Optional[Callable[[], Awaitable[None]]] = None,
    conversation_history: list | None = None,
    parent_container: Optional[str] = None,
    on_error: Optional[Callable[[str], Awaitable[None]]] = None,
) -> dict:
    """
    在獨立的 Docker container 中執行 agent，並等待結果。

    輸入方式：透過 stdin 傳入 JSON（而非環境變數），原因是：
    1. 環境變數在 `docker inspect` 或 /proc/self/environ 中可能洩漏敏感資料
    2. JSON stdin 讓 container 啟動後才讀取，更難被外部觀察

    輸出解析：從 stdout 中尋找 OUTPUT_START/OUTPUT_END 標記，
    截取中間的 JSON 作為結果（忽略 agent 的其他 debug 輸出）。

    on_success callback 的時機：只有在 container 正常結束且輸出有效時才呼叫，
    讓呼叫方（通常是 _message_loop）安全地推進游標。
    若 container 逾時或解析失敗，不呼叫 on_success，保留 rollback 安全性。

    on_error callback：當 container 執行失敗（crash / timeout / exception）時呼叫，
    讓呼叫方可以即時在對話中通知用戶，無需主動查看後台 log。
    傳入的字串是已格式化的用戶通知訊息（繁體中文）。
    """

    async def _notify_error(msg: str) -> None:
        """Safe wrapper — silently ignore errors in the notification itself."""
        if on_error:
            try:
                await on_error(msg)
            except Exception as _ne:
                log.debug("on_error callback raised: %s", _ne)

    folder = group["folder"]

    if _docker_circuit_open(folder):
        await _notify_error(
            f"⚠️ 此群組 Docker 暫時受阻（連續失敗 {_DOCKER_CIRCUIT_THRESHOLD} 次），"
            f"約 {_DOCKER_HALF_OPEN_SECS} 秒後自動恢復。其他群組不受影響。"
        )
        return {"status": "error", "error": f"Docker circuit breaker open for {folder}"}
    jid = group["jid"]
    # 用時間戳記讓 container 名稱唯一，方便 debug 與孤兒清理
    run_id = str(uuid.uuid4())
    # Use the first 8 hex chars of run_id (not timestamp) so concurrent containers
    # for the same group within the same second get unique names (Issue #59).
    container_name = f"evoclaw-{_safe_name(folder)}-{run_id[:8]}"

    mounts = _build_volume_mounts(group)
    mount_args = []
    for m in mounts:
        if m.startswith("--mount "):
            # Shadow mount entries use --mount syntax rather than -v
            _, rest = m.split(" ", 1)
            mount_args += ["--mount", rest]
        else:
            mount_args += ["-v", m]

    # ── 演化提示（表觀遺傳）：根據環境和群組基因組動態附加提示 ─────────────────
    # 這些提示不修改 CLAUDE.md，只在本次 container 執行時附加，
    # 讓 AI 在不同環境下表現不同（例如系統忙碌時自動簡短回答）
    evolution_hints = get_adaptive_hints(jid) + get_genome_style_hints(jid)

    # 將 API 金鑰等敏感資料包進 input_data，透過 stdin 傳給 container
    secrets = _read_secrets()
    # 取得最近對話歷史（最多 50 則），提供給 agent 作為上下文記憶
    # history_lookback_hours 可在 group config 中設定（預設 4 小時）
    history_lookback = group.get("history_lookback_hours", _DEFAULT_HISTORY_LOOKBACK_HOURS) * 3600
    history_cutoff = int((time.time() - history_lookback) * 1000)
    history_msgs = db.get_messages_since(jid, history_cutoff, limit=50)

    # 防止對話歷史超過 token 上限（粗略估算：1 token ≈ 4 字符）
    _MAX_HISTORY_CHARS = 20_000  # 約 5000 token
    _total_chars = 0
    _trimmed_history = []
    for _msg in reversed(history_msgs):  # 從最新開始保留
        _msg_chars = len(str(_msg.get("content", "")))
        if _total_chars + _msg_chars > _MAX_HISTORY_CHARS:
            log.warning("Trimming conversation history for %s: %d messages dropped", jid, len(history_msgs) - len(_trimmed_history))
            break
        _trimmed_history.append(_msg)
        _total_chars += _msg_chars
    history_msgs = list(reversed(_trimmed_history))

    conv_history = []
    for m in history_msgs:
        role = "assistant" if m.get("is_bot_message") else "user"
        text = str(m.get("content") or "").strip()
        if text:
            conv_history.append({"role": role, "content": text})

    # 取得此群組的排程任務清單，傳給 container 讓 agent 可以列出和取消任務
    scheduled_tasks = db.get_all_tasks(group_folder=folder)

    # ── 三層記憶系統：注入熱記憶 ────────────────────────────────────────────
    # 取得此群組的熱記憶（per-group MEMORY.md，8KB 上限），注入到 container 的系統上下文
    hot_memory = get_hot_memory(jid)

    # Phase 2 (UnifiedClaw): inject stable agent_id so FitnessReporter can self-identify
    _agent_id = _get_agent_id(
        group_name=folder,
        project=group.get("project", ""),
        channel=group.get("channel", ""),
    )
    input_data = {
        "prompt": prompt,
        "sessionId": session_id,
        "groupFolder": folder,
        "chatJid": jid,
        "isMain": bool(group.get("is_main")),
        "isScheduledTask": is_scheduled_task,
        "assistantName": config.ASSISTANT_NAME,
        "secrets": secrets,  # API keys 等，container 內讀取後設定為 env vars
        "evolutionHints": evolution_hints,  # 演化引擎動態注入的行為指引
        "conversationHistory": conversation_history if conversation_history is not None else conv_history,  # 最近的對話歷史，提供記憶能力
        "scheduledTasks": scheduled_tasks,  # 此群組的排程任務清單，讓 agent 可以列出和取消
        "runId": run_id,  # 關聯 ID：供 container 在 stderr 中記錄，與 host 日誌對齊
        "hotMemory": hot_memory,  # 三層記憶：熱記憶（8KB MEMORY.md，每次對話自動注入）
        "agentId": _agent_id,  # Phase 2 (UnifiedClaw): stable agent_id for FitnessReporter
    }
    input_json = json.dumps(input_data, ensure_ascii=True)
    # 記錄 container 啟動時間，用於計算回應時間（適應度追蹤）
    t0 = time.time()
    async with _active_lock:
        _active_containers[container_name] = {
            "name": container_name,
            "folder": folder,
            "jid": jid,
            "run_id": run_id,
            "started_at": int(t0 * 1000),
            "is_scheduled": is_scheduled_task,
            "parent_container": parent_container,   # None = 主 agent，str = subagent
            "current_activity": "starting...",      # 即時 stderr 活動狀態
        }

    # 讓 container 以 host 的 UID/GID 執行，確保寫入 volume 的檔案有正確的擁有者
    # os.getuid/getgid are not available on Windows — use safe fallback
    uid = getattr(os, 'getuid', lambda: None)()
    gid = getattr(os, 'getgid', lambda: None)()

    cmd = [
        "docker", "run",
        "-i",      # 需要 interactive 模式才能讀取 stdin
        "--rm",    # container 結束後自動刪除，避免殘留
        "--name", container_name,
        "-e", f"TZ={config.TIMEZONE}",  # 時區設定，確保 agent 顯示正確時間
        "-e", "PYTHONUNBUFFERED=1",  # 強制 Python stdout 立即 flush，讓 Docker Desktop 日誌即時顯示
    ]
    # ── Per-container resource limits (Issue #61) ──────────────────────────────
    # Prevent a runaway agent from OOM-killing the host process.
    # Both limits are opt-out: set CONTAINER_MEMORY="" or CONTAINER_CPUS="" to disable.
    if config.CONTAINER_MEMORY:
        cmd += ["--memory", config.CONTAINER_MEMORY, "--memory-swap", config.CONTAINER_MEMORY]
    if config.CONTAINER_CPUS:
        cmd += ["--cpus", config.CONTAINER_CPUS]
    if uid is not None and gid is not None:
        cmd += ["--user", f"{uid}:{gid}"]
    cmd += [
        *mount_args,
        config.CONTAINER_IMAGE,
    ]

    log.info(f"Starting container {container_name} for group {folder} (run_id={run_id})")
    _started_at = time.monotonic()
    db.log_container_start(run_id, jid, folder, container_name, time.time())

    input_bytes = input_json.encode("utf-8")

    proc = None  # asyncio subprocess reference — used for direct kill on CancelledError
    try:
        if sys.platform == "win32":
            # On Windows, asyncio subprocess pipes can deadlock with Docker.
            # Use subprocess.run() in a thread instead — it handles pipes correctly.
            import subprocess as _subprocess

            def _sync_docker_run() -> tuple[bytes, bytes]:
                r = _subprocess.run(
                    cmd,
                    input=input_bytes,
                    capture_output=True,
                    timeout=config.CONTAINER_TIMEOUT,
                )
                return r.stdout, r.stderr

            log.debug(f"[DEBUG] Running docker in thread (Windows mode)...")
            stdout_data, stderr_data = await asyncio.to_thread(_sync_docker_run)
            log.debug(f"[DEBUG] Docker thread returned. stdout={len(stdout_data)}b stderr={len(stderr_data)}b")
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # ── 串流 stderr（即時顯示 container 活動狀態）─────────────────────
            # 將 stdin 寫入後立即關閉，然後並行收集 stdout 與串流 stderr，
            # 讓 dashboard 和 Docker Desktop 都能即時看到 _log() 輸出。
            proc.stdin.write(input_bytes)
            await proc.stdin.drain()
            proc.stdin.close()

            stderr_lines: list[str] = []
            _MAX_STDERR_LINES = 5000  # Cap to prevent unbounded memory growth

            async def _stream_stderr() -> None:
                """逐行讀取 stderr 並更新 current_activity + log_buffer。"""
                assert proc.stderr is not None
                _EMOJI_TAGS = ("🚀","📥","💬","🤖","🧠","🔧","📨","📎","📤","❌","🏁","⚠️")
                while True:
                    try:
                        line_bytes = await asyncio.wait_for(proc.stderr.readline(), timeout=_STDERR_READLINE_TIMEOUT)
                    except asyncio.TimeoutError:
                        log.warning("Stderr readline timed out, stopping stream")
                        break
                    if not line_bytes:
                        break
                    line = line_bytes.decode(errors="replace").rstrip()
                    if line:
                        if len(stderr_lines) < _MAX_STDERR_LINES:
                            stderr_lines.append(line)
                        elif len(stderr_lines) == _MAX_STDERR_LINES:
                            stderr_lines.append(f"... (truncated, >{_MAX_STDERR_LINES} lines)")
                        # Redact secrets before logging (Fix #110)
                        safe_line = _redact_secrets(line)
                        # Elevate structured agent log lines to INFO
                        if any(e in safe_line for e in _EMOJI_TAGS):
                            log.info("[%s] %s", container_name, safe_line)
                        else:
                            log.debug("[%s] %s", container_name, safe_line)
                        async with _active_lock:
                            if container_name in _active_containers:
                                _active_containers[container_name]["current_activity"] = safe_line

            async def _collect() -> tuple[bytes, bytes]:
                stdout_task = asyncio.create_task(proc.stdout.read())
                stderr_task = asyncio.create_task(_stream_stderr())
                stdout_data, _ = await asyncio.gather(stdout_task, stderr_task)
                return stdout_data, b"\n".join(l.encode() for l in stderr_lines)

            stdout_data, stderr_data = await asyncio.wait_for(
                _collect(),
                timeout=config.CONTAINER_TIMEOUT,
            )

        stdout = stdout_data.decode(errors="replace")
        stderr = stderr_data.decode(errors="replace")

        log.debug(f"[DEBUG] Container stdout preview: {stdout[:200]!r}")
        if stderr:
            log.debug(f"[DEBUG] Container stderr: {_redact_secrets(stderr[:500])}")

        # 從 stdout 中尋找輸出標記，截取 JSON 結果
        # agent 可能在標記前後有其他 debug 輸出，只取標記之間的部分
        start_idx = stdout.find(OUTPUT_START)
        end_idx = stdout.find(OUTPUT_END)

        if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
            # 找不到標記代表 container 在輸出結果前就結束了
            stderr_lines = stderr.splitlines() if stderr else []
            if stderr_lines:
                log.warning(
                    "Container %s stderr (last 5 lines):\n%s",
                    container_name,
                    "\n".join(_redact_secrets(l) for l in stderr_lines[-5:])
                )
            log.warning("No valid output markers in container stdout")
            response_ms = int((time.time() - t0) * 1000)
            record_run(jid, run_id, response_ms, retry_count=0, success=False)
            db.log_container_finish(run_id, time.time(), "error", _redact_secrets(stderr) if stderr else "", stdout[:200] if stdout else "", response_ms)
            # ── 區分「Docker daemon 失敗」vs「container agent 崩潰」────────────
            # Use exit code to determine failure type (not stderr heuristic).
            # Docker exit codes:
            #   0   = success (agent ran and exited cleanly)
            #   124 = timeout (agent ran but timed out — agent issue)
            #   137 = OOM killed (agent issue)
            #   143 = SIGTERM (agent issue)
            #   125, 126, 127 = Docker itself failed (image not found, permission, etc.)
            #   other non-zero = likely Docker/container issue
            _AGENT_EXIT_CODES = {0, 124, 137, 143}  # exit codes where container itself ran fine
            # Guard against proc being None (Docker failed to spawn at OS level)
            _container_ran = proc is not None and proc.returncode in _AGENT_EXIT_CODES

            if _container_ran:
                log.info("Container %s crashed before emit() but Docker is healthy — resetting circuit breaker", container_name)
                _record_docker_success(folder)
            else:
                # Exit code indicates Docker itself failed (not an agent-level issue)
                log.warning("Container exit code %s indicates Docker daemon issue — recording failure", proc.returncode if proc else '?')
                _record_docker_failure(folder)
            # Bundle stderr context for monitor channel (separator parsed by on_error in main.py)
            _stderr_ctx_lines = [_redact_secrets(l) for l in (stderr_lines or [])[-15:]]
            _stderr_ctx = (
                f"container: {container_name}\n"
                f"exit_code: {proc.returncode if proc else '?'}\n"
                f"stderr (last {len(_stderr_ctx_lines)} lines):\n" +
                ("\n".join(_stderr_ctx_lines) if _stderr_ctx_lines else "(empty)")
            )
            await _notify_error("⚠️ 系統暫時發生問題，請稍後再傳訊息，會自動重試。|||MONITOR_CONTEXT|||" + _stderr_ctx)
            return {"status": "error", "error": "no output markers", "messages": []}

        # 截取兩個標記之間的內容並解析為 JSON
        raw = stdout[start_idx + len(OUTPUT_START):end_idx].strip()
        if len(raw) > _MAX_OUTPUT_SIZE:
            log.error("Container output too large (%d bytes), truncating", len(raw))
            await _notify_error("⚠️ 系統回應內容過大，請嘗試縮短請求。")
            return {"status": "error", "result": "Output too large"}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("Container output JSON parse error: %s | raw=%r", e, raw[:200])
            # 記錄 JSON 解析失敗的執行，確保演化引擎收到完整的失敗樣本
            response_ms = int((time.time() - t0) * 1000)
            record_run(jid, run_id, response_ms, retry_count=0, success=False)
            db.log_container_finish(run_id, time.time(), "error", _redact_secrets(stderr) if stderr else "", stdout[:200] if stdout else "", response_ms)
            _record_docker_failure(folder)
            _json_ctx = (
                f"container: {container_name}\n"
                f"json_error: {e}\n"
                f"stdout_raw (first 500):\n{_redact_secrets(raw[:500]) if raw else '(empty)'}\n"
                f"stderr (last 10 lines):\n" +
                ("\n".join(_redact_secrets(l) for l in (stderr.splitlines() if stderr else [])[-10:]) or "(empty)")
            )
            await _notify_error("⚠️ 系統暫時發生問題，請稍後再傳訊息，會自動重試。|||MONITOR_CONTEXT|||" + _json_ctx)
            return {"status": "error", "error": f"JSON parse error: {e}", "messages": []}

        # 若 container 有產生回覆文字，透過 on_output callback 發送到聊天室
        result_text = result.get("result")
        if on_output and result_text:
            await on_output(result_text)

        # 三層記憶系統：container 可透過 memory_patch 欄位更新熱記憶
        # agent 在回覆中附上新的記憶內容，host 自動寫入熱記憶供下次對話使用
        if isinstance(result, dict) and result.get("memory_patch"):
            try:
                update_hot_memory(jid, result["memory_patch"])
                log.debug("hot_memory: updated via memory_patch for jid=%s", jid)
            except Exception as _mem_exc:
                log.warning("hot_memory: failed to apply memory_patch for jid=%s: %s", jid, _mem_exc)

        # 更新 session ID：agent 執行後可能建立新的 session，存入 DB 供下次使用
        if result.get("newSessionId"):
            db.set_session(folder, result["newSessionId"])

        # container 成功完成：通知呼叫方可以安全推進游標
        # 這是 rollback 安全機制的最後一步 — 只有到這裡才確認「已處理完畢」
        response_ms = int((time.time() - t0) * 1000)
        # 記錄成功執行數據到演化引擎（適應度追蹤）
        record_run(jid, run_id, response_ms, retry_count=0, success=True)
        safe_stderr = _redact_secrets(stderr) if stderr else ""
        stdout_preview = stdout[:200] if stdout else ""
        db.log_container_finish(run_id, time.time(), "success", safe_stderr, stdout_preview, response_ms)
        _record_docker_success(folder)

        # ── Host Auto-Write Fallback：確保 MEMORY.md 每次 session 都有記錄 ────
        # 若 agent 在本次執行中沒有更新 MEMORY.md（mtime < t0），
        # host 自動補寫最小記錄，確保長期記憶的連續性。
        # 這是最後防線，不替代 agent 的深度記錄，只保證時間軸不中斷。
        try:
            import datetime as _dt
            _host_memory_path = config.GROUPS_DIR / folder / "MEMORY.md"
            _mem_mtime = _host_memory_path.stat().st_mtime if _host_memory_path.exists() else 0.0
            if _mem_mtime < t0:
                _date_str = _dt.datetime.now().strftime("%Y-%m-%d")
                _prompt_preview = (prompt or "")[:80].replace("\n", " ") if prompt else "(no prompt)"
                _auto_entry = f"\n[{_date_str}] [auto] Task: {_prompt_preview}. Result: success.\n"
                with open(_host_memory_path, "a", encoding="utf-8") as _mf:
                    _mf.write(_auto_entry)
                log.info("host auto-wrote MEMORY.md fallback entry for %s", folder)
            else:
                log.debug("MEMORY.md already updated by agent for %s", folder)
        except Exception as _auto_mem_exc:
            log.warning("host auto-write MEMORY.md failed for %s: %s", folder, _auto_mem_exc)

        if on_success:
            await on_success()

        return result

    except asyncio.TimeoutError:
        # 超時：強制停止 container，避免佔用資源；不呼叫 on_success
        log.error(f"Container {folder} timed out after {config.CONTAINER_TIMEOUT}s")
        await _stop_container(container_name)
        # 記錄超時失敗數據（適應度扣分）
        _timeout_ms = int(config.CONTAINER_TIMEOUT * 1000)
        record_run(jid, run_id, _timeout_ms, retry_count=0, success=False)
        db.log_container_finish(run_id, time.time(), "timeout", "Container timed out", "", _timeout_ms)
        _record_docker_failure(folder)
        await _notify_error(f"⏱️ 這個請求超過 {config.CONTAINER_TIMEOUT}s 未完成，會在下次自動重試。")
        return {"status": "error", "result": None, "error": "Container timed out"}
    except asyncio.CancelledError:
        # task.cancel() 從 shutdown 觸發 — 立即 kill container，不等待 grace period
        log.warning("Container %s cancelled, force-killing...", container_name)
        try:
            # 直接 kill asyncio subprocess（比 docker kill 更快）
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            # 用 asyncio.shield 保護 docker kill 不被再次取消
            await asyncio.shield(_stop_container(container_name))
        except Exception:
            pass
        raise  # Must re-raise CancelledError
    except Exception as e:
        log.error(f"Container {folder} error: {e}")
        response_ms = int((time.time() - t0) * 1000)
        # 記錄異常失敗數據
        record_run(jid, run_id, response_ms, retry_count=0, success=False)
        db.log_container_finish(run_id, time.time(), "error", str(e), "", response_ms)
        _record_docker_failure(folder)
        _exc_ctx = (
            f"container: {container_name}\n"
            f"exception: {type(e).__name__}: {e}"
        )
        await _notify_error(f"⚠️ 執行時發生錯誤（{type(e).__name__}），請稍後再試。|||MONITOR_CONTEXT|||{_exc_ctx}")
        return {"status": "error", "result": None, "error": str(e)}
    finally:
        async with _active_lock:
            _active_containers.pop(container_name, None)

async def _stop_container(name: str) -> None:
    """發送 docker kill 指令立即停止指定 container（超時時呼叫）。
    使用 docker kill（SIGKILL）而非 docker stop --time 10（先 SIGTERM 再等 10s），
    以確保 shutdown 時不額外阻塞 10 秒。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except Exception:
        pass


async def kill_all_containers() -> None:
    """強制 kill 所有正在追蹤的 container（shutdown 時呼叫）。
    使用 docker kill（SIGKILL）確保即時終止，不等待 grace period。
    """
    async with _active_lock:
        names = list(_active_containers.keys())
    if not names:
        return
    log.warning("Force-killing %d container(s): %s", len(names), names)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", *names,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=10.0)
    except Exception as e:
        log.warning("kill_all_containers failed: %s", e)

async def cleanup_orphans() -> None:
    """
    啟動時清理上次程序崩潰遺留的孤兒 container。

    用 --filter name=evoclaw- 找出所有屬於本系統的 container，
    強制刪除（-f）避免名稱衝突或資源洩漏。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-q", "--filter", "name=evoclaw-",
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        ids = out.decode().split()
        if ids:
            rm_proc = await asyncio.create_subprocess_exec("docker", "rm", "-f", *ids,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await rm_proc.wait()
            log.info("Cleaned up %d orphan containers", len(ids))
    except Exception as e:
        log.warning(f"Orphan cleanup failed: {e}")

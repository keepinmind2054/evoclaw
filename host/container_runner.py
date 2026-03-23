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

# ── Module-level semaphore for defense-in-depth concurrency limiting ──────────
# Ensures run_container_agent() itself enforces MAX_CONCURRENT_CONTAINERS even if
# a future code path bypasses GroupQueue's concurrency check (STABILITY_ANALYSIS 2.4).
_container_semaphore: asyncio.Semaphore | None = None


def _get_container_semaphore() -> asyncio.Semaphore:
    global _container_semaphore
    if _container_semaphore is None:
        from . import config as _cfg
        _container_semaphore = asyncio.Semaphore(_cfg.MAX_CONCURRENT_CONTAINERS)
    return _container_semaphore


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
# p15d BUG-FIX (MEDIUM): asyncio.Lock() created at module import time is bound to
# the event loop that exists at import time (Python 3.10 deprecation, Python 3.12
# may raise RuntimeError when the lock is used in a different loop).  Use None and
# lazily initialize via _get_active_lock() once the event loop is running.
_active_lock: asyncio.Lock | None = None  # lazily initialized; see _get_active_lock()


def _get_active_lock() -> asyncio.Lock:
    """Return (and lazily create) the asyncio.Lock for _active_containers.

    Called from within coroutines so the running event loop always exists.
    """
    global _active_lock
    if _active_lock is None:
        _active_lock = asyncio.Lock()
    return _active_lock

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


def _docker_circuit_open(group_folder: str = "_global") -> float:
    """Per-group circuit breaker：每個群組獨立追蹤，互不干擾。

    Returns 0.0 when the circuit is closed (requests allowed).
    Returns the remaining cooldown seconds (> 0) when the circuit is open.
    Callers should treat any non-zero return as "circuit open".
    """
    global _docker_failures, _docker_failure_time
    with _docker_failure_lock:
        failures = _docker_failures.get(group_folder, 0)
        if failures < _DOCKER_CIRCUIT_THRESHOLD:
            return 0.0
        last_failure = _docker_failure_time.get(group_folder, 0.0)
        elapsed = time.time() - last_failure
        if elapsed >= _DOCKER_HALF_OPEN_SECS:
            _docker_failures[group_folder] = 0  # Reset counter when half-open
            log.info("[%s] Docker circuit half-open after %.0fs", group_folder, elapsed)
            return 0.0
        remaining = _DOCKER_HALF_OPEN_SECS - elapsed
        log.warning("[%s] Docker circuit OPEN (failures=%d, retry in %.0fs)",
                    group_folder, failures, remaining)
        return remaining


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
    secrets = read_env_file([
        "GOOGLE_API_KEY", "GEMINI_MODEL",
        "NIM_API_KEY", "NIM_MODEL", "NIM_BASE_URL",
        "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL",
        "CLAUDE_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_MODEL",
        "ASSISTANT_NAME",
    ])
    # p21c: ANTHROPIC_API_KEY alias — users who follow the README_en.md example
    # (which referenced ANTHROPIC_API_KEY) silently fell back to Gemini.
    # If CLAUDE_API_KEY is absent but ANTHROPIC_API_KEY is set, promote the alias.
    if not secrets.get("CLAUDE_API_KEY") and secrets.get("ANTHROPIC_API_KEY"):
        secrets["CLAUDE_API_KEY"] = secrets["ANTHROPIC_API_KEY"]
    return secrets

def _validate_secrets(secrets: dict) -> bool:
    """Validate that at least one LLM API key is present; warn on startup for missing keys.

    Returns True when at least one valid LLM key is present, False otherwise.
    Emits a CRITICAL log when no LLM key is found (agent will be unable to call any LLM),
    and an individual WARNING for each key that is set but appears malformed (too short).

    p12b: now returns a bool so callers (main.py startup summary) can act on the result.
    """
    llm_keys = ["GOOGLE_API_KEY", "NIM_API_KEY", "OPENAI_API_KEY", "CLAUDE_API_KEY"]
    has_any = any(secrets.get(k, "").strip() for k in llm_keys)
    if not has_any:
        log.critical(
            "STARTUP FAILURE: No LLM API key is set (%s). "
            "Every agent invocation will fail until at least one key is added to .env. "
            "Add one of these keys to .env and restart: %s",
            ", ".join(llm_keys),
            ", ".join(llm_keys),
        )
        return False
    else:
        # Warn on each key that is present but suspiciously short (likely a placeholder).
        for key in llm_keys:
            val = secrets.get(key, "").strip()
            if val and len(val) < 10:
                log.warning(
                    "Secret %s is set but appears too short (%d chars) — "
                    "check that it is not a placeholder value.",
                    key, len(val),
                )
    return True

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

    # ── Path-traversal guard (p13d) ────────────────────────────────────────────
    # ``folder`` is derived from config / DB, but validate that the resolved
    # host paths stay inside the expected parent directories.  An attacker who
    # can inject ``../`` sequences into the folder name would otherwise escape
    # the groups / data directories and mount arbitrary host paths into the
    # container (e.g. /etc, /root, the host's .ssh directory).
    def _assert_within(child: Path, parent: Path, label: str) -> None:
        try:
            child.resolve().relative_to(parent.resolve())
        except ValueError:
            raise ValueError(
                f"Security: resolved {label} path {child!r} is outside "
                f"expected parent {parent!r} — possible path traversal in folder={folder!r}"
            )

    group_host_path = groups_dir / folder
    _assert_within(group_host_path, groups_dir, "group")
    session_host_path = data_dir / "sessions" / folder
    _assert_within(session_host_path, data_dir / "sessions", "sessions")
    ipc_host_path = data_dir / "ipc" / folder
    _assert_within(ipc_host_path, data_dir / "ipc", "ipc")

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
    """Convert a folder name to a valid Docker container name segment.

    Strips every character that is not alphanumeric or a hyphen so that
    a JID or folder string containing path-traversal sequences (``../``,
    ``/``, ``.``) cannot escape the expected naming scheme or influence
    the volume-mount paths that embed ``folder`` directly.  The result is
    then truncated to 40 characters so the full container name stays
    within Docker's 63-character limit.
    """
    # Keep only alphanumeric chars and hyphens; replace everything else
    # (including dots, slashes, underscores) with a hyphen.
    safe = re.sub(r"[^a-zA-Z0-9-]", "-", folder)
    # Collapse consecutive hyphens and strip leading/trailing hyphens.
    safe = re.sub(r"-{2,}", "-", safe).strip("-")
    return safe[:40] or "group"

async def update_container_activity(container_name: str, activity: str) -> None:
    """Update the current_activity field for a running container (called from stderr stream)."""
    async with _get_active_lock():
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

    _circuit_remaining = _docker_circuit_open(folder)
    if _circuit_remaining:
        _wait_secs = int(_circuit_remaining) + 1  # round up so user isn't surprised by early retry
        # Fix: prefix with URGENT marker so main.py's on_error rate-limiter bypasses
        # the 5-minute cooldown.  Circuit breaker messages are rare (fired only after
        # _DOCKER_CIRCUIT_THRESHOLD consecutive failures) and must always reach the
        # user so they know when to retry; suppressing them causes silent hangs.
        await _notify_error(
            f"|||URGENT|||⚠️ 此群組 Docker 暫時受阻（連續失敗 {_DOCKER_CIRCUIT_THRESHOLD} 次），"
            f"請等待約 {_wait_secs} 秒後再試，屆時將自動恢復。其他群組不受影響。"
        )
        return {"status": "error", "error": f"Docker circuit breaker open for {folder}"}

    # Defense-in-depth concurrency guard (STABILITY_ANALYSIS 2.4):
    # Acquire the module-level semaphore so that even if a caller bypasses
    # GroupQueue's concurrency check, at most MAX_CONCURRENT_CONTAINERS
    # containers can execute simultaneously across all code paths.
    # Released unconditionally in the existing finally block below.
    await _get_container_semaphore().acquire()
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
    async with _get_active_lock():
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
        # ── Network isolation (p13d) ───────────────────────────────────────────
        # Agent containers must not make arbitrary outbound network calls.
        # LLM API calls are initiated by the host, not the container.
        "--network", "none",
        # ── Capability hardening (p13d) ────────────────────────────────────────
        # Drop all Linux capabilities; grant none back.  The agent only needs to
        # read/write files in mounted volumes — no raw sockets, no mknod, etc.
        "--cap-drop", "ALL",
        # ── Privilege escalation prevention (p13d) ─────────────────────────────
        # Prevent setuid/setgid binaries inside the container from gaining new
        # privileges (e.g. sudo, ping).
        "--security-opt", "no-new-privileges:true",
        # ── PID limit (p13d) ───────────────────────────────────────────────────
        # Prevent fork bombs: cap the number of processes the container can spawn.
        "--pids-limit", str(config.CONTAINER_PIDS_LIMIT),
    ]
    # ── Per-container resource limits (Issue #61) ──────────────────────────────
    # Prevent a runaway agent from OOM-killing the host process.
    # Both limits are opt-out: set CONTAINER_MEMORY="" or CONTAINER_CPUS="" to disable.
    if config.CONTAINER_MEMORY:
        cmd += ["--memory", config.CONTAINER_MEMORY, "--memory-swap", config.CONTAINER_MEMORY]
    if config.CONTAINER_CPUS:
        cmd += ["--cpus", config.CONTAINER_CPUS]
    # ── Container log size limit (BUG-19B-01) ─────────────────────────────────
    # Without --log-opt max-size Docker accumulates container log files on the
    # host indefinitely.  A long-running or verbose container (especially one
    # calling tool_write in a loop) can fill the host disk via the Docker
    # json-file log driver.  Cap at 10 MB per container with a single rotation
    # file so operators can still read the last chunk of output while disk usage
    # is bounded.
    cmd += ["--log-opt", f"max-size={config.CONTAINER_LOG_MAX_SIZE}",
            "--log-opt", f"max-file={config.CONTAINER_LOG_MAX_FILES}"]
    # ── Writable /tmp bounded via tmpfs (BUG-19B-02) ──────────────────────────
    # The container writes /tmp/input.json at startup (entrypoint.sh) and may
    # accumulate other temporary files during a run.  Without a size cap a
    # runaway agent can fill the host's overlay storage via the container layer.
    # Mount a dedicated tmpfs so /tmp is memory-backed and size-limited.
    cmd += ["--tmpfs", f"/tmp:size={config.CONTAINER_TMPFS_SIZE},mode=1777"]
    if uid is not None and gid is not None:
        cmd += ["--user", f"{uid}:{gid}"]
    cmd += [
        *mount_args,
        config.CONTAINER_IMAGE,
    ]

    log.info("Starting container %s for group %s (run_id=%s)", container_name, folder, run_id)
    _started_at = time.monotonic()
    db.log_container_start(run_id, jid, folder, container_name, time.time())

    input_bytes = input_json.encode("utf-8")

    proc = None  # asyncio subprocess reference — used for direct kill on CancelledError
    # p16c BUG-FIX (CRITICAL): stderr_lines must be initialised at function scope
    # before the platform branch.  On Windows the subprocess is run via
    # asyncio.to_thread(), so the Linux-only _stream_stderr() closure that
    # populates this list is never executed.  When no output markers are found
    # the error-reporting block at the bottom of the try references stderr_lines,
    # causing an unhandled NameError on Windows that masks the real failure and
    # prevents the on_error notification from being delivered.
    stderr_lines: list[str] = []
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

            log.debug("[DEBUG] Running docker in thread (Windows mode)...")
            try:
                stdout_data, stderr_data = await asyncio.to_thread(_sync_docker_run)
            except _subprocess.TimeoutExpired:
                # Fix: Windows subprocess.TimeoutExpired is NOT asyncio.TimeoutError,
                # so it would fall through to the generic Exception handler and produce
                # a confusing error message.  Re-raise as asyncio.TimeoutError so the
                # timeout handler below fires with the correct Chinese message.
                log.error("Container %s timed out after %ds (Windows)", folder, config.CONTAINER_TIMEOUT)
                raise asyncio.TimeoutError()
            log.debug("[DEBUG] Docker thread returned. stdout=%db stderr=%db", len(stdout_data), len(stderr_data))
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

            # stderr_lines already declared at function scope above (p16c fix); reuse it.
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
                        async with _get_active_lock():
                            if container_name in _active_containers:
                                _active_containers[container_name]["current_activity"] = safe_line

            async def _collect() -> tuple[bytes, bytes]:
                # Read stdout in chunks up to _MAX_OUTPUT_SIZE to prevent host OOM.
                # proc.stdout.read() with no limit would buffer the entire container
                # output — a runaway or malicious container emitting gigabytes of data
                # would exhaust host memory before the outer wait_for timeout fires.
                async def _read_stdout_bounded() -> bytes:
                    chunks: list[bytes] = []
                    total = 0
                    assert proc.stdout is not None
                    while True:
                        chunk = await proc.stdout.read(65536)  # 64 KiB at a time
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _MAX_OUTPUT_SIZE:
                            log.warning(
                                "Container %s stdout exceeded %d bytes — truncating",
                                container_name, _MAX_OUTPUT_SIZE,
                            )
                            chunks.append(chunk[:_MAX_OUTPUT_SIZE - (total - len(chunk))])
                            # Drain the rest without buffering to let the container exit
                            while await proc.stdout.read(65536):
                                pass
                            break
                        chunks.append(chunk)
                    return b"".join(chunks)

                stdout_task = asyncio.create_task(_read_stdout_bounded())
                stderr_task = asyncio.create_task(_stream_stderr())
                stdout_data, _ = await asyncio.gather(stdout_task, stderr_task)
                return stdout_data, b"\n".join(l.encode() for l in stderr_lines)

            stdout_data, stderr_data = await asyncio.wait_for(
                _collect(),
                timeout=config.CONTAINER_TIMEOUT,
            )

        stdout = stdout_data.decode(errors="replace")
        stderr = stderr_data.decode(errors="replace")

        log.debug("[DEBUG] Container stdout preview: %r", stdout[:200])
        if stderr:
            log.debug("[DEBUG] Container stderr: %s", _redact_secrets(stderr[:500]))

        # 從 stdout 中尋找輸出標記，截取 JSON 結果
        # agent 可能在標記前後有其他 debug 輸出，只取標記之間的部分。
        # Fix: use rfind for OUTPUT_START so that if the agent emits multiple
        # output sections (e.g. partial output followed by a retry), only the
        # last (most complete) section is used.  OUTPUT_END is searched forward
        # from that last START position so we always get the matching pair.
        start_idx = stdout.rfind(OUTPUT_START)
        end_idx = stdout.find(OUTPUT_END, start_idx + len(OUTPUT_START)) if start_idx != -1 else -1

        if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
            # 找不到標記代表 container 在輸出結果前就結束了。
            # Fix: if the START marker was found but END was not, stdout was likely
            # truncated at the 2MB limit mid-output.  Emit a more specific error so
            # operators understand the root cause (output too large) vs a crash.
            if start_idx != -1 and end_idx == -1:
                log.error(
                    "Container %s: OUTPUT_START found but OUTPUT_END missing — "
                    "stdout was likely truncated at the %d-byte limit. "
                    "The agent's JSON output is too large; consider reducing response size.",
                    container_name, _MAX_OUTPUT_SIZE,
                )
                await _notify_error(
                    "⚠️ AI 回應內容超過大小限制，輸出不完整，請嘗試縮短請求。"
                )
                response_ms = int((time.time() - t0) * 1000)
                record_run(jid, run_id, response_ms, retry_count=0, success=False)
                db.log_container_finish(run_id, time.time(), "error", _redact_secrets(stderr) if stderr else "", stdout[:200] if stdout else "", response_ms)
                _record_docker_success(folder)  # Docker ran fine; the agent's output was too big
                return {"status": "error", "error": "output truncated (too large)", "messages": []}

            # 找不到標記代表 container 在輸出結果前就結束了
            # Fix: avoid shadowing the outer `stderr_lines` list collected from
            # streaming stderr; use a distinct local name for the split lines
            # so the context bundled into the monitor error message is correct.
            _stderr_split_lines = stderr.splitlines() if stderr else []
            if _stderr_split_lines:
                log.warning(
                    "Container %s stderr (last 5 lines):\n%s",
                    container_name,
                    "\n".join(_redact_secrets(l) for l in _stderr_split_lines[-5:])
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

            # p16c BUG-FIX (MEDIUM): distinguish OOM (exit 137) in the user-facing
            # on_error notification.  Previously the OOM path emitted the same generic
            # Chinese message as any other crash, leaving operators and users with no
            # indication that the memory limit was breached and should be raised.
            # We now emit a specific OOM message for exit 137 and keep the generic
            # message for all other non-zero exits.
            _exit_code = proc.returncode if proc is not None else None
            if _container_ran:
                # Log OOM explicitly so operators can act (raise --memory limit)
                if _exit_code == 137:
                    log.error(
                        "Container %s was OOM-killed (exit 137). "
                        "Consider raising CONTAINER_MEMORY in config (currently %r). "
                        "Circuit breaker NOT tripped — Docker daemon is healthy.",
                        container_name, config.CONTAINER_MEMORY,
                    )
                    # p16d: inform the user that the task was killed due to memory limits
                    # so they can simplify their request rather than wondering about silence.
                    await _notify_error(
                        "⚠️ AI 執行時記憶體不足（已被系統終止），請嘗試縮短或簡化您的請求，系統將自動重試。"
                    )
                else:
                    log.info("Container %s crashed before emit() but Docker is healthy — resetting circuit breaker", container_name)
                _record_docker_success(folder)
            else:
                # Exit code indicates Docker itself failed (not an agent-level issue)
                log.warning("Container exit code %s indicates Docker daemon issue — recording failure", _exit_code if _exit_code is not None else '?')
                _record_docker_failure(folder)
            # Bundle stderr context for monitor channel (separator parsed by on_error in main.py)
            # Use the streaming-collected stderr_lines when available (non-Windows path),
            # fall back to split lines otherwise.
            _ctx_source = stderr_lines if stderr_lines else _stderr_split_lines
            _stderr_ctx_lines = [_redact_secrets(l) for l in (_ctx_source or [])[-15:]]
            _stderr_ctx = (
                f"container: {container_name}\n"
                f"exit_code: {_exit_code if _exit_code is not None else '?'}\n"
                f"stderr (last {len(_stderr_ctx_lines)} lines):\n" +
                ("\n".join(_stderr_ctx_lines) if _stderr_ctx_lines else "(empty)")
            )
            # User-visible message: OOM gets a specific hint; other failures get the generic message.
            if _exit_code == 137:
                _user_msg = (
                    "⚠️ 系統記憶體不足（容器被強制終止），請稍後再試。"
                    "如果問題持續請通知管理員調高記憶體上限。"
                )
            else:
                _user_msg = "⚠️ 系統暫時發生問題，請稍後再傳訊息，會自動重試。"
            await _notify_error(_user_msg + "|||MONITOR_CONTEXT|||" + _stderr_ctx)
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

        # Fix: validate that the parsed JSON is a dict (not a list/scalar) so
        # downstream code calling result.get() never raises AttributeError.
        # A non-dict payload indicates a schema mismatch — treat it as an error
        # so GroupQueue retries rather than silently delivering broken output.
        if not isinstance(result, dict):
            log.error(
                "Container output schema mismatch: expected dict, got %s | raw=%r",
                type(result).__name__, raw[:200],
            )
            response_ms = int((time.time() - t0) * 1000)
            record_run(jid, run_id, response_ms, retry_count=0, success=False)
            db.log_container_finish(run_id, time.time(), "error", _redact_secrets(stderr) if stderr else "", stdout[:200] if stdout else "", response_ms)
            _record_docker_failure(folder)
            _schema_ctx = (
                f"container: {container_name}\n"
                f"schema_error: expected dict, got {type(result).__name__}\n"
                f"stdout_raw (first 300):\n{_redact_secrets(raw[:300]) if raw else '(empty)'}"
            )
            await _notify_error("⚠️ AI 回應格式異常，將自動重試，無需任何操作。|||MONITOR_CONTEXT|||" + _schema_ctx)
            return {"status": "error", "error": "schema mismatch: result is not a dict", "messages": []}


        # 三層記憶系統：container 可透過 memory_patch 欄位更新熱記憶
        # agent 在回覆中附上新的記憶內容，host 自動寫入熱記憶供下次對話使用
        # p16c NOTE (LOW): as of the current agent.py, emit() never includes
        # "memory_patch" — this field was planned but not yet wired up on the
        # container side, so this branch is dead code for now.  When the agent
        # is extended to produce memory patches, include this field in the
        # emit({...}) call in container/agent-runner/agent.py.
        # p16c BUG-FIX (MEDIUM): validate that memory_patch is a non-empty string
        # before calling update_hot_memory().  A non-string value (e.g. dict, list)
        # would propagate to content.encode("utf-8") in hot.py and raise AttributeError,
        # which was silently swallowed — the bad patch was never applied but the
        # failure reason was obscured.  Explicit type + emptiness guard makes the
        # error immediately visible and prevents future regressions if update_hot_memory
        # ever removes its own try/except.
        _memory_patch = result.get("memory_patch") if isinstance(result, dict) else None
        if _memory_patch:
            if not isinstance(_memory_patch, str):
                log.warning(
                    "hot_memory: memory_patch for jid=%s has unexpected type %s — skipping update",
                    jid, type(_memory_patch).__name__,
                )
            else:
                try:
                    update_hot_memory(jid, _memory_patch)
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
                # p17c BUG-FIX (LOW): open() + write() are blocking syscalls that
                # can stall the event loop on a slow filesystem (NFS, overlayfs,
                # disk pressure).  Run in an executor so other coroutines are not
                # blocked while this small write completes.
                def _write_mem():
                    with open(_host_memory_path, "a", encoding="utf-8") as _mf:
                        _mf.write(_auto_entry)
                await asyncio.get_running_loop().run_in_executor(None, _write_mem)
                log.info("host auto-wrote MEMORY.md fallback entry for %s", folder)
            else:
                log.debug("MEMORY.md already updated by agent for %s", folder)
        except Exception as _auto_mem_exc:
            log.warning("host auto-write MEMORY.md failed for %s: %s", folder, _auto_mem_exc)

        # p15b-fix: advance the cursor (on_success) BEFORE delivering the reply
        # to the user (on_output).  Previously on_output fired first — if
        # on_success then raised (e.g. DB error), the cursor was never advanced,
        # the message loop retried, and the user received a duplicate reply.
        # Advancing the cursor first is safe: if on_output then fails, the user
        # simply does not see the reply for this run; the message will NOT be
        # retried (cursor already advanced) but the silent drop is far less
        # disruptive than a duplicate.  Operators will see the on_output error
        # in logs and can investigate.
        if on_success:
            await on_success()

        # 若 container 有產生回覆文字，透過 on_output callback 發送到聊天室
        result_text = result.get("result")
        if on_output and result_text:
            await on_output(result_text)

        return result

    except asyncio.TimeoutError:
        # 超時：強制停止 container，避免佔用資源；不呼叫 on_success
        log.error("Container %s timed out after %ds", folder, config.CONTAINER_TIMEOUT)
        await _stop_container(container_name)
        # 記錄超時失敗數據（適應度扣分）
        _timeout_ms = int(config.CONTAINER_TIMEOUT * 1000)
        record_run(jid, run_id, _timeout_ms, retry_count=0, success=False)
        db.log_container_finish(run_id, time.time(), "timeout", "Container timed out", "", _timeout_ms)
        _record_docker_failure(folder)
        # Fix: show a human-readable timeout limit (minutes if ≥ 60s) so the
        # user understands this was a timeout (not another kind of error) and
        # roughly how long the system waited before giving up.
        _to_secs = int(config.CONTAINER_TIMEOUT)
        _to_display = f"{_to_secs // 60} 分鐘" if _to_secs >= 60 else f"{_to_secs} 秒"
        await _notify_error(
            f"⏱️ 這個請求超過 {_to_display} 仍未完成（逾時），系統將自動重試，請稍候。"
        )
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
        log.error("Container %s error: %s", folder, e)
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
        async with _get_active_lock():
            _active_containers.pop(container_name, None)
        # Release the defense-in-depth semaphore acquired before execution
        # (STABILITY_ANALYSIS 2.4).  Placed last so active-container cleanup
        # runs first; wrapped in try/except so a NameError for container_name
        # above cannot prevent the semaphore from being released.
        try:
            _get_container_semaphore().release()
        except Exception:
            pass

async def _stop_container(name: str) -> None:
    """Stop a container on timeout using a two-phase SIGTERM → SIGKILL sequence.

    BUG-19B-03 FIX: previously this function issued docker kill (SIGKILL)
    immediately, giving the agent process no opportunity to flush its output
    buffers, close open files, or write a partial result to the IPC results
    directory.  A sudden SIGKILL can corrupt in-progress writes to the shared
    workspace volume.

    New behaviour:
      1. docker stop --time <grace> sends SIGTERM and waits up to
         CONTAINER_STOP_GRACE_SECS for a clean exit.
      2. If the container has not exited within the grace period Docker sends
         SIGKILL automatically — no second command is needed.
      3. If docker stop itself fails (container already gone, daemon hiccup)
         we fall back to docker rm -f to free the name slot.

    The grace period is intentionally short (default 5 s) so a timed-out
    container does not delay shutdown by more than 5 extra seconds.
    """
    _stop_grace = str(config.CONTAINER_STOP_GRACE_SECS)
    _stop_ok = False
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stop", "--time", _stop_grace, name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Wait slightly longer than the grace period so the SIGKILL has time to fire.
        await asyncio.wait_for(proc.wait(), timeout=int(_stop_grace) + 3.0)
        _stop_ok = (proc.returncode == 0)
    except Exception as _ke:
        log.debug("docker stop %s failed (%s) — attempting docker rm -f fallback", name, _ke)
    if not _stop_ok:
        # Fallback: force-remove the container so the name slot and resources are freed.
        try:
            rm_proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(rm_proc.wait(), timeout=5.0)
        except Exception as _rme:
            log.debug("docker rm -f %s also failed: %s", name, _rme)


async def kill_all_containers() -> None:
    """強制 kill 所有正在追蹤的 container（shutdown 時呼叫）。
    使用 docker kill（SIGKILL）確保即時終止，不等待 grace period。
    """
    async with _get_active_lock():
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

    p15d BUG-FIX (HIGH): previously used `docker ps` (only RUNNING containers).
    After a SIGKILL the Python process dies before Docker's --rm cleanup runs,
    leaving containers in the "Exited" state that `docker ps` (without -a) does
    NOT list.  These stopped-but-not-removed containers block future runs with
    the same name and waste storage.  Use `docker ps -a` to catch all states.
    """
    try:
        # -a: include stopped containers (Exited, Created, etc.) not just running ones.
        # This is critical for post-SIGKILL recovery where --rm never fired.
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "-q", "--filter", "name=evoclaw-",
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        ids = [i for i in out.decode().split() if i]
        if ids:
            rm_proc = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", *ids,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(rm_proc.wait(), timeout=15.0)
            log.info("Cleaned up %d orphan container(s) (running + stopped)", len(ids))
        else:
            log.debug("No orphan containers found at startup")
    except asyncio.TimeoutError:
        log.warning("cleanup_orphans timed out — Docker may be slow; orphans may remain")
    except Exception as e:
        log.warning("Orphan cleanup failed: %s", e)

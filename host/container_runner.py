"""Spawns and manages agent execution in Docker containers"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Callable, Awaitable

from . import config, db
from .env import read_env_file
from .evolution import record_run, get_adaptive_hints, get_genome_style_hints

log = logging.getLogger(__name__)


def _docker_path(p) -> str:
    """Convert path to Docker-compatible forward-slash format."""
    return str(p).replace("\\", "/")


# container 輸出的邊界標記，用於從 stdout 中精確截取 JSON 結果
# 使用不常見的字串避免與 agent 的正常輸出衝突
OUTPUT_START = "---EVOCLAW_OUTPUT_START---"
OUTPUT_END = "---EVOCLAW_OUTPUT_END---"

def _read_secrets() -> dict:
    """從 .env 檔案讀取敏感金鑰（API key 等），以字典形式回傳給 container。"""
    return read_env_file(["GOOGLE_API_KEY", "TELEGRAM_BOT_TOKEN", "GEMINI_MODEL", "ASSISTANT_NAME", "NIM_API_KEY", "NIM_MODEL", "NIM_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL", "CLAUDE_API_KEY", "CLAUDE_MODEL"])

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
    for sub in ["messages", "tasks", "input"]:
        (ipc_dir / sub).mkdir(parents=True, exist_ok=True)
    mounts.append(f"{_docker_path(ipc_dir)}:/workspace/ipc:rw")

    return mounts

def _safe_name(folder: str) -> str:
    """將 folder 名稱轉換為合法的 Docker container 名稱（底線換連字號，截斷過長部分）。"""
    return folder.replace("_", "-")[:40]

async def run_container_agent(
    group: dict,
    prompt: str,
    on_output: Optional[Callable[[str], Awaitable[None]]] = None,
    session_id: Optional[str] = None,
    is_scheduled_task: bool = False,
    on_success: Optional[Callable[[], Awaitable[None]]] = None,
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
    """
    folder = group["folder"]
    jid = group["jid"]
    # 用時間戳記讓 container 名稱唯一，方便 debug 與孤兒清理
    run_id = str(uuid.uuid4())
    container_name = f"evoclaw-{_safe_name(folder)}-{int(time.time())}"

    mounts = _build_volume_mounts(group)
    mount_args = []
    for m in mounts:
        mount_args += ["-v", m]

    # ── 演化提示（表觀遺傳）：根據環境和群組基因組動態附加提示 ─────────────────
    # 這些提示不修改 CLAUDE.md，只在本次 container 執行時附加，
    # 讓 AI 在不同環境下表現不同（例如系統忙碌時自動簡短回答）
    evolution_hints = get_adaptive_hints(jid) + get_genome_style_hints(jid)

    # 將 API 金鑰等敏感資料包進 input_data，透過 stdin 傳給 container
    secrets = _read_secrets()
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
    }
    input_json = json.dumps(input_data, ensure_ascii=True)
    # 記錄 container 啟動時間，用於計算回應時間（適應度追蹤）
    t0 = time.time()

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
    ]
    if uid is not None and gid is not None:
        cmd += ["--user", f"{uid}:{gid}"]
    cmd += [
        *mount_args,
        config.CONTAINER_IMAGE,
    ]

    log.info(f"Starting container {container_name} for group {folder}")

    input_bytes = input_json.encode("utf-8")

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

            log.info(f"[DEBUG] Running docker in thread (Windows mode)...")
            stdout_data, stderr_data = await asyncio.to_thread(_sync_docker_run)
            log.info(f"[DEBUG] Docker thread returned. stdout={len(stdout_data)}b stderr={len(stderr_data)}b")
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(input_bytes),
                timeout=config.CONTAINER_TIMEOUT,
            )

        stdout = stdout_data.decode(errors="replace")
        stderr = stderr_data.decode(errors="replace")

        log.info(f"[DEBUG] Container stdout preview: {stdout[:200]!r}")
        if stderr:
            log.warning(f"[DEBUG] Container stderr: {stderr[:500]}")

        # 從 stdout 中尋找輸出標記，截取 JSON 結果
        # agent 可能在標記前後有其他 debug 輸出，只取標記之間的部分
        start_idx = stdout.find(OUTPUT_START)
        end_idx = stdout.find(OUTPUT_END)

        if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
            # 找不到標記代表 container 可能在輸出結果前就崩潰了
            log.warning("No valid output markers in container stdout")
            return {"status": "error", "error": "no output markers", "messages": []}

        # 截取兩個標記之間的內容並解析為 JSON
        raw = stdout[start_idx + len(OUTPUT_START):end_idx].strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("Container output JSON parse error: %s | raw=%r", e, raw[:200])
            return {"status": "error", "error": f"JSON parse error: {e}", "messages": []}

        # 若 container 有產生回覆文字，透過 on_output callback 發送到聊天室
        if on_output and result.get("result"):
            await on_output(result["result"])

        # 更新 session ID：agent 執行後可能建立新的 session，存入 DB 供下次使用
        if result.get("newSessionId"):
            db.set_session(folder, result["newSessionId"])

        # container 成功完成：通知呼叫方可以安全推進游標
        # 這是 rollback 安全機制的最後一步 — 只有到這裡才確認「已處理完畢」
        response_ms = int((time.time() - t0) * 1000)
        # 記錄成功執行數據到演化引擎（適應度追蹤）
        record_run(jid, run_id, response_ms, retry_count=0, success=True)

        if on_success:
            await on_success()

        return result

    except asyncio.TimeoutError:
        # 超時：強制停止 container，避免佔用資源；不呼叫 on_success
        log.error(f"Container {folder} timed out after {config.CONTAINER_TIMEOUT}s")
        await _stop_container(container_name)
        # 記錄超時失敗數據（適應度扣分）
        record_run(jid, run_id, int(config.CONTAINER_TIMEOUT * 1000), retry_count=0, success=False)
        return {"status": "error", "result": None, "error": "Container timed out"}
    except Exception as e:
        log.error(f"Container {folder} error: {e}")
        response_ms = int((time.time() - t0) * 1000)
        # 記錄異常失敗數據
        record_run(jid, run_id, response_ms, retry_count=0, success=False)
        return {"status": "error", "result": None, "error": str(e)}

async def _stop_container(name: str) -> None:
    """發送 docker stop 指令強制停止指定 container（超時時呼叫）。"""
    try:
        await asyncio.create_subprocess_exec("docker", "stop", name)
    except Exception:
        pass

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
            await asyncio.create_subprocess_exec("docker", "rm", "-f", *ids)
            log.info(f"Cleaned up {len(ids)} orphan containers")
    except Exception as e:
        log.warning(f"Orphan cleanup failed: {e}")

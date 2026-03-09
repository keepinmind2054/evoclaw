"""IPC file watcher — processes agent-to-host messages"""
import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Callable, Awaitable

from . import config, db
from .group_folder import is_valid_group_folder

_running = True

log = logging.getLogger(__name__)

async def process_ipc_dir(group_folder: str, is_main: bool, route_fn: Callable) -> None:
    """
    掃描某個群組的 IPC 目錄，處理所有待辦的 JSON 指令檔案。

    IPC 目錄結構：
        data/ipc/<group_folder>/
            messages/   ← container 要發送給用戶的訊息（type: "message"）
            tasks/      ← container 要建立或管理的排程任務（type: "schedule_task" 等）

    每個 JSON 檔案處理完畢後立即刪除（避免重複執行）。
    若處理失敗，檔案移動到 data/ipc/errors/ 目錄供事後診斷，
    而不是直接刪除，以便排查問題。
    """
    ipc_dir = config.DATA_DIR / "ipc" / group_folder
    msg_dir = ipc_dir / "messages"
    task_dir = ipc_dir / "tasks"

    for d in [msg_dir, task_dir]:
        if not d.exists():
            continue
        # sorted() 確保按檔名（時間戳記前綴）的 FIFO 順序處理
        for f in sorted(d.glob("*.json")):
            try:
                content = f.read_text()
                try:
                    payload = json.loads(content)
                except json.JSONDecodeError as e:
                    log.error("IPC JSON parse error in %s: %s", f.name, e)
                    # Move to errors dir instead of deleting
                    errors_dir = f.parent.parent / "errors"
                    errors_dir.mkdir(exist_ok=True)
                    f.rename(errors_dir / f.name)
                    continue
                await _handle_ipc(payload, group_folder, is_main, route_fn)
                f.unlink(missing_ok=True)  # 成功後刪除，避免重複處理
            except Exception as e:
                log.error(f"IPC error processing {f}: {e}")
                # 失敗時移到 errors/ 目錄而非直接刪除，方便事後診斷
                err_dir = config.DATA_DIR / "ipc" / "errors"
                err_dir.mkdir(parents=True, exist_ok=True)
                try:
                    f.rename(err_dir / f.name)
                except Exception:
                    f.unlink(missing_ok=True)  # 若 rename 也失敗才刪除

async def _handle_ipc(payload: dict, group_folder: str, is_main: bool, route_fn: Callable) -> None:
    """
    根據 IPC 訊息的 type 欄位分派到對應的處理邏輯。

    所有 IPC 訊息都帶有 type 欄位，目前支援的類型：
    - "message"：將文字訊息發送到指定聊天室
    - "schedule_task"：建立新的排程任務
    - "pause_task" / "resume_task" / "cancel_task"：管理現有任務狀態
    - "update_task"：修改排程任務的 prompt 或時間設定
    - "register_group"：登記新的群組（僅主群組可用）
    - "refresh_groups"：通知 host 重新載入群組清單
    """
    msg_type = payload.get("type")

    if msg_type == "message":
        # 直接路由：把 container 的訊息透過 route_fn 發送到對應的聊天室
        jid = payload.get("chatJid", "")
        text = payload.get("text", "")
        if jid and text:
            await route_fn(jid, text, payload.get("sender"))

    elif msg_type == "schedule_task":
        # 建立新排程任務：先驗證權限，再計算下次執行時間，最後寫入 DB
        _require_own_or_main(group_folder, payload.get("groupFolder", group_folder), is_main)
        task_id = str(uuid.uuid4())
        schedule_type = payload.get("schedule_type", "")
        schedule_value = payload.get("schedule_value", "")
        # 計算下次執行的 Unix timestamp（毫秒），根據 schedule_type 不同邏輯不同
        next_run = _compute_next_run(schedule_type, schedule_value)
        group = db.get_registered_group(payload.get("chatJid", ""))
        db.create_task(
            task_id=task_id,
            group_folder=group_folder,
            chat_jid=payload.get("chatJid", ""),
            prompt=payload.get("prompt", ""),
            schedule_type=schedule_type,
            schedule_value=schedule_value,
            next_run=next_run,
            context_mode=payload.get("context_mode", "group"),
        )
        log.info(f"Task created: {task_id} for {group_folder}")

    elif msg_type in ("pause_task", "resume_task", "cancel_task"):
        # 任務狀態管理：先驗證該群組有權限操作此任務，再更新 DB
        task_id = payload.get("task_id", "")
        task = _get_authorized_task(task_id, group_folder, is_main)
        if task:
            if msg_type == "pause_task":
                db.update_task(task_id, status="paused")
            elif msg_type == "resume_task":
                db.update_task(task_id, status="active")
            elif msg_type == "cancel_task":
                db.delete_task(task_id)

    elif msg_type == "update_task":
        # 更新任務設定：若排程規則有變，需同時重新計算下次執行時間
        task_id = payload.get("task_id", "")
        task = _get_authorized_task(task_id, group_folder, is_main)
        if task:
            updates = {}
            for key in ("prompt", "schedule_type", "schedule_value", "context_mode"):
                if key in payload:
                    updates[key] = payload[key]
            if "schedule_type" in updates or "schedule_value" in updates:
                # 排程規則改變，重新計算 next_run
                st = updates.get("schedule_type", task["schedule_type"])
                sv = updates.get("schedule_value", task["schedule_value"])
                updates["next_run"] = _compute_next_run(st, sv)
            db.update_task(task_id, **updates)

    elif msg_type == "register_group":
        # 登記新群組：只有主群組的 container 才能呼叫此操作
        # 防止一般群組的 agent 未經授權擴展系統存取範圍
        if not is_main:
            raise PermissionError("Only main group can register new groups")
        jid = payload.get("jid", "")
        name = payload.get("name", "")
        folder = payload.get("folder", "")
        trigger = payload.get("trigger", f"@{config.ASSISTANT_NAME}")
        if jid and name and folder:
            db.set_registered_group(
                jid=jid,
                name=name,
                folder=folder,
                trigger_pattern=trigger,
                container_config=None,
                requires_trigger=True,
                is_main=False,
            )
            # 建立群組資料夾，container 掛載時需要此目錄存在
            (config.GROUPS_DIR / folder).mkdir(parents=True, exist_ok=True)
            log.info(f"Group registered via IPC: {folder}")

    elif msg_type == "refresh_groups":
        # 寫入旗標檔案通知 _message_loop 重新從 DB 載入群組清單
        # 用檔案旗標（而非直接呼叫函式）是因為 IPC watcher 與 message loop
        # 在不同的 asyncio task 中，透過旗標可以避免跨 task 的直接耦合
        flag = config.DATA_DIR / "refresh_groups.flag"
        flag.write_text("1")
        log.info("Groups refresh requested via IPC")

def _require_own_or_main(group_folder: str, target_folder: str, is_main: bool) -> None:
    """
    權限模型：一般群組的 container 只能管理自己的資源。
    只有主群組（is_main=True）的 container 可以跨群組操作。
    這防止不同群組的 agent 互相干擾或越權存取。
    """
    if not is_main and group_folder != target_folder:
        raise PermissionError(f"Group {group_folder} cannot manage {target_folder}")

def _get_authorized_task(task_id: str, group_folder: str, is_main: bool) -> dict | None:
    """
    查找任務並驗證操作權限。
    - 任務不存在：回傳 None
    - 一般群組嘗試操作其他群組的任務：記錄警告並回傳 None
    - 主群組可以操作所有任務
    """
    tasks = db.get_all_tasks()
    task = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        return None
    if not is_main and task["group_folder"] != group_folder:
        log.warning(f"Unauthorized task access by {group_folder}")
        return None
    return task

def _compute_next_run(schedule_type: str, schedule_value: str) -> int | None:
    """
    計算任務的下次執行時間（Unix timestamp，毫秒單位）。

    三種排程類型：
    - "once"：解析 ISO 8601 時間字串（如 "2026-03-07T15:30:00"），只執行一次
    - "interval"：schedule_value 為毫秒數，下次 = 現在 + interval
    - "cron"：使用 croniter 函式庫解析 cron 表達式（如 "0 9 * * *"），
              計算下次符合條件的時間點

    croniter 使用 cron 表達式是因為 cron 是業界標準格式，
    表達「每天早上9點」等週期性時間比 interval 更直觀。
    """
    now_ms = int(time.time() * 1000)
    if schedule_type == "once":
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(schedule_value)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    elif schedule_type == "interval":
        try:
            # schedule_value 單位是毫秒，直接加到現在時間上
            return now_ms + int(schedule_value)
        except Exception:
            return None
    elif schedule_type == "cron":
        try:
            from croniter import croniter
            # croniter(cron_expr, start_time) 計算 start_time 之後的下次執行時間
            c = croniter(schedule_value, time.time())
            return int(c.get_next() * 1000)
        except Exception:
            return None
    return None

async def start_ipc_watcher(get_groups_fn: Callable, route_fn: Callable, stop_event: asyncio.Event) -> None:
    """
    IPC 監控主迴圈：每隔 IPC_POLL_INTERVAL 秒掃描所有群組的 IPC 目錄。

    之所以用輪詢（polling）而非 inotify/watchdog 等檔案系統事件，
    是為了保持跨平台相容性，並簡化 container volume mount 的互動邏輯。
    """
    log.info("IPC watcher started")
    while _running:
        try:
            groups = get_groups_fn()
            for group in groups:
                await process_ipc_dir(group["folder"], bool(group.get("is_main")), route_fn)
        except Exception as e:
            log.error(f"IPC watcher error: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.IPC_POLL_INTERVAL)
            break  # stop_event was set, exit loop
        except asyncio.TimeoutError:
            pass  # Normal cycle, continue

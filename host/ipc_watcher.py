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
import asyncio as _asyncio

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
    - "spawn_agent"：啟動子 agent 並將結果寫入 results/ 目錄
    - "dev_task"：觸發 DevEngine 7 階段開發流程
    - "apply_skill"：安裝 Skill Plugin（僅主群組可用）
    - "uninstall_skill"：移除 Skill Plugin（僅主群組可用）
    - "list_skills"：列出已安裝的 Skills（任何群組均可查詢）
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
        chat_jid = payload.get("chatJid", "")
        # Fallback: if chatJid missing (e.g. old Docker image), look up from registered_groups
        if not chat_jid:
            _groups = db.get_all_registered_groups()
            _match = next((g for g in _groups if g.get("folder") == group_folder), None)
            if _match:
                chat_jid = _match.get("jid", "")
                log.warning(f"schedule_task: chatJid missing in payload, resolved from DB: {chat_jid!r}")
        db.create_task(
            task_id=task_id,
            group_folder=group_folder,
            chat_jid=chat_jid,
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

    elif msg_type == "apply_skill":
        # 安裝 Skill Plugin：只有主群組的 container 才能呼叫（避免一般群組隨意修改框架檔案）
        # skill_path 可以是本地路徑或 URL（由 skills_engine.apply_skill 處理）
        if not is_main:
            raise PermissionError("Only main group can apply skills")
        skill_path = payload.get("skill_path", "")
        request_id = payload.get("requestId", "")
        if skill_path:
            _asyncio.ensure_future(_run_apply_skill(skill_path, request_id, group_folder, route_fn))

    elif msg_type == "uninstall_skill":
        # 移除 Skill Plugin：只有主群組的 container 才能呼叫
        if not is_main:
            raise PermissionError("Only main group can uninstall skills")
        skill_name = payload.get("skill_name", "")
        request_id = payload.get("requestId", "")
        if skill_name:
            _asyncio.ensure_future(_run_uninstall_skill(skill_name, request_id, group_folder, route_fn))

    elif msg_type == "list_skills":
        # 列出已安裝的 Skills：任何群組都可以查詢（唯讀操作）
        # 結果寫入 results 目錄（供 container 輪詢讀取），同時如有 requestId 也可 route 回訊息
        request_id = payload.get("requestId", "")
        _asyncio.ensure_future(_run_list_skills(request_id, group_folder, route_fn))

    elif msg_type == "spawn_agent":
        # 執行子 agent 並將結果寫回 results 目錄
        request_id = payload.get("requestId", "")
        prompt = payload.get("prompt", "")
        context_mode = payload.get("context_mode", "isolated")
        if request_id and prompt:
            # 找出目前正在執行此群組的父 container（用於 dashboard 親子關係顯示）
            parent_name = _find_parent_container(group_folder)
            _asyncio.ensure_future(_run_subagent(request_id, prompt, context_mode, group_folder, parent_name))

    elif msg_type == "dev_task":
        # 觸發 DevEngine 7 階段開發流程
        # agent 可以寫入此 IPC 訊息來啟動自動化開發任務
        dev_prompt = payload.get("prompt", "")
        mode = payload.get("mode", "auto")  # "auto" | "interactive"
        session_id = payload.get("session_id", "")  # 若提供則 resume，否則新建
        if dev_prompt or session_id:
            _asyncio.ensure_future(_run_dev_task(
                dev_prompt, mode, session_id, group_folder, route_fn
            ))

async def _run_dev_task(
    prompt: str, mode: str, session_id: str,
    group_folder: str, route_fn,
) -> None:
    """
    在背景執行 DevEngine 7 階段開發流程。
    每個階段完成後透過 route_fn 發送進度通知給用戶。
    """
    from .dev_engine import DevEngine, load_session
    try:
        groups = db.get_all_registered_groups()
        group = next((g for g in groups if g["folder"] == group_folder), None)
        if not group:
            log.error(f"DevEngine IPC: group {group_folder} not found")
            return

        jid = group["jid"]
        engine = DevEngine(jid=jid)

        async def notify(text: str) -> None:
            try:
                await route_fn(jid, text)
            except Exception as e:
                log.warning(f"DevEngine notify error: {e}")

        if session_id:
            # Resume existing session
            session = load_session(session_id)
            if not session:
                await notify(f"❌ DevEngine: session `{session_id}` not found")
                return
            await notify(f"▶️ DevEngine resuming session `{session_id}`...")
            await engine.resume(session_id, group=group, notify_fn=notify)
        else:
            # New session
            await notify(f"🚀 DevEngine 啟動（mode={mode}）\n> {prompt[:100]}")
            session = await engine.start(prompt=prompt, mode=mode)
            await engine.run(session, group=group, notify_fn=notify)

    except Exception as e:
        log.error(f"DevEngine IPC error: {e}")


async def _run_apply_skill(
    skill_path: str, request_id: str, group_folder: str, route_fn: Callable,
) -> None:
    """
    在 thread executor 中執行 skills_engine.apply_skill()（同步函式）。
    完成後透過 route_fn 發送結果，並（若提供 requestId）寫入 results 目錄。
    """
    import sys, os
    # 確保 skills_engine 在 Python path 中（從 host/ 往上一層找）
    root_dir = str(Path(__file__).parent.parent)
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    try:
        from skills_engine import apply_skill
        groups = db.get_all_registered_groups()
        group = next((g for g in groups if g["folder"] == group_folder), None)
        jid = group["jid"] if group else ""

        # skills_engine.apply_skill 是同步函式，用 executor 避免阻塞 event loop
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, apply_skill, skill_path)

        if result.success:
            msg = f"✅ Skill applied: {result.skill} v{result.version}"
        else:
            msg = f"❌ Skill apply failed: {result.error}"
            if result.merge_conflicts:
                msg += f"\nConflicts: {', '.join(result.merge_conflicts)}"

        if jid:
            await route_fn(jid, msg)

        # 若有 requestId，將結果寫入 results 目錄供 container 輪詢讀取
        if request_id:
            result_dir = config.DATA_DIR / "ipc" / group_folder / "results"
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / f"{request_id}.json").write_text(
                json.dumps({
                    "requestId": request_id,
                    "output": msg,
                    "success": result.success,
                    "skill": result.skill,
                    "version": result.version,
                }),
                encoding="utf-8",
            )
    except Exception as e:
        log.error(f"apply_skill IPC error: {e}")


async def _run_uninstall_skill(
    skill_name: str, request_id: str, group_folder: str, route_fn: Callable,
) -> None:
    """
    在 thread executor 中執行 skills_engine.uninstall_skill()（同步函式）。
    """
    import sys
    root_dir = str(Path(__file__).parent.parent)
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    try:
        from skills_engine import uninstall_skill
        groups = db.get_all_registered_groups()
        group = next((g for g in groups if g["folder"] == group_folder), None)
        jid = group["jid"] if group else ""

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, uninstall_skill, skill_name)

        if result.success:
            msg = f"✅ Skill uninstalled: {result.skill}"
            if result.custom_patch_warning:
                msg += f"\n⚠️ {result.custom_patch_warning}"
        else:
            msg = f"❌ Skill uninstall failed: {result.error}"

        if jid:
            await route_fn(jid, msg)

        if request_id:
            result_dir = config.DATA_DIR / "ipc" / group_folder / "results"
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / f"{request_id}.json").write_text(
                json.dumps({
                    "requestId": request_id,
                    "output": msg,
                    "success": result.success,
                }),
                encoding="utf-8",
            )
    except Exception as e:
        log.error(f"uninstall_skill IPC error: {e}")


async def _run_list_skills(
    request_id: str, group_folder: str, route_fn: Callable,
) -> None:
    """
    列出已安裝的 Skills，將結果寫入 results 目錄（唯讀，任何群組均可查詢）。
    """
    import sys
    root_dir = str(Path(__file__).parent.parent)
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)
    try:
        from skills_engine import get_applied_skills
        loop = asyncio.get_event_loop()
        skills = await loop.run_in_executor(None, get_applied_skills)

        skills_list = [
            {"name": s.name, "version": s.version, "applied_at": s.applied_at}
            for s in skills
        ]
        output = json.dumps(skills_list, ensure_ascii=False, indent=2)

        if request_id:
            result_dir = config.DATA_DIR / "ipc" / group_folder / "results"
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / f"{request_id}.json").write_text(
                json.dumps({
                    "requestId": request_id,
                    "output": output,
                    "skills": skills_list,
                }),
                encoding="utf-8",
            )
    except Exception as e:
        log.error(f"list_skills IPC error: {e}")


def _find_parent_container(group_folder: str) -> str | None:
    """
    找出目前正在為此群組執行的主 container 名稱（用於 subagent 親子關係追蹤）。
    回傳最早啟動的非 subagent container（parent_container is None）。
    """
    try:
        from .container_runner import _active_containers, _active_lock
        with _active_lock:
            candidates = [
                info for info in _active_containers.values()
                if info.get("folder") == group_folder and info.get("parent_container") is None
            ]
        if candidates:
            # 取最早啟動的（started_at 最小）
            return min(candidates, key=lambda x: x["started_at"])["name"]
    except Exception:
        pass
    return None


async def _run_subagent(
    request_id: str, prompt: str, context_mode: str, group_folder: str,
    parent_container: str | None = None,
) -> None:
    """
    在獨立 Docker container 中執行子 agent，並將結果寫入 results 目錄。
    父 agent 透過輪詢此目錄來取得子 agent 的輸出。
    parent_container 用於 dashboard 顯示親子關係。
    """
    from .container_runner import run_container_agent
    result_dir = config.DATA_DIR / "ipc" / group_folder / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    output_file = result_dir / f"{request_id}.json"
    try:
        groups = db.get_all_registered_groups()
        group = next((g for g in groups if g["folder"] == group_folder), None)
        if not group:
            result_text = f"Error: group {group_folder} not found"
        else:
            conv_history = [] if context_mode == "isolated" else None
            result = await run_container_agent(
                group=group,
                prompt=prompt,
                conversation_history=conv_history,
                parent_container=parent_container,
            )
            result_text = result.get("result") or result.get("error") or "(no output)"
        output_file.write_text(
            json.dumps({"requestId": request_id, "output": result_text}),
            encoding="utf-8"
        )
        log.info(f"Subagent {request_id} completed, result written to {output_file}")
    except Exception as e:
        log.error(f"Subagent {request_id} error: {e}")
        try:
            output_file.write_text(
                json.dumps({"requestId": request_id, "output": f"Subagent error: {e}"}),
                encoding="utf-8"
            )
        except Exception:
            pass


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
    while True:
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

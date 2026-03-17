"""IPC file watcher — processes agent-to-host messages"""
import asyncio
import importlib
import json
import logging
import os
import sys as _sys
import pathlib as _pathlib
import time
import uuid
from pathlib import Path
from typing import Callable, Awaitable

from . import config, db
from .group_folder import is_valid_group_folder
from .router import route_file
import asyncio as _asyncio

log = logging.getLogger(__name__)

# ── skills_engine loader: add repo root to sys.path once at module load ────────
_REPO_ROOT = _pathlib.Path(__file__).parent.parent
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

_skills_engine_mod = None

def _get_skills_engine():
    """Lazily load skills_engine via importlib.import_module (handles relative imports correctly)."""
    global _skills_engine_mod
    if _skills_engine_mod is None:
        _skills_engine_mod = importlib.import_module("skills_engine")
    return _skills_engine_mod

# Module-level lock to prevent concurrent skill installs/uninstalls
_skills_lock = asyncio.Lock()

# ── dev_task concurrency guard ─────────────────────────────────────────────────
_dev_task_active: set[str] = set()
_dev_task_lock = asyncio.Lock()


def _ipc_task_done_callback(task: asyncio.Task) -> None:
    """Log any unhandled exception from a fire-and-forget IPC asyncio Task (Issue #73).

    ensure_future() / create_task() calls in _handle_ipc() are fire-and-forget.
    Without a done-callback, exceptions raised outside the inner try/except blocks
    (e.g. CancelledError during shutdown, or an unexpected RuntimeError) are
    silently swallowed by the event loop.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Unhandled exception in IPC task %s: %s", task.get_name(), exc, exc_info=exc)


import re as _re

def _sanitize_error_for_notification(error: str) -> str:
    """Remove or relativize filesystem paths from error messages."""
    # Replace any absolute path starting with / or drive letter
    error = _re.sub(r'[A-Za-z]:[\\\/][^\s\'",:;()\[\]{}]*', '<path>', error)
    error = _re.sub(r'\/[^\s\'",:;()\[\]{}]{3,}', '<path>', error)
    # Cap length
    if len(error) > 500:
        error = error[:500] + '…'
    return error


def _notify_main_group_error(filename: str, error: str) -> None:
    """Send IPC error notification to main group by writing a new IPC message file.
    The error text is sanitized to remove internal filesystem paths before delivery."""
    try:
        groups = db.get_all_registered_groups()
        main = next((g for g in groups if g.get("is_main")), None)
        if not main:
            return
        ipc_dir = config.DATA_DIR / "ipc" / main["folder"] / "messages"
        if not ipc_dir.exists():
            return
        safe_error = _sanitize_error_for_notification(error)
        # Use only the base filename (no directory) to avoid leaking host paths
        safe_filename = _pathlib.Path(filename).name
        payload = {
            "type": "message",
            "chatJid": main["jid"],
            "text": f"IPC Error: failed to process `{safe_filename}`\n```\n{safe_error}\n```",
        }
        out = ipc_dir / f"ipc_error_alert_{int(time.time() * 1000)}.json"
        out.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.debug("_notify_main_group_error failed: %s", exc)


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
                content = f.read_text(encoding="utf-8")
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
                # Alert main group about the IPC processing failure
                try:
                    _notify_main_group_error(f.name, str(e))
                except Exception:
                    pass

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
    - "send_file"：將容器內的檔案傳送給用戶（透過 Telegram 等頻道的 send_document）
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
        # Fix #202: validate folder name to prevent path traversal via IPC
        if folder and not is_valid_group_folder(folder):
            raise ValueError(f"Invalid group folder name: {folder!r} — must be alphanumeric with hyphens/underscores only")
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
        flag.write_text("1", encoding="utf-8")
        log.info("Groups refresh requested via IPC")

    elif msg_type == "reset_group":
        # 重置指定群組的失敗計數器，解凍被 cooldown 鎖定的群組。
        # 僅限 monitor 群組（is_monitor=True）或主群組可呼叫，防止普通群組互相干擾。
        target_jid = payload.get("jid", "")
        if not target_jid:
            raise ValueError("reset_group requires 'jid' field")
        # Write a flag file — main.py's _message_loop reads it and resets counters
        # Using file flag avoids cross-task direct coupling (same pattern as refresh_groups)
        import json as _json
        flag = config.DATA_DIR / "reset_group.flag"
        flag.write_text(_json.dumps({"jid": target_jid, "ts": time.time()}), encoding="utf-8")
        log.info("reset_group requested via IPC for jid=%s by group=%s", target_jid, group_folder)

    elif msg_type == "apply_skill":
        # 安裝 Skill Plugin：只有主群組的 container 才能呼叫（避免一般群組隨意修改框架檔案）
        # skill_path 可以是本地路徑或 URL（由 skills_engine.apply_skill 處理）
        if not is_main:
            raise PermissionError("Only main group can apply skills")
        skill_path = payload.get("skill_path", "")
        request_id = payload.get("requestId", "")
        if skill_path:
            t = _asyncio.ensure_future(_run_apply_skill(skill_path, request_id, group_folder, route_fn))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "uninstall_skill":
        # 移除 Skill Plugin：只有主群組的 container 才能呼叫
        if not is_main:
            raise PermissionError("Only main group can uninstall skills")
        skill_name = payload.get("skill_name", "")
        request_id = payload.get("requestId", "")
        if skill_name:
            t = _asyncio.ensure_future(_run_uninstall_skill(skill_name, request_id, group_folder, route_fn))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "list_skills":
        # 列出已安裝的 Skills：任何群組都可以查詢（唯讀操作）
        # 結果寫入 results 目錄（供 container 輪詢讀取），同時如有 requestId 也可 route 回訊息
        request_id = payload.get("requestId", "")
        t = _asyncio.ensure_future(_run_list_skills(request_id, group_folder, route_fn))
        t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "spawn_agent":
        # 執行子 agent 並將結果寫回 results 目錄
        request_id = payload.get("requestId", "")
        prompt = payload.get("prompt", "")
        context_mode = payload.get("context_mode", "isolated")
        if request_id and prompt:
            # 找出目前正在執行此群組的父 container（用於 dashboard 親子關係顯示）
            parent_name = _find_parent_container(group_folder)
            t = _asyncio.ensure_future(_run_subagent(request_id, prompt, context_mode, group_folder, parent_name))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "dev_task":
        # 觸發 DevEngine 7 階段開發流程
        # agent 可以寫入此 IPC 訊息來啟動自動化開發任務
        dev_prompt = payload.get("prompt", "")
        mode = payload.get("mode", "auto")  # "auto" | "interactive"
        session_id = payload.get("session_id", "")  # 若提供則 resume，否則新建
        if dev_prompt or session_id:
            groups = db.get_all_registered_groups()
            _dev_group = next((g for g in groups if g["folder"] == group_folder), None)
            if _dev_group:
                _dev_group_jid = _dev_group["jid"]
            else:
                log.warning(
                    "DevEngine IPC: group folder '%s' not found in registered groups — "
                    "will attempt lookup by folder in _run_dev_task", group_folder
                )
                _dev_group_jid = group_folder  # pass folder as fallback, handled in _run_dev_task
            t = _asyncio.ensure_future(_run_dev_task(
                {"prompt": dev_prompt, "mode": mode, "session_id": session_id},
                _dev_group_jid,
                route_fn,
            ))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "send_file":
        container_path = payload.get("filePath", "")
        caption = payload.get("caption", "")
        # Resolve chatJid: use payload value, or fall back to group's registered JID
        _sf_jid = payload.get("chatJid", "")
        if not _sf_jid:
            _sf_groups = db.get_all_registered_groups()
            _sf_match = next((g for g in _sf_groups if g.get("folder") == group_folder), None)
            _sf_jid = _sf_match["jid"] if _sf_match else ""

        log.info("send_file IPC: container_path=%r group_folder=%r chat_jid=%r",
                 container_path, group_folder, _sf_jid)

        # Resolve container path to host path
        # Container sees /workspace/group/ → host sees {GROUPS_DIR}/{folder}/
        host_path = _resolve_container_path(container_path, group_folder)
        log.info("send_file IPC: resolved host_path=%r", host_path)

        if host_path and os.path.exists(host_path):
            log.info("send_file IPC: file exists, routing to channel")
            delete_after = payload.get("deleteAfterSend", False)

            async def _send_and_cleanup():
                await route_file(_sf_jid, host_path, caption)
                if delete_after:
                    try:
                        os.unlink(host_path)
                        log.info("send_file IPC: deleted temp file after send: %r", host_path)
                    except OSError as e:
                        log.warning("send_file IPC: failed to delete temp file %r: %s", host_path, e)

            t = _asyncio.ensure_future(_send_and_cleanup())
            t.add_done_callback(_ipc_task_done_callback)
        else:
            log.warning("send_file IPC: file NOT found at host_path=%r (container: %r)",
                        host_path, container_path)
            fname = os.path.basename(container_path) if container_path else "unknown"
            _asyncio.ensure_future(route_fn(
                _sf_jid,
                f"⚠️ 檔案無法傳送：找不到 {fname}\n路徑：{host_path}"
            ))

    elif msg_type == "memory_search":
        # 三層記憶系統：冷/暖記憶混合搜尋 — container 可透過 IPC 查詢歷史記憶
        # 結果寫入 results 目錄，container 透過輪詢讀取
        request_id = payload.get("requestId", "")
        query = payload.get("query", "")
        _sf_groups = db.get_all_registered_groups()
        _sf_match = next((g for g in _sf_groups if g.get("folder") == group_folder), None)
        _ms_jid = _sf_match["jid"] if _sf_match else ""
        if query and _ms_jid:
            t = _asyncio.ensure_future(_run_memory_search(query, request_id, _ms_jid, group_folder))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "start_remote_control":
        rc_jid = payload.get("jid", "")
        rc_sender = payload.get("sender", "")
        if not rc_jid:
            _rc_groups = db.get_all_registered_groups()
            _rc_match = next((g for g in _rc_groups if g.get("folder") == group_folder), None)
            rc_jid = _rc_match["jid"] if _rc_match else ""
        if rc_jid:
            t = _asyncio.ensure_future(_run_start_remote_control(rc_jid, rc_sender, route_fn))
            t.add_done_callback(_ipc_task_done_callback)
        else:
            log.warning("start_remote_control IPC: could not resolve JID for group %s", group_folder)

    elif msg_type == "self_update":
        _su_jid = payload.get("jid", "")
        if not _su_jid:
            _su_groups = db.get_all_registered_groups()
            _su_match = next((g for g in _su_groups if g.get("folder") == group_folder), None)
            _su_jid = _su_match["jid"] if _su_match else ""
        t = _asyncio.ensure_future(_run_self_update(_su_jid, route_fn))
        t.add_done_callback(_ipc_task_done_callback)

    else:
        # Unknown IPC message type — log a warning instead of silently ignoring.
        # This aids debugging when a stale container image sends an unrecognised type.
        log.warning(
            "Unknown IPC message type %r from group %s — payload keys: %s",
            msg_type,
            group_folder,
            list(payload.keys()),
        )

async def _run_dev_task(payload: dict, group_jid: str, route_fn) -> None:
    """
    在背景執行 DevEngine 7 階段開發流程。
    每個階段完成後透過 route_fn 發送進度通知給用戶。
    Uses _dev_task_lock to prevent concurrent dev_task invocations per group.
    """
    async with _dev_task_lock:
        if group_jid in _dev_task_active:
            await route_fn(group_jid, "⚙️ A dev task is already running for this group. Please wait.")
            return
        _dev_task_active.add(group_jid)
    try:
        from .dev_engine import DevEngine, load_session
        prompt = payload.get("prompt", "")
        mode = payload.get("mode", "auto")
        session_id = payload.get("session_id", "")
        groups = db.get_all_registered_groups()
        group = next((g for g in groups if g["jid"] == group_jid), None)
        if not group:
            # Check if group_jid is actually a folder (fallback from ipc handler)
            group = next((g for g in groups if g.get("folder") == group_jid), None)
            if not group:
                log.error(
                    "DevEngine IPC: cannot find group by jid or folder '%s'. "
                    "Group may not be registered yet.",
                    group_jid
                )
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
    finally:
        async with _dev_task_lock:
            _dev_task_active.discard(group_jid)


async def _run_apply_skill(
    skill_path: str, request_id: str, group_folder: str, route_fn: Callable,
) -> None:
    """
    在 thread executor 中執行 skills_engine.apply_skill()（同步函式）。
    完成後透過 route_fn 發送結果，並（若提供 requestId）寫入 results 目錄。
    Uses _skills_lock to prevent concurrent skill installs/uninstalls.
    """
    async with _skills_lock:
        try:
            apply_skill = _get_skills_engine().apply_skill
            groups = db.get_all_registered_groups()
            group = next((g for g in groups if g["folder"] == group_folder), None)
            jid = group["jid"] if group else ""

            # skills_engine.apply_skill 是同步函式，用 to_thread 避免阻塞 event loop
            # Fix #109: wrap with asyncio.wait_for to prevent indefinite hangs
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(apply_skill, skill_path),
                    timeout=300.0,
                )
            except asyncio.TimeoutError:
                log.error("Skill operation timed out after 300s")
                if jid:
                    await route_fn(jid, "⚠️ Skill operation timed out (>5 min). Try again.")
                return

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
    Uses _skills_lock to prevent concurrent skill installs/uninstalls.
    """
    async with _skills_lock:
        try:
            uninstall_skill = _get_skills_engine().uninstall_skill
            groups = db.get_all_registered_groups()
            group = next((g for g in groups if g["folder"] == group_folder), None)
            jid = group["jid"] if group else ""

            # Fix #109: wrap with asyncio.wait_for to prevent indefinite hangs
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(uninstall_skill, skill_name),
                    timeout=300.0,
                )
            except asyncio.TimeoutError:
                log.error("Skill operation timed out after 300s")
                if jid:
                    await route_fn(jid, "⚠️ Skill operation timed out (>5 min). Try again.")
                return

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
    try:
        get_applied_skills = _get_skills_engine().get_applied_skills
        skills = await asyncio.to_thread(get_applied_skills)

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


_RC_URL_RE = __import__('re').compile(r"https://claude\.ai/code\S+")
_rc_active_pid: "int | None" = None
_rc_active_url: "str | None" = None


def _rc_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _rc_state_file() -> Path:
    return config.DATA_DIR / "remote-control.json"


def _rc_save(pid: int, url: str, sender: str, jid: str) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _rc_state_file().write_text(
        json.dumps({"pid": pid, "url": url, "sender": sender, "jid": jid,
                    "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
        encoding="utf-8",
    )


def restore_remote_control() -> None:
    """Re-adopt a still-alive remote-control process from a previous run."""
    global _rc_active_pid, _rc_active_url
    try:
        data = json.loads(_rc_state_file().read_text(encoding="utf-8"))
        pid, url = data.get("pid"), data.get("url", "")
        if pid and _rc_is_alive(pid):
            _rc_active_pid, _rc_active_url = pid, url
            log.info("Restored remote-control session pid=%s url=%s", pid, url)
        else:
            _rc_state_file().unlink(missing_ok=True)
    except Exception:
        pass


async def _run_start_remote_control(jid: str, sender: str, route_fn: Callable) -> None:
    """Spawn `claude remote-control` in the EvoClaw directory, poll for the URL,
    and deliver it to the originating group. Mirrors nanoclaw's startRemoteControl()."""
    global _rc_active_pid, _rc_active_url

    if _rc_active_pid is not None:
        if _rc_is_alive(_rc_active_pid):
            log.info("remote_control: reusing existing session pid=%s", _rc_active_pid)
            await route_fn(jid, f"✅ Remote control 已啟動：{_rc_active_url}")
            return
        _rc_active_pid = _rc_active_url = None
        _rc_state_file().unlink(missing_ok=True)

    cwd = str(config.BASE_DIR)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    stdout_path = config.DATA_DIR / "remote-control.stdout"
    stderr_path = config.DATA_DIR / "remote-control.stderr"
    stdout_path.write_text("", encoding="utf-8")

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "remote-control", "--name", "EvoClaw Remote",
            stdin=asyncio.subprocess.PIPE,
            stdout=open(stdout_path, "w"),
            stderr=open(stderr_path, "w"),
            cwd=cwd,
            start_new_session=True,
        )
    except Exception as exc:
        log.error("remote_control: spawn failed: %s", exc)
        await route_fn(jid, f"❌ Remote control 啟動失敗：{exc}")
        return

    if proc.stdin:
        try:
            proc.stdin.write(b"y\n")
            await proc.stdin.drain()
            proc.stdin.close()
        except Exception:
            pass

    pid = proc.pid
    log.info("remote_control: spawned pid=%s cwd=%s", pid, cwd)

    deadline = time.monotonic() + 30.0
    while True:
        if not _rc_is_alive(pid):
            await route_fn(jid, "❌ Remote control 程序意外結束，請再試一次。")
            return
        try:
            content = stdout_path.read_text(encoding="utf-8")
        except Exception:
            content = ""
        m = _RC_URL_RE.search(content)
        if m:
            url = m.group(0)
            _rc_active_pid, _rc_active_url = pid, url
            _rc_save(pid, url, sender, jid)
            log.info("remote_control: URL ready: %s", url)
            await route_fn(jid, f"🖥️ Remote control 已就緒！\n點此連線：{url}")
            return
        if time.monotonic() >= deadline:
            try:
                os.kill(pid, 15)
            except Exception:
                pass
            await route_fn(jid, "⏱️ Remote control 等待 URL 逾時，請再試一次。")
            return
        await asyncio.sleep(0.2)


async def _run_self_update(jid: str, route_fn: Callable) -> None:
    """Run git pull + pip install on the host, then write self_update.flag so
    main._message_loop() can call os.execv() for an in-place restart."""
    cwd = str(config.BASE_DIR)
    try:
        if jid:
            await route_fn(jid, "🔄 EvoClaw 更新中：執行 git pull...")

        # ── git pull ──────────────────────────────────────────────────────────
        proc = await asyncio.create_subprocess_exec(
            "git", "pull",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            if jid:
                await route_fn(jid, "⏱️ git pull 逾時（>60s），取消更新。")
            log.error("self_update: git pull timed out")
            return

        git_output = stdout.decode("utf-8", errors="replace").strip()
        log.info("self_update: git pull rc=%d output: %s", proc.returncode, git_output[:200])

        if proc.returncode != 0:
            msg = f"❌ git pull 失敗 (exit {proc.returncode}):\n```\n{git_output[:500]}\n```"
            if jid:
                await route_fn(jid, msg)
            return

        # ── pip install -e . (optional, only if project has setup files) ──────
        _pip_marker = _pathlib.Path(cwd) / "pyproject.toml"
        _setup_marker = _pathlib.Path(cwd) / "setup.py"
        if _pip_marker.exists() or _setup_marker.exists():
            pip_proc = await asyncio.create_subprocess_exec(
                "pip", "install", "-e", ".", "--quiet",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
            try:
                pip_out, _ = await asyncio.wait_for(pip_proc.communicate(), timeout=120.0)
            except asyncio.TimeoutError:
                log.warning("self_update: pip install timed out — continuing with restart anyway")
            else:
                if pip_proc.returncode != 0:
                    pip_output = pip_out.decode("utf-8", errors="replace").strip()
                    log.warning("self_update: pip install non-zero exit: %s", pip_output[:300])

        # ── write flag for main loop to pick up and os.execv() ───────────────
        flag = config.DATA_DIR / "self_update.flag"
        flag.write_text(git_output[:1000], encoding="utf-8")
        log.info("self_update: flag written at %s — restart pending", flag)

        if jid:
            changed = "Already up to date." not in git_output
            if changed:
                status = f"✅ 更新完成！\n```\n{git_output[:300]}\n```\nEvoClaw 即將重啟..."
            else:
                status = "✅ 已是最新版本，EvoClaw 即將重啟..."
            await route_fn(jid, status)

    except Exception as exc:
        log.error("self_update: unexpected error: %s", exc)
        if jid:
            await route_fn(jid, f"❌ 更新失敗：{exc}")


async def _run_memory_search(
    query: str, request_id: str, jid: str, group_folder: str,
) -> None:
    """
    執行三層記憶混合搜尋（FTS5 關鍵字 + 時效性評分）。
    結果寫入 results 目錄供 container 輪詢讀取。
    """
    try:
        from .memory.search import memory_search
        results = memory_search(jid, query)
        output = json.dumps(results, ensure_ascii=False, indent=2)

        if request_id:
            result_dir = config.DATA_DIR / "ipc" / group_folder / "results"
            result_dir.mkdir(parents=True, exist_ok=True)
            (result_dir / f"{request_id}.json").write_text(
                json.dumps({
                    "requestId": request_id,
                    "output": output,
                    "results": results,
                }),
                encoding="utf-8",
            )
        log.info("memory_search IPC: query=%r found %d results for jid=%s", query, len(results), jid)
    except Exception as e:
        log.error("memory_search IPC error: %s", e)


def _find_parent_container(group_folder: str) -> str | None:
    """
    找出目前正在為此群組執行的主 container 名稱（用於 subagent 親子關係追蹤）。
    回傳最早啟動的非 subagent container（parent_container is None）。
    """
    try:
        from .container_runner import _active_containers
        # _active_containers is only mutated from the asyncio event loop thread;
        # a GIL-safe snapshot copy is sufficient for this read-only lookup.
        candidates = [
            info for info in list(_active_containers.values())
            if info.get("folder") == group_folder and info.get("parent_container") is None
        ]
        if candidates:
            # 取最早啟動的（started_at 最小）
            return min(candidates, key=lambda x: x["started_at"])["name"]
    except Exception:
        pass
    return None


_SUBAGENT_RESULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MB cap on subagent result files (Issue #57)


async def _run_subagent(
    request_id: str, prompt: str, context_mode: str, group_folder: str,
    parent_container: str | None = None,
) -> None:
    """
    在獨立 Docker container 中執行子 agent，並將結果寫入 results 目錄。
    父 agent 透過輪詢此目錄來取得子 agent 的輸出。
    parent_container 用於 dashboard 顯示親子關係。

    Result text is capped at _SUBAGENT_RESULT_MAX_BYTES to prevent a runaway
    subagent from filling the host disk via the IPC results directory (Issue #57).
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
        # Enforce size cap before writing to disk (Issue #57)
        _encoded = result_text.encode("utf-8", errors="replace")
        if len(_encoded) > _SUBAGENT_RESULT_MAX_BYTES:
            result_text = _encoded[:_SUBAGENT_RESULT_MAX_BYTES].decode("utf-8", errors="ignore")
            log.warning("Subagent result truncated from %d to %d bytes", len(_encoded), _SUBAGENT_RESULT_MAX_BYTES)
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


def _resolve_container_path(container_path: str, group_folder: str) -> str | None:
    """Convert a container-side file path to the equivalent host path.

    Container /workspace/group/   → host {GROUPS_DIR}/{folder}/
    Container /workspace/project/ → host {BASE_DIR}/
    Container /workspace/ipc/     → host {DATA_DIR}/ipc/{folder}/
    Container /workspace/global/  → host {GROUPS_DIR}/global/

    Uses pathlib.Path throughout for correct Windows backslash handling.
    """
    if not group_folder:
        log.warning("_resolve_container_path: empty group_folder for path %r", container_path)
        return None

    import pathlib

    # Normalize to forward slashes for prefix matching (container paths are always POSIX)
    p = container_path.replace("\\", "/").strip()

    groups_dir = pathlib.Path(config.GROUPS_DIR)
    base_dir = pathlib.Path(config.BASE_DIR)
    data_dir = pathlib.Path(config.DATA_DIR)

    host: pathlib.Path | None = None
    expected_root: pathlib.Path | None = None

    if p.startswith("/workspace/group/"):
        rel = p[len("/workspace/group/"):]
        host = groups_dir / group_folder / rel
        expected_root = groups_dir / group_folder
    elif p.startswith("/workspace/project/"):
        rel = p[len("/workspace/project/"):]
        host = base_dir / rel
        expected_root = base_dir
    elif p.startswith("/workspace/ipc/"):
        rel = p[len("/workspace/ipc/"):]
        host = data_dir / "ipc" / group_folder / rel
        expected_root = data_dir / "ipc" / group_folder
    elif p.startswith("/workspace/global/"):
        rel = p[len("/workspace/global/"):]
        host = groups_dir / "global" / rel
        expected_root = groups_dir / "global"
    else:
        # Unrecognized prefix — log and return None
        log.warning("_resolve_container_path: unrecognized path prefix in %r", container_path)
        return None

    # Guard against path traversal: resolved path must stay within the expected root.
    # Fix #201: use is_relative_to() instead of str.startswith() to prevent prefix bypass.
    # str.startswith("/data/groups/foo") would wrongly allow "/data/groups/foobar/evil".
    try:
        resolved = host.resolve()
        if expected_root and not resolved.is_relative_to(expected_root.resolve()):
            log.warning(
                "_resolve_container_path: path traversal attempt detected — "
                "container_path=%r resolved to %r which is outside %r",
                container_path, str(resolved), str(expected_root),
            )
            return None
    except Exception as exc:
        log.warning("_resolve_container_path: resolution error for %r: %s", container_path, exc)
        return None

    return str(host)


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

_RESULT_MAX_AGE_SECS = 3600  # 1 hour — stale result files older than this are removed
_result_cleanup_cycle = 0
_RESULT_CLEANUP_EVERY = 120  # run cleanup every ~120 IPC poll cycles (~60s at default 0.5s interval)


async def _cleanup_stale_results() -> None:
    """Fix #119: Remove subagent result files older than _RESULT_MAX_AGE_SECS.

    Result files in data/ipc/*/results/ accumulate when a container crashes before writing
    or when a parent agent is cancelled before reading. Without cleanup they fill the disk.
    """
    cutoff = time.time() - _RESULT_MAX_AGE_SECS
    removed = 0
    try:
        for result_file in config.DATA_DIR.glob("ipc/*/results/*.json"):
            try:
                if result_file.stat().st_mtime < cutoff:
                    result_file.unlink(missing_ok=True)
                    removed += 1
            except Exception:
                pass
        if removed:
            log.info("IPC result cleanup: removed %d stale result file(s)", removed)
    except Exception as exc:
        log.warning("IPC result cleanup error: %s", exc)


async def start_ipc_watcher(get_groups_fn: Callable, route_fn: Callable, stop_event: asyncio.Event) -> None:
    """
    IPC 監控主迴圈：每隔 IPC_POLL_INTERVAL 秒掃描所有群組的 IPC 目錄。

    之所以用輪詢（polling）而非 inotify/watchdog 等檔案系統事件，
    是為了保持跨平台相容性，並簡化 container volume mount 的互動邏輯。
    """
    global _result_cleanup_cycle
    restore_remote_control()  # Re-adopt any surviving remote-control session from previous run
    log.info("IPC watcher started")
    while True:
        try:
            groups = get_groups_fn()
            for group in groups:
                await process_ipc_dir(group["folder"], bool(group.get("is_main")), route_fn)
        except Exception as e:
            log.error(f"IPC watcher error: {e}")

        # Periodically purge stale subagent result files (Fix #119)
        _result_cleanup_cycle += 1
        if _result_cleanup_cycle % _RESULT_CLEANUP_EVERY == 0:
            await _cleanup_stale_results()

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.IPC_POLL_INTERVAL)
            break  # stop_event was set, exit loop
        except asyncio.TimeoutError:
            pass  # Normal cycle, continue

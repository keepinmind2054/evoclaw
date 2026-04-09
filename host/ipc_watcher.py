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

# Optional: inotify for Linux (pip install inotify-simple)
_INOTIFY_AVAILABLE = False
try:
    import inotify_simple as _inotify_simple
    _INOTIFY_AVAILABLE = True
except ImportError:
    pass

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
# p17c BUG-FIX (HIGH): asyncio.Lock() created at module import time is bound to
# the event loop that exists at import time.  In Python 3.10+ this is deprecated
# and in Python 3.12 may raise RuntimeError when used on a different loop (e.g.
# after asyncio.run() creates a fresh loop).  Lazily initialise via accessors so
# the Lock is always created on the running loop.
_skills_lock: asyncio.Lock | None = None
_dev_task_lock: asyncio.Lock | None = None

# ── dev_task concurrency guard ─────────────────────────────────────────────────
_dev_task_active: set[str] = set()


def _get_skills_lock() -> asyncio.Lock:
    """Return (and lazily create) the asyncio.Lock for skill install/uninstall."""
    global _skills_lock
    if _skills_lock is None:
        _skills_lock = asyncio.Lock()
    return _skills_lock


def _get_dev_task_lock() -> asyncio.Lock:
    """Return (and lazily create) the asyncio.Lock for dev_task concurrency."""
    global _dev_task_lock
    if _dev_task_lock is None:
        _dev_task_lock = asyncio.Lock()
    return _dev_task_lock


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
    The error text is sanitized to remove internal filesystem paths before delivery.

    Fix(p12a): previously used Path.write_text() which is non-atomic — the OS
    creates the file (triggering inotify CREATE) while the write is still in
    progress, so the host may read a partial JSON and log a parse error.  Now
    uses an atomic write: write to a .tmp sibling then os.rename() so the
    inotify MOVED_TO event fires only after the file is fully written.
    """
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
        # Atomic write: write to .tmp then rename so inotify MOVED_TO fires only
        # after the file is complete, preventing partial-JSON reads.
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.rename(out)
    except Exception as exc:
        log.debug("_notify_main_group_error failed: %s", exc)


def _write_ipc_response(path: str, data: dict) -> None:
    """Atomically write *data* as JSON to *path* (write to .tmp then rename).

    Used by memory_recall and memory_remember handlers to deliver results to
    the container-side polling loop without risking a partial-read race.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.rename(tmp, path)


# p28b: IPC backpressure — maximum number of JSON files to process per group per
# cycle.  Without a cap a runaway agent (e.g. a tight container loop writing IPC
# files faster than they are processed) can monopolise the event loop and prevent
# other groups from receiving messages.  Files beyond this limit are left on disk
# and processed in the next watcher cycle.  At default IPC_POLL_INTERVAL of 1 s
# this means a 500-file burst is drained in ~5 cycles (~5 s), which is acceptable.
# Set IPC_MAX_FILES_PER_CYCLE=0 in the environment to disable the cap (not recommended).
_IPC_MAX_FILES_PER_CYCLE: int = int(os.environ.get("IPC_MAX_FILES_PER_CYCLE", "100"))

# Warn operators when IPC files accumulate above this threshold across all groups.
_IPC_FLOOD_WARN_THRESHOLD: int = 500


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

    p28b: Per-cycle file limit (_IPC_MAX_FILES_PER_CYCLE) provides backpressure
    so a runaway container cannot monopolise the event loop.  Remaining files are
    processed in subsequent cycles.  A flood warning is emitted when the pending
    file count exceeds _IPC_FLOOD_WARN_THRESHOLD so operators are alerted early.
    """
    ipc_dir = config.DATA_DIR / "ipc" / group_folder
    msg_dir = ipc_dir / "messages"
    task_dir = ipc_dir / "tasks"

    for d in [msg_dir, task_dir]:
        if not d.exists():
            continue
        # sorted() 確保按檔名（時間戳記前綴）的 FIFO 順序處理。
        # NOTE (STABILITY_ANALYSIS 5.1): File ordering relies on wall-clock
        # timestamps embedded in filenames (e.g. ipc_error_alert_1700000000000.json).
        # On a single-machine deployment this is safe because all writers share the
        # same system clock.  In a multi-machine / NTP-adjusted environment, clock
        # skew could cause two near-simultaneous files from different hosts to sort
        # in the wrong order.  If multi-host IPC is ever needed, replace the
        # timestamp prefix with a monotonically-increasing sequence number (e.g.
        # using an atomic integer in shared memory or a database sequence) so that
        # ordering is guaranteed regardless of clock skew.
        # p28b: collect file list once (for count) then slice for backpressure.
        all_files = sorted(d.glob("*.json"))
        pending_count = len(all_files)
        if pending_count > _IPC_FLOOD_WARN_THRESHOLD:
            log.warning(
                "IPC backpressure: %d pending files in %s/%s (threshold=%d). "
                "A container may be writing files faster than they are processed. "
                "Processing up to %d files this cycle; remainder deferred.",
                pending_count, group_folder, d.name,
                _IPC_FLOOD_WARN_THRESHOLD,
                _IPC_MAX_FILES_PER_CYCLE if _IPC_MAX_FILES_PER_CYCLE > 0 else pending_count,
            )
        files_to_process = (
            all_files[:_IPC_MAX_FILES_PER_CYCLE]
            if _IPC_MAX_FILES_PER_CYCLE > 0
            else all_files
        )
        for f in files_to_process:
            try:
                # TOCTOU fix (p22d-B): the file can be deleted between glob()
                # and read_text() (e.g. a concurrent watcher process or manual
                # cleanup).  Catch FileNotFoundError explicitly and skip the
                # file rather than propagating to the generic handler which
                # would try to move a non-existent file to errors/ dir.
                try:
                    content = f.read_text(encoding="utf-8")
                except FileNotFoundError:
                    log.debug("IPC file vanished before read (race): %s", f.name)
                    continue
                # BUG-IPC-04 FIX (MEDIUM): skip empty files instead of treating
                # them as JSON parse errors.  An empty file is produced by an
                # aborted partial write or a zero-byte flush mid-write; it is
                # not a genuine IPC error and should not be moved to errors/.
                # Delete it silently — it will be re-written by the agent if
                # the operation is retried.
                if not content.strip():
                    log.debug("IPC file empty (partial write?), skipping: %s", f.name)
                    f.unlink(missing_ok=True)
                    continue
                try:
                    payload = json.loads(content)
                except json.JSONDecodeError as e:
                    log.error("IPC JSON parse error in %s: %s", f.name, e)
                    # Move to errors dir instead of deleting.
                    # BUG-IPC-05 FIX (MEDIUM): use a timestamped destination
                    # name to avoid FileExistsError on Windows (and silent
                    # overwrites on Linux) when a file with the same basename
                    # already exists in errors_dir from a previous failure.
                    errors_dir = f.parent.parent / "errors"
                    errors_dir.mkdir(exist_ok=True)
                    _err_dest = errors_dir / f"{f.stem}_{int(time.time() * 1000)}{f.suffix}"
                    try:
                        f.rename(_err_dest)
                    except Exception:
                        f.unlink(missing_ok=True)
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
        # Fix #442: validate that the target JID belongs to this group_folder.
        # A container should only be able to send messages to its own group's JID
        # to prevent cross-group message injection.
        if jid and not is_main:
            groups = db.get_all_registered_groups()
            own_group = next((g for g in groups if g.get("folder") == group_folder), None)
            if own_group and own_group.get("jid") != jid:
                log.warning(
                    "IPC cross-group message rejected: group_folder=%r tried to target jid=%r "
                    "(expected %r) — dropping",
                    group_folder, jid, own_group.get("jid"),
                )
                return
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
        # p16c BUG-FIX (MEDIUM): use atomic tmp+rename so the main loop never reads
        # an empty or half-written flag file if it polls exactly at write time.
        flag = config.DATA_DIR / "refresh_groups.flag"
        _flag_tmp = flag.with_suffix(".flag.tmp")
        _flag_tmp.write_text("1", encoding="utf-8")
        _flag_tmp.rename(flag)
        log.info("Groups refresh requested via IPC")

    elif msg_type == "reset_group":
        # 重置指定群組的失敗計數器，解凍被 cooldown 鎖定的群組。
        # 僅限主群組可呼叫，防止普通群組互相干擾或濫用重置功能。
        # p16c BUG-FIX (HIGH): the comment described a main/monitor restriction but
        # no code enforced it, allowing any group's container to reset the failure
        # counter of any other group — a privilege-escalation vector.
        if not is_main:
            raise PermissionError("Only main group can reset group failure counters")
        target_jid = payload.get("jid", "")
        if not target_jid:
            raise ValueError("reset_group requires 'jid' field")
        # Write a flag file — main.py's _message_loop reads it and resets counters
        # Using file flag avoids cross-task direct coupling (same pattern as refresh_groups)
        # p16c BUG-FIX (MEDIUM): use atomic tmp+rename so the main loop never reads
        # a partial JSON payload if it polls exactly at write time.
        flag = config.DATA_DIR / "reset_group.flag"
        _reset_tmp = flag.with_suffix(".flag.tmp")
        _reset_tmp.write_text(json.dumps({"jid": target_jid, "ts": time.time()}), encoding="utf-8")
        _reset_tmp.rename(flag)
        log.info("reset_group requested via IPC for jid=%s by group=%s", target_jid, group_folder)

    elif msg_type == "apply_skill":
        # 安裝 Skill Plugin：只有主群組的 container 才能呼叫（避免一般群組隨意修改框架檔案）
        # skill_path 可以是本地路徑或 URL（由 skills_engine.apply_skill 處理）
        if not is_main:
            raise PermissionError("Only main group can apply skills")
        skill_path = payload.get("skill_path", "")
        request_id = payload.get("requestId", "")
        if skill_path:
            t = _asyncio.create_task(_run_apply_skill(skill_path, request_id, group_folder, route_fn))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "uninstall_skill":
        # 移除 Skill Plugin：只有主群組的 container 才能呼叫
        if not is_main:
            raise PermissionError("Only main group can uninstall skills")
        skill_name = payload.get("skill_name", "")
        request_id = payload.get("requestId", "")
        if skill_name:
            t = _asyncio.create_task(_run_uninstall_skill(skill_name, request_id, group_folder, route_fn))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "list_skills":
        # 列出已安裝的 Skills：任何群組都可以查詢（唯讀操作）
        # 結果寫入 results 目錄（供 container 輪詢讀取），同時如有 requestId 也可 route 回訊息
        request_id = payload.get("requestId", "")
        t = _asyncio.create_task(_run_list_skills(request_id, group_folder, route_fn))
        t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "spawn_agent":
        # 執行子 agent 並將結果寫回 results 目錄
        request_id = payload.get("requestId", "")
        prompt = payload.get("prompt", "")
        context_mode = payload.get("context_mode", "isolated")
        if request_id and prompt:
            # 找出目前正在執行此群組的父 container（用於 dashboard 親子關係顯示）
            parent_name = _find_parent_container(group_folder)
            t = _asyncio.create_task(_run_subagent(request_id, prompt, context_mode, group_folder, parent_name))
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
            t = _asyncio.create_task(_run_dev_task(
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

            t = _asyncio.create_task(_send_and_cleanup())
            t.add_done_callback(_ipc_task_done_callback)
        else:
            log.warning("send_file IPC: file NOT found at host_path=%r (container: %r)",
                        host_path, container_path)
            fname = os.path.basename(container_path) if container_path else "unknown"
            # BUG-IPC-01 FIX (HIGH): add done-callback so unhandled exceptions
            # from this fire-and-forget task are logged rather than silently swallowed.
            _t = _asyncio.create_task(route_fn(
                _sf_jid,
                f"⚠️ 檔案無法傳送：找不到 {fname}\n路徑：{host_path}"
            ))
            _t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "memory_recall":
        # MemoryBus recall: container agents can query the unified memory store
        # via IPC and receive results by polling a response file.
        _mr_query = payload.get("query", "")
        _mr_k = payload.get("k", 5)
        _mr_namespace = payload.get("namespace", "")
        _mr_topic_tag = payload.get("topic_tag", "")
        _mr_response_file = payload.get("response_file", "")
        if _mr_query and _mr_response_file:
            _mr_groups = db.get_all_registered_groups()
            _mr_match = next((g for g in _mr_groups if g.get("folder") == group_folder), None)
            _mr_agent_id = _mr_match["jid"] if _mr_match else group_folder
            t = _asyncio.create_task(_run_memory_recall(
                _mr_query, _mr_k, _mr_namespace, _mr_topic_tag,
                _mr_agent_id, _mr_response_file, group_folder,
            ))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "memory_remember":
        # MemoryBus remember: container agents can store a memory via IPC
        # and receive an ack by polling a response file.
        _mm_content = payload.get("content", "")
        _mm_importance = payload.get("importance", 0.7)
        _mm_namespace = payload.get("namespace", "")
        _mm_topic_tag = payload.get("topic_tag", "")
        _mm_response_file = payload.get("response_file", "")
        if _mm_content and _mm_response_file:
            _mm_groups = db.get_all_registered_groups()
            _mm_match = next((g for g in _mm_groups if g.get("folder") == group_folder), None)
            _mm_agent_id = _mm_match["jid"] if _mm_match else group_folder
            t = _asyncio.create_task(_run_memory_remember(
                _mm_content, _mm_importance, _mm_namespace, _mm_topic_tag,
                _mm_agent_id, _mm_response_file, group_folder,
            ))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "memory_search":
        # 三層記憶系統：冷/暖記憶混合搜尋 — container 可透過 IPC 查詢歷史記憶
        # 結果寫入 results 目錄，container 透過輪詢讀取
        request_id = payload.get("requestId", "")
        query = payload.get("query", "")
        _sf_groups = db.get_all_registered_groups()
        _sf_match = next((g for g in _sf_groups if g.get("folder") == group_folder), None)
        _ms_jid = _sf_match["jid"] if _sf_match else ""
        if query and _ms_jid:
            t = _asyncio.create_task(_run_memory_search(query, request_id, _ms_jid, group_folder))
            t.add_done_callback(_ipc_task_done_callback)

    elif msg_type == "start_remote_control":
        rc_jid = payload.get("jid", "")
        rc_sender = payload.get("sender", "")
        if not rc_jid:
            _rc_groups = db.get_all_registered_groups()
            _rc_match = next((g for g in _rc_groups if g.get("folder") == group_folder), None)
            rc_jid = _rc_match["jid"] if _rc_match else ""
        if rc_jid:
            t = _asyncio.create_task(_run_start_remote_control(rc_jid, rc_sender, route_fn))
            t.add_done_callback(_ipc_task_done_callback)
        else:
            log.warning("start_remote_control IPC: could not resolve JID for group %s", group_folder)

    elif msg_type == "self_update":
        # Security: require an out-of-band confirmation token to prevent
        # prompt-injection attacks from triggering unattended code updates.
        confirm_token = payload.get("confirm_token", "")
        expected_token = os.environ.get("SELF_UPDATE_TOKEN", "")
        _su_jid = payload.get("jid", "")
        if not _su_jid:
            _su_groups = db.get_all_registered_groups()
            _su_match = next((g for g in _su_groups if g.get("folder") == group_folder), None)
            _su_jid = _su_match["jid"] if _su_match else ""
        if not expected_token:
            # If no token configured, self_update is disabled entirely
            log.warning(
                "self_update IPC: SELF_UPDATE_TOKEN not set — "
                "self_update is disabled. Set SELF_UPDATE_TOKEN in .env to enable."
            )
            if _su_jid:
                _asyncio.create_task(route_fn(_su_jid,
                    "❌ self_update 已停用。請在 .env 設定 SELF_UPDATE_TOKEN，再以 token 確認後執行。"
                ))
            return
        if confirm_token != expected_token:
            log.warning(
                "self_update IPC: invalid or missing confirm_token from group %s — rejected",
                group_folder,
            )
            return
        # Token valid — proceed with update
        t = _asyncio.create_task(_run_self_update(_su_jid, route_fn))
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
    # p17c BUG-FIX (HIGH): The original code acquired _dev_task_lock for the
    # membership check+add, released it, then re-acquired it in finally for
    # discard.  Between the two acquisitions any awaited operation (e.g.
    # route_fn below) could yield, and a second coroutine for the same group
    # could enter the lock, see _dev_task_active still populated (because we
    # haven't removed yet), and correctly reject itself — BUT if the first
    # coroutine crashes before reaching finally the entry is never removed,
    # permanently blocking future dev_tasks.  More subtly: if _dev_task_active
    # is mutated by concurrent coroutines between lock acquisitions the set is
    # no longer consistent.  Fix: keep the add AND the guard check inside the
    # same lock scope, and use a single lock reference from the lazy accessor.
    _dtl = _get_dev_task_lock()
    async with _dtl:
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
        async with _get_dev_task_lock():
            _dev_task_active.discard(group_jid)


async def _run_apply_skill(
    skill_path: str, request_id: str, group_folder: str, route_fn: Callable,
) -> None:
    """
    在 thread executor 中執行 skills_engine.apply_skill()（同步函式）。
    完成後透過 route_fn 發送結果，並（若提供 requestId）寫入 results 目錄。
    Uses _skills_lock to prevent concurrent skill installs/uninstalls.
    """
    async with _get_skills_lock():
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
                # p15b-fix: atomic write (tmp+rename) so the container polling
                # for this file never reads a partial JSON.
                _out = result_dir / f"{request_id}.json"
                _tmp = _out.with_suffix(".json.tmp")
                _tmp.write_text(
                    json.dumps({
                        "requestId": request_id,
                        "output": msg,
                        "success": result.success,
                        "skill": result.skill,
                        "version": result.version,
                    }),
                    encoding="utf-8",
                )
                _tmp.rename(_out)
        except Exception as e:
            log.error(f"apply_skill IPC error: {e}")


async def _run_uninstall_skill(
    skill_name: str, request_id: str, group_folder: str, route_fn: Callable,
) -> None:
    """
    在 thread executor 中執行 skills_engine.uninstall_skill()（同步函式）。
    Uses _skills_lock to prevent concurrent skill installs/uninstalls.
    """
    async with _get_skills_lock():
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
                # p15b-fix: atomic write
                _out = result_dir / f"{request_id}.json"
                _tmp = _out.with_suffix(".json.tmp")
                _tmp.write_text(
                    json.dumps({
                        "requestId": request_id,
                        "output": msg,
                        "success": result.success,
                    }),
                    encoding="utf-8",
                )
                _tmp.rename(_out)
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
            # p15b-fix: atomic write
            _out = result_dir / f"{request_id}.json"
            _tmp = _out.with_suffix(".json.tmp")
            _tmp.write_text(
                json.dumps({
                    "requestId": request_id,
                    "output": output,
                    "skills": skills_list,
                }),
                encoding="utf-8",
            )
            _tmp.rename(_out)
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
    # BUG-IPC-06 FIX (LOW): use atomic tmp+rename so restore_remote_control()
    # never reads a partial JSON if the process crashes mid-write.
    # Path.write_text() creates the file (triggering inotify CREATE) before the
    # full content is on disk, which can leave a truncated JSON on a crash.
    _dest = _rc_state_file()
    _tmp = _dest.with_suffix(".json.tmp")
    _tmp.write_text(
        json.dumps({"pid": pid, "url": url, "sender": sender, "jid": jid,
                    "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
        encoding="utf-8",
    )
    _tmp.rename(_dest)


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
    except Exception as exc:
        # BUG-19D-04 (LOW): was bare pass — log at debug so startup state
        # restore failures are visible without being noisy on fresh installs
        # (state file simply does not exist yet on first run).
        log.debug("restore_remote_control: could not restore state: %s", exc)


async def _run_start_remote_control(jid: str, sender: str, route_fn: Callable) -> None:
    """Spawn `claude remote-control` in the EvoClaw directory, poll for the URL,
    and deliver it to the originating group."""
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
    # p28a: write_text() is a blocking I/O syscall.  Run in an executor so the
    # event loop is not stalled if the filesystem is slow (NFS, overlayfs).
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: stdout_path.write_text("", encoding="utf-8")
    )

    # p17c BUG-FIX (MEDIUM): open() is a blocking syscall.  Calling it directly
    # inside an async function can block the event loop if the filesystem is slow
    # (NFS, overlayfs, full disk).  Open the file handles in an executor thread
    # before passing them to create_subprocess_exec().
    try:
        _loop = asyncio.get_running_loop()
        _stdout_fh, _stderr_fh = await _loop.run_in_executor(
            None,
            lambda: (open(stdout_path, "w"), open(stderr_path, "w")),
        )
    except Exception as exc:
        log.error("remote_control: failed to open log files: %s", exc)
        await route_fn(jid, f"❌ Remote control 啟動失敗：{exc}")
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "remote-control", "--name", "EvoClaw Remote",
            stdin=asyncio.subprocess.PIPE,
            stdout=_stdout_fh,
            stderr=_stderr_fh,
            cwd=cwd,
            start_new_session=True,
        )
    except Exception as exc:
        log.error("remote_control: spawn failed: %s", exc)
        await route_fn(jid, f"❌ Remote control 啟動失敗：{exc}")
        return
    finally:
        # File handles are now owned by the subprocess; close our references.
        try:
            _stdout_fh.close()
        except Exception:
            pass
        try:
            _stderr_fh.close()
        except Exception:
            pass

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
        # BUG-IPC-03 FIX (MEDIUM): read_text() is a blocking I/O syscall.
        # Calling it directly in an async function stalls the event loop for
        # the duration of the read on every 200ms poll iteration (up to 150
        # iterations over the 30s deadline).  Run in an executor so other
        # coroutines (IPC watcher, health monitor, message loop) are not blocked.
        try:
            _rc_loop = asyncio.get_running_loop()
            content = await _rc_loop.run_in_executor(
                None, lambda: stdout_path.read_text(encoding="utf-8")
            )
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
                # BUG-IPC-02 FIX (HIGH): kill the orphaned pip process so it
                # does not continue consuming CPU/network after the timeout.
                # Without this the pip subprocess runs indefinitely, holding
                # package-index connections and potentially corrupting a
                # partial install.
                try:
                    pip_proc.kill()
                except Exception:
                    pass
                log.warning("self_update: pip install timed out — continuing with restart anyway")
            else:
                if pip_proc.returncode != 0:
                    pip_output = pip_out.decode("utf-8", errors="replace").strip()
                    log.warning("self_update: pip install non-zero exit: %s", pip_output[:300])

        # ── write flag for main loop to pick up and os.execv() ───────────────
        flag = config.DATA_DIR / "self_update.flag"
        # p28a: write_text() is blocking I/O — run in executor to avoid
        # stalling the event loop on a slow or pressured filesystem.
        _flag_content = git_output[:1000]
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: flag.write_text(_flag_content, encoding="utf-8")
        )
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


async def _run_memory_recall(
    query: str,
    k: int,
    namespace: str,
    topic_tag: str,
    agent_id: str,
    response_file: str,
    group_folder: str,
) -> None:
    """Call MemoryBus.recall() and write JSON results to *response_file* atomically.

    The container-side tool polls *response_file* with a 50 ms x 200 loop
    (10 s total timeout).  An atomic write (tmp + rename) ensures the container
    never reads a partial JSON.
    """
    try:
        from .memory.memory_bus import MemoryBus
        _db_conn = db.get_db()
        bus = MemoryBus(_db_conn, config.GROUPS_DIR)
        memories = await bus.recall(
            query=query,
            agent_id=agent_id,
            k=k,
            project=namespace or "",
        )
        result_list = [
            {
                "memory_id": m.memory_id,
                "content": m.content,
                "score": m.score,
                "source": m.source,
                "scope": m.scope,
                "created_at": m.created_at,
            }
            for m in memories
        ]
        _write_ipc_response(response_file, {"ok": True, "memories": result_list})
        log.info(
            "memory_recall IPC: query=%r found %d memories for agent=%s",
            query, len(result_list), agent_id,
        )
    except Exception as exc:
        log.error("memory_recall IPC error: %s", exc)
        try:
            _write_ipc_response(response_file, {"ok": False, "error": str(exc)})
        except Exception:
            pass


async def _run_memory_remember(
    content: str,
    importance: float,
    namespace: str,
    topic_tag: str,
    agent_id: str,
    response_file: str,
    group_folder: str,
) -> None:
    """Call MemoryBus.remember() and write an ack JSON to *response_file* atomically."""
    try:
        from .memory.memory_bus import MemoryBus
        _db_conn = db.get_db()
        bus = MemoryBus(_db_conn, config.GROUPS_DIR)
        memory_id = await bus.remember(
            content=content,
            agent_id=agent_id,
            scope="shared",
            project=namespace or "",
            importance=float(importance),
        )
        _write_ipc_response(response_file, {"ok": True, "memory_id": memory_id})
        log.info(
            "memory_remember IPC: stored memory_id=%s for agent=%s importance=%.2f",
            memory_id, agent_id, importance,
        )
    except Exception as exc:
        log.error("memory_remember IPC error: %s", exc)
        try:
            _write_ipc_response(response_file, {"ok": False, "error": str(exc)})
        except Exception:
            pass


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
            # p15b-fix: atomic write
            _out = result_dir / f"{request_id}.json"
            _tmp = _out.with_suffix(".json.tmp")
            _tmp.write_text(
                json.dumps({
                    "requestId": request_id,
                    "output": output,
                    "results": results,
                }),
                encoding="utf-8",
            )
            _tmp.rename(_out)
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
    except Exception as exc:
        # BUG-19D-04 (LOW): was bare pass — log at debug so lookup errors
        # are traceable without being noisy in normal operation.
        log.debug("_find_parent_container(%r) failed: %s", group_folder, exc)
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
        # p15b-fix: atomic write (tmp+rename) so the parent container polling
        # for this file never reads a partial JSON midway through the write.
        _out_tmp = output_file.with_suffix(".json.tmp")
        _out_tmp.write_text(
            json.dumps({"requestId": request_id, "output": result_text}),
            encoding="utf-8",
        )
        _out_tmp.rename(output_file)
        log.info(f"Subagent {request_id} completed, result written to {output_file}")
    except Exception as e:
        log.error(f"Subagent {request_id} error: {e}")
        try:
            # p15b-fix: atomic write for error result too
            _err_tmp = output_file.with_suffix(".json.tmp")
            _err_tmp.write_text(
                json.dumps({"requestId": request_id, "output": f"Subagent error: {e}"}),
                encoding="utf-8",
            )
            _err_tmp.rename(output_file)
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

    # Normalize to forward slashes for prefix matching (container paths are always POSIX)
    p = container_path.replace("\\", "/").strip()

    groups_dir = _pathlib.Path(config.GROUPS_DIR)
    base_dir = _pathlib.Path(config.BASE_DIR)
    data_dir = _pathlib.Path(config.DATA_DIR)

    host: _pathlib.Path | None = None
    expected_root: _pathlib.Path | None = None

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
        except Exception as exc:
            # BUG-19D-05 (LOW): silent parse failures make it impossible to
            # diagnose why a scheduled task never fires.  Log at warning so
            # operators can spot misconfigured schedule_value strings.
            log.warning("_compute_next_run: invalid 'once' value %r: %s", schedule_value, exc)
            return None
    elif schedule_type == "interval":
        try:
            # schedule_value 單位是毫秒，直接加到現在時間上
            return now_ms + int(schedule_value)
        except Exception as exc:
            log.warning("_compute_next_run: invalid 'interval' value %r: %s", schedule_value, exc)
            return None
    elif schedule_type == "cron":
        try:
            from croniter import croniter
            # croniter(cron_expr, start_time) 計算 start_time 之後的下次執行時間
            c = croniter(schedule_value, time.time())
            return int(c.get_next() * 1000)
        except Exception as exc:
            log.warning("_compute_next_run: invalid 'cron' expression %r: %s", schedule_value, exc)
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


async def _start_ipc_watcher_inotify(get_groups_fn: Callable, route_fn: Callable, stop_event: asyncio.Event) -> None:
    """
    Linux inotify-based IPC watcher.
    Reacts to file CREATE events instead of polling — latency <20ms vs ~500ms.
    Falls back to polling if inotify setup fails.
    """
    inotify = _inotify_simple.INotify()
    watch_map: dict = {}  # wd → (group_folder, is_main)

    def _refresh_watches():
        """Update watches when groups change."""
        groups = get_groups_fn()
        watched_dirs = set()
        for group in groups:
            folder = group.get("folder", "")
            is_main = bool(group.get("is_main"))
            for sub in ("messages", "tasks"):
                ipc_dir = config.DATA_DIR / "ipc" / folder / sub
                ipc_dir.mkdir(parents=True, exist_ok=True)
                dir_str = str(ipc_dir)
                if dir_str not in watched_dirs:
                    try:
                        wd = inotify.add_watch(
                            dir_str,
                            _inotify_simple.flags.CREATE | _inotify_simple.flags.MOVED_TO
                        )
                        watch_map[wd] = (folder, is_main)
                        watched_dirs.add(dir_str)
                    except OSError as _we:
                        # Check if this is an inotify watch limit error (errno 28 = ENOSPC)
                        if hasattr(_we, 'errno') and _we.errno == 28:
                            log.warning(
                                "inotify watch limit exceeded — falling back to polling. "
                                "Fix: sudo sysctl fs.inotify.max_user_watches=65536 "
                                "or add to /etc/sysctl.conf: fs.inotify.max_user_watches=65536"
                            )
                            # Clean up any partially-initialized inotify watches before failing
                            try:
                                inotify.close()
                            except Exception:
                                pass
                            raise
                        log.warning("inotify: cannot watch %s: %s", dir_str, _we)
                    except Exception as _we:
                        log.warning("inotify: cannot watch %s: %s", dir_str, _we)

    _refresh_watches()
    log.info("IPC watcher (inotify): watching %d directories", len(watch_map))

    # p17c BUG-FIX (MEDIUM): asyncio.get_event_loop() is deprecated in Python
    # 3.10+ when called from a coroutine — use asyncio.get_running_loop() which
    # always returns the loop the current coroutine is executing on.
    _loop = asyncio.get_running_loop()
    _last_refresh = _loop.time()
    _REFRESH_INTERVAL = 30.0  # Re-scan groups every 30s
    # Fix(p12a): the inotify path never called _cleanup_stale_results(), so on
    # Linux (the primary deployment) subagent result files accumulated forever.
    # Mirror the polling backend: run cleanup roughly every 60 s.
    _last_result_cleanup = _loop.time()
    _RESULT_CLEANUP_INTERVAL = 60.0  # seconds between stale-result sweeps

    while not stop_event.is_set():
        try:
            # Non-blocking read with 1s timeout (also serves as keepalive)
            loop = asyncio.get_running_loop()
            events = await loop.run_in_executor(
                None,
                lambda: inotify.read(timeout=1000)  # 1000ms timeout
            )

            # Refresh watches periodically (new groups may have been added)
            now = loop.time()
            if now - _last_refresh > _REFRESH_INTERVAL:
                _refresh_watches()
                _last_refresh = now

            # Periodically purge stale subagent result files
            if now - _last_result_cleanup > _RESULT_CLEANUP_INTERVAL:
                try:
                    await _cleanup_stale_results()
                except Exception as _cse:
                    log.debug("inotify: stale result cleanup error: %s", _cse)
                _last_result_cleanup = now

            # Process events
            triggered_folders: set = set()
            for event in events:
                if event.wd in watch_map and event.name.endswith(".json"):
                    folder, is_main = watch_map[event.wd]
                    triggered_folders.add((folder, is_main))

            # Process IPC for triggered folders
            for folder, is_main in triggered_folders:
                try:
                    await process_ipc_dir(folder, is_main, route_fn)
                except Exception as _e:
                    log.error("inotify: process_ipc_dir error for %s: %s", folder, _e)

        except asyncio.CancelledError:
            break
        except Exception as _err:
            log.error("inotify: read error: %s", _err)
            await asyncio.sleep(0.5)

    # Cleanup
    try:
        inotify.close()
    except Exception:
        pass
    log.info("IPC watcher (inotify) stopped")


async def start_ipc_watcher(get_groups_fn: Callable, route_fn: Callable, stop_event: asyncio.Event) -> None:
    """
    IPC 監控主迴圈：自動選擇 inotify（Linux）或 polling（其他平台）後端。

    在 Linux 且已安裝 inotify-simple 時使用 inotify backend，訊息延遲 <20ms；
    其他平台或 inotify 初始化失敗時 fallback 到 polling（IPC_POLL_INTERVAL 秒間隔）。
    完全向後相容。
    """
    global _result_cleanup_cycle
    # p28a: restore_remote_control() does blocking read_text() on the state file.
    # Run it in an executor so the event loop is not stalled at startup on a slow
    # filesystem (NFS, overlayfs with disk pressure).
    await asyncio.get_running_loop().run_in_executor(None, restore_remote_control)
    log.info("IPC watcher started")

    # Try inotify on Linux
    if _INOTIFY_AVAILABLE and _sys.platform.startswith("linux"):
        log.info("IPC watcher: attempting inotify backend (Linux)")
        try:
            await _start_ipc_watcher_inotify(get_groups_fn, route_fn, stop_event)
            return  # inotify ran successfully
        except Exception as _ino_err:
            log.warning("IPC watcher: inotify failed (%s), falling back to polling", _ino_err)

    # Fallback: polling (all platforms)
    log.info("IPC watcher: using polling backend (interval=%.1fs)", config.IPC_POLL_INTERVAL)
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

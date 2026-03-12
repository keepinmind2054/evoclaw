"""Scheduled task execution engine"""
import asyncio
import logging
import time
from typing import Callable

from . import config, db

log = logging.getLogger(__name__)

def compute_next_run(schedule_type: str, schedule_value: str, last_run: int | None = None) -> int | None:
    """
    計算任務的「下次」執行時間（Unix timestamp，毫秒單位）。

    此函式在任務「執行完畢後」呼叫，與 ipc_watcher._compute_next_run
    的差異在於：這裡可以利用 last_run 作為 interval 的基準點，
    避免因執行時間過長而導致週期「漂移」。

    三種排程類型：
    - "once"：單次執行，結束後回傳 None（讓 scheduler 不再排入佇列）
    - "interval"：固定間隔，以 last_run 為基準點計算下次時間
      （而非以「現在」為基準，避免執行時間造成漂移）
    - "cron"：使用 croniter 解析標準 cron 表達式，計算下一個符合的時間點
      croniter 支援時區（ZoneInfo），確保日光節約時間等邊際情況正確處理
    """
    now = time.time()
    if schedule_type == "once":
        return None  # 單次任務不重複執行
    elif schedule_type == "interval":
        try:
            ms = int(schedule_value)
            # 以 last_run 為起點計算下次（避免漂移），若無 last_run 則從現在算起
            base = (last_run / 1000) if last_run else now
            return int((base + ms / 1000) * 1000)
        except Exception:
            return None
    elif schedule_type == "cron":
        try:
            from croniter import croniter
            import zoneinfo
            # 帶入設定的時區，確保 cron 表達式依本地時間解讀（例如「每天早上9點」）
            tz = zoneinfo.ZoneInfo(config.TIMEZONE)
            c = croniter(schedule_value, now)
            return int(c.get_next() * 1000)
        except Exception as e:
            log.warning(f"Invalid cron: {schedule_value}: {e}")
            return None
    return None

async def run_task(task: dict, get_group_fn: Callable, run_agent_fn: Callable) -> None:
    """
    執行單一排程任務：
    1. 找到對應的群組設定（若群組已被刪除則放棄）
    2. 呼叫 run_agent_fn 在 container 中執行任務
    3. 將執行結果記錄到 task_run_logs
    4. 計算並更新下次執行時間（next_run）

    context_mode == "isolated"：不帶入對話歷史（session_id=None），
    適用於不依賴群組對話脈絡的獨立排程工作（如每日報告）。
    """
    task_id = task["id"]
    group_folder = task["group_folder"]
    jid = task["chat_jid"]
    start = int(time.time() * 1000)

    group = get_group_fn(jid)
    if group is None:
        log.warning("task_scheduler: group %s not found for task %s — applying backoff", jid, task_id)
        # Backoff: delay 1 hour before next retry (task may have been orphaned)
        backoff_next = int(time.time()) + 3600
        db.update_task(task_id, next_run=backoff_next)
        return

    log.info(f"Running task {task_id} for {group_folder}")

    # next_run_ts is computed in the success or except branch, then applied in finally.
    # This ensures next_run is ALWAYS advanced regardless of success or failure (Fix #106).
    next_run_ts = None
    try:
        result = await run_agent_fn(
            group=group,
            prompt=task["prompt"],
            is_scheduled_task=True,
            # isolated 模式不傳 session_id，讓 agent 以全新狀態執行
            session_id=None if task.get("context_mode") == "isolated" else db.get_session(group_folder),
        )
        duration = int(time.time() * 1000) - start
        status = result.get("status", "error")
        # 記錄本次執行結果（供監控與除錯用）
        db.log_task_run(task_id, start, duration, status, result.get("result"), result.get("error"))
        # 更新任務狀態：記錄最後執行時間、結果摘要，並計算下次執行時間
        next_run_ts = compute_next_run(task["schedule_type"], task["schedule_value"], start)
        db.update_task(task_id,
                       last_run=start,
                       last_result=result.get("result", "")[:500],  # 只存前 500 字，節省空間
                       next_run=next_run_ts)
    except Exception as e:
        log.error(f"Task {task_id} failed: {e}")
        # 失敗時記錄 log 並推進 next_run，防止任務因 next_run 未更新而在每次
        # scheduler 輪詢時立即重試，形成緊密的無限重試迴圈（Issue #54）。
        db.log_task_run(task_id, start, 0, "error", None, str(e))
        # Compute a backoff next_run so the task retries after a normal cycle,
        # not immediately on every scheduler poll.
        next_run_ts = compute_next_run(task["schedule_type"], task["schedule_value"], start)
    finally:
        # Always advance next_run so the task is never stuck at a past timestamp
        # even if an unexpected exception escapes both the try and except blocks.
        db.update_task(task_id, next_run=next_run_ts)

async def start_scheduler_loop(
    get_group_fn: Callable,
    run_agent_fn: Callable,
    stop_event: asyncio.Event,
    group_queue=None,   # Optional GroupQueue for per-group serialization
) -> None:
    """
    排程器主迴圈：每隔 SCHEDULER_POLL_INTERVAL 秒檢查是否有到期的任務。

    若提供 group_queue，透過 GroupQueue.enqueue_task() 排程，確保每個群組
    同時只有一個 container 在執行（與訊息處理共用同一個序列化佇列）。
    若未提供（向後相容），退回到直接 create_task 的舊行為。
    """
    log.info("Task scheduler started")
    while True:
        try:
            now_ms = int(time.time() * 1000)
            # 查詢所有 next_run <= now_ms 且狀態為 active 的任務
            due = db.get_due_tasks(now_ms)
            for task in due:
                if group_queue is not None:
                    # Enqueue through GroupQueue for per-group serialization.
                    # Use chat_jid as the queue key — this is the canonical group
                    # identifier used by enqueue_message_check() so tasks and
                    # message processing share the same serialization slot (Issue #48).
                    jid = task.get("chat_jid", "")
                    task_id = task["id"]
                    if not jid:
                        log.warning("Task %s has empty chat_jid — skipping enqueue", task_id)
                        continue
                    group_queue.enqueue_task(
                        jid,
                        task_id,
                        lambda t=task: run_task(t, get_group_fn, run_agent_fn),
                    )
                else:
                    # Fallback: direct dispatch (backward compat)
                    asyncio.create_task(run_task(task, get_group_fn, run_agent_fn))
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.SCHEDULER_POLL_INTERVAL)
            break  # shutdown
        except asyncio.TimeoutError:
            pass  # normal poll cycle

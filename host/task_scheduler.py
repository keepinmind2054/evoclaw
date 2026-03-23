"""Scheduled task execution engine"""
import asyncio
import logging
import time
from typing import Callable

from . import config, db

log = logging.getLogger(__name__)

# BUG-TS-4 FIX: Maximum number of consecutive errors a task may produce before
# it is automatically paused to prevent an infinite error loop.  Each failure
# advances next_run by one normal cycle (existing behaviour) but once this
# threshold is reached the task is paused so operators can investigate.
_MAX_TASK_FAILURES = int(
    getattr(config, "TASK_MAX_FAILURES", 5)
    if hasattr(config, "TASK_MAX_FAILURES")
    else 5
)

# BUG-TS-3 FIX: Maximum time (seconds) a single task execution may take.
# Without this a hung container holds a scheduler slot indefinitely and
# later polls re-dispatch the same task creating concurrent duplicate runs.
_TASK_TIMEOUT_SECS = int(
    getattr(config, "TASK_TIMEOUT_SECS", 3600)
    if hasattr(config, "TASK_TIMEOUT_SECS")
    else 3600
)


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
            if ms <= 0:
                log.warning("interval schedule_value must be positive, got %s", schedule_value)
                return None
            # 以 last_run 為起點計算下次（避免漂移），若無 last_run 則從現在算起
            base = (last_run / 1000) if last_run else now
            next_ts = int((base + ms / 1000) * 1000)
            # BUG-TS-5 FIX: If next_run is still in the past (e.g. bot was
            # down for longer than one interval), advance to the next future
            # occurrence rather than returning a stale past timestamp that
            # would cause the scheduler to fire immediately on every poll
            # until it catches up.
            now_ms = int(now * 1000)
            while next_ts <= now_ms:
                next_ts += ms
            return next_ts
        except Exception:
            return None
    elif schedule_type == "cron":
        try:
            from croniter import croniter
            from datetime import datetime
            import pytz
            # 帶入設定的時區，確保 cron 表達式依本地時間解讀（例如「每天早上9點」）
            timezone_str = getattr(config, 'TIMEZONE', None)
            tz = pytz.timezone(timezone_str) if timezone_str else pytz.UTC
            # Use timezone-aware start time so croniter resolves cron in local timezone
            start_time = datetime.fromtimestamp(now, tz=tz)
            c = croniter(schedule_value, start_time, hash_use_datetime=True)
            next_run = c.get_next(datetime)
            if tz != pytz.UTC:
                next_run = tz.localize(next_run.replace(tzinfo=None)) if next_run.tzinfo is None else next_run.astimezone(tz)
            return int(next_run.timestamp() * 1000)
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
        backoff_next = int(time.time() * 1000) + 3600 * 1000  # BUG-TS-6 FIX: was seconds not ms
        db.update_task(task_id, next_run=backoff_next)
        return

    log.info(f"Running task {task_id} for {group_folder}")

    # BUG-TS-1 FIX: Atomically mark the task as "running" before executing it.
    # Without this, if the scheduler polls again before the task finishes (or if
    # the process crashes mid-execution) the same task can be dispatched a second
    # time — violating at-most-once semantics.  We update the status to "running"
    # here; on completion (success or failure) it is returned to "active" with an
    # advanced next_run.  On a hard crash, tasks left in "running" state can be
    # detected and recovered by the startup recovery path.
    try:
        db.update_task(task_id, status="running")
    except Exception as mark_exc:
        log.error("task_scheduler: could not mark task %s as running: %s — aborting dispatch", task_id, mark_exc)
        return

    # next_run_ts is computed in the success or except branch, then applied in finally.
    # This ensures next_run is ALWAYS advanced regardless of success or failure (Fix #106).
    next_run_ts = None
    failed = False
    try:
        # BUG-TS-3 FIX: Wrap the agent call in a timeout so a hung container
        # cannot hold the task in "running" state indefinitely and trigger
        # duplicate dispatches on subsequent scheduler polls.
        result = await asyncio.wait_for(
            run_agent_fn(
                group=group,
                prompt=task["prompt"],
                is_scheduled_task=True,
                # isolated 模式不傳 session_id，讓 agent 以全新狀態執行
                session_id=None if task.get("context_mode") == "isolated" else db.get_session(group_folder),
            ),
            timeout=_TASK_TIMEOUT_SECS,
        )
        duration = int(time.time() * 1000) - start
        status = result.get("status", "error")
        # 記錄本次執行結果（供監控與除錯用）
        db.log_task_run(task_id, start, duration, status, result.get("result"), result.get("error"))
        # For interval tasks, use the scheduled run time (task["next_run"]) as the base for
        # computing the next interval — prevents cumulative drift caused by long execution times.
        # For cron/once tasks, use start time as before.
        scheduled_time = task.get("next_run") or start
        last_run_base = scheduled_time if task.get("schedule_type") == "interval" else start
        # 更新任務狀態：記錄最後執行時間、結果摘要，並計算下次執行時間
        next_run_ts = compute_next_run(task["schedule_type"], task["schedule_value"], last_run_base)
        db.update_task(task_id,
                       last_run=start,
                       last_result=result.get("result", "")[:500],  # 只存前 500 字，節省空間
                       next_run=next_run_ts)
    except asyncio.TimeoutError:
        # BUG-TS-3 FIX: Task timed out.  Treat as a failure so next_run is
        # advanced and we do not get stuck re-dispatching the same task.
        log.error("Task %s timed out after %ds", task_id, _TASK_TIMEOUT_SECS)
        failed = True
        db.log_task_run(task_id, start, int(time.time() * 1000) - start, "timeout", None,
                        f"Task timed out after {_TASK_TIMEOUT_SECS}s")
        _err_scheduled_time = task.get("next_run") or start
        _err_last_run_base = _err_scheduled_time if task.get("schedule_type") == "interval" else start
        next_run_ts = compute_next_run(task["schedule_type"], task["schedule_value"], _err_last_run_base)
    except Exception as e:
        log.error(f"Task {task_id} failed: {e}")
        failed = True
        # 失敗時記錄 log 並推進 next_run，防止任務因 next_run 未更新而在每次
        # scheduler 輪詢時立即重試，形成緊密的無限重試迴圈（Issue #54）。
        db.log_task_run(task_id, start, 0, "error", None, str(e))
        # Compute a backoff next_run so the task retries after a normal cycle,
        # not immediately on every scheduler poll.
        # For interval tasks, use the scheduled run time to avoid drift on error paths too.
        _err_scheduled_time = task.get("next_run") or start
        _err_last_run_base = _err_scheduled_time if task.get("schedule_type") == "interval" else start
        next_run_ts = compute_next_run(task["schedule_type"], task["schedule_value"], _err_last_run_base)
    finally:
        # Fix #122: if next_run_ts is None (invalid schedule expression), mark the task
        # as "paused" so it doesn't linger silently with next_run=NULL.  Users can
        # repair the schedule expression and manually resume the task.
        if next_run_ts is None and task.get("schedule_type") != "once":
            log.error(
                "Task %s has invalid %s schedule %r — marking as paused",
                task_id, task.get("schedule_type"), task.get("schedule_value"),
            )
            db.update_task(
                task_id,
                status="paused",
                next_run=None,
                last_result="Invalid schedule expression — task paused. Repair the schedule to resume.",
            )
        elif failed:
            # BUG-TS-4 FIX: Track consecutive failures using the run log and
            # pause the task after _MAX_TASK_FAILURES to prevent infinite retry
            # loops.  We count recent "error"/"timeout" runs in the log table
            # to avoid adding a dedicated column to scheduled_tasks.
            try:
                failure_count = _count_recent_failures(task_id)
            except Exception:
                failure_count = 0
            if failure_count >= _MAX_TASK_FAILURES:
                log.error(
                    "Task %s has failed %d times consecutively — pausing to prevent infinite retry loop",
                    task_id, failure_count,
                )
                db.update_task(
                    task_id,
                    status="paused",
                    next_run=next_run_ts,
                    last_result=f"Auto-paused after {failure_count} consecutive failures.",
                )
            else:
                # Return to active with the advanced next_run
                db.update_task(task_id, status="active", next_run=next_run_ts)
        else:
            # Success path: return to active and advance next_run.
            # Always advance next_run so the task is never stuck at a past timestamp
            # even if an unexpected exception escapes both the try and except blocks.
            db.update_task(task_id, status="active", next_run=next_run_ts)


def _count_recent_failures(task_id: str) -> int:
    """Count consecutive failure/timeout runs for a task (most recent first).

    Returns the length of the trailing run of non-success statuses.
    """
    try:
        conn = db.get_db()
        rows = conn.execute(
            """SELECT status FROM task_run_logs
               WHERE task_id = ?
               ORDER BY run_at DESC
               LIMIT ?""",
            (task_id, _MAX_TASK_FAILURES),
        ).fetchall()
        count = 0
        for row in rows:
            status = row[0] if isinstance(row, (tuple, list)) else row["status"]
            if status in ("error", "timeout"):
                count += 1
            else:
                break
        return count
    except Exception:
        return 0


async def recover_stale_running_tasks() -> int:
    """Reset tasks stuck in 'running' state from a previous crashed process.

    BUG-TS-1 FIX (recovery): On startup, any task left in status='running'
    from a previous process that crashed mid-execution is reset to 'active'
    with next_run advanced by one normal interval so it will be retried on
    the next scheduler poll rather than being silently skipped forever.

    Returns the number of tasks recovered.
    """
    try:
        conn = db.get_db()
        rows = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE status='running'"
        ).fetchall()
        recovered = 0
        for row in rows:
            task = dict(row)
            task_id = task["id"]
            log.warning(
                "task_scheduler: task %s was stuck in 'running' state (process crash?) — recovering",
                task_id,
            )
            next_run_ts = compute_next_run(
                task.get("schedule_type", "once"),
                task.get("schedule_value", ""),
                task.get("next_run"),
            )
            db.update_task(task_id, status="active", next_run=next_run_ts)
            recovered += 1
        if recovered:
            log.info("task_scheduler: recovered %d stale 'running' task(s)", recovered)
        return recovered
    except Exception as e:
        log.error("task_scheduler: recovery scan failed: %s", e)
        return 0


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

    # BUG-TS-1 FIX: Recover any tasks left in "running" state from a
    # previous crashed process before starting the normal dispatch loop.
    await recover_stale_running_tasks()

    while True:
        try:
            now_ms = int(time.time() * 1000)
            # 查詢所有 next_run <= now_ms 且狀態為 active 的任務
            # BUG-TS-1 FIX: Only dispatch tasks with status='active' (not
            # 'running') so a task already being executed is not dispatched
            # a second time if the scheduler polls before it finishes.
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
                    # Fallback: direct dispatch (backward compat).
                    # p17c BUG-FIX (MEDIUM): store the task reference in a local
                    # variable and attach a done-callback.  Without storing the
                    # reference the task can be garbage-collected before it
                    # finishes (Python asyncio does not keep strong refs to
                    # tasks created with create_task()).  The done-callback also
                    # logs any unhandled exception that would otherwise be
                    # silently swallowed.
                    _t = asyncio.create_task(
                        run_task(task, get_group_fn, run_agent_fn),
                        name=f"sched-fallback-{task.get('id', 'unknown')}",
                    )
                    _t.add_done_callback(
                        lambda _task: (
                            log.error(
                                "Unhandled exception in scheduler fallback task %s: %s",
                                _task.get_name(), _task.exception(), exc_info=_task.exception(),
                            )
                            if not _task.cancelled() and _task.exception() is not None
                            else None
                        )
                    )
        except Exception as e:
            log.error(f"Scheduler error: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.SCHEDULER_POLL_INTERVAL)
            break  # shutdown
        except asyncio.TimeoutError:
            pass  # normal poll cycle

"""
Per-group queue with global concurrency control.

Ensures only one container runs per group at a time, and enforces a global
MAX_CONCURRENT_CONTAINERS limit across all groups. Tasks are prioritized
over pending messages. Includes exponential backoff retry on failure.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

from . import config

log = logging.getLogger(__name__)


def _task_done_callback(task: asyncio.Task) -> None:
    """Log any unhandled exception from a fire-and-forget asyncio Task (Issue #71).

    Without this callback, exceptions raised inside create_task() coroutines
    are silently discarded by the event loop and only surface as a vague
    'Task exception was never retrieved' message at DEBUG level.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Unhandled exception in task %s: %s", task.get_name(), exc, exc_info=exc)


# 失敗後最多重試幾次（超過後放棄，等下一則新訊息觸發）
MAX_RETRIES = 5
# 第一次重試的等待秒數，之後每次倍增（指數退避 exponential backoff）
BASE_RETRY_SECS = 5.0
# Backpressure: maximum pending tasks per group before new ones are dropped.
# Prevents unbounded memory growth when the scheduler fires faster than containers complete.
MAX_PENDING_TASKS_PER_GROUP = 50
# Maximum number of groups waiting for a concurrency slot (FIFO waiting list cap).
# Prevents _waiting_groups from growing without bound under heavy load.
MAX_WAITING_GROUPS = 100


@dataclass
class _QueuedTask:
    """代表一個等待執行的排程任務。"""
    id: str               # 任務唯一 ID（用於去重複）
    group_jid: str        # 所屬群組的 JID
    fn: Callable[[], Awaitable[None]]  # 任務實際執行的 coroutine


@dataclass
class _GroupState:
    """記錄單一群組的 container 執行狀態與待辦工作。"""
    active: bool = False            # 目前是否有 container 在跑
    idle_waiting: bool = False      # 是否在等待全域 concurrency 槽位釋放
    is_task_container: bool = False # 目前跑的是排程任務（而非訊息回覆）
    running_task_id: Optional[str] = None  # 正在執行的任務 ID（去重複用）
    pending_messages: bool = False  # 是否有訊息等待下一輪處理
    pending_tasks: list = field(default_factory=list)  # 等待執行的 _QueuedTask 清單
    retry_count: int = 0            # 目前連續失敗次數（用於退避計算）


class GroupQueue:
    """
    管理每個群組的 container 執行排程，並強制全域並發上限。

    核心設計原則：
    - 每個群組同時只能有一個 container 在執行（避免並發寫入衝突）
    - 全域最多同時執行 MAX_CONCURRENT_CONTAINERS 個 container
    - 排程任務（Task）優先於訊息回覆（Message），因為任務有固定執行時間、
      而訊息已存入 DB 可以稍後重新撈取，任務若不優先可能導致排程時間漂移
    - 等待中的群組使用 FIFO 佇列，公平分配 concurrency 槽位

    使用方式：
        gq = GroupQueue()
        gq.set_process_messages_fn(my_fn)

        # 當某群組有新訊息時：
        gq.enqueue_message_check(jid)

        # 當排程任務到期時：
        gq.enqueue_task(jid, task_id, task_fn)
    """

    def __init__(self):
        self._groups: dict[str, _GroupState] = {}  # 各群組的狀態，以 JID 為 key
        self._active_count: int = 0                 # 目前正在執行的 container 總數
        self._waiting_groups: list[str] = []        # 等待 concurrency 槽位的群組 JID 清單（FIFO）
        self._process_messages_fn: Optional[Callable[[str], Awaitable[bool]]] = None
        self._shutting_down: bool = False

    def set_process_messages_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        """
        Register the coroutine to call when messages need processing.
        fn(group_jid) -> bool (True = success, False = retry)
        """
        self._process_messages_fn = fn

    def _get_group(self, jid: str) -> _GroupState:
        """取得或建立某群組的狀態物件（懶初始化）。"""
        if jid not in self._groups:
            self._groups[jid] = _GroupState()
        return self._groups[jid]

    # ── Public API ────────────────────────────────────────────────────────────

    def enqueue_message_check(self, group_jid: str) -> None:
        """
        通知系統某群組有新訊息需要處理。

        不立即執行：先檢查兩個限制條件 ——
        1. 若該群組目前有 container 在跑，只標記 pending_messages = True，
           等 container 結束後由 _drain_group 繼續處理。
        2. 若全域 concurrency 已滿，加入 _waiting_groups 等待槽位釋放。
        只有兩個條件都通過，才建立 asyncio task 立即執行。
        """
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        if state.active:
            # 有 container 正在跑，先標記等下次排程
            state.pending_messages = True
            log.debug(f"[{group_jid}] Container active — message queued")
            return

        if self._active_count >= config.MAX_CONCURRENT_CONTAINERS:
            # 全域並發已滿，加入等待佇列（FIFO，避免飢餓）
            state.pending_messages = True
            if group_jid not in self._waiting_groups:
                if len(self._waiting_groups) < MAX_WAITING_GROUPS:
                    self._waiting_groups.append(group_jid)
                else:
                    log.warning(
                        "[%s] _waiting_groups at cap (%d), message will be retried on next poll",
                        group_jid, MAX_WAITING_GROUPS,
                    )
            log.debug(f"[{group_jid}] At concurrency limit ({self._active_count}) — message queued")
            return

        # 條件都滿足，同步更新狀態後建立 asyncio task（避免 race：多個 task 排入前計數來不及更新）
        state.active = True
        self._active_count += 1
        t = asyncio.create_task(
            self._run_for_group(group_jid, reason="messages"),
            name=f"group-msg-{group_jid}",
        )
        t.add_done_callback(_task_done_callback)

    def enqueue_task(self, group_jid: str, task_id: str, fn: Callable[[], Awaitable[None]]) -> None:
        """
        將一個排程任務加入執行佇列。

        透過 task_id 去重複：若相同 task_id 已在執行中或已在等待佇列，
        則直接忽略。這防止排程器在短時間內重複觸發同一任務
        （例如 scheduler loop 比任務本身跑得更快的情況）。
        """
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        # 去重複檢查：避免同一任務被排入兩次
        if state.running_task_id == task_id:
            log.debug(f"[{group_jid}] Task {task_id} already running — skipped")
            return
        if any(t.id == task_id for t in state.pending_tasks):
            log.debug(f"[{group_jid}] Task {task_id} already queued — skipped")
            return

        task = _QueuedTask(id=task_id, group_jid=group_jid, fn=fn)

        if state.active:
            # 有 container 在跑，任務先排入 pending_tasks 佇列
            if len(state.pending_tasks) >= MAX_PENDING_TASKS_PER_GROUP:
                log.warning(
                    "[%s] pending_tasks full (%d/%d), dropping task %s",
                    group_jid, len(state.pending_tasks), MAX_PENDING_TASKS_PER_GROUP, task_id,
                )
                return
            state.pending_tasks.append(task)
            log.debug(f"[{group_jid}] Container active — task {task_id} queued")
            return

        if self._active_count >= config.MAX_CONCURRENT_CONTAINERS:
            # 全域並發已滿，也加入等待佇列
            if len(state.pending_tasks) >= MAX_PENDING_TASKS_PER_GROUP:
                log.warning(
                    "[%s] pending_tasks full (%d/%d), dropping task %s",
                    group_jid, len(state.pending_tasks), MAX_PENDING_TASKS_PER_GROUP, task_id,
                )
                return
            state.pending_tasks.append(task)
            if group_jid not in self._waiting_groups:
                if len(self._waiting_groups) < MAX_WAITING_GROUPS:
                    self._waiting_groups.append(group_jid)
                else:
                    log.warning(
                        "[%s] _waiting_groups at cap (%d), group will not be queued",
                        group_jid, MAX_WAITING_GROUPS,
                    )
            log.debug(f"[{group_jid}] At concurrency limit — task {task_id} queued")
            return

        # 同步更新狀態後建立 asyncio task（避免 race）
        state.active = True
        state.is_task_container = True
        state.running_task_id = task.id
        self._active_count += 1
        t = asyncio.create_task(
            self._run_task(group_jid, task),
            name=f"group-task-{group_jid}-{task_id}",
        )
        t.add_done_callback(_task_done_callback)

    # ── Internal runners ──────────────────────────────────────────────────────

    async def _run_for_group(self, group_jid: str, reason: str) -> None:
        """
        實際執行「訊息回覆」container 的內部方法。
        標記群組為 active、增加全域計數，執行完畢後自動呼叫 _drain_group
        繼續處理待辦工作。
        """
        state = self._get_group(group_jid)
        # active and _active_count were already incremented synchronously before create_task
        state.idle_waiting = False
        state.is_task_container = False
        state.pending_messages = False  # 清除 pending flag，開始處理

        log.debug(f"[{group_jid}] Starting container (reason={reason}, active={self._active_count})")

        try:
            if self._process_messages_fn:
                success = await self._process_messages_fn(group_jid)
                if success:
                    state.retry_count = 0  # 成功後重置退避計數
                else:
                    self._schedule_retry(group_jid, state)
        except Exception as e:
            log.error(f"[{group_jid}] Error processing messages: {e}")
            self._schedule_retry(group_jid, state)
        finally:
            # 無論成功或失敗都要釋放狀態，確保下一個工作可以進來
            state.active = False
            self._active_count -= 1
            self._drain_group(group_jid)

    async def _run_task(self, group_jid: str, task: _QueuedTask) -> None:
        """
        實際執行「排程任務」container 的內部方法。
        與 _run_for_group 邏輯相似，但不需要 retry（任務失敗會記錄 log）。
        """
        state = self._get_group(group_jid)
        # active, is_task_container, running_task_id, _active_count set synchronously before create_task
        state.idle_waiting = False

        log.debug(f"[{group_jid}] Running task {task.id} (active={self._active_count})")

        try:
            await task.fn()
        except Exception as e:
            log.error(f"[{group_jid}] Error running task {task.id}: {e}")
        finally:
            state.active = False
            state.is_task_container = False
            state.running_task_id = None
            self._active_count -= 1
            self._drain_group(group_jid)

    def _schedule_retry(self, group_jid: str, state: _GroupState) -> None:
        """
        排程指數退避重試（exponential backoff）。
        delay = BASE_RETRY_SECS * 2^(retry_count - 1)
        超過 MAX_RETRIES 後放棄，等下一則新訊息自然觸發。
        """
        state.retry_count += 1
        if state.retry_count > MAX_RETRIES:
            log.error(f"[{group_jid}] Max retries exceeded — dropping (will retry on next message)")
            state.retry_count = 0
            return

        delay = BASE_RETRY_SECS * (2 ** (state.retry_count - 1))
        log.info(f"[{group_jid}] Retry {state.retry_count}/{MAX_RETRIES} in {delay:.1f}s")

        async def _retry():
            await asyncio.sleep(delay)
            if not self._shutting_down:
                self.enqueue_message_check(group_jid)

        t = asyncio.create_task(_retry(), name=f"retry-{group_jid}")
        t.add_done_callback(_task_done_callback)

    def _drain_group(self, group_jid: str) -> None:
        """
        當某個群組的 container 結束後，檢查是否還有待辦工作並繼續執行。

        優先順序：排程任務 > 訊息回覆 > 釋放槽位給其他等待群組。

        任務優先的原因：排程任務不在 DB 中排隊（只存在記憶體的 pending_tasks），
        如果不優先處理，任務可能因訊息插隊而被無限期延後，破壞排程準時性。
        """
        if self._shutting_down:
            return

        state = self._get_group(group_jid)

        # 任務優先：先把待辦任務清空
        if state.pending_tasks:
            task = state.pending_tasks.pop(0)
            state.active = True
            state.is_task_container = True
            state.running_task_id = task.id
            self._active_count += 1
            t = asyncio.create_task(
                self._run_task(group_jid, task),
                name=f"group-task-{group_jid}-{task.id}",
            )
            t.add_done_callback(_task_done_callback)
            return

        # 再處理待辦訊息
        if state.pending_messages:
            state.active = True
            self._active_count += 1
            t = asyncio.create_task(
                self._run_for_group(group_jid, reason="drain"),
                name=f"group-msg-{group_jid}",
            )
            t.add_done_callback(_task_done_callback)
            return

        # 本群組沒有待辦工作，嘗試把空出的 concurrency 槽位給等待中的群組
        self._drain_waiting()

    def _drain_waiting(self) -> None:
        """
        從 FIFO 等待佇列（_waiting_groups）中依序取出群組並啟動執行。
        每次呼叫只消耗可用的 concurrency 槽位數量，不超過全域上限。
        FIFO 順序確保早進來的群組不會被後來的群組插隊（公平調度）。
        """
        while self._waiting_groups and self._active_count < config.MAX_CONCURRENT_CONTAINERS:
            next_jid = self._waiting_groups.pop(0)
            state = self._get_group(next_jid)

            # 同樣優先處理任務，再處理訊息
            if state.pending_tasks:
                task = state.pending_tasks.pop(0)
                state.active = True
                state.is_task_container = True
                state.running_task_id = task.id
                self._active_count += 1
                t = asyncio.create_task(
                    self._run_task(next_jid, task),
                    name=f"group-task-{next_jid}-{task.id}",
                )
                t.add_done_callback(_task_done_callback)
            elif state.pending_messages:
                state.active = True
                self._active_count += 1
                t = asyncio.create_task(
                    self._run_for_group(next_jid, reason="waiting"),
                    name=f"group-msg-{next_jid}",
                )
                t.add_done_callback(_task_done_callback)

    def shutdown_sync(self) -> None:
        """Signal shutdown from a synchronous context (e.g. signal handler).
        No new tasks will be accepted after this call.
        """
        self._shutting_down = True
        log.info(f"GroupQueue: shutdown signalled (active containers: {self._active_count})")

    async def shutdown(self) -> None:
        """Signal shutdown — no new tasks will be started."""
        self._shutting_down = True
        log.info(f"GroupQueue shutting down (active containers: {self._active_count})")

    async def wait_for_active(self, timeout: float = 30.0) -> None:
        """Wait until all in-flight containers finish, or until timeout expires.

        Should be called after shutdown() during graceful shutdown to avoid
        aborting containers mid-response and leaving cursor/IPC state inconsistent.
        """
        if self._active_count == 0:
            return
        log.info(
            "Waiting up to %.0fs for %d active container(s) to finish...",
            timeout,
            self._active_count,
        )
        deadline = asyncio.get_event_loop().time() + timeout
        while self._active_count > 0:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                log.warning(
                    "Graceful shutdown timeout: %d container(s) still active",
                    self._active_count,
                )
                break
            await asyncio.sleep(min(0.5, remaining))
        log.info("GroupQueue: all containers finished (or timeout reached)")

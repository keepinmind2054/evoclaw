"""系統健康監控模組（Health Monitor）

監控 EvoClaw 系統的整體健康狀態，包括：
- Container 排隊數量
- 錯誤率統計
- 記憶體使用量
- 資料庫大小
- 群組活躍度

當檢測到異常時，自動發出警告通知。
"""
import asyncio
import logging
import os
import threading
import time
import psutil
from datetime import datetime, timedelta
from typing import Callable, Awaitable

from . import config, db

log = logging.getLogger(__name__)

# 監控閾值設定
CONTAINER_QUEUE_WARNING = 10  # 排隊數量警告閾值
CONTAINER_QUEUE_CRITICAL = 50  # 排隊數量嚴重閾值
ERROR_RATE_WARNING = 0.3  # 錯誤率警告閾值（30%）
ERROR_RATE_MIN_SAMPLES = 5  # 最小樣本數，避免小樣本誤報（e.g. 1/1 = 100%）
MEMORY_USAGE_WARNING_MB = 500  # 記憶體使用量警告閾值（MB）
DB_SIZE_WARNING_MB = 100  # 資料庫大小警告閾值（MB）
GROUP_INACTIVE_DAYS = 7  # 群組不活躍天數閾值

# 警告冷卻時間（秒），避免重複警告
WARNING_COOLDOWN = 300  # 5 分鐘

# 最後警告時間記錄
_last_warnings: dict[str, datetime] = {}
# Lock protecting _last_warnings for thread-safe access from sync and async paths
_warnings_lock = threading.Lock()

# Liveness tracking: updated each time the monitor loop completes a cycle.
# External callers can read _last_liveness_ts to distinguish "monitor alive
# but system healthy" from "monitor itself has crashed".
_last_liveness_ts: float = 0.0

# How long (seconds) without a completed cycle before the monitor is considered stuck.
LIVENESS_STALE_THRESHOLD = 180  # 3 minutes


def is_monitor_alive() -> bool:
    """Return True if the health monitor loop has run within the liveness threshold."""
    if _last_liveness_ts == 0.0:
        return False  # never completed a cycle yet
    return (time.monotonic() - _last_liveness_ts) < LIVENESS_STALE_THRESHOLD


async def health_monitor_loop(stop_event: asyncio.Event) -> None:
    """
    健康監控主迴圈，每 60 秒檢查一次系統健康狀態。

    監控項目：
    1. Container 排隊數量
    2. 最近 5 分鐘錯誤率
    3. 記憶體使用量
    4. 資料庫大小
    5. 群組活躍度

    BUG-HM-01 (CRITICAL): Previously, an unhandled exception in
    _check_all_health_metrics() would cause the loop to silently stop if the
    outer try/except itself faulted.  The loop is now wrapped in a separate
    inner try so a crash in one cycle is logged at ERROR level and the loop
    continues.  _last_liveness_ts is updated after every successful cycle so
    callers can detect a stuck monitor via is_monitor_alive().
    """
    global _last_liveness_ts
    log.info("Health monitor started")

    while not stop_event.is_set():
        try:
            await _check_all_health_metrics()
            # Update liveness timestamp after each successful pass
            _last_liveness_ts = time.monotonic()
        except Exception as e:
            # BUG-HM-01: Log at ERROR (not just debug) so operators notice a
            # broken health-check cycle.
            log.error(f"Health check cycle failed: {e}", exc_info=True)

        # 每 60 秒檢查一次
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass

    log.info("Health monitor stopped")


async def _check_all_health_metrics() -> None:
    """檢查所有健康指標並發出警告。"""
    
    # 1. 檢查 Container 排隊數量
    await _check_container_queue()
    
    # 2. 檢查錯誤率
    await _check_error_rate()
    
    # 3. 檢查記憶體使用量
    _check_memory_usage()
    
    # 4. 檢查資料庫大小
    _check_database_size()
    
    # 5. 檢查群組活躍度
    await _check_group_activity()


async def _check_container_queue() -> None:
    """檢查 Container 排隊數量。"""
    try:
        from .group_queue import GroupQueue
        # 獲取全域隊列實例（如果有）
        # 這裡我們直接從 DB 獲取待處理任務數量
        pending_count = db.get_pending_task_count() if hasattr(db, 'get_pending_task_count') else 0

        if pending_count >= CONTAINER_QUEUE_CRITICAL:
            await _send_warning(
                "critical",
                f"Container queue critical: {pending_count} tasks pending",
                "container_queue"
            )
        elif pending_count >= CONTAINER_QUEUE_WARNING:
            await _send_warning(
                "warning",
                f"Container queue high: {pending_count} tasks pending",
                "container_queue"
            )
    except Exception as e:
        # BUG-HM-02 (MEDIUM): Was silently swallowed at debug level — use warning
        # so container runner issues are visible in production logs.
        log.warning(f"Failed to check container queue: {e}")


async def _check_error_rate() -> None:
    """檢查最近 5 分鐘的錯誤率。"""
    try:
        # 從資料庫獲取錯誤統計
        error_stats = db.get_error_stats(minutes=5) if hasattr(db, 'get_error_stats') else None

        if error_stats:
            total = error_stats.get('total', 0)
            errors = error_stats.get('errors', 0)

            # BUG-HM-03 (MEDIUM): Guard against division by zero even when
            # total < ERROR_RATE_MIN_SAMPLES to avoid a ZeroDivisionError if
            # the branch is ever reached with total==0.
            if total >= ERROR_RATE_MIN_SAMPLES and total > 0:
                error_rate = errors / total

                if error_rate >= ERROR_RATE_WARNING:
                    await _send_warning(
                        "warning",
                        f"High error rate detected: {error_rate:.1%} ({errors}/{total})",
                        "error_rate"
                    )
    except Exception as e:
        log.warning(f"Failed to check error rate: {e}")


def _check_memory_usage() -> None:
    """檢查記憶體使用量。"""
    try:
        process = psutil.Process(os.getpid())
        memory_mb = process.memory_info().rss / 1024 / 1024  # 轉換為 MB
        
        if memory_mb >= MEMORY_USAGE_WARNING_MB:
            _send_warning_sync(
                "warning",
                f"High memory usage: {memory_mb:.1f} MB",
                "memory_usage"
            )
    except Exception as e:
        log.debug(f"Failed to check memory usage: {e}")


def _check_database_size() -> None:
    """檢查資料庫大小。"""
    try:
        db_path = config.STORE_DIR / "messages.db"
        if db_path.exists():
            size_mb = db_path.stat().st_size / 1024 / 1024  # 轉換為 MB
            
            if size_mb >= DB_SIZE_WARNING_MB:
                _send_warning_sync(
                    "warning",
                    f"Database size large: {size_mb:.1f} MB",
                    "db_size"
                )
    except Exception as e:
        log.debug(f"Failed to check database size: {e}")


async def _check_group_activity() -> None:
    """檢查群組活躍度。"""
    try:
        groups = db.get_all_registered_groups()
        now = datetime.now()
        
        for group in groups:
            jid = group.get('jid', '')
            last_activity = group.get('last_activity', 0)
            
            if last_activity > 0:
                last_activity_dt = datetime.fromtimestamp(last_activity / 1000)
                days_inactive = (now - last_activity_dt).days
                
                if days_inactive >= GROUP_INACTIVE_DAYS:
                    # 只對主群組發送一次警告
                    if group.get('is_main', False):
                        await _send_warning(
                            "info",
                            f"Group inactive for {days_inactive} days: {jid}",
                            f"group_inactive_{jid}"
                        )
    except Exception as e:
        log.debug(f"Failed to check group activity: {e}")


async def _send_warning(level: str, message: str, warning_id: str) -> None:
    """發送警告（非同步版本）。"""
    if _should_send_warning(warning_id):
        log_msg = f"[{level.upper()}] {message}"
        if level == "critical":
            log.critical(log_msg)
        elif level == "warning":
            log.warning(log_msg)
        else:
            log.info(log_msg)

        # TODO: 可以在這裡加入发送通知到 Telegram/Slack 等

        # BUG-HM-04 (MEDIUM): _last_warnings is mutated from both the async
        # loop and the sync helper without any lock.  Use _warnings_lock to
        # prevent a race condition on multi-threaded deployments.
        with _warnings_lock:
            _last_warnings[warning_id] = datetime.now()


def _send_warning_sync(level: str, message: str, warning_id: str) -> None:
    """發送警告（同步版本）。"""
    if _should_send_warning(warning_id):
        log_msg = f"[{level.upper()}] {message}"
        if level == "critical":
            log.critical(log_msg)
        elif level == "warning":
            log.warning(log_msg)
        else:
            log.info(log_msg)

        with _warnings_lock:
            _last_warnings[warning_id] = datetime.now()


def _should_send_warning(warning_id: str) -> bool:
    """檢查是否應該發送警告（避免重複）。"""
    # BUG-HM-04 (MEDIUM): protect read under the same lock used for writes.
    with _warnings_lock:
        if warning_id not in _last_warnings:
            return True
        time_since_last = datetime.now() - _last_warnings[warning_id]
    return time_since_last.total_seconds() >= WARNING_COOLDOWN


def get_health_status() -> dict:
    """
    獲取當前健康狀態摘要。

    BUG-HM-05 (HIGH): Previously always returned {"status": "healthy"} even
    when memory or DB thresholds were exceeded.  Now derives status from
    actual threshold checks, and includes a liveness field so operators can
    detect a stuck monitor loop.

    回傳：
        dict: 包含各項健康指標的當前狀態
    """
    try:
        process = psutil.Process(os.getpid())
        memory_mb = process.memory_info().rss / 1024 / 1024

        # 資料庫大小
        db_path = config.STORE_DIR / "messages.db"
        db_size_mb = db_path.stat().st_size / 1024 / 1024 if db_path.exists() else 0

        # 群組數量
        groups = db.get_all_registered_groups()
        active_groups = len([g for g in groups if g.get('is_main', False)])

        # Derive overall status from threshold violations
        warnings = []
        if memory_mb >= MEMORY_USAGE_WARNING_MB:
            warnings.append(f"high_memory:{memory_mb:.1f}MB")
        if db_size_mb >= DB_SIZE_WARNING_MB:
            warnings.append(f"large_db:{db_size_mb:.1f}MB")

        overall = "degraded" if warnings else "healthy"

        return {
            "status": overall,
            "warnings": warnings,
            "memory_usage_mb": round(memory_mb, 2),
            "database_size_mb": round(db_size_mb, 2),
            "total_groups": len(groups),
            "active_groups": active_groups,
            # Liveness: lets callers distinguish "system healthy" from
            # "monitor loop itself is stuck / crashed".
            "monitor_alive": is_monitor_alive(),
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        log.error(f"Failed to get health status: {e}")
        return {
            "status": "error",
            "error": str(e),
            "monitor_alive": is_monitor_alive(),
            "timestamp": datetime.now().isoformat(),
        }

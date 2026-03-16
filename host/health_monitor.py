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


async def health_monitor_loop(stop_event: asyncio.Event) -> None:
    """
    健康監控主迴圈，每 60 秒檢查一次系統健康狀態。
    
    監控項目：
    1. Container 排隊數量
    2. 最近 5 分鐘錯誤率
    3. 記憶體使用量
    4. 資料庫大小
    5. 群組活躍度
    """
    log.info("Health monitor started")
    
    while not stop_event.is_set():
        try:
            await _check_all_health_metrics()
        except Exception as e:
            log.error(f"Health check error: {e}")
        
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
        log.debug(f"Failed to check container queue: {e}")


async def _check_error_rate() -> None:
    """檢查最近 5 分鐘的錯誤率。"""
    try:
        # 從資料庫獲取錯誤統計
        error_stats = db.get_error_stats(minutes=5) if hasattr(db, 'get_error_stats') else None
        
        if error_stats:
            total = error_stats.get('total', 0)
            errors = error_stats.get('errors', 0)
            
            if total >= ERROR_RATE_MIN_SAMPLES:
                error_rate = errors / total

                if error_rate >= ERROR_RATE_WARNING:
                    await _send_warning(
                        "warning",
                        f"High error rate detected: {error_rate:.1%} ({errors}/{total})",
                        "error_rate"
                    )
    except Exception as e:
        log.debug(f"Failed to check error rate: {e}")


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
        
        _last_warnings[warning_id] = datetime.now()


def _should_send_warning(warning_id: str) -> bool:
    """檢查是否應該發送警告（避免重複）。"""
    if warning_id not in _last_warnings:
        return True
    
    time_since_last = datetime.now() - _last_warnings[warning_id]
    return time_since_last.total_seconds() >= WARNING_COOLDOWN


def get_health_status() -> dict:
    """
    獲取當前健康狀態摘要。
    
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
        
        return {
            "status": "healthy",
            "memory_usage_mb": round(memory_mb, 2),
            "database_size_mb": round(db_size_mb, 2),
            "total_groups": len(groups),
            "active_groups": active_groups,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        log.error(f"Failed to get health status: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        }

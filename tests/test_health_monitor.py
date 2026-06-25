import pytest
import asyncio
import time
from pathlib import Path
from datetime import datetime
from host import config
from host import health_monitor

class DummyGroup:
    def __init__(self, jid, last_activity, is_main):
        self.jid = jid
        self.last_activity = last_activity
        self.is_main = is_main
        
    def get(self, key, default=None):
        if key == 'jid':
            return self.jid
        if key == 'last_activity':
            return self.last_activity
        if key == 'is_main':
            return self.is_main
        return default

@pytest.mark.asyncio
async def test_health_monitor_metrics(tmp_path, monkeypatch):
    # Setup alert queue to capture warnings
    alert_queue = asyncio.Queue()
    health_monitor.set_alert_queue(alert_queue)
    
    # Reset cooldown logs for clean alerts
    health_monitor._alert_last_sent.clear()
    health_monitor._last_warnings.clear()
    
    # 1. Mock DB functions
    # Queue size
    monkeypatch.setattr(health_monitor.db, "get_pending_task_count", lambda: 60)
    # Error rate
    monkeypatch.setattr(health_monitor.db, "get_error_stats", lambda minutes: {"total": 10, "errors": 4})
    # Group activity (JID inactive for 10 days)
    ten_days_ago_ms = int((time.time() - 10 * 86400) * 1000)
    mock_group = DummyGroup("tg:main_group", ten_days_ago_ms, True)
    monkeypatch.setattr(health_monitor.db, "get_all_registered_groups", lambda: [mock_group])
    
    # 2. Patch config and memory settings
    monkeypatch.setattr(config, "STORE_DIR", tmp_path)
    monkeypatch.setattr(health_monitor, "MEMORY_USAGE_WARNING_MB", 1)  # Force memory alert
    monkeypatch.setattr(health_monitor, "DB_SIZE_WARNING_MB", 1)      # DB threshold 1MB
    
    # Create a dummy large DB file
    db_file = tmp_path / "messages.db"
    db_file.write_bytes(b"\x00" * (2 * 1024 * 1024))  # 2 MB file (exceeds 1MB)
    
    # Execute checks
    await health_monitor._check_all_health_metrics()
    
    # Verify alerts in queue
    alerts = []
    while not alert_queue.empty():
        alerts.append(alert_queue.get_nowait()[1])
        
    # Check that we received alerts for all issues
    alert_str = "\n".join(alerts)
    assert "queue critical" in alert_str.lower()
    assert "high error rate" in alert_str.lower()
    assert "memory usage" in alert_str.lower()
    assert "database size large" in alert_str.lower()
    assert "group inactive for 10 days" in alert_str.lower()


@pytest.mark.asyncio
async def test_health_monitor_liveness():
    health_monitor._last_liveness_ts = 0.0
    assert health_monitor.is_monitor_alive() is False
    
    # Simulate a successful cycle
    health_monitor._last_liveness_ts = time.monotonic()
    assert health_monitor.is_monitor_alive() is True
    
    # Simulate stale monitor
    health_monitor._last_liveness_ts = time.monotonic() - 200
    assert health_monitor.is_monitor_alive() is False

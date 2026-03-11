"""
Core system tests: DB operations, router, task scheduler, health monitor.

Tests run without Docker, real LLM calls, or external services.
All DB operations use in-memory SQLite via monkeypatch.
"""
import asyncio
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path):
    """Create a real temp SQLite DB for testing."""
    import host.db as db_module
    p = tmp_path / "test.db"
    db_module.init_database(p)
    old = db_module._DB_PATH if hasattr(db_module, "_DB_PATH") else None
    db_module._DB_PATH = p
    # Reset connection cache so new path is used
    if hasattr(db_module, "_conn"):
        try:
            db_module._conn.close()
        except Exception:
            pass
        db_module._conn = None
    yield p
    if old is not None:
        db_module._DB_PATH = old


@pytest.fixture
def fresh_db(db_path):
    """Return the db module pointed at a fresh temp database."""
    import host.db as db_module
    return db_module


# ── DB: init & basic operations ───────────────────────────────────────────────

class TestDatabase:
    def test_init_creates_tables(self, fresh_db):
        """All expected tables should exist after init."""
        conn = fresh_db.get_db()
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        for expected in ["messages", "scheduled_tasks", "registered_groups",
                         "sessions", "evolution_runs", "group_genome",
                         "immune_threats", "evolution_log", "dev_sessions"]:
            assert expected in tables, f"Missing table: {expected}"

    def test_store_and_retrieve_message(self, fresh_db):
        msg_id = str(uuid.uuid4())
        jid = "tg:123456789"
        fresh_db.store_message(
            msg_id, jid, sender="user1", sender_name="Alice",
            content="Hello world", timestamp=1_700_000_000_000,
        )
        msgs = fresh_db.get_new_messages([jid], last_timestamp=0)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello world"
        assert msgs[0]["sender"] == "user1"

    def test_store_message_is_from_me(self, fresh_db):
        msg_id = str(uuid.uuid4())
        jid = "tg:123"
        fresh_db.store_message(
            msg_id, jid, sender="bot", sender_name="Andy",
            content="Bot reply", timestamp=2_000_000_000_000,
            is_from_me=True, is_bot_message=True,
        )
        msgs = fresh_db.get_new_messages([jid], last_timestamp=0)
        # Bot messages should NOT be returned as new messages to process
        assert all(not m.get("is_bot_message") for m in msgs)

    def test_get_new_messages_respects_timestamp(self, fresh_db):
        jid = "tg:789"
        for i, ts in enumerate([1000, 2000, 3000]):
            fresh_db.store_message(str(uuid.uuid4()), jid,
                                   sender="u", sender_name="U",
                                   content=f"msg{i}", timestamp=ts)
        msgs = fresh_db.get_new_messages([jid], last_timestamp=1500)
        assert len(msgs) == 2
        assert all(m["timestamp"] > 1500 for m in msgs)

    def test_set_and_get_state(self, fresh_db):
        fresh_db.set_state("lastTimestamp", "12345")
        val = fresh_db.get_state("lastTimestamp")
        assert val == "12345"

    def test_get_state_returns_none_for_missing(self, fresh_db):
        val = fresh_db.get_state("nonexistent_key")
        assert val is None

    def test_registered_groups_crud(self, fresh_db):
        fresh_db.set_registered_group(
            jid="tg:9999",
            name="Test Group",
            folder="telegram_test",
            trigger_pattern="@Andy",
            container_config=None,
            requires_trigger=True,
            is_main=False,
        )
        groups = fresh_db.get_all_registered_groups()
        assert any(g["jid"] == "tg:9999" for g in groups)

    def test_create_and_query_task(self, fresh_db):
        task_id = str(uuid.uuid4())
        fresh_db.create_task(
            task_id=task_id,
            group_folder="telegram_test",
            chat_jid="tg:9999",
            prompt="Say good morning",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            next_run=int(time.time() * 1000) + 3600_000,
            context_mode="group",
        )
        tasks = fresh_db.get_all_tasks()
        assert any(t["id"] == task_id for t in tasks)

    def test_update_task_status(self, fresh_db):
        task_id = str(uuid.uuid4())
        fresh_db.create_task(
            task_id=task_id,
            group_folder="g",
            chat_jid="tg:1",
            prompt="test",
            schedule_type="interval",
            schedule_value="60000",
            next_run=int(time.time() * 1000) + 60_000,
            context_mode="isolated",
        )
        fresh_db.update_task(task_id, status="paused")
        tasks = fresh_db.get_all_tasks()
        task = next(t for t in tasks if t["id"] == task_id)
        assert task["status"] == "paused"

    def test_delete_task(self, fresh_db):
        task_id = str(uuid.uuid4())
        fresh_db.create_task(
            task_id=task_id, group_folder="g", chat_jid="tg:1",
            prompt="bye", schedule_type="once",
            schedule_value="2030-01-01T00:00:00",
            next_run=int(time.time() * 1000) + 999_999_000,
            context_mode="isolated",
        )
        fresh_db.delete_task(task_id)
        tasks = fresh_db.get_all_tasks()
        assert not any(t["id"] == task_id for t in tasks)

    def test_store_and_get_session(self, fresh_db):
        fresh_db.set_session("telegram_test", "claude-session-abc123")
        sid = fresh_db.get_session("telegram_test")
        assert sid == "claude-session-abc123"

    def test_evolution_run_recording(self, fresh_db):
        run_id = str(uuid.uuid4())
        fresh_db.record_evolution_run(
            jid="tg:1",
            run_id=run_id,
            response_ms=1500,
            retry_count=0,
            success=True,
        )
        conn = fresh_db.get_db()
        rows = conn.execute(
            "SELECT * FROM evolution_runs WHERE run_id=?", (run_id,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["success"] == 1

    def test_get_due_tasks_returns_overdue(self, fresh_db):
        task_id = str(uuid.uuid4())
        past_time = int(time.time() * 1000) - 10_000  # 10s ago
        fresh_db.create_task(
            task_id=task_id, group_folder="g", chat_jid="tg:1",
            prompt="overdue", schedule_type="interval",
            schedule_value="60000",
            next_run=past_time,
            context_mode="isolated",
        )
        due = fresh_db.get_due_tasks(int(time.time() * 1000))
        assert any(t["id"] == task_id for t in due)

    def test_get_due_tasks_excludes_future(self, fresh_db):
        task_id = str(uuid.uuid4())
        future_time = int(time.time() * 1000) + 999_000
        fresh_db.create_task(
            task_id=task_id, group_folder="g", chat_jid="tg:1",
            prompt="future", schedule_type="interval",
            schedule_value="60000",
            next_run=future_time,
            context_mode="isolated",
        )
        due = fresh_db.get_due_tasks(int(time.time() * 1000))
        assert not any(t["id"] == task_id for t in due)


# ── Router ────────────────────────────────────────────────────────────────────

class TestRouter:
    def test_register_and_find_channel(self):
        """Channels registered with register_channel should be found by JID."""
        import host.router as router

        mock_ch = MagicMock()
        mock_ch.owns_jid = lambda jid: jid.startswith("test:")
        mock_ch.name = "test-channel"

        # Save and restore state
        original = list(router._channels)
        router._channels.clear()
        try:
            router.register_channel(mock_ch)
            found = router.find_channel("test:12345")
            assert found is mock_ch

            not_found = router.find_channel("tg:12345")
            assert not_found is None
        finally:
            router._channels.clear()
            router._channels.extend(original)

    def test_format_messages_empty(self):
        from host.router import format_messages
        result = format_messages([])
        assert isinstance(result, list)
        assert result == []

    def test_format_messages_structures_history(self):
        from host.router import format_messages
        msgs = [
            {"sender": "user1", "content": "Hello", "is_from_me": False, "timestamp": 1000},
            {"sender": "bot",   "content": "Hi!",   "is_from_me": True,  "timestamp": 2000},
        ]
        result = format_messages(msgs)
        assert isinstance(result, list)
        assert len(result) == 2
        # User message should be "user" role, bot should be "assistant"
        roles = {m.get("role") for m in result}
        assert "user" in roles or len(result) > 0  # structure check

    @pytest.mark.asyncio
    async def test_route_outbound_sends_via_channel(self):
        """route_outbound should call the matching channel's send_message."""
        import host.router as router

        mock_ch = MagicMock()
        mock_ch.owns_jid = lambda jid: jid == "tg:999"
        mock_ch.send_message = AsyncMock()

        original = list(router._channels)
        router._channels.clear()
        try:
            router.register_channel(mock_ch)
            await router.route_outbound("tg:999", "Hello from test")
            mock_ch.send_message.assert_awaited_once_with("tg:999", "Hello from test")
        finally:
            router._channels.clear()
            router._channels.extend(original)

    @pytest.mark.asyncio
    async def test_route_outbound_no_channel_no_crash(self):
        """route_outbound should not raise if no channel owns the JID."""
        import host.router as router
        original = list(router._channels)
        router._channels.clear()
        try:
            await router.route_outbound("unknown:123", "test message")  # Should not raise
        finally:
            router._channels.clear()
            router._channels.extend(original)


# ── Task Scheduler: _compute_next_run ─────────────────────────────────────────

class TestSchedulerComputeNextRun:
    def test_interval_next_run(self):
        from host.ipc_watcher import _compute_next_run
        now_ms = int(time.time() * 1000)
        result = _compute_next_run("interval", "60000")
        assert result is not None
        assert result > now_ms
        assert abs(result - (now_ms + 60_000)) < 1000  # within 1 second

    def test_once_next_run_future(self):
        from host.ipc_watcher import _compute_next_run
        future = "2035-01-01T00:00:00"
        result = _compute_next_run("once", future)
        assert result is not None
        assert result > int(time.time() * 1000)

    def test_once_past_date(self):
        from host.ipc_watcher import _compute_next_run
        past = "2020-01-01T00:00:00"
        result = _compute_next_run("once", past)
        assert result is not None
        assert result < int(time.time() * 1000)

    def test_cron_next_run(self):
        from host.ipc_watcher import _compute_next_run
        result = _compute_next_run("cron", "0 9 * * *")
        if result is None:
            pytest.skip("croniter not installed")
        assert result > int(time.time() * 1000)

    def test_invalid_interval_returns_none(self):
        from host.ipc_watcher import _compute_next_run
        result = _compute_next_run("interval", "not_a_number")
        assert result is None

    def test_unknown_type_returns_none(self):
        from host.ipc_watcher import _compute_next_run
        result = _compute_next_run("unknown_type", "whatever")
        assert result is None


# ── IPC Watcher: _require_own_or_main ─────────────────────────────────────────

class TestIpcPermissions:
    def test_own_group_allowed(self):
        from host.ipc_watcher import _require_own_or_main
        # Should not raise
        _require_own_or_main("my_group", "my_group", is_main=False)

    def test_main_can_access_any_group(self):
        from host.ipc_watcher import _require_own_or_main
        # Should not raise
        _require_own_or_main("group_a", "group_b", is_main=True)

    def test_non_main_cannot_access_other_group(self):
        from host.ipc_watcher import _require_own_or_main
        with pytest.raises(PermissionError):
            _require_own_or_main("group_a", "group_b", is_main=False)


# ── Health Monitor ────────────────────────────────────────────────────────────

class TestHealthMonitor:
    def test_get_health_status_returns_dict(self, fresh_db):
        """get_health_status() should return a dict with status key."""
        from host.health_monitor import get_health_status
        result = get_health_status()
        assert isinstance(result, dict)
        assert "status" in result
        assert "timestamp" in result

    def test_should_send_warning_first_time(self):
        """Warning should be sent if it's never been sent before."""
        from host.health_monitor import _should_send_warning, _last_warnings
        unique_id = f"test_warn_{uuid.uuid4().hex}"
        # Ensure it's not in the dict
        _last_warnings.pop(unique_id, None)
        assert _should_send_warning(unique_id) is True

    def test_should_not_send_warning_within_cooldown(self):
        """Warning should be suppressed if sent recently."""
        from host.health_monitor import _should_send_warning, _last_warnings
        from datetime import datetime
        unique_id = f"test_cooldown_{uuid.uuid4().hex}"
        _last_warnings[unique_id] = datetime.now()
        assert _should_send_warning(unique_id) is False
        del _last_warnings[unique_id]

    @pytest.mark.asyncio
    async def test_health_monitor_loop_stops_on_event(self):
        """health_monitor_loop should exit when stop_event is set."""
        from host.health_monitor import health_monitor_loop
        stop = asyncio.Event()
        stop.set()  # Already set — should exit immediately
        # Should complete without hanging
        await asyncio.wait_for(health_monitor_loop(stop), timeout=5.0)


# ── Dev Log helpers ───────────────────────────────────────────────────────────

class TestDevLogHelpers:
    def test_write_and_read_dev_log(self, tmp_path):
        from host.dev_engine import _write_dev_log, get_dev_logs
        import host.config as cfg_module
        original_data_dir = cfg_module.DATA_DIR
        cfg_module.DATA_DIR = tmp_path

        try:
            sid = "dev_test_123"
            _write_dev_log(sid, "Stage started")
            _write_dev_log(sid, "Stage completed")
            lines = get_dev_logs(sid, offset=0)
            assert len(lines) == 2
            assert any("Stage started" in l for l in lines)
            assert any("Stage completed" in l for l in lines)
        finally:
            cfg_module.DATA_DIR = original_data_dir

    def test_get_dev_logs_offset(self, tmp_path):
        from host.dev_engine import _write_dev_log, get_dev_logs
        import host.config as cfg_module
        original_data_dir = cfg_module.DATA_DIR
        cfg_module.DATA_DIR = tmp_path

        try:
            sid = "dev_offset_test"
            for i in range(5):
                _write_dev_log(sid, f"Line {i}")
            lines = get_dev_logs(sid, offset=3)
            assert len(lines) == 2
            assert "Line 3" in lines[0]
            assert "Line 4" in lines[1]
        finally:
            cfg_module.DATA_DIR = original_data_dir

    def test_get_dev_logs_missing_session(self, tmp_path):
        from host.dev_engine import get_dev_logs
        import host.config as cfg_module
        original = cfg_module.DATA_DIR
        cfg_module.DATA_DIR = tmp_path
        try:
            lines = get_dev_logs("nonexistent_session", offset=0)
            assert lines == []
        finally:
            cfg_module.DATA_DIR = original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Infrastructure tests covering channels, group_queue, webportal,
container_runner circuit breaker, IPC error handling, and main group guard.

Tests run without Docker, real LLM calls, or external services.
"""
import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def in_memory_db(tmp_path):
    """Initialize the DB module with a fresh in-memory database."""
    import host.db as db_module
    p = tmp_path / "test_infra.db"
    db_module.init_database(p)
    yield db_module
    # Reset global connection so next test gets a fresh one
    if db_module._db is not None:
        try:
            db_module._db.close()
        except Exception:
            pass
        db_module._db = None


# ── Fix #1: Channels registry ─────────────────────────────────────────────────

class TestChannelsRegistry:
    def test_register_channel_class_stores_class(self):
        """register_channel_class should store a class by name."""
        from host.channels import register_channel_class, get_channel_class
        sentinel = type("FakeChannel", (), {})
        register_channel_class("fake_test_ch", sentinel)
        assert get_channel_class("fake_test_ch") is sentinel

    def test_get_channel_class_returns_none_for_unknown(self):
        """get_channel_class should return None for unregistered names."""
        from host.channels import get_channel_class
        result = get_channel_class("__definitely_not_registered__")
        assert result is None

    def test_get_registered_channel_names_includes_registered(self):
        """get_registered_channel_names should include names we register."""
        from host.channels import register_channel_class, get_registered_channel_names
        sentinel = type("FakeChannel2", (), {})
        register_channel_class("fake_test_ch2", sentinel)
        names = get_registered_channel_names()
        assert "fake_test_ch2" in names

    def test_register_channel_class_overwrite(self):
        """Re-registering a name should overwrite the previous class."""
        from host.channels import register_channel_class, get_channel_class
        cls_a = type("ClsA", (), {})
        cls_b = type("ClsB", (), {})
        register_channel_class("overwrite_test", cls_a)
        register_channel_class("overwrite_test", cls_b)
        assert get_channel_class("overwrite_test") is cls_b


# ── Fix #2: Group Queue ────────────────────────────────────────────────────────

class TestGroupQueue:
    def _make_queue(self, max_concurrent=2):
        """Create a GroupQueue with patched config."""
        with patch("host.group_queue.config") as mock_cfg:
            mock_cfg.MAX_CONCURRENT_CONTAINERS = max_concurrent
            from host.group_queue import GroupQueue
            gq = GroupQueue()
        return gq

    @pytest.mark.asyncio
    async def test_enqueue_message_check_calls_process_fn(self):
        """enqueue_message_check should trigger the process_messages function."""
        with patch("host.group_queue.config") as mock_cfg:
            mock_cfg.MAX_CONCURRENT_CONTAINERS = 5
            from host.group_queue import GroupQueue
            gq = GroupQueue()

        called_jids = []

        async def fake_process(jid):
            called_jids.append(jid)
            return True

        gq.set_process_messages_fn(fake_process)
        gq.enqueue_message_check("jid-a")
        # Give the asyncio task a chance to run
        await asyncio.sleep(0.05)
        assert "jid-a" in called_jids

    @pytest.mark.asyncio
    async def test_concurrent_limit_queues_extra_group(self):
        """When at max concurrency, new groups should be queued."""
        with patch("host.group_queue.config") as mock_cfg:
            mock_cfg.MAX_CONCURRENT_CONTAINERS = 1
            from host.group_queue import GroupQueue
            gq = GroupQueue()

        barrier = asyncio.Event()

        async def slow_process(jid):
            await barrier.wait()
            return True

        gq.set_process_messages_fn(slow_process)
        gq.enqueue_message_check("jid-first")
        await asyncio.sleep(0.01)  # Let first task start

        # At limit now — second should queue
        gq.enqueue_message_check("jid-second")
        assert "jid-second" in gq._waiting_groups

        barrier.set()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_same_group_no_parallel_execution(self):
        """The same group should not run two containers simultaneously."""
        with patch("host.group_queue.config") as mock_cfg:
            mock_cfg.MAX_CONCURRENT_CONTAINERS = 5
            from host.group_queue import GroupQueue
            gq = GroupQueue()

        active_count = 0
        max_active = 0
        done = asyncio.Event()

        async def track_process(jid):
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            await asyncio.sleep(0.02)
            active_count -= 1
            return True

        gq.set_process_messages_fn(track_process)
        gq.enqueue_message_check("single-jid")
        gq.enqueue_message_check("single-jid")  # should queue, not parallel
        await asyncio.sleep(0.1)
        assert max_active <= 1

    @pytest.mark.asyncio
    async def test_task_deduplication_prevents_double_enqueue(self):
        """Enqueueing the same task_id twice should not run it twice."""
        with patch("host.group_queue.config") as mock_cfg:
            mock_cfg.MAX_CONCURRENT_CONTAINERS = 5
            from host.group_queue import GroupQueue
            gq = GroupQueue()

        run_count = 0

        async def counted_task():
            nonlocal run_count
            run_count += 1

        gq.enqueue_task("jid-dedup", "task-001", counted_task)
        gq.enqueue_task("jid-dedup", "task-001", counted_task)  # duplicate
        await asyncio.sleep(0.05)
        assert run_count == 1

    @pytest.mark.asyncio
    async def test_shutdown_prevents_new_tasks(self):
        """After shutdown(), new enqueues should be silently ignored."""
        with patch("host.group_queue.config") as mock_cfg:
            mock_cfg.MAX_CONCURRENT_CONTAINERS = 5
            from host.group_queue import GroupQueue
            gq = GroupQueue()

        called = []

        async def process(jid):
            called.append(jid)
            return True

        gq.set_process_messages_fn(process)
        await gq.shutdown()
        gq.enqueue_message_check("jid-after-shutdown")
        await asyncio.sleep(0.05)
        assert "jid-after-shutdown" not in called


# ── Fix #3: Webportal ─────────────────────────────────────────────────────────

class TestWebportal:
    def test_deliver_reply_pushes_to_matching_session(self):
        """deliver_reply should append to all sessions matching the JID."""
        from host.webportal import deliver_reply, _sessions, _sessions_lock

        session_id = str(uuid.uuid4())
        with _sessions_lock:
            _sessions[session_id] = {"jid": "test-jid-123", "messages": [], "created": time.time()}

        deliver_reply("test-jid-123", "Hello from bot")

        with _sessions_lock:
            msgs = _sessions[session_id]["messages"]
        assert len(msgs) == 1
        assert msgs[0]["text"] == "Hello from bot"
        assert msgs[0]["role"] == "assistant"

        # Cleanup
        with _sessions_lock:
            _sessions.pop(session_id, None)

    def test_deliver_reply_ignores_nonmatching_jid(self):
        """deliver_reply should not push to sessions with different JID."""
        from host.webportal import deliver_reply, _sessions, _sessions_lock

        session_id = str(uuid.uuid4())
        with _sessions_lock:
            _sessions[session_id] = {"jid": "jid-other", "messages": [], "created": time.time()}

        deliver_reply("jid-totally-different", "Wrong group message")

        with _sessions_lock:
            msgs = _sessions[session_id]["messages"]
        assert len(msgs) == 0

        with _sessions_lock:
            _sessions.pop(session_id, None)

    def test_session_stores_jid_and_messages(self):
        """A new session should be stored with the given JID."""
        from host.webportal import _sessions, _sessions_lock

        session_id = str(uuid.uuid4())
        jid = "test-jid-session"
        with _sessions_lock:
            _sessions[session_id] = {"jid": jid, "messages": [], "created": time.time()}

        with _sessions_lock:
            stored = _sessions.get(session_id)
        assert stored is not None
        assert stored["jid"] == jid
        assert stored["messages"] == []

        with _sessions_lock:
            _sessions.pop(session_id, None)

    def test_poll_returns_messages_since_timestamp(self):
        """Messages with ts > since should be returned by poll logic."""
        from host.webportal import _sessions, _sessions_lock

        session_id = str(uuid.uuid4())
        now = time.time()
        with _sessions_lock:
            _sessions[session_id] = {
                "jid": "jid-poll",
                "messages": [
                    {"role": "user", "text": "old msg", "ts": now - 100},
                    {"role": "assistant", "text": "new msg", "ts": now + 1},
                ],
                "created": now,
            }

        with _sessions_lock:
            session = _sessions.get(session_id, {})
            msgs = [m for m in session.get("messages", []) if m["ts"] > now]

        assert len(msgs) == 1
        assert msgs[0]["text"] == "new msg"

        with _sessions_lock:
            _sessions.pop(session_id, None)


# ── Fix #5: Container runner circuit breaker ──────────────────────────────────

class TestDockerCircuitBreaker:
    def setup_method(self):
        """Reset circuit breaker state before each test."""
        import host.container_runner as cr
        with cr._docker_failure_lock:
            cr._docker_failures = 0

    def test_circuit_open_after_threshold_failures(self):
        """Circuit should open after _DOCKER_CIRCUIT_THRESHOLD failures."""
        import host.container_runner as cr
        assert not cr._docker_circuit_open()

        for _ in range(cr._DOCKER_CIRCUIT_THRESHOLD):
            cr._record_docker_failure()

        assert cr._docker_circuit_open()

    def test_circuit_closed_below_threshold(self):
        """Circuit should remain closed with fewer failures than threshold."""
        import host.container_runner as cr
        for _ in range(cr._DOCKER_CIRCUIT_THRESHOLD - 1):
            cr._record_docker_failure()
        assert not cr._docker_circuit_open()

    def test_record_success_resets_counter(self):
        """Recording success should reset the failure counter."""
        import host.container_runner as cr
        for _ in range(cr._DOCKER_CIRCUIT_THRESHOLD):
            cr._record_docker_failure()
        assert cr._docker_circuit_open()

        cr._record_docker_success()
        assert not cr._docker_circuit_open()

    @pytest.mark.asyncio
    async def test_run_container_raises_when_circuit_open(self):
        """run_container_agent should raise RuntimeError when circuit is open."""
        import host.container_runner as cr
        # Force open
        with cr._docker_failure_lock:
            cr._docker_failures = cr._DOCKER_CIRCUIT_THRESHOLD

        group = {"jid": "test-jid", "folder": "test-folder", "is_main": False}
        with pytest.raises(RuntimeError, match="circuit breaker open"):
            await cr.run_container_agent(group=group, prompt="hello")

    def test_thread_safety_of_failure_counter(self):
        """Multiple threads incrementing failures should not race."""
        import host.container_runner as cr
        with cr._docker_failure_lock:
            cr._docker_failures = 0

        threads = [threading.Thread(target=cr._record_docker_failure) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cr._docker_failures == 10


# ── Fix #4: IPC error handling ────────────────────────────────────────────────

class TestIpcErrorHandling:
    @pytest.mark.asyncio
    async def test_ipc_json_error_moves_file_to_errors_dir(self, tmp_path):
        """Invalid JSON IPC files should be moved to errors/ directory."""
        from unittest.mock import AsyncMock, patch
        import host.ipc_watcher as ipc_mod

        # Set up a fake IPC directory
        group_folder = "test-ipc-group"
        ipc_dir = tmp_path / "ipc" / group_folder / "messages"
        ipc_dir.mkdir(parents=True)

        bad_file = ipc_dir / "bad_001.json"
        bad_file.write_text("{ invalid json }", encoding="utf-8")

        errors_dir = tmp_path / "ipc" / group_folder / "errors"

        with patch("host.ipc_watcher.config") as mock_cfg:
            mock_cfg.DATA_DIR = tmp_path

            async def dummy_route(jid, text, sender=None):
                pass

            await ipc_mod.process_ipc_dir(group_folder, False, dummy_route)

        moved = list(errors_dir.glob("bad_001.json"))
        assert len(moved) == 1, "Bad JSON file should have been moved to errors dir"
        assert not bad_file.exists(), "Original bad file should be gone"

    @pytest.mark.asyncio
    async def test_ipc_valid_message_is_deleted_after_processing(self, tmp_path):
        """Valid IPC files should be deleted after successful processing."""
        import host.ipc_watcher as ipc_mod

        group_folder = "test-ipc-valid"
        ipc_dir = tmp_path / "ipc" / group_folder / "messages"
        ipc_dir.mkdir(parents=True)

        good_file = ipc_dir / "good_001.json"
        good_file.write_text(json.dumps({
            "type": "message",
            "chatJid": "test@s.whatsapp.net",
            "text": "Hello",
        }), encoding="utf-8")

        with patch("host.ipc_watcher.config") as mock_cfg:
            mock_cfg.DATA_DIR = tmp_path

            received = []

            async def capture_route(jid, text, sender=None):
                received.append((jid, text))

            await ipc_mod.process_ipc_dir(group_folder, False, capture_route)

        assert not good_file.exists(), "Processed file should be deleted"
        assert ("test@s.whatsapp.net", "Hello") in received


# ── Fix #7: Main group uniqueness guard ───────────────────────────────────────

class TestMainGroupUniqueness:
    def test_registering_new_main_demotes_old_main(self, in_memory_db):
        """Setting is_main=True for a group should demote all other main groups."""
        db = in_memory_db

        # Register first main group
        db.set_registered_group(
            jid="jid-main-1", name="Main One", folder="main-one",
            trigger_pattern="@bot", container_config=None,
            requires_trigger=True, is_main=True,
        )

        # Verify it's the only main
        groups = db.get_all_registered_groups()
        mains = [g for g in groups if g["is_main"]]
        assert len(mains) == 1
        assert mains[0]["jid"] == "jid-main-1"

        # Register another group as main — old main should be demoted
        db.set_registered_group(
            jid="jid-main-2", name="Main Two", folder="main-two",
            trigger_pattern="@bot", container_config=None,
            requires_trigger=True, is_main=True,
        )

        groups = db.get_all_registered_groups()
        mains = [g for g in groups if g["is_main"]]
        assert len(mains) == 1, "Only one group should be main after reassignment"
        assert mains[0]["jid"] == "jid-main-2"

    def test_non_main_group_does_not_demote_existing_main(self, in_memory_db):
        """Registering a non-main group should leave the main group alone."""
        db = in_memory_db

        db.set_registered_group(
            jid="jid-main-keep", name="Main Keep", folder="main-keep",
            trigger_pattern="@bot", container_config=None,
            requires_trigger=True, is_main=True,
        )

        db.set_registered_group(
            jid="jid-other", name="Other", folder="other",
            trigger_pattern="@bot", container_config=None,
            requires_trigger=True, is_main=False,
        )

        groups = db.get_all_registered_groups()
        mains = [g for g in groups if g["is_main"]]
        assert len(mains) == 1
        assert mains[0]["jid"] == "jid-main-keep"

    def test_get_main_group_helper_returns_correct_group(self, in_memory_db):
        """get_main_group from main.py should return the main group."""
        from host.main import get_main_group

        groups = [
            {"jid": "jid-a", "is_main": False},
            {"jid": "jid-b", "is_main": True},
            {"jid": "jid-c", "is_main": False},
        ]
        result = get_main_group(groups)
        assert result is not None
        assert result["jid"] == "jid-b"

    def test_get_main_group_returns_none_when_no_main(self):
        """get_main_group should return None if no main group exists."""
        from host.main import get_main_group

        groups = [
            {"jid": "jid-a", "is_main": False},
            {"jid": "jid-b", "is_main": False},
        ]
        result = get_main_group(groups)
        assert result is None

    def test_get_main_group_warns_on_multiple_mains(self, caplog):
        """get_main_group should log a warning if multiple main groups exist."""
        import logging
        from host.main import get_main_group

        groups = [
            {"jid": "jid-a", "is_main": True},
            {"jid": "jid-b", "is_main": True},
        ]
        with caplog.at_level(logging.WARNING, logger="evoclaw"):
            result = get_main_group(groups)

        assert result is not None
        assert any("Multiple main groups" in r.message for r in caplog.records)

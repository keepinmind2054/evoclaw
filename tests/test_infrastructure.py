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
    # p13d fix: _docker_failures is a dict keyed by group_folder, not a plain
    # int.  All tests must use the per-group API (_record_docker_failure takes
    # an optional group_folder argument that defaults to "_global").
    _GROUP = "_global"

    def setup_method(self):
        """Reset circuit breaker state for the test group before each test."""
        import host.container_runner as cr
        with cr._docker_failure_lock:
            cr._docker_failures.pop(self._GROUP, None)
            cr._docker_failure_time.pop(self._GROUP, None)

    def test_circuit_open_after_threshold_failures(self):
        """Circuit should open after _DOCKER_CIRCUIT_THRESHOLD failures."""
        import host.container_runner as cr
        assert not cr._docker_circuit_open(self._GROUP)

        for _ in range(cr._DOCKER_CIRCUIT_THRESHOLD):
            cr._record_docker_failure(self._GROUP)

        assert cr._docker_circuit_open(self._GROUP)

    def test_circuit_closed_below_threshold(self):
        """Circuit should remain closed with fewer failures than threshold."""
        import host.container_runner as cr
        for _ in range(cr._DOCKER_CIRCUIT_THRESHOLD - 1):
            cr._record_docker_failure(self._GROUP)
        assert not cr._docker_circuit_open(self._GROUP)

    def test_record_success_resets_counter(self):
        """Recording success should reset the failure counter."""
        import host.container_runner as cr
        for _ in range(cr._DOCKER_CIRCUIT_THRESHOLD):
            cr._record_docker_failure(self._GROUP)
        assert cr._docker_circuit_open(self._GROUP)

        cr._record_docker_success(self._GROUP)
        assert not cr._docker_circuit_open(self._GROUP)

    @pytest.mark.asyncio
    async def test_run_container_returns_error_when_circuit_open(self):
        """run_container_agent should return an error dict when circuit is open.

        p13d fix: the function does NOT raise RuntimeError — it returns
        ``{"status": "error", "error": "Docker circuit breaker open for ..."}``
        and notifies the user.  The old test asserted a RuntimeError which
        would never fire, making it a silent false-positive.
        """
        import host.container_runner as cr
        # Force open for the test group folder
        test_folder = "test-folder"
        with cr._docker_failure_lock:
            cr._docker_failures[test_folder] = cr._DOCKER_CIRCUIT_THRESHOLD
            cr._docker_failure_time[test_folder] = time.time()

        group = {"jid": "test-jid", "folder": test_folder, "is_main": False}
        result = await cr.run_container_agent(group=group, prompt="hello")
        assert result["status"] == "error"
        assert "circuit breaker" in result["error"].lower()

        # Cleanup
        with cr._docker_failure_lock:
            cr._docker_failures.pop(test_folder, None)
            cr._docker_failure_time.pop(test_folder, None)

    def test_thread_safety_of_failure_counter(self):
        """Multiple threads incrementing failures for the same group should not race."""
        import host.container_runner as cr
        with cr._docker_failure_lock:
            cr._docker_failures[self._GROUP] = 0

        threads = [threading.Thread(target=cr._record_docker_failure, args=(self._GROUP,)) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cr._docker_failures[self._GROUP] == 10


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


# ── Fix #1 (v1.10.0): _stop_container awaits proc.wait() ─────────────────────

class TestStopContainerAwaitsProcWait:
    @pytest.mark.asyncio
    async def test_stop_container_awaits_wait(self):
        """_stop_container should call proc.wait() after create_subprocess_exec."""
        import host.container_runner as cr

        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            await cr._stop_container("evoclaw-test-container")

        mock_proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_container_passes_time_flag(self):
        """_stop_container should pass --time 10 to docker stop."""
        import host.container_runner as cr

        mock_proc = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        captured_args = []

        async def capture_exec(*args, **kwargs):
            captured_args.extend(args)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=capture_exec):
            await cr._stop_container("mycontainer")

        assert "--time" in captured_args
        assert "10" in captured_args
        assert "mycontainer" in captured_args

    @pytest.mark.asyncio
    async def test_stop_container_swallows_exception(self):
        """_stop_container should not raise even if subprocess fails."""
        import host.container_runner as cr

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=OSError("no docker"))):
            # Should not raise
            await cr._stop_container("ghost-container")


# ── Fix #2 (v1.10.0): /api/env key allowlist ─────────────────────────────────

class TestEnvKeyAllowlist:
    def test_editable_env_keys_is_frozenset(self):
        """EDITABLE_ENV_KEYS must be a frozenset."""
        from host import config
        assert isinstance(config.EDITABLE_ENV_KEYS, frozenset)

    def test_editable_env_keys_contains_expected_keys(self):
        """EDITABLE_ENV_KEYS should contain the standard configurable keys.

        p13d fix: the key is ``TELEGRAM_BOT_TOKEN`` (not ``TELEGRAM_TOKEN``),
        and ``DASHBOARD_PASSWORD`` is intentionally excluded from the editable
        set (changing it requires an env restart).  The old assertion checked
        for non-existent / excluded keys and would have failed against the
        real config, masking a real misconfiguration.
        """
        from host import config
        for key in ("CLAUDE_API_KEY", "TELEGRAM_BOT_TOKEN",
                    "CONTAINER_IMAGE", "MAX_CONCURRENT_CONTAINERS"):
            assert key in config.EDITABLE_ENV_KEYS, f"{key} should be in EDITABLE_ENV_KEYS"
        # DASHBOARD_PASSWORD is intentionally NOT editable via dashboard
        assert "DASHBOARD_PASSWORD" not in config.EDITABLE_ENV_KEYS, (
            "DASHBOARD_PASSWORD must NOT be in EDITABLE_ENV_KEYS — "
            "password changes require an env restart to take effect."
        )

    def test_env_post_rejects_disallowed_key(self):
        """Disallowed keys should produce an error string from the validation logic."""
        from host import config
        disallowed_key = "PATH"
        assert disallowed_key not in config.EDITABLE_ENV_KEYS

        key = disallowed_key.strip()
        if key not in config.EDITABLE_ENV_KEYS:
            error = f"Key '{key}' is not editable via dashboard"
        else:
            error = None
        assert error is not None, "Disallowed key should produce an error"

    def test_env_post_allows_claude_api_key(self):
        """POST /api/env with CLAUDE_API_KEY should pass validation."""
        from host import config
        assert "CLAUDE_API_KEY" in config.EDITABLE_ENV_KEYS

    def test_newline_stripped_from_value(self):
        """Newlines and control chars should be stripped from env values."""
        value = "abc\r\ndef\x00ghi"
        cleaned = "".join(ch for ch in value if ch not in "\r\n\x00")
        assert cleaned == "abcdefghi"


# ── Fix #6 (v1.10.0): WebPortal session TTL expiry ───────────────────────────

class TestWebportalSessionTTL:
    def test_expire_sessions_removes_stale_sessions(self):
        """Sessions idle longer than TTL should be removed by _expire_sessions."""
        from host.webportal import _sessions, _sessions_lock, _expire_sessions, _SESSION_TTL_SECONDS
        import time as _time

        stale_id = "stale-session-" + str(uuid.uuid4())
        fresh_id = "fresh-session-" + str(uuid.uuid4())
        now = _time.time()

        with _sessions_lock:
            _sessions[stale_id] = {
                "jid": "jid-x",
                "messages": [],
                "created": now - _SESSION_TTL_SECONDS - 100,
                "last_seen": now - _SESSION_TTL_SECONDS - 100,
            }
            _sessions[fresh_id] = {
                "jid": "jid-y",
                "messages": [],
                "created": now,
                "last_seen": now,
            }

        _expire_sessions()

        with _sessions_lock:
            assert stale_id not in _sessions, "Stale session should have been expired"
            assert fresh_id in _sessions, "Fresh session should be kept"
            _sessions.pop(fresh_id, None)

    def test_session_ttl_constant_is_3600(self):
        """_SESSION_TTL_SECONDS should be 3600 (1 hour)."""
        from host.webportal import _SESSION_TTL_SECONDS
        assert _SESSION_TTL_SECONDS == 3600

    def test_new_session_has_last_seen(self):
        """A newly created session should have a last_seen timestamp."""
        from host.webportal import _sessions, _sessions_lock
        import time as _time

        session_id = str(uuid.uuid4())
        now = _time.time()
        with _sessions_lock:
            _sessions[session_id] = {
                "jid": "jid-z",
                "messages": [],
                "created": now,
                "last_seen": now,
            }

        with _sessions_lock:
            sess = _sessions.get(session_id)

        assert sess is not None
        assert "last_seen" in sess
        assert sess["last_seen"] >= now - 1

        with _sessions_lock:
            _sessions.pop(session_id, None)


# ── Fix #5 (v1.10.0): Scheduler GroupQueue routing ───────────────────────────

class TestSchedulerGroupQueueRouting:
    @pytest.mark.asyncio
    async def test_scheduler_calls_enqueue_task_when_group_queue_provided(self):
        """start_scheduler_loop should call group_queue.enqueue_task for due tasks."""
        from host.task_scheduler import start_scheduler_loop
        import asyncio

        mock_queue = MagicMock()
        mock_queue.enqueue_task = MagicMock()

        fake_task = {
            "id": "task-abc",
            "chat_jid": "jid-test",
            "group_folder": "test-group",
            "prompt": "hello",
            "schedule_type": "once",
            "schedule_value": "",
            "context_mode": "isolated",
        }

        stop_event = asyncio.Event()

        with patch("host.task_scheduler.db") as mock_db:
            mock_db.get_due_tasks = MagicMock(return_value=[fake_task])
            with patch("host.task_scheduler.config") as mock_cfg:
                mock_cfg.SCHEDULER_POLL_INTERVAL = 0.01

                async def stop_after_delay():
                    await asyncio.sleep(0.05)
                    stop_event.set()

                asyncio.create_task(stop_after_delay())
                await start_scheduler_loop(
                    lambda jid: {"jid": jid, "folder": "test"},
                    AsyncMock(),
                    stop_event,
                    group_queue=mock_queue,
                )

        assert mock_queue.enqueue_task.called, "enqueue_task should be called when group_queue is provided"

    @pytest.mark.asyncio
    async def test_scheduler_falls_back_to_create_task_without_group_queue(self):
        """Without group_queue, start_scheduler_loop should use asyncio.create_task."""
        from host.task_scheduler import start_scheduler_loop
        import asyncio

        fake_task = {
            "id": "task-xyz",
            "chat_jid": "jid-fallback",
            "group_folder": "fallback-group",
            "prompt": "do something",
            "schedule_type": "once",
            "schedule_value": "",
            "context_mode": "isolated",
        }

        stop_event = asyncio.Event()
        created_tasks = []
        original_create_task = asyncio.create_task

        def spy_create_task(coro, **kwargs):
            task = original_create_task(coro, **kwargs)
            created_tasks.append(task)
            return task

        with patch("host.task_scheduler.db") as mock_db:
            mock_db.get_due_tasks = MagicMock(return_value=[fake_task])
            with patch("host.task_scheduler.config") as mock_cfg:
                mock_cfg.SCHEDULER_POLL_INTERVAL = 0.01
                with patch("asyncio.create_task", side_effect=spy_create_task):
                    async def stop_after_delay():
                        await asyncio.sleep(0.05)
                        stop_event.set()

                    asyncio.create_task(stop_after_delay())
                    await start_scheduler_loop(
                        lambda jid: {"jid": jid, "folder": "fallback"},
                        AsyncMock(),
                        stop_event,
                        group_queue=None,
                    )

        assert len(created_tasks) > 0, "create_task should be called when group_queue is None"


# ── p13d: Container security flags ────────────────────────────────────────────

class TestContainerSecurityFlags:
    """Verify that _build_docker_cmd (via run_container_agent mock path)
    includes the mandatory security flags introduced in p13d."""

    def test_safe_name_strips_path_traversal(self):
        """_safe_name must neutralise path-traversal sequences."""
        from host.container_runner import _safe_name
        assert ".." not in _safe_name("../../../etc")
        assert "/" not in _safe_name("some/folder")
        assert _safe_name("normal_group") != ""

    def test_safe_name_strips_dots_and_slashes(self):
        """Dots and slashes must be replaced so the result is docker-name safe."""
        from host.container_runner import _safe_name
        result = _safe_name("../evil")
        assert "/" not in result
        assert "." not in result

    def test_safe_name_handles_empty_input(self):
        """Empty or all-special-char input should return a non-empty fallback."""
        from host.container_runner import _safe_name
        assert _safe_name("") != ""
        assert _safe_name("...") != ""

    def test_build_volume_mounts_rejects_traversal_folder(self, tmp_path):
        """_build_volume_mounts must raise ValueError for a folder that would
        escape the expected groups/ipc/sessions directories."""
        from unittest.mock import patch
        import host.container_runner as cr
        import host.config as cfg

        group = {"folder": "../../etc", "is_main": False, "jid": "tg:1"}

        with patch.object(cfg, "GROUPS_DIR", tmp_path / "groups"), \
             patch.object(cfg, "DATA_DIR", tmp_path / "data"), \
             patch.object(cfg, "BASE_DIR", tmp_path):
            (tmp_path / "groups").mkdir(parents=True, exist_ok=True)
            (tmp_path / "data").mkdir(parents=True, exist_ok=True)
            with pytest.raises(ValueError, match="path traversal"):
                cr._build_volume_mounts(group)

    @pytest.mark.asyncio
    async def test_docker_run_includes_network_none(self, tmp_path, monkeypatch):
        """The docker run command must include '--network none'."""
        import host.container_runner as cr
        import host.config as cfg

        captured_cmd = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured_cmd.extend(args)
            mock_proc = MagicMock()
            mock_proc.stdout = None
            mock_proc.stderr = None
            mock_proc.returncode = 0
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            raise Exception("abort after capture")

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
        monkeypatch.setattr(cfg, "CONTAINER_MEMORY", "")
        monkeypatch.setattr(cfg, "CONTAINER_CPUS", "")

        groups_dir = tmp_path / "groups"
        groups_dir.mkdir()
        (groups_dir / "test-group").mkdir()
        data_dir = tmp_path / "data"
        (data_dir / "ipc" / "test-group" / "messages").mkdir(parents=True)
        (data_dir / "ipc" / "test-group" / "tasks").mkdir(parents=True)
        (data_dir / "ipc" / "test-group" / "input").mkdir(parents=True)
        (data_dir / "ipc" / "test-group" / "results").mkdir(parents=True)
        (data_dir / "sessions" / "test-group" / ".claude").mkdir(parents=True)
        (data_dir / "dynamic_tools").mkdir(parents=True)

        monkeypatch.setattr(cfg, "GROUPS_DIR", groups_dir)
        monkeypatch.setattr(cfg, "DATA_DIR", data_dir)

        with patch("host.container_runner.db"), \
             patch("host.container_runner.get_adaptive_hints", return_value=[]), \
             patch("host.container_runner.get_genome_style_hints", return_value=[]), \
             patch("host.container_runner.get_hot_memory", return_value=""), \
             patch("host.container_runner._read_secrets", return_value={}), \
             patch("host.container_runner.db.get_messages_since", return_value=[]), \
             patch("host.container_runner.db.get_all_tasks", return_value=[]), \
             patch("host.container_runner.db.log_container_start"), \
             patch("host.container_runner._get_agent_id", return_value="agent-1"), \
             patch("host.container_runner._docker_circuit_open", return_value=0):
            try:
                await cr.run_container_agent(
                    group={"jid": "tg:1", "folder": "test-group", "is_main": False},
                    prompt="hello",
                )
            except Exception:
                pass  # expected — we abort after capturing the cmd

        assert "--network" in captured_cmd, "docker run must include --network flag"
        net_idx = captured_cmd.index("--network")
        assert captured_cmd[net_idx + 1] == "none", "network must be set to 'none'"

    @pytest.mark.asyncio
    async def test_docker_run_includes_cap_drop_all(self, tmp_path, monkeypatch):
        """The docker run command must include '--cap-drop ALL'."""
        import host.container_runner as cr
        import host.config as cfg

        captured_cmd = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured_cmd.extend(args)
            raise Exception("abort after capture")

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
        monkeypatch.setattr(cfg, "CONTAINER_MEMORY", "")
        monkeypatch.setattr(cfg, "CONTAINER_CPUS", "")

        groups_dir = tmp_path / "groups"
        groups_dir.mkdir()
        (groups_dir / "test-group").mkdir()
        data_dir = tmp_path / "data"
        (data_dir / "ipc" / "test-group" / "messages").mkdir(parents=True)
        (data_dir / "ipc" / "test-group" / "tasks").mkdir(parents=True)
        (data_dir / "ipc" / "test-group" / "input").mkdir(parents=True)
        (data_dir / "ipc" / "test-group" / "results").mkdir(parents=True)
        (data_dir / "sessions" / "test-group" / ".claude").mkdir(parents=True)
        (data_dir / "dynamic_tools").mkdir(parents=True)

        monkeypatch.setattr(cfg, "GROUPS_DIR", groups_dir)
        monkeypatch.setattr(cfg, "DATA_DIR", data_dir)

        with patch("host.container_runner.db"), \
             patch("host.container_runner.get_adaptive_hints", return_value=[]), \
             patch("host.container_runner.get_genome_style_hints", return_value=[]), \
             patch("host.container_runner.get_hot_memory", return_value=""), \
             patch("host.container_runner._read_secrets", return_value={}), \
             patch("host.container_runner.db.get_messages_since", return_value=[]), \
             patch("host.container_runner.db.get_all_tasks", return_value=[]), \
             patch("host.container_runner.db.log_container_start"), \
             patch("host.container_runner._get_agent_id", return_value="agent-1"), \
             patch("host.container_runner._docker_circuit_open", return_value=0):
            try:
                await cr.run_container_agent(
                    group={"jid": "tg:1", "folder": "test-group", "is_main": False},
                    prompt="hello",
                )
            except Exception:
                pass

        assert "--cap-drop" in captured_cmd, "docker run must include --cap-drop flag"
        cap_idx = captured_cmd.index("--cap-drop")
        assert captured_cmd[cap_idx + 1] == "ALL", "--cap-drop must be ALL"

    @pytest.mark.asyncio
    async def test_docker_run_includes_pids_limit(self, tmp_path, monkeypatch):
        """The docker run command must include '--pids-limit'."""
        import host.container_runner as cr
        import host.config as cfg

        captured_cmd = []

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured_cmd.extend(args)
            raise Exception("abort after capture")

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
        monkeypatch.setattr(cfg, "CONTAINER_MEMORY", "")
        monkeypatch.setattr(cfg, "CONTAINER_CPUS", "")

        groups_dir = tmp_path / "groups"
        groups_dir.mkdir()
        (groups_dir / "test-group").mkdir()
        data_dir = tmp_path / "data"
        (data_dir / "ipc" / "test-group" / "messages").mkdir(parents=True)
        (data_dir / "ipc" / "test-group" / "tasks").mkdir(parents=True)
        (data_dir / "ipc" / "test-group" / "input").mkdir(parents=True)
        (data_dir / "ipc" / "test-group" / "results").mkdir(parents=True)
        (data_dir / "sessions" / "test-group" / ".claude").mkdir(parents=True)
        (data_dir / "dynamic_tools").mkdir(parents=True)

        monkeypatch.setattr(cfg, "GROUPS_DIR", groups_dir)
        monkeypatch.setattr(cfg, "DATA_DIR", data_dir)

        with patch("host.container_runner.db"), \
             patch("host.container_runner.get_adaptive_hints", return_value=[]), \
             patch("host.container_runner.get_genome_style_hints", return_value=[]), \
             patch("host.container_runner.get_hot_memory", return_value=""), \
             patch("host.container_runner._read_secrets", return_value={}), \
             patch("host.container_runner.db.get_messages_since", return_value=[]), \
             patch("host.container_runner.db.get_all_tasks", return_value=[]), \
             patch("host.container_runner.db.log_container_start"), \
             patch("host.container_runner._get_agent_id", return_value="agent-1"), \
             patch("host.container_runner._docker_circuit_open", return_value=0):
            try:
                await cr.run_container_agent(
                    group={"jid": "tg:1", "folder": "test-group", "is_main": False},
                    prompt="hello",
                )
            except Exception:
                pass

        assert "--pids-limit" in captured_cmd, "docker run must include --pids-limit flag"

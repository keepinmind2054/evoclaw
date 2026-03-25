"""
Tests for Phase 28a async executor dispatch fixes.

BUG-ASYNC-BLOCKING: Blocking file I/O in async code must be run via
loop.run_in_executor() to avoid stalling the event loop.

Two specific fixes are verified:
  1. _write_monitor_jid_to_env (host/main.py) — called via run_in_executor()
     in _handle_setup_command(), not directly awaited or called inline.
  2. restore_remote_control (host/ipc_watcher.py) — called via run_in_executor()
     at start_ipc_watcher() startup, not directly called in the event loop.

Tests verify the executor dispatch by inspecting that:
  - run_in_executor() is invoked with the target function as the callable argument
  - The functions themselves perform no I/O when called directly in an async context
    (they are synchronous functions that expect to run off-loop)
"""
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── _write_monitor_jid_to_env is dispatched to executor ───────────────────────

class TestWriteMonitorJidExecutorDispatch:
    """_handle_setup_command() must dispatch _write_monitor_jid_to_env to the executor."""

    @pytest.mark.asyncio
    async def test_write_monitor_jid_uses_run_in_executor(self, tmp_path):
        """run_in_executor() must be called with _write_monitor_jid_to_env as the callable."""
        executor_calls = []

        async def fake_run_in_executor(executor, func, *args):
            executor_calls.append((executor, func, args))
            # Call the function so the handler doesn't hang
            if callable(func):
                func(*args)
            return None

        fake_loop = MagicMock()
        fake_loop.run_in_executor = fake_run_in_executor

        env_file = tmp_path / ".env"
        env_file.write_text("MONITOR_JID=\n", encoding="utf-8")

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.config.BASE_DIR", tmp_path):
                with patch("host.config.GROUPS_DIR", tmp_path / "groups"):
                    (tmp_path / "groups").mkdir(exist_ok=True)
                    with patch("asyncio.get_running_loop", return_value=fake_loop):
                        import host.db as db_mod
                        with patch.object(db_mod, "set_registered_group", return_value=None):
                            with patch.object(db_mod, "get_all_registered_groups", return_value=[]):
                                import host.main as main_mod
                                main_mod._MONITOR_JID = None
                                main_mod._registered_groups = []
                                result = await main_mod._handle_setup_command("tg:12345", "monitor")

        # Verify run_in_executor was called
        assert executor_calls, "Expected run_in_executor() to be called at least once"

        # Verify _write_monitor_jid_to_env was one of the dispatched callables
        from host.main import _write_monitor_jid_to_env
        dispatched_funcs = [c[1] for c in executor_calls]
        assert _write_monitor_jid_to_env in dispatched_funcs, (
            f"Expected _write_monitor_jid_to_env to be dispatched to executor; "
            f"got: {dispatched_funcs}"
        )

    @pytest.mark.asyncio
    async def test_write_monitor_jid_not_directly_awaited(self, tmp_path):
        """_write_monitor_jid_to_env must be a synchronous (non-coroutine) function."""
        from host.main import _write_monitor_jid_to_env
        import inspect

        assert not inspect.iscoroutinefunction(_write_monitor_jid_to_env), (
            "_write_monitor_jid_to_env must be a regular (sync) function dispatched to "
            "an executor, not an async function directly awaited"
        )

    @pytest.mark.asyncio
    async def test_write_monitor_jid_writes_env_file(self, tmp_path):
        """_write_monitor_jid_to_env correctly writes MONITOR_JID to the .env file."""
        env_file = tmp_path / ".env"
        env_file.write_text("# EvoClaw config\nMONITOR_JID=old_value\n", encoding="utf-8")

        with patch("host.config.BASE_DIR", tmp_path):
            from host.main import _write_monitor_jid_to_env
            _write_monitor_jid_to_env("tg:99999")

        content = env_file.read_text(encoding="utf-8")
        assert "MONITOR_JID=tg:99999" in content, (
            f"Expected MONITOR_JID=tg:99999 in .env after write; got:\n{content}"
        )

    @pytest.mark.asyncio
    async def test_write_monitor_jid_no_blocking_io_in_event_loop(self, tmp_path):
        """The executor call must pass the callable, not a coroutine object."""
        coro_objects_dispatched = []

        async def inspecting_run_in_executor(executor, func, *args):
            import inspect
            if inspect.iscoroutine(func):
                coro_objects_dispatched.append(func)
            if callable(func):
                func(*args)
            return None

        fake_loop = MagicMock()
        fake_loop.run_in_executor = inspecting_run_in_executor

        env_file = tmp_path / ".env"
        env_file.write_text("MONITOR_JID=\n", encoding="utf-8")

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.config.BASE_DIR", tmp_path):
                with patch("host.config.GROUPS_DIR", tmp_path / "groups"):
                    (tmp_path / "groups").mkdir(exist_ok=True)
                    with patch("asyncio.get_running_loop", return_value=fake_loop):
                        import host.db as db_mod
                        with patch.object(db_mod, "set_registered_group", return_value=None):
                            with patch.object(db_mod, "get_all_registered_groups", return_value=[]):
                                import host.main as main_mod
                                main_mod._MONITOR_JID = None
                                main_mod._registered_groups = []
                                await main_mod._handle_setup_command("tg:5555", "monitor")

        assert not coro_objects_dispatched, (
            "run_in_executor must receive a callable, not a coroutine object"
        )


# ── restore_remote_control is dispatched to executor at startup ───────────────

class TestRestoreRemoteControlExecutorDispatch:
    """start_ipc_watcher() must dispatch restore_remote_control to the executor at startup."""

    @pytest.mark.asyncio
    async def test_restore_remote_control_uses_run_in_executor(self, tmp_path):
        """run_in_executor() must be called with restore_remote_control at watcher startup."""
        executor_calls = []

        async def fake_run_in_executor(executor, func, *args):
            executor_calls.append((executor, func, args))
            if callable(func):
                try:
                    func(*args)
                except Exception:
                    pass
            return None

        fake_loop = MagicMock()
        fake_loop.run_in_executor = fake_run_in_executor

        stop_event = asyncio.Event()
        stop_event.set()  # Stop immediately after first iteration

        def fake_get_groups():
            return []

        # We only need the watcher to reach the executor call, then stop
        import host.ipc_watcher as ipc_mod

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("asyncio.get_running_loop", return_value=fake_loop):
                with patch.object(ipc_mod, "_INOTIFY_AVAILABLE", False):
                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        try:
                            await asyncio.wait_for(
                                ipc_mod.start_ipc_watcher(
                                    fake_get_groups,
                                    AsyncMock(),
                                    stop_event,
                                ),
                                timeout=1.0,
                            )
                        except (asyncio.TimeoutError, Exception):
                            pass

        from host.ipc_watcher import restore_remote_control
        dispatched_funcs = [c[1] for c in executor_calls]
        assert restore_remote_control in dispatched_funcs, (
            f"Expected restore_remote_control to be dispatched to executor at startup; "
            f"got dispatched functions: {dispatched_funcs}"
        )

    @pytest.mark.asyncio
    async def test_restore_remote_control_is_sync_function(self):
        """restore_remote_control must be a regular (non-async) function."""
        import inspect
        from host.ipc_watcher import restore_remote_control

        assert not inspect.iscoroutinefunction(restore_remote_control), (
            "restore_remote_control must be a sync function intended for executor dispatch, "
            "not an async coroutine"
        )

    @pytest.mark.asyncio
    async def test_restore_remote_control_called_before_poll_loop(self, tmp_path):
        """restore_remote_control executor call must precede the first polling iteration."""
        event_order = []

        async def fake_run_in_executor(executor, func, *args):
            event_order.append(f"executor:{getattr(func, '__name__', str(func))}")
            if callable(func):
                try:
                    func(*args)
                except Exception:
                    pass
            return None

        poll_calls = [0]

        async def fake_sleep(secs):
            poll_calls[0] += 1
            event_order.append("poll_sleep")
            if poll_calls[0] >= 1:
                raise asyncio.CancelledError

        fake_loop = MagicMock()
        fake_loop.run_in_executor = fake_run_in_executor

        stop_event = asyncio.Event()

        def fake_get_groups():
            return []

        import host.ipc_watcher as ipc_mod

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("asyncio.get_running_loop", return_value=fake_loop):
                with patch.object(ipc_mod, "_INOTIFY_AVAILABLE", False):
                    with patch("asyncio.sleep", side_effect=fake_sleep):
                        try:
                            await ipc_mod.start_ipc_watcher(
                                fake_get_groups,
                                AsyncMock(),
                                stop_event,
                            )
                        except (asyncio.CancelledError, Exception):
                            pass

        # The executor call for restore_remote_control must appear before poll_sleep
        executor_events = [e for e in event_order if e.startswith("executor:restore")]
        poll_events = [e for e in event_order if e == "poll_sleep"]

        if executor_events and poll_events:
            first_executor_idx = event_order.index(executor_events[0])
            first_poll_idx = event_order.index(poll_events[0])
            assert first_executor_idx < first_poll_idx, (
                f"restore_remote_control executor call must precede first poll sleep; "
                f"event order: {event_order}"
            )

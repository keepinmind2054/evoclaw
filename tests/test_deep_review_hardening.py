"""Regression tests for deep-review hardening fixes."""
import asyncio
import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestIpcSendFileHardening:
    def test_non_main_group_cannot_resolve_project_file(self, tmp_path):
        from host import config
        from host.ipc_watcher import _resolve_container_path

        with patch.object(config, "BASE_DIR", tmp_path):
            (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
            assert _resolve_container_path(
                "/workspace/project/.env",
                "group-a",
                is_main=False,
            ) is None

    def test_sensitive_project_file_denied_even_for_main_group(self, tmp_path):
        from host import config
        from host.ipc_watcher import _resolve_container_path

        with patch.object(config, "BASE_DIR", tmp_path):
            (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
            assert _resolve_container_path(
                "/workspace/project/.env",
                "main",
                is_main=True,
            ) is None

    def test_group_artifact_still_resolves(self, tmp_path):
        from host import config
        from host.ipc_watcher import _resolve_container_path

        group_dir = tmp_path / "groups" / "group-a"
        group_dir.mkdir(parents=True)
        artifact = group_dir / "report.txt"
        artifact.write_text("ok", encoding="utf-8")
        with patch.object(config, "GROUPS_DIR", tmp_path / "groups"):
            assert _resolve_container_path(
                "/workspace/group/report.txt",
                "group-a",
                is_main=False,
            ) == str(artifact)


class TestIpcMemoryResponseHardening:
    def test_memory_response_must_stay_in_group_results_dir(self, tmp_path):
        from host import config
        from host.ipc_watcher import _write_ipc_response

        with patch.object(config, "DATA_DIR", tmp_path):
            with pytest.raises(ValueError):
                _write_ipc_response(str(tmp_path / ".env"), {"ok": True}, "group-a")

    def test_memory_response_writes_inside_results_dir(self, tmp_path):
        from host import config
        from host.ipc_watcher import _write_ipc_response

        response = tmp_path / "ipc" / "group-a" / "results" / "reply.json"
        with patch.object(config, "DATA_DIR", tmp_path):
            _write_ipc_response(str(response), {"ok": True}, "group-a")
        assert json.loads(response.read_text(encoding="utf-8")) == {"ok": True}


class TestGroupQueueHardening:
    @pytest.mark.asyncio
    async def test_retry_count_reaches_max_retries_without_resetting_each_attempt(self, monkeypatch):
        import host.group_queue as group_queue

        monkeypatch.setattr(group_queue, "BASE_RETRY_SECS", 0.001)
        monkeypatch.setattr(group_queue, "MAX_RETRIES", 2)
        gq = group_queue.GroupQueue()
        attempts = 0

        async def always_fail(jid, meta):
            nonlocal attempts
            attempts += 1
            return False

        gq.set_process_messages_fn(always_fail)
        gq.enqueue_message_check("jid-retry")

        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            state = gq._get_group("jid-retry")
            if attempts >= 3 and state.retry_count == 0 and not state.active:
                break
            await asyncio.sleep(0.01)

        assert attempts == 3  # initial try + two retries; third failure trips max and resets
        state = gq._get_group("jid-retry")
        assert state.retry_count == 0

    def test_waiting_queue_cap_drops_task_without_permanent_pending_state(self, monkeypatch):
        import host.group_queue as group_queue

        monkeypatch.setattr(group_queue.config, "MAX_CONCURRENT_CONTAINERS", 1)
        monkeypatch.setattr(group_queue, "MAX_WAITING_GROUPS", 1)
        gq = group_queue.GroupQueue()
        gq._active_count = 1
        gq._waiting_groups.append("already-waiting")
        gq._waiting_set.add("already-waiting")

        async def noop():
            return None

        gq.enqueue_task("overflow-group", "task-1", noop)
        state = gq._get_group("overflow-group")
        assert not state.pending_tasks
        assert "task-1" not in state.pending_task_ids


class TestConfigEnvFallback:
    def test_dashboard_password_reads_project_env_file(self, tmp_path, monkeypatch):
        import host.env as env_mod
        import host.config as config_mod

        env_path = tmp_path / ".env"
        env_path.write_text("DASHBOARD_PASSWORD=from-dotenv\nDASHBOARD_USER=owner\n", encoding="utf-8")
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
        monkeypatch.delenv("DASHBOARD_USER", raising=False)
        monkeypatch.setattr(env_mod, "_ENV_PATH", env_path)

        reloaded = importlib.reload(config_mod)
        try:
            assert reloaded.DASHBOARD_PASSWORD == "from-dotenv"
            assert reloaded.DASHBOARD_USER == "owner"
        finally:
            importlib.reload(config_mod)

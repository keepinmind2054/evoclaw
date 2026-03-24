"""
Tests for IPC watcher safety fixes (Phase 24):

BUG-IPC-04: Empty JSON files must be deleted silently, not moved to errors/.
  An empty file results from an aborted partial write.  It is not a genuine
  parse error and must not pollute the errors/ directory.

BUG-IPC-06: _rc_save() must use the atomic tmp+rename pattern so
  restore_remote_control() never reads a partial JSON if the process
  crashes mid-write.

Both fixes were already present before Phase 24; these tests act as
regression guards to prevent accidental reversion.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── BUG-IPC-04: Empty file handling ───────────────────────────────────────────

class TestEmptyIpcFileHandling:
    """process_ipc_dir() must delete empty files silently, not move to errors/."""

    @pytest.mark.asyncio
    async def test_empty_file_is_deleted_not_moved_to_errors(self, tmp_path):
        """An empty .json file must be deleted and not appear in errors/."""
        # Set up the IPC directory structure
        group_folder = "test_group"
        ipc_dir = tmp_path / "ipc" / group_folder
        msg_dir = ipc_dir / "messages"
        msg_dir.mkdir(parents=True)
        errors_dir = ipc_dir / "errors"

        # Create an empty JSON file (simulating an aborted partial write)
        empty_file = msg_dir / "msg_001.json"
        empty_file.write_text("", encoding="utf-8")

        assert empty_file.exists(), "Precondition: empty file should exist"

        route_fn = AsyncMock()

        with patch("host.config.DATA_DIR", tmp_path):
            # We also need to patch db.get_all_registered_groups to avoid real DB calls
            with patch("host.db.get_all_registered_groups", return_value=[]):
                from host.ipc_watcher import process_ipc_dir
                await process_ipc_dir(group_folder, is_main=False, route_fn=route_fn)

        # Empty file must be deleted
        assert not empty_file.exists(), "Empty IPC file must be deleted"

        # errors/ directory must either not exist or be empty
        if errors_dir.exists():
            error_files = list(errors_dir.glob("*.json"))
            assert not error_files, (
                f"Empty file must not be moved to errors/; found: {error_files}"
            )

        # route_fn must not have been called for the empty file
        route_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only_file_is_deleted_not_moved_to_errors(self, tmp_path):
        """A file containing only whitespace is treated as empty and deleted silently."""
        group_folder = "ws_group"
        ipc_dir = tmp_path / "ipc" / group_folder
        msg_dir = ipc_dir / "messages"
        msg_dir.mkdir(parents=True)
        errors_dir = ipc_dir / "errors"

        ws_file = msg_dir / "msg_ws.json"
        ws_file.write_text("   \n\t  \n", encoding="utf-8")

        route_fn = AsyncMock()

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.db.get_all_registered_groups", return_value=[]):
                from host.ipc_watcher import process_ipc_dir
                await process_ipc_dir(group_folder, is_main=False, route_fn=route_fn)

        assert not ws_file.exists(), "Whitespace-only IPC file must be deleted"

        if errors_dir.exists():
            assert not list(errors_dir.glob("*.json")), (
                "Whitespace-only file must not be moved to errors/"
            )

    @pytest.mark.asyncio
    async def test_invalid_json_file_moved_to_errors(self, tmp_path):
        """A non-empty but invalid JSON file must be moved to errors/ (not deleted silently)."""
        group_folder = "invalid_group"
        ipc_dir = tmp_path / "ipc" / group_folder
        msg_dir = ipc_dir / "messages"
        msg_dir.mkdir(parents=True)

        bad_json_file = msg_dir / "bad_msg.json"
        bad_json_file.write_text("{not valid json!!!", encoding="utf-8")

        route_fn = AsyncMock()

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.db.get_all_registered_groups", return_value=[]):
                from host.ipc_watcher import process_ipc_dir
                await process_ipc_dir(group_folder, is_main=False, route_fn=route_fn)

        # The bad file should no longer be in messages/
        assert not bad_json_file.exists(), "Bad JSON file must be removed from messages/"

        # It should have been moved to errors/
        errors_dir = ipc_dir / "errors"
        error_files = list(errors_dir.glob("*.json")) if errors_dir.exists() else []
        assert error_files, "Bad JSON file must be moved to errors/ directory"

    @pytest.mark.asyncio
    async def test_valid_json_file_processed_and_deleted(self, tmp_path):
        """A valid JSON file is processed and then deleted (not retained)."""
        group_folder = "valid_group"
        ipc_dir = tmp_path / "ipc" / group_folder
        msg_dir = ipc_dir / "messages"
        msg_dir.mkdir(parents=True)

        payload = {"type": "message", "chatJid": "tg:12345", "text": "hello"}
        valid_file = msg_dir / "valid_msg.json"
        valid_file.write_text(json.dumps(payload), encoding="utf-8")

        route_fn = AsyncMock()

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.db.get_all_registered_groups", return_value=[]):
                # Patch _handle_ipc to avoid full IPC processing
                with patch("host.ipc_watcher._handle_ipc", new_callable=AsyncMock) as mock_handle:
                    from host.ipc_watcher import process_ipc_dir
                    await process_ipc_dir(group_folder, is_main=False, route_fn=route_fn)

        assert not valid_file.exists(), "Processed JSON file must be deleted after handling"
        mock_handle.assert_called_once()


# ── BUG-IPC-06: _rc_save() atomic write ────────────────────────────────────────

class TestRcSaveAtomicWrite:
    """_rc_save() must write via a .tmp file and rename, never directly."""

    def test_rc_save_creates_tmp_then_renames(self, tmp_path):
        """
        _rc_save() must:
          1. Write content to a .json.tmp file first.
          2. Rename the .tmp file to the final .json file.
        The final file must exist and the .tmp file must not remain.
        """
        with patch("host.config.DATA_DIR", tmp_path):
            from host.ipc_watcher import _rc_save, _rc_state_file

            _rc_save(pid=12345, url="http://example.com/rc", sender="user1", jid="tg:999")

            final_path = _rc_state_file()
            tmp_path_file = final_path.with_suffix(".json.tmp")

            assert final_path.exists(), "_rc_save must create the final .json file"
            assert not tmp_path_file.exists(), (
                "_rc_save must not leave a .json.tmp file behind after rename"
            )

    def test_rc_save_output_is_valid_json(self, tmp_path):
        """The file written by _rc_save() must contain valid JSON."""
        with patch("host.config.DATA_DIR", tmp_path):
            from host.ipc_watcher import _rc_save, _rc_state_file

            _rc_save(pid=99, url="http://localhost/rc", sender="admin", jid="tg:777")

            data = json.loads(_rc_state_file().read_text(encoding="utf-8"))
            assert data["pid"] == 99
            assert data["url"] == "http://localhost/rc"
            assert data["sender"] == "admin"
            assert data["jid"] == "tg:777"
            assert "startedAt" in data

    def test_rc_save_overwrites_existing_file(self, tmp_path):
        """Calling _rc_save() twice overwrites the first file cleanly."""
        with patch("host.config.DATA_DIR", tmp_path):
            from host.ipc_watcher import _rc_save, _rc_state_file

            _rc_save(pid=1, url="http://old.example.com", sender="s1", jid="tg:1")
            _rc_save(pid=2, url="http://new.example.com", sender="s2", jid="tg:2")

            data = json.loads(_rc_state_file().read_text(encoding="utf-8"))
            assert data["pid"] == 2
            assert data["url"] == "http://new.example.com"

    def test_rc_save_atomic_pattern_via_rename_spy(self, tmp_path):
        """
        Verify the atomic pattern by spying on Path.rename.
        The .tmp file must be renamed to the final path.
        """
        rename_calls = []
        _orig_rename = Path.rename

        def _spy_rename(self_path, target):
            rename_calls.append((Path(self_path).name, Path(str(target)).name))
            return _orig_rename(self_path, target)

        with patch("host.config.DATA_DIR", tmp_path):
            with patch.object(Path, "rename", _spy_rename):
                from host.ipc_watcher import _rc_save
                _rc_save(pid=7, url="http://rc.test", sender="spy", jid="tg:42")

        # Verify that a rename from the .tmp file to the final .json file occurred.
        # We compare basenames only so this is independent of the tmp_path root.
        tmp_to_final = [
            (src, dst) for src, dst in rename_calls
            if src == "remote-control.json.tmp" and dst == "remote-control.json"
        ]
        assert tmp_to_final, (
            f"Expected a rename from 'remote-control.json.tmp' to 'remote-control.json'; "
            f"actual rename calls (basenames): {rename_calls}"
        )

"""
Tests for Phase 28b IPC backpressure fixes.

BUG-IPC-BACKPRESSURE: process_ipc_dir() must cap the number of files processed
per cycle to _IPC_MAX_FILES_PER_CYCLE (default 100) so a runaway container
cannot monopolise the event loop.  Remaining files are left on disk for the next
cycle.  A flood warning is emitted when pending files exceed
_IPC_FLOOD_WARN_THRESHOLD (500).

Tests verify:
  - With 200 files, at most 100 are processed per call
  - Remaining files are not lost (still on disk)
  - Flood warning is logged when 500+ files are pending
  - Normal operation with <100 files processes all files correctly
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_json_files(directory: Path, count: int) -> list[Path]:
    """Create `count` valid JSON IPC files in `directory` and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for i in range(count):
        f = directory / f"msg_{i:06d}.json"
        f.write_text(
            json.dumps({"type": "message", "chatJid": f"tg:{i}", "text": f"hello {i}"}),
            encoding="utf-8",
        )
        files.append(f)
    return files


# ── Cap enforcement: 200 files → at most 100 processed ────────────────────────

class TestIpcBackpressureCap:
    """process_ipc_dir() must process at most _IPC_MAX_FILES_PER_CYCLE per call."""

    @pytest.mark.asyncio
    async def test_200_files_at_most_100_processed(self, tmp_path):
        """With 200 pending files, a single process_ipc_dir() call processes ≤100."""
        group_folder = "flood_group"
        msg_dir = tmp_path / "ipc" / group_folder / "messages"
        _make_json_files(msg_dir, 200)

        route_fn = AsyncMock()

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.db.get_all_registered_groups", return_value=[]):
                with patch("host.ipc_watcher._handle_ipc", new_callable=AsyncMock):
                    # Ensure the cap is 100 for this test
                    with patch("host.ipc_watcher._IPC_MAX_FILES_PER_CYCLE", 100):
                        from host.ipc_watcher import process_ipc_dir
                        await process_ipc_dir(group_folder, is_main=False, route_fn=route_fn)

        remaining = list(msg_dir.glob("*.json"))
        processed = 200 - len(remaining)
        assert processed <= 100, (
            f"Expected at most 100 files processed, but {processed} were processed "
            f"({len(remaining)} remaining)."
        )

    @pytest.mark.asyncio
    async def test_200_files_exactly_100_processed(self, tmp_path):
        """With 200 pending files and cap=100, exactly 100 files are processed."""
        group_folder = "exact_cap_group"
        msg_dir = tmp_path / "ipc" / group_folder / "messages"
        _make_json_files(msg_dir, 200)

        handled_files = []

        async def capture_handle(data, group_folder, is_main, route_fn):
            pass

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.db.get_all_registered_groups", return_value=[]):
                with patch("host.ipc_watcher._handle_ipc", new_callable=AsyncMock) as mock_handle:
                    with patch("host.ipc_watcher._IPC_MAX_FILES_PER_CYCLE", 100):
                        from host.ipc_watcher import process_ipc_dir
                        await process_ipc_dir(group_folder, is_main=False, route_fn=route_fn)
                    call_count = mock_handle.call_count

        # Exactly 100 should have been dispatched to _handle_ipc
        assert call_count == 100, (
            f"Expected exactly 100 _handle_ipc calls, got {call_count}"
        )

    @pytest.mark.asyncio
    async def test_remaining_files_not_lost(self, tmp_path):
        """Files beyond the cap must remain on disk (not deleted or moved)."""
        group_folder = "remaining_group"
        msg_dir = tmp_path / "ipc" / group_folder / "messages"
        _make_json_files(msg_dir, 200)

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.db.get_all_registered_groups", return_value=[]):
                with patch("host.ipc_watcher._handle_ipc", new_callable=AsyncMock):
                    with patch("host.ipc_watcher._IPC_MAX_FILES_PER_CYCLE", 100):
                        from host.ipc_watcher import process_ipc_dir
                        await process_ipc_dir(group_folder, is_main=False, route_fn=AsyncMock())

        remaining = list(msg_dir.glob("*.json"))
        assert len(remaining) >= 100, (
            f"Expected at least 100 remaining files, found {len(remaining)}"
        )

    @pytest.mark.asyncio
    async def test_remaining_files_have_valid_json(self, tmp_path):
        """Remaining (unprocessed) files must still contain valid JSON."""
        group_folder = "valid_remaining_group"
        msg_dir = tmp_path / "ipc" / group_folder / "messages"
        _make_json_files(msg_dir, 150)

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.db.get_all_registered_groups", return_value=[]):
                with patch("host.ipc_watcher._handle_ipc", new_callable=AsyncMock):
                    with patch("host.ipc_watcher._IPC_MAX_FILES_PER_CYCLE", 100):
                        from host.ipc_watcher import process_ipc_dir
                        await process_ipc_dir(group_folder, is_main=False, route_fn=AsyncMock())

        remaining = list(msg_dir.glob("*.json"))
        for f in remaining:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                assert "type" in data
            except (json.JSONDecodeError, KeyError) as exc:
                pytest.fail(f"Remaining file {f.name} contains invalid JSON: {exc}")


# ── Flood warning ──────────────────────────────────────────────────────────────

class TestIpcFloodWarning:
    """A flood warning must be logged when pending files exceed the threshold."""

    @pytest.mark.asyncio
    async def test_flood_warning_logged_for_500_plus_files(self, tmp_path, caplog):
        """When ≥500 files are pending, a WARNING containing 'backpressure' is logged."""
        group_folder = "flood_warn_group"
        msg_dir = tmp_path / "ipc" / group_folder / "messages"
        _make_json_files(msg_dir, 510)

        with caplog.at_level(logging.WARNING, logger="host.ipc_watcher"):
            with patch("host.config.DATA_DIR", tmp_path):
                with patch("host.db.get_all_registered_groups", return_value=[]):
                    with patch("host.ipc_watcher._handle_ipc", new_callable=AsyncMock):
                        with patch("host.ipc_watcher._IPC_MAX_FILES_PER_CYCLE", 100):
                            from host.ipc_watcher import process_ipc_dir
                            await process_ipc_dir(group_folder, is_main=False, route_fn=AsyncMock())

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("backpressure" in msg.lower() for msg in warning_messages), (
            f"Expected a 'backpressure' warning; got: {warning_messages}"
        )

    @pytest.mark.asyncio
    async def test_flood_warning_not_logged_below_threshold(self, tmp_path, caplog):
        """When <500 files are pending, no flood warning is logged."""
        group_folder = "no_warn_group"
        msg_dir = tmp_path / "ipc" / group_folder / "messages"
        _make_json_files(msg_dir, 50)

        with caplog.at_level(logging.WARNING, logger="host.ipc_watcher"):
            with patch("host.config.DATA_DIR", tmp_path):
                with patch("host.db.get_all_registered_groups", return_value=[]):
                    with patch("host.ipc_watcher._handle_ipc", new_callable=AsyncMock):
                        with patch("host.ipc_watcher._IPC_MAX_FILES_PER_CYCLE", 100):
                            from host.ipc_watcher import process_ipc_dir
                            await process_ipc_dir(group_folder, is_main=False, route_fn=AsyncMock())

        backpressure_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "backpressure" in r.message.lower()
        ]
        assert not backpressure_warnings, (
            f"Unexpected backpressure warning with only 50 files: {backpressure_warnings}"
        )


# ── Normal operation ───────────────────────────────────────────────────────────

class TestIpcNormalOperation:
    """With fewer than _IPC_MAX_FILES_PER_CYCLE files, all are processed normally."""

    @pytest.mark.asyncio
    async def test_all_files_processed_when_below_cap(self, tmp_path):
        """With 10 files and cap=100, all 10 are processed."""
        group_folder = "normal_group"
        msg_dir = tmp_path / "ipc" / group_folder / "messages"
        _make_json_files(msg_dir, 10)

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.db.get_all_registered_groups", return_value=[]):
                with patch("host.ipc_watcher._handle_ipc", new_callable=AsyncMock) as mock_handle:
                    with patch("host.ipc_watcher._IPC_MAX_FILES_PER_CYCLE", 100):
                        from host.ipc_watcher import process_ipc_dir
                        await process_ipc_dir(group_folder, is_main=False, route_fn=AsyncMock())

        assert mock_handle.call_count == 10, (
            f"Expected 10 calls to _handle_ipc, got {mock_handle.call_count}"
        )

    @pytest.mark.asyncio
    async def test_empty_ipc_dir_no_errors(self, tmp_path):
        """process_ipc_dir() runs without error when the messages dir is empty."""
        group_folder = "empty_group"
        msg_dir = tmp_path / "ipc" / group_folder / "messages"
        msg_dir.mkdir(parents=True)

        with patch("host.config.DATA_DIR", tmp_path):
            with patch("host.db.get_all_registered_groups", return_value=[]):
                from host.ipc_watcher import process_ipc_dir
                # Should complete without raising
                await process_ipc_dir(group_folder, is_main=False, route_fn=AsyncMock())

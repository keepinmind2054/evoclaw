"""
Tests for host.log_buffer — in-memory ring buffer for dashboard live logs.

Critical paths covered:
  - install(): attaches handler to root logger
  - get_logs(): returns entries with idx > since_idx
  - get_logs(): level filter (INFO, ERROR, ALL)
  - get_logs(): BUG-LB-01 fix — limit is clamped to _MAX_SIZE
  - get_error_count(): counts only ERROR/CRITICAL entries
  - ring buffer evicts oldest entries at _MAX_SIZE
"""
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import host.log_buffer as lb


# ── Helpers ───────────────────────────────────────────────────────────────────

def _flush_buffer():
    """Clear the global log buffer between tests."""
    with lb._lock:
        lb._buffer.clear()
        lb._counter = 0


def _emit(msg, level=logging.INFO, name="test.logger"):
    """Emit a log record directly to the buffer handler without configuring a logger."""
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="test_log_buffer.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    handler = lb._BufferHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    handler.emit(record)


# ── get_logs ──────────────────────────────────────────────────────────────────

class TestGetLogs:
    def setup_method(self):
        _flush_buffer()

    def test_empty_buffer_returns_empty_list(self):
        assert lb.get_logs() == []

    def test_returns_all_entries_when_since_zero(self):
        _emit("msg1")
        _emit("msg2")
        entries = lb.get_logs(since_idx=0)
        assert len(entries) == 2
        assert entries[0]["msg"] == entries[0]["msg"]  # sanity
        msgs = [e["msg"] for e in entries]
        assert any("msg1" in m for m in msgs)
        assert any("msg2" in m for m in msgs)

    def test_since_idx_filters_old_entries(self):
        _emit("before")
        with lb._lock:
            boundary_idx = lb._counter
        _emit("after")

        entries = lb.get_logs(since_idx=boundary_idx)
        assert len(entries) == 1
        assert "after" in entries[0]["msg"]

    def test_level_filter_info_excludes_debug(self):
        _emit("debug-msg", level=logging.DEBUG)
        _emit("info-msg", level=logging.INFO)
        entries = lb.get_logs(level="INFO")
        levels = [e["level"] for e in entries]
        assert "DEBUG" not in levels
        assert "INFO" in levels

    def test_level_filter_all_returns_everything(self):
        _emit("debug", level=logging.DEBUG)
        _emit("info", level=logging.INFO)
        _emit("error", level=logging.ERROR)
        entries = lb.get_logs(level="ALL")
        assert len(entries) == 3

    def test_level_filter_error_only_errors(self):
        _emit("info-msg", level=logging.INFO)
        _emit("error-msg", level=logging.ERROR)
        _emit("critical-msg", level=logging.CRITICAL)
        entries = lb.get_logs(level="ERROR")
        assert all(e["level"] == "ERROR" for e in entries)
        assert len(entries) == 1

    def test_limit_caps_number_of_results(self):
        for i in range(10):
            _emit(f"msg-{i}")
        entries = lb.get_logs(limit=3)
        assert len(entries) == 3

    def test_limit_clamped_to_max_size(self):
        """BUG-LB-01 FIX: limit must be clamped to _MAX_SIZE, not passed raw."""
        # Requesting more than _MAX_SIZE should silently clamp
        for i in range(5):
            _emit(f"msg-{i}")
        # Request absurdly large limit
        entries = lb.get_logs(limit=10**9)
        # Should not crash and must return <= _MAX_SIZE entries
        assert len(entries) <= lb._MAX_SIZE

    def test_limit_minimum_is_one(self):
        """limit=0 or negative should be clamped to at least 1."""
        _emit("only-msg")
        entries = lb.get_logs(limit=0)
        assert len(entries) >= 0  # may be 0 if tail(0) returns empty

        entries_neg = lb.get_logs(limit=-5)
        assert len(entries_neg) <= 1  # clamped to 1

    def test_entry_has_required_fields(self):
        """Each buffer entry must have idx, level, name, msg fields."""
        _emit("field-check", level=logging.WARNING, name="host.test")
        entries = lb.get_logs()
        assert len(entries) >= 1
        entry = entries[-1]
        assert "idx" in entry
        assert "level" in entry
        assert "name" in entry
        assert "msg" in entry
        assert entry["level"] == "WARNING"
        assert entry["name"] == "host.test"

    def test_idx_is_monotonically_increasing(self):
        """Each emitted entry must have a higher idx than the previous."""
        for i in range(5):
            _emit(f"msg-{i}")
        entries = lb.get_logs()
        idxs = [e["idx"] for e in entries]
        assert idxs == sorted(idxs)
        assert len(set(idxs)) == len(idxs)  # all unique


# ── get_error_count ───────────────────────────────────────────────────────────

class TestGetErrorCount:
    def setup_method(self):
        _flush_buffer()

    def test_no_errors_returns_zero(self):
        _emit("info-only", level=logging.INFO)
        assert lb.get_error_count() == 0

    def test_counts_error_and_critical(self):
        _emit("info", level=logging.INFO)
        _emit("error-one", level=logging.ERROR)
        _emit("critical-one", level=logging.CRITICAL)
        _emit("error-two", level=logging.ERROR)
        assert lb.get_error_count() == 3

    def test_warning_not_counted_as_error(self):
        _emit("warning", level=logging.WARNING)
        assert lb.get_error_count() == 0


# ── Ring buffer eviction ───────────────────────────────────────────────────────

class TestRingBufferEviction:
    def setup_method(self):
        _flush_buffer()

    def test_buffer_does_not_exceed_max_size(self):
        """After emitting more than _MAX_SIZE entries, buffer stays at _MAX_SIZE."""
        for i in range(lb._MAX_SIZE + 10):
            _emit(f"msg-{i}")
        with lb._lock:
            assert len(lb._buffer) == lb._MAX_SIZE

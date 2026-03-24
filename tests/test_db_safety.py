"""
Tests for database safety fixes (Phase 24):

BUG-DB-02: get_container_logs(limit=0) must return at least 1 result.
  The fix clamps limit to max(1, limit) so a caller passing 0 always
  gets at least one row, never a silently empty list.

BUG-DB-01: init_database() must be safe to call concurrently from multiple
  threads.  The fix wraps the global _db assignment in _db_lock so two
  simultaneous initializations cannot race on the global connection pointer.
"""
import os
import sys
import threading
import time
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _init_db(db_path: Path):
    """Initialize host.db at the given path and return the module."""
    import importlib
    import host.db as db_mod
    # Reset module-level state between tests
    db_mod._db = None
    db_mod.init_database(db_path)
    return db_mod


def _insert_container_log(db_mod, run_id: str, jid: str = "tg:test"):
    """Insert a minimal container_logs row for testing."""
    import time as _time
    with db_mod._db_lock:
        db = db_mod.get_db()
        db.execute(
            "INSERT OR IGNORE INTO container_logs "
            "(run_id, jid, folder, container_name, started_at, status) "
            "VALUES (?, ?, '', '', ?, 'finished')",
            (run_id, jid, _time.time()),
        )
        db.commit()


# ── BUG-DB-02: get_container_logs(limit=0) ────────────────────────────────────

class TestGetContainerLogsLimitZero:
    """get_container_logs(limit=0) must clamp to 1 and return at least 1 result."""

    def test_limit_zero_returns_at_least_one_row(self, tmp_path):
        """BUG-DB-02: limit=0 must not return an empty list when rows exist."""
        db_path = tmp_path / "test_limit0.db"
        db_mod = _init_db(db_path)

        # Insert a row so the table is not empty
        _insert_container_log(db_mod, run_id="run-abc-001")

        rows = db_mod.get_container_logs(limit=0)
        assert len(rows) >= 1, (
            "get_container_logs(limit=0) must return at least 1 row (limit clamped to 1)"
        )

    def test_limit_zero_clamped_to_one_matches_limit_one(self, tmp_path):
        """limit=0 and limit=1 must produce the same result."""
        db_path = tmp_path / "test_clamp.db"
        db_mod = _init_db(db_path)

        _insert_container_log(db_mod, run_id="run-clamp-001")
        _insert_container_log(db_mod, run_id="run-clamp-002")

        rows_zero = db_mod.get_container_logs(limit=0)
        rows_one = db_mod.get_container_logs(limit=1)

        assert len(rows_zero) == len(rows_one) == 1, (
            f"limit=0 ({len(rows_zero)} rows) and limit=1 ({len(rows_one)} rows) "
            "should both return exactly 1 row"
        )

    def test_limit_negative_also_clamped(self, tmp_path):
        """A negative limit must also be clamped to at least 1."""
        db_path = tmp_path / "test_neg.db"
        db_mod = _init_db(db_path)

        _insert_container_log(db_mod, run_id="run-neg-001")

        rows = db_mod.get_container_logs(limit=-5)
        assert len(rows) >= 1, (
            "get_container_logs(limit=-5) must return at least 1 row (limit clamped)"
        )

    def test_limit_positive_respected(self, tmp_path):
        """A positive limit value must be respected normally."""
        db_path = tmp_path / "test_pos.db"
        db_mod = _init_db(db_path)

        for i in range(5):
            _insert_container_log(db_mod, run_id=f"run-pos-{i:03d}")

        rows = db_mod.get_container_logs(limit=3)
        assert len(rows) == 3, f"Expected 3 rows for limit=3, got {len(rows)}"

    def test_limit_zero_empty_table_returns_empty_list(self, tmp_path):
        """With an empty table, limit=0 returns an empty list (no rows to clamp to)."""
        db_path = tmp_path / "test_empty.db"
        db_mod = _init_db(db_path)

        rows = db_mod.get_container_logs(limit=0)
        # Table is empty — clamping to 1 still returns 0 rows (correct behaviour)
        assert isinstance(rows, list)
        assert len(rows) == 0, (
            "Empty table with limit=0 should return empty list, not raise"
        )

    def test_limit_zero_with_jid_filter(self, tmp_path):
        """limit=0 with a jid filter still returns at least 1 matching row."""
        db_path = tmp_path / "test_jid.db"
        db_mod = _init_db(db_path)

        _insert_container_log(db_mod, run_id="run-jid-001", jid="tg:filter_jid")
        _insert_container_log(db_mod, run_id="run-jid-002", jid="tg:other_jid")

        rows = db_mod.get_container_logs(jid="tg:filter_jid", limit=0)
        assert len(rows) >= 1
        assert all(r["jid"] == "tg:filter_jid" for r in rows)


# ── BUG-DB-01: init_database() thread safety ──────────────────────────────────

class TestInitDatabaseConcurrency:
    """init_database() must not raise when called from two threads simultaneously."""

    def test_concurrent_init_does_not_raise(self, tmp_path):
        """Two threads calling init_database() simultaneously must not crash."""
        import importlib
        import host.db as db_mod

        db_path = tmp_path / "concurrent_init.db"
        errors: list[Exception] = []

        # Reset module state
        db_mod._db = None

        def _init():
            try:
                db_mod.init_database(db_path)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=_init)
        t2 = threading.Thread(target=_init)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Concurrent init_database() raised: {errors}"

    def test_concurrent_init_leaves_db_usable(self, tmp_path):
        """After concurrent initialization, the DB must be usable."""
        import host.db as db_mod

        db_path = tmp_path / "usable_after_concurrent.db"
        db_mod._db = None

        barrier = threading.Barrier(2)

        def _init_sync():
            barrier.wait()  # Both threads start at the same moment
            db_mod.init_database(db_path)

        t1 = threading.Thread(target=_init_sync)
        t2 = threading.Thread(target=_init_sync)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # After both threads finish, _db must be non-None and usable
        assert db_mod._db is not None, "DB must be initialized after concurrent calls"

        # Basic query must work
        with db_mod._db_lock:
            rows = db_mod._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = {r[0] for r in rows}
        assert "container_logs" in table_names, "container_logs table must exist after init"

    def test_init_database_idempotent_sequential(self, tmp_path):
        """Calling init_database() twice sequentially must not raise."""
        import host.db as db_mod

        db_path = tmp_path / "idempotent.db"
        db_mod._db = None

        db_mod.init_database(db_path)
        db_mod.init_database(db_path)  # Second call must not raise

        assert db_mod._db is not None

    def test_init_database_replaces_previous_connection(self, tmp_path):
        """Re-initializing replaces the old connection with a fresh one."""
        import host.db as db_mod

        db_path1 = tmp_path / "db1.db"
        db_path2 = tmp_path / "db2.db"
        db_mod._db = None

        db_mod.init_database(db_path1)
        conn1 = db_mod._db

        db_mod.init_database(db_path2)
        conn2 = db_mod._db

        assert conn2 is not conn1, "Re-initialization must create a new connection object"

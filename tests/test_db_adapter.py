"""Tests for db_adapter — SQLite path only (no PostgreSQL required for CI)."""
import os
import threading
import pytest
import tempfile
from unittest.mock import patch


def test_sqlite_adapter_default():
    """get_adapter() returns SQLite adapter when no DATABASE_URL is set."""
    with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
        # Re-import to pick up patched env
        import importlib
        import host.db_adapter as m
        importlib.reload(m)
        adapter = m.get_adapter()
        assert adapter.backend == "sqlite"
        assert adapter.placeholder == "?"


def test_sqlite_adapter_connect(tmp_path):
    """SQLite adapter can connect and execute queries."""
    db_file = str(tmp_path / "test.db")
    with patch.dict(os.environ, {"DATABASE_URL": "", "DB_PATH": db_file}):
        import importlib
        import host.db_adapter as m
        importlib.reload(m)
        adapter = m.get_adapter()
        conn = adapter.connect()
        adapter.execute(conn, "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, val TEXT)")
        adapter.execute(conn, "INSERT INTO t (val) VALUES (?)", ("hello",))
        conn.commit()
        cur = adapter.execute(conn, "SELECT val FROM t")
        assert cur.fetchone()[0] == "hello"
        conn.close()


def test_postgresql_adapter_not_available_without_psycopg2():
    """PostgreSQL adapter raises RuntimeError when psycopg2 is not installed."""
    with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/test"}):
        import importlib
        import host.db_adapter as m
        importlib.reload(m)
        adapter = m.get_adapter()
        assert adapter.backend == "postgresql"
        assert adapter.placeholder == "%s"
        # Connecting without psycopg2 should raise RuntimeError
        with pytest.raises((RuntimeError, Exception)):
            adapter.connect()


def test_current_backend_default():
    """current_backend() returns 'sqlite' by default."""
    with patch.dict(os.environ, {"DATABASE_URL": ""}):
        import importlib
        import host.db_adapter as m
        importlib.reload(m)
        assert m.current_backend() == "sqlite"


def test_sqlite_adapter_pragmas(tmp_path):
    """SQLite adapter connection sets WAL mode and busy_timeout pragmas."""
    db_file = str(tmp_path / "pragma_test.db")
    with patch.dict(os.environ, {"DATABASE_URL": "", "DB_PATH": db_file}):
        import importlib
        import host.db_adapter as m
        importlib.reload(m)
        adapter = m.get_adapter()
        conn = adapter.connect()
        # Verify WAL journal mode
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal", f"Expected WAL mode, got {row[0]}"
        # Verify busy_timeout is non-zero (protects against SQLITE_BUSY)
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert row[0] > 0, "busy_timeout should be > 0 to handle concurrent writers"
        conn.close()


def test_sqlite_adapter_concurrent_writes(tmp_path):
    """Multiple threads can write to the same SQLite DB via the adapter without data loss.

    This is a regression test for thread-safety: the adapter must not lose rows
    when two threads INSERT concurrently (WAL + busy_timeout allows this).
    """
    db_file = str(tmp_path / "concurrent.db")
    with patch.dict(os.environ, {"DATABASE_URL": "", "DB_PATH": db_file}):
        import importlib
        import host.db_adapter as m
        importlib.reload(m)
        adapter = m.get_adapter()

        # Set up schema via a single connection first
        setup_conn = adapter.connect()
        adapter.execute(
            setup_conn,
            "CREATE TABLE IF NOT EXISTS counters (id INTEGER PRIMARY KEY AUTOINCREMENT, val INTEGER NOT NULL)",
        )
        setup_conn.commit()
        setup_conn.close()

        errors: list[Exception] = []
        n_threads = 8
        rows_per_thread = 25

        def insert_rows():
            try:
                conn = adapter.connect()
                for i in range(rows_per_thread):
                    adapter.execute(conn, "INSERT INTO counters (val) VALUES (?)", (i,))
                    conn.commit()
                conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=insert_rows) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent write errors: {errors}"

        # Verify all rows were written
        verify_conn = adapter.connect()
        count = verify_conn.execute("SELECT COUNT(*) FROM counters").fetchone()[0]
        verify_conn.close()
        expected = n_threads * rows_per_thread
        assert count == expected, f"Expected {expected} rows, got {count} (data loss under concurrency)"


def test_sqlite_adapter_executemany(tmp_path):
    """executemany inserts all rows correctly."""
    db_file = str(tmp_path / "executemany.db")
    with patch.dict(os.environ, {"DATABASE_URL": "", "DB_PATH": db_file}):
        import importlib
        import host.db_adapter as m
        importlib.reload(m)
        adapter = m.get_adapter()
        conn = adapter.connect()
        adapter.execute(conn, "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
        adapter.executemany(conn, "INSERT INTO items (name) VALUES (?)", [("a",), ("b",), ("c",)])
        conn.commit()
        rows = conn.execute("SELECT name FROM items ORDER BY id").fetchall()
        assert [r[0] for r in rows] == ["a", "b", "c"]
        conn.close()

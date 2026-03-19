"""Tests for db_adapter — SQLite path only (no PostgreSQL required for CI)."""
import os
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

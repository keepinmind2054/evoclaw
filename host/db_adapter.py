"""Database adapter — supports SQLite (default) and PostgreSQL (DATABASE_URL set).

Usage:
    from host.db_adapter import get_adapter
    db = get_adapter()
    conn = db.connect()  # returns a DBAPI2-compatible connection
    db.execute(conn, "SELECT ...", params)
    db.executemany(conn, "INSERT ...", rows)
    db.placeholder  # "?" for SQLite, "%s" for PostgreSQL
"""
import os
import logging
from typing import Any

log = logging.getLogger(__name__)

_DATABASE_URL = os.environ.get("DATABASE_URL", "")


class _SqliteAdapter:
    """SQLite adapter using stdlib sqlite3."""
    placeholder = "?"
    backend = "sqlite"

    def connect(self):
        import sqlite3
        db_path = os.environ.get("DB_PATH", "data/messages.db")
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Mirror the pragmas used by init_database() in db.py so that any
        # secondary connection opened through this adapter also benefits from
        # WAL mode, reduced lock contention, and a retry window on SQLITE_BUSY.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def execute(self, conn, sql: str, params=()) -> Any:
        return conn.execute(sql, params)

    def executemany(self, conn, sql: str, params_seq) -> Any:
        return conn.executemany(sql, params_seq)


class _PostgresAdapter:
    """PostgreSQL adapter using psycopg2."""
    placeholder = "%s"
    backend = "postgresql"

    def __init__(self, url: str):
        self._url = url

    def connect(self):
        try:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(self._url)
            conn.autocommit = False
            return conn
        except ImportError:
            raise RuntimeError(
                "psycopg2 not installed. Run: pip install psycopg2-binary"
            )

    def execute(self, conn, sql: str, params=()) -> Any:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, conn, sql: str, params_seq) -> Any:
        cur = conn.cursor()
        cur.executemany(sql, params_seq)
        return cur


def get_adapter():
    """Return the appropriate DB adapter based on DATABASE_URL env var."""
    if _DATABASE_URL.startswith("postgresql://") or _DATABASE_URL.startswith("postgres://"):
        log.info("DB adapter: PostgreSQL (%s)", _DATABASE_URL.split("@")[-1])
        return _PostgresAdapter(_DATABASE_URL)
    log.info("DB adapter: SQLite (default)")
    return _SqliteAdapter()


# Module-level singleton
_adapter = get_adapter()


def current_backend() -> str:
    """Return 'sqlite' or 'postgresql'."""
    return _adapter.backend

"""Migrate data from SQLite to PostgreSQL.

Usage:
    DATABASE_URL=postgresql://... python -m host.migrations.sqlite_to_pg

SQLITE_PATH defaults to ``config.STORE_DIR / "messages.db"`` (the same path the
live host process uses). Override by setting ``SQLITE_PATH`` explicitly.
"""
import os
import re
import sqlite3
import sys
import logging

from host import config

log = logging.getLogger(__name__)

# Allowlist of table names that may be migrated.  Using an allowlist rather than
# just a regex prevents a maliciously-crafted SQLite file from injecting
# arbitrary SQL via a table name embedded in the f-string queries below.
_ALLOWED_TABLES = frozenset({
    "chats",
    "messages",
    "scheduled_tasks",
    "task_run_logs",
    "router_state",
    "sessions",
    "registered_groups",
    "container_logs",
    "evolution_runs",
    "group_genome",
    "immune_threats",
    "evolution_log",
    "dev_sessions",
    "dev_events",
    "group_hot_memory",
    "group_warm_logs",
    "group_cold_memory",
    "group_memory_sync",
    "shared_memories",
    "vec_memories",
})

_SAFE_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def _safe_identifier(name: str) -> str:
    """Raise ValueError if *name* is not a safe SQL identifier."""
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier rejected: {name!r}")
    return name


def migrate():
    sqlite_path = os.environ.get("SQLITE_PATH") or str(config.STORE_DIR / "messages.db")
    pg_url = os.environ.get("DATABASE_URL", "")

    if not pg_url:
        print("Error: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    try:
        import psycopg2
    except ImportError:
        print("Error: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = psycopg2.connect(pg_url)
    dst.autocommit = False

    # Discover all tables in SQLite, filtering to the allowlist only.
    all_tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    tables = [t for t in all_tables if t in _ALLOWED_TABLES]
    skipped = set(all_tables) - set(tables)
    if skipped:
        log.warning("Skipping non-allowlisted tables: %s", sorted(skipped))
    log.info("Tables to migrate: %s", tables)

    for table in tables:
        # Validate table name as a safe identifier (belt-and-suspenders after
        # the allowlist check above).
        safe_table = _safe_identifier(table)

        rows = src.execute(f"SELECT * FROM {safe_table}").fetchall()
        if not rows:
            log.info("Skipping empty table: %s", safe_table)
            continue

        cols = list(rows[0].keys())
        # Validate every column name as a safe identifier before embedding in SQL.
        safe_cols = [_safe_identifier(c) for c in cols]
        col_list = ", ".join(safe_cols)
        placeholders = ", ".join(["%s"] * len(safe_cols))

        try:
            with dst.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {safe_table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    [tuple(row) for row in rows],
                )
            dst.commit()
            log.info("Migrated %d rows from table '%s'", len(rows), safe_table)
        except Exception as exc:
            dst.rollback()
            log.error("Failed to migrate table '%s': %s — rolling back this table", safe_table, exc)

    src.close()
    dst.close()
    log.info("Migration complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate()

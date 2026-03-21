#!/usr/bin/env python3
"""Run database migrations for EvoClaw.

Each migration is identified by an integer version number.  Migrations are
applied in ascending order and recorded in the ``schema_migrations`` table so
that re-running this script is safe (already-applied migrations are skipped).
"""
import sys
import sqlite3
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from host import db, config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Migration registry
# Each entry: (version: int, description: str, sql: str | None)
# sql=None means the migration is handled by init_database() (baseline schema).
# ---------------------------------------------------------------------------
MIGRATIONS: list[tuple[int, str, str | None]] = [
    (
        1,
        "Baseline schema — created by init_database()",
        None,  # init_database() already applied this via CREATE TABLE IF NOT EXISTS
    ),
    (
        2,
        "Add schema_migrations, UNIQUE(sender_jid, pattern_hash) on immune_threats, "
        "idx_immune_sender_hash, idx_task_run_logs_task_id, idx_container_logs_status, "
        "idx_dev_sessions_status",
        # These DDL statements are idempotent (IF NOT EXISTS / ignored if already present).
        """
        CREATE INDEX IF NOT EXISTS idx_immune_sender_hash
            ON immune_threats(sender_jid, pattern_hash);
        CREATE INDEX IF NOT EXISTS idx_task_run_logs_task_id
            ON task_run_logs(task_id);
        CREATE INDEX IF NOT EXISTS idx_container_logs_status
            ON container_logs(status);
        CREATE INDEX IF NOT EXISTS idx_dev_sessions_status
            ON dev_sessions(status);
        """,
    ),
]


def _get_applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Return the set of migration versions already recorded in schema_migrations."""
    try:
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        # Table doesn't exist yet (very old DB without version tracking).
        return set()


def _record_migration(conn: sqlite3.Connection, version: int, description: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, description) VALUES(?, ?)",
        (version, description),
    )


def main():
    db_path = config.STORE_DIR / "messages.db"
    log.info("Running EvoClaw database migrations against %s", db_path)

    # init_database creates all tables (including schema_migrations) if they
    # don't exist yet.
    db.init_database(db_path)

    conn = db.get_db()
    applied = _get_applied_versions(conn)
    log.info("Already applied migrations: %s", sorted(applied))

    pending = [(v, desc, sql) for v, desc, sql in MIGRATIONS if v not in applied]
    if not pending:
        log.info("No pending migrations — schema is up to date.")
        print("✓ Database schema up to date")
        return

    for version, description, sql in sorted(pending, key=lambda x: x[0]):
        log.info("Applying migration v%d: %s", version, description)
        try:
            if sql:
                conn.executescript(sql)
            _record_migration(conn, version, description)
            conn.commit()
            log.info("Migration v%d applied successfully.", version)
        except Exception as exc:
            conn.rollback()
            log.error("Migration v%d FAILED: %s — aborting.", version, exc)
            sys.exit(1)

    print("✓ Database schema up to date")


if __name__ == "__main__":
    main()

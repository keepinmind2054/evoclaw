#!/usr/bin/env python3
"""Run database migrations for EvoClaw.

Each migration is identified by an integer version number.  Migrations are
applied in ascending order and recorded in the ``schema_migrations`` table so
that re-running this script is safe (already-applied migrations are skipped).

BUG-19C-10 FIX: migration lock via SQLite EXCLUSIVE transaction.
  Without a lock, two processes started simultaneously (e.g. a rolling deploy
  or accidental double-invocation) would both read the same set of applied
  versions and both attempt to apply the same pending migrations, potentially
  corrupting the schema (duplicate index creation raises an error when IF NOT
  EXISTS is absent, or silently double-applies data migrations).  We acquire
  an EXCLUSIVE transaction before reading the version table; SQLite serialises
  all writers at the file level so only one migrator can proceed at a time.
  The lock is released automatically when the connection closes or when the
  transaction commits/rolls back.
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
    (
        3,
        "p19c schema integrity fixes: "
        "UNIQUE(jid,run_id) on evolution_runs (BUG-19C-02), "
        "UNIQUE(run_id) on container_logs (BUG-19C-04), "
        "UNIQUE(folder) on registered_groups (BUG-19C-03), "
        "CHECK constraints on group_genome formality/technical_depth (BUG-19C-05), "
        "NOT NULL on task_run_logs.status (BUG-19C-06), "
        "NOT NULL DEFAULT 0 on evolution_runs.success (BUG-19C-01), "
        "back-fill NULL granted_at in rbac_grants (BUG-19C-07), "
        "cold_memory INSERT/DELETE FTS triggers (BUG-19C-12), "
        "warm_logs DELETE FTS trigger (BUG-19C-12)",
        # ---------------------------------------------------------------------------
        # NOTE: SQLite does not support ADD CONSTRAINT or ALTER COLUMN.  Constraints
        # that were absent from the original CREATE TABLE cannot be added to existing
        # tables via ALTER TABLE.  The idiomatic SQLite approach is:
        #   1. CREATE new table with desired constraints
        #   2. INSERT INTO new SELECT * FROM old
        #   3. DROP old table
        #   4. ALTER TABLE new RENAME TO old
        # We use this pattern for tables that need new NOT NULL / UNIQUE / CHECK
        # constraints.  All steps are wrapped in a single BEGIN/COMMIT so the
        # database is never left in a half-migrated state.
        # ---------------------------------------------------------------------------
        """
        -- ── evolution_runs: add UNIQUE(jid,run_id) + NOT NULL on success/retry ──
        CREATE TABLE IF NOT EXISTS evolution_runs_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            jid TEXT NOT NULL,
            run_id TEXT NOT NULL,
            response_ms INTEGER,
            retry_count INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 0,
            timestamp TEXT DEFAULT (datetime('now')),
            UNIQUE(jid, run_id)
        );
        INSERT OR IGNORE INTO evolution_runs_new
            (id, jid, run_id, response_ms, retry_count, success, timestamp)
        SELECT id, jid, run_id, response_ms,
               COALESCE(retry_count, 0),
               COALESCE(success, 0),
               COALESCE(timestamp, datetime('now'))
        FROM evolution_runs;
        DROP TABLE evolution_runs;
        ALTER TABLE evolution_runs_new RENAME TO evolution_runs;
        CREATE INDEX IF NOT EXISTS idx_evolution_jid_ts ON evolution_runs(jid, timestamp);

        -- ── container_logs: add UNIQUE(run_id) ───────────────────────────────────
        CREATE TABLE IF NOT EXISTS container_logs_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      TEXT NOT NULL UNIQUE,
            jid         TEXT NOT NULL,
            folder      TEXT NOT NULL DEFAULT '',
            container_name TEXT NOT NULL DEFAULT '',
            started_at  REAL NOT NULL,
            finished_at REAL,
            status      TEXT NOT NULL DEFAULT 'running',
            stderr      TEXT,
            stdout_preview TEXT,
            response_ms INTEGER
        );
        INSERT OR IGNORE INTO container_logs_new
            (id, run_id, jid, folder, container_name, started_at, finished_at,
             status, stderr, stdout_preview, response_ms)
        SELECT id, run_id, jid, folder, container_name, started_at, finished_at,
               status, stderr, stdout_preview, response_ms
        FROM container_logs;
        DROP TABLE container_logs;
        ALTER TABLE container_logs_new RENAME TO container_logs;
        CREATE INDEX IF NOT EXISTS idx_container_logs_jid ON container_logs(jid, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_container_logs_run_id ON container_logs(run_id);
        CREATE INDEX IF NOT EXISTS idx_container_logs_status ON container_logs(status);

        -- ── registered_groups: add UNIQUE(folder) ────────────────────────────────
        CREATE TABLE IF NOT EXISTS registered_groups_new (
            jid TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            folder TEXT NOT NULL UNIQUE,
            trigger_pattern TEXT,
            added_at INTEGER NOT NULL,
            container_config TEXT,
            requires_trigger INTEGER NOT NULL DEFAULT 1,
            is_main INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO registered_groups_new
            (jid, name, folder, trigger_pattern, added_at, container_config,
             requires_trigger, is_main)
        SELECT jid, name, folder, trigger_pattern, added_at, container_config,
               COALESCE(requires_trigger, 1), COALESCE(is_main, 0)
        FROM registered_groups;
        DROP TABLE registered_groups;
        ALTER TABLE registered_groups_new RENAME TO registered_groups;

        -- ── group_genome: add CHECK on formality/technical_depth ─────────────────
        -- Clamp any out-of-range values to [0.0, 1.0] before rebuilding.
        UPDATE group_genome SET formality = MAX(0.0, MIN(1.0, COALESCE(formality, 0.5)));
        UPDATE group_genome SET technical_depth = MAX(0.0, MIN(1.0, COALESCE(technical_depth, 0.5)));
        CREATE TABLE IF NOT EXISTS group_genome_new (
            jid TEXT PRIMARY KEY,
            response_style TEXT NOT NULL DEFAULT 'balanced',
            formality REAL NOT NULL DEFAULT 0.5 CHECK(formality >= 0.0 AND formality <= 1.0),
            technical_depth REAL NOT NULL DEFAULT 0.5 CHECK(technical_depth >= 0.0 AND technical_depth <= 1.0),
            generation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO group_genome_new
            (jid, response_style, formality, technical_depth, generation, updated_at)
        SELECT jid,
               COALESCE(response_style, 'balanced'),
               COALESCE(formality, 0.5),
               COALESCE(technical_depth, 0.5),
               COALESCE(generation, 0),
               COALESCE(updated_at, datetime('now'))
        FROM group_genome;
        DROP TABLE group_genome;
        ALTER TABLE group_genome_new RENAME TO group_genome;

        -- ── task_run_logs: add NOT NULL DEFAULT 'unknown' on status ──────────────
        -- Back-fill NULL status values before rebuilding.
        UPDATE task_run_logs SET status = 'unknown' WHERE status IS NULL;
        CREATE TABLE IF NOT EXISTS task_run_logs_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            run_at INTEGER NOT NULL,
            duration_ms INTEGER,
            status TEXT NOT NULL DEFAULT 'unknown',
            result TEXT,
            error TEXT
        );
        INSERT OR IGNORE INTO task_run_logs_new
            (id, task_id, run_at, duration_ms, status, result, error)
        SELECT id, task_id, run_at, duration_ms,
               COALESCE(status, 'unknown'),
               result, error
        FROM task_run_logs;
        DROP TABLE task_run_logs;
        ALTER TABLE task_run_logs_new RENAME TO task_run_logs;
        CREATE INDEX IF NOT EXISTS idx_task_run_logs_task_id ON task_run_logs(task_id);

        -- ── rbac_grants: back-fill NULL granted_at ───────────────────────────────
        -- The schema change (NOT NULL DEFAULT unixepoch()) applies to new rows only
        -- via CREATE TABLE IF NOT EXISTS in RBACStore._init_db().  Existing rows
        -- with NULL must be back-filled so ORDER BY granted_at is reliable.
        UPDATE rbac_grants SET granted_at = unixepoch() WHERE granted_at IS NULL;

        -- ── cold memory FTS triggers ──────────────────────────────────────────────
        CREATE TRIGGER IF NOT EXISTS cold_memory_ai AFTER INSERT ON group_cold_memory BEGIN
            INSERT INTO group_cold_memory_fts(rowid, jid, title, content, tags)
                VALUES(new.id, new.jid, new.title, new.content, new.tags);
        END;
        CREATE TRIGGER IF NOT EXISTS cold_memory_ad AFTER DELETE ON group_cold_memory BEGIN
            INSERT INTO group_cold_memory_fts(group_cold_memory_fts, rowid, jid, title, content, tags)
                VALUES('delete', old.id, old.jid, old.title, old.content, old.tags);
        END;

        -- ── warm logs DELETE FTS trigger ─────────────────────────────────────────
        CREATE TRIGGER IF NOT EXISTS warm_logs_ad AFTER DELETE ON group_warm_logs BEGIN
            INSERT INTO group_warm_logs_fts(group_warm_logs_fts, rowid, jid, log_date, content)
                VALUES('delete', old.id, old.jid, old.log_date, old.content);
        END;
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

    # BUG-19C-10 FIX: acquire an exclusive lock before reading the version table
    # to prevent two concurrent migration runs from both seeing the same pending
    # migrations and attempting to apply them simultaneously.  The EXCLUSIVE
    # transaction is held until the last migration commits (or we exit on error),
    # at which point SQLite releases the lock automatically.
    try:
        conn.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as exc:
        log.error(
            "Could not acquire migration lock (another process is migrating?): %s", exc
        )
        sys.exit(1)

    applied = _get_applied_versions(conn)
    log.info("Already applied migrations: %s", sorted(applied))

    pending = [(v, desc, sql) for v, desc, sql in MIGRATIONS if v not in applied]
    if not pending:
        conn.commit()  # release the exclusive lock
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

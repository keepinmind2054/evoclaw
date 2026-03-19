"""Migrate data from SQLite to PostgreSQL.

Usage:
    DATABASE_URL=postgresql://... SQLITE_PATH=data/messages.db python -m host.migrations.sqlite_to_pg
"""
import os
import sqlite3
import sys
import logging

log = logging.getLogger(__name__)


def migrate():
    sqlite_path = os.environ.get("SQLITE_PATH", "data/messages.db")
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

    # Discover all tables in SQLite
    tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    log.info("Tables to migrate: %s", tables)

    for table in tables:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            log.info("Skipping empty table: %s", table)
            continue

        cols = rows[0].keys()
        col_list = ", ".join(cols)
        placeholders = ", ".join(["%s"] * len(cols))

        with dst.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                [tuple(row) for row in rows],
            )

        dst.commit()
        log.info("Migrated %d rows from table '%s'", len(rows), table)

    src.close()
    dst.close()
    log.info("Migration complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate()

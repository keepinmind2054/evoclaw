"""
Migration 0002 — Add vectorized column to group_warm_logs.

Adds a vectorized flag to track which warm log entries have been
processed by the VectorIngestor background task (#496).
"""


def upgrade(conn):
    try:
        conn.execute("ALTER TABLE group_warm_logs ADD COLUMN vectorized INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # Column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_warm_logs_vec ON group_warm_logs(vectorized)")
    conn.commit()

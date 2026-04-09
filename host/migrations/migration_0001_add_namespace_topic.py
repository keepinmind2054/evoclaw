"""Migration 0001: Add namespace and topic_tag columns to shared_memories.

Implements the PalaceStore hierarchical memory structure (issue #495),
inspired by mempalace's wing/room hierarchy.

This migration is idempotent: the ALTER TABLE statements are wrapped in
try/except so re-running on a database that already has these columns
is safe and will not raise an error.
"""


def upgrade(conn):
    """Apply migration: add namespace + topic_tag to shared_memories."""
    try:
        conn.execute(
            "ALTER TABLE shared_memories ADD COLUMN namespace TEXT NOT NULL DEFAULT ''"
        )
    except Exception:
        # Column already exists — safe to ignore.
        pass
    try:
        conn.execute(
            "ALTER TABLE shared_memories ADD COLUMN topic_tag TEXT NOT NULL DEFAULT ''"
        )
    except Exception:
        # Column already exists — safe to ignore.
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_ns_topic"
        " ON shared_memories(namespace, topic_tag)"
    )
    conn.commit()
    # Record this migration in the schema_migrations tracking table so that
    # run_migrations.py (or any future migration runner) knows it has been applied.
    try:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, description)"
            " VALUES (1, 'add namespace and topic_tag to shared_memories')"
        )
        conn.commit()
    except Exception:
        pass

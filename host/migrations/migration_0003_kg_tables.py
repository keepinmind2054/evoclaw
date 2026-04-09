def upgrade(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kg_entities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'general',
            jid TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name, jid);
        CREATE TABLE IF NOT EXISTS kg_triples (
            id TEXT PRIMARY KEY,
            subject_id TEXT NOT NULL REFERENCES kg_entities(id),
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            jid TEXT NOT NULL,
            valid_from REAL NOT NULL,
            valid_to REAL,
            confidence REAL DEFAULT 1.0,
            source_memory_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_kg_triples_subject ON kg_triples(subject_id, predicate, valid_to);
    """)
    conn.commit()

"""
EvoKnowledgeGraph — temporal entity-relationship triple store.
Inspired by mempalace's knowledge_graph.py.
Stores facts as (subject, predicate, object) triples with temporal validity windows.
"""
import hashlib, logging, sqlite3, time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass
class Entity:
    id: str
    name: str
    entity_type: str
    jid: str
    created_at: float

@dataclass
class Triple:
    id: str
    subject_id: str
    predicate: str
    object: str
    jid: str
    valid_from: float
    valid_to: Optional[float]
    confidence: float
    source_memory_id: Optional[str]

class EvoKnowledgeGraph:
    # Predicates where a new value should auto-invalidate old ones
    HARD_UPDATE_PREDICATES = {"is", "works_at", "located_in", "belongs_to", "has_role", "uses_tool"}
    # Predicates where old + new can coexist
    OPINION_PREDICATES = {"thinks", "believes", "feels", "considers"}

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def _entity_id(self, name: str, jid: str) -> str:
        return hashlib.sha1(f"{jid}:{name.lower().strip()}".encode()).hexdigest()[:16]

    def _triple_id(self, subject_id: str, predicate: str, object_: str) -> str:
        return hashlib.sha1(f"{subject_id}:{predicate}:{object_}".encode()).hexdigest()[:16]

    def add_entity(self, name: str, entity_type: str, jid: str) -> str:
        """Get or create an entity. Returns entity_id."""
        eid = self._entity_id(name, jid)
        self._conn.execute(
            "INSERT OR IGNORE INTO kg_entities(id, name, entity_type, jid, created_at) VALUES(?,?,?,?,?)",
            (eid, name.strip(), entity_type, jid, time.time())
        )
        self._conn.commit()
        return eid

    def add_triple(self, subject: str, predicate: str, object_: str, jid: str,
                   confidence: float = 1.0, source_memory_id: str = None) -> str:
        """Add a fact triple. Auto-invalidates conflicting triples for hard-update predicates."""
        subject_id = self.add_entity(subject, "general", jid)
        now = time.time()

        # Check for conflicts
        conflicts = self.check_contradiction(subject, predicate, object_, jid)
        if conflicts:
            pred_lower = predicate.lower()
            if pred_lower in self.HARD_UPDATE_PREDICATES or pred_lower not in self.OPINION_PREDICATES:
                # Auto-invalidate conflicting triples
                for c in conflicts:
                    self.invalidate(c["id"], valid_to=now)
                    logger.info("KG: invalidated triple %s (superseded by new %s %s %s)",
                                c["id"], subject, predicate, object_)

        tid = self._triple_id(subject_id, predicate, object_)
        self._conn.execute(
            """INSERT OR IGNORE INTO kg_triples
               (id, subject_id, predicate, object, jid, valid_from, valid_to, confidence, source_memory_id)
               VALUES(?,?,?,?,?,?,NULL,?,?)""",
            (tid, subject_id, predicate, object_, jid, now, confidence, source_memory_id)
        )
        self._conn.commit()
        return tid

    def invalidate(self, triple_id: str, valid_to: float = None) -> None:
        """Mark a triple as no longer valid."""
        self._conn.execute(
            "UPDATE kg_triples SET valid_to=? WHERE id=?",
            (valid_to or time.time(), triple_id)
        )
        self._conn.commit()

    def query_entity(self, name: str, jid: str, as_of: float = None) -> list[dict]:
        """Return all active triples for an entity (optionally as of a past timestamp)."""
        eid = self._entity_id(name, jid)
        ts = as_of or time.time()
        rows = self._conn.execute(
            """SELECT t.id, t.predicate, t.object, t.valid_from, t.valid_to, t.confidence
               FROM kg_triples t
               WHERE t.subject_id=? AND t.jid=? AND t.valid_from<=?
                 AND (t.valid_to IS NULL OR t.valid_to>?)
               ORDER BY t.valid_from DESC""",
            (eid, jid, ts, ts)
        ).fetchall()
        return [{"id": r[0], "predicate": r[1], "object": r[2],
                 "valid_from": r[3], "valid_to": r[4], "confidence": r[5]} for r in rows]

    def check_contradiction(self, subject: str, predicate: str, object_: str, jid: str) -> list[dict]:
        """Find active triples with same subject+predicate but different object."""
        subject_id = self._entity_id(subject, jid)
        now = time.time()
        rows = self._conn.execute(
            """SELECT id, object, confidence FROM kg_triples
               WHERE subject_id=? AND predicate=? AND jid=?
                 AND object!=? AND valid_to IS NULL AND valid_from<=?""",
            (subject_id, predicate, jid, object_, now)
        ).fetchall()
        return [{"id": r[0], "object": r[1], "confidence": r[2]} for r in rows]

    def stats(self, jid: str) -> dict:
        """Return entity and triple counts for a jid."""
        e_count = self._conn.execute("SELECT COUNT(*) FROM kg_entities WHERE jid=?", (jid,)).fetchone()[0]
        t_active = self._conn.execute(
            "SELECT COUNT(*) FROM kg_triples WHERE jid=? AND valid_to IS NULL", (jid,)).fetchone()[0]
        t_total = self._conn.execute("SELECT COUNT(*) FROM kg_triples WHERE jid=?", (jid,)).fetchone()[0]
        return {"entities": e_count, "active_triples": t_active, "total_triples": t_total}

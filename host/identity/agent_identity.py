"""
Agent Identity Layer - Phase 1 of UnifiedClaw architecture

Provides persistent agent identity across container restarts.
Each agent has a stable identity derived from its name + project + channel.

Identity persists in SQLite table `agent_identities`.

Usage:
    store = AgentIdentityStore(db_conn)
    
    # Get or create identity
    identity = store.get_or_create("mybot", project="evoclaw", channel="telegram")
    print(identity.agent_id)  # stable hash
    
    # Update after conversation
    store.update_summary(identity.agent_id, "User is a developer who prefers Python")
    store.add_skill(identity.agent_id, "python-debugging")
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentIdentity:
    """Persistent identity for an AI agent."""
    agent_id: str           # Stable hash: SHA-256(name:project:channel)[:16]
    name: str               # Human-readable agent name
    project: str            # Project this agent belongs to
    channel: str            # Primary channel (telegram/discord/etc)
    skills: list[str]       # Accumulated skill tags
    profile: dict           # Free-form profile data
    history_summary: str    # Compressed conversation history
    genome_ref: str         # Reference to evolution genome entry
    last_active: float      # Unix timestamp
    created_at: float       # Unix timestamp
    message_count: int = 0  # Total messages processed

    @classmethod
    def make_id(cls, name: str, project: str, channel: str) -> str:
        """Generate stable agent_id from name + project + channel."""
        raw = f"{name.lower()}:{project.lower()}:{channel.lower()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class AgentIdentityStore:
    """
    SQLite-backed store for agent identities.
    
    Table: agent_identities
    """

    TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS agent_identities (
        agent_id        TEXT    PRIMARY KEY,
        name            TEXT    NOT NULL,
        project         TEXT    NOT NULL DEFAULT '',
        channel         TEXT    NOT NULL DEFAULT '',
        skills          TEXT    NOT NULL DEFAULT '[]',
        profile         TEXT    NOT NULL DEFAULT '{}',
        history_summary TEXT    NOT NULL DEFAULT '',
        genome_ref      TEXT    NOT NULL DEFAULT '',
        last_active     REAL    NOT NULL,
        created_at      REAL    NOT NULL,
        message_count   INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_agent_identities_name
        ON agent_identities(name, project);
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self):
        try:
            for stmt in self.TABLE_DDL.strip().split(";"):
                s = stmt.strip()
                if s:
                    self._conn.execute(s)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error(f"AgentIdentityStore schema error: {e}")

    def get_or_create(
        self,
        name: str,
        project: str = "",
        channel: str = "",
    ) -> AgentIdentity:
        """
        Retrieve existing identity or create a new one.
        
        The agent_id is stable: same name+project+channel always
        returns the same agent_id.
        """
        agent_id = AgentIdentity.make_id(name, project, channel)
        row = self._conn.execute(
            "SELECT * FROM agent_identities WHERE agent_id = ?", (agent_id,)
        ).fetchone()

        if row:
            return self._row_to_identity(row)

        # Create new identity
        now = time.time()
        identity = AgentIdentity(
            agent_id=agent_id,
            name=name,
            project=project,
            channel=channel,
            skills=[],
            profile={},
            history_summary="",
            genome_ref="",
            last_active=now,
            created_at=now,
            message_count=0,
        )
        self._insert(identity)
        logger.info(f"New agent identity created: {name} (id={agent_id})")
        return identity

    def get(self, agent_id: str) -> Optional[AgentIdentity]:
        """Retrieve identity by agent_id. Returns None if not found."""
        row = self._conn.execute(
            "SELECT * FROM agent_identities WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return self._row_to_identity(row) if row else None

    def update_summary(self, agent_id: str, summary: str):
        """Update compressed conversation history summary."""
        try:
            self._conn.execute(
                "UPDATE agent_identities SET history_summary = ?, last_active = ? WHERE agent_id = ?",
                (summary, time.time(), agent_id)
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error(f"AgentIdentity update_summary error: {e}")

    def add_skill(self, agent_id: str, skill: str):
        """Add a skill tag to the agent's skill list."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT skills FROM agent_identities WHERE agent_id=?", (agent_id,)
                ).fetchone()
                if not row:
                    return
                skills = json.loads(row[0] or "[]")
                if skill not in skills:
                    skills.append(skill)
                    self._conn.execute(
                        "UPDATE agent_identities SET skills=? WHERE agent_id=?",
                        (json.dumps(skills), agent_id)
                    )
                    self._conn.commit()
        except sqlite3.Error as e:
            logger.error(f"AgentIdentity add_skill error: {e}")

    def increment_message_count(self, agent_id: str):
        """Increment the message counter for this agent."""
        try:
            self._conn.execute(
                """UPDATE agent_identities
                   SET message_count = message_count + 1, last_active = ?
                   WHERE agent_id = ?""",
                (time.time(), agent_id)
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error(f"AgentIdentity increment_message_count error: {e}")

    def update_profile(self, agent_id: str, updates: dict):
        """Merge updates into the agent's profile dict."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT profile FROM agent_identities WHERE agent_id=?", (agent_id,)
                ).fetchone()
                if not row:
                    return
                profile = json.loads(row[0] or "{}")
                profile.update(updates)
                self._conn.execute(
                    "UPDATE agent_identities SET profile=? WHERE agent_id=?",
                    (json.dumps(profile), agent_id)
                )
                self._conn.commit()
        except sqlite3.Error as e:
            logger.error(f"AgentIdentity update_profile error: {e}")

    def list_agents(self, project: str = "") -> list[AgentIdentity]:
        """List all known agents, optionally filtered by project."""
        try:
            if project:
                rows = self._conn.execute(
                    "SELECT * FROM agent_identities WHERE project = ? ORDER BY last_active DESC",
                    (project,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM agent_identities ORDER BY last_active DESC"
                ).fetchall()
            return [self._row_to_identity(r) for r in rows]
        except sqlite3.Error as e:
            logger.error(f"AgentIdentity list_agents error: {e}")
            return []

    def _insert(self, identity: AgentIdentity):
        try:
            self._conn.execute(
                """INSERT INTO agent_identities
                   (agent_id, name, project, channel, skills, profile,
                    history_summary, genome_ref, last_active, created_at, message_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity.agent_id, identity.name, identity.project,
                    identity.channel, json.dumps(identity.skills),
                    json.dumps(identity.profile), identity.history_summary,
                    identity.genome_ref, identity.last_active,
                    identity.created_at, identity.message_count,
                )
            )
            self._conn.commit()
        except sqlite3.Error as e:
            logger.error(f"AgentIdentity insert error: {e}")

    def _row_to_identity(self, row) -> AgentIdentity:
        cols = [
            "agent_id", "name", "project", "channel", "skills", "profile",
            "history_summary", "genome_ref", "last_active", "created_at", "message_count"
        ]
        d = dict(zip(cols, row))
        d["skills"] = json.loads(d.get("skills", "[]") or "[]")
        d["profile"] = json.loads(d.get("profile", "{}") or "{}")
        return AgentIdentity(**d)

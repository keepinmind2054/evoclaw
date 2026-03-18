"""
RBAC — Role-Based Access Control — Phase 3

Roles:
  admin     — full access, can grant roles
  operator  — can run agents, manage memory
  agent     — can read/write own memory, submit tasks
  viewer    — read-only access

Permissions:
  memory:read, memory:write, memory:delete
  agent:spawn, agent:kill, agent:list
  task:submit, task:cancel
  registry:read, registry:write
  rbac:grant, rbac:revoke
"""
import sqlite3
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Set, Optional, List

logger = logging.getLogger(__name__)


class Permission(str, Enum):
    MEMORY_READ    = "memory:read"
    MEMORY_WRITE   = "memory:write"
    MEMORY_DELETE  = "memory:delete"
    AGENT_SPAWN    = "agent:spawn"
    AGENT_KILL     = "agent:kill"
    AGENT_LIST     = "agent:list"
    TASK_SUBMIT    = "task:submit"
    TASK_CANCEL    = "task:cancel"
    REGISTRY_READ  = "registry:read"
    REGISTRY_WRITE = "registry:write"
    RBAC_GRANT     = "rbac:grant"
    RBAC_REVOKE    = "rbac:revoke"


class Role(str, Enum):
    ADMIN    = "admin"
    OPERATOR = "operator"
    AGENT    = "agent"
    VIEWER   = "viewer"


ROLE_PERMISSIONS: dict = {
    Role.ADMIN: set(Permission),  # all permissions
    Role.OPERATOR: {
        Permission.MEMORY_READ, Permission.MEMORY_WRITE,
        Permission.AGENT_SPAWN, Permission.AGENT_KILL, Permission.AGENT_LIST,
        Permission.TASK_SUBMIT, Permission.TASK_CANCEL,
        Permission.REGISTRY_READ,
    },
    Role.AGENT: {
        Permission.MEMORY_READ, Permission.MEMORY_WRITE,
        Permission.AGENT_LIST, Permission.TASK_SUBMIT,
        Permission.REGISTRY_READ,
    },
    Role.VIEWER: {
        Permission.MEMORY_READ, Permission.AGENT_LIST, Permission.REGISTRY_READ,
    },
}


@dataclass
class RBACEntry:
    subject_id: str   # agent_id or bot_id
    role: Role
    granted_by: Optional[str] = None
    granted_at: float = 0.0


class RBACStore:
    """SQLite-backed RBAC store."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path.home() / ".evoclaw" / "rbac.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS rbac_grants (
                subject_id TEXT NOT NULL,
                role TEXT NOT NULL,
                granted_by TEXT,
                granted_at REAL,
                PRIMARY KEY (subject_id, role)
            )
        """)
        self._conn.commit()

    def grant(self, subject_id: str, role: Role, granted_by: Optional[str] = None):
        import time
        self._conn.execute("""
            INSERT OR REPLACE INTO rbac_grants (subject_id, role, granted_by, granted_at)
            VALUES (?,?,?,?)
        """, (subject_id, role.value, granted_by, time.time()))
        self._conn.commit()
        logger.info(f"RBAC grant: {subject_id} -> {role.value} (by {granted_by})")

    def revoke(self, subject_id: str, role: Role):
        self._conn.execute(
            "DELETE FROM rbac_grants WHERE subject_id=? AND role=?",
            (subject_id, role.value)
        )
        self._conn.commit()

    def get_roles(self, subject_id: str) -> Set[Role]:
        rows = self._conn.execute(
            "SELECT role FROM rbac_grants WHERE subject_id=?", (subject_id,)
        ).fetchall()
        return {Role(r[0]) for r in rows}

    def get_permissions(self, subject_id: str) -> Set[Permission]:
        roles = self.get_roles(subject_id)
        perms: Set[Permission] = set()
        for role in roles:
            perms.update(ROLE_PERMISSIONS.get(role, set()))
        return perms

    def has_permission(self, subject_id: str, permission: Permission) -> bool:
        return permission in self.get_permissions(subject_id)

    def list_grants(self) -> List[RBACEntry]:
        rows = self._conn.execute(
            "SELECT subject_id, role, granted_by, granted_at FROM rbac_grants"
        ).fetchall()
        return [RBACEntry(r[0], Role(r[1]), r[2], r[3]) for r in rows]


def require_permission(rbac: RBACStore, subject_id: str, permission: Permission) -> bool:
    """Check permission, raise PermissionError if denied."""
    if not rbac.has_permission(subject_id, permission):
        raise PermissionError(f"{subject_id} lacks permission: {permission.value}")
    return True

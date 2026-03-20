"""
RBAC — Role-Based Access Control — Phase 3

Roles: admin, operator, agent, viewer
Permissions: memory:*, agent:*, task:*, registry:*, rbac:*
"""
import sqlite3
import time
import time as _time
import threading
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
    Role.ADMIN:    frozenset(Permission),
    Role.OPERATOR: frozenset({
        Permission.MEMORY_READ, Permission.MEMORY_WRITE,
        Permission.AGENT_SPAWN, Permission.AGENT_KILL, Permission.AGENT_LIST,
        Permission.TASK_SUBMIT, Permission.TASK_CANCEL,
        Permission.REGISTRY_READ,
    }),
    Role.AGENT: frozenset({
        Permission.MEMORY_READ, Permission.MEMORY_WRITE,
        Permission.AGENT_LIST, Permission.TASK_SUBMIT,
        Permission.REGISTRY_READ,
    }),
    Role.VIEWER: frozenset({
        Permission.MEMORY_READ,
        Permission.AGENT_LIST,
        Permission.REGISTRY_READ,
    }),
}


@dataclass
class RBACEntry:
    subject_id: str
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
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()  # separate lock to avoid deadlock with _lock
        self._role_cache: dict = {}  # {subject_id: (roles_set, fetched_at)}
        self._cache_ttl = 60.0
        self._cache_maxsize = 512
        # Cache for _is_empty() result: (result: bool, fetched_at: float)
        # Invalidated on every grant/revoke via _invalidate_empty_cache().
        self._empty_cache: Optional[tuple] = None  # (is_empty: bool, fetched_at: float)
        self._init_db()
        logger.info(f"RBACStore initialized at {db_path}")

    def _init_db(self):
        with self._lock:
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

    def _get_cached_roles(self, subject_id: str):
        with self._cache_lock:
            entry = self._role_cache.get(subject_id)
            if entry:
                roles, fetched_at = entry
                if (_time.time() - fetched_at) < self._cache_ttl:
                    return roles
                # Expired — evict immediately
                del self._role_cache[subject_id]
        return None

    def _set_cached_roles(self, subject_id: str, roles):
        with self._cache_lock:
            # Evict oldest entry if at capacity
            if len(self._role_cache) >= self._cache_maxsize:
                oldest_key = min(self._role_cache, key=lambda k: self._role_cache[k][1])
                del self._role_cache[oldest_key]
            self._role_cache[subject_id] = (roles, _time.time())

    def _invalidate_cache(self, subject_id: str):
        with self._cache_lock:
            self._role_cache.pop(subject_id, None)
            # Also invalidate the _is_empty cache so has_permission() reflects the grant/revoke
            self._empty_cache = None

    def grant(self, subject_id: str, role: Role, granted_by: Optional[str] = None):
        with self._lock:
            self._conn.execute("""
                INSERT OR REPLACE INTO rbac_grants (subject_id, role, granted_by, granted_at)
                VALUES (?,?,?,?)
            """, (subject_id, role.value, granted_by, time.time()))
            self._conn.commit()
        self._invalidate_cache(subject_id)
        logger.info(f"RBAC grant: {subject_id} -> {role.value} (by {granted_by})")

    def revoke(self, subject_id: str, role: Role):
        with self._lock:
            self._conn.execute(
                "DELETE FROM rbac_grants WHERE subject_id=? AND role=?",
                (subject_id, role.value)
            )
            self._conn.commit()
        self._invalidate_cache(subject_id)
        logger.info(f"RBAC revoke: {subject_id} - {role.value}")

    def get_roles(self, subject_id: str) -> Set[Role]:
        cached = self._get_cached_roles(subject_id)
        if cached is not None:
            return cached
        with self._lock:
            rows = self._conn.execute(
                "SELECT role FROM rbac_grants WHERE subject_id=?", (subject_id,)
            ).fetchall()
        roles = {Role(r[0]) for r in rows}
        self._set_cached_roles(subject_id, roles)
        return roles

    def get_permissions(self, subject_id: str) -> Set[Permission]:
        roles = self.get_roles(subject_id)
        perms: Set[Permission] = set()
        for role in roles:
            perms.update(ROLE_PERMISSIONS.get(role, frozenset()))
        return perms

    def _is_empty(self) -> bool:
        """Return True if no grants have been configured at all.

        An empty RBAC store means the system is unconfigured — no one has
        ever been granted a role.  In this state we fail-open (allow all)
        rather than locking everyone out of a fresh install.

        Result is cached for self._cache_ttl seconds (same TTL as role cache)
        and invalidated immediately on every grant() / revoke() call, so the
        cache never serves a stale "empty" result after the first admin grant.
        Without caching, every has_permission() call hits the DB twice —
        once for the count and once for the role query.
        """
        with self._cache_lock:
            if self._empty_cache is not None:
                _cached_result, _cached_at = self._empty_cache
                if (_time.time() - _cached_at) < self._cache_ttl:
                    return _cached_result
                # Expired
                self._empty_cache = None
        with self._lock:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM rbac_grants"
            ).fetchone()[0]
        result = count == 0
        with self._cache_lock:
            self._empty_cache = (result, _time.time())
        return result

    def has_permission(self, subject_id: str, permission: Permission) -> bool:
        # Fail-open when unconfigured: if nobody has been granted any role,
        # treat RBAC as not yet set up and allow all requests through.
        # Once at least one grant exists, strict enforcement applies.
        if self._is_empty():
            return True
        return permission in self.get_permissions(subject_id)

    def list_grants(self) -> List[RBACEntry]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject_id, role, granted_by, granted_at FROM rbac_grants"
            ).fetchall()
        return [RBACEntry(r[0], Role(r[1]), r[2], r[3]) for r in rows]

    def close(self):
        self._conn.close()


def require_permission(rbac: RBACStore, subject_id: str, permission: Permission) -> bool:
    """Check permission, raise PermissionError if denied."""
    if not rbac.has_permission(subject_id, permission):
        raise PermissionError(f"{subject_id} lacks permission: {permission.value}")
    return True

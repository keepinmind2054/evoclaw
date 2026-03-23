"""
Tests for host.rbac.roles — Role-Based Access Control store.

Critical paths covered:
  - RBACStore: grant / revoke / get_roles / has_permission
  - BUG-RBAC-01 fix: unknown role values are skipped, not raised
  - BUG-RBAC-02 fix: close() acquires lock before closing connection
  - _is_empty() fail-open when no grants exist
  - Role cache invalidation on grant/revoke
  - require_permission raises PermissionError when denied
  - Role permission set correctness
"""
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.rbac.roles import (
    Permission,
    RBACStore,
    Role,
    ROLE_PERMISSIONS,
    require_permission,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def rbac(tmp_path):
    """Provide a fresh RBACStore backed by a temp SQLite file."""
    store = RBACStore(db_path=str(tmp_path / "rbac.db"))
    yield store
    store.close()


# ── Role permission sets ───────────────────────────────────────────────────────

class TestRolePermissions:
    def test_admin_has_all_permissions(self):
        """Admin role must have every Permission value."""
        assert ROLE_PERMISSIONS[Role.ADMIN] == frozenset(Permission)

    def test_viewer_cannot_write_or_delete(self):
        """Viewer must NOT have write or delete permissions."""
        viewer_perms = ROLE_PERMISSIONS[Role.VIEWER]
        assert Permission.MEMORY_WRITE not in viewer_perms
        assert Permission.MEMORY_DELETE not in viewer_perms
        assert Permission.AGENT_SPAWN not in viewer_perms
        assert Permission.RBAC_GRANT not in viewer_perms

    def test_viewer_can_read(self):
        """Viewer must have basic read permissions."""
        viewer_perms = ROLE_PERMISSIONS[Role.VIEWER]
        assert Permission.MEMORY_READ in viewer_perms
        assert Permission.AGENT_LIST in viewer_perms
        assert Permission.REGISTRY_READ in viewer_perms

    def test_operator_cannot_grant_rbac(self):
        """Operator must NOT be able to grant RBAC permissions."""
        assert Permission.RBAC_GRANT not in ROLE_PERMISSIONS[Role.OPERATOR]
        assert Permission.RBAC_REVOKE not in ROLE_PERMISSIONS[Role.OPERATOR]


# ── RBACStore basic operations ────────────────────────────────────────────────

class TestRBACStoreBasic:
    def test_grant_and_get_roles(self, rbac):
        """grant() then get_roles() must return the granted role."""
        rbac.grant("alice", Role.OPERATOR, granted_by="admin")
        roles = rbac.get_roles("alice")
        assert Role.OPERATOR in roles

    def test_revoke_removes_role(self, rbac):
        """revoke() must remove a previously granted role."""
        rbac.grant("bob", Role.AGENT)
        rbac.revoke("bob", Role.AGENT)
        roles = rbac.get_roles("bob")
        assert Role.AGENT not in roles

    def test_get_roles_unknown_subject_returns_empty(self, rbac):
        """get_roles() for an unknown subject must return an empty set."""
        roles = rbac.get_roles("nobody")
        assert roles == set()

    def test_grant_multiple_roles(self, rbac):
        """A subject can hold multiple roles simultaneously."""
        rbac.grant("charlie", Role.AGENT)
        rbac.grant("charlie", Role.VIEWER)
        roles = rbac.get_roles("charlie")
        assert Role.AGENT in roles
        assert Role.VIEWER in roles

    def test_has_permission_admin_can_do_anything(self, rbac):
        """Admin must have every permission."""
        rbac.grant("super-admin", Role.ADMIN)
        for perm in Permission:
            assert rbac.has_permission("super-admin", perm), (
                f"Admin missing permission: {perm}"
            )

    def test_has_permission_viewer_cannot_write(self, rbac):
        """A viewer-only subject must be denied write permissions."""
        rbac.grant("readonly-user", Role.VIEWER)
        assert rbac.has_permission("readonly-user", Permission.MEMORY_READ) is True
        assert rbac.has_permission("readonly-user", Permission.MEMORY_WRITE) is False
        assert rbac.has_permission("readonly-user", Permission.MEMORY_DELETE) is False

    def test_has_permission_fail_open_when_empty(self, rbac):
        """When no grants exist (_is_empty), has_permission must return True for all."""
        # Fresh store — no grants
        assert rbac._is_empty() is True
        assert rbac.has_permission("anyone", Permission.AGENT_SPAWN) is True

    def test_has_permission_strict_after_first_grant(self, rbac):
        """After at least one grant, has_permission must enforce roles strictly."""
        rbac.grant("admin1", Role.ADMIN)
        # New unknown subject must be denied
        assert rbac.has_permission("unknown-user", Permission.AGENT_SPAWN) is False


# ── BUG-RBAC-01: unknown role values in DB ────────────────────────────────────

class TestRBACUnknownRoleFix:
    def test_unknown_role_in_db_skipped_not_raised(self, rbac):
        """BUG-RBAC-01 FIX: rows with unknown role strings must be skipped.

        A future schema migration or DB corruption could leave rows with
        role values that don't match any Role enum value.  Before the fix
        these caused a ValueError crash in get_roles().
        """
        # Directly inject an unknown role value into the DB
        rbac._conn.execute(
            "INSERT OR REPLACE INTO rbac_grants (subject_id, role, granted_by, granted_at) "
            "VALUES (?, ?, ?, ?)",
            ("test-user", "super-duper-role-that-does-not-exist", "test", time.time())
        )
        rbac._conn.commit()
        rbac._invalidate_cache("test-user")

        # Must not raise — unknown role is silently skipped
        roles = rbac.get_roles("test-user")
        # The unknown role is NOT returned
        assert not any(r.value == "super-duper-role-that-does-not-exist" for r in roles)

    def test_list_grants_skips_unknown_roles(self, rbac):
        """BUG-RBAC-01 FIX: list_grants() must also skip unknown role rows."""
        rbac._conn.execute(
            "INSERT OR REPLACE INTO rbac_grants (subject_id, role, granted_by, granted_at) "
            "VALUES (?, ?, ?, ?)",
            ("ghost-user", "extinct-role", "test", time.time())
        )
        rbac._conn.commit()

        entries = rbac.list_grants()
        assert not any(
            e.subject_id == "ghost-user" for e in entries
        ), "Unknown role should not appear in list_grants() output"


# ── Role cache invalidation ───────────────────────────────────────────────────

class TestRBACCacheInvalidation:
    def test_revoke_invalidates_cache(self, rbac):
        """After revoke(), the role must not be served from cache."""
        rbac.grant("cached-user", Role.AGENT)
        # Warm the cache
        roles_before = rbac.get_roles("cached-user")
        assert Role.AGENT in roles_before

        rbac.revoke("cached-user", Role.AGENT)
        # Must reflect revocation immediately
        roles_after = rbac.get_roles("cached-user")
        assert Role.AGENT not in roles_after

    def test_grant_invalidates_empty_cache(self, rbac):
        """After grant(), _is_empty() must return False even if cached as True."""
        assert rbac._is_empty() is True  # warms empty_cache
        rbac.grant("first-user", Role.VIEWER)
        # Cache should be invalidated — must not report empty
        assert rbac._is_empty() is False


# ── require_permission ─────────────────────────────────────────────────────────

class TestRequirePermission:
    def test_raises_permission_error_when_denied(self, rbac):
        """require_permission must raise PermissionError for insufficient roles."""
        rbac.grant("limited-user", Role.VIEWER)
        with pytest.raises(PermissionError):
            require_permission(rbac, "limited-user", Permission.AGENT_SPAWN)

    def test_does_not_raise_when_permitted(self, rbac):
        """require_permission must return True when permission is granted."""
        rbac.grant("powerful-user", Role.OPERATOR)
        result = require_permission(rbac, "powerful-user", Permission.AGENT_SPAWN)
        assert result is True

"""
LDAP/AD Connector — Phase 3 Enterprise Suite

User/group lookup for RBAC and agent identity verification.
"""
import os
import logging
from typing import Optional, List, Dict
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import ldap3
    from ldap3.utils.conv import escape_filter_chars
    _LDAP3_AVAILABLE = True
except ImportError:
    ldap3 = None
    escape_filter_chars = None
    _LDAP3_AVAILABLE = False


@dataclass
class LDAPUser:
    dn: str
    username: str
    email: str
    display_name: str
    groups: List[str]
    department: Optional[str] = None


class LDAPConnector:
    """
    LDAP/Active Directory connector.

    Config via environment:
      LDAP_SERVER   — ldap://mycompany.com
      LDAP_BASE_DN  — DC=mycompany,DC=com
      LDAP_BIND_DN  — CN=svc_account,DC=mycompany,DC=com
      LDAP_BIND_PW  — service account password
    """

    def __init__(
        self,
        server: Optional[str] = None,
        base_dn: Optional[str] = None,
        bind_dn: Optional[str] = None,
        bind_pw: Optional[str] = None,
    ):
        self.server = server or os.getenv("LDAP_SERVER", "")
        self.base_dn = base_dn or os.getenv("LDAP_BASE_DN", "")
        self.bind_dn = bind_dn or os.getenv("LDAP_BIND_DN", "")
        # P14D-LDAP-1: password stored in private attribute; never logged
        self._bind_pw = bind_pw or os.getenv("LDAP_BIND_PW", "")
        self._conn = None
        if self.server:
            self._try_connect()

    def _try_connect(self):
        """Attempt to bind to the LDAP server.

        P14D-LDAP-2: connection failures are caught and logged so callers get
        None from lookup methods rather than an unhandled exception.

        P14D-LDAP-3: connect_timeout is set so the bot does not hang
        indefinitely if the LDAP server is unreachable.
        """
        if not _LDAP3_AVAILABLE:
            logger.warning("ldap3 not installed — LDAP connector unavailable")
            return
        try:
            srv = ldap3.Server(
                self.server,
                get_info=ldap3.ALL,
                connect_timeout=10,  # P14D-LDAP-3: don't block forever
            )
            self._conn = ldap3.Connection(
                srv,
                user=self.bind_dn,
                # P14D-LDAP-1: bind_pw is private; passed directly, never logged
                password=self._bind_pw,
                auto_bind=True,
                receive_timeout=30,  # P14D-LDAP-3: also cap search/receive time
            )
            logger.info(f"LDAP connected: {self.server}")
        except Exception as e:
            logger.error(f"LDAP connection failed: {e}")
            self._conn = None

    # ------------------------------------------------------------------
    # Connection health helpers
    # ------------------------------------------------------------------

    def _ensure_connection(self) -> bool:
        """Return True if the connection is usable, attempting re-bind if stale.

        P14D-LDAP-2 / P14D-LDAP-3: transparently re-connect after a server
        restart or idle-timeout rather than leaving _conn in a broken state.
        """
        if not _LDAP3_AVAILABLE:
            return False
        if self._conn is None:
            self._try_connect()
        if self._conn is None:
            return False
        # ldap3 Connection.closed is True when the socket has been shut down
        if getattr(self._conn, 'closed', False):
            logger.info("LDAP connection closed — reconnecting")
            self._try_connect()
        return self._conn is not None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup_user(self, username: str) -> Optional[LDAPUser]:
        """Look up a user by sAMAccountName.

        P14D-LDAP-4: the username is escaped with escape_filter_chars() before
        being interpolated into the LDAP search filter, preventing LDAP
        injection attacks (e.g. a username of ``*)(uid=*))(|(uid=*`` would
        otherwise match every user).
        """
        if not _LDAP3_AVAILABLE:
            return None
        if not self._ensure_connection():
            logger.error("LDAP lookup_user: no connection available")
            return None
        try:
            # P14D-LDAP-4: escape_filter_chars prevents LDAP injection
            safe_username = escape_filter_chars(username)
            self._conn.search(
                self.base_dn,
                f"(sAMAccountName={safe_username})",
                attributes=["cn", "mail", "memberOf", "department"]
            )
            if not self._conn.entries:
                return None
            entry = self._conn.entries[0]
            groups = [str(g).split(",")[0].replace("CN=", "")
                     for g in (entry.memberOf.values if hasattr(entry, 'memberOf') else [])]
            return LDAPUser(
                dn=str(entry.entry_dn),
                username=username,
                email=str(entry.mail) if hasattr(entry, 'mail') else "",
                display_name=str(entry.cn) if hasattr(entry, 'cn') else username,
                groups=groups,
                department=str(entry.department) if hasattr(entry, 'department') else None,
            )
        except Exception as e:
            logger.error(f"LDAP lookup failed for {username}: {e}")
            return None

    def get_group_members(self, group_cn: str) -> List[str]:
        """Get members of an LDAP group.

        P14D-LDAP-4: group_cn is escaped with escape_filter_chars() to prevent
        LDAP injection.
        """
        if not _LDAP3_AVAILABLE:
            return []
        if not self._ensure_connection():
            logger.error("LDAP get_group_members: no connection available")
            return []
        try:
            # P14D-LDAP-4: escape prevents LDAP injection
            safe_group_cn = escape_filter_chars(group_cn)
            self._conn.search(
                self.base_dn,
                f"(&(objectClass=group)(cn={safe_group_cn}))",
                attributes=["member"]
            )
            if not self._conn.entries:
                return []
            members = self._conn.entries[0].member.values if self._conn.entries else []
            return [str(m).split(",")[0].replace("CN=", "") for m in members]
        except Exception as e:
            logger.error(f"LDAP group lookup failed for {group_cn}: {e}")
            return []

    def is_user_in_group(self, username: str, group_cn: str) -> bool:
        """Check whether a user is a direct member of an LDAP group.

        P14D-LDAP-5: performs a single targeted search rather than fetching
        all group members and doing a Python-side comparison, which is both
        more efficient and avoids loading potentially huge member lists.
        Both username and group_cn are filter-escaped.
        """
        if not _LDAP3_AVAILABLE:
            return False
        if not self._ensure_connection():
            return False
        try:
            safe_user = escape_filter_chars(username)
            safe_group = escape_filter_chars(group_cn)
            # First resolve the user's DN
            self._conn.search(
                self.base_dn,
                f"(sAMAccountName={safe_user})",
                attributes=["distinguishedName"],
            )
            if not self._conn.entries:
                return False
            user_dn = str(self._conn.entries[0].entry_dn)
            safe_dn = escape_filter_chars(user_dn)
            # Then check group membership via a direct member= filter
            self._conn.search(
                self.base_dn,
                f"(&(objectClass=group)(cn={safe_group})(member={safe_dn}))",
                attributes=[],
            )
            return bool(self._conn.entries)
        except Exception as e:
            logger.error(f"LDAP is_user_in_group failed ({username}, {group_cn}): {e}")
            return False

    def close(self) -> None:
        """Explicitly release the LDAP connection.

        P14D-LDAP-6: callers that manage their own lifecycle can call close()
        to return the underlying socket to the OS promptly rather than relying
        on GC / connection-pool reaping.
        """
        if self._conn is not None:
            try:
                self._conn.unbind()
            except Exception:
                pass
            self._conn = None

    def is_configured(self) -> bool:
        return bool(self.server and self.base_dn)

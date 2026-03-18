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
    _LDAP3_AVAILABLE = True
except ImportError:
    ldap3 = None
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
        self.bind_pw = bind_pw or os.getenv("LDAP_BIND_PW", "")
        self._conn = None
        if self.server:
            self._try_connect()

    def _try_connect(self):
        if not _LDAP3_AVAILABLE:
            logger.warning("ldap3 not installed — LDAP connector unavailable")
            return
        try:
            server = ldap3.Server(self.server, get_info=ldap3.ALL)
            self._conn = ldap3.Connection(
                server, user=self.bind_dn, password=self.bind_pw, auto_bind=True
            )
            logger.info(f"LDAP connected: {self.server}")
        except Exception as e:
            logger.error(f"LDAP connection failed: {e}")

    def lookup_user(self, username: str) -> Optional[LDAPUser]:
        """Look up a user by sAMAccountName."""
        if not _LDAP3_AVAILABLE:
            return None
        if not self._conn:
            return None
        try:
            self._conn.search(
                self.base_dn,
                f"(sAMAccountName={username})",
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
        """Get members of an LDAP group."""
        if not _LDAP3_AVAILABLE:
            return []
        if not self._conn:
            return []
        try:
            self._conn.search(
                self.base_dn,
                f"(&(objectClass=group)(cn={group_cn}))",
                attributes=["member"]
            )
            if not self._conn.entries:
                return []
            members = self._conn.entries[0].member.values if self._conn.entries else []
            return [str(m).split(",")[0].replace("CN=", "") for m in members]
        except Exception as e:
            logger.error(f"LDAP group lookup failed for {group_cn}: {e}")
            return []

    def is_configured(self) -> bool:
        return bool(self.server and self.base_dn)

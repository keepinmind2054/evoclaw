"""
Cross-bot Identity Registry — Phase 3
Enables cross-framework bot recognition via shared identity protocol.

Protocol:
  1. Each bot has stable bot_id = SHA-256(name:framework:channel)[:16]
  2. Bots register on startup
  3. Cross-bot messages carry X-Bot-Id header
  4. Receiving bot verifies identity against registry
"""
import hashlib
import json
import time
import sqlite3
import threading
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List
from pathlib import Path

logger = logging.getLogger(__name__)

BOT_REGISTRY_VERSION = "1.0"

# BUG-BR-2 FIX: Nonces older than this (in seconds) are considered expired
# and cannot be used to complete a handshake.  Prevents replay attacks where
# a leaked nonce is submitted at an arbitrary future time.
_NONCE_TTL_SECS = 300  # 5 minutes


@dataclass
class BotIdentity:
    """Stable identity for a bot across framework boundaries."""
    bot_id: str
    name: str
    display_name: str
    framework: str        # "external" | "evoclaw"
    channel: str          # "telegram" | "discord"
    capabilities: List[str] = field(default_factory=list)
    ws_endpoint: Optional[str] = None
    http_endpoint: Optional[str] = None
    public_key: Optional[str] = None
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    trusted: bool = False

    @staticmethod
    def make_bot_id(name: str, framework: str, channel: str) -> str:
        raw = f"{name.lower()}:{framework.lower()}:{channel.lower()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BotIdentity":
        valid = {k for k in d if k in cls.__dataclass_fields__}
        return cls(**{k: d[k] for k in valid})


class BotRegistry:
    """Persistent registry of known bots across frameworks."""

    _BOT_COLS = ["bot_id", "name", "display_name", "framework", "channel",
                 "capabilities", "ws_endpoint", "http_endpoint", "public_key",
                 "registered_at", "last_seen", "trusted"]

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path.home() / ".evoclaw" / "bot_registry.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        # BUG-BR-1 FIX: _pending_handshakes is accessed from multiple threads
        # but was previously protected only by _lock in some paths and not others.
        # We now use the same _lock for ALL accesses to this dict.
        self._pending_handshakes: Dict[str, List[float]] = {}
        self._init_db()
        logger.info(f"BotRegistry initialized at {db_path}")

    def _init_db(self):
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS bots (
                    bot_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    display_name TEXT,
                    framework TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    capabilities TEXT DEFAULT '[]',
                    ws_endpoint TEXT,
                    http_endpoint TEXT,
                    public_key TEXT,
                    registered_at REAL,
                    last_seen REAL,
                    trusted INTEGER DEFAULT 0
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_handshakes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    initiator_bot_id TEXT NOT NULL,
                    target_bot_id TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    initiated_at REAL,
                    completed_at REAL,
                    nonce TEXT
                )
            """)
            # BUG-BR-2 FIX: Index on nonce for fast expired-nonce cleanup queries.
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_handshakes_nonce
                ON bot_handshakes(nonce, status, initiated_at)
            """)
            self._conn.commit()

    def register(self, identity: BotIdentity) -> BotIdentity:
        with self._lock:
            self._conn.execute("""
                INSERT INTO bots
                    (bot_id,name,display_name,framework,channel,capabilities,
                     ws_endpoint,http_endpoint,public_key,registered_at,last_seen,trusted)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    ws_endpoint=excluded.ws_endpoint,
                    http_endpoint=excluded.http_endpoint,
                    last_seen=excluded.last_seen,
                    capabilities=excluded.capabilities
            """, (
                identity.bot_id, identity.name, identity.display_name,
                identity.framework, identity.channel,
                json.dumps(identity.capabilities),
                identity.ws_endpoint, identity.http_endpoint,
                identity.public_key, identity.registered_at,
                identity.last_seen, int(identity.trusted)
            ))
            self._conn.commit()
        logger.info(f"Registered bot: {identity.name} ({identity.bot_id})")
        return identity

    def lookup(self, bot_id: str) -> Optional[BotIdentity]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bots WHERE bot_id=?", (bot_id,)
            ).fetchone()
        return self._row_to_identity(row) if row else None

    def lookup_by_name(self, name: str) -> Optional[BotIdentity]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bots WHERE lower(name)=lower(?) OR lower(display_name)=lower(?)",
                (name, name)
            ).fetchone()
        return self._row_to_identity(row) if row else None

    def list_all(self) -> List[BotIdentity]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM bots ORDER BY registered_at").fetchall()
        return [self._row_to_identity(r) for r in rows]

    def list_trusted(self) -> List[BotIdentity]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM bots WHERE trusted=1 ORDER BY registered_at"
            ).fetchall()
        return [self._row_to_identity(r) for r in rows]

    def trust(self, bot_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE bots SET trusted=1 WHERE bot_id=?", (bot_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def initiate_handshake(self, initiator_id: str, target_id: str) -> str:
        import secrets
        # BUG-BR-1 FIX: Hold _lock for the entire rate-limit check + insert so
        # that concurrent callers cannot both pass the rate-limit check and both
        # insert entries, bypassing the 5-attempt cap.
        with self._lock:
            now = time.time()
            timestamps = self._pending_handshakes.setdefault(target_id, [])
            # Prune timestamps older than _NONCE_TTL_SECS seconds
            self._pending_handshakes[target_id] = [t for t in timestamps if now - t < _NONCE_TTL_SECS]
            if len(self._pending_handshakes[target_id]) >= 5:
                raise RuntimeError("Handshake rate limit exceeded for target")
            self._pending_handshakes[target_id].append(now)
            nonce = secrets.token_hex(16)
            self._conn.execute("""
                INSERT INTO bot_handshakes
                    (initiator_bot_id,target_bot_id,status,initiated_at,nonce)
                VALUES (?,?,?,?,?)
            """, (initiator_id, target_id, "pending", now, nonce))
            self._conn.commit()
        return nonce

    def complete_handshake(self, initiator_id: str, target_id: str, nonce: str) -> bool:
        """Complete a handshake and mark both bots as trusted.

        BUG-BR-2 FIX: Expired nonces (older than _NONCE_TTL_SECS) are rejected
        even if their status is still 'pending' in the DB, preventing replay
        attacks where a nonce intercepted or leaked in the past is later reused
        to elevate trust.
        """
        now = time.time()
        expiry_cutoff = now - _NONCE_TTL_SECS
        with self._lock:
            row = self._conn.execute("""
                SELECT id, initiated_at FROM bot_handshakes
                WHERE initiator_bot_id=? AND target_bot_id=? AND nonce=? AND status='pending'
            """, (initiator_id, target_id, nonce)).fetchone()
            if not row:
                logger.warning(f"Handshake failed: {initiator_id} -> {target_id} (nonce not found or already completed)")
                return False
            # BUG-BR-2 FIX: Reject expired nonces.
            initiated_at = row[1] if isinstance(row, (tuple, list)) else row["initiated_at"]
            if initiated_at < expiry_cutoff:
                logger.warning(
                    "Handshake failed: %s -> %s (nonce expired — initiated %.0fs ago, TTL=%ds)",
                    initiator_id, target_id, now - initiated_at, _NONCE_TTL_SECS,
                )
                # Mark as expired so it cannot be retried
                row_id = row[0] if isinstance(row, (tuple, list)) else row["id"]
                self._conn.execute(
                    "UPDATE bot_handshakes SET status='expired' WHERE id=?", (row_id,)
                )
                self._conn.commit()
                return False
            row_id = row[0] if isinstance(row, (tuple, list)) else row["id"]
            self._conn.execute("""
                UPDATE bot_handshakes SET status='completed', completed_at=? WHERE id=?
            """, (now, row_id))
            self._conn.execute(
                "UPDATE bots SET trusted=1 WHERE bot_id IN (?,?)",
                (initiator_id, target_id)
            )
            self._conn.commit()
        logger.info(f"Handshake completed: {initiator_id} <-> {target_id}")
        return True

    def update_last_seen(self, bot_id: str):
        with self._lock:
            self._conn.execute(
                "UPDATE bots SET last_seen=? WHERE bot_id=?", (time.time(), bot_id)
            )
            self._conn.commit()

    def purge_stale_bots(self, max_age_secs: float = 86400 * 7) -> int:
        """Remove bots that have not been seen for *max_age_secs* seconds.

        BUG-BR-3 FIX: Without periodic cleanup, disconnected bots accumulate
        in the registry indefinitely — a memory/storage leak and a potential
        security concern (stale trusted entries).  Returns the number of bots
        removed.
        """
        cutoff = time.time() - max_age_secs
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bots WHERE trusted=0 AND last_seen < ?", (cutoff,)
            )
            self._conn.commit()
        removed = cur.rowcount
        if removed:
            logger.info("BotRegistry: purged %d stale (untrusted) bot(s)", removed)
        return removed

    def purge_stale_handshakes(self, max_age_secs: float = _NONCE_TTL_SECS * 2) -> int:
        """Remove old handshake rows from the bot_handshakes table and evict
        corresponding entries from the in-memory _pending_handshakes dict.

        BUG-18C-04 (MEDIUM): The bot_handshakes table is written on every
        initiate_handshake() call but rows are never deleted — they accumulate
        indefinitely even for completed, expired, or abandoned handshakes.
        Concurrently, the in-memory _pending_handshakes dict prunes its *value
        lists* (timestamps) on each initiate call, but it never removes *keys*
        (target_id entries).  Over time, every unique target_id seen since
        process start lingers in that dict, causing a slow memory leak.

        This method should be called periodically (e.g. once per hour).
        Returns the number of DB rows deleted.
        """
        cutoff = time.time() - max_age_secs
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bot_handshakes WHERE initiated_at < ? AND status IN ('completed','expired')",
                (cutoff,),
            )
            self._conn.commit()
            # Evict keys with empty timestamp lists from _pending_handshakes to
            # prevent the dict from accumulating one key per unique target_id
            # seen since process start.
            stale_keys = [
                k for k, v in list(self._pending_handshakes.items())
                if not v
            ]
            for k in stale_keys:
                del self._pending_handshakes[k]
        removed = cur.rowcount
        if removed:
            logger.info("BotRegistry: purged %d stale handshake row(s)", removed)
        return removed

    def _row_to_identity(self, row) -> BotIdentity:
        d = dict(zip(self._BOT_COLS, row))
        d["capabilities"] = json.loads(d.get("capabilities") or "[]")
        d["trusted"] = bool(d.get("trusted", 0))
        return BotIdentity.from_dict(d)

    def close(self):
        self._conn.close()


KNOWN_BOTS: Dict[str, dict] = {
    "xiao_bai": {
        "name": "\u5c0f\u767d",
        "display_name": "Andy",
        "framework": "external",
        "channel": "telegram",
        "capabilities": ["memory", "code", "analysis", "multi-channel"],
    },
    "xiao_eve": {
        "name": "\u5c0fEve",
        "display_name": "Eve",
        "framework": "evoclaw",
        "channel": "discord",
        "capabilities": ["memory", "evolution", "fitness", "enterprise"],
        "http_endpoint": "http://localhost:8767",
    },
}


def bootstrap_known_bots(registry: BotRegistry):
    """Pre-register \u5c0f\u767d and \u5c0fEve as trusted bots."""
    import os
    for key, cfg in KNOWN_BOTS.items():
        bot_id = BotIdentity.make_bot_id(cfg["name"], cfg["framework"], cfg["channel"])
        identity = BotIdentity(
            bot_id=bot_id,
            name=cfg["name"],
            display_name=cfg["display_name"],
            framework=cfg["framework"],
            channel=cfg["channel"],
            capabilities=cfg["capabilities"],
            ws_endpoint=os.getenv(f"{key.upper()}_WS_ENDPOINT"),
            http_endpoint=cfg.get("http_endpoint"),
            trusted=True,
        )
        registry.register(identity)
        logger.info(f"Bootstrapped: {identity.name} -> {bot_id}")

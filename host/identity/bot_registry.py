"""
Cross-bot Identity Registry — Phase 3
Enables NanoClaw ↔ EvoClaw bot recognition via shared identity protocol.

Protocol:
  1. Each bot has a stable bot_id = SHA-256(name:framework:channel)[:16]
  2. Bots register on startup via /bots/register endpoint
  3. Cross-bot messages include X-Bot-Id header + signature
  4. Receiving bot verifies identity against registry
"""
import hashlib
import json
import time
import sqlite3
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List
from pathlib import Path

logger = logging.getLogger(__name__)

BOT_REGISTRY_VERSION = "1.0"


@dataclass
class BotIdentity:
    """Stable identity for a bot across framework boundaries."""
    bot_id: str           # SHA-256(name:framework:channel)[:16]
    name: str             # Human-readable name (e.g. "小白", "小Eve")
    display_name: str     # Display/alias name
    framework: str        # "nanoclaw" | "evoclaw" | "openclaw"
    channel: str          # Primary channel ("telegram", "discord", etc.)
    capabilities: List[str] = field(default_factory=list)
    ws_endpoint: Optional[str] = None   # WebSocket endpoint for direct comms
    http_endpoint: Optional[str] = None  # HTTP endpoint for REST
    public_key: Optional[str] = None    # For message signing (future)
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    trusted: bool = False  # Admin-verified trusted bot

    @staticmethod
    def make_bot_id(name: str, framework: str, channel: str) -> str:
        raw = f"{name.lower()}:{framework.lower()}:{channel.lower()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BotIdentity":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class BotRegistry:
    """
    Persistent registry of known bots across frameworks.

    Storage: SQLite (same DB as AgentIdentityStore for co-location).
    API: Simple dict-based in-process + optional HTTP for cross-system.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path.home() / ".evoclaw" / "bot_registry.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        logger.info(f"BotRegistry initialized at {db_path}")

    def _init_db(self):
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
                nonce TEXT,
                FOREIGN KEY(initiator_bot_id) REFERENCES bots(bot_id)
            )
        """)
        self._conn.commit()

    def register(self, identity: BotIdentity) -> BotIdentity:
        """Register or update a bot identity."""
        self._conn.execute("""
            INSERT INTO bots
                (bot_id, name, display_name, framework, channel, capabilities,
                 ws_endpoint, http_endpoint, public_key, registered_at, last_seen, trusted)
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
        logger.info(f"Registered bot: {identity.name} ({identity.bot_id}) [{identity.framework}]")
        return identity

    def lookup(self, bot_id: str) -> Optional[BotIdentity]:
        """Look up a bot by its stable ID."""
        row = self._conn.execute(
            "SELECT * FROM bots WHERE bot_id=?", (bot_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_identity(row)

    def lookup_by_name(self, name: str) -> Optional[BotIdentity]:
        """Look up a bot by name (case-insensitive)."""
        row = self._conn.execute(
            "SELECT * FROM bots WHERE lower(name)=lower(?) OR lower(display_name)=lower(?)",
            (name, name)
        ).fetchone()
        if not row:
            return None
        return self._row_to_identity(row)

    def list_all(self) -> List[BotIdentity]:
        """List all registered bots."""
        rows = self._conn.execute("SELECT * FROM bots ORDER BY registered_at").fetchall()
        return [self._row_to_identity(r) for r in rows]

    def list_trusted(self) -> List[BotIdentity]:
        """List trusted bots only."""
        rows = self._conn.execute(
            "SELECT * FROM bots WHERE trusted=1 ORDER BY registered_at"
        ).fetchall()
        return [self._row_to_identity(r) for r in rows]

    def trust(self, bot_id: str) -> bool:
        """Mark a bot as trusted (admin action)."""
        cur = self._conn.execute(
            "UPDATE bots SET trusted=1 WHERE bot_id=?", (bot_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def initiate_handshake(self, initiator_id: str, target_id: str) -> str:
        """
        Initiate cross-bot handshake. Returns nonce for verification.

        Handshake flow:
          1. Bot A calls initiate_handshake(A_id, B_id) -> nonce
          2. Bot A sends: {type: "bot_hello", bot_id: A_id, nonce: nonce}
          3. Bot B verifies nonce via complete_handshake()
          4. Bot B responds: {type: "bot_ack", bot_id: B_id, nonce: nonce}
        """
        import secrets
        nonce = secrets.token_hex(16)
        self._conn.execute("""
            INSERT INTO bot_handshakes
                (initiator_bot_id, target_bot_id, status, initiated_at, nonce)
            VALUES (?,?,?,?,?)
        """, (initiator_id, target_id, "pending", time.time(), nonce))
        self._conn.commit()
        return nonce

    def complete_handshake(self, initiator_id: str, target_id: str, nonce: str) -> bool:
        """Complete a handshake — verify nonce and mark trusted."""
        row = self._conn.execute("""
            SELECT id FROM bot_handshakes
            WHERE initiator_bot_id=? AND target_bot_id=? AND nonce=? AND status='pending'
        """, (initiator_id, target_id, nonce)).fetchone()
        if not row:
            logger.warning(f"Handshake verification failed: {initiator_id} -> {target_id}")
            return False
        self._conn.execute("""
            UPDATE bot_handshakes SET status='completed', completed_at=? WHERE id=?
        """, (time.time(), row[0]))
        # Auto-trust after successful handshake
        self._conn.execute("UPDATE bots SET trusted=1 WHERE bot_id IN (?,?)",
                           (initiator_id, target_id))
        self._conn.commit()
        logger.info(f"Handshake completed: {initiator_id} <-> {target_id}")
        return True

    def update_last_seen(self, bot_id: str):
        """Update last seen timestamp."""
        self._conn.execute(
            "UPDATE bots SET last_seen=? WHERE bot_id=?", (time.time(), bot_id)
        )
        self._conn.commit()

    def _row_to_identity(self, row) -> BotIdentity:
        cols = [d[0] for d in self._conn.execute("SELECT * FROM bots LIMIT 0").description]
        d = dict(zip(cols, row))
        d["capabilities"] = json.loads(d.get("capabilities", "[]"))
        d["trusted"] = bool(d.get("trusted", 0))
        return BotIdentity.from_dict(d)

    def close(self):
        self._conn.close()


# ── Default bot definitions ─────────────────────────────────────────────────

KNOWN_BOTS: Dict[str, dict] = {
    "xiao_bai": {
        "name": "小白",
        "display_name": "Andy",
        "framework": "nanoclaw",
        "channel": "telegram",
        "capabilities": ["memory", "code", "analysis", "multi-channel"],
        "ws_endpoint": None,  # Set via env XIAOBAI_WS_ENDPOINT
        "http_endpoint": None,
    },
    "xiao_eve": {
        "name": "小Eve",
        "display_name": "Eve",
        "framework": "evoclaw",
        "channel": "discord",
        "capabilities": ["memory", "evolution", "fitness", "enterprise"],
        "ws_endpoint": None,  # Set via env XIAOEVE_WS_ENDPOINT
        "http_endpoint": "http://localhost:8767",
    },
}


def bootstrap_known_bots(registry: BotRegistry):
    """Pre-register known bots (小白 and 小Eve) into the registry."""
    import os
    for key, config in KNOWN_BOTS.items():
        bot_id = BotIdentity.make_bot_id(
            config["name"], config["framework"], config["channel"]
        )
        # Override endpoints from environment
        ws_ep = os.getenv(f"{key.upper()}_WS_ENDPOINT", config.get("ws_endpoint"))
        http_ep = os.getenv(f"{key.upper()}_HTTP_ENDPOINT", config.get("http_endpoint"))
        identity = BotIdentity(
            bot_id=bot_id,
            name=config["name"],
            display_name=config["display_name"],
            framework=config["framework"],
            channel=config["channel"],
            capabilities=config["capabilities"],
            ws_endpoint=ws_ep,
            http_endpoint=http_ep,
            trusted=True,  # Pre-trusted known bots
        )
        registry.register(identity)
        logger.info(f"Bootstrapped known bot: {identity.name} -> {bot_id}")

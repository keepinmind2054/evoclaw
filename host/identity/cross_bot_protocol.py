"""
Cross-bot Message Protocol — Phase 3 (crossbot/1.0)

Message envelope:
{
  "protocol":    "crossbot/1.0",
  "from_bot_id": "abc123...",
  "to_bot_id":   "def456..." | null,
  "msg_id":      "uuid4",
  "timestamp":   1234567890.123,
  "type":        "hello|ack|memory_share|task_delegate|status|ping|pong",
  "payload":     {...},
  "signature":   "hmac_sha256..." | null
}
"""
import json
import time
import uuid
import hmac
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional, Any, Dict, Callable

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "crossbot/1.0"

MSG_HELLO         = "hello"
MSG_ACK           = "ack"
MSG_MEMORY_SHARE  = "memory_share"
MSG_TASK_DELEGATE = "task_delegate"
MSG_STATUS        = "status"
MSG_PING          = "ping"
MSG_PONG          = "pong"

_ALLOWED_MSG_TYPES = frozenset({"hello", "ack", "message", "heartbeat", "query", "response"})


@dataclass
class CrossBotMessage:
    from_bot_id: str
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    to_bot_id: Optional[str] = None
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    protocol: str = PROTOCOL_VERSION
    signature: Optional[str] = None

    def _signing_body(self) -> str:
        ts_ms = int(self.timestamp * 1000)  # integer ms, no float precision issues
        return f"{self.msg_id}:{self.from_bot_id}:{ts_ms}:{json.dumps(self.payload, sort_keys=True)}"

    def sign(self, secret: str) -> "CrossBotMessage":
        body = self._signing_body()
        self.signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return self

    def verify(self, secret: str) -> bool:
        if not self.signature:
            return False
        body = self._signing_body()
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.signature, expected)

    def to_json(self) -> str:
        return json.dumps({
            "protocol": self.protocol,
            "from_bot_id": self.from_bot_id,
            "to_bot_id": self.to_bot_id,
            "msg_id": self.msg_id,
            "timestamp_ms": int(self.timestamp * 1000),  # integer ms, not float
            "type": self.type,
            "payload": self.payload,
            "signature": self.signature,
        })

    @classmethod
    def from_json(cls, data: str) -> "CrossBotMessage":
        d = json.loads(data)
        # Support both old float "timestamp" and new integer "timestamp_ms"
        if "timestamp_ms" in d:
            ts = d["timestamp_ms"] / 1000.0
        else:
            ts = d.get("timestamp", time.time())
        return cls(
            from_bot_id=d["from_bot_id"],
            to_bot_id=d.get("to_bot_id"),
            msg_id=d.get("msg_id", str(uuid.uuid4())),
            timestamp=ts,
            type=d["type"],
            payload=d.get("payload", {}),
            protocol=d.get("protocol", PROTOCOL_VERSION),
            signature=d.get("signature"),
        )


class CrossBotProtocol:
    """Handles cross-bot communication for NanoClaw <-> EvoClaw."""

    def __init__(self, my_bot_id: str, registry=None, secret: Optional[str] = None):
        self.my_bot_id = my_bot_id
        self.registry = registry
        self.secret = secret
        self._handlers: Dict[str, Callable] = {}

    def make_hello(self, target_bot_id: Optional[str] = None, nonce: Optional[str] = None) -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id, to_bot_id=target_bot_id,
            type=MSG_HELLO, payload={"nonce": nonce, "version": PROTOCOL_VERSION}
        )

    def make_ack(self, original: CrossBotMessage) -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id, to_bot_id=original.from_bot_id,
            type=MSG_ACK,
            payload={"ack_msg_id": original.msg_id, "nonce": original.payload.get("nonce")}
        )

    def make_memory_share(self, target_bot_id: str, key: str, value: Any, scope: str = "shared") -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id, to_bot_id=target_bot_id,
            type=MSG_MEMORY_SHARE, payload={"key": key, "value": value, "scope": scope}
        )

    def make_task_delegate(self, target_bot_id: str, task: str, context: Dict) -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id, to_bot_id=target_bot_id,
            type=MSG_TASK_DELEGATE, payload={"task": task, "context": context}
        )

    def make_ping(self) -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id, type=MSG_PING, payload={"ts": time.time()}
        )

    def on(self, msg_type: str):
        def decorator(fn):
            self._handlers[msg_type] = fn
            return fn
        return decorator

    def handle(self, raw: str) -> Optional[CrossBotMessage]:
        try:
            msg = CrossBotMessage.from_json(raw)
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return None
        if not msg.protocol.startswith("crossbot/"):
            logger.warning(f"Unknown protocol: {msg.protocol}")
            return None
        if msg.type not in _ALLOWED_MSG_TYPES:
            logger.warning("Rejected unknown message type: %s", msg.type)
            return None
        if self.registry:
            self.registry.update_last_seen(msg.from_bot_id)
        handler = self._handlers.get(msg.type)
        if handler:
            return handler(msg)
        if msg.type == MSG_PING:
            return CrossBotMessage(
                from_bot_id=self.my_bot_id, to_bot_id=msg.from_bot_id,
                type=MSG_PONG, payload={"ts": time.time()}
            )
        if msg.type == MSG_HELLO:
            logger.info(f"Hello from: {msg.from_bot_id}")
            return self.make_ack(msg)
        return None

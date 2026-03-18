"""
Cross-bot Message Protocol — Phase 3

Defines the message envelope format for bot-to-bot communication
across NanoClaw <-> EvoClaw boundaries.

Message format:
{
  "protocol": "crossbot/1.0",
  "from_bot_id": "abc123...",
  "to_bot_id":   "def456..." | null (broadcast),
  "msg_id":      "uuid4",
  "timestamp":   1234567890.123,
  "type":        "hello" | "ack" | "memory_share" | "task_delegate" | "status",
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
from typing import Optional, Any, Dict

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "crossbot/1.0"

# Message types
MSG_HELLO         = "hello"           # Initial greeting
MSG_ACK           = "ack"             # Acknowledgment
MSG_MEMORY_SHARE  = "memory_share"    # Share memory across bots
MSG_TASK_DELEGATE = "task_delegate"   # Delegate a task to another bot
MSG_STATUS        = "status"          # Status update
MSG_PING          = "ping"            # Keepalive
MSG_PONG          = "pong"            # Keepalive response


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

    def sign(self, secret: str) -> "CrossBotMessage":
        """Sign message with HMAC-SHA256."""
        body = self._signing_body()
        self.signature = hmac.new(
            secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        return self

    def verify(self, secret: str) -> bool:
        """Verify message signature."""
        if not self.signature:
            return False
        body = self._signing_body()
        expected = hmac.new(
            secret.encode(), body.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(self.signature, expected)

    def _signing_body(self) -> str:
        return f"{self.msg_id}:{self.from_bot_id}:{self.timestamp}:{json.dumps(self.payload, sort_keys=True)}"

    def to_json(self) -> str:
        return json.dumps({
            "protocol": self.protocol,
            "from_bot_id": self.from_bot_id,
            "to_bot_id": self.to_bot_id,
            "msg_id": self.msg_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": self.payload,
            "signature": self.signature,
        })

    @classmethod
    def from_json(cls, data: str) -> "CrossBotMessage":
        d = json.loads(data)
        return cls(
            from_bot_id=d["from_bot_id"],
            to_bot_id=d.get("to_bot_id"),
            msg_id=d.get("msg_id", str(uuid.uuid4())),
            timestamp=d.get("timestamp", time.time()),
            type=d["type"],
            payload=d.get("payload", {}),
            protocol=d.get("protocol", PROTOCOL_VERSION),
            signature=d.get("signature"),
        )


class CrossBotProtocol:
    """
    Handles cross-bot communication protocol.

    Usage:
        protocol = CrossBotProtocol(my_bot_id, registry)

        # Send hello to another bot
        msg = protocol.make_hello(target_bot_id="def456...")
        await send_to_bot(target_endpoint, msg.to_json())

        # Handle incoming message
        response = protocol.handle(incoming_json)
        if response:
            await send_back(response.to_json())
    """

    def __init__(self, my_bot_id: str, registry=None, secret: Optional[str] = None):
        self.my_bot_id = my_bot_id
        self.registry = registry
        self.secret = secret
        self._handlers: Dict[str, Any] = {}

    def make_hello(self, target_bot_id: Optional[str] = None, nonce: Optional[str] = None) -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id,
            to_bot_id=target_bot_id,
            type=MSG_HELLO,
            payload={"nonce": nonce, "version": PROTOCOL_VERSION},
        )

    def make_ack(self, original_msg: CrossBotMessage) -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id,
            to_bot_id=original_msg.from_bot_id,
            type=MSG_ACK,
            payload={"ack_msg_id": original_msg.msg_id, "nonce": original_msg.payload.get("nonce")},
        )

    def make_memory_share(self, target_bot_id: str, key: str, value: Any, scope: str = "shared") -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id,
            to_bot_id=target_bot_id,
            type=MSG_MEMORY_SHARE,
            payload={"key": key, "value": value, "scope": scope},
        )

    def make_task_delegate(self, target_bot_id: str, task: str, context: Dict) -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id,
            to_bot_id=target_bot_id,
            type=MSG_TASK_DELEGATE,
            payload={"task": task, "context": context},
        )

    def make_ping(self) -> CrossBotMessage:
        return CrossBotMessage(
            from_bot_id=self.my_bot_id,
            type=MSG_PING,
            payload={"ts": time.time()},
        )

    def on(self, msg_type: str):
        """Decorator to register a message handler."""
        def decorator(fn):
            self._handlers[msg_type] = fn
            return fn
        return decorator

    def handle(self, raw: str) -> Optional[CrossBotMessage]:
        """Parse and dispatch an incoming cross-bot message."""
        try:
            msg = CrossBotMessage.from_json(raw)
        except Exception as e:
            logger.error(f"Failed to parse cross-bot message: {e}")
            return None

        # Verify protocol version
        if not msg.protocol.startswith("crossbot/"):
            logger.warning(f"Unknown protocol: {msg.protocol}")
            return None

        # Update registry last_seen
        if self.registry:
            self.registry.update_last_seen(msg.from_bot_id)

        # Dispatch to handler
        handler = self._handlers.get(msg.type)
        if handler:
            return handler(msg)

        # Default handlers
        if msg.type == MSG_PING:
            return CrossBotMessage(
                from_bot_id=self.my_bot_id,
                to_bot_id=msg.from_bot_id,
                type=MSG_PONG,
                payload={"ts": time.time()},
            )
        elif msg.type == MSG_HELLO:
            logger.info(f"Bot hello from: {msg.from_bot_id}")
            return self.make_ack(msg)

        logger.debug(f"No handler for message type: {msg.type}")
        return None

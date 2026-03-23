# Cross-bot Identity Protocol v1.0

## Overview
Enables stable identity recognition between external bots (小白/Andy) and
EvoClaw-hosted (小Eve) bots across framework boundaries.

## Bot ID Generation
```
bot_id = SHA-256("{name}:{framework}:{channel}")[:16]
```

Examples:
- 小白: `SHA-256("小白:external:telegram")[:16]`
- 小Eve: `SHA-256("小eve:evoclaw:discord")[:16]`

## Message Envelope (crossbot/1.0)
```json
{
  "protocol":    "crossbot/1.0",
  "from_bot_id": "abc123def456...",
  "to_bot_id":   "789xyz...",
  "msg_id":      "uuid4",
  "timestamp":   1234567890.123,
  "type":        "hello|ack|memory_share|task_delegate|ping|pong",
  "payload":     {},
  "signature":   "hmac_sha256_optional"
}
```

## Handshake Flow
```
Bot A                          Bot B
 |--[hello, nonce=N]----------->|
 |<--[ack, ack_msg_id, nonce=N]-|
 |  (both now trusted)          |
```

## Message Types
| Type            | Direction | Description                         |
|-----------------|-----------|-------------------------------------|
| hello           | A -> B    | Initial greeting with nonce         |
| ack             | B -> A    | Acknowledgment, completes handshake |
| memory_share    | Any       | Share a memory entry cross-bot      |
| task_delegate   | Any       | Delegate a task to another bot      |
| status          | Any       | Status broadcast                    |
| ping / pong     | Any       | Keepalive                           |

## Registry
Bots are stored in `~/.evoclaw/bot_registry.db` (SQLite).
Both 小白 and 小Eve are pre-registered as trusted on startup via `bootstrap_known_bots()`.

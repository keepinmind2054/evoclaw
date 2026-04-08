"""
SDK WebSocket API -- Phase 2 of UnifiedClaw architecture

External WebSocket API allowing SDK clients, CLI tools, and monitoring
systems to connect and interact with the Gateway.

Port: 8767 (configurable via SDK_API_PORT env var)

Endpoints:
  ws://localhost:8767/ws/sdk      <- SDK clients (query memory, submit tasks)
  ws://localhost:8767/ws/monitor  <- Read-only monitoring

Protocol (JSON):
  Client -> Server:
    {"type": "memory_query",  "query": "...", "agent_id": "..."}
    {"type": "memory_write",  "content": "...", "agent_id": "...", "scope": "shared"}
    {"type": "agent_list"}
    {"type": "system_status"}
    {"type": "task_submit",   "group": "...", "message": "..."}
    {"type": "ping"}

  Server -> Client:
    {"type": "memory_result",  "memories": [...], "query": "..."}
    {"type": "agent_list",     "agents": [...]}
    {"type": "system_status",  "status": {...}}
    {"type": "task_ack",       "task_id": "..."}
    {"type": "event",          "event": "...", "data": {...}}
    {"type": "pong"}
    {"type": "error",          "code": "...", "message": "..."}

Usage:
    sdk_api = SdkApi(memory_bus, identity_store, port=8767)
    asyncio.create_task(sdk_api.start())

Example client (Python):
    import websockets, json, asyncio
    
    async def query_memory():
        async with websockets.connect("ws://localhost:8767") as ws:
            await ws.send(json.dumps({
                "type": "memory_query",
                "query": "user preferences",
                "agent_id": "mybot"
            }))
            result = json.loads(await ws.recv())
            print(result["memories"])
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import TYPE_CHECKING, Optional, Set

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .memory.memory_bus import MemoryBus
    from .identity.agent_identity import AgentIdentityStore


_VALID_SCOPES = frozenset({"private", "shared", "project"})

class SdkApi:
    """
    External WebSocket SDK API for the UnifiedClaw Gateway.

    Allows external tools to query memory, check agent status,
    and submit tasks without needing direct database access.

    Authentication: Required bearer token via SDK_API_TOKEN env var.
    If not set, all connections are rejected and a startup warning is logged.
    Set SDK_API_NO_AUTH=1 to explicitly allow unauthenticated access (dev only).
    """

    DEFAULT_PORT = 8767
    MAX_CONNECTIONS = 50
    PING_INTERVAL = 30  # seconds

    # BUG-18C-03 (MEDIUM): No per-connection message rate limit — an
    # authenticated client can spam memory_write / memory_query at unlimited
    # throughput, flooding SQLite and starving other coroutines.
    # Cap at MAX_MSG_PER_WINDOW messages in a RATE_WINDOW_SECS rolling window.
    MAX_MSG_PER_WINDOW = 60   # messages
    RATE_WINDOW_SECS   = 10   # seconds

    def __init__(
        self,
        memory_bus: "MemoryBus",
        identity_store: "AgentIdentityStore",
        port: Optional[int] = None,
        token: Optional[str] = None,
        bot_registry=None,
    ):
        self._memory_bus = memory_bus
        self._identity_store = identity_store
        self._port = port or int(os.environ.get("SDK_API_PORT", self.DEFAULT_PORT))
        _raw_token = token or os.environ.get("SDK_API_TOKEN", "")
        self._token = _raw_token if _raw_token else None
        self._no_auth = os.environ.get("SDK_API_NO_AUTH", "") == "1"
        self._connections: Set = set()
        self._running = False
        self._task_submit_callback = None
        self._bot_registry = bot_registry  # Phase 3: BotRegistry
        self._handlers = {
            "ping":           self._handle_ping,
            "memory_query":   self._handle_memory_query,
            "memory_write":   self._handle_memory_write,
            "agent_list":     self._handle_agent_list,
            "bot_register":   self._handle_bot_register,
            "bot_lookup":     self._handle_bot_lookup,
            "bot_list":       self._handle_bot_list,
            "bot_handshake":  self._handle_bot_handshake,
            "system_status":  self._handle_system_status,
            "task_submit":    self._handle_task_submit,
        }

    async def start(self):
        """Start the SDK API WebSocket server."""
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.warning("SdkApi: websockets not installed. Run: pip install websockets")
            return

        self._running = True
        _host = os.environ.get("SDK_API_HOST", "127.0.0.1")
        if _host not in ("127.0.0.1", "localhost"):
            logger.warning("SDK API bound to %s — ensure firewall rules are in place", _host)
        if not self._token:
            if self._no_auth:
                logger.warning(
                    "SDK_API_TOKEN is not set — SDK API authentication is DISABLED "
                    "(SDK_API_NO_AUTH=1 explicit opt-out active)"
                )
            else:
                logger.warning(
                    "SDK_API_TOKEN is not set — SDK API authentication is DISABLED; "
                    "all connections will be rejected. Set SDK_API_TOKEN or use "
                    "SDK_API_NO_AUTH=1 to allow unauthenticated access."
                )
        logger.info(f"SdkApi starting on ws://{_host}:{self._port}")
        async with websockets.serve(
            self._handle_connection,
            _host,
            self._port,
            max_size=1_048_576,  # 1MB max message
        ):
            while self._running:
                await asyncio.sleep(1)

    async def stop(self):
        self._running = False
        for ws in list(self._connections):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()

    async def broadcast_event(self, event: str, data: dict):
        """Broadcast an event to all connected SDK clients."""
        if not self._connections:
            return
        msg = json.dumps({"type": "event", "event": event, "data": data, "ts": time.time()})
        dead = set()
        for ws in self._connections:
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        self._connections -= dead

    async def _handle_connection(self, websocket, path: str = "/"):
        """Handle an incoming SDK client connection."""
        if len(self._connections) >= self.MAX_CONNECTIONS:
            await websocket.send(json.dumps({
                "type": "error", "code": "too_many_connections",
                "message": f"Max {self.MAX_CONNECTIONS} concurrent connections"
            }))
            # BUG-P27A-SDKAPI-1 FIX: explicitly close the WebSocket after
            # rejecting a connection.  Without this the rejected socket stays
            # open indefinitely (waiting for the remote to close it), holding a
            # file descriptor and an OS-level socket resource even though it is
            # not tracked in self._connections and will never send another
            # message.  Closing proactively returns the resource immediately.
            try:
                await websocket.close()
            except Exception:
                pass
            return

        self._connections.add(websocket)
        remote = getattr(websocket, "remote_address", "unknown")
        logger.info(f"SdkApi: client connected from {remote}")

        # BUG-SDK-01 (CRITICAL): Auth was checked per-message — an unauthenticated
        # client received an error reply but the loop continued, allowing
        # unlimited retry attempts without disconnecting.  Authenticate once on
        # the first message and close the socket on failure.
        #
        # Security fix (#441): When SDK_API_TOKEN is unset, reject ALL connections
        # unless SDK_API_NO_AUTH=1 is explicitly set for development use.
        if not self._token and not self._no_auth:
            await self._send_error(websocket, "unauthorized", "SDK API token not configured — connections rejected")
            await websocket.close()
            self._connections.discard(websocket)
            return
        _authenticated = not bool(self._token)  # True only when no-auth mode is active

        # BUG-18C-03 (MEDIUM): Per-connection rate limiting.
        # Track timestamps of recent messages in a deque; prune entries older
        # than RATE_WINDOW_SECS and reject when the count exceeds the cap.
        _msg_times: deque = deque()

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_error(websocket, "invalid_json", "Invalid JSON")
                    continue

                if not _authenticated:
                    if msg.get("token") != self._token:
                        await self._send_error(websocket, "unauthorized", "Invalid or missing token")
                        await websocket.close()
                        return
                    _authenticated = True
                    # Strip token from msg before dispatching to handlers so it
                    # is not accidentally echoed back in error responses.
                    msg.pop("token", None)

                # BUG-18C-03 (MEDIUM): Enforce per-connection message rate limit.
                now = time.time()
                while _msg_times and now - _msg_times[0] > self.RATE_WINDOW_SECS:
                    _msg_times.popleft()
                if len(_msg_times) >= self.MAX_MSG_PER_WINDOW:
                    await self._send_error(
                        websocket, "rate_limited",
                        f"Rate limit: max {self.MAX_MSG_PER_WINDOW} messages per {self.RATE_WINDOW_SECS}s"
                    )
                    continue
                _msg_times.append(now)

                await self._dispatch(websocket, msg)

        except Exception as e:
            logger.debug(f"SdkApi: connection closed: {e}")
        finally:
            self._connections.discard(websocket)
            logger.debug(f"SdkApi: client disconnected from {remote}")

    async def _dispatch(self, websocket, msg: dict):
        """Route incoming message to appropriate handler."""
        msg_type = msg.get("type", "")
        handler = self._handlers.get(msg_type)
        if handler:
            await handler(websocket, msg)
        else:
            await self._send_error(websocket, "unknown_type", f"Unknown message type: {msg_type}")

    async def _handle_ping(self, websocket, msg: dict):
        await websocket.send(json.dumps({"type": "pong", "ts": time.time()}))

    async def _handle_memory_query(self, websocket, msg: dict):
        """Query memories for an agent."""
        query = msg.get("query", "")
        agent_id = msg.get("agent_id", "")
        # BUG-SDK-02 (MEDIUM): Unbounded `k` allows a client to request
        # millions of results, exhausting memory.  Cap at 100.
        try:
            k = max(1, min(int(msg.get("k", 5)), 100))
        except (TypeError, ValueError):
            k = 5
        project = msg.get("project", "")

        if not query or not agent_id:
            await self._send_error(websocket, "missing_params", "query and agent_id required")
            return

        try:
            memories = await self._memory_bus.recall(query, agent_id=agent_id, k=k, project=project)
            await websocket.send(json.dumps({
                "type": "memory_result",
                "query": query,
                "agent_id": agent_id,
                "memories": [
                    {
                        "id": m.memory_id,
                        "content": m.content,
                        "score": round(m.score, 3),
                        "source": m.source,
                        "scope": m.scope,
                        "age_hours": round(m.age_hours, 1),
                    }
                    for m in memories
                ],
                "count": len(memories),
            }))
        except Exception as e:
            # BUG-SDK-03 (MEDIUM): Exception message leaked verbatim — may
            # contain internal paths or DB schema.  Log the full error
            # server-side; return only a generic message to the client.
            logger.error("SdkApi: memory_query error: %s", e, exc_info=True)
            await self._send_error(websocket, "memory_error", "Internal error during memory query")

    # BUG-SDK-04 (HIGH): No size limit on memory write content — an attacker
    # can write multi-MB strings and exhaust memory / disk.
    _MAX_MEMORY_CONTENT = 64 * 1024  # 64 KB

    async def _handle_memory_write(self, websocket, msg: dict):
        """Write a memory entry."""
        content = msg.get("content", "")
        agent_id = msg.get("agent_id", "")
        scope = msg.get("scope", "shared")
        project = msg.get("project", "")
        # BUG-SDK-02 (MEDIUM): importance not validated / clamped.
        try:
            importance = max(0.0, min(1.0, float(msg.get("importance", 0.5))))
        except (TypeError, ValueError):
            importance = 0.5

        if not content or not agent_id:
            await self._send_error(websocket, "missing_params", "content and agent_id required")
            return

        # BUG-18C-02 (MEDIUM): scope was never validated against the allowed set
        # {"private", "shared", "project"}.  An invalid scope (e.g. "admin",
        # "", or a random string) was silently written to the DB and became
        # permanently invisible to all search queries (no WHERE clause branch
        # matches it), wasting storage and confusing callers who received a
        # successful memory_ack.  Reject invalid scopes up-front.
        if scope not in _VALID_SCOPES:
            await self._send_error(
                websocket, "invalid_scope",
                f"scope must be one of: {', '.join(sorted(_VALID_SCOPES))}"
            )
            return

        # BUG-SDK-04 (HIGH): Enforce content size limit.
        if len(content) > self._MAX_MEMORY_CONTENT:
            await self._send_error(
                websocket, "content_too_large",
                f"Content exceeds {self._MAX_MEMORY_CONTENT} byte limit"
            )
            return

        try:
            memory_id = await self._memory_bus.remember(
                content, agent_id=agent_id, scope=scope,
                project=project, importance=importance
            )
            await websocket.send(json.dumps({
                "type": "memory_ack",
                "memory_id": memory_id,
                "scope": scope,
            }))
        except Exception as e:
            # BUG-SDK-03 (MEDIUM): Do not leak internal error detail.
            logger.error("SdkApi: memory_write error: %s", e, exc_info=True)
            await self._send_error(websocket, "memory_error", "Internal error during memory write")

    # BUG-18C-05 (MEDIUM): agent_list previously returned ALL agents in a
    # single WebSocket frame.  With many agents this can exhaust memory when
    # serialising the list and may exceed the 1 MB max_size frame limit.
    # Cap the response at _MAX_AGENT_LIST entries and honour a caller-supplied
    # "limit" (clamped to the same cap).
    _MAX_AGENT_LIST = 500

    async def _handle_agent_list(self, websocket, msg: dict):
        """List all known agents (paginated, max _MAX_AGENT_LIST per call)."""
        project = msg.get("project", "")
        # BUG-18C-05 (MEDIUM): apply a hard cap so the serialised response
        # cannot exceed the WebSocket max_size limit.
        try:
            limit = max(1, min(int(msg.get("limit", self._MAX_AGENT_LIST)), self._MAX_AGENT_LIST))
        except (TypeError, ValueError):
            limit = self._MAX_AGENT_LIST
        try:
            agents = self._identity_store.list_agents(project=project)
            page = agents[:limit]
            await websocket.send(json.dumps({
                "type": "agent_list",
                "agents": [
                    {
                        "agent_id": a.agent_id,
                        "name": a.name,
                        "project": a.project,
                        "channel": a.channel,
                        "skills": a.skills,
                        "message_count": a.message_count,
                        "last_active": a.last_active,
                    }
                    for a in page
                ],
                "count": len(page),
                "total": len(agents),
                "truncated": len(agents) > limit,
            }))
        except Exception as e:
            # p17c BUG-FIX (MEDIUM): do not leak internal error detail to clients.
            logger.error("SdkApi: agent_list error: %s", e, exc_info=True)
            await self._send_error(websocket, "identity_error", "Internal error listing agents")

    async def _handle_system_status(self, websocket, msg: dict):
        """Return system status."""
        try:
            memory_status = self._memory_bus.status()
            try:
                # p17c BUG-FIX (HIGH): the previous code called
                # `with self._identity_store._lock:` — a threading.Lock — directly
                # inside an async function.  threading.Lock.acquire() is a blocking
                # call: if the lock is contended (e.g. another thread is writing to
                # the identity store) the entire asyncio event loop thread is blocked
                # for the duration, stalling every other coroutine.  Fix: run the
                # DB read in a thread-pool executor so the lock acquisition blocks
                # only the executor thread, not the event loop.
                def _count_agents():
                    with self._identity_store._lock:
                        row = self._identity_store._conn.execute(
                            "SELECT COUNT(*) FROM agent_identities"
                        ).fetchone()
                    return row[0] if row else 0

                agent_count = await asyncio.get_running_loop().run_in_executor(
                    None, _count_agents
                )
            except Exception:
                agent_count = -1
            await websocket.send(json.dumps({
                "type": "system_status",
                "status": {
                    "memory": memory_status,
                    "agents": agent_count,
                    "sdk_connections": len(self._connections),
                    "ts": time.time(),
                    "phase": "2",
                },
            }))
        except Exception as e:
            # p17c BUG-FIX (MEDIUM): do not leak internal error detail.
            logger.error("SdkApi: system_status error: %s", e, exc_info=True)
            await self._send_error(websocket, "status_error", "Internal error fetching system status")

    # BUG-SDK-05 (HIGH): No size limit on task message content.
    _MAX_TASK_MESSAGE = 32 * 1024  # 32 KB

    async def _handle_task_submit(self, websocket, msg: dict):
        """Submit a task to a group (if callback registered)."""
        group = msg.get("group", "")
        message = msg.get("message", "")

        if not group or not message:
            await self._send_error(websocket, "missing_params", "group and message required")
            return

        # BUG-SDK-05 (HIGH): Enforce message size limit.
        if len(message) > self._MAX_TASK_MESSAGE:
            await self._send_error(
                websocket, "message_too_large",
                f"Message exceeds {self._MAX_TASK_MESSAGE} byte limit"
            )
            return

        # Validate group is registered
        from host import db as _db
        registered = [g.get("folder") for g in _db.get_all_registered_groups()]
        if group not in registered:
            await self._send_error(websocket, "invalid_group", f"Group '{group}' is not registered")
            return

        if self._task_submit_callback:
            try:
                task_id = await self._task_submit_callback(group, message)
                await websocket.send(json.dumps({"type": "task_ack", "task_id": task_id, "group": group}))
            except Exception as e:
                # BUG-SDK-03 (MEDIUM): Do not leak internal error detail.
                logger.error("SdkApi: task_submit error: %s", e, exc_info=True)
                await self._send_error(websocket, "task_error", "Internal error during task submission")
        else:
            await self._send_error(websocket, "not_configured", "Task submission not configured")

    # ── Phase 3: Bot Registry handlers ──────────────────────────────────────

    async def _handle_bot_register(self, websocket, msg: dict):
        """Register a bot identity in the registry."""
        if not self._bot_registry:
            await self._send_error(websocket, "not_configured", "BotRegistry not initialized")
            return
        try:
            from .identity.bot_registry import BotIdentity
            required = ("name", "display_name", "framework", "channel")
            for field_name in required:
                if not msg.get(field_name):
                    await self._send_error(websocket, "missing_params", f"Missing field: {field_name}")
                    return
            bot_id = BotIdentity.make_bot_id(msg["name"], msg["framework"], msg["channel"])
            identity = BotIdentity(
                bot_id=bot_id,
                name=msg["name"],
                display_name=msg["display_name"],
                framework=msg["framework"],
                channel=msg["channel"],
                capabilities=msg.get("capabilities", []),
                ws_endpoint=msg.get("ws_endpoint"),
                http_endpoint=msg.get("http_endpoint"),
                trusted=False,
            )
            self._bot_registry.register(identity)
            await websocket.send(json.dumps({
                "type": "bot_registered",
                "bot_id": bot_id,
                "name": identity.name,
            }))
        except Exception as e:
            # BUG-18C-01 (HIGH): str(e) leaks internal exception details
            # (DB paths, schema, stack frames) to the remote client.
            logger.error("SdkApi: bot_register error: %s", e, exc_info=True)
            await self._send_error(websocket, "bot_register_error", "Internal error during bot registration")

    async def _handle_bot_lookup(self, websocket, msg: dict):
        """Look up a bot by ID or name."""
        if not self._bot_registry:
            await self._send_error(websocket, "not_configured", "BotRegistry not initialized")
            return
        try:
            bot_id = msg.get("bot_id")
            name = msg.get("name")
            if bot_id:
                identity = self._bot_registry.lookup(bot_id)
            elif name:
                identity = self._bot_registry.lookup_by_name(name)
            else:
                await self._send_error(websocket, "missing_params", "bot_id or name required")
                return
            if identity:
                await websocket.send(json.dumps({"type": "bot_identity", "bot": identity.to_dict()}))
            else:
                await websocket.send(json.dumps({"type": "bot_not_found", "bot_id": bot_id, "name": name}))
        except Exception as e:
            # BUG-18C-01 (HIGH): str(e) leaks internal exception details.
            logger.error("SdkApi: bot_lookup error: %s", e, exc_info=True)
            await self._send_error(websocket, "bot_lookup_error", "Internal error during bot lookup")

    async def _handle_bot_list(self, websocket, msg: dict):
        """List all registered bots."""
        if not self._bot_registry:
            await self._send_error(websocket, "not_configured", "BotRegistry not initialized")
            return
        try:
            trusted_only = msg.get("trusted_only", False)
            bots = self._bot_registry.list_trusted() if trusted_only else self._bot_registry.list_all()
            await websocket.send(json.dumps({
                "type": "bot_list",
                "bots": [b.to_dict() for b in bots],
                "count": len(bots),
            }))
        except Exception as e:
            # BUG-18C-01 (HIGH): str(e) leaks internal exception details.
            logger.error("SdkApi: bot_list error: %s", e, exc_info=True)
            await self._send_error(websocket, "bot_list_error", "Internal error listing bots")

    async def _handle_bot_handshake(self, websocket, msg: dict):
        """Initiate or complete a cross-bot handshake."""
        if not self._bot_registry:
            await self._send_error(websocket, "not_configured", "BotRegistry not initialized")
            return
        try:
            action = msg.get("action", "initiate")
            if action == "initiate":
                initiator_id = msg.get("initiator_id", "")
                target_id = msg.get("target_id", "")
                if not initiator_id or not target_id:
                    await self._send_error(websocket, "missing_params", "initiator_id and target_id required")
                    return
                nonce = self._bot_registry.initiate_handshake(initiator_id, target_id)
                await websocket.send(json.dumps({
                    "type": "bot_handshake_initiated",
                    "initiator_id": initiator_id,
                    "target_id": target_id,
                    "nonce": nonce,
                }))
            elif action == "complete":
                initiator_id = msg.get("initiator_id", "")
                target_id = msg.get("target_id", "")
                nonce = msg.get("nonce", "")
                if not all([initiator_id, target_id, nonce]):
                    await self._send_error(websocket, "missing_params", "initiator_id, target_id, nonce required")
                    return
                success = self._bot_registry.complete_handshake(initiator_id, target_id, nonce)
                await websocket.send(json.dumps({
                    "type": "bot_handshake_result",
                    "success": success,
                    "initiator_id": initiator_id,
                    "target_id": target_id,
                }))
            else:
                await self._send_error(websocket, "unknown_action", f"Unknown handshake action: {action}")
        except Exception as e:
            # BUG-18C-01 (HIGH): str(e) leaks internal exception details.
            logger.error("SdkApi: bot_handshake error: %s", e, exc_info=True)
            await self._send_error(websocket, "bot_handshake_error", "Internal error during bot handshake")

    @staticmethod
    async def _send_error(websocket, code: str, message: str):
        await websocket.send(json.dumps({"type": "error", "code": code, "message": message}))

    def on_task_submit(self, callback):
        """Register callback for task submissions from SDK clients."""
        self._task_submit_callback = callback
        return callback

    @property
    def port(self) -> int:
        return self._port

    @property
    def connection_count(self) -> int:
        return len(self._connections)

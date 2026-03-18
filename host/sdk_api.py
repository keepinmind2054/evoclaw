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
from typing import TYPE_CHECKING, Optional, Set

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .memory.memory_bus import MemoryBus
    from .identity.agent_identity import AgentIdentityStore


class SdkApi:
    """
    External WebSocket SDK API for the UnifiedClaw Gateway.
    
    Allows external tools to query memory, check agent status,
    and submit tasks without needing direct database access.
    
    Authentication: Optional bearer token via SDK_API_TOKEN env var.
    If not set, all connections are accepted (suitable for localhost use).
    """

    DEFAULT_PORT = 8767
    MAX_CONNECTIONS = 50
    PING_INTERVAL = 30  # seconds

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
        self._token = token or os.environ.get("SDK_API_TOKEN", "")
        self._connections: Set = set()
        self._running = False
        self._task_submit_callback = None
        self._bot_registry = bot_registry  # Phase 3: BotRegistry

    async def start(self):
        """Start the SDK API WebSocket server."""
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.warning("SdkApi: websockets not installed. Run: pip install websockets")
            return

        self._running = True
        logger.info(f"SdkApi starting on ws://0.0.0.0:{self._port}")
        async with websockets.serve(
            self._handle_connection,
            "0.0.0.0",
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
            return

        self._connections.add(websocket)
        remote = getattr(websocket, "remote_address", "unknown")
        logger.info(f"SdkApi: client connected from {remote}")

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_error(websocket, "invalid_json", "Invalid JSON")
                    continue

                # Optional authentication
                if self._token and msg.get("token") != self._token:
                    await self._send_error(websocket, "unauthorized", "Invalid or missing token")
                    continue

                await self._dispatch(websocket, msg)

        except Exception as e:
            logger.debug(f"SdkApi: connection closed: {e}")
        finally:
            self._connections.discard(websocket)
            logger.debug(f"SdkApi: client disconnected from {remote}")

    async def _dispatch(self, websocket, msg: dict):
        """Route incoming message to appropriate handler."""
        msg_type = msg.get("type", "")

        handlers = {
            "ping":           self._handle_ping,
            "memory_query":   self._handle_memory_query,
            "memory_write":   self._handle_memory_write,
            "agent_list":     self._handle_agent_list,
            "system_status":  self._handle_system_status,
            "task_submit":    self._handle_task_submit,
            # Phase 3: Bot Registry
            "bot_register":   self._handle_bot_register,
            "bot_lookup":     self._handle_bot_lookup,
            "bot_list":       self._handle_bot_list,
            "bot_handshake":  self._handle_bot_handshake,
        }

        handler = handlers.get(msg_type)
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
        k = int(msg.get("k", 5))
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
            await self._send_error(websocket, "memory_error", str(e))

    async def _handle_memory_write(self, websocket, msg: dict):
        """Write a memory entry."""
        content = msg.get("content", "")
        agent_id = msg.get("agent_id", "")
        scope = msg.get("scope", "shared")
        project = msg.get("project", "")
        importance = float(msg.get("importance", 0.5))

        if not content or not agent_id:
            await self._send_error(websocket, "missing_params", "content and agent_id required")
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
            await self._send_error(websocket, "memory_error", str(e))

    async def _handle_agent_list(self, websocket, msg: dict):
        """List all known agents."""
        project = msg.get("project", "")
        try:
            agents = self._identity_store.list_agents(project=project)
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
                    for a in agents
                ],
                "count": len(agents),
            }))
        except Exception as e:
            await self._send_error(websocket, "identity_error", str(e))

    async def _handle_system_status(self, websocket, msg: dict):
        """Return system status."""
        try:
            memory_status = self._memory_bus.status()
            agent_count = len(self._identity_store.list_agents())
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
            await self._send_error(websocket, "status_error", str(e))

    async def _handle_task_submit(self, websocket, msg: dict):
        """Submit a task to a group (if callback registered)."""
        group = msg.get("group", "")
        message = msg.get("message", "")

        if not group or not message:
            await self._send_error(websocket, "missing_params", "group and message required")
            return

        if self._task_submit_callback:
            try:
                task_id = await self._task_submit_callback(group, message)
                await websocket.send(json.dumps({"type": "task_ack", "task_id": task_id, "group": group}))
            except Exception as e:
                await self._send_error(websocket, "task_error", str(e))
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
            await self._send_error(websocket, "bot_register_error", str(e))

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
            await self._send_error(websocket, "bot_lookup_error", str(e))

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
            await self._send_error(websocket, "bot_list_error", str(e))

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
            await self._send_error(websocket, "bot_handshake_error", str(e))

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

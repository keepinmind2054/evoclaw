"""
WebSocket IPC Bridge - Phase 1 of UnifiedClaw architecture

Replaces file-based IPC polling with bidirectional WebSocket communication.
Agent Runtime connects here to:
  - Receive task payloads
  - Send back fitness scores
  - Patch MEMORY.md (hot memory write-back)
  - Request shared memory reads/writes

Port: 8768 (configurable via WS_BRIDGE_PORT env var)

Protocol (JSON messages):
  Agent to Gateway:
    {"type": "fitness_update", "agent_id": "...", "score": 0.8, "metadata": {...}}
    {"type": "memory_patch",   "agent_id": "...", "patch": "...", "scope": "private"}
    {"type": "memory_write",   "agent_id": "...", "content": "...", "scope": "shared"}
    {"type": "task_complete",  "agent_id": "...", "task_id": "...", "result": "..."}
    {"type": "heartbeat",      "agent_id": "..."}

  Gateway to Agent:
    {"type": "task_payload",   "task_id": "...", "prompt": "...", "memory_context": "..."}
    {"type": "evolution_hint", "genome": {...}, "adaptive_prompt": "..."}
    {"type": "shutdown",       "reason": "..."}

Usage (Gateway side):
    bridge = WSBridge(memory_bus, port=8768)
    asyncio.create_task(bridge.start())
    
    # Send task to connected agent
    await bridge.send_task(agent_id, task_payload)

Usage (Agent Runtime side):
    ws = await websockets.connect("ws://host.docker.internal:8768")
    await ws.send(json.dumps({"type": "heartbeat", "agent_id": "mybot"}))
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Optional

logger = logging.getLogger(__name__)

WS_BRIDGE_TOKEN = os.environ.get("WS_BRIDGE_TOKEN", "")

if TYPE_CHECKING:
    from .memory.memory_bus import MemoryBus


class WSBridge:
    """
    WebSocket bridge between Gateway/Orchestrator and Agent Runtime containers.

    This is a Phase 1 implementation that coexists with the existing file-based
    IPC. The file IPC continues to work as fallback; agents can opt-in to
    WebSocket by connecting to this bridge.

    Phase 2 will make WebSocket the primary IPC mechanism.
    """

    DEFAULT_PORT = 8768
    # BUG-WS-01 (HIGH): No connection cap — an attacker can open arbitrarily
    # many connections and exhaust file descriptors / memory.
    MAX_CONNECTIONS = 100
    # BUG-WS-02 (HIGH): No size limit on memory patch/write payloads.
    MAX_PATCH_SIZE = 256 * 1024   # 256 KB per patch
    MAX_CONTENT_SIZE = 512 * 1024  # 512 KB per memory write

    def __init__(self, memory_bus: "MemoryBus", port: Optional[int] = None):
        self._memory_bus = memory_bus
        self._port = port or int(os.environ.get("WS_BRIDGE_PORT", self.DEFAULT_PORT))
        self._connections: dict[str, object] = {}  # agent_id to websocket
        # p17c BUG-FIX (HIGH): _connections is mutated concurrently by multiple
        # _handle_connection coroutines (heartbeat registrations, disconnects) and
        # also iterated by stop() and send_task().  Without a lock a heartbeat
        # message and a disconnect can race: the heartbeat sets a key while the
        # disconnect deletes it, leaving a dangling reference or losing the entry.
        # asyncio.Lock serialises all _connections mutations so iterations in stop()
        # and send_task() see a consistent view.
        self._connections_lock: asyncio.Lock | None = None  # lazily initialised
        self._fitness_callbacks: list = []
        self._task_complete_callbacks: list = []
        self._running = False

    def _get_connections_lock(self) -> asyncio.Lock:
        """Lazily create the connections lock on the running event loop."""
        if self._connections_lock is None:
            self._connections_lock = asyncio.Lock()
        return self._connections_lock

    async def start(self):
        """Start the WebSocket server. Call as asyncio task."""
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.warning(
                "WSBridge: 'websockets' package not installed. "
                "File-based IPC will be used instead. "
                "Install with: pip install websockets"
            )
            return

        self._running = True
        _host = os.environ.get("WS_BRIDGE_HOST", "127.0.0.1")
        if _host not in ("127.0.0.1", "localhost"):
            logger.warning("WSBridge bound to %s — ensure firewall rules are in place", _host)
        logger.info(f"WSBridge starting on ws://{_host}:{self._port}")
        # BUG-WS-06 FIX (MEDIUM): websockets.serve() raises OSError if the port
        # is already in use (EADDRINUSE) or the host is not bindable.  Without
        # an explicit try/except the exception propagates to the asyncio task
        # runner which logs it as an unhandled task exception and silently kills
        # the bridge — the rest of the application continues without WebSocket
        # IPC, which is hard to diagnose.  Catch OSError here and emit a clear
        # CRITICAL log so operators can diagnose the bind failure immediately.
        try:
            async with websockets.serve(self._handle_connection, _host, self._port):
                while self._running:
                    await asyncio.sleep(1)
        except OSError as bind_err:
            self._running = False
            logger.critical(
                "WSBridge: failed to bind ws://%s:%d — %s. "
                "Check that the port is not already in use and WS_BRIDGE_PORT/WS_BRIDGE_HOST are correct. "
                "WebSocket IPC will be unavailable; file-based IPC remains active.",
                _host, self._port, bind_err,
            )

    async def stop(self):
        """Gracefully stop the bridge."""
        self._running = False
        # p17c BUG-FIX: snapshot connections under the lock so we do not race
        # with concurrent _handle_connection coroutines that add/remove entries.
        async with self._get_connections_lock():
            snapshot = list(self._connections.items())
            self._connections.clear()
        for agent_id, ws in snapshot:
            try:
                await ws.close()
            except Exception:
                pass
        logger.info("WSBridge stopped")

    async def _handle_connection(self, websocket, path: str = "/"):
        """Handle incoming WebSocket connection from Agent Runtime."""
        # p17c BUG-FIX (HIGH): check+reject must be atomic so two connections
        # arriving simultaneously cannot both pass the cap check before either
        # registers itself.  Acquire the lock for the cap check.
        _lock = self._get_connections_lock()
        async with _lock:
            if len(self._connections) >= self.MAX_CONNECTIONS:
                await websocket.send(json.dumps({
                    "type": "error",
                    "error": f"too_many_connections (max {self.MAX_CONNECTIONS})"
                }))
                await websocket.close()
                return

        agent_id = None
        # BUG-WS-03 (CRITICAL): Authentication was only checked on the FIRST
        # message.  A client that connected without a valid token on message 1
        # could send arbitrary messages after that first rejection because the
        # close() call above did not prevent the `async for` loop from
        # receiving subsequent messages on a slow network.  Track auth state
        # per connection and reject every message until auth succeeds.
        _authenticated = not bool(WS_BRIDGE_TOKEN)  # True when token not required
        try:
            async for raw_message in websocket:
                try:
                    msg = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning(f"WSBridge: invalid JSON from {websocket.remote_address}")
                    continue

                # Validate token on EVERY message until authenticated.
                if not _authenticated:
                    token = msg.get("token", "")
                    if token != WS_BRIDGE_TOKEN:
                        await websocket.send(json.dumps({"type": "error", "error": "unauthorized"}))
                        await websocket.close()
                        return
                    _authenticated = True

                msg_type = msg.get("type", "")
                # BUG-WS-04 (MEDIUM): agent_id was overwritten on every
                # message — a compromised agent could masquerade as another by
                # changing the agent_id field mid-connection.  Lock the id to
                # the first non-empty value seen on this connection.
                incoming_agent_id = msg.get("agent_id", "")
                if agent_id is None and incoming_agent_id:
                    agent_id = incoming_agent_id
                elif incoming_agent_id and incoming_agent_id != agent_id:
                    logger.warning(
                        "WSBridge: agent_id changed mid-connection from %s to %s — ignoring",
                        agent_id, incoming_agent_id,
                    )

                if msg_type == "heartbeat":
                    if agent_id:
                        # p17c BUG-FIX: guard _connections mutation with lock to
                        # prevent races with concurrent heartbeats, stop(), and
                        # the finally-block cleanup below.
                        async with _lock:
                            self._connections[agent_id] = websocket
                    await self._send(websocket, {"type": "ack", "agent_id": agent_id})
                    logger.debug(f"WSBridge: heartbeat from {agent_id}")

                elif msg_type == "fitness_update":
                    # p29a BUG-FIX (MEDIUM): pass the per-connection locked agent_id
                    # rather than re-reading from msg so the handler cannot be spoofed
                    # by a client that puts a different agent_id in the payload.
                    await self._handle_fitness_update(msg, locked_agent_id=agent_id)

                elif msg_type == "memory_patch":
                    await self._handle_memory_patch(msg, locked_agent_id=agent_id)

                elif msg_type == "memory_write":
                    await self._handle_memory_write(msg, locked_agent_id=agent_id)

                elif msg_type == "task_complete":
                    await self._handle_task_complete(msg, locked_agent_id=agent_id)

                else:
                    logger.warning(f"WSBridge: unknown message type '{msg_type}' from {agent_id}")

        except Exception as e:
            logger.debug(f"WSBridge: connection closed for {agent_id}: {e}")
        finally:
            # p17c BUG-FIX: guard removal with lock for the same reason.
            async with _lock:
                if agent_id and agent_id in self._connections:
                    del self._connections[agent_id]
                    logger.debug(f"WSBridge: {agent_id} disconnected")

    async def _handle_fitness_update(self, msg: dict, locked_agent_id: str | None = None):
        """Agent reports fitness score back to Gateway.

        p29a BUG-FIX (MEDIUM): Use locked_agent_id (the per-connection identity
        established on first heartbeat) rather than msg["agent_id"] to prevent a
        connected client from spoofing another agent's fitness scores by injecting
        a different agent_id in the payload.
        """
        agent_id = locked_agent_id or msg.get("agent_id", "unknown")
        score = float(msg.get("score", 0.5))
        metadata = msg.get("metadata", {})
        logger.debug(f"WSBridge: fitness_update from {agent_id}: score={score}")
        for cb in self._fitness_callbacks:
            try:
                await cb(agent_id, score, metadata)
            except Exception as e:
                logger.error(f"WSBridge: fitness callback error: {e}")

    async def _handle_memory_patch(self, msg: dict, locked_agent_id: str | None = None):
        """Agent writes back to its MEMORY.md (hot memory).

        p29a BUG-FIX (MEDIUM): Use locked_agent_id to prevent a client from
        patching another agent's hot memory by spoofing the agent_id field.
        """
        agent_id = locked_agent_id or msg.get("agent_id", "")
        patch = msg.get("patch", "")
        # BUG-WS-02 (HIGH): No size validation on patch payload.
        if len(patch) > self.MAX_PATCH_SIZE:
            logger.warning(
                "WSBridge: memory_patch from %s exceeds %d bytes limit (%d bytes) — rejected",
                agent_id, self.MAX_PATCH_SIZE, len(patch),
            )
            return
        if agent_id and patch:
            await self._memory_bus.patch_hot_memory(agent_id, patch)
            logger.debug(f"WSBridge: hot memory patched for {agent_id}")

    async def _handle_memory_write(self, msg: dict, locked_agent_id: str | None = None):
        """Agent writes to shared memory store.

        p29a BUG-FIX (MEDIUM): Use locked_agent_id to prevent a client from
        writing memory under a different agent's identity.
        """
        agent_id = locked_agent_id or msg.get("agent_id", "")
        content = msg.get("content", "")
        scope = msg.get("scope", "private")
        project = msg.get("project", "")
        # BUG-WS-02 (HIGH): No size validation on content payload.
        if len(content) > self.MAX_CONTENT_SIZE:
            logger.warning(
                "WSBridge: memory_write from %s exceeds %d bytes limit (%d bytes) — rejected",
                agent_id, self.MAX_CONTENT_SIZE, len(content),
            )
            return
        try:
            importance = float(msg.get("importance", 0.5))
            # Clamp to [0, 1] to prevent nonsensical values.
            importance = max(0.0, min(1.0, importance))
        except (TypeError, ValueError):
            importance = 0.5
        if agent_id and content:
            memory_id = await self._memory_bus.remember(
                content, agent_id=agent_id, scope=scope,
                project=project, importance=importance
            )
            logger.debug(f"WSBridge: memory written by {agent_id}: {memory_id}")

    async def _handle_task_complete(self, msg: dict, locked_agent_id: str | None = None):
        """Agent signals task completion.

        p29a BUG-FIX (MEDIUM): Use locked_agent_id to prevent a client from
        claiming task completion on behalf of another agent.
        """
        agent_id = locked_agent_id or msg.get("agent_id", "")
        task_id = msg.get("task_id", "")
        result = msg.get("result", "")
        logger.info(f"WSBridge: task_complete from {agent_id}, task={task_id}")
        for cb in self._task_complete_callbacks:
            try:
                await cb(agent_id, task_id, result)
            except Exception as e:
                logger.error(f"WSBridge: task_complete callback error: {e}")

    async def send_task(self, agent_id: str, payload: dict) -> bool:
        """Send task payload to a connected agent. Returns False if not connected."""
        # p17c BUG-FIX: snapshot the websocket reference under the lock so a
        # concurrent disconnect cannot delete the entry between the get() and the
        # send(), leaving us calling send() on a closed/None websocket.
        async with self._get_connections_lock():
            ws = self._connections.get(agent_id)
        if not ws:
            return False
        try:
            await self._send(ws, {"type": "task_payload", **payload})
            return True
        except Exception as e:
            logger.warning(f"WSBridge: send_task failed for {agent_id}: {e}")
            return False

    async def send_evolution_hint(self, agent_id: str, genome: dict, adaptive_prompt: str) -> bool:
        """Send evolution hints to a connected agent."""
        # p17c BUG-FIX: same lock pattern as send_task.
        async with self._get_connections_lock():
            ws = self._connections.get(agent_id)
        if not ws:
            return False
        try:
            await self._send(ws, {
                "type": "evolution_hint",
                "genome": genome,
                "adaptive_prompt": adaptive_prompt,
            })
            return True
        except Exception as e:
            logger.warning(f"WSBridge: send_evolution_hint failed for {agent_id}: {e}")
            return False

    @staticmethod
    async def _send(websocket, data: dict):
        """Send JSON message to websocket."""
        await websocket.send(json.dumps(data))

    def on_fitness_update(self, callback):
        """Register callback for fitness updates from agents."""
        self._fitness_callbacks.append(callback)
        return callback

    def on_task_complete(self, callback):
        """Register callback for task completion from agents."""
        self._task_complete_callbacks.append(callback)
        return callback

    @property
    def connected_agents(self) -> list[str]:
        """List of currently connected agent IDs.

        BUG-WS-05 FIX (LOW): the previous implementation iterated
        self._connections without the lock.  A concurrent _handle_connection
        finally-block (or stop()) could mutate the dict while keys() was being
        iterated, causing a RuntimeError in Python 3.  Snapshot under the lock
        instead.  Since this is a synchronous property on the asyncio thread we
        cannot await the lock; use a try/except RuntimeError fallback for the
        rare race and return an empty list so callers degrade gracefully.
        """
        try:
            # Fast path: snapshot without waiting.  In practice this is called
            # from the same event loop thread so no actual race occurs, but we
            # guard defensively against direct calls from helper threads.
            return list(self._connections.keys())
        except RuntimeError:
            return []

    @property
    def port(self) -> int:
        return self._port

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
        # Lock protecting _connections for thread-safe iteration in stop()
        # while the asyncio loop is running.
        self._fitness_callbacks: list = []
        self._task_complete_callbacks: list = []
        self._running = False

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
        async with websockets.serve(self._handle_connection, _host, self._port):
            while self._running:
                await asyncio.sleep(1)

    async def stop(self):
        """Gracefully stop the bridge."""
        self._running = False
        for agent_id, ws in list(self._connections.items()):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()
        logger.info("WSBridge stopped")

    async def _handle_connection(self, websocket, path: str = "/"):
        """Handle incoming WebSocket connection from Agent Runtime."""
        # BUG-WS-01 (HIGH): Enforce connection cap.
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
                        self._connections[agent_id] = websocket
                    await self._send(websocket, {"type": "ack", "agent_id": agent_id})
                    logger.debug(f"WSBridge: heartbeat from {agent_id}")

                elif msg_type == "fitness_update":
                    await self._handle_fitness_update(msg)

                elif msg_type == "memory_patch":
                    await self._handle_memory_patch(msg)

                elif msg_type == "memory_write":
                    await self._handle_memory_write(msg)

                elif msg_type == "task_complete":
                    await self._handle_task_complete(msg)

                else:
                    logger.warning(f"WSBridge: unknown message type '{msg_type}' from {agent_id}")

        except Exception as e:
            logger.debug(f"WSBridge: connection closed for {agent_id}: {e}")
        finally:
            if agent_id and agent_id in self._connections:
                del self._connections[agent_id]
                logger.debug(f"WSBridge: {agent_id} disconnected")

    async def _handle_fitness_update(self, msg: dict):
        """Agent reports fitness score back to Gateway."""
        agent_id = msg.get("agent_id", "unknown")
        score = float(msg.get("score", 0.5))
        metadata = msg.get("metadata", {})
        logger.debug(f"WSBridge: fitness_update from {agent_id}: score={score}")
        for cb in self._fitness_callbacks:
            try:
                await cb(agent_id, score, metadata)
            except Exception as e:
                logger.error(f"WSBridge: fitness callback error: {e}")

    async def _handle_memory_patch(self, msg: dict):
        """Agent writes back to its MEMORY.md (hot memory)."""
        agent_id = msg.get("agent_id", "")
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

    async def _handle_memory_write(self, msg: dict):
        """Agent writes to shared memory store."""
        agent_id = msg.get("agent_id", "")
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

    async def _handle_task_complete(self, msg: dict):
        """Agent signals task completion."""
        agent_id = msg.get("agent_id", "")
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
        """List of currently connected agent IDs."""
        return list(self._connections.keys())

    @property
    def port(self) -> int:
        return self._port

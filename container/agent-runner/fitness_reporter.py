"""
Fitness Reporter - Agent Runtime side of WebSocket IPC (Phase 1)

Allows the Agent Runtime (inside Docker) to:
1. Connect to the Gateway's WebSocket bridge
2. Report fitness scores back to the evolution engine
3. Write memories back to the Gateway (hot/shared)
4. Signal task completion

This runs inside the Docker container (Agent Runtime side).

Usage in agent.py:
    reporter = FitnessReporter(
        gateway_url="ws://host.docker.internal:8768",
        agent_id="mybot"
    )
    await reporter.connect()
    
    # After generating a response
    await reporter.report_fitness(score=0.9, metadata={"response_time": 1.2})
    
    # Write back to memory
    await reporter.write_memory("User prefers Python examples", scope="private")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


class FitnessReporter:
    """
    Sends fitness feedback and memory updates from Agent Runtime to Gateway.
    
    Gracefully degrades to no-op if WebSocket bridge is unavailable
    (backward compatible with file-based IPC).
    """

    RECONNECT_DELAY = 5.0   # seconds between reconnection attempts
    HEARTBEAT_INTERVAL = 30  # seconds between heartbeats

    def __init__(
        self,
        agent_id: str,
        gateway_url: Optional[str] = None,
    ):
        self.agent_id = agent_id
        self._url = gateway_url or os.environ.get(
            "WS_BRIDGE_URL",
            "ws://host.docker.internal:8768"
        )
        self._ws = None
        self._connected = False
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def connect(self) -> bool:
        """
        Connect to Gateway WebSocket bridge.
        Returns True if connected, False if unavailable (fallback to file IPC).
        """
        try:
            import websockets  # type: ignore
            self._ws = await websockets.connect(self._url, open_timeout=3)
            self._connected = True
            # Send initial heartbeat to register
            await self._send({"type": "heartbeat", "agent_id": self.agent_id})
            # Start background heartbeat
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info(f"FitnessReporter: connected to {self._url}")
            return True
        except Exception as e:
            logger.debug(f"FitnessReporter: WebSocket unavailable ({e}), using file IPC fallback")
            self._connected = False
            return False

    async def disconnect(self):
        """Cleanly disconnect from Gateway."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._connected = False

    async def report_fitness(
        self,
        score: float,
        metadata: Optional[dict] = None,
    ):
        """
        Report response quality score to evolution engine.
        
        Args:
            score:    Quality score 0.0-1.0 (1.0 = perfect response)
            metadata: Optional metadata (response_time, retry_count, etc.)
        """
        if not self._connected:
            return
        await self._send({
            "type": "fitness_update",
            "agent_id": self.agent_id,
            "score": max(0.0, min(1.0, score)),
            "metadata": metadata or {},
            "timestamp": time.time(),
        })

    async def write_memory(
        self,
        content: str,
        scope: str = "private",
        project: str = "",
        importance: float = 0.5,
    ):
        """
        Write a memory to the Gateway's memory store.
        
        Args:
            content:    Text to remember
            scope:      "private" | "shared" | "project"
            project:    Project name (for "project" scope)
            importance: 0.0-1.0
        """
        if not self._connected:
            return
        await self._send({
            "type": "memory_write",
            "agent_id": self.agent_id,
            "content": content,
            "scope": scope,
            "project": project,
            "importance": importance,
        })

    async def patch_hot_memory(self, patch: str):
        """
        Append text to this agent's MEMORY.md file.
        Called when agent wants to persist important information.
        """
        if not self._connected:
            # Fallback: write to local file (existing behavior)
            self._fallback_memory_patch(patch)
            return
        await self._send({
            "type": "memory_patch",
            "agent_id": self.agent_id,
            "patch": patch,
        })

    async def signal_complete(self, task_id: str, result: str = ""):
        """Signal that a task has been completed."""
        if not self._connected:
            return
        await self._send({
            "type": "task_complete",
            "agent_id": self.agent_id,
            "task_id": task_id,
            "result": result,
            "timestamp": time.time(),
        })

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to maintain connection."""
        while self._connected:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                await self._send({"type": "heartbeat", "agent_id": self.agent_id})
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"FitnessReporter: heartbeat failed: {e}")
                self._connected = False
                break

    async def _send(self, data: dict):
        """Send JSON message to Gateway."""
        if not self._ws:
            return
        try:
            await self._ws.send(json.dumps(data))
        except Exception as e:
            logger.debug(f"FitnessReporter: send failed: {e}")
            self._connected = False

    def _fallback_memory_patch(self, patch: str):
        """Fallback: write memory patch to local MEMORY.md file."""
        try:
            memory_file = os.path.join(
                os.environ.get("GROUP_DIR", "/workspace"), "MEMORY.md"
            )
            with open(memory_file, "a", encoding="utf-8") as f:
                f.write(f"\n{patch}")
        except OSError as e:
            logger.debug(f"FitnessReporter: fallback memory write failed: {e}")

    @property
    def connected(self) -> bool:
        return self._connected

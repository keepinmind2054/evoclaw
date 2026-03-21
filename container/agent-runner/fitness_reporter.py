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
    # Fix p14c: cap reconnection attempts so a permanently-down gateway doesn't
    # loop forever and waste CPU inside a short-lived agent container.
    MAX_RECONNECT_ATTEMPTS = 3

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
        # Fix p14c: track reconnection attempts to avoid infinite retry loops.
        self._reconnect_attempts: int = 0

    async def connect(self) -> bool:
        """
        Connect to Gateway WebSocket bridge.
        Returns True if connected, False if unavailable (fallback to file IPC).
        """
        try:
            import websockets  # type: ignore
            self._ws = await websockets.connect(self._url, open_timeout=3)
            self._connected = True
            self._reconnect_attempts = 0  # Fix p14c: reset counter on successful connect
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

    async def _try_reconnect(self) -> bool:
        """
        Fix p14c: attempt to re-establish a dropped WebSocket connection.

        Called from _send() and report_fitness() when _connected is False.
        Capped at MAX_RECONNECT_ATTEMPTS to avoid infinite loops inside a
        short-lived agent container where the gateway may be permanently down.

        Returns True if the connection was restored.
        """
        if self._reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
            return False
        self._reconnect_attempts += 1
        logger.debug(
            "FitnessReporter: reconnect attempt %d/%d",
            self._reconnect_attempts,
            self.MAX_RECONNECT_ATTEMPTS,
        )
        try:
            await asyncio.sleep(self.RECONNECT_DELAY)
            return await self.connect()
        except Exception as e:
            logger.debug("FitnessReporter: reconnect failed: %s", e)
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

        Fix p14c (a): if the WebSocket is not connected, attempt a single
        reconnect before falling back to a local JSON file so that fitness
        data is never silently dropped.
        Fix p14c (b): clamp score through float() first to guard against
        non-numeric inputs that could break max/min comparisons.
        """
        try:
            clamped_score = max(0.0, min(1.0, float(score)))
        except (TypeError, ValueError):
            logger.warning("FitnessReporter.report_fitness: invalid score %r — defaulting to 0.0", score)
            clamped_score = 0.0

        payload = {
            "type": "fitness_update",
            "agent_id": self.agent_id,
            "score": clamped_score,
            "metadata": metadata or {},
            "timestamp": time.time(),
        }

        # Fix p14c: attempt reconnect if needed before giving up on WebSocket.
        if not self._connected:
            await self._try_reconnect()

        if self._connected:
            await self._send(payload)
        else:
            # Fix p14c: WebSocket unavailable — persist to local file so the
            # host can pick it up via file-based IPC (same as patch_hot_memory fallback).
            self._fallback_fitness_write(payload)

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
        """Send periodic heartbeats to maintain connection.

        Fix p14c: on failure, attempt a reconnect (up to MAX_RECONNECT_ATTEMPTS)
        before giving up, so a transient network blip doesn't permanently disable
        the reporter for the remainder of the agent run.
        """
        while self._connected:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                await self._send({"type": "heartbeat", "agent_id": self.agent_id})
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"FitnessReporter: heartbeat failed: {e}")
                self._connected = False
                # Attempt to restore the connection before the next heartbeat.
                reconnected = await self._try_reconnect()
                if not reconnected:
                    logger.debug("FitnessReporter: heartbeat giving up after failed reconnect")
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

    def _fallback_fitness_write(self, payload: dict) -> None:
        """Fix p14c: persist a fitness payload to a local JSONL file when the
        WebSocket is unavailable.  The host's file-IPC reader can consume these
        lines and feed them into the evolution engine."""
        try:
            fitness_file = os.path.join(
                os.environ.get("GROUP_DIR", "/workspace"), "fitness_queue.jsonl"
            )
            with open(fitness_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")
            logger.debug("FitnessReporter: fitness written to fallback file %s", fitness_file)
        except OSError as e:
            logger.debug("FitnessReporter: fallback fitness write failed: %s", e)

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

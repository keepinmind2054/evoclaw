"""Lightweight leader election for multi-instance evoclaw deployments.

Strategy: One instance acts as "leader" and processes messages.
          Others are "standby" and will take over if the leader fails.

Mechanism (SQLite-based, single machine):
  - Uses a dedicated `leader_election` table in the messages DB
  - Leader writes a heartbeat row every HEARTBEAT_INTERVAL seconds
  - Standby instances check: if last heartbeat > LEASE_TIMEOUT seconds ago, claim leadership
  - On shutdown, leader releases the lease

Mechanism (file lock, single machine alternative):
  - Uses fcntl.flock() on a lock file (Linux/macOS only)
  - Simpler but no visibility into who holds the lease

Environment variables:
  LEADER_ELECTION_ENABLED=true/false   (default: false — single instance mode)
  LEADER_HEARTBEAT_INTERVAL=10         (seconds between heartbeats, default 10)
  LEADER_LEASE_TIMEOUT=30              (seconds before lease is considered expired, default 30)
  INSTANCE_ID=<string>                 (unique ID for this instance, defaults to hostname:pid)

Usage in main():
    from host.leader_election import LeaderElection
    leader = LeaderElection(db_conn)
    await leader.acquire()       # blocks until this instance becomes leader
    # ... run as leader ...
    await leader.release()       # on shutdown
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Optional

log = logging.getLogger(__name__)

_ENABLED = os.environ.get("LEADER_ELECTION_ENABLED", "false").lower() == "true"
_HEARTBEAT_INTERVAL = int(os.environ.get("LEADER_HEARTBEAT_INTERVAL", "10"))
_LEASE_TIMEOUT = int(os.environ.get("LEADER_LEASE_TIMEOUT", "30"))
_INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{socket.gethostname()}:{os.getpid()}")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS leader_election (
    singleton       INTEGER PRIMARY KEY DEFAULT 1 CHECK (singleton = 1),
    instance_id     TEXT    NOT NULL,
    acquired_at     REAL    NOT NULL,
    heartbeat_at    REAL    NOT NULL
)
"""

_ACQUIRE = """
INSERT INTO leader_election (singleton, instance_id, acquired_at, heartbeat_at)
VALUES (1, ?, ?, ?)
ON CONFLICT (singleton) DO UPDATE SET
    instance_id  = excluded.instance_id,
    acquired_at  = excluded.acquired_at,
    heartbeat_at = excluded.heartbeat_at
WHERE leader_election.heartbeat_at < ?
"""

_HEARTBEAT = """
UPDATE leader_election
SET heartbeat_at = ?
WHERE singleton = 1 AND instance_id = ?
"""

_READ = "SELECT instance_id, heartbeat_at FROM leader_election WHERE singleton = 1"


class LeaderElection:
    """DB-backed leader election with heartbeat renewal.

    If LEADER_ELECTION_ENABLED is false (default), this is a no-op:
    acquire() returns immediately and is_leader is always True.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._is_leader = not _ENABLED  # no-op mode: always leader
        self._task: Optional[asyncio.Task] = None

        if _ENABLED:
            conn.execute(_CREATE_TABLE)
            conn.commit()
            log.info(
                "LeaderElection initialized: instance_id=%s, heartbeat=%ds, lease=%ds",
                _INSTANCE_ID, _HEARTBEAT_INTERVAL, _LEASE_TIMEOUT,
            )

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def acquire(self) -> None:
        """Block until this instance acquires the leader lease."""
        if not _ENABLED:
            return

        while True:
            now = time.time()
            expiry = now - _LEASE_TIMEOUT
            try:
                cur = self._conn.execute(_ACQUIRE, (_INSTANCE_ID, now, now, expiry))
                self._conn.commit()
                if cur.rowcount > 0:
                    self._is_leader = True
                    log.info("LeaderElection: acquired leadership (%s)", _INSTANCE_ID)
                    self._task = asyncio.create_task(self._heartbeat_loop())
                    return
            except Exception as exc:
                log.debug("LeaderElection: acquire attempt failed: %s", exc)

            # Check who holds the lease
            try:
                row = self._conn.execute(_READ).fetchone()
                if row:
                    log.info(
                        "LeaderElection: standby — leader is %s (heartbeat %.0fs ago)",
                        row[0], time.time() - row[1],
                    )
            except Exception:
                pass

            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    async def release(self) -> None:
        """Release the leader lease on shutdown."""
        if not _ENABLED or not self._is_leader:
            return

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        try:
            self._conn.execute(
                "DELETE FROM leader_election WHERE singleton = 1 AND instance_id = ?",
                (_INSTANCE_ID,),
            )
            self._conn.commit()
            log.info("LeaderElection: released lease (%s)", _INSTANCE_ID)
        except Exception as exc:
            log.warning("LeaderElection: release failed (non-fatal): %s", exc)

        self._is_leader = False

    async def _heartbeat_loop(self) -> None:
        """Periodically update heartbeat_at to keep the lease alive."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                now = time.time()
                cur = self._conn.execute(_HEARTBEAT, (now, _INSTANCE_ID))
                self._conn.commit()
                if cur.rowcount == 0:
                    # Another instance stole the lease
                    log.warning(
                        "LeaderElection: lease lost! Another instance became leader. Shutting down."
                    )
                    self._is_leader = False
                    # Signal main process to stop
                    import signal
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                log.debug("LeaderElection: heartbeat renewed (%.0f)", now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("LeaderElection: heartbeat failed: %s", exc)

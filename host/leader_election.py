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
import random
import signal
import socket
import time
from typing import Optional

log = logging.getLogger(__name__)

_ENABLED = os.environ.get("LEADER_ELECTION_ENABLED", "false").lower() == "true"
_HEARTBEAT_INTERVAL = int(os.environ.get("LEADER_HEARTBEAT_INTERVAL", "10"))
_LEASE_TIMEOUT = int(os.environ.get("LEADER_LEASE_TIMEOUT", "30"))
_INSTANCE_ID = os.environ.get("INSTANCE_ID", f"{socket.gethostname()}:{os.getpid()}")

# BUG-LE-1 FIX: Enforce LEASE_TIMEOUT > HEARTBEAT_INTERVAL at startup.
# If LEASE_TIMEOUT <= HEARTBEAT_INTERVAL a valid leader can never renew before
# its own lease is considered expired, causing continuous leadership churn /
# split-brain where multiple standby instances simultaneously claim the lease.
if _ENABLED and _LEASE_TIMEOUT <= _HEARTBEAT_INTERVAL:
    _LEASE_TIMEOUT = _HEARTBEAT_INTERVAL * 3
    log.warning(
        "LeaderElection: LEADER_LEASE_TIMEOUT must be > LEADER_HEARTBEAT_INTERVAL. "
        "Automatically adjusted to %ds (3 × heartbeat interval).",
        _LEASE_TIMEOUT,
    )

# BUG-LE-2 FIX: Maximum wall-clock seconds to wait for a single SQLite call.
# Without this, a blocked/hung DB causes acquire() and the heartbeat loop to
# freeze indefinitely, silently stalling the entire event loop.
_DB_OP_TIMEOUT = 5  # seconds

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

# Consecutive heartbeat failures before we self-demote and signal shutdown.
# A single transient DB error should not cause a false demotion.
_MAX_CONSECUTIVE_HEARTBEAT_FAILURES = 3


def _db_execute(conn, *args, **kwargs):
    """Execute a SQLite statement with a hard timeout.

    BUG-LE-2: Wraps conn.execute in a thread so that a blocked/locked DB
    cannot freeze the asyncio event loop indefinitely.  Raises RuntimeError
    if the operation does not complete within _DB_OP_TIMEOUT seconds.
    """
    import concurrent.futures
    import threading

    result_holder: list = []
    exc_holder: list = []

    def _run():
        try:
            result_holder.append(conn.execute(*args, **kwargs))
        except Exception as e:  # noqa: BLE001
            exc_holder.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=_DB_OP_TIMEOUT)
    if t.is_alive():
        raise RuntimeError(
            f"LeaderElection: DB operation timed out after {_DB_OP_TIMEOUT}s"
        )
    if exc_holder:
        raise exc_holder[0]
    return result_holder[0]


class LeaderElection:
    """DB-backed leader election with heartbeat renewal.

    If LEADER_ELECTION_ENABLED is false (default), this is a no-op:
    acquire() returns immediately and is_leader is always True.
    """

    def __init__(self, conn) -> None:
        self._conn = conn
        self._is_leader = not _ENABLED  # no-op mode: always leader
        self._task: Optional[asyncio.Task] = None
        # BUG-LE-5: Track consecutive heartbeat failures so we don't demote
        # ourselves on a single transient DB error (but DO demote after
        # _MAX_CONSECUTIVE_HEARTBEAT_FAILURES consecutive failures).
        self._consecutive_hb_failures = 0

        if _ENABLED:
            _db_execute(conn, _CREATE_TABLE)
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
                # BUG-LE-2 FIX: use timeout-guarded execute
                cur = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: _db_execute(
                        self._conn, _ACQUIRE, (_INSTANCE_ID, now, now, expiry)
                    ),
                )
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
                row = _db_execute(self._conn, _READ).fetchone()
                if row:
                    log.info(
                        "LeaderElection: standby — leader is %s (heartbeat %.0fs ago)",
                        row[0], time.time() - row[1],
                    )
            except Exception:
                pass

            # BUG-LE-3 FIX: Add random jitter (0–2 s) to the standby poll
            # interval to prevent a thundering-herd split-brain when multiple
            # standbys simultaneously detect an expired lease and race to
            # acquire it.  The SQLite upsert is still the authoritative
            # arbiter, but jitter reduces contention significantly.
            jitter = random.uniform(0, 2)
            await asyncio.sleep(_HEARTBEAT_INTERVAL + jitter)

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

        # BUG-LE-4 FIX: set _is_leader = False BEFORE the DB delete so that if
        # the delete raises an exception the instance is already demoted and will
        # not continue processing messages believing it is still the leader.
        self._is_leader = False

        try:
            _db_execute(
                self._conn,
                "DELETE FROM leader_election WHERE singleton = 1 AND instance_id = ?",
                (_INSTANCE_ID,),
            )
            self._conn.commit()
            log.info("LeaderElection: released lease (%s)", _INSTANCE_ID)
        except Exception as exc:
            log.warning("LeaderElection: release failed (non-fatal): %s", exc)

    async def _heartbeat_loop(self) -> None:
        """Periodically update heartbeat_at to keep the lease alive."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                now = time.time()
                # BUG-LE-2 FIX: use timeout-guarded execute in executor so a
                # stuck DB does not freeze the event loop here either.
                cur = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: _db_execute(self._conn, _HEARTBEAT, (now, _INSTANCE_ID)),
                )
                self._conn.commit()
                if cur.rowcount == 0:
                    # Another instance stole the lease (genuine split-brain)
                    log.warning(
                        "LeaderElection: lease lost! Another instance became leader. Shutting down."
                    )
                    self._is_leader = False
                    # Reset failure counter — this is a clean demotion
                    self._consecutive_hb_failures = 0
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                # Successful heartbeat: reset failure counter
                self._consecutive_hb_failures = 0
                log.debug("LeaderElection: heartbeat renewed (%.0f)", now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # BUG-LE-5 FIX: Do not silently continue after DB errors.
                # Count consecutive failures.  After _MAX_CONSECUTIVE_HEARTBEAT_FAILURES
                # we cannot guarantee we are still leader (another instance may have
                # taken over while our heartbeat was failing), so we self-demote
                # and signal shutdown rather than continuing to act as leader.
                self._consecutive_hb_failures += 1
                log.error(
                    "LeaderElection: heartbeat failed (%d/%d): %s",
                    self._consecutive_hb_failures,
                    _MAX_CONSECUTIVE_HEARTBEAT_FAILURES,
                    exc,
                )
                if self._consecutive_hb_failures >= _MAX_CONSECUTIVE_HEARTBEAT_FAILURES:
                    log.error(
                        "LeaderElection: too many consecutive heartbeat failures — "
                        "self-demoting to avoid split-brain. Sending SIGTERM."
                    )
                    self._is_leader = False
                    os.kill(os.getpid(), signal.SIGTERM)
                    return

"""Tests for LeaderElection — using in-memory SQLite."""
import asyncio
import sqlite3
import os
import pytest
from unittest.mock import patch


@pytest.fixture
def mem_conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.mark.asyncio
async def test_no_op_when_disabled(mem_conn):
    """When LEADER_ELECTION_ENABLED=false, acquire() is instant and is_leader is True."""
    with patch.dict(os.environ, {"LEADER_ELECTION_ENABLED": "false"}):
        import importlib
        import host.leader_election as m
        importlib.reload(m)
        le = m.LeaderElection(mem_conn)
        assert le.is_leader is True
        await le.acquire()
        assert le.is_leader is True
        await le.release()


@pytest.mark.asyncio
async def test_acquire_when_no_existing_leader(mem_conn):
    """First instance acquires the lease immediately."""
    with patch.dict(os.environ, {
        "LEADER_ELECTION_ENABLED": "true",
        "INSTANCE_ID": "test-instance-1",
        "LEADER_HEARTBEAT_INTERVAL": "1",
        "LEADER_LEASE_TIMEOUT": "5",
    }):
        import importlib
        import host.leader_election as m
        importlib.reload(m)
        le = m.LeaderElection(mem_conn)
        await asyncio.wait_for(le.acquire(), timeout=2.0)
        assert le.is_leader is True
        await le.release()
        assert le.is_leader is False


@pytest.mark.asyncio
async def test_release_clears_lease(mem_conn):
    """After release(), the leader_election table has no row."""
    with patch.dict(os.environ, {
        "LEADER_ELECTION_ENABLED": "true",
        "INSTANCE_ID": "test-instance-2",
        "LEADER_HEARTBEAT_INTERVAL": "1",
        "LEADER_LEASE_TIMEOUT": "5",
    }):
        import importlib
        import host.leader_election as m
        importlib.reload(m)
        le = m.LeaderElection(mem_conn)
        await asyncio.wait_for(le.acquire(), timeout=2.0)
        await le.release()
        row = mem_conn.execute("SELECT * FROM leader_election").fetchone()
        assert row is None


@pytest.mark.asyncio
async def test_expired_lease_can_be_stolen(mem_conn):
    """A new instance steals the lease when the old heartbeat is stale."""
    import time
    with patch.dict(os.environ, {
        "LEADER_ELECTION_ENABLED": "true",
        "INSTANCE_ID": "instance-thief",
        "LEADER_HEARTBEAT_INTERVAL": "1",
        "LEADER_LEASE_TIMEOUT": "2",
    }):
        import importlib
        import host.leader_election as m
        importlib.reload(m)

        # Manually insert an old heartbeat for a different instance
        mem_conn.execute(m._CREATE_TABLE)
        stale_time = time.time() - 100  # 100 seconds ago — well expired
        mem_conn.execute(
            "INSERT INTO leader_election (singleton, instance_id, acquired_at, heartbeat_at) VALUES (1, 'old-instance', ?, ?)",
            (stale_time, stale_time)
        )
        mem_conn.commit()

        le = m.LeaderElection(mem_conn)
        await asyncio.wait_for(le.acquire(), timeout=3.0)
        assert le.is_leader is True

        # Verify the thief's ID is now in the table
        row = mem_conn.execute("SELECT instance_id FROM leader_election WHERE singleton=1").fetchone()
        assert row[0] == "instance-thief"

        await le.release()

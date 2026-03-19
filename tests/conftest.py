"""
Shared pytest fixtures for the EvoClaw test suite.

These fixtures are automatically discovered by pytest from all test files
in the tests/ directory.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure the repository root is on sys.path so all host.* imports resolve
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Shared: in-memory SQLite connection ───────────────────────────────────────

@pytest.fixture
def mem_conn():
    """
    Provide a fresh in-memory SQLite connection for each test.

    Row factory is set to sqlite3.Row so columns can be accessed by name.
    Connection is closed after the test completes.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ── Shared: temporary groups directory ───────────────────────────────────────

@pytest.fixture
def groups_dir(tmp_path):
    """
    Provide a temporary 'groups' directory that mirrors config.GROUPS_DIR.

    Each test receives its own isolated directory; no cleanup is needed.
    """
    d = tmp_path / "groups"
    d.mkdir()
    return d


# ── Shared: temporary skills directory ────────────────────────────────────────

@pytest.fixture
def skills_dir(tmp_path):
    """
    Provide a temporary 'skills' directory for SkillLoader tests.

    Isolated per test so skill operations do not bleed between tests.
    """
    d = tmp_path / "skills"
    d.mkdir()
    return d


# ── Shared: MemoryBus instance ────────────────────────────────────────────────

@pytest.fixture
def memory_bus(mem_conn, groups_dir):
    """
    Provide a MemoryBus initialised with an in-memory database and temporary
    groups directory.  VectorStore degrades gracefully when sqlite-vec is
    absent — no special setup is required.
    """
    from host.memory.memory_bus import MemoryBus
    return MemoryBus(mem_conn, groups_dir)


# ── Shared: SkillLoader instance ──────────────────────────────────────────────

@pytest.fixture
def skill_loader(skills_dir):
    """
    Provide a SkillLoader pointed at the temporary skills directory.
    """
    from host.skill_loader import SkillLoader
    return SkillLoader(skills_dir=skills_dir)

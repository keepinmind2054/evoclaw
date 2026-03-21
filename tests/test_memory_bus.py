"""
Smoke tests for host.memory.memory_bus.MemoryBus.

Covers:
- Hot layer recall (reads MEMORY.md from the groups directory)
- Shared layer FTS5 write + search (SharedMemoryStore)
- MemoryBus.remember / MemoryBus.recall integration
- MemoryBus.forget removes from shared store
- Regression tests for p14b bug fixes

All tests run without external services.  sqlite-vec is not required — the
VectorStore silently degrades to a no-op when the extension is absent.
"""
import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.memory.memory_bus import (
    Memory,
    MemoryBus,
    SharedMemoryStore,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mem_db():
    """In-memory SQLite connection with all MemoryBus schemas applied."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def groups_dir(tmp_path):
    """Temporary groups directory (mimics config.GROUPS_DIR)."""
    d = tmp_path / "groups"
    d.mkdir()
    return d


@pytest.fixture
def bus(mem_db, groups_dir):
    """A fully initialised MemoryBus using in-memory DB and temp groups dir."""
    return MemoryBus(mem_db, groups_dir)


@pytest.fixture
def shared_store(mem_db):
    """A SharedMemoryStore backed by an in-memory SQLite connection."""
    return SharedMemoryStore(mem_db)


# ── SharedMemoryStore ─────────────────────────────────────────────────────────

class TestSharedMemoryStore:
    def test_write_returns_memory_id(self, shared_store):
        """write() should return a non-empty memory_id string."""
        memory_id = shared_store.write("Test content", agent_id="agent1")
        assert isinstance(memory_id, str)
        assert len(memory_id) > 0

    def test_write_and_search_fts5(self, shared_store):
        """Content written as 'shared' scope should be found by FTS5 search."""
        shared_store.write(
            "The quick brown fox jumps",
            agent_id="agent1",
            scope="shared",
        )
        results = shared_store.search("brown fox", agent_id="agent1")
        assert len(results) >= 1
        assert any("brown fox" in r["content"] for r in results)

    def test_search_respects_private_scope(self, shared_store):
        """A private memory should only be visible to its owner agent."""
        shared_store.write(
            "Secret note",
            agent_id="agent-owner",
            scope="private",
        )
        # Owner can find it
        results_owner = shared_store.search("Secret note", agent_id="agent-owner")
        assert len(results_owner) >= 1

        # Different agent cannot see it
        results_other = shared_store.search("Secret note", agent_id="agent-stranger")
        assert len(results_other) == 0

    def test_search_shared_visible_to_all_agents(self, shared_store):
        """Shared memories should be readable by any agent."""
        shared_store.write("Shared knowledge", agent_id="agent-a", scope="shared")

        results = shared_store.search("Shared knowledge", agent_id="agent-b")
        assert len(results) >= 1

    def test_delete_removes_memory(self, shared_store):
        """delete() should remove the memory so it no longer appears in search."""
        memory_id = shared_store.write(
            "Ephemeral fact",
            agent_id="agent1",
            scope="shared",
        )
        deleted = shared_store.delete(memory_id, agent_id="agent1")
        assert deleted is True

        results = shared_store.search("Ephemeral fact", agent_id="agent1")
        assert all("Ephemeral fact" not in r["content"] for r in results)

    def test_delete_returns_false_for_missing(self, shared_store):
        """delete() on a non-existent memory_id should return False."""
        result = shared_store.delete("nonexistent-id", agent_id="agent1")
        assert result is False


# ── MemoryBus hot layer ───────────────────────────────────────────────────────

class TestMemoryBusHotLayer:
    @pytest.mark.asyncio
    async def test_recall_hot_returns_memory_md_content(self, bus, groups_dir):
        """recall() with include_sources=('hot',) should return MEMORY.md content."""
        agent_id = "test-agent"
        agent_dir = groups_dir / agent_id
        agent_dir.mkdir()
        memory_file = agent_dir / "MEMORY.md"
        memory_file.write_text("User prefers concise answers.", encoding="utf-8")

        memories = await bus.recall(
            "user preferences",
            agent_id=agent_id,
            include_sources=("hot",),
        )

        assert len(memories) == 1
        assert memories[0].source == "hot"
        assert "concise" in memories[0].content
        assert memories[0].score == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_recall_hot_returns_empty_when_no_memory_md(self, bus):
        """recall() should return no hot memories when MEMORY.md does not exist."""
        memories = await bus.recall(
            "anything",
            agent_id="agent-without-memory",
            include_sources=("hot",),
        )
        assert memories == []

    @pytest.mark.asyncio
    async def test_recall_hot_ignores_blank_memory_md(self, bus, groups_dir):
        """recall() should ignore a MEMORY.md that contains only whitespace."""
        agent_id = "blank-memory-agent"
        agent_dir = groups_dir / agent_id
        agent_dir.mkdir()
        (agent_dir / "MEMORY.md").write_text("   \n\n  ", encoding="utf-8")

        memories = await bus.recall(
            "anything",
            agent_id=agent_id,
            include_sources=("hot",),
        )
        assert memories == []


# ── MemoryBus remember/recall/forget integration ──────────────────────────────

class TestMemoryBusIntegration:
    @pytest.mark.asyncio
    async def test_remember_and_recall_shared(self, bus):
        """A remembered shared memory should appear in a subsequent recall."""
        agent_id = "integration-agent"
        await bus.remember(
            "The deployment server is at 192.168.1.100",
            agent_id=agent_id,
            scope="shared",
        )

        memories = await bus.recall(
            "deployment server",
            agent_id=agent_id,
            include_sources=("shared",),
        )

        assert len(memories) >= 1
        assert any("192.168.1.100" in m.content for m in memories)
        assert all(isinstance(m, Memory) for m in memories)

    @pytest.mark.asyncio
    async def test_remember_returns_memory_id(self, bus):
        """remember() should return a non-empty string memory_id."""
        memory_id = await bus.remember(
            "Some fact",
            agent_id="agent1",
            scope="private",
        )
        assert isinstance(memory_id, str)
        assert len(memory_id) > 0

    @pytest.mark.asyncio
    async def test_forget_removes_from_shared(self, bus):
        """forget() should remove a memory so it no longer appears in recall."""
        agent_id = "forgettable-agent"
        memory_id = await bus.remember(
            "Temporary sensitive info",
            agent_id=agent_id,
            scope="shared",
        )

        forgot = await bus.forget(memory_id, agent_id=agent_id)
        assert forgot is True

        memories = await bus.recall(
            "Temporary sensitive info",
            agent_id=agent_id,
            include_sources=("shared",),
        )
        assert all("Temporary sensitive info" not in m.content for m in memories)

    @pytest.mark.asyncio
    async def test_recall_deduplicates_results(self, bus):
        """The same memory should not appear twice in recall results."""
        agent_id = "dedup-agent"
        await bus.remember(
            "Unique piece of knowledge",
            agent_id=agent_id,
            scope="shared",
        )

        memories = await bus.recall(
            "Unique knowledge",
            agent_id=agent_id,
            include_sources=("shared",),
            k=10,
        )

        memory_ids = [m.memory_id for m in memories]
        assert len(memory_ids) == len(set(memory_ids)), "Duplicate memory_ids in recall result"

    @pytest.mark.asyncio
    async def test_recall_respects_k_limit(self, bus):
        """recall() must not return more than k results."""
        agent_id = "limit-agent"
        for i in range(10):
            await bus.remember(f"Fact number {i}", agent_id=agent_id, scope="shared")

        memories = await bus.recall(
            "Fact",
            agent_id=agent_id,
            include_sources=("shared",),
            k=3,
        )
        assert len(memories) <= 3


# ── MemoryBus status ──────────────────────────────────────────────────────────

class TestMemoryBusStatus:
    def test_status_returns_dict_with_expected_keys(self, bus):
        """status() should return a dict containing shared_memories and vector_available."""
        status = bus.status()
        assert isinstance(status, dict)
        assert "shared_memories" in status
        assert "vector_available" in status
        assert "groups_dir" in status

    @pytest.mark.asyncio
    async def test_status_shared_count_increases_after_remember(self, bus):
        """shared_memories count should increase after remember()."""
        before = bus.status()["shared_memories"]
        await bus.remember("New fact", agent_id="agent1", scope="shared")
        after = bus.status()["shared_memories"]
        assert after == before + 1


# ── MemoryBus patch_hot_memory ────────────────────────────────────────────────

class TestMemoryBusPatchHotMemory:
    @pytest.mark.asyncio
    async def test_patch_creates_memory_md(self, bus, groups_dir):
        """patch_hot_memory() should create MEMORY.md if it does not yet exist."""
        agent_id = "patch-agent"
        (groups_dir / agent_id).mkdir(parents=True, exist_ok=True)

        result = await bus.patch_hot_memory(agent_id, "New memory patch.")
        assert result is True

        memory_file = groups_dir / agent_id / "MEMORY.md"
        assert memory_file.exists()
        assert "New memory patch." in memory_file.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_patch_appends_to_existing_memory_md(self, bus, groups_dir):
        """patch_hot_memory() should append content, not overwrite existing content."""
        agent_id = "append-agent"
        agent_dir = groups_dir / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "MEMORY.md").write_text("Initial memory.", encoding="utf-8")

        await bus.patch_hot_memory(agent_id, "Appended memory.")

        content = (agent_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert "Initial memory." in content
        assert "Appended memory." in content

    @pytest.mark.asyncio
    async def test_patch_enforces_max_bytes(self, bus, groups_dir):
        """patch_hot_memory() must not grow MEMORY.md beyond max_bytes."""
        agent_id = "size-limit-agent"
        agent_dir = groups_dir / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Fill with 7900 bytes of content then append 400 more — total would
        # exceed the 8192-byte default limit.
        initial = "x" * 7900
        (agent_dir / "MEMORY.md").write_text(initial, encoding="utf-8")

        result = await bus.patch_hot_memory(agent_id, "y" * 400, max_bytes=8192)
        assert result is True

        final = (agent_dir / "MEMORY.md").read_bytes()
        assert len(final) <= 8192, (
            f"MEMORY.md grew to {len(final)} bytes, exceeding the 8192-byte limit"
        )


# ── p14b regression tests ─────────────────────────────────────────────────────


class TestP14bDeleteAuthorization:
    """p14b-6: SharedMemoryStore.delete must NOT allow non-owners to delete shared memories."""

    def test_owner_can_delete_shared_memory(self, shared_store):
        """The agent that wrote a shared memory should be able to delete it."""
        mid = shared_store.write("Shared fact", agent_id="owner", scope="shared")
        assert shared_store.delete(mid, agent_id="owner") is True

    def test_non_owner_cannot_delete_shared_memory(self, shared_store):
        """A different agent must NOT be able to delete another agent's shared memory."""
        mid = shared_store.write("Shared fact", agent_id="owner", scope="shared")
        deleted = shared_store.delete(mid, agent_id="attacker")
        assert deleted is False, (
            "Non-owner was able to delete a shared memory (authorization bypass)"
        )
        # Memory should still be searchable by anyone.
        results = shared_store.search("Shared fact", agent_id="attacker")
        assert len(results) >= 1

    def test_non_owner_cannot_delete_project_memory(self, shared_store):
        """A different agent must NOT be able to delete a project-scoped memory."""
        mid = shared_store.write(
            "Project secret",
            agent_id="owner",
            scope="project",
            project="proj-x",
        )
        deleted = shared_store.delete(mid, agent_id="intruder")
        assert deleted is False


class TestP14bVectorScoreCap:
    """p14b-7: Vector result scores must stay within [0.0, 1.0]."""

    @pytest.mark.asyncio
    async def test_recall_scores_within_bounds(self, bus):
        """All Memory objects returned by recall() must have score in [0.0, 1.0]."""
        agent_id = "score-check-agent"
        for i in range(3):
            await bus.remember(f"Fact {i}", agent_id=agent_id, scope="shared")

        memories = await bus.recall(
            "Fact",
            agent_id=agent_id,
            include_sources=("shared",),
            k=10,
        )
        for m in memories:
            assert 0.0 <= m.score <= 1.0, (
                f"Memory score {m.score} is outside [0.0, 1.0] range for source={m.source}"
            )


class TestP14bSummarizerOutputValidation:
    """p14b-12/13: MemorySummarizer must validate LLM output before storing."""

    def test_looks_like_summary_accepts_bullets(self):
        from host.memory.summarizer import _looks_like_summary
        assert _looks_like_summary("- User prefers Python\n- Deadline is March 31\n") is True
        assert _looks_like_summary("• Key fact one\n• Key fact two\n") is True
        assert _looks_like_summary("bullet [important thing]\n") is True

    def test_looks_like_summary_rejects_garbage(self):
        from host.memory.summarizer import _looks_like_summary
        assert _looks_like_summary("") is False
        assert _looks_like_summary("   \n\n  ") is False
        assert _looks_like_summary("Error: rate limit exceeded") is False
        assert _looks_like_summary("ok") is False

    @pytest.mark.asyncio
    async def test_compress_memory_rejects_larger_output(self, tmp_path):
        """compress_memory() must fall back to truncation if LLM returns larger content."""
        from host.memory.summarizer import MemorySummarizer, COMPRESS_THRESHOLD

        summarizer = MemorySummarizer()

        # Build content that is well above the threshold and above the
        # target_bytes (4096) so _truncate_memory actually reduces the size.
        # 1200 × "- fact\n" = 8400 bytes, above both COMPRESS_THRESHOLD (6144)
        # and MAX_MEMORY_BYTES (8192).
        original_content = "- fact\n" * 1200
        original_size = len(original_content.encode("utf-8"))
        assert original_size > COMPRESS_THRESHOLD

        # LLM returns something even larger than the original.
        inflated_result = "x" * (original_size + 1000)

        async def _fake_llm(prompt, max_tokens=300):
            return inflated_result

        summarizer._call_llm = _fake_llm

        result = await summarizer.compress_memory("agent1", original_content, target_bytes=4096)
        # compress_memory must NOT store the inflated result — it must fall
        # back to _truncate_memory which keeps only the last 4096 bytes.
        result_size = len(result.encode("utf-8"))
        assert result_size <= 4096 + 200, (  # +200 for the "<!-- memory compressed -->" header
            f"compress_memory() stored a {result_size}-byte result for a {original_size}-byte "
            f"input when the LLM returned {len(inflated_result)} bytes (inflated)"
        )
        assert result_size < original_size, (
            "compress_memory() returned a result not smaller than the original"
        )

    @pytest.mark.asyncio
    async def test_compress_memory_rejects_empty_output(self):
        """compress_memory() must fall back to truncation if LLM returns empty string."""
        from host.memory.summarizer import MemorySummarizer

        summarizer = MemorySummarizer()

        async def _empty_llm(prompt, max_tokens=300):
            return ""

        summarizer._call_llm = _empty_llm

        content = "- fact\n" * 300
        result = await summarizer.compress_memory("agent1", content)
        # Should get the truncated version, not an empty string.
        assert len(result) > 0


class TestP14bHotMemoryTruncation:
    """p14b-1: UTF-8 safe truncation in hot memory."""

    def test_safe_truncate_utf8_does_not_split_multibyte(self):
        from host.memory.hot import _safe_truncate_utf8
        # 3-byte UTF-8 chars (e.g. Japanese CJK)
        text = "あ" * 5000  # each "あ" is 3 bytes → 15000 bytes total
        result = _safe_truncate_utf8(text, 8192)
        # result must be valid UTF-8 (no UnicodeDecodeError)
        result.encode("utf-8")
        assert len(result.encode("utf-8")) <= 8192
        # Must not end with a partial sequence — re-encoding must be stable
        assert result.encode("utf-8").decode("utf-8") == result

    def test_safe_truncate_within_limit_unchanged(self):
        from host.memory.hot import _safe_truncate_utf8
        text = "hello world"
        assert _safe_truncate_utf8(text, 8192) == text

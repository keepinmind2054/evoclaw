"""
tests/test_memory_utf8.py — Phase 27B coverage for BUG-P26A-3 and BUG-P26B-2.

BUG-P26A-3 (memory_bus.patch_hot_memory UTF-8 truncation):
    The naive encoded[:max_bytes].decode("utf-8", errors="ignore") silently
    discards the last partial multi-byte character at the cut boundary.
    Fix: use _safe_truncate_utf8() (the same helper as hot.py) which walks
    back from the cut point to a valid UTF-8 boundary before decoding.

BUG-P26B-2 (MEMORY.md encoding):
    recall() previously called hot_path.read_text(encoding="utf-8") without
    the errors="ignore" argument.  A MEMORY.md file that contains non-UTF-8
    bytes (e.g. written by an older agent or corrupted on disk) would raise
    UnicodeDecodeError, crashing the recall loop.
    Fix: added errors="ignore" to the read_text() call so corrupted bytes are
    replaced rather than raising.

Covers:
  - patch_hot_memory() with CJK content near the byte limit doesn't corrupt
    multi-byte chars (no UnicodeDecodeError on re-read, no partial sequences)
  - MEMORY.md with non-UTF-8 bytes reads successfully with replacement chars
    (recall() does NOT raise UnicodeDecodeError)
  - _safe_truncate_utf8() never produces invalid UTF-8
  - _safe_truncate_utf8() result always fits within the byte limit
  - _safe_truncate_utf8() returns unchanged text when below the limit
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.memory.hot import _safe_truncate_utf8, HOT_MEMORY_MAX_BYTES
from host.memory.memory_bus import MemoryBus
import sqlite3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def groups_dir(tmp_path):
    d = tmp_path / "groups"
    d.mkdir()
    return d


@pytest.fixture
def bus(groups_dir):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return MemoryBus(conn, groups_dir)


# ---------------------------------------------------------------------------
# _safe_truncate_utf8 unit tests (BUG-P26A-3 / BUG-MB-01)
# ---------------------------------------------------------------------------

class TestSafeTruncateUtf8:
    """Direct unit tests for the _safe_truncate_utf8 helper."""

    def test_cjk_content_does_not_split_multibyte(self):
        """
        CJK characters (3 bytes each in UTF-8) near the byte limit must not be
        split, leaving invalid continuation bytes.
        """
        # Each "あ" is 3 bytes; 2731 chars × 3 bytes = 8193 bytes → 1 byte over 8192
        text = "あ" * 2731
        assert len(text.encode("utf-8")) > 8192

        result = _safe_truncate_utf8(text, 8192)

        # Result must be valid UTF-8 (no decode error)
        encoded = result.encode("utf-8")
        encoded.decode("utf-8")  # raises UnicodeDecodeError if invalid

        # Must stay within the byte limit
        assert len(encoded) <= 8192

        # Round-trip must be stable (no partial sequences)
        assert encoded.decode("utf-8") == result

    def test_4byte_emoji_not_split(self):
        """
        4-byte UTF-8 characters (emoji) must not be split at the truncation
        boundary (splitting a 4-byte char would leave 1–3 orphan continuation bytes).
        """
        # U+1F600 GRINNING FACE = 4 bytes; choose count so total just exceeds limit
        limit = 100
        emoji = "\U0001F600"  # 4 bytes
        text = emoji * (limit // 4 + 2)  # slightly over limit

        result = _safe_truncate_utf8(text, limit)
        encoded = result.encode("utf-8")

        assert len(encoded) <= limit
        encoded.decode("utf-8")  # must not raise

    def test_within_limit_unchanged(self):
        """Text whose encoded size is at or below max_bytes is returned unchanged."""
        text = "hello world — short enough"
        assert len(text.encode("utf-8")) < 8192
        result = _safe_truncate_utf8(text, 8192)
        assert result == text

    def test_exactly_at_limit_unchanged(self):
        """Text whose byte size exactly equals max_bytes is returned unchanged."""
        # Build a string whose UTF-8 encoding is exactly 100 bytes
        text = "A" * 100
        result = _safe_truncate_utf8(text, 100)
        assert result == text

    def test_result_never_exceeds_max_bytes(self):
        """The result's encoded length must never exceed max_bytes."""
        # Use 2-byte chars (U+00E9 é)
        text = "é" * 5000  # 10000 bytes
        result = _safe_truncate_utf8(text, 8192)
        assert len(result.encode("utf-8")) <= 8192

    def test_pure_ascii_truncation_exact(self):
        """ASCII-only text truncation is byte-exact (1 byte per char)."""
        text = "x" * 200
        result = _safe_truncate_utf8(text, 100)
        assert len(result.encode("utf-8")) == 100
        assert result == "x" * 100

    def test_empty_string_returns_empty(self):
        """Empty input returns empty string without errors."""
        result = _safe_truncate_utf8("", 8192)
        assert result == ""


# ---------------------------------------------------------------------------
# patch_hot_memory UTF-8 safety (BUG-P26A-3 / BUG-MB-01)
# ---------------------------------------------------------------------------

class TestPatchHotMemoryUtf8:
    """BUG-P26A-3: patch_hot_memory() must not corrupt multi-byte chars at truncation."""

    @pytest.mark.asyncio
    async def test_cjk_content_near_limit_no_corruption(self, bus, groups_dir):
        """
        Filling MEMORY.md with CJK content near the 8192-byte limit and then
        patching should not produce invalid UTF-8.
        """
        agent_id = "cjk-agent"
        (groups_dir / agent_id).mkdir(parents=True, exist_ok=True)

        # Fill with 8100 bytes of CJK ('あ' = 3 bytes each → 2700 chars)
        # so that appending more CJK will trigger truncation at a 3-byte boundary
        initial = "あ" * 2700  # 8100 bytes
        (groups_dir / agent_id / "MEMORY.md").write_text(initial, encoding="utf-8")

        # Patch with more CJK content — this will push total well over 8192 bytes
        await bus.patch_hot_memory(agent_id, "い" * 100, max_bytes=8192)

        # Read back and verify it is valid UTF-8 and within limit
        memory_path = groups_dir / agent_id / "MEMORY.md"
        raw_bytes = memory_path.read_bytes()
        assert len(raw_bytes) <= 8192, (
            f"MEMORY.md is {len(raw_bytes)} bytes, exceeds 8192-byte limit"
        )
        # Must decode without error
        decoded = raw_bytes.decode("utf-8")
        # Round-trip must be stable
        assert decoded.encode("utf-8") == raw_bytes

    @pytest.mark.asyncio
    async def test_patch_result_is_valid_utf8(self, bus, groups_dir):
        """After patching near the limit, MEMORY.md must decode without errors."""
        agent_id = "utf8-check-agent"
        (groups_dir / agent_id).mkdir(parents=True, exist_ok=True)

        # Use 2-byte chars (é = U+00E9) near the 1024-byte limit
        initial = "é" * 480  # 960 bytes
        (groups_dir / agent_id / "MEMORY.md").write_text(initial, encoding="utf-8")

        await bus.patch_hot_memory(agent_id, "ñ" * 50, max_bytes=1024)

        raw = (groups_dir / agent_id / "MEMORY.md").read_bytes()
        assert len(raw) <= 1024
        raw.decode("utf-8")  # must not raise

    @pytest.mark.asyncio
    async def test_patch_does_not_produce_partial_sequences(self, bus, groups_dir):
        """
        Byte-level check: no continuation byte (0x80–0xBF) appears at position 0
        (which would indicate a split multi-byte sequence at the start of the file,
        or generally invalid UTF-8 structure).
        """
        agent_id = "no-partial-agent"
        (groups_dir / agent_id).mkdir(parents=True, exist_ok=True)

        # Approx 8180 bytes of 3-byte CJK
        initial = "漢" * 2726  # 8178 bytes
        (groups_dir / agent_id / "MEMORY.md").write_text(initial, encoding="utf-8")

        await bus.patch_hot_memory(agent_id, "字" * 10, max_bytes=8192)

        raw = (groups_dir / agent_id / "MEMORY.md").read_bytes()
        # A standalone continuation byte at the cut would be 0x80–0xBF
        # Verify by simply attempting to decode
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            pytest.fail(
                f"patch_hot_memory produced invalid UTF-8: {exc}\n"
                f"Raw tail (last 10 bytes): {raw[-10:]!r}"
            )


# ---------------------------------------------------------------------------
# MEMORY.md non-UTF-8 encoding robustness (BUG-P26B-2)
# ---------------------------------------------------------------------------

class TestMemoryMdEncodingRobustness:
    """BUG-P26B-2: recall() must not raise when MEMORY.md contains non-UTF-8 bytes."""

    @pytest.mark.asyncio
    async def test_non_utf8_memory_md_does_not_raise(self, bus, groups_dir):
        """
        MEMORY.md containing raw non-UTF-8 bytes (e.g. Latin-1 encoded text)
        must not cause recall() to raise UnicodeDecodeError.
        """
        agent_id = "latin1-agent"
        agent_dir = groups_dir / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Write Latin-1 bytes that are invalid UTF-8 (0x80–0xBF are lone continuation bytes)
        # Use only characters in the Latin-1 range (0x00–0xFF)
        latin1_content = "Caf\xe9 au lait r\xe9sum\xe9".encode("latin-1")
        (agent_dir / "MEMORY.md").write_bytes(latin1_content)

        # recall() must not raise
        try:
            memories = await bus.recall(
                "cafe",
                agent_id=agent_id,
                include_sources=("hot",),
            )
        except UnicodeDecodeError as exc:
            pytest.fail(
                f"recall() raised UnicodeDecodeError for non-UTF-8 MEMORY.md: {exc}"
            )

        # May return 0 or 1 results depending on whether content is blank after decode
        assert isinstance(memories, list)

    @pytest.mark.asyncio
    async def test_null_byte_in_memory_md_does_not_raise(self, bus, groups_dir):
        """
        MEMORY.md with embedded null bytes must not crash recall().
        (Null bytes are valid bytes but invalid in UTF-8 text context for some
        decoders; errors="ignore" handles them gracefully.)
        """
        agent_id = "null-byte-agent"
        agent_dir = groups_dir / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)

        # A mix of valid UTF-8 and a null byte
        content_bytes = "important fact\x00hidden".encode("utf-8")
        (agent_dir / "MEMORY.md").write_bytes(content_bytes)

        try:
            memories = await bus.recall(
                "fact",
                agent_id=agent_id,
                include_sources=("hot",),
            )
        except Exception as exc:
            pytest.fail(f"recall() raised unexpectedly for MEMORY.md with null byte: {exc}")

        assert isinstance(memories, list)

    @pytest.mark.asyncio
    async def test_corrupted_bytes_replaced_not_raised(self, bus, groups_dir):
        """
        Completely corrupted bytes (invalid UTF-8 sequences like 0xFF 0xFE)
        must be replaced (errors="ignore") not raise.
        """
        agent_id = "corrupt-agent"
        agent_dir = groups_dir / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)

        # 0xFF and 0xFE are never valid in UTF-8
        corrupted = b"valid start\xff\xfe invalid end"
        (agent_dir / "MEMORY.md").write_bytes(corrupted)

        try:
            memories = await bus.recall(
                "valid",
                agent_id=agent_id,
                include_sources=("hot",),
            )
        except UnicodeDecodeError as exc:
            pytest.fail(
                f"recall() raised UnicodeDecodeError for corrupted MEMORY.md: {exc}"
            )

        assert isinstance(memories, list)

    @pytest.mark.asyncio
    async def test_valid_utf8_memory_md_still_works(self, bus, groups_dir):
        """Normal UTF-8 MEMORY.md continues to work correctly after the fix."""
        agent_id = "valid-utf8-agent"
        agent_dir = groups_dir / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "MEMORY.md").write_text(
            "User prefers Python. Deadline is next Friday.", encoding="utf-8"
        )

        memories = await bus.recall(
            "Python",
            agent_id=agent_id,
            include_sources=("hot",),
        )

        assert len(memories) == 1
        assert "Python" in memories[0].content
        assert memories[0].source == "hot"

    @pytest.mark.asyncio
    async def test_cjk_utf8_memory_md_reads_correctly(self, bus, groups_dir):
        """CJK content in MEMORY.md (valid UTF-8) is read without loss."""
        agent_id = "cjk-read-agent"
        agent_dir = groups_dir / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "MEMORY.md").write_text("用戶偏好簡短回答。", encoding="utf-8")

        memories = await bus.recall(
            "用戶",
            agent_id=agent_id,
            include_sources=("hot",),
        )

        assert len(memories) == 1
        assert "用戶" in memories[0].content

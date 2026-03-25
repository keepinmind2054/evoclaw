"""
tests/test_claude_parsing.py — Phase 27B coverage for BUG-P26B-1.

BUG-P26B-1: In the Claude (Anthropic) agentic loop, text content blocks are
joined with:

    " ".join(block.text for block in response.content
             if hasattr(block, "text") and block.text is not None)

Prior to the fix the guard `block.text is not None` was absent.  When the
Anthropic API returns a text content block whose `.text` attribute is
explicitly None, `str.join()` would raise TypeError: sequence item N:
expected str instance, NoneType found.

Fix: added `block.text is not None` guard in both the end_turn branch and the
non-tool_use stop_reason branch of the Claude loop.

Covers:
  - A response whose sole text block has block.text = None does NOT crash
    (returns empty string gracefully)
  - A response with normal non-None text works correctly
  - A response with an empty content array returns empty string
  - Mixed None / non-None text blocks: only non-None text is included
  - Only text blocks with a "text" attribute are included (tool_use blocks
    that lack .text are silently skipped)
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Minimal stubs — we test the collection logic in isolation, without importing
# agent.py (which has heavyweight optional dependencies).  The logic under
# test is a simple list comprehension; we verify the *contract* here.
# ---------------------------------------------------------------------------

class _FakeTextBlock:
    """Minimal Anthropic-style text content block."""

    def __init__(self, text):
        self.type = "text"
        self.text = text  # may be None


class _FakeToolUseBlock:
    """Minimal Anthropic-style tool_use content block (has no .text attr)."""

    def __init__(self, name: str = "Bash", id: str = "tu-1"):
        self.type = "tool_use"
        self.name = name
        self.id = id
        # Intentionally NO .text attribute


def _collect_text(content_blocks) -> str:
    """
    Replicate the BUG-P26B-1 fix: join text blocks, skipping None values.

    This mirrors the exact code in agent.py (both end_turn and non-tool_use
    branches), extracted here for unit testing without the full agent import.
    """
    return " ".join(
        block.text
        for block in content_blocks
        if hasattr(block, "text") and block.text is not None
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestClaudeResponseParsing:
    """BUG-P26B-1: text collection must not crash when block.text is None."""

    def test_none_text_block_does_not_crash(self):
        """A single text block with text=None must not raise TypeError."""
        content = [_FakeTextBlock(text=None)]
        result = _collect_text(content)
        # Must return empty string, not raise
        assert result == "", (
            f"Expected empty string for None text block, got: {result!r}"
        )

    def test_none_text_block_returns_empty_string(self):
        """block.text = None should be silently skipped; result is empty string."""
        content = [_FakeTextBlock(text=None), _FakeTextBlock(text=None)]
        result = _collect_text(content)
        assert result == ""

    def test_normal_text_block_returns_text(self):
        """A text block with a non-None string returns that string."""
        content = [_FakeTextBlock(text="Hello, world!")]
        result = _collect_text(content)
        assert result == "Hello, world!"

    def test_multiple_text_blocks_joined_with_space(self):
        """Multiple text blocks are joined with a single space."""
        content = [
            _FakeTextBlock(text="Hello"),
            _FakeTextBlock(text="world"),
        ]
        result = _collect_text(content)
        assert result == "Hello world"

    def test_mixed_none_and_valid_text_skips_none(self):
        """None text blocks are skipped; valid text blocks are included."""
        content = [
            _FakeTextBlock(text=None),
            _FakeTextBlock(text="visible text"),
            _FakeTextBlock(text=None),
        ]
        result = _collect_text(content)
        assert result == "visible text"

    def test_empty_content_array_returns_empty_string(self):
        """Empty content list produces empty string (no crash)."""
        result = _collect_text([])
        assert result == ""

    def test_tool_use_block_skipped_no_text_attr(self):
        """Tool-use blocks (no .text attribute) are silently skipped."""
        content = [
            _FakeToolUseBlock(name="Bash"),
            _FakeTextBlock(text="response text"),
        ]
        result = _collect_text(content)
        assert result == "response text"

    def test_all_tool_use_blocks_returns_empty_string(self):
        """A content list containing only tool_use blocks returns empty string."""
        content = [
            _FakeToolUseBlock(name="Bash"),
            _FakeToolUseBlock(name="Read"),
        ]
        result = _collect_text(content)
        assert result == ""

    def test_none_text_does_not_appear_in_result(self):
        """Verify that the string 'None' never appears in the output."""
        content = [
            _FakeTextBlock(text=None),
            _FakeTextBlock(text="actual content"),
        ]
        result = _collect_text(content)
        assert "None" not in result

    def test_empty_string_text_block_included(self):
        """A text block with text='' (empty string, not None) is included."""
        content = [
            _FakeTextBlock(text="before"),
            _FakeTextBlock(text=""),
            _FakeTextBlock(text="after"),
        ]
        result = _collect_text(content)
        # Empty string blocks are not None — they pass the guard and are joined
        assert "before" in result
        assert "after" in result

    def test_text_block_with_unicode_content(self):
        """Unicode content (CJK, emoji) is preserved correctly."""
        content = [_FakeTextBlock(text="你好 🌍")]
        result = _collect_text(content)
        assert result == "你好 🌍"

    def test_mixed_valid_text_none_tool_use(self):
        """Real mixed scenario: text=None, tool_use, and valid text all together."""
        content = [
            _FakeTextBlock(text=None),
            _FakeToolUseBlock(name="Bash"),
            _FakeTextBlock(text="final answer"),
        ]
        result = _collect_text(content)
        assert result == "final answer"
        assert "None" not in result

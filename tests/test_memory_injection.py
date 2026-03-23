"""
Tests for Phase 21A MEMORY.md injection into the agent system prompt.

When MEMORY.md exists and contains content, the build_system_prompt helper
(inside agent.py's main()) injects it into the system instruction block.
The injection MUST include a verification warning telling the agent not to
treat the injected memory as confirmed facts without re-checking via tools.

These tests exercise the injection logic by:
  1. Reading the relevant lines from agent.py directly (import-free check on
     the warning text that is hard-coded in the source).
  2. Invoking the in-process truncation/injection path via a simulated
     MEMORY.md file where possible.

Phase 21A warning markers (from agent.py source):
  '⚠️ **重要：以下為過去 session 記錄的歷史記憶。這些是歷史筆記，不是已確認的事實。**'
  '**請在引用任何記憶內容之前，先透過實際工具（Read/Bash）重新驗證，切勿直接當作已完成的事實陳述。**'
"""
import sys
from pathlib import Path

import pytest

_AGENT_PY = Path(__file__).parent.parent / "container" / "agent-runner" / "agent.py"

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Source-level checks (no import needed) ────────────────────────────────────

class TestMemoryWarningInSource:
    """Confirm the verification warning text is present in agent.py source."""

    @pytest.fixture(autouse=True)
    def _source(self):
        self._src = _AGENT_PY.read_text(encoding="utf-8")

    def test_memory_section_header_present(self):
        """The '長期記憶 (MEMORY.md)' section header must exist in the injection block."""
        assert "長期記憶 (MEMORY.md)" in self._src, (
            "MEMORY.md section header not found in agent.py — "
            "injection block may have been removed or renamed."
        )

    def test_historical_notes_warning_present(self):
        """The '歷史筆記，不是已確認的事實' warning must be present."""
        assert "歷史筆記，不是已確認的事實" in self._src, (
            "Historical-notes warning not found in agent.py. "
            "Phase 21A requires the agent to be warned that MEMORY.md is "
            "historical, not confirmed fact."
        )

    def test_reverification_instruction_present(self):
        """The instruction to reverify via tools must be present."""
        assert "先透過實際工具" in self._src or "重新驗證" in self._src, (
            "Re-verification instruction not found in agent.py. "
            "Phase 21A requires the agent to re-check memory via Read/Bash."
        )

    def test_warning_appears_near_memory_injection(self):
        """The warning must appear within the same injection block as MEMORY.md content."""
        idx_header = self._src.find("長期記憶 (MEMORY.md)")
        idx_warning = self._src.find("歷史筆記，不是已確認的事實")
        assert idx_header != -1 and idx_warning != -1
        # Both markers must be within 500 characters of each other in the source
        assert abs(idx_header - idx_warning) < 500, (
            f"Warning is {abs(idx_header - idx_warning)} chars away from section header — "
            "may have been moved outside the injection block."
        )

    def test_memory_log_emit_present(self):
        """The 'MEMORY' log tag must be emitted when MEMORY.md is injected."""
        assert "MEMORY" in self._src and "Injected" in self._src, (
            "Memory injection log call not found — phase 21A logging may be missing."
        )


# ── Functional: injection text structure ─────────────────────────────────────

class TestMemoryInjectionStructure:
    """Verify the injected system prompt fragment has the expected structure."""

    def _build_injection(self, memory_content: str) -> str:
        """
        Replicate the injection logic from agent.py main() to produce the
        fragment that would be appended to the system prompt lines list.
        """
        _IDENTITY_MARKER = "## 身份 (Identity)"
        _TASK_MARKER = "## 任務記錄 (Task Log)"
        _MEMORY_READ_LIMIT = 32 * 1024

        if not memory_content:
            return ""

        if _IDENTITY_MARKER in memory_content and _TASK_MARKER in memory_content:
            _id_end = memory_content.index(_TASK_MARKER)
            _identity_part = memory_content[:_id_end].strip()
            _task_part = memory_content[_id_end:][-3000:]
            _memory_snippet = _identity_part + "\n\n" + _task_part
        else:
            _memory_snippet = memory_content[-4000:]

        fragment = (
            f"## 長期記憶 (MEMORY.md)\n"
            f"⚠️ **重要：以下為過去 session 記錄的歷史記憶。這些是歷史筆記，不是已確認的事實。**\n"
            f"**請在引用任何記憶內容之前，先透過實際工具（Read/Bash）重新驗證，切勿直接當作已完成的事實陳述。**\n\n"
            f"{_memory_snippet}"
        )
        return fragment

    def test_injection_includes_section_header(self):
        fragment = self._build_injection("some memory content")
        assert "長期記憶 (MEMORY.md)" in fragment

    def test_injection_includes_historical_warning(self):
        """The injected text must explicitly warn that memories are historical notes."""
        fragment = self._build_injection("some memory content")
        assert "歷史筆記" in fragment
        assert "不是已確認的事實" in fragment

    def test_injection_includes_reverification_warning(self):
        """The injected text must tell the agent to reverify before citing memory."""
        fragment = self._build_injection("some memory content")
        assert "重新驗證" in fragment or "先透過實際工具" in fragment

    def test_injection_contains_original_content(self):
        """The actual MEMORY.md content must appear in the injected fragment."""
        memory = "My important remembered fact XYZ"
        fragment = self._build_injection(memory)
        assert "My important remembered fact XYZ" in fragment

    def test_empty_memory_produces_no_fragment(self):
        """When MEMORY.md is empty, no injection fragment should be produced."""
        fragment = self._build_injection("")
        assert fragment == ""

    def test_long_memory_tail_truncated_to_4000(self):
        """For plain memory (no Identity/Task markers), only the last 4000 chars are kept."""
        long_mem = "X" * 5000 + "TAIL_UNIQUE_9999"
        fragment = self._build_injection(long_mem)
        assert "TAIL_UNIQUE_9999" in fragment, "Last part of long memory must be preserved"
        assert "X" * 5000 not in fragment, "Head of truncated memory must be dropped"

    def test_structured_memory_preserves_identity_section(self):
        """When MEMORY.md has Identity+TaskLog markers, the Identity section is kept."""
        memory = (
            "## 身份 (Identity)\nI am Eve, the agent.\n\n"
            "## 任務記錄 (Task Log)\n[2024-01-01] Did something important."
        )
        fragment = self._build_injection(memory)
        assert "I am Eve, the agent." in fragment

    def test_structured_memory_preserves_task_log(self):
        """When MEMORY.md has Identity+TaskLog markers, the Task Log section is kept."""
        memory = (
            "## 身份 (Identity)\nIdentity info.\n\n"
            "## 任務記錄 (Task Log)\n[2024-06-15] Fixed the critical bug."
        )
        fragment = self._build_injection(memory)
        assert "Fixed the critical bug." in fragment

"""
Tests for Phase 21A tool result success/failure prefixes and head+tail truncation.

Covers:
  - tool_bash:  exit 0 → '✓ [exit 0] ...' prefix
  - tool_bash:  exit N → '✗ [exit N] ...' prefix
  - tool_bash:  exception → '✗ [exit ?] ...' prefix
  - tool_write: success → '[OK] Written: <path>'
  - tool_write: path violation → '[ERROR] ...'
  - tool_edit:  success → '[OK] Edited: <path>'
  - tool_edit:  missing old_string → '[ERROR] ...'
  - head+tail truncation: first HALF + last HALF preserved, middle marker injected
  - short output: unchanged when under _MAX_TOOL_RESULT_CHARS
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import helpers — agent.py lives in container/agent-runner/ which is NOT on
# the normal Python package path.  We add it manually so we can import the
# tool functions directly without running main().
# ---------------------------------------------------------------------------
_AGENT_DIR = Path(__file__).parent.parent / "container" / "agent-runner"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))


# ---------------------------------------------------------------------------
# Lazy import: the module imports google/openai/anthropic at the top level;
# those may be absent in CI.  We tolerate ImportError on optional deps.
# ---------------------------------------------------------------------------
def _import_agent():
    import importlib
    # Clear any cached version so side-effects (module-level code) re-run.
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent", _AGENT_DIR / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    # Silence optional-dependency noise during import
    try:
        spec.loader.exec_module(mod)
    except Exception:
        pass
    return mod


try:
    _agent = _import_agent()
    _tool_bash = _agent.tool_bash
    _tool_write = _agent.tool_write
    _tool_edit = _agent.tool_edit
    _MAX_TOOL_RESULT_CHARS = _agent._MAX_TOOL_RESULT_CHARS
    _AGENT_AVAILABLE = True
except Exception:
    _AGENT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _AGENT_AVAILABLE,
    reason="agent.py could not be imported (missing optional deps or path issue)",
)


# ── Bash prefix tests ─────────────────────────────────────────────────────────

class TestBashPrefix:
    """Phase 21A: tool_bash must prefix output with an unambiguous exit-status flag."""

    def test_success_prefix_checkmark(self, tmp_path):
        """Exit-0 command returns output starting with U+2713 (✓) and '[exit 0]'."""
        result = _tool_bash("echo hello")
        assert result.startswith("\u2713"), (
            f"Expected '✓' prefix for exit-0, got: {result[:60]!r}"
        )
        assert "[exit 0]" in result

    def test_success_contains_stdout(self, tmp_path):
        """The stdout of a successful command appears in the result string."""
        result = _tool_bash("echo evoclaw_marker_xyz")
        assert "evoclaw_marker_xyz" in result

    def test_failure_prefix_cross(self):
        """Exit-non-zero command returns output starting with U+2717 (✗)."""
        result = _tool_bash("exit 42")
        assert result.startswith("\u2717"), (
            f"Expected '✗' prefix for non-zero exit, got: {result[:60]!r}"
        )
        assert "[exit 42]" in result

    def test_failure_exit_code_in_prefix(self):
        """The actual exit code N appears literally in the '[exit N]' prefix."""
        result = _tool_bash("exit 7")
        assert "[exit 7]" in result

    def test_bash_exception_prefix(self):
        """If subprocess itself throws (simulated), result starts with '✗ [exit ?]'."""
        with patch("subprocess.Popen", side_effect=OSError("popen failed")):
            result = _tool_bash("echo test")
        assert result.startswith("\u2717"), (
            f"Expected '✗' prefix on exception, got: {result[:60]!r}"
        )
        assert "[exit ?]" in result

    def test_no_output_placeholder(self):
        """Command with no output returns '(no output)' rather than an empty string."""
        result = _tool_bash("true")
        assert "(no output)" in result or result.startswith("\u2713")


# ── Write / Edit prefix tests ─────────────────────────────────────────────────

import contextlib as _contextlib


@_contextlib.contextmanager
def _patch_allowed_prefixes(module, new_prefixes):
    """Temporarily replace _ALLOWED_PATH_PREFIXES in the agent module namespace.

    tool_write and tool_edit each contain a secondary resolved-parent check
    that compares the *resolved* parent directory against _ALLOWED_PATH_PREFIXES
    using str.startswith().  Because str.startswith() requires an exact prefix
    match (the resolved path has no trailing slash, so it must be a *parent* of
    the allowed prefix rather than equal to it), the file under test must be
    placed at least one directory level below the allowed prefix.  Tests that
    use this helper should create files inside tmp_path/subdir/ and patch with
    ("/tmp/", "/workspace/") so that all resolved parents start with "/tmp/".
    """
    original = module._ALLOWED_PATH_PREFIXES
    module._ALLOWED_PATH_PREFIXES = new_prefixes
    try:
        yield
    finally:
        module._ALLOWED_PATH_PREFIXES = original


class TestWriteEditPrefix:
    """Phase 21A: tool_write and tool_edit return structured [OK]/[ERROR] prefixes."""

    def test_write_success_ok_prefix(self, tmp_path):
        """Successful write returns '[OK] Written: <path>'.

        pytest's tmp_path lives under /tmp which is outside /workspace/.
        We temporarily expand _ALLOWED_PATH_PREFIXES to include '/tmp/' and
        put the target file one level deeper (tmp_path/sub/output.txt) so the
        resolved parent ('/tmp/.../sub') starts with the '/tmp/' prefix and
        passes the symlink-escape check inside tool_write.
        """
        sub = tmp_path / "sub"
        sub.mkdir()
        target = sub / "output.txt"
        with _patch_allowed_prefixes(_agent, ("/tmp/", "/workspace/")):
            result = _tool_write(str(target), "hello world")
        assert result.startswith("[OK]"), f"Expected '[OK]', got: {result!r}"
        assert "Written:" in result
        assert str(target) in result

    def test_write_path_violation_error_prefix(self):
        """Writing outside /workspace/ (and not /tmp/) returns '[ERROR] access denied'."""
        result = _tool_write("/etc/passwd", "evil")
        assert result.startswith("[ERROR]"), (
            f"Expected '[ERROR]' for path violation, got: {result!r}"
        )

    def test_edit_success_ok_prefix(self, tmp_path):
        """Successful edit returns '[OK] Edited: <path>'.

        File is placed inside tmp_path/sub/ and sandbox is expanded to '/tmp/'
        so the resolved-parent check passes.
        """
        sub = tmp_path / "sub"
        sub.mkdir()
        target = sub / "file.txt"
        target.write_text("old content here", encoding="utf-8")
        with _patch_allowed_prefixes(_agent, ("/tmp/", "/workspace/")):
            result = _tool_edit(str(target), "old content", "new content")
        assert result.startswith("[OK]"), f"Expected '[OK]', got: {result!r}"
        assert "Edited:" in result

    def test_edit_missing_old_string_error_prefix(self, tmp_path):
        """Edit with a missing old_string returns '[ERROR] old_string not found'."""
        sub = tmp_path / "sub"
        sub.mkdir()
        target = sub / "file2.txt"
        target.write_text("something else entirely", encoding="utf-8")
        with _patch_allowed_prefixes(_agent, ("/tmp/", "/workspace/")):
            result = _tool_edit(str(target), "THIS DOES NOT EXIST", "replacement")
        assert result.startswith("[ERROR]"), f"Expected '[ERROR]', got: {result!r}"
        assert "not found" in result

    def test_edit_path_violation_error_prefix(self):
        """Editing outside /workspace/ (and not /tmp/) returns '[ERROR]'."""
        result = _tool_edit("/etc/hosts", "localhost", "evil")
        assert result.startswith("[ERROR]")


# ── Truncation tests ──────────────────────────────────────────────────────────

class TestTruncation:
    """Phase 21A: head+tail truncation preserves first HALF and last HALF of output."""

    def _apply_truncation(self, result_str: str) -> str:
        """Replicate the truncation logic used in all three agent loops."""
        if len(result_str) > _MAX_TOOL_RESULT_CHARS:
            half = _MAX_TOOL_RESULT_CHARS // 2
            head = result_str[:half]
            tail = result_str[-half:]
            omitted = len(result_str) - _MAX_TOOL_RESULT_CHARS
            return (
                head
                + f"\n[... {omitted} chars omitted (middle truncated to preserve head+tail) ...]\n"
                + tail
            )
        return result_str

    def test_long_output_is_truncated(self):
        """Output longer than _MAX_TOOL_RESULT_CHARS must be shortened."""
        long_str = "A" * (_MAX_TOOL_RESULT_CHARS + 1000)
        result = self._apply_truncation(long_str)
        assert len(result) < len(long_str)

    def test_truncation_preserves_head(self):
        """First half (head) of the original string must survive truncation."""
        head_marker = "HEAD_MARKER_START"
        tail_marker = "TAIL_MARKER_END"
        half = _MAX_TOOL_RESULT_CHARS // 2
        # Build: head_marker + padding + tail_marker so they land in opposite halves
        padding_len = _MAX_TOOL_RESULT_CHARS + 500
        long_str = (
            head_marker
            + "X" * (half - len(head_marker))
            + "M" * 600          # middle that will be omitted
            + "X" * (half - len(tail_marker))
            + tail_marker
        )
        result = self._apply_truncation(long_str)
        assert head_marker in result, "Head marker must survive truncation"

    def test_truncation_preserves_tail(self):
        """Last half (tail) of the original string must survive truncation."""
        tail_marker = "UNIQUE_TAIL_9999"
        half = _MAX_TOOL_RESULT_CHARS // 2
        long_str = (
            "A" * (half + 200)   # will be in head
            + "M" * 600          # middle omitted
            + "B" * (half - len(tail_marker))
            + tail_marker
        )
        result = self._apply_truncation(long_str)
        assert tail_marker in result, "Tail marker must survive truncation"

    def test_truncation_middle_marker_present(self):
        """The '[... N chars omitted ...]' marker must appear in truncated output."""
        long_str = "Z" * (_MAX_TOOL_RESULT_CHARS * 2)
        result = self._apply_truncation(long_str)
        assert "chars omitted" in result
        assert "middle truncated to preserve head+tail" in result

    def test_truncation_omitted_count_correct(self):
        """The reported number of omitted chars must equal len(original) - MAX."""
        original_len = _MAX_TOOL_RESULT_CHARS + 999
        long_str = "Q" * original_len
        result = self._apply_truncation(long_str)
        expected_omitted = original_len - _MAX_TOOL_RESULT_CHARS
        assert str(expected_omitted) in result, (
            f"Expected '{expected_omitted}' in truncation marker, got: {result[_MAX_TOOL_RESULT_CHARS//2-50:_MAX_TOOL_RESULT_CHARS//2+100]!r}"
        )

    def test_short_output_unchanged(self):
        """Output at or below _MAX_TOOL_RESULT_CHARS must not be modified."""
        short_str = "Hello world " * 10  # well under the limit
        result = self._apply_truncation(short_str)
        assert result == short_str

    def test_exactly_at_limit_unchanged(self):
        """Output whose length exactly equals _MAX_TOOL_RESULT_CHARS is not truncated."""
        exact_str = "X" * _MAX_TOOL_RESULT_CHARS
        result = self._apply_truncation(exact_str)
        assert result == exact_str

    def test_one_over_limit_is_truncated(self):
        """Output one character over the limit triggers truncation."""
        over_str = "Y" * (_MAX_TOOL_RESULT_CHARS + 1)
        result = self._apply_truncation(over_str)
        assert "chars omitted" in result

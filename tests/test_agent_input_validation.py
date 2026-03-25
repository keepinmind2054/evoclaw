"""
tests/test_agent_input_validation.py — Phase 27B coverage for BUG-P26B-4 and BUG-P26B-5.

BUG-P26B-4: _check_path_allowed() previously called Path(file_path).resolve()
    before validating the input string.  An empty string resolves to the Python
    process CWD (which may be /workspace and thus pass the sandbox check), and
    a path containing a null byte raises ValueError deep inside the C runtime.
    Fix: reject empty paths and null-byte paths early, before Path() is called.

BUG-P26B-5: tool_bash() previously passed the command string verbatim to the
    shell.  A command containing a null byte would be silently truncated by the
    OS (which uses null as a C-string terminator), allowing a prompt-injection
    payload to hide commands after a null byte.
    Fix: reject any command containing a null byte before spawning the subprocess.

Covers:
  - _check_path_allowed("") returns error string (empty path rejected)
  - _check_path_allowed("test\x00evil") returns error string (null byte rejected)
  - tool_bash("echo hello\x00rm -rf /") returns error string (null byte rejected)
  - Normal valid paths still return None (no error)
  - Normal bash commands still run correctly
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Import agent.py from container/agent-runner (not on standard sys.path)
# ---------------------------------------------------------------------------

_AGENT_DIR = Path(__file__).parent.parent / "container" / "agent-runner"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))


def _import_agent():
    import importlib.util
    spec = importlib.util.spec_from_file_location("agent_p27b_val", _AGENT_DIR / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        pass
    return mod


try:
    _agent = _import_agent()
    _check_path_allowed = _agent._check_path_allowed
    _tool_bash = _agent.tool_bash
    _AGENT_AVAILABLE = True
except Exception:
    _AGENT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _AGENT_AVAILABLE,
    reason="agent.py could not be imported (missing optional deps or path issue)",
)


# ---------------------------------------------------------------------------
# _check_path_allowed — BUG-P26B-4
# ---------------------------------------------------------------------------

class TestCheckPathAllowed:
    """BUG-P26B-4: _check_path_allowed must reject empty and null-byte paths early."""

    def test_empty_path_returns_error(self):
        """An empty string path must be rejected before Path().resolve() is called."""
        result = _check_path_allowed("")
        assert result is not None, (
            "_check_path_allowed('') must return an error string, got None"
        )
        assert isinstance(result, str)
        # Error message must be meaningful
        assert len(result) > 0
        # Should mention 'empty' or similar
        assert any(
            keyword in result.lower()
            for keyword in ("empty", "must not be empty", "invalid")
        ), f"Expected informative error for empty path, got: {result!r}"

    def test_null_byte_in_path_returns_error(self):
        """A path containing a null byte must be rejected before Path() is called."""
        result = _check_path_allowed("test\x00evil")
        assert result is not None, (
            "_check_path_allowed('test\\x00evil') must return an error, got None"
        )
        assert isinstance(result, str)
        assert any(
            keyword in result.lower()
            for keyword in ("null", "null byte", "invalid")
        ), f"Expected null-byte error message, got: {result!r}"

    def test_null_byte_at_start_returns_error(self):
        """A path starting with a null byte must be rejected."""
        result = _check_path_allowed("\x00/workspace/file.txt")
        assert result is not None
        assert isinstance(result, str)

    def test_null_byte_at_end_returns_error(self):
        """A path ending with a null byte must be rejected."""
        result = _check_path_allowed("/workspace/file.txt\x00")
        assert result is not None
        assert isinstance(result, str)

    def test_multiple_null_bytes_returns_error(self):
        """A path with multiple null bytes must be rejected."""
        result = _check_path_allowed("a\x00b\x00c")
        assert result is not None
        assert isinstance(result, str)

    def test_valid_workspace_path_returns_none(self):
        """A valid /workspace path must return None (no error)."""
        result = _check_path_allowed("/workspace/group/test.txt")
        # Should return None (allowed) — no error
        assert result is None, (
            f"Expected None for valid /workspace path, got: {result!r}"
        )

    def test_valid_tmp_path_allowed_or_rejected(self):
        """A /tmp path may or may not be allowed; must not crash."""
        # Just verify it doesn't raise an exception — result depends on config
        result = _check_path_allowed("/tmp/some/file.txt")
        # result is None (allowed) or a non-empty error string (not allowed)
        assert result is None or isinstance(result, str)

    def test_path_outside_workspace_returns_error(self):
        """/etc/passwd is outside /workspace and must be rejected."""
        result = _check_path_allowed("/etc/passwd")
        assert result is not None, (
            "Expected error for /etc/passwd path, got None (access should be denied)"
        )
        assert isinstance(result, str)

    def test_path_with_whitespace_only_treated_as_normal(self):
        """A whitespace-only path is not empty (has bytes), but still likely denied."""
        # The function must not crash — result is either None or an error string
        try:
            result = _check_path_allowed("   ")
            assert result is None or isinstance(result, str)
        except Exception as exc:
            pytest.fail(f"_check_path_allowed('   ') raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# tool_bash — BUG-P26B-5
# ---------------------------------------------------------------------------

class TestToolBashNullByte:
    """BUG-P26B-5: tool_bash must reject commands containing null bytes."""

    def test_null_byte_in_command_returns_error(self):
        """A command with a null byte must be rejected immediately (no shell spawn)."""
        result = _tool_bash("echo hello\x00rm -rf /")
        assert result is not None
        assert isinstance(result, str)
        # Must start with the failure prefix (cross mark or [ERROR]/Error:)
        assert (
            result.startswith("\u2717")          # ✗
            or result.startswith("[ERROR]")
            or result.lower().startswith("error")
        ), f"Expected error prefix for null-byte command, got: {result[:80]!r}"

    def test_null_byte_at_start_of_command_returns_error(self):
        """Null byte at the start of a command must be rejected."""
        result = _tool_bash("\x00echo hi")
        assert result.startswith("\u2717") or "error" in result.lower()

    def test_null_byte_only_returns_error(self):
        """A command consisting solely of a null byte must be rejected."""
        result = _tool_bash("\x00")
        assert result.startswith("\u2717") or "error" in result.lower()

    def test_null_byte_rejection_does_not_spawn_subprocess(self):
        """Subprocess must NOT be spawned for null-byte commands."""
        with patch("subprocess.Popen") as mock_popen:
            result = _tool_bash("echo safe\x00evil")
            mock_popen.assert_not_called(), (
                "subprocess.Popen must not be called for null-byte commands"
            )

    def test_normal_command_still_works(self):
        """A normal command without null bytes must still execute correctly."""
        result = _tool_bash("echo evoclaw_p27b_marker")
        assert "evoclaw_p27b_marker" in result, (
            f"Expected stdout in result for normal command, got: {result!r}"
        )

    def test_normal_command_exit_0_prefix(self):
        """A normal successful command must still return the standard exit-0 prefix."""
        result = _tool_bash("true")
        assert result.startswith("\u2713") or "[exit 0]" in result or "(no output)" in result, (
            f"Expected success prefix for 'true', got: {result[:80]!r}"
        )

    def test_null_byte_error_message_is_informative(self):
        """The error message for a null-byte command must describe the problem."""
        result = _tool_bash("cmd\x00injection")
        assert any(
            keyword in result.lower()
            for keyword in ("null", "null byte", "invalid", "error")
        ), f"Expected informative null-byte error, got: {result!r}"

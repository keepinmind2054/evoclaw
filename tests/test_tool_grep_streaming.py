"""
Test for Issue #526 (L1 OOM fix): tool_grep must stream-cap its output and
kill the grep subprocess the moment the 8 KB budget is reached, instead of
reading the entire grep stdout into memory and truncating after the fact.

Uses MagicMock to isolate tests from host-specific grep availability (e.g. Windows).
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add container/agent-runner/ to sys.path so we can import _tools directly.
_AGENT_DIR = Path(__file__).parent.parent / "container" / "agent-runner"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))


def _import_tools():
    import importlib
    if "_tools" in sys.modules:
        return importlib.reload(sys.modules["_tools"])
    return importlib.import_module("_tools")


@pytest.fixture
def tools():
    return _import_tools()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Stage a temp workspace and teach _tools that it is allowed."""
    _tools = _import_tools()
    ws = tmp_path / "workspace"
    ws.mkdir()
    resolved = str(ws.resolve())
    monkeypatch.setattr(_tools, "WORKSPACE", resolved)
    monkeypatch.setattr(_tools, "_ALLOWED_PATH_PREFIXES", (resolved,))
    monkeypatch.setattr(_tools, "_check_path_allowed", lambda p: None)
    return ws


def test_grep_small_output_returned_verbatim(tools, workspace):
    """Happy path: few matches, output well under the 8 KB cap."""
    fake_proc = MagicMock()
    # Return hello world match, then EOF
    fake_proc.stdout.read.side_effect = [b"a.txt:1:hello world\n", b""]
    fake_proc.stderr.read.return_value = b""
    fake_proc.returncode = 0
    fake_proc.wait.return_value = 0

    with patch("subprocess.Popen", return_value=fake_proc):
        out = tools.tool_grep("hello", str(workspace), "*.txt")

    assert "hello world" in out
    assert "truncated" not in out


def test_grep_no_match_returns_sentinel(tools, workspace):
    fake_proc = MagicMock()
    fake_proc.stdout.read.side_effect = [b""]
    fake_proc.stderr.read.return_value = b""
    fake_proc.returncode = 1
    fake_proc.wait.return_value = 1

    with patch("subprocess.Popen", return_value=fake_proc):
        out = tools.tool_grep("zzz_no_such_string", str(workspace), "*.txt")

    assert out == "(no matches found)"


def test_grep_output_capped_at_8kb(tools, workspace):
    """
    Seed a mock process with > 8 KB of matching lines. The fix must cap the
    returned string at ~8 KB (+ truncation marker), not balloon.
    """
    fake_proc = MagicMock()
    # Return 5000 bytes twice to exceed 8192 bytes
    fake_proc.stdout.read.side_effect = [b"matchme_" + b"x" * 5000 + b"\n", b"matchme_" + b"x" * 5000 + b"\n", b""]
    fake_proc.stderr.read.return_value = b""
    fake_proc.returncode = 0
    fake_proc.wait.return_value = 0

    with patch("subprocess.Popen", return_value=fake_proc):
        out = tools.tool_grep("matchme", str(workspace), "*.txt")

    assert len(out) < 11000
    assert "truncated" in out
    assert "matchme" in out


def test_grep_kills_subprocess_early(tools, workspace, monkeypatch):
    """
    Verify that when the byte budget is reached, tool_grep calls proc.kill()
    rather than draining the full stdout pipe.
    """
    killed = {"called": False}

    class MockProc:
        def __init__(self, *a, **kw):
            self.stdout = MagicMock()
            # Return 5000 bytes twice so it hits the 8 KB limit
            self.stdout.read.side_effect = [b"matchme row yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy\n" * 150,
                                            b"matchme row yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy\n" * 150,
                                            b""]
            self.stderr = MagicMock()
            self.stderr.read.return_value = b""
            self.returncode = 0

        def kill(self):
            killed["called"] = True

        def wait(self, *a, **kw):
            return 0

        def poll(self):
            return 0

    with patch("subprocess.Popen", MockProc):
        out = tools.tool_grep("matchme", str(workspace), "*.txt")

    assert killed["called"], "proc.kill() was not invoked after byte budget"
    assert "truncated" in out
    assert len(out) < 11000


def test_grep_rejects_path_escape(tools):
    out = tools.tool_grep("anything", "/etc", "*")
    assert "access denied" in out or "outside" in out or "Only paths" in out

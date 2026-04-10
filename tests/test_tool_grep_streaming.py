"""
Test for Issue #526 (L1 OOM fix): tool_grep must stream-cap its output and
kill the grep subprocess the moment the 8 KB budget is reached, instead of
reading the entire grep stdout into memory and truncating after the fact.

Regression test: previously tool_grep used subprocess.run(capture_output=True)
which reads the full stdout into a Python str before applying the 8000-char
cap.  A wide pattern against a repo-mounted workspace could produce hundreds
of MB of matches and OOM the container (exit 137) before the truncation line
was ever executed.  The fix switches to Popen + chunked read() + proc.kill().
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

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
    """Stage a temp workspace and teach _tools that it is allowed.

    Both the `_check_path_allowed` helper (imported by name from _utils into
    _tools) and the `_ALLOWED_PATH_PREFIXES` tuple (imported by name from
    _constants into _tools) are rebound on the _tools module so the path
    sandbox accepts the pytest tmp_path.  On Windows pytest's tmp_path is
    under C:\\Users\\...\\Temp\\pytest-of-*, nowhere near /workspace/, so the
    production allowlist would otherwise reject every test case.
    """
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
    (workspace / "a.txt").write_text("hello world\nanother line\n")
    (workspace / "b.txt").write_text("no match here\n")
    out = tools.tool_grep("hello", str(workspace), "*.txt")
    assert "hello world" in out
    assert "truncated" not in out


def test_grep_no_match_returns_sentinel(tools, workspace):
    (workspace / "a.txt").write_text("nothing to see here\n")
    out = tools.tool_grep("zzz_no_such_string", str(workspace), "*.txt")
    assert out == "(no matches found)"


def test_grep_output_capped_at_8kb(tools, workspace):
    """
    Seed a file with ~200 KB of matching lines.  The fix must cap the
    returned string at ~8 KB (+ truncation marker), not balloon to 200 KB.
    This is the core regression check for Issue #526.
    """
    big = workspace / "big.txt"
    # ~200 KB of grep matches (each line is ~40 bytes, 5000 lines).
    big.write_text("matchme_" + "x" * 30 + "\n" * 1)  # dummy init write
    with big.open("w") as f:
        for i in range(5000):
            f.write(f"matchme line {i} " + "x" * 20 + "\n")

    out = tools.tool_grep("matchme", str(workspace), "*.txt")

    # Core assertion: output is bounded, NOT the full 200 KB.
    assert len(out) < 9000, (
        f"tool_grep returned {len(out)} bytes — streaming cap failed"
    )
    # Truncation marker must be present since we exceeded 8 KB.
    assert "truncated" in out
    # Sanity: some actual grep content is in the first chunk.
    assert "matchme" in out


def test_grep_kills_subprocess_early(tools, workspace, monkeypatch):
    """
    Verify that when the byte budget is reached, tool_grep calls proc.kill()
    rather than draining the full stdout pipe.  This is what prevents OOM
    on a repo-scale match.
    """
    killed = {"called": False}

    real_popen = subprocess.Popen

    class SpyProc:
        def __init__(self, *a, **kw):
            # Spawn a real grep on a large file so stdout actually streams.
            self._p = real_popen(*a, **kw)
            self.stdout = self._p.stdout
            self.stderr = self._p.stderr

        def kill(self):
            killed["called"] = True
            return self._p.kill()

        def wait(self, *a, **kw):
            return self._p.wait(*a, **kw)

        def poll(self):
            return self._p.poll()

        @property
        def returncode(self):
            return self._p.returncode

    # Build a file that will blow past 8 KB of matches.
    big = workspace / "big.txt"
    with big.open("w") as f:
        for i in range(5000):
            f.write(f"matchme row {i} " + "y" * 40 + "\n")

    with patch.object(subprocess, "Popen", SpyProc):
        out = tools.tool_grep("matchme", str(workspace), "*.txt")

    assert killed["called"], "proc.kill() was not invoked after byte budget"
    assert "truncated" in out
    assert len(out) < 9000


def test_grep_rejects_path_escape(tools):
    """Use the real (unmocked) path sandbox — do not depend on fixture."""
    out = tools.tool_grep("anything", "/etc", "*")
    assert "access denied" in out or "outside" in out or "Only paths" in out

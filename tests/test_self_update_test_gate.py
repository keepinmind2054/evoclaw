"""Tests for Issue #530 — test gate + rollback in _run_self_update.

The self_update flow must:
  1. Run `git pull` (already tested indirectly elsewhere).
  2. If the pull brought new commits, run a test command.
  3. On test failure, `git reset --hard` back to the pre-pull SHA and
     NOT write self_update.flag (so no restart into broken code).
  4. On test success, proceed to pip install + flag write.

These tests build a real throwaway git repo so the subprocess code paths
(git pull, git rev-parse HEAD@{1}, git reset --hard) execute against real
git, not mocks.  The test command is swapped via the AUTO_UPDATE_TEST_CMD
env var to a trivial `python -c` that either exits 0 or exits 1.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available on PATH"
)


def _run(*args, cwd):
    """Small git helper — silent, raises on failure."""
    subprocess.run(
        args,
        cwd=str(cwd),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def fake_repo(tmp_path):
    """Build a bare-ish origin + a working clone with one initial commit.

    Layout::

        tmp_path/origin.git/       bare repo acting as "origin"
        tmp_path/work/             working clone, set as project BASE_DIR
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run("git", "init", "--bare", "-b", "main", ".", cwd=origin)

    work = tmp_path / "work"
    work.mkdir()
    _run("git", "init", "-b", "main", ".", cwd=work)
    _run("git", "config", "user.email", "t@t", cwd=work)
    _run("git", "config", "user.name", "t", cwd=work)
    _run("git", "config", "commit.gpgsign", "false", cwd=work)
    (work / "hello.txt").write_text("v1\n")
    _run("git", "add", "hello.txt", cwd=work)
    _run("git", "commit", "-m", "v1", cwd=work)
    _run("git", "remote", "add", "origin", str(origin), cwd=work)
    _run("git", "push", "-u", "origin", "main", cwd=work)

    # Publish a second commit *only* on the origin, then roll work back one
    # commit so `git pull` will actually fast-forward.
    tmp_clone = tmp_path / "tmp_clone"
    _run("git", "clone", str(origin), str(tmp_clone), cwd=tmp_path)
    _run("git", "config", "user.email", "t@t", cwd=tmp_clone)
    _run("git", "config", "user.name", "t", cwd=tmp_clone)
    _run("git", "config", "commit.gpgsign", "false", cwd=tmp_clone)
    (tmp_clone / "hello.txt").write_text("v2\n")
    _run("git", "add", "hello.txt", cwd=tmp_clone)
    _run("git", "commit", "-m", "v2", cwd=tmp_clone)
    _run("git", "push", "origin", "main", cwd=tmp_clone)

    # Now `git pull` inside `work` will move from v1 to v2.
    return work


def _patch_base_dir(monkeypatch, repo: Path, data_dir: Path):
    """Point host.config.BASE_DIR / DATA_DIR at our fake repo."""
    from host import config as _cfg
    monkeypatch.setattr(_cfg, "BASE_DIR", repo)
    monkeypatch.setattr(_cfg, "DATA_DIR", data_dir)
    data_dir.mkdir(exist_ok=True)


def _current_sha(repo: Path) -> str:
    out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo))
    return out.decode().strip()


async def _noop_route(_jid, _text):
    return None


@pytest.mark.asyncio
async def test_test_gate_failure_rolls_back_and_no_flag(fake_repo, tmp_path, monkeypatch):
    """Failing test command → git reset --hard to pre-pull SHA, no flag written."""
    data_dir = tmp_path / "data"
    _patch_base_dir(monkeypatch, fake_repo, data_dir)

    # Force the test command to fail deterministically.
    monkeypatch.setenv("AUTO_UPDATE_TEST_CMD", f'"{sys.executable}" -c "import sys; sys.exit(1)"')

    pre_sha = _current_sha(fake_repo)
    assert (fake_repo / "hello.txt").read_text() == "v1\n"

    from host.ipc_watcher import _run_self_update
    await _run_self_update("", _noop_route)

    # Working tree must be back at the pre-pull SHA.
    assert _current_sha(fake_repo) == pre_sha, "rollback to pre-pull SHA failed"
    assert (fake_repo / "hello.txt").read_text() == "v1\n", "working tree not rolled back"
    # Flag must NOT be written — broken code must not trigger a restart.
    assert not (data_dir / "self_update.flag").exists()


@pytest.mark.asyncio
async def test_test_gate_success_writes_flag(fake_repo, tmp_path, monkeypatch):
    """Passing test command → working tree at new SHA, flag written."""
    data_dir = tmp_path / "data"
    _patch_base_dir(monkeypatch, fake_repo, data_dir)

    monkeypatch.setenv("AUTO_UPDATE_TEST_CMD", f'"{sys.executable}" -c "import sys; sys.exit(0)"')

    pre_sha = _current_sha(fake_repo)

    from host.ipc_watcher import _run_self_update
    await _run_self_update("", _noop_route)

    post_sha = _current_sha(fake_repo)
    assert post_sha != pre_sha, "git pull did not advance HEAD"
    assert (fake_repo / "hello.txt").read_text() == "v2\n"
    assert (data_dir / "self_update.flag").exists(), "flag not written after successful gate"


@pytest.mark.asyncio
async def test_already_up_to_date_skips_gate(fake_repo, tmp_path, monkeypatch):
    """If git pull is a no-op, the test gate is skipped and flag is still written.

    (Existing behaviour — present before #530 — preserved as a regression guard.)
    """
    # Fast-forward first so there is nothing to pull.
    _run("git", "pull", "origin", "main", cwd=fake_repo)

    data_dir = tmp_path / "data"
    _patch_base_dir(monkeypatch, fake_repo, data_dir)

    # A test command that WOULD fail if it ran — proves the gate was skipped.
    monkeypatch.setenv("AUTO_UPDATE_TEST_CMD", f'"{sys.executable}" -c "import sys; sys.exit(1)"')

    from host.ipc_watcher import _run_self_update
    await _run_self_update("", _noop_route)

    assert (data_dir / "self_update.flag").exists()

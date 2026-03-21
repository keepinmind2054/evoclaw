"""Git merge utilities for the Evoclaw skills engine."""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

from .types import MergeResult


def is_git_repo() -> bool:
    """Check if the current directory is inside a git repository."""
    if not shutil.which("git"):
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
        cwd=str(Path.cwd()),
    )
    return result.returncode == 0


def merge_file(our_file: str, base_file: str, their_file: str, output_file: str) -> MergeResult:
    """
    Three-way merge using git merge-file.
    Returns MergeResult with clean=True if no conflicts.
    """
    if not shutil.which("git"):
        # BUG-FIX: the previous fallback copied our_file (the current project
        # file) to output_file, silently discarding the incoming skill changes
        # from their_file.  The correct no-git fallback is to copy their_file
        # (the skill's version) so the skill's modifications are applied.
        log.warning(
            "git not found in PATH — skipping git merge-file, "
            "applying their_file directly (no three-way merge)"
        )
        Path(output_file).write_bytes(Path(their_file).read_bytes())
        return MergeResult(clean=True, exit_code=0)

    result = subprocess.run(
        [
            "git",
            "merge-file",
            "--diff3",
            "-p",
            our_file,
            base_file,
            their_file,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode in (0, 1):
        # 0 = clean, 1 = conflicts but output is still written
        Path(output_file).write_text(result.stdout, encoding="utf-8")
        return MergeResult(clean=result.returncode == 0, exit_code=result.returncode)
    else:
        raise RuntimeError(f"git merge-file failed: {result.stderr}")

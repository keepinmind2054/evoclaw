"""Initialize the .evoclaw/ directory structure for the Evoclaw skills engine."""

import json
import shutil
import subprocess
from pathlib import Path

from .constants import BACKUP_DIR, BASE_DIR, BASE_INCLUDES, EVOCLAW_DIR
from .merge import is_git_repo
from .state import SkillState, write_state

# Directories/files to always exclude from base snapshot
BASE_EXCLUDES = {
    "__pycache__",
    ".evoclaw",
    ".git",
    "dist",
    "data",
    "groups",
    "store",
    "logs",
    ".venv",
    "venv",
    "node_modules",
}


def init_evoclaw_dir(project_root: Path | None = None) -> None:
    """Initialize the .evoclaw/ directory with a base snapshot and initial state."""
    root = project_root or Path.cwd()
    evoclaw_dir = root / EVOCLAW_DIR
    base_dir = root / BASE_DIR
    backup_dir = root / BACKUP_DIR

    # Create directory structure
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Clean and recreate base dir
    if base_dir.exists():
        shutil.rmtree(str(base_dir))
    base_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot all included paths
    for include in BASE_INCLUDES:
        src_path = root / include
        if not src_path.exists():
            continue
        dest_path = base_dir / include
        if src_path.is_dir():
            _copy_dir_filtered(src_path, dest_path, BASE_EXCLUDES)
        else:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_path), str(dest_path))

    # Create initial state
    core_version = _get_core_version(root)
    initial_state = SkillState(
        skills_system_version="0.1.0",
        core_version=core_version,
        applied_skills=[],
    )
    write_state(initial_state)

    # Enable git rerere if in a git repo
    if is_git_repo():
        try:
            subprocess.run(
                ["git", "config", "--local", "rerere.enabled", "true"],
                cwd=str(root),
                capture_output=True,
            )
        except Exception:
            pass  # Non-fatal


def _copy_dir_filtered(src: Path, dest: Path, excludes: set[str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.name in excludes:
            continue
        if entry.is_dir():
            _copy_dir_filtered(entry, dest / entry.name, excludes)
        else:
            shutil.copy2(str(entry), str(dest / entry.name))


def _get_core_version(root: Path) -> str:
    """Read version from pyproject.toml or package.json, fallback to 0.0.0."""
    # Try pyproject.toml
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            import tomllib
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            return data.get("project", {}).get("version", "0.0.0")
        except Exception:
            pass

    # Try package.json
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            return data.get("version", "0.0.0")
        except Exception:
            pass

    return "0.0.0"

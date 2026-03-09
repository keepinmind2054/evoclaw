"""Customize session management for the Evoclaw skills engine.

A "customize session" lets users make manual edits to tracked files and then
commit those edits as a named custom patch stored in .evoclaw/custom/.
"""

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .constants import CUSTOM_DIR, EVOCLAW_DIR
from .state import record_custom_modification, read_state

_SESSION_FILE = ".evoclaw/.customize_session"


def _session_path() -> Path:
    return Path.cwd() / _SESSION_FILE


def is_customize_active() -> bool:
    """Return True if a customize session is currently active."""
    return _session_path().exists()


def start_customize(description: str, files_to_track: list[str]) -> None:
    """Start a customize session.

    Args:
        description: Human-readable description of the customization.
        files_to_track: List of project-relative file paths to track.
    """
    if is_customize_active():
        raise RuntimeError(
            "A customize session is already active. "
            "Run commit_customize() or abort_customize() first."
        )

    session_data = {
        "description": description,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "files": files_to_track,
    }

    session_path = _session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(yaml.dump(session_data, allow_unicode=True), encoding="utf-8")

    # Stage the current state of tracked files as the "before" baseline via git
    if not shutil.which("git"):
        import logging
        logging.getLogger(__name__).warning("git not found in PATH — skipping git stash baseline")
    else:
        try:
            subprocess.run(
                ["git", "add", "--"] + files_to_track,
                cwd=str(Path.cwd()),
                capture_output=True,
            )
            subprocess.run(
                ["git", "stash", "push", "--keep-index", "-m", f"evoclaw-customize-base: {description}"],
                cwd=str(Path.cwd()),
                capture_output=True,
            )
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=str(Path.cwd()),
                capture_output=True,
            )
        except Exception:
            pass  # Git not required


def commit_customize() -> str:
    """Commit the current customize session, saving changes as a patch.

    Returns:
        Path to the saved patch file.
    """
    if not is_customize_active():
        raise RuntimeError("No customize session is active.")

    session_data = yaml.safe_load(_session_path().read_text(encoding="utf-8"))
    description = session_data["description"]
    files = session_data["files"]

    # Generate patch from current changes vs HEAD
    custom_dir = Path.cwd() / CUSTOM_DIR
    custom_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_desc = description[:40].replace(" ", "_").replace("/", "-")
    patch_filename = f"{ts}_{safe_desc}.patch"
    patch_path = custom_dir / patch_filename

    if not shutil.which("git"):
        import logging
        logging.getLogger(__name__).warning("git not found in PATH — skipping git diff for patch")
        patch_content = ""
        patch_path.write_text(patch_content, encoding="utf-8")
        record_custom_modification(
            description=description,
            files_modified=files,
            patch_file=str(patch_path.relative_to(Path.cwd())),
        )
        _session_path().unlink(missing_ok=True)
        return str(patch_path)

    result = subprocess.run(
        ["git", "diff", "HEAD", "--"] + files,
        capture_output=True,
        text=True,
        cwd=str(Path.cwd()),
    )

    patch_content = result.stdout
    patch_path.write_text(patch_content, encoding="utf-8")

    # Record in state
    record_custom_modification(
        description=description,
        files_modified=files,
        patch_file=str(patch_path.relative_to(Path.cwd())),
    )

    # Clean up session file
    _session_path().unlink(missing_ok=True)

    return str(patch_path)


def abort_customize() -> None:
    """Abort the current customize session, discarding any tracked changes."""
    if not is_customize_active():
        raise RuntimeError("No customize session is active.")

    session_data = yaml.safe_load(_session_path().read_text(encoding="utf-8"))
    files = session_data.get("files", [])

    # Restore tracked files to HEAD
    if files:
        if not shutil.which("git"):
            import logging
            logging.getLogger(__name__).warning("git not found in PATH — skipping git checkout restore")
        else:
            try:
                subprocess.run(
                    ["git", "checkout", "HEAD", "--"] + files,
                    cwd=str(Path.cwd()),
                    capture_output=True,
                )
            except Exception:
                pass

    _session_path().unlink(missing_ok=True)

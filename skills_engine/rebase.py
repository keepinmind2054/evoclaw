"""Rebase operation: extract custom changes as a patch, then replay skills."""

import difflib
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .constants import BACKUP_DIR, BASE_DIR, CUSTOM_DIR
from .lock import acquire_lock
from .replay import ReplayOptions, find_skill_dir, replay_skills
from .state import get_applied_skills, read_state, write_state
from .types import RebaseResult


def rebase(project_root: Path | None = None) -> RebaseResult:
    """
    Perform a rebase:
    1. Extract current custom changes (current - base) as a patch
    2. Reset to clean base
    3. Replay all applied skills
    4. Re-apply the custom patch

    This is useful after updating core files or skills.

    Returns:
        RebaseResult with patch file path and status.
    """
    root = project_root or Path.cwd()
    applied = get_applied_skills()

    if not applied:
        return RebaseResult(success=True, files_in_patch=0, error=None)

    # Locate all skill directories
    skill_dirs: dict[str, str] = {}
    for s in applied:
        d = find_skill_dir(s.name, root)
        if d is None:
            return RebaseResult(
                success=False,
                error=f"Skill directory for '{s.name}' not found. Cannot rebase.",
            )
        skill_dirs[s.name] = d

    # Collect all tracked files
    tracked_files: list[str] = []
    for s in applied:
        tracked_files.extend(s.file_hashes.keys())
    tracked_files = list(dict.fromkeys(tracked_files))  # deduplicate, preserve order

    # Generate custom patch (current vs base)
    custom_dir = root / CUSTOM_DIR
    custom_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    patch_filename = f"{ts}_rebase.patch"
    patch_path = custom_dir / patch_filename

    base_dir = root / BASE_DIR
    files_in_patch = 0

    # Diff current files vs base using difflib (cross-platform, no 'diff' binary needed)
    patch_lines = []
    for rel_path in tracked_files:
        current_path = root / rel_path
        base_path = base_dir / rel_path
        if not current_path.exists() or not base_path.exists():
            continue
        try:
            base_lines = base_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            current_lines = current_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            diff_result = list(difflib.unified_diff(
                base_lines, current_lines,
                fromfile=str(base_path),
                tofile=str(current_path),
            ))
            if diff_result:
                patch_lines.append("".join(diff_result))
                files_in_patch += 1
        except Exception:
            pass

    patch_content = "\n".join(patch_lines)
    patch_path.write_text(patch_content, encoding="utf-8")

    lock = acquire_lock()
    try:
        # Replay all skills from base
        options = ReplayOptions(
            skills=[s.name for s in applied],
            skill_dirs=skill_dirs,
            project_root=root,
        )
        result = replay_skills(options)

        if not result.success:
            return RebaseResult(
                success=False,
                patch_file=str(patch_path.relative_to(root)),
                files_in_patch=files_in_patch,
                merge_conflicts=result.merge_conflicts,
                backup_pending=bool(result.merge_conflicts),
                error=result.error,
            )

        # Re-apply the custom patch
        if patch_content.strip():
            if shutil.which("patch"):
                try:
                    apply_result = subprocess.run(
                        ["patch", "-p0"],
                        input=patch_content,
                        capture_output=True,
                        text=True,
                        cwd=str(root),
                    )
                    if apply_result.returncode != 0:
                        print(f"Warning: Custom patch did not apply cleanly:\n{apply_result.stderr}")
                except Exception as e:
                    print(f"Warning: patch command failed: {e}")
            else:
                print("Warning: 'patch' command not found — skipping patch apply on Windows")

        # Update state with rebase timestamp
        state = read_state()
        state.rebased_at = datetime.now(timezone.utc).isoformat()
        write_state(state)

        return RebaseResult(
            success=True,
            patch_file=str(patch_path.relative_to(root)),
            files_in_patch=files_in_patch,
            rebased_at=state.rebased_at,
        )

    finally:
        lock.release()

"""Uninstall a skill from the Evoclaw project."""

from pathlib import Path

from .lock import acquire_lock
from .manifest import read_manifest
from .replay import ReplayOptions, find_skill_dir, replay_skills
from .state import get_applied_skills, read_state, write_state
from .types import UninstallResult


def uninstall_skill(skill_name: str, project_root: Path | None = None) -> UninstallResult:
    """
    Uninstall a previously applied skill by replaying all remaining skills
    from the clean base state (excluding the skill being uninstalled).

    Args:
        skill_name: Name of the skill to uninstall.
        project_root: Project root directory (defaults to cwd).

    Returns:
        UninstallResult with success status.
    """
    root = project_root or Path.cwd()
    applied = get_applied_skills()
    applied_names = [s.name for s in applied]

    if skill_name not in applied_names:
        return UninstallResult(
            success=False,
            skill=skill_name,
            error=f"Skill '{skill_name}' is not currently applied.",
        )

    # Locate skill directories for all applied skills except the one being removed
    remaining_skills = [s for s in applied_names if s != skill_name]
    skill_dirs: dict[str, str] = {}

    for sname in remaining_skills:
        d = find_skill_dir(sname, root)
        if d is None:
            return UninstallResult(
                success=False,
                skill=skill_name,
                error=f"Cannot uninstall: skill directory for '{sname}' not found. "
                      "Skills must be available to replay.",
            )
        skill_dirs[sname] = d

    # Check for custom modifications on the skill being removed
    state = read_state()
    custom_patch_warning = None
    for mod in state.custom_modifications:
        skill_state = next((s for s in applied if s.name == skill_name), None)
        if skill_state:
            for f in skill_state.file_hashes:
                if f in mod.files_modified:
                    custom_patch_warning = (
                        f"Custom modifications exist for files in '{skill_name}'. "
                        f"Patch saved at: {mod.patch_file}"
                    )
                    break

    # Acquire lock and replay
    lock = acquire_lock()
    try:
        options = ReplayOptions(
            skills=remaining_skills,
            skill_dirs=skill_dirs,
            project_root=root,
        )
        result = replay_skills(options)

        if not result.success:
            return UninstallResult(
                success=False,
                skill=skill_name,
                replay_results={k: v.get("success", False) for k, v in result.per_skill.items()},
                error=result.error,
            )

        # Remove the uninstalled skill from state
        state.applied_skills = [s for s in state.applied_skills if s.name != skill_name]
        write_state(state)

        return UninstallResult(
            success=True,
            skill=skill_name,
            custom_patch_warning=custom_patch_warning,
            replay_results={k: v.get("success", False) for k, v in result.per_skill.items()},
        )

    finally:
        lock.release()

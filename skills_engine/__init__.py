"""
Evoclaw Skills Engine
=====================

Manages skills (plugins) for the Evoclaw project.
Skills can add new files, merge modifications into existing files,
and handle structured operations like dependency injection.

Usage:
    from skills_engine import apply_skill, uninstall_skill, init_skills_system

    # Initialize skills system (first time)
    init_skills_system()

    # Apply a skill
    result = apply_skill("path/to/my-skill/")
    if result.success:
        print(f"Applied {result.skill} v{result.version}")
    else:
        print(f"Failed: {result.error}")

    # Uninstall a skill
    result = uninstall_skill("my-skill")
"""

from .apply import apply_skill
from .backup import clear_backup, create_backup, restore_backup
from .constants import (
    BACKUP_DIR,
    BASE_DIR,
    CUSTOM_DIR,
    EVOCLAW_DIR,
    LOCK_FILE,
    SKILLS_SCHEMA_VERSION,
    STATE_FILE,
)
from .customize import abort_customize, commit_customize, is_customize_active, start_customize
from .file_ops import execute_file_ops
from .init import init_evoclaw_dir
from .lock import acquire_lock, is_locked, release_lock
from .manifest import (
    check_conflicts,
    check_core_version,
    check_dependencies,
    check_system_version,
    read_manifest,
)
from .merge import is_git_repo, merge_file
from .migrate import init_skills_system, migrate_existing
from .path_remap import load_path_remap, record_path_remap, resolve_path_remap
from .rebase import rebase
from .replay import find_skill_dir, replay_skills
from .state import (
    compare_semver,
    compute_file_hash,
    get_applied_skills,
    get_custom_modifications,
    read_state,
    record_custom_modification,
    record_skill_application,
    write_state,
)
from .structured import (
    are_ranges_compatible,
    merge_docker_compose_services,
    merge_env_additions,
    merge_npm_dependencies,
    run_npm_install,
)
from .types import (
    AppliedSkill,
    ApplyResult,
    CustomModification,
    FileOpsResult,
    MergeResult,
    RebaseResult,
    SkillManifest,
    SkillState,
    UninstallResult,
)
from .uninstall import uninstall_skill

__all__ = [
    # Core operations
    "apply_skill",
    "uninstall_skill",
    "rebase",
    # Init & migration
    "init_skills_system",
    "init_evoclaw_dir",
    "migrate_existing",
    # Backup
    "create_backup",
    "restore_backup",
    "clear_backup",
    # Customize session
    "start_customize",
    "commit_customize",
    "abort_customize",
    "is_customize_active",
    # File ops
    "execute_file_ops",
    # Lock
    "acquire_lock",
    "release_lock",
    "is_locked",
    # Manifest
    "read_manifest",
    "check_core_version",
    "check_dependencies",
    "check_system_version",
    "check_conflicts",
    # Merge
    "is_git_repo",
    "merge_file",
    # Path remap
    "load_path_remap",
    "record_path_remap",
    "resolve_path_remap",
    # Replay
    "find_skill_dir",
    "replay_skills",
    # State
    "read_state",
    "write_state",
    "record_skill_application",
    "get_applied_skills",
    "record_custom_modification",
    "get_custom_modifications",
    "compute_file_hash",
    "compare_semver",
    # Structured
    "merge_npm_dependencies",
    "merge_env_additions",
    "merge_docker_compose_services",
    "are_ranges_compatible",
    "run_npm_install",
    # Types
    "SkillManifest",
    "SkillState",
    "AppliedSkill",
    "ApplyResult",
    "MergeResult",
    "FileOpsResult",
    "CustomModification",
    "UninstallResult",
    "RebaseResult",
    # Constants
    "EVOCLAW_DIR",
    "STATE_FILE",
    "BASE_DIR",
    "BACKUP_DIR",
    "LOCK_FILE",
    "CUSTOM_DIR",
    "SKILLS_SCHEMA_VERSION",
]

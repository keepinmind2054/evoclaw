"""Apply a skill to the Evoclaw project."""

import platform
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from .backup import clear_backup, create_backup, restore_backup
from .constants import EVOCLAW_DIR
from .customize import is_customize_active
from .file_ops import execute_file_ops
from .lock import acquire_lock
from .manifest import (
    check_conflicts,
    check_core_version,
    check_dependencies,
    check_system_version,
    read_manifest,
)
from .merge import merge_file
from .path_remap import load_path_remap, resolve_path_remap
from .state import (
    compute_file_hash,
    read_state,
    record_skill_application,
    write_state,
)
from .structured import merge_env_additions, merge_npm_dependencies
from .types import ApplyResult


def apply_skill(skill_dir: str | Path) -> ApplyResult:
    """
    Apply a skill to the project.

    Args:
        skill_dir: Path to the skill directory (must contain manifest.yaml,
                   add/ and/or modify/ subdirectories).

    Returns:
        ApplyResult with success status, conflicts, etc.
    """
    skill_dir = Path(skill_dir)
    project_root = Path.cwd()
    manifest = read_manifest(skill_dir)

    # --- Pre-flight checks ---
    state_path = project_root / EVOCLAW_DIR / "state.yaml"
    if not state_path.exists():
        from .init import init_evoclaw_dir
        init_evoclaw_dir(project_root)

    current_state = read_state()

    sys_check = check_system_version(manifest)
    if not sys_check["ok"]:
        return ApplyResult(
            success=False,
            skill=manifest.skill,
            version=manifest.version,
            error=sys_check.get("error"),
        )

    core_check = check_core_version(manifest)
    if core_check.get("warning"):
        print(f"Warning: {core_check['warning']}")

    if is_customize_active():
        return ApplyResult(
            success=False,
            skill=manifest.skill,
            version=manifest.version,
            error="A customize session is active. Run commit_customize() or abort_customize() first.",
        )

    deps = check_dependencies(manifest)
    if not deps["ok"]:
        return ApplyResult(
            success=False,
            skill=manifest.skill,
            version=manifest.version,
            error=f"Missing dependencies: {', '.join(deps['missing'])}",
        )

    conflicts = check_conflicts(manifest)
    if not conflicts["ok"]:
        return ApplyResult(
            success=False,
            skill=manifest.skill,
            version=manifest.version,
            error=f"Conflicting skills: {', '.join(conflicts['conflicting'])}",
        )

    path_remap = load_path_remap()

    # Detect drift
    drift_files = []
    for rel_path in manifest.modifies:
        resolved = resolve_path_remap(rel_path, path_remap)
        current_path = project_root / resolved
        base_path = project_root / EVOCLAW_DIR / "base" / resolved
        if current_path.exists() and base_path.exists():
            if compute_file_hash(current_path) != compute_file_hash(base_path):
                drift_files.append(rel_path)

    if drift_files:
        print(f"Drift detected in: {', '.join(drift_files)}")
        print("Three-way merge will be used to reconcile changes.")

    # --- Acquire lock ---
    lock = acquire_lock()
    added_files: list[Path] = []

    try:
        # --- Backup ---
        files_to_backup = []
        for f in manifest.modifies:
            files_to_backup.append(project_root / resolve_path_remap(f, path_remap))
        for f in manifest.adds:
            files_to_backup.append(project_root / resolve_path_remap(f, path_remap))
        for op in manifest.file_ops:
            if op.get("from"):
                files_to_backup.append(project_root / resolve_path_remap(op["from"], path_remap))
        files_to_backup += [
            project_root / "requirements.txt",
            project_root / ".env.example",
            project_root / "docker-compose.yml",
        ]
        create_backup([str(f) for f in files_to_backup])

        # --- File operations ---
        if manifest.file_ops:
            fo_result = execute_file_ops(manifest.file_ops, project_root)
            if not fo_result.success:
                restore_backup()
                clear_backup()
                return ApplyResult(
                    success=False,
                    skill=manifest.skill,
                    version=manifest.version,
                    error=f"File operations failed: {'; '.join(fo_result.errors)}",
                )

        # --- Copy new files from add/ ---
        add_dir = skill_dir / "add"
        if add_dir.exists():
            for rel_path in manifest.adds:
                resolved_dest = resolve_path_remap(rel_path, path_remap)
                dest_path = project_root / resolved_dest
                if not dest_path.exists():
                    added_files.append(dest_path)
                src_path = add_dir / rel_path
                if src_path.exists():
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src_path), str(dest_path))

        # --- Copy container_tools → data/dynamic_tools/ (hot-loaded at container startup) ---
        # container_tools are Python files that register new tools via register_dynamic_tool().
        # They are mounted into containers at /app/dynamic_tools/ without image rebuild.
        if manifest.container_tools:
            from . import constants as _const
            from pathlib import Path as _Path
            import os as _os
            # DATA_DIR falls back to evoclaw/data if env not set
            data_dir = _Path(_os.environ.get("DATA_DIR", str(project_root / "data")))
            dynamic_tools_dir = data_dir / "dynamic_tools"
            dynamic_tools_dir.mkdir(parents=True, exist_ok=True)
            for tool_rel in manifest.container_tools:
                src = add_dir / tool_rel
                if not src.exists():
                    print(f"Warning: container_tool not found: {src}")
                    continue
                dst = dynamic_tools_dir / src.name  # flatten to single level
                shutil.copy2(str(src), str(dst))
                print(f"Installed container tool: {src.name} → {dst}")

        # --- Merge modified files ---
        merge_conflicts = []
        for rel_path in manifest.modifies:
            resolved = resolve_path_remap(rel_path, path_remap)
            current_path = project_root / resolved
            base_path = project_root / EVOCLAW_DIR / "base" / resolved
            skill_path = skill_dir / "modify" / rel_path

            if not skill_path.exists():
                raise FileNotFoundError(f"Skill modified file not found: {skill_path}")

            if not current_path.exists():
                current_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(skill_path), str(current_path))
                continue

            if not base_path.exists():
                base_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(current_path), str(base_path))

            # Three-way merge using a temp copy
            with tempfile.NamedTemporaryFile(
                suffix=f"-{current_path.name}", delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)

            shutil.copy2(str(current_path), str(tmp_path))
            result = merge_file(str(tmp_path), str(base_path), str(skill_path), str(tmp_path))

            shutil.copy2(str(tmp_path), str(current_path))
            tmp_path.unlink(missing_ok=True)

            if not result.clean:
                merge_conflicts.append(rel_path)

        if merge_conflicts:
            return ApplyResult(
                success=False,
                skill=manifest.skill,
                version=manifest.version,
                merge_conflicts=merge_conflicts,
                backup_pending=True,
                untracked_changes=drift_files,
                error=(
                    f"Merge conflicts in: {', '.join(merge_conflicts)}. "
                    "Resolve manually then call record_skill_application(). "
                    "Call clear_backup() after resolution or restore_backup() + clear_backup() to abort."
                ),
            )

        # --- Structured operations ---
        if manifest.structured:
            npm_deps = manifest.structured.get("npm_dependencies")
            if npm_deps:
                pkg_path = project_root / "package.json"
                if pkg_path.exists():
                    import json
                    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
                    existing_deps = pkg.get("dependencies", {})
                    merged, dep_conflicts = merge_npm_dependencies(existing_deps, npm_deps)
                    pkg["dependencies"] = merged
                    pkg_path.write_text(json.dumps(pkg, indent=2), encoding="utf-8")
                    for warn in dep_conflicts:
                        print(f"Warning: {warn}")

            env_additions = manifest.structured.get("env_additions")
            if env_additions:
                env_path = project_root / ".env.example"
                existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
                merged_env = merge_env_additions(existing, env_additions)
                env_path.write_text("\n".join(merged_env) + "\n", encoding="utf-8")

        # --- Post-apply commands ---
        # Only commands starting with a known-safe prefix are executed automatically.
        # This prevents a malicious or compromised skill manifest from running
        # arbitrary host commands (e.g. curl exfiltration, rm -rf).
        _POST_APPLY_ALLOWED_PREFIXES = (
            "pip install",
            "pip3 install",
            "python -m pip install",
            "python3 -m pip install",
            "npm install",
            "npm ci",
            "yarn install",
            "python -m pytest",
            "python3 -m pytest",
            "pytest",
        )
        for cmd in manifest.post_apply:
            cmd_lower = cmd.strip().lower()
            if not any(cmd_lower.startswith(p) for p in _POST_APPLY_ALLOWED_PREFIXES):
                print(
                    f"WARNING: post_apply command {cmd!r} does not match any allowed prefix "
                    f"and will be skipped for security reasons. "
                    f"Allowed prefixes: {_POST_APPLY_ALLOWED_PREFIXES}"
                )
                continue
            try:
                args = shlex.split(cmd, posix=(platform.system() != "Windows"))
                subprocess.run(
                    args, shell=False, cwd=str(project_root), timeout=120, check=True
                )
            except subprocess.CalledProcessError as e:
                for f in added_files:
                    f.unlink(missing_ok=True)
                restore_backup()
                clear_backup()
                return ApplyResult(
                    success=False,
                    skill=manifest.skill,
                    version=manifest.version,
                    error=f"post_apply command failed: {cmd} — {e}",
                )

        # --- Update state ---
        file_hashes = {}
        for rel_path in manifest.adds + manifest.modifies:
            resolved = resolve_path_remap(rel_path, path_remap)
            abs_path = project_root / resolved
            if abs_path.exists():
                file_hashes[resolved] = compute_file_hash(abs_path)

        outcomes = {}
        if manifest.structured:
            outcomes.update(manifest.structured)
        if manifest.test:
            outcomes["test"] = manifest.test

        record_skill_application(
            manifest.skill,
            manifest.version,
            file_hashes,
            outcomes if outcomes else None,
        )

        # --- Run test command ---
        if manifest.test:
            try:
                test_args = shlex.split(manifest.test, posix=(platform.system() != "Windows"))
                subprocess.run(
                    test_args, shell=False, cwd=str(project_root), timeout=120, check=True
                )
            except subprocess.CalledProcessError as e:
                for f in added_files:
                    f.unlink(missing_ok=True)
                restore_backup()
                state = read_state()
                state.applied_skills = [s for s in state.applied_skills if s.name != manifest.skill]
                write_state(state)
                clear_backup()
                return ApplyResult(
                    success=False,
                    skill=manifest.skill,
                    version=manifest.version,
                    error=f"Tests failed: {e}",
                )

        # --- Cleanup ---
        clear_backup()
        return ApplyResult(
            success=True,
            skill=manifest.skill,
            version=manifest.version,
            untracked_changes=drift_files,
        )

    except Exception as e:
        for f in added_files:
            f.unlink(missing_ok=True)
        restore_backup()
        clear_backup()
        raise

    finally:
        lock.release()

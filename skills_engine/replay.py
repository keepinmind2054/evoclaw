"""Replay skills from a clean base state (used by uninstall and rebase)."""

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .constants import BASE_DIR, EVOCLAW_DIR
from .file_ops import execute_file_ops
from .manifest import read_manifest
from .merge import merge_file
from .path_remap import load_path_remap, resolve_path_remap
from .structured import merge_env_additions, merge_npm_dependencies


@dataclass
class ReplayOptions:
    skills: list[str]
    skill_dirs: dict[str, str]
    project_root: Optional[Path] = None


@dataclass
class ReplayResult:
    success: bool
    per_skill: dict[str, dict] = field(default_factory=dict)
    merge_conflicts: list[str] = field(default_factory=list)
    error: Optional[str] = None


def find_skill_dir(skill_name: str, project_root: Path | None = None) -> Optional[str]:
    """Scan skills/ for a directory whose manifest.yaml has skill: <skill_name>."""
    root = project_root or Path.cwd()
    skills_root = root / "skills"
    if not skills_root.exists():
        return None

    for entry in skills_root.iterdir():
        if not entry.is_dir():
            continue
        manifest_path = entry / "manifest.yaml"
        if not manifest_path.exists():
            continue
        try:
            manifest = read_manifest(entry)
            if manifest.skill == skill_name:
                return str(entry)
        except Exception:
            pass

    return None


def replay_skills(options: ReplayOptions) -> ReplayResult:
    """
    Replay a list of skills from a clean base state.
    Used by uninstall (replay-without) and rebase.
    """
    project_root = options.project_root or Path.cwd()
    base_dir = project_root / BASE_DIR
    path_remap = load_path_remap()

    per_skill: dict[str, dict] = {}
    all_merge_conflicts: list[str] = []

    # 1. Collect all files touched by any skill
    all_touched: set[str] = set()
    for skill_name in options.skills:
        skill_dir_str = options.skill_dirs.get(skill_name)
        if not skill_dir_str:
            per_skill[skill_name] = {"success": False, "error": f"Skill directory not found for: {skill_name}"}
            return ReplayResult(
                success=False,
                per_skill=per_skill,
                error=f"Missing skill directory for: {skill_name}",
            )
        manifest = read_manifest(skill_dir_str)
        all_touched.update(manifest.adds)
        all_touched.update(manifest.modifies)

    # 2. Reset touched files to clean base
    for rel_path in all_touched:
        resolved = resolve_path_remap(rel_path, path_remap)
        current_path = project_root / resolved
        base_path = base_dir / resolved

        if base_path.exists():
            current_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(base_path), str(current_path))
        elif current_path.exists():
            current_path.unlink()

    # 3. Replay each skill in order
    all_npm_deps: dict[str, str] = {}
    all_env_additions: list[str] = []
    has_npm_deps = False

    for skill_name in options.skills:
        skill_dir = Path(options.skill_dirs[skill_name])
        try:
            manifest = read_manifest(skill_dir)

            # File ops
            if manifest.file_ops:
                fo_result = execute_file_ops(manifest.file_ops, project_root)
                if not fo_result.success:
                    per_skill[skill_name] = {
                        "success": False,
                        "error": f"File operations failed: {'; '.join(fo_result.errors)}",
                    }
                    return ReplayResult(
                        success=False,
                        per_skill=per_skill,
                        error=f"File ops failed for {skill_name}",
                    )

            # Copy add/ files
            add_dir = skill_dir / "add"
            if add_dir.exists():
                for rel_path in manifest.adds:
                    resolved_dest = resolve_path_remap(rel_path, path_remap)
                    dest_path = project_root / resolved_dest
                    src_path = add_dir / rel_path
                    if src_path.exists():
                        dest_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src_path), str(dest_path))

            # Three-way merge modify/ files
            skill_conflicts: list[str] = []
            for rel_path in manifest.modifies:
                resolved = resolve_path_remap(rel_path, path_remap)
                current_path = project_root / resolved
                base_path = base_dir / resolved
                skill_path = skill_dir / "modify" / rel_path

                if not skill_path.exists():
                    skill_conflicts.append(rel_path)
                    continue

                if not current_path.exists():
                    current_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(skill_path), str(current_path))
                    continue

                if not base_path.exists():
                    base_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(current_path), str(base_path))

                with tempfile.NamedTemporaryFile(suffix=f"-{current_path.name}", delete=False) as tmp:
                    tmp_path = Path(tmp.name)

                shutil.copy2(str(current_path), str(tmp_path))
                result = merge_file(str(tmp_path), str(base_path), str(skill_path), str(tmp_path))
                shutil.copy2(str(tmp_path), str(current_path))
                tmp_path.unlink(missing_ok=True)

                if not result.clean:
                    skill_conflicts.append(resolved)

            if skill_conflicts:
                all_merge_conflicts.extend(skill_conflicts)
                per_skill[skill_name] = {
                    "success": False,
                    "error": f"Merge conflicts: {', '.join(skill_conflicts)}",
                }
                break  # Stop on first conflict
            else:
                per_skill[skill_name] = {"success": True}

            # Collect structured ops
            if manifest.structured:
                npm_deps = manifest.structured.get("npm_dependencies")
                if npm_deps:
                    all_npm_deps.update(npm_deps)
                    has_npm_deps = True
                env_additions = manifest.structured.get("env_additions")
                if env_additions:
                    all_env_additions.extend(env_additions)

        except Exception as e:
            per_skill[skill_name] = {"success": False, "error": str(e)}
            return ReplayResult(
                success=False,
                per_skill=per_skill,
                error=f"Replay failed for {skill_name}: {e}",
            )

    if all_merge_conflicts:
        return ReplayResult(
            success=False,
            per_skill=per_skill,
            merge_conflicts=all_merge_conflicts,
            error=f"Unresolved merge conflicts: {', '.join(all_merge_conflicts)}",
        )

    # 4. Apply aggregated structured operations
    if has_npm_deps:
        pkg_path = project_root / "package.json"
        if pkg_path.exists():
            import json
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
            merged, _ = merge_npm_dependencies(pkg.get("dependencies", {}), all_npm_deps)
            pkg["dependencies"] = merged
            pkg_path.write_text(json.dumps(pkg, indent=2), encoding="utf-8")

    if all_env_additions:
        env_path = project_root / ".env.example"
        existing = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        merged_env = merge_env_additions(existing, all_env_additions)
        env_path.write_text("\n".join(merged_env) + "\n", encoding="utf-8")

    return ReplayResult(success=True, per_skill=per_skill)

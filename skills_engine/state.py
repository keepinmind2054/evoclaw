"""State management for the Evoclaw skills engine."""

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .constants import EVOCLAW_DIR, SKILLS_SCHEMA_VERSION, STATE_FILE
from .types import AppliedSkill, CustomModification, SkillState


def _get_state_path() -> Path:
    return Path.cwd() / EVOCLAW_DIR / STATE_FILE


def _parse_state(data: dict) -> SkillState:
    applied = [
        AppliedSkill(
            name=s["name"],
            version=s["version"],
            applied_at=s["applied_at"],
            file_hashes=s.get("file_hashes", {}),
            structured_outcomes=s.get("structured_outcomes"),
            custom_patch=s.get("custom_patch"),
            custom_patch_description=s.get("custom_patch_description"),
        )
        for s in data.get("applied_skills", [])
    ]
    mods = [
        CustomModification(
            description=m["description"],
            applied_at=m["applied_at"],
            files_modified=m.get("files_modified", []),
            patch_file=m["patch_file"],
        )
        for m in data.get("custom_modifications", [])
    ]
    return SkillState(
        skills_system_version=data["skills_system_version"],
        core_version=data["core_version"],
        applied_skills=applied,
        custom_modifications=mods,
        path_remap=data.get("path_remap", {}),
        rebased_at=data.get("rebased_at"),
    )


def _state_to_dict(state: SkillState) -> dict:
    d = {
        "skills_system_version": state.skills_system_version,
        "core_version": state.core_version,
        "applied_skills": [
            {
                "name": s.name,
                "version": s.version,
                "applied_at": s.applied_at,
                "file_hashes": s.file_hashes,
                **({"structured_outcomes": s.structured_outcomes} if s.structured_outcomes else {}),
                **({"custom_patch": s.custom_patch} if s.custom_patch else {}),
                **({"custom_patch_description": s.custom_patch_description} if s.custom_patch_description else {}),
            }
            for s in state.applied_skills
        ],
    }
    if state.custom_modifications:
        d["custom_modifications"] = [
            {
                "description": m.description,
                "applied_at": m.applied_at,
                "files_modified": m.files_modified,
                "patch_file": m.patch_file,
            }
            for m in state.custom_modifications
        ]
    if state.path_remap:
        d["path_remap"] = state.path_remap
    if state.rebased_at:
        d["rebased_at"] = state.rebased_at
    return d


def read_state() -> SkillState:
    state_path = _get_state_path()
    if not state_path.exists():
        raise FileNotFoundError(
            ".evoclaw/state.yaml not found. Run init_skills_system() first."
        )
    raw = state_path.read_text(encoding="utf-8")
    # BUG-FIX: yaml.safe_load returns None on empty/truncated file (e.g. crash
    # during atomic replace).  Fall back to the .tmp file if it exists and is
    # valid; otherwise raise a descriptive error rather than an AttributeError.
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        # Try the temp file left by a failed atomic write
        tmp_path = Path(str(state_path) + ".tmp")
        if tmp_path.exists():
            try:
                data = yaml.safe_load(tmp_path.read_text(encoding="utf-8"))
            except Exception:
                data = None
        if not isinstance(data, dict):
            raise RuntimeError(
                f"{state_path} is empty or corrupt. "
                "Restore from backup or re-run init_skills_system()."
            )

    state = _parse_state(data)

    if compare_semver(state.skills_system_version, SKILLS_SCHEMA_VERSION) > 0:
        raise RuntimeError(
            f"state.yaml version {state.skills_system_version} is newer than "
            f"tooling version {SKILLS_SCHEMA_VERSION}. Update your skills engine."
        )
    return state


def write_state(state: SkillState) -> None:
    state_path = _get_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.dump(_state_to_dict(state), sort_keys=True, allow_unicode=True)
    # Atomic write via temp file + rename
    tmp_path = Path(str(state_path) + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(state_path)
    # BUG-FIX: clean up any leftover .tmp file from a previous failed write
    # (the replace above already handles the normal case, but a previous crash
    # might have left a stale .tmp that is now outdated — remove it).
    # NOTE: tmp_path.replace() already moves the file so the .tmp is gone on
    # success; this unlink is a no-op in that case but protects the crash path.
    tmp_path.unlink(missing_ok=True)


def record_skill_application(
    skill_name: str,
    version: str,
    file_hashes: dict[str, str],
    structured_outcomes: dict | None = None,
) -> None:
    state = read_state()
    state.applied_skills = [s for s in state.applied_skills if s.name != skill_name]
    state.applied_skills.append(
        AppliedSkill(
            name=skill_name,
            version=version,
            applied_at=datetime.now(timezone.utc).isoformat(),
            file_hashes=file_hashes,
            structured_outcomes=structured_outcomes,
        )
    )
    write_state(state)


def get_applied_skills() -> list[AppliedSkill]:
    return read_state().applied_skills


def record_custom_modification(
    description: str,
    files_modified: list[str],
    patch_file: str,
) -> None:
    state = read_state()
    state.custom_modifications.append(
        CustomModification(
            description=description,
            applied_at=datetime.now(timezone.utc).isoformat(),
            files_modified=files_modified,
            patch_file=patch_file,
        )
    )
    write_state(state)


def get_custom_modifications() -> list[CustomModification]:
    return read_state().custom_modifications


def compute_file_hash(file_path: str | Path) -> str:
    content = Path(file_path).read_bytes()
    return hashlib.sha256(content).hexdigest()


def compare_semver(a: str, b: str) -> int:
    """Compare two semver strings. Returns negative if a < b, 0 if equal, positive if a > b."""
    parts_a = [int(x) for x in a.split(".")]
    parts_b = [int(x) for x in b.split(".")]
    for i in range(max(len(parts_a), len(parts_b))):
        va = parts_a[i] if i < len(parts_a) else 0
        vb = parts_b[i] if i < len(parts_b) else 0
        if va != vb:
            return va - vb
    return 0

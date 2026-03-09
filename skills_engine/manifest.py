"""Manifest reading and validation for the Evoclaw skills engine."""

import os
from pathlib import Path

import yaml

from .constants import SKILLS_SCHEMA_VERSION
from .state import compare_semver, get_applied_skills, read_state
from .types import SkillManifest


def read_manifest(skill_dir: str | Path) -> SkillManifest:
    skill_dir = Path(skill_dir)
    manifest_path = skill_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    # Validate required fields
    required = ["skill", "version", "core_version", "adds", "modifies"]
    for field in required:
        if data.get(field) is None:
            raise ValueError(f"Manifest missing required field: {field}")

    manifest = SkillManifest(
        skill=data["skill"],
        version=str(data["version"]),
        description=data.get("description", ""),
        core_version=str(data["core_version"]),
        adds=data["adds"],
        modifies=data["modifies"],
        conflicts=data.get("conflicts") or [],
        depends=data.get("depends") or [],
        file_ops=data.get("file_ops") or [],
        structured=data.get("structured"),
        test=data.get("test"),
        author=data.get("author"),
        license=data.get("license"),
        min_skills_system_version=data.get("min_skills_system_version"),
        tested_with=data.get("tested_with") or [],
        post_apply=data.get("post_apply") or [],
    )

    # Validate paths don't escape project root
    all_paths = manifest.adds + manifest.modifies
    for p in all_paths:
        if ".." in p or os.path.isabs(p):
            raise ValueError(
                f"Invalid path in manifest: {p} (must be relative without '..')"
            )

    return manifest


def check_core_version(manifest: SkillManifest) -> dict:
    state = read_state()
    cmp = compare_semver(manifest.core_version, state.core_version)
    if cmp > 0:
        return {
            "ok": True,
            "warning": (
                f"Skill targets core {manifest.core_version} but current core is "
                f"{state.core_version}. The merge might still work but there's a "
                "compatibility risk."
            ),
        }
    return {"ok": True}


def check_dependencies(manifest: SkillManifest) -> dict:
    applied = get_applied_skills()
    applied_names = {s.name for s in applied}
    missing = [dep for dep in manifest.depends if dep not in applied_names]
    return {"ok": len(missing) == 0, "missing": missing}


def check_system_version(manifest: SkillManifest) -> dict:
    if not manifest.min_skills_system_version:
        return {"ok": True}
    cmp = compare_semver(manifest.min_skills_system_version, SKILLS_SCHEMA_VERSION)
    if cmp > 0:
        return {
            "ok": False,
            "error": (
                f"Skill requires skills system version "
                f"{manifest.min_skills_system_version} but current is "
                f"{SKILLS_SCHEMA_VERSION}. Update your skills engine."
            ),
        }
    return {"ok": True}


def check_conflicts(manifest: SkillManifest) -> dict:
    applied = get_applied_skills()
    applied_names = {s.name for s in applied}
    conflicting = [c for c in manifest.conflicts if c in applied_names]
    return {"ok": len(conflicting) == 0, "conflicting": conflicting}

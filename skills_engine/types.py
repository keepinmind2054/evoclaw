"""Type definitions for the Evoclaw skills engine."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SkillManifest:
    skill: str
    version: str
    description: str
    core_version: str
    adds: list[str]
    modifies: list[str]
    conflicts: list[str] = field(default_factory=list)
    depends: list[str] = field(default_factory=list)
    file_ops: list[dict] = field(default_factory=list)
    structured: Optional[dict[str, Any]] = None
    test: Optional[str] = None
    author: Optional[str] = None
    license: Optional[str] = None
    min_skills_system_version: Optional[str] = None
    tested_with: list[str] = field(default_factory=list)
    post_apply: list[str] = field(default_factory=list)


@dataclass
class AppliedSkill:
    name: str
    version: str
    applied_at: str
    file_hashes: dict[str, str] = field(default_factory=dict)
    structured_outcomes: Optional[dict[str, Any]] = None
    custom_patch: Optional[str] = None
    custom_patch_description: Optional[str] = None


@dataclass
class CustomModification:
    description: str
    applied_at: str
    files_modified: list[str]
    patch_file: str


@dataclass
class SkillState:
    skills_system_version: str
    core_version: str
    applied_skills: list[AppliedSkill] = field(default_factory=list)
    custom_modifications: list[CustomModification] = field(default_factory=list)
    path_remap: dict[str, str] = field(default_factory=dict)
    rebased_at: Optional[str] = None


@dataclass
class ApplyResult:
    success: bool
    skill: str
    version: str
    merge_conflicts: list[str] = field(default_factory=list)
    backup_pending: bool = False
    untracked_changes: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class MergeResult:
    clean: bool
    exit_code: int


@dataclass
class FileOpsResult:
    success: bool
    executed: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class UninstallResult:
    success: bool
    skill: str
    custom_patch_warning: Optional[str] = None
    replay_results: Optional[dict[str, bool]] = None
    error: Optional[str] = None


@dataclass
class RebaseResult:
    success: bool
    patch_file: Optional[str] = None
    files_in_patch: int = 0
    rebased_at: Optional[str] = None
    merge_conflicts: list[str] = field(default_factory=list)
    backup_pending: bool = False
    error: Optional[str] = None

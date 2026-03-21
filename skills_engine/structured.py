"""Structured metadata merging for the Evoclaw skills engine (npm deps, env vars, etc.)."""

import json
import os
import subprocess
from pathlib import Path


def compare_semver_range(a: str, b: str) -> bool:
    """Check if two semver range strings are compatible (both point to same major)."""
    def _major(s: str) -> str:
        s = s.lstrip("^~>=<")
        return s.split(".")[0] if s else "0"
    return _major(a) == _major(b)


def are_ranges_compatible(range_a: str, range_b: str) -> bool:
    """Return True if two npm version ranges are compatible."""
    return compare_semver_range(range_a, range_b)


def merge_npm_dependencies(
    base: dict[str, str],
    incoming: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """
    Merge two sets of npm dependencies.
    Returns (merged, conflicts) where conflicts is a list of warning strings.
    """
    merged = dict(base)
    conflicts = []

    for pkg, version in incoming.items():
        if pkg in merged:
            if merged[pkg] != version and not are_ranges_compatible(merged[pkg], version):
                conflicts.append(
                    f"npm dep conflict: {pkg} {merged[pkg]} vs {version} — keeping existing"
                )
        else:
            merged[pkg] = version

    return merged, conflicts


def merge_env_additions(existing: list[str], incoming: list[str]) -> list[str]:
    """
    Merge env variable additions. Deduplicates by variable name (KEY=value → KEY).
    BUG-FIX: comment lines (starting with '#') are now excluded from key
    extraction so they don't accidentally block legitimate KEY=value additions.
    """
    existing_keys = {
        line.split("=")[0].strip()
        for line in existing
        if "=" in line and not line.lstrip().startswith("#")
    }
    result = list(existing)
    for line in incoming:
        # Skip comment lines in incoming additions too
        if line.lstrip().startswith("#"):
            result.append(line)
            continue
        key = line.split("=")[0].strip()
        if key not in existing_keys:
            result.append(line)
            existing_keys.add(key)
    return result


def merge_docker_compose_services(
    base: dict,
    incoming: dict,
) -> tuple[dict, list[str]]:
    """
    Merge docker-compose service definitions.
    Incoming services that don't exist in base are added.
    Conflicts (same service name) are reported.
    """
    merged = dict(base)
    conflicts = []

    for service_name, service_def in incoming.items():
        if service_name in merged:
            conflicts.append(
                f"docker-compose service conflict: '{service_name}' already exists — skipping"
            )
        else:
            merged[service_name] = service_def

    return merged, conflicts


def run_npm_install(project_root: Path | None = None) -> None:
    """Run npm install in the project root."""
    cwd = str(project_root or Path.cwd())
    result = subprocess.run(["npm", "install"], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"npm install failed:\n{result.stderr}")

"""Migration utilities for the Evoclaw skills engine."""

import subprocess
from pathlib import Path

from .constants import BASE_DIR, CUSTOM_DIR
from .init import init_evoclaw_dir
from .state import record_custom_modification


def init_skills_system(project_root: Path | None = None) -> None:
    """Initialize the skills system from scratch."""
    init_evoclaw_dir(project_root)
    print("Skills system initialized. .evoclaw/ directory created.")


def migrate_existing(project_root: Path | None = None) -> None:
    """
    Migrate an existing Evoclaw installation to the skills system.

    1. Takes a fresh base snapshot of the current codebase.
    2. Diffs the current host/ against the base to capture custom modifications.
    3. Records those modifications in state.yaml.
    """
    root = project_root or Path.cwd()

    # Fresh init
    init_evoclaw_dir(root)

    # Diff current host/ against base to find custom modifications
    base_host_dir = root / BASE_DIR / "host"
    host_dir = root / "host"
    custom_dir = root / CUSTOM_DIR
    patch_rel_path = f"{CUSTOM_DIR}/migration.patch"

    try:
        result = subprocess.run(
            ["diff", "-ruN", str(base_host_dir), str(host_dir)],
            capture_output=True,
            text=True,
            cwd=str(root),
        )
        # diff exits 1 when files differ (expected)
        diff_output = result.stdout

        if diff_output.strip():
            custom_dir.mkdir(parents=True, exist_ok=True)
            patch_path = root / patch_rel_path
            patch_path.write_text(diff_output, encoding="utf-8")

            # Extract modified file paths from the diff header
            import re
            files_modified = []
            for match in re.finditer(r"^diff -ruN .+ (.+)$", diff_output, re.MULTILINE):
                rel = str(Path(match.group(1)).relative_to(root))
                if not rel.startswith(".evoclaw"):
                    files_modified.append(rel)

            record_custom_modification(
                description="Pre-skills migration",
                files_modified=files_modified,
                patch_file=patch_rel_path,
            )
            print("Custom modifications captured in .evoclaw/custom/migration.patch")
        else:
            print("No custom modifications detected.")

    except Exception as e:
        print(f"Could not generate diff: {e}. Continuing with clean base.")

    print("Migration complete. Skills system ready.")

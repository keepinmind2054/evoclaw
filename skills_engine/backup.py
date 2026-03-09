"""Backup and restore for the Evoclaw skills engine."""

import shutil
from pathlib import Path

from .constants import BACKUP_DIR

TOMBSTONE_SUFFIX = ".tombstone"


def _get_backup_dir() -> Path:
    return Path.cwd() / BACKUP_DIR


def create_backup(file_paths: list[str | Path]) -> None:
    """Backup files before applying a skill. Non-existent files get a tombstone."""
    backup_dir = _get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    for file_path in file_paths:
        abs_path = Path(file_path).resolve()
        relative_path = abs_path.relative_to(Path.cwd())
        backup_path = backup_dir / relative_path
        backup_path.parent.mkdir(parents=True, exist_ok=True)

        if abs_path.exists():
            shutil.copy2(str(abs_path), str(backup_path))
        else:
            # File doesn't exist yet — write tombstone so restore can delete it
            Path(str(backup_path) + TOMBSTONE_SUFFIX).write_text("", encoding="utf-8")


def restore_backup() -> None:
    """Restore all files from the backup directory."""
    backup_dir = _get_backup_dir()
    if not backup_dir.exists():
        return

    for backup_file in backup_dir.rglob("*"):
        if backup_file.is_dir():
            continue

        if backup_file.name.endswith(TOMBSTONE_SUFFIX):
            # Tombstone: delete the corresponding project file
            tomb_rel = backup_file.relative_to(backup_dir)
            orig_rel = str(tomb_rel)[: -len(TOMBSTONE_SUFFIX)]
            orig_path = Path.cwd() / orig_rel
            orig_path.unlink(missing_ok=True)
        else:
            relative = backup_file.relative_to(backup_dir)
            orig_path = Path.cwd() / relative
            orig_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(backup_file), str(orig_path))


def clear_backup() -> None:
    """Remove the backup directory."""
    backup_dir = _get_backup_dir()
    if backup_dir.exists():
        shutil.rmtree(str(backup_dir))

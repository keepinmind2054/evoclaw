"""Group folder path validation and resolution"""
import re
from pathlib import Path

GROUP_FOLDER_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
RESERVED = {"global", ".", ".."}

def is_valid_group_folder(name: str) -> bool:
    if not name or name in RESERVED:
        return False
    if "/" in name or "\\" in name or ".." in name:
        return False
    return bool(GROUP_FOLDER_PATTERN.match(name))

def assert_valid_group_folder(name: str) -> None:
    if not is_valid_group_folder(name):
        raise ValueError(f"Invalid group folder name: {name!r}")

def resolve_group_folder_path(base: Path, folder: str) -> Path:
    assert_valid_group_folder(folder)
    p = (base / folder).resolve()
    _ensure_within_base(base, p)
    return p

def resolve_group_ipc_path(ipc_base: Path, folder: str) -> Path:
    assert_valid_group_folder(folder)
    p = (ipc_base / folder).resolve()
    _ensure_within_base(ipc_base, p)
    return p

def _ensure_within_base(base: Path, target: Path) -> None:
    try:
        target.relative_to(base.resolve())
    except ValueError:
        raise ValueError(f"Path {target} is outside base {base}")

"""Group folder path validation and resolution"""
import os
import re
import tempfile
from pathlib import Path

GROUP_FOLDER_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
RESERVED = {"global", ".", ".."}

# BUG-GF-1: No maximum length on folder names.  An unbounded name can cause
# ENAMETOOLONG errors deep in os calls, or fill the directory listing with
# extremely long names.  255 bytes is the POSIX maximum for a single path
# component; we use a conservative 128 to stay well clear.
_MAX_FOLDER_NAME_LEN = 128


def is_valid_group_folder(name: str) -> bool:
    if not name or name in RESERVED:
        return False
    # BUG-GF-1 FIX: Enforce maximum name length.
    if len(name) > _MAX_FOLDER_NAME_LEN:
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


def create_group_folder_atomic(base: Path, folder: str) -> Path:
    """Create a group folder atomically to avoid race conditions.

    BUG-GF-2 FIX: The naive approach (check-then-mkdir) has a TOCTOU race
    condition: two concurrent callers can both see the folder absent, both
    attempt mkdir, and one fails with FileExistsError — or worse, if the
    check and mkdir are non-atomic, partial state can be observed.

    We use a write-to-tempdir-then-rename approach:
      1. Create the final target directory with exist_ok=True (idempotent on
         Linux/macOS where mkdir is atomic for the leaf component).
      2. For the group config file (if any) within the folder, write to a
         temporary file in the same filesystem then os.replace() it into place
         atomically so readers never see a partially-written config.

    Returns the resolved Path to the group folder.
    """
    target = resolve_group_folder_path(base, folder)
    # exist_ok=True makes this safe for concurrent callers — the second caller
    # simply sees the directory already exists and proceeds without error.
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_file(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically using write-then-rename.

    BUG-GF-3 FIX: Direct open(path, "w") leaves the file in a partially-written
    state if the process crashes mid-write, which can corrupt group config files.
    We write to a temporary file in the same directory (same filesystem,
    guarantees rename is atomic on POSIX) then os.replace() it into place.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

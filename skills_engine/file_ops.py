"""File operations (rename, move, delete) for the Evoclaw skills engine."""

import shutil
from pathlib import Path

from .types import FileOpsResult


def _check_path_traversal(rel: str, op_type: str, key: str) -> str | None:
    """
    Return an error string if rel escapes the project root, else None.
    BUG-FIX: without this check a malicious skill could use file_ops to rename,
    move, or delete arbitrary files outside the project directory.
    """
    if not rel:
        return f"{op_type}: empty path for key '{key}'"
    p = Path(rel)
    if p.is_absolute():
        return f"{op_type}: absolute path not allowed: {rel!r}"
    for part in p.parts:
        if part == "..":
            return f"{op_type}: path traversal not allowed: {rel!r}"
    return None


def execute_file_ops(file_ops: list[dict], project_root: Path | None = None) -> FileOpsResult:
    """
    Execute a list of file operations from a skill manifest.

    Supported operations:
      {"type": "rename", "from": "old/path", "to": "new/path"}
      {"type": "move",   "from": "old/path", "to": "new/path"}
      {"type": "delete", "path": "file/to/delete"}
    """
    if project_root is None:
        project_root = Path.cwd()

    executed = []
    warnings = []
    errors = []

    for op in file_ops:
        op_type = op.get("type")
        try:
            if op_type in ("rename", "move"):
                # BUG-FIX: validate both 'from' and 'to' paths
                for key in ("from", "to"):
                    err = _check_path_traversal(op.get(key, ""), op_type, key)
                    if err:
                        errors.append(err)
                        break
                else:
                    src = project_root / op["from"]
                    dst = project_root / op["to"]
                    if not src.exists():
                        warnings.append(f"{op_type}: source not found: {op['from']}")
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    executed.append(op)

            elif op_type == "delete":
                # BUG-FIX: validate 'path'
                err = _check_path_traversal(op.get("path", ""), op_type, "path")
                if err:
                    errors.append(err)
                    continue
                target = project_root / op["path"]
                if not target.exists():
                    warnings.append(f"delete: path not found: {op['path']}")
                    continue
                if target.is_dir():
                    shutil.rmtree(str(target))
                else:
                    target.unlink()
                executed.append(op)

            else:
                errors.append(f"Unknown file_op type: {op_type}")

        except Exception as e:
            errors.append(f"{op_type} failed: {e}")

    return FileOpsResult(
        success=len(errors) == 0,
        executed=executed,
        warnings=warnings,
        errors=errors,
    )

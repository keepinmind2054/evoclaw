"""File operations (rename, move, delete) for the Evoclaw skills engine."""

import shutil
from pathlib import Path

from .types import FileOpsResult


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
                src = project_root / op["from"]
                dst = project_root / op["to"]
                if not src.exists():
                    warnings.append(f"{op_type}: source not found: {op['from']}")
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                executed.append(op)

            elif op_type == "delete":
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

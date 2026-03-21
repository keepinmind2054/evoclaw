"""File-based locking for the Evoclaw skills engine."""

import json
import os
import time
from pathlib import Path

from .constants import LOCK_FILE

STALE_TIMEOUT_SECS = 5 * 60  # 5 minutes


def _get_lock_path() -> Path:
    return Path.cwd() / LOCK_FILE


def _is_process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        # BUG-FIX: on Windows os.kill raises OSError for non-existent PIDs
        # rather than ProcessLookupError.  Treat any OSError as "not alive" so
        # that stale lock files created on Windows are correctly evicted.
        return False


def _is_stale(lock: dict) -> bool:
    return time.time() - lock.get("timestamp", 0) > STALE_TIMEOUT_SECS


def acquire_lock() -> "LockHandle":
    """Acquire the skills engine lock. Returns a LockHandle context manager."""
    lock_path = _get_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    lock_info = {"pid": os.getpid(), "timestamp": time.time()}

    # Try atomic create
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            json.dump(lock_info, f)
        return LockHandle(lock_path, os.getpid())
    except FileExistsError:
        pass

    # Lock file exists — check if stale/dead
    try:
        existing = json.loads(lock_path.read_text(encoding="utf-8"))
        if not _is_stale(existing) and _is_process_alive(existing["pid"]):
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(existing["timestamp"]))
            raise RuntimeError(
                f"Operation in progress (pid {existing['pid']}, started {ts}). "
                f"If this is stale, delete {LOCK_FILE}"
            )
    except (json.JSONDecodeError, KeyError, OSError):
        pass  # Corrupt — overwrite

    # Stale or corrupt — try to remove and re-acquire
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            json.dump(lock_info, f)
        return LockHandle(lock_path, os.getpid())
    except FileExistsError:
        raise RuntimeError("Lock contention: another process acquired the lock. Retry.")


def release_lock() -> None:
    lock_path = _get_lock_path()
    if not lock_path.exists():
        return
    try:
        existing = json.loads(lock_path.read_text(encoding="utf-8"))
        if existing.get("pid") == os.getpid():
            lock_path.unlink(missing_ok=True)
    except (json.JSONDecodeError, OSError):
        lock_path.unlink(missing_ok=True)


def is_locked() -> bool:
    lock_path = _get_lock_path()
    if not lock_path.exists():
        return False
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        return not _is_stale(lock) and _is_process_alive(lock["pid"])
    except (json.JSONDecodeError, OSError):
        return False


class LockHandle:
    """Context manager that releases the lock on exit."""

    def __init__(self, lock_path: Path, pid: int):
        self._lock_path = lock_path
        self._pid = pid

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    def release(self):
        if self._lock_path.exists():
            try:
                existing = json.loads(self._lock_path.read_text(encoding="utf-8"))
                if existing.get("pid") == self._pid:
                    self._lock_path.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError):
                self._lock_path.unlink(missing_ok=True)

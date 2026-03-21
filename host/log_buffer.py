"""In-memory ring buffer for log records — used by dashboard live log viewer."""
import logging
import threading
from collections import deque

_MAX_SIZE = 2000
_buffer: deque = deque(maxlen=_MAX_SIZE)
_lock = threading.Lock()
_counter = 0  # monotonically increasing index for SSE "since" queries


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _counter
        try:
            msg = self.format(record)
            with _lock:
                _counter += 1
                _buffer.append({
                    "idx": _counter,
                    "time": record.asctime if hasattr(record, "asctime") else "",
                    "level": record.levelname,
                    "name": record.name,
                    "msg": msg,
                })
        except Exception:
            pass


def install() -> None:
    """Install the buffer handler on the root logger. Call once at startup."""
    handler = _BufferHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logging.getLogger().addHandler(handler)


def get_logs(since_idx: int = 0, level: str = None, limit: int = 200) -> list:
    """Return log entries with idx > since_idx, optionally filtered by level.

    BUG-LB-01 (MEDIUM): `limit` was forwarded from caller without a cap
    here.  Even though the dashboard now caps at 1000, defensive clamping
    inside the buffer prevents misuse from other callers.
    """
    # Clamp limit defensively
    limit = max(1, min(int(limit), _MAX_SIZE))
    with _lock:
        items = list(_buffer)
    if since_idx > 0:
        items = [i for i in items if i["idx"] > since_idx]
    if level and level.upper() != "ALL":
        items = [i for i in items if i["level"] == level.upper()]
    return items[-limit:]


def get_error_count() -> int:
    """Count ERROR+ entries in the buffer."""
    with _lock:
        return sum(1 for i in _buffer if i["level"] in ("ERROR", "CRITICAL"))

"""Structured JSON log formatter for evoclaw.

When LOG_FORMAT=json, every log record is emitted as a single-line JSON object:
{
  "ts": "2026-03-19T13:15:00.123Z",   # ISO-8601 UTC timestamp
  "level": "INFO",
  "logger": "host.main",
  "msg": "Processing 3 message(s) for telegram_foo",
  "run_id": "a1b2c3d4",              # present when set via extra={"run_id": ...}
  "jid": "tg:123456",               # any extra= fields passed through
  "folder": "telegram_foo",
  "exc": "Traceback ..."            # only present when exception attached
}
"""
import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    # Fields that should NOT be copied from LogRecord into the output
    # (they're either rendered explicitly or are internal Python logging state)
    _SKIP = frozenset({
        "args", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process",
        "processName", "relativeCreated", "stack_info", "taskName",
        "thread", "threadName",
    })

    def format(self, record: logging.LogRecord) -> str:
        # Build the message first (applies %-formatting)
        record.message = record.getMessage()

        obj: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.message,
        }

        # Copy any extra= fields (run_id, jid, folder, etc.)
        for key, value in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                obj[key] = value

        # Append exception traceback if present
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)

        return json.dumps(obj, ensure_ascii=False, default=str)

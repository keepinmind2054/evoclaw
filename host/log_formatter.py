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
        "funcName", "levelno", "levelname",  # BUG-LF-01: levelname rendered as "level" below
        "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process",
        "processName", "relativeCreated", "stack_info", "taskName",
        "thread", "threadName",
        # BUG-LF-02 (HIGH): asctime should be skipped — it is only present
        # when a previous Formatter has already called formatTime(), and if
        # copied it duplicates the timestamp under a different key name.
        "asctime",
    })

    # BUG-LF-03 (HIGH): Any extra= field is blindly forwarded to the JSON
    # output.  A developer passing extra={"token": "sk-...", "api_key": ...}
    # would leak secrets into structured logs (and any log shipper).
    # Maintain a denylist of well-known sensitive field names that must never
    # appear in emitted log records.
    _SENSITIVE_KEYS = frozenset({
        "token", "api_key", "apikey", "secret", "password", "passwd",
        "credential", "auth", "authorization", "bearer",
        "telegram_bot_token", "whatsapp_token", "discord_bot_token",
        "slack_bot_token", "claude_api_key", "openai_api_key",
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
            if key in self._SKIP or key.startswith("_"):
                continue
            # BUG-LF-03 (HIGH): Suppress sensitive keys to prevent secret
            # exfiltration via structured logging pipelines.
            if key.lower() in self._SENSITIVE_KEYS:
                obj[key] = "***REDACTED***"
                continue
            obj[key] = value

        # Append exception traceback if present
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)

        return json.dumps(obj, ensure_ascii=False, default=str)

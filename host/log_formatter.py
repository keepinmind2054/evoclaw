"""Structured JSON log formatter + secret-URL redaction for evoclaw.

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

#590: this module also exposes ``SecretUrlRedactor``, a ``logging.Filter`` that
rewrites known secret-bearing URL patterns inside the record's ``msg`` and
``args`` so third-party loggers (httpx, urllib3, discord.gateway, ...) cannot
leak credentials into stdout / pm2 log files.
"""
import json
import logging
import re
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


# #590: redact secret-bearing URL patterns so third-party loggers (httpx,
# urllib3, discord.gateway, ...) cannot leak credentials into pm2 / log files.
#
# Currently covers:
#   - Telegram:  https://api.telegram.org/bot<digits>:<base64>/<method>
#                → https://api.telegram.org/bot***REDACTED***/<method>
#   - Discord:   https://discord.com/api/webhooks/<id>/<token>
#                → https://discord.com/api/webhooks/<id>/***REDACTED***
#   - Slack:     https://hooks.slack.com/services/<workspace>/<channel>/<token>
#                → https://hooks.slack.com/services/<workspace>/<channel>/***REDACTED***
#
# Add new patterns here when a new channel / service is wired in.
_SECRET_URL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(api\.telegram\.org/bot)\d+:[A-Za-z0-9_-]+(/)"),
        r"\1***REDACTED***\2",
    ),
    (
        re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_-]+"),
        r"\1***REDACTED***",
    ),
    (
        re.compile(r"(hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/)[A-Za-z0-9_-]+"),
        r"\1***REDACTED***",
    ),
)


def _redact_url_secrets(text: str) -> str:
    """Apply every secret-URL pattern to *text*. No-op if no pattern matches."""
    for pattern, replacement in _SECRET_URL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SecretUrlRedactor(logging.Filter):
    """Redact secret-bearing URLs in any log record (msg + args).

    Installed once on the root logger by :func:`host.main._setup_logging` so
    it runs before every handler emit, covering third-party loggers as well
    as evoclaw's own.  Mutates the record in place; always returns True so
    the record itself is never dropped — only its content is sanitised.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_url_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: (_redact_url_secrets(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    _redact_url_secrets(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True

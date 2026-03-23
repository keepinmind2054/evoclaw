"""Tests for Phase 4C: structured JSON logging (JsonFormatter)."""
import json
import logging
import sys
import io
import traceback

import pytest


def _make_formatter():
    from host.log_formatter import JsonFormatter
    return JsonFormatter()


def _make_record(msg="test message", level=logging.INFO, name="test.logger", **extra):
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname="test_json_logging.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestJsonFormatter:
    """Tests for JsonFormatter."""

    def test_output_is_valid_json(self):
        """JsonFormatter must produce valid JSON for a basic log record."""
        formatter = _make_formatter()
        record = _make_record("hello world")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_required_fields_present(self):
        """Output must contain ts, level, logger, and msg fields."""
        formatter = _make_formatter()
        record = _make_record("required fields test", level=logging.WARNING, name="myapp.module")
        output = formatter.format(record)
        parsed = json.loads(output)

        assert "ts" in parsed, "Missing 'ts' field"
        assert "level" in parsed, "Missing 'level' field"
        assert "logger" in parsed, "Missing 'logger' field"
        assert "msg" in parsed, "Missing 'msg' field"

    def test_ts_format_is_iso8601_utc(self):
        """The 'ts' field must be in ISO-8601 UTC format ending with Z."""
        formatter = _make_formatter()
        record = _make_record("timestamp test")
        output = formatter.format(record)
        parsed = json.loads(output)

        ts = parsed["ts"]
        assert ts.endswith("Z"), f"Timestamp must end with Z, got: {ts}"
        # Should match pattern: YYYY-MM-DDTHH:MM:SS.mmmZ
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", ts), (
            f"Timestamp does not match ISO-8601 UTC format: {ts}"
        )

    def test_level_name_is_correct(self):
        """The 'level' field must match the log level name."""
        formatter = _make_formatter()
        for level, name in [
            (logging.DEBUG, "DEBUG"),
            (logging.INFO, "INFO"),
            (logging.WARNING, "WARNING"),
            (logging.ERROR, "ERROR"),
            (logging.CRITICAL, "CRITICAL"),
        ]:
            record = _make_record("level test", level=level)
            output = formatter.format(record)
            parsed = json.loads(output)
            assert parsed["level"] == name, f"Expected level {name}, got {parsed['level']}"

    def test_logger_name_is_correct(self):
        """The 'logger' field must match the logger name."""
        formatter = _make_formatter()
        record = _make_record("logger name test", name="host.main")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["logger"] == "host.main"

    def test_msg_field_contains_formatted_message(self):
        """The 'msg' field must contain the fully formatted log message."""
        formatter = _make_formatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Processing %d message(s) for %s",
            args=(3, "telegram_foo"),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["msg"] == "Processing 3 message(s) for telegram_foo"

    def test_extra_fields_appear_in_output(self):
        """Fields passed via extra= must appear in the JSON output."""
        formatter = _make_formatter()
        record = _make_record(
            "extra fields test",
            run_id="abc123",
            jid="tg:123456",
            folder="telegram_foo",
        )
        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed.get("run_id") == "abc123", f"Missing run_id, got: {parsed}"
        assert parsed.get("jid") == "tg:123456", f"Missing jid, got: {parsed}"
        assert parsed.get("folder") == "telegram_foo", f"Missing folder, got: {parsed}"

    def test_exception_info_appears_as_exc_field(self):
        """When a record has exc_info, the 'exc' field must contain the traceback."""
        formatter = _make_formatter()
        try:
            raise ValueError("something went wrong")
        except ValueError:
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="An error occurred",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        parsed = json.loads(output)

        assert "exc" in parsed, "Missing 'exc' field for exception"
        assert "ValueError" in parsed["exc"], "Exception type not in 'exc' field"
        assert "something went wrong" in parsed["exc"], "Exception message not in 'exc' field"

    def test_no_exc_field_when_no_exception(self):
        """When no exception is attached, 'exc' must not appear in output."""
        formatter = _make_formatter()
        record = _make_record("no exception")
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exc" not in parsed, f"'exc' should not be present without exception: {parsed}"

    def test_output_is_single_line(self):
        """Each log record must produce exactly one line of output (no embedded newlines)."""
        formatter = _make_formatter()
        record = _make_record("single line test")
        output = formatter.format(record)
        # The output should be a single JSON object with no unescaped newlines
        assert "\n" not in output, "JSON output must be a single line"

    def test_internal_fields_not_leaked(self):
        """Internal LogRecord fields (args, exc_info, etc.) must not appear in output."""
        formatter = _make_formatter()
        record = _make_record("internal fields test")
        output = formatter.format(record)
        parsed = json.loads(output)

        internal_fields = {"args", "exc_info", "exc_text", "filename", "funcName",
                           "levelno", "lineno", "module", "msecs", "pathname",
                           "process", "processName", "relativeCreated", "stack_info",
                           "thread", "threadName"}
        leaked = internal_fields & set(parsed.keys())
        assert not leaked, f"Internal fields leaked into JSON output: {leaked}"


class TestTextFormatRegression:
    """Verify that the text format (default) still works without regression."""

    def test_text_format_produces_human_readable_output(self):
        """Text format handler should produce non-JSON, human-readable output."""
        handler = logging.StreamHandler(io.StringIO())
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger = logging.getLogger("test.text.format")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        logger.info("Text format test message")

        output = handler.stream.getvalue()
        # Should contain the logger name and message but not be valid JSON
        assert "test.text.format" in output
        assert "Text format test message" in output
        with pytest.raises((json.JSONDecodeError, ValueError)):
            json.loads(output)

    def test_setup_logging_text_format(self, monkeypatch):
        """_setup_logging() with LOG_FORMAT=text should not set JsonFormatter.

        TEST-09 FIX: host.main imports host.health_monitor which imports psutil
        at module level.  Guard with importorskip so the test is a clear SKIP
        rather than a confusing ModuleNotFoundError when psutil is absent.
        """
        pytest.importorskip("psutil", reason="host.main requires psutil")
        monkeypatch.setenv("LOG_FORMAT", "text")
        monkeypatch.setenv("LOG_LEVEL", "WARNING")

        # Import and call _setup_logging
        import importlib
        import host.main as host_main
        importlib.reload(host_main)

        root = logging.getLogger()
        assert root.level == logging.WARNING or root.level <= logging.WARNING
        if root.handlers:
            handler = root.handlers[0]
            from host.log_formatter import JsonFormatter
            assert not isinstance(handler.formatter, JsonFormatter), (
                "Text format should not use JsonFormatter"
            )

    def test_setup_logging_json_format(self, monkeypatch):
        """_setup_logging() with LOG_FORMAT=json should set JsonFormatter."""
        pytest.importorskip("psutil", reason="host.main requires psutil")
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")

        import importlib
        import host.main as host_main
        importlib.reload(host_main)

        root = logging.getLogger()
        assert root.level == logging.DEBUG
        if root.handlers:
            handler = root.handlers[0]
            from host.log_formatter import JsonFormatter
            assert isinstance(handler.formatter, JsonFormatter), (
                "JSON format should use JsonFormatter"
            )

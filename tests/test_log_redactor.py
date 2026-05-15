"""Tests for the secret-URL redactor that protects pm2 logs (#590)."""
import logging

import pytest

from host.log_formatter import SecretUrlRedactor, _redact_url_secrets


@pytest.fixture
def filt() -> SecretUrlRedactor:
    return SecretUrlRedactor()


def _make_record(msg: str, args: tuple | dict | None = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


class TestRedactUrlSecrets:
    def test_telegram_url_is_redacted(self):
        s = "HTTP Request: POST https://api.telegram.org/bot8744114061:AAGzGHcU2KZI7Y0IRJFzJL245WCvW7_8pEo/getMe HTTP/1.1 200 OK"
        out = _redact_url_secrets(s)
        assert "AAGzGHcU2KZI7Y0IRJFzJL245WCvW7_8pEo" not in out
        assert "8744114061" not in out
        assert "/bot***REDACTED***/getMe" in out

    def test_discord_webhook_is_redacted(self):
        s = "POST https://discord.com/api/webhooks/123456/abcdefghijkl_MNOPQR-stuvwx"
        out = _redact_url_secrets(s)
        assert "abcdefghijkl_MNOPQR-stuvwx" not in out
        assert "/webhooks/123456/***REDACTED***" in out

    def test_discordapp_webhook_alias_is_redacted(self):
        s = "POST https://discordapp.com/api/webhooks/123/secrettoken"
        out = _redact_url_secrets(s)
        assert "secrettoken" not in out
        assert "/webhooks/123/***REDACTED***" in out

    def test_slack_hook_is_redacted(self):
        s = "POST https://hooks.slack.com/services/T0XXX/B0YYY/zZ_aB_secret_token"
        out = _redact_url_secrets(s)
        assert "zZ_aB_secret_token" not in out
        assert "/services/T0XXX/B0YYY/***REDACTED***" in out

    def test_plain_log_line_unchanged(self):
        s = "evoclaw - INFO - container started for telegram_foo"
        assert _redact_url_secrets(s) == s

    def test_partial_match_only_redacts_secret_part(self):
        s = "see api.telegram.org/bot123:abc/sendMessage and api.telegram.org/bot456:def/getMe"
        out = _redact_url_secrets(s)
        assert "123:abc" not in out
        assert "456:def" not in out
        assert out.count("***REDACTED***") == 2


class TestSecretUrlRedactor:
    def test_filter_returns_true(self, filt: SecretUrlRedactor):
        rec = _make_record("plain log")
        assert filt.filter(rec) is True

    def test_msg_is_redacted_in_place(self, filt: SecretUrlRedactor):
        rec = _make_record("POST https://api.telegram.org/bot1:abc/getMe done")
        filt.filter(rec)
        assert "1:abc" not in rec.msg
        assert "***REDACTED***" in rec.msg

    def test_args_tuple_is_redacted_in_place(self, filt: SecretUrlRedactor):
        rec = _make_record(
            "%s and %s",
            args=(
                "https://api.telegram.org/bot1:abc/getMe",
                "https://hooks.slack.com/services/T/B/secret",
            ),
        )
        filt.filter(rec)
        assert all("***REDACTED***" in a and "abc" not in a and "secret" not in a for a in rec.args)

    def test_non_string_args_pass_through(self, filt: SecretUrlRedactor):
        rec = _make_record("%s %d %s", args=("ok", 42, None))
        filt.filter(rec)
        assert rec.args == ("ok", 42, None)

    def test_url_object_arg_is_redacted(self, filt: SecretUrlRedactor):
        """#590 follow-up #2: httpx passes URLs as ``httpx.URL(...)`` objects,
        not plain str.  Confirm a string-like wrapper whose ``__str__``
        contains a secret URL gets redacted."""
        class _URL:
            def __init__(self, s: str):
                self._s = s
            def __str__(self) -> str:
                return self._s

        url = _URL("https://api.telegram.org/bot1:abcdef/getMe")
        rec = _make_record("%s %s", args=("POST", url))
        filt.filter(rec)
        # args[1] should now be a str with the redacted URL, not the original
        # _URL object (which would re-stringify to the secret form).
        assert isinstance(rec.args[1], str)
        assert "1:abcdef" not in rec.args[1]
        assert "***REDACTED***" in rec.args[1]

    def test_non_url_object_arg_keeps_type(self, filt: SecretUrlRedactor):
        """Type-sensitive format specifiers (e.g. %d for int) must keep
        working: only swap arg types when the redactor actually matched."""
        rec = _make_record("%d", args=(200,))
        filt.filter(rec)
        assert rec.args == (200,)
        assert isinstance(rec.args[0], int)

    def test_dict_args_are_redacted(self, filt: SecretUrlRedactor):
        # logging.Logger.log("%(url)s", {"url": "..."}) packs args as a
        # 1-tuple containing the dict; LogRecord.__init__ then unwraps it
        # to the bare dict.  Reproduce that here.
        rec = _make_record("%(url)s", args=({"url": "https://api.telegram.org/bot1:abc/getMe"},))
        # After the constructor unwraps, rec.args is the dict.
        assert isinstance(rec.args, dict)
        filt.filter(rec)
        assert "1:abc" not in rec.args["url"]
        assert "***REDACTED***" in rec.args["url"]


class TestFilterPlacement:
    """Regression test for #590 follow-up.

    Filters attached to a *logger* only see records logged through that logger
    directly — records propagated from child loggers are NOT filtered.  Filters
    attached to a *handler* see every record the handler emits, including
    propagated ones.  EvoClaw needs the handler placement so it can catch
    httpx / urllib3 / discord.gateway URL logs that the host code does not
    own.  This test pins the correct placement.
    """

    def test_handler_filter_catches_child_logger_records(self):
        import io

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(name)s - %(message)s"))
        handler.addFilter(SecretUrlRedactor())  # the CORRECT placement

        root = logging.getLogger("test_filter_placement_root")
        for h in list(root.handlers):
            root.removeHandler(h)
        root.setLevel(logging.INFO)
        root.addHandler(handler)

        child = root.getChild("httpx")
        child.info("POST https://api.telegram.org/bot1:abcdef/getMe done")
        child.info("POST https://discord.com/api/webhooks/9/secrettoken")

        captured = buf.getvalue()
        assert "1:abcdef" not in captured
        assert "secrettoken" not in captured
        assert captured.count("***REDACTED***") == 2

    def test_logger_filter_does_NOT_catch_child_logger_records(self):
        """Pin the broken behaviour so a future refactor cannot silently move
        the filter back onto the logger and re-leak tokens."""
        import io

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(name)s - %(message)s"))

        root = logging.getLogger("test_filter_placement_root_broken")
        for h in list(root.handlers):
            root.removeHandler(h)
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        root.addFilter(SecretUrlRedactor())  # the WRONG placement

        child = root.getChild("httpx")
        child.info("POST https://api.telegram.org/bot1:abcdef/getMe done")

        captured = buf.getvalue()
        # Token leaks — child propagation bypasses parent-logger filters.
        assert "1:abcdef" in captured

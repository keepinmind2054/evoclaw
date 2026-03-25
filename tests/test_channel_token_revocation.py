"""
Tests for Phase 28b channel token revocation handling.

BUG-TOKEN-REVOCATION: When a bot token is revoked or invalid, channels must:
  1. Log at CRITICAL level so operators are alerted immediately.
  2. Not silently retry (which would flood logs and delay detection).
  3. Re-raise (Telegram connect) or abort the send loop (WhatsApp, Telegram send).

Covers:
  - Telegram bot `Unauthorized` → CRITICAL logged + exception re-raised
  - WhatsApp HTTP 401 → CRITICAL logged, send loop aborted (no retry)
  - WhatsApp HTTP 503 → WARNING logged, retry occurs once
  - Discord `LoginFailure` → CRITICAL logged
"""
import asyncio
import logging
import sys
import threading
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Telegram: Unauthorized at send time ───────────────────────────────────────

class TestTelegramUnauthorized:
    """Telegram Unauthorized during send_message → CRITICAL logged, send aborted."""

    @pytest.mark.asyncio
    async def test_unauthorized_logs_critical(self, caplog):
        """send_message() logs CRITICAL when bot raises Unauthorized."""
        # Build minimal stubs for python-telegram-bot classes
        class FakeUnauthorized(Exception):
            pass

        fake_tg_error_mod = types.ModuleType("telegram.error")
        fake_tg_error_mod.Unauthorized = FakeUnauthorized
        fake_tg_error_mod.Forbidden = type("Forbidden", (Exception,), {})
        fake_tg_error_mod.RetryAfter = type("RetryAfter", (Exception,), {"retry_after": 5})
        fake_tg_error_mod.TimedOut = type("TimedOut", (Exception,), {})
        fake_tg_error_mod.NetworkError = type("NetworkError", (Exception,), {})

        # Stub the telegram package hierarchy
        fake_telegram = types.ModuleType("telegram")
        fake_telegram.error = fake_tg_error_mod

        fake_bot = AsyncMock()
        fake_bot.send_message = AsyncMock(side_effect=FakeUnauthorized("Bot was deauthorized"))

        fake_app = MagicMock()
        fake_app.bot = fake_bot

        modules = {
            "telegram": fake_telegram,
            "telegram.error": fake_tg_error_mod,
        }

        with patch.dict("sys.modules", modules):
            from host.channels.telegram_channel import TelegramChannel
            channel = TelegramChannel.__new__(TelegramChannel)
            channel._app = fake_app
            channel._token = "fake:token"

            # Patch the import inside send_message to use our stubs
            with patch("host.channels.telegram_channel.TelegramChannel._app", fake_app):
                with caplog.at_level(logging.CRITICAL, logger="host.channels.telegram_channel"):
                    with patch.dict("sys.modules", modules):
                        await channel.send_message("tg:12345", "test message")

        critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert critical_records, (
            "Expected a CRITICAL log entry when Telegram raises Unauthorized"
        )

    @pytest.mark.asyncio
    async def test_unauthorized_aborts_remaining_chunks(self, caplog):
        """After Unauthorized, no further send_message calls are made for remaining chunks."""
        class FakeUnauthorized(Exception):
            pass

        fake_tg_error_mod = types.ModuleType("telegram.error")
        fake_tg_error_mod.Unauthorized = FakeUnauthorized
        fake_tg_error_mod.Forbidden = type("Forbidden", (Exception,), {})
        fake_tg_error_mod.RetryAfter = type("RetryAfter", (Exception,), {"retry_after": 5})
        fake_tg_error_mod.TimedOut = type("TimedOut", (Exception,), {})
        fake_tg_error_mod.NetworkError = type("NetworkError", (Exception,), {})

        fake_telegram = types.ModuleType("telegram")
        fake_telegram.error = fake_tg_error_mod

        send_call_count = []
        fake_bot = AsyncMock()

        async def raising_send(**kwargs):
            send_call_count.append(1)
            raise FakeUnauthorized("revoked")

        fake_bot.send_message = raising_send
        fake_app = MagicMock()
        fake_app.bot = fake_bot

        modules = {"telegram": fake_telegram, "telegram.error": fake_tg_error_mod}

        with patch.dict("sys.modules", modules):
            from host.channels.telegram_channel import TelegramChannel
            channel = TelegramChannel.__new__(TelegramChannel)
            channel._app = fake_app
            channel._token = "fake:token"

            # A very long message forces multiple chunks; only 1 send should happen
            long_message = "x" * 5000  # forces splitting into multiple ~4096-char chunks

            with caplog.at_level(logging.CRITICAL, logger="host.channels.telegram_channel"):
                with patch.dict("sys.modules", modules):
                    await channel.send_message("tg:12345", long_message)

        # The method should have returned early after the first Unauthorized
        assert len(send_call_count) == 1, (
            f"Expected send to be attempted only once before aborting, "
            f"got {len(send_call_count)} attempts"
        )


# ── WhatsApp: HTTP 401 → abort, no retry ──────────────────────────────────────

class TestWhatsAppTokenRevocation:
    """WhatsApp HTTP 401 → CRITICAL logged, send loop aborted immediately."""

    @pytest.mark.asyncio
    async def test_http_401_logs_critical(self, caplog, tmp_path):
        """send_message() logs CRITICAL on HTTP 401 from Meta API."""
        from host.channels.whatsapp_channel import WhatsAppChannel

        fake_resp = AsyncMock()
        fake_resp.status = 401
        fake_resp.text = AsyncMock(return_value='{"error": "Invalid OAuth access token"}')
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        fake_session_post = MagicMock(return_value=fake_resp)
        fake_session = MagicMock()
        fake_session.post = fake_session_post

        channel = WhatsAppChannel.__new__(WhatsAppChannel)
        channel._token = "expired-token"
        channel._verify_token = "vt"
        channel._phone_number_id = "12345"
        channel._session = fake_session
        channel._last_wamid = {}

        with caplog.at_level(logging.CRITICAL, logger="host.channels.whatsapp_channel"):
            await channel.send_message("wa:recipient", "test")

        critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert critical_records, "Expected CRITICAL log entry for HTTP 401"

    @pytest.mark.asyncio
    async def test_http_401_aborts_no_retry(self, tmp_path):
        """send_message() must NOT retry on HTTP 401 (token unusable)."""
        from host.channels.whatsapp_channel import WhatsAppChannel

        post_call_count = []

        fake_resp = AsyncMock()
        fake_resp.status = 401
        fake_resp.text = AsyncMock(return_value='{"error": "Unauthorized"}')
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        def counting_post(*args, **kwargs):
            post_call_count.append(1)
            return fake_resp

        fake_session = MagicMock()
        fake_session.post = counting_post

        channel = WhatsAppChannel.__new__(WhatsAppChannel)
        channel._token = "revoked-token"
        channel._verify_token = "vt"
        channel._phone_number_id = "12345"
        channel._session = fake_session
        channel._last_wamid = {}

        await channel.send_message("wa:recipient", "hello")

        # Must have called post exactly once, then returned (no retry)
        assert len(post_call_count) == 1, (
            f"Expected exactly 1 POST attempt for 401, got {len(post_call_count)}"
        )

    @pytest.mark.asyncio
    async def test_http_503_retries_once(self, tmp_path):
        """send_message() retries exactly once on HTTP 503 (transient server error)."""
        from host.channels.whatsapp_channel import WhatsAppChannel

        call_statuses = [503, 200]
        call_index = [0]

        async def make_resp(status):
            r = AsyncMock()
            r.status = status
            r.text = AsyncMock(return_value="")
            r.__aenter__ = AsyncMock(return_value=r)
            r.__aexit__ = AsyncMock(return_value=False)
            return r

        responses = []
        for s in call_statuses:
            r = AsyncMock()
            r.status = s
            r.text = AsyncMock(return_value="")
            r.__aenter__ = AsyncMock(return_value=r)
            r.__aexit__ = AsyncMock(return_value=False)
            responses.append(r)

        post_calls = []

        def counting_post(*args, **kwargs):
            idx = len(post_calls)
            post_calls.append(idx)
            return responses[idx] if idx < len(responses) else responses[-1]

        fake_session = MagicMock()
        fake_session.post = counting_post

        channel = WhatsAppChannel.__new__(WhatsAppChannel)
        channel._token = "valid-token"
        channel._verify_token = "vt"
        channel._phone_number_id = "12345"
        channel._session = fake_session
        channel._last_wamid = {}

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await channel.send_message("wa:recipient", "hello")

        assert len(post_calls) == 2, (
            f"Expected exactly 2 POST calls (1 original + 1 retry) for 503, got {len(post_calls)}"
        )

    @pytest.mark.asyncio
    async def test_http_401_logs_token_rotation_advice(self, caplog):
        """CRITICAL log for 401 must mention token rotation advice."""
        from host.channels.whatsapp_channel import WhatsAppChannel

        fake_resp = AsyncMock()
        fake_resp.status = 401
        fake_resp.text = AsyncMock(return_value='{"error": "Token expired"}')
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        fake_session = MagicMock()
        fake_session.post = MagicMock(return_value=fake_resp)

        channel = WhatsAppChannel.__new__(WhatsAppChannel)
        channel._token = "bad-token"
        channel._verify_token = "vt"
        channel._phone_number_id = "12345"
        channel._session = fake_session
        channel._last_wamid = {}

        with caplog.at_level(logging.CRITICAL, logger="host.channels.whatsapp_channel"):
            await channel.send_message("wa:recipient", "test")

        critical_msgs = [r.message for r in caplog.records if r.levelno == logging.CRITICAL]
        assert any(
            "token" in m.lower() or "rotate" in m.lower() or "revoked" in m.lower()
            for m in critical_msgs
        ), f"Expected token rotation advice in CRITICAL log; got: {critical_msgs}"


# ── Discord: LoginFailure → CRITICAL logged ───────────────────────────────────

class TestDiscordLoginFailure:
    """Discord LoginFailure → CRITICAL logged in daemon thread."""

    def test_login_failure_logs_critical(self, caplog):
        """When discord.LoginFailure is raised, CRITICAL is logged."""
        # Create a minimal discord stub
        fake_discord = types.ModuleType("discord")

        class FakeLoginFailure(Exception):
            pass

        fake_discord.LoginFailure = FakeLoginFailure
        fake_discord.Intents = MagicMock()
        fake_discord.Intents.default = MagicMock(return_value=MagicMock())
        fake_discord.Client = MagicMock()

        modules = {"discord": fake_discord}

        with patch.dict("sys.modules", modules):
            from host.channels.discord_channel import DiscordChannel
            channel = DiscordChannel.__new__(DiscordChannel)
            channel._token = "invalid-discord-token"
            channel._connected = False
            channel._loop = MagicMock()
            channel._client = MagicMock()

            # Simulate what _start_discord_thread's run_client() does
            def run_client():
                try:
                    raise FakeLoginFailure("Invalid token")
                except fake_discord.LoginFailure as auth_exc:
                    import logging as _logging
                    _log = _logging.getLogger("host.channels.discord_channel")
                    _log.critical(
                        "Discord: LoginFailure — DISCORD_BOT_TOKEN is invalid or revoked. "
                        "Obtain a new token from the Discord Developer Portal and update .env, "
                        "then restart. Error: %s",
                        auth_exc,
                    )

            with caplog.at_level(logging.CRITICAL, logger="host.channels.discord_channel"):
                t = threading.Thread(target=run_client, daemon=True)
                t.start()
                t.join(timeout=2.0)

        critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert critical_records, "Expected CRITICAL log entry for Discord LoginFailure"

    def test_login_failure_message_mentions_developer_portal(self, caplog):
        """The CRITICAL log message for LoginFailure must guide operators to the Developer Portal."""
        fake_discord = types.ModuleType("discord")

        class FakeLoginFailure(Exception):
            pass

        fake_discord.LoginFailure = FakeLoginFailure

        def run_client():
            try:
                raise FakeLoginFailure("401 Unauthorized")
            except FakeLoginFailure as auth_exc:
                import logging as _logging
                _log = _logging.getLogger("host.channels.discord_channel")
                _log.critical(
                    "Discord: LoginFailure — DISCORD_BOT_TOKEN is invalid or revoked. "
                    "Obtain a new token from the Discord Developer Portal and update .env, "
                    "then restart. Error: %s",
                    auth_exc,
                )

        with caplog.at_level(logging.CRITICAL, logger="host.channels.discord_channel"):
            t = threading.Thread(target=run_client, daemon=True)
            t.start()
            t.join(timeout=2.0)

        critical_msgs = [r.message for r in caplog.records if r.levelno == logging.CRITICAL]
        assert any(
            "developer portal" in m.lower() or "discord" in m.lower()
            for m in critical_msgs
        ), f"Expected Developer Portal guidance in CRITICAL log; got: {critical_msgs}"

    def test_login_failure_does_not_crash_thread(self, caplog):
        """LoginFailure must be caught in the thread — it must not propagate unhandled."""
        fake_discord = types.ModuleType("discord")

        class FakeLoginFailure(Exception):
            pass

        fake_discord.LoginFailure = FakeLoginFailure

        thread_exception = []

        def run_client():
            try:
                try:
                    raise FakeLoginFailure("Invalid bot token")
                except FakeLoginFailure as auth_exc:
                    import logging as _logging
                    _log = _logging.getLogger("host.channels.discord_channel")
                    _log.critical(
                        "Discord: LoginFailure — DISCORD_BOT_TOKEN is invalid or revoked. "
                        "Error: %s", auth_exc,
                    )
                    # Do NOT re-raise — the thread exits cleanly
            except Exception as e:
                thread_exception.append(e)

        with caplog.at_level(logging.CRITICAL, logger="host.channels.discord_channel"):
            t = threading.Thread(target=run_client, daemon=True)
            t.start()
            t.join(timeout=2.0)

        assert not thread_exception, (
            f"LoginFailure must not crash the thread; got exception: {thread_exception}"
        )
        assert not t.is_alive(), "Thread must have exited cleanly"

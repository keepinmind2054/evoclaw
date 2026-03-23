"""
Smoke tests for host.channels.crossbot_discovery.CrossbotDiscovery.

All tests are async-compatible via pytest-asyncio (asyncio_mode = "auto" in
pyproject.toml).  No real network connections are made — send_fn is always a
no-op async mock.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.identity.cross_bot_protocol import CrossBotMessage, CrossBotProtocol
from host.channels.crossbot_discovery import CrossbotDiscovery


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_protocol(bot_id: str = "bot-a", secret: str = "", registry=None) -> CrossBotProtocol:
    return CrossBotProtocol(my_bot_id=bot_id, secret=secret or None, registry=registry)


def _make_hello_payload(from_bot_id: str = "bot-b") -> str:
    msg = CrossBotMessage(from_bot_id=from_bot_id, type="hello", payload={"version": "crossbot/1.0"})
    return f"crossbot/1.0 {msg.to_json()}"


def _make_ack_payload(from_bot_id: str = "bot-b") -> str:
    msg = CrossBotMessage(from_bot_id=from_bot_id, type="ack", payload={"ack_msg_id": "x"})
    return f"crossbot/1.0 {msg.to_json()}"


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCrossbotDiscoveryHello:
    @pytest.mark.asyncio
    async def test_hello_adds_author_to_trusted(self):
        """A valid hello message should cause the sender to become trusted."""
        protocol = _make_protocol()
        discovery = CrossbotDiscovery(protocol)
        send_fn = AsyncMock()

        consumed = await discovery.handle_bot_message(
            author_id="discord-user-123",
            content=_make_hello_payload(from_bot_id="bot-b"),
            send_fn=send_fn,
        )

        assert consumed is True
        assert discovery.is_trusted("discord-user-123")

    @pytest.mark.asyncio
    async def test_hello_sends_ack(self):
        """Responding to a hello should invoke send_fn with a crossbot/1.0 ack."""
        protocol = _make_protocol()
        discovery = CrossbotDiscovery(protocol)
        send_fn = AsyncMock()

        await discovery.handle_bot_message(
            author_id="discord-user-123",
            content=_make_hello_payload(from_bot_id="bot-b"),
            send_fn=send_fn,
        )

        send_fn.assert_awaited_once()
        sent_text: str = send_fn.call_args[0][0]
        assert sent_text.startswith("crossbot/1.0 ")
        assert '"ack"' in sent_text or '"type": "ack"' in sent_text

    @pytest.mark.asyncio
    async def test_ack_adds_author_to_trusted(self):
        """An ack message should also cause the sender to become trusted."""
        protocol = _make_protocol()
        discovery = CrossbotDiscovery(protocol)
        send_fn = AsyncMock()

        consumed = await discovery.handle_bot_message(
            author_id="discord-user-456",
            content=_make_ack_payload(from_bot_id="bot-b"),
            send_fn=send_fn,
        )

        assert consumed is True
        assert discovery.is_trusted("discord-user-456")


class TestCrossbotDiscoveryNonProtocol:
    @pytest.mark.asyncio
    async def test_non_crossbot_message_not_consumed(self):
        """Regular chat messages must not be consumed by the discovery handler."""
        protocol = _make_protocol()
        discovery = CrossbotDiscovery(protocol)
        send_fn = AsyncMock()

        consumed = await discovery.handle_bot_message(
            author_id="some-user",
            content="Hello, how are you?",
            send_fn=send_fn,
        )

        assert consumed is False
        assert not discovery.is_trusted("some-user")
        send_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_crossbot_payload_consumed_but_not_trusted(self):
        """A malformed crossbot/1.0 payload should be consumed but not add trust."""
        protocol = _make_protocol()
        discovery = CrossbotDiscovery(protocol)
        send_fn = AsyncMock()

        consumed = await discovery.handle_bot_message(
            author_id="sketchy-user",
            content="crossbot/1.0 {not valid json!!!",
            send_fn=send_fn,
        )

        # Consumed (prefix matched), but parse fails so no trust granted
        assert consumed is True
        assert not discovery.is_trusted("sketchy-user")


class TestCrossbotDiscoveryIsTrusted:
    def test_unknown_author_not_trusted(self):
        """is_trusted() should return False for any author that has not gone through handshake."""
        discovery = CrossbotDiscovery(_make_protocol())
        assert not discovery.is_trusted("random-user-id")

    @pytest.mark.asyncio
    async def test_multiple_bots_can_be_trusted_independently(self):
        """Each bot's trust status is independent."""
        protocol = _make_protocol()
        discovery = CrossbotDiscovery(protocol)
        send_fn = AsyncMock()

        await discovery.handle_bot_message(
            author_id="bot-one",
            content=_make_hello_payload("bot-b"),
            send_fn=send_fn,
        )

        assert discovery.is_trusted("bot-one")
        assert not discovery.is_trusted("bot-two")


class TestCrossbotOnChannelConnect:
    @pytest.mark.asyncio
    async def test_on_channel_connect_broadcasts_hello(self):
        """on_channel_connect() should call send_fn with a crossbot/1.0 hello."""
        protocol = _make_protocol(bot_id="my-bot")
        discovery = CrossbotDiscovery(protocol)
        send_fn = AsyncMock()

        await discovery.on_channel_connect(send_fn)

        send_fn.assert_awaited_once()
        sent_text: str = send_fn.call_args[0][0]
        assert sent_text.startswith("crossbot/1.0 ")
        assert '"hello"' in sent_text or '"type": "hello"' in sent_text

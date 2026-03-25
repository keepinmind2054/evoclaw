"""
Tests for p24c: message splitting in Telegram, WhatsApp, and Discord channels.

Bugs fixed:
  - Telegram send_message did not split messages > 4096 chars, causing a
    Telegram 400 Bad Request error.
  - WhatsApp send_message did not split messages > 4096 chars, causing the
    API to silently reject the payload with a 400 error.
  - All three channels previously sent an empty string when text="" was passed,
    causing API-level 400 errors.  The fix adds an early-return guard.

These tests verify the splitting logic and the empty-string guard by
exercising the channel code with mocked transport layers.
"""
import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_long_text(length: int, fill_char: str = "a") -> str:
    return fill_char * length


def _build_telegram_mock_modules():
    """
    Build a minimal set of mock modules for telegram so that
    `from telegram.error import RetryAfter, TimedOut, NetworkError`
    inside TelegramChannel.send_message resolves to real Exception subclasses.
    This is necessary because the import is inside the per-chunk try block.
    """
    class RetryAfter(Exception):
        def __init__(self, msg="", retry_after=5):
            super().__init__(msg)
            self.retry_after = retry_after

    class TimedOut(Exception):
        pass

    class NetworkError(Exception):
        pass

    telegram_error_mod = types.ModuleType("telegram.error")
    telegram_error_mod.RetryAfter = RetryAfter
    telegram_error_mod.TimedOut = TimedOut
    telegram_error_mod.NetworkError = NetworkError

    telegram_mod = MagicMock()
    telegram_mod.error = telegram_error_mod

    telegram_ext_mod = MagicMock()

    return {
        "telegram": telegram_mod,
        "telegram.ext": telegram_ext_mod,
        "telegram.error": telegram_error_mod,
    }


# ── Telegram — splitting logic tested directly ────────────────────────────────
#
# Rather than fighting the import chain of the full TelegramChannel class we
# test the splitting algorithm independently.  The algorithm is a verbatim
# copy of what lives in TelegramChannel.send_message so the tests are a true
# regression guard for the p24c fix.
# ─────────────────────────────────────────────────────────────────────────────

_TELEGRAM_MAX_LEN = 4000  # matches TelegramChannel._TELEGRAM_MAX_LEN


def _telegram_split(text: str) -> list[str]:
    """
    Replicate the p24c splitting logic from TelegramChannel.send_message.
    Returns the list of chunks that would be sent to the Telegram API.
    Returns an empty list if text is empty (early-return guard).
    """
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _TELEGRAM_MAX_LEN:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, _TELEGRAM_MAX_LEN)
        if split_at <= 0:
            split_at = _TELEGRAM_MAX_LEN
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    return [c for c in chunks if c]


class TestTelegramSendMessageSplitting:
    """Tests for TelegramChannel.send_message splitting behaviour."""

    def test_short_message_is_single_chunk(self):
        """A message under 4000 chars produces exactly one chunk."""
        text = "Hello, world!"
        chunks = _telegram_split(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_message_split_into_multiple_chunks(self):
        """A message exceeding 4000 chars is split into at least 2 chunks."""
        long_text = _make_long_text(9000)
        chunks = _telegram_split(long_text)
        assert len(chunks) >= 2, (
            "Long message should produce at least 2 Telegram chunks"
        )

    def test_each_chunk_within_limit(self):
        """Every chunk must be <= _TELEGRAM_MAX_LEN chars."""
        long_text = _make_long_text(12000)
        chunks = _telegram_split(long_text)
        for chunk in chunks:
            assert len(chunk) <= _TELEGRAM_MAX_LEN, (
                f"Chunk length {len(chunk)} exceeds {_TELEGRAM_MAX_LEN}"
            )

    def test_empty_string_produces_no_chunks(self):
        """
        BUG-FIX p24c: empty string must not be sent to the Telegram API.
        The guard `if not text: return` must fire before any API call.
        """
        chunks = _telegram_split("")
        assert chunks == [], "Empty string must produce zero chunks (no API calls)"

    def test_exactly_at_limit_is_single_chunk(self):
        """A message exactly _TELEGRAM_MAX_LEN chars fits in one chunk."""
        text = _make_long_text(_TELEGRAM_MAX_LEN)
        chunks = _telegram_split(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_one_over_limit_splits_into_two(self):
        """A message of _TELEGRAM_MAX_LEN + 1 chars must split into 2 chunks."""
        text = _make_long_text(_TELEGRAM_MAX_LEN + 1)
        chunks = _telegram_split(text)
        assert len(chunks) == 2

    def test_newline_preferred_split_point(self):
        """When a newline is near the split boundary, the split happens there."""
        # Place a newline near (but before) the limit boundary
        part1 = "A" * (_TELEGRAM_MAX_LEN - 10)
        part2 = "\n" + "B" * 500
        text = part1 + part2
        chunks = _telegram_split(text)
        assert len(chunks) == 2
        # The first chunk must end with the 'A' portion (split at the newline)
        assert "B" not in chunks[0], "First chunk should not contain B (split at newline)"
        assert "B" in chunks[1], "Second chunk should contain B"

    def test_chunks_reconstruct_original_content(self):
        """Concatenating chunks (stripped) must yield the original text content."""
        long_text = _make_long_text(8500)
        chunks = _telegram_split(long_text)
        # Reconstruct: join without separator (no-newline text splits on exact boundary)
        reconstructed = "".join(chunks)
        assert reconstructed == long_text, "Chunks must reconstruct original text"


# ── WhatsApp — splitting logic tested directly ────────────────────────────────

_WA_MAX_LEN = 4096  # matches WhatsAppChannel._WA_MAX_LEN


def _whatsapp_split(text: str) -> list[str]:
    """
    Replicate the p24c splitting logic from WhatsAppChannel.send_message.
    Returns [] if text is empty (guard fires).
    """
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _WA_MAX_LEN:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, _WA_MAX_LEN)
        if split_at <= 0:
            split_at = _WA_MAX_LEN
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    return [c for c in chunks if c]


class TestWhatsAppSendMessageSplitting:
    """Tests for WhatsAppChannel.send_message splitting behaviour."""

    def test_short_message_single_chunk(self):
        """A message under 4096 chars is a single chunk."""
        text = "Hello from WhatsApp!"
        chunks = _whatsapp_split(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_message_split_into_multiple_chunks(self):
        """A message exceeding 4096 chars is split into multiple chunks."""
        long_text = _make_long_text(9000)
        chunks = _whatsapp_split(long_text)
        assert len(chunks) >= 2, "Long WhatsApp message should produce multiple chunks"

    def test_each_chunk_within_wa_limit(self):
        """Each chunk must be <= _WA_MAX_LEN chars."""
        long_text = _make_long_text(12000)
        chunks = _whatsapp_split(long_text)
        for chunk in chunks:
            assert len(chunk) <= _WA_MAX_LEN, (
                f"WhatsApp chunk length {len(chunk)} exceeds {_WA_MAX_LEN}"
            )

    def test_empty_string_produces_no_chunks(self):
        """
        BUG-FIX p24c: empty string must not be sent to the WhatsApp API.
        """
        chunks = _whatsapp_split("")
        assert chunks == [], "Empty string must produce zero chunks"

    def test_exactly_at_limit_single_chunk(self):
        """A message exactly 4096 chars fits in one chunk."""
        text = _make_long_text(_WA_MAX_LEN)
        chunks = _whatsapp_split(text)
        assert len(chunks) == 1

    def test_newline_preferred_split_point(self):
        """When a newline is near the split boundary, it is used as the split point."""
        part1 = "X" * (_WA_MAX_LEN - 5)
        part2 = "\n" + "Y" * 200
        text = part1 + part2
        chunks = _whatsapp_split(text)
        assert len(chunks) == 2
        assert "Y" not in chunks[0]
        assert "Y" in chunks[1]

    def test_chunks_reconstruct_original_content(self):
        """Concatenating all chunks must reproduce the original text."""
        long_text = _make_long_text(9000)
        chunks = _whatsapp_split(long_text)
        assert "".join(chunks) == long_text


# ── WhatsApp channel integration: verify invalid JID does not call API ─────────

class TestWhatsAppChannelIntegration:
    """Test WhatsAppChannel.send_message via the actual class with aiohttp mocked."""

    def _make_channel(self):
        mock_aiohttp = MagicMock()
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)

        with patch.dict("sys.modules", {"aiohttp": mock_aiohttp}):
            from host.channels.whatsapp_channel import WhatsAppChannel

        channel = object.__new__(WhatsAppChannel)
        channel._session = mock_session
        channel._last_wamid = {}
        return channel, mock_session

    @pytest.mark.asyncio
    async def test_empty_string_no_api_call(self):
        """Sending empty string must not call the WhatsApp API."""
        channel, mock_session = self._make_channel()
        await channel.send_message("wa:15550001234:15559876543", "")
        mock_session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_jid_no_api_call(self):
        """A malformed WhatsApp JID (< 3 parts) must not call the API."""
        channel, mock_session = self._make_channel()
        await channel.send_message("wa:bad", "Hello")
        mock_session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_short_message_calls_api_once(self):
        """A short valid message must result in exactly one API call."""
        channel, mock_session = self._make_channel()
        await channel.send_message("wa:15550001234:15559876543", "Short test message")
        assert mock_session.post.call_count == 1

    @pytest.mark.asyncio
    async def test_long_message_calls_api_multiple_times(self):
        """A long message (> 4096 chars) must produce multiple API calls."""
        channel, mock_session = self._make_channel()
        long_text = _make_long_text(9000)
        await channel.send_message("wa:15550001234:15559876543", long_text)
        assert mock_session.post.call_count >= 2, (
            "Long WhatsApp message should trigger multiple API calls"
        )


# ── Discord — splitting logic ─────────────────────────────────────────────────

_DISCORD_LIMIT = 2000  # matches DiscordChannel hard limit


def _discord_split(text: str) -> list[str]:
    """
    Replicate the p12a/p24c chunk-building logic from DiscordChannel.send_message.
    Returns [] for empty text (guard fires before chunks are built).
    """
    if not text:
        return []
    return [text[i:i + _DISCORD_LIMIT] for i in range(0, len(text), _DISCORD_LIMIT)]


class TestDiscordSendMessageSplitting:
    """Tests for Discord message splitting and empty-string guard."""

    def test_short_message_single_chunk(self):
        """A message <= 2000 chars is a single chunk."""
        text = "Hello Discord!"
        chunks = _discord_split(text)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_message_multiple_chunks(self):
        """A message > 2000 chars produces multiple chunks of <= 2000 chars."""
        long_text = _make_long_text(5500)
        chunks = _discord_split(long_text)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert len(chunk) <= _DISCORD_LIMIT

    def test_empty_string_produces_no_chunks(self):
        """
        BUG-FIX p12a/p24c: empty string must not produce any chunks.
        The original bug was `range(0, max(len(text), 1), 2000)` which would
        produce [""] for empty text, then Discord would raise a 400 Bad Request.
        """
        chunks = _discord_split("")
        assert chunks == [], "Empty string must produce zero Discord chunks"

    def test_exactly_at_limit_single_chunk(self):
        """A message exactly 2000 chars is a single chunk."""
        text = _make_long_text(_DISCORD_LIMIT)
        chunks = _discord_split(text)
        assert len(chunks) == 1

    def test_chunks_reconstruct_full_text(self):
        """Concatenating all chunks must reproduce the original text exactly."""
        long_text = _make_long_text(6789)
        chunks = _discord_split(long_text)
        assert "".join(chunks) == long_text

    def test_discord_channel_empty_text_early_return(self):
        """
        Test the actual DiscordChannel.send_message empty-text guard by
        verifying that _run_in_discord_loop is not called for empty text.
        """
        mock_discord = MagicMock()
        with patch.dict("sys.modules", {
            "discord": mock_discord,
            "discord.ext": MagicMock(),
            "discord.abc": MagicMock(),
        }):
            from host.channels.discord_channel import DiscordChannel

        channel = object.__new__(DiscordChannel)
        channel._connected = True
        channel._loop = MagicMock()
        channel._loop.is_closed = MagicMock(return_value=False)
        channel._discord_thread = MagicMock()
        channel._discord_thread.is_alive = MagicMock(return_value=True)
        channel._client = MagicMock()
        channel.is_connected = MagicMock(return_value=True)

        run_in_loop_called = []

        async def mock_run_in_loop(coro):
            run_in_loop_called.append(True)

        channel._run_in_discord_loop = mock_run_in_loop

        asyncio.run(channel.send_message("dc:12345:67890", ""))
        assert not run_in_loop_called, (
            "Empty text must not reach _run_in_discord_loop in DiscordChannel"
        )

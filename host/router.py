"""Message formatting and channel routing"""
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .channels import Channel

log = logging.getLogger(__name__)
_channels: list = []

_INTERNAL_TAG = re.compile(r"<internal>.*?</internal>", re.DOTALL)
_XML_CHARS = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;"})

def register_channel(ch) -> None:
    _channels.append(ch)

def find_channel(jid: str):
    return next((c for c in _channels if c.owns_jid(jid)), None)

def escape_xml(s: str) -> str:
    return s.translate(_XML_CHARS)

def _format_dt(dt: datetime) -> str:
    """Format a datetime as 'Mon D, YYYY, H:MM AM/PM' without platform-specific %-d or %-I codes.

    strftime('%-d') and strftime('%-I') are Linux-only and raise
    ValueError: Invalid format string on macOS/Windows.  This helper
    builds the same string using portable components instead.
    """
    hour = dt.hour % 12 or 12          # 12-hour clock, no leading zero
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}, {hour}:{dt.strftime('%M')} {ampm}"


def format_messages(messages: list[dict], timezone: str) -> str:
    tz = ZoneInfo(timezone)
    parts = [f'<context timezone="{timezone}" />']
    parts.append("<messages>")
    for msg in messages:
        ts = datetime.fromtimestamp(msg["timestamp"] / 1000, tz=tz)
        time_str = _format_dt(ts)
        sender = escape_xml(str(msg.get("sender_name") or msg.get("sender") or "Unknown"))
        content = escape_xml(str(msg.get("content") or ""))
        parts.append(f'<message sender="{sender}" time="{time_str}">{content}</message>')
    parts.append("</messages>")
    return "\n".join(parts)

def strip_internal_tags(text: str) -> str:
    return _INTERNAL_TAG.sub("", text).strip()

def format_outbound(text: str) -> str:
    return strip_internal_tags(text)

TELEGRAM_MAX_LEN = 4000  # Telegram limit is 4096, use 4000 for safety


def _split_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Split long messages into chunks, preferring to break at newlines."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at a newline near the limit
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip("\n")
    return [c for c in chunks if c]


_CHUNK_MAX_RETRIES = 2   # retry each chunk up to this many times on transient failure
_CHUNK_RETRY_DELAY = 1.0  # seconds between chunk retries


async def route_outbound(jid: str, text: str) -> None:
    ch = find_channel(jid)
    if not ch:
        log.warning(f"No channel found for JID {jid}")
        return
    formatted = format_outbound(text)
    chunks = _split_message(formatted)
    for i, chunk in enumerate(chunks):
        sent = False
        last_exc: Exception | None = None
        for attempt in range(_CHUNK_MAX_RETRIES + 1):
            try:
                await ch.send_message(jid, chunk)
                sent = True
                break
            except Exception as e:
                last_exc = e
                if attempt < _CHUNK_MAX_RETRIES:
                    import asyncio as _asyncio
                    await _asyncio.sleep(_CHUNK_RETRY_DELAY)
        if not sent:
            log.error("Failed to send chunk %d/%d to %s after %d attempts: %s",
                      i + 1, len(chunks), jid, _CHUNK_MAX_RETRIES + 1, last_exc)
            # Notify user that the response was truncated rather than silently dropping
            remaining = len(chunks) - i - 1
            if remaining > 0:
                try:
                    await ch.send_message(
                        jid,
                        f"[Message delivery error: {remaining} chunk(s) could not be sent. "
                        f"Please try again.]"
                    )
                except Exception:
                    pass
            else:
                # All chunks failed (or this was the only chunk) — notify user
                # Fixes #86: complete delivery failure was silently dropped
                try:
                    await ch.send_message(jid, "⚠️ 回應傳送失敗，請再試一次。")
                except Exception:
                    pass
            break


_MAX_FILE_BYTES = 45 * 1024 * 1024  # 45 MB — safely under Telegram's 50 MB bot limit


async def route_file(jid: str, file_path: str, caption: str = "") -> None:
    """Route a file to the appropriate channel for delivery.

    Pre-flight checks performed before handing off to the channel:
    - File must exist on disk.
    - File must be under _MAX_FILE_BYTES (45 MB).  Files that exceed the limit
      trigger a plain-text notification to the user instead of a broken upload.
    """
    import pathlib as _pl

    ch = find_channel(jid)
    if ch is None:
        log.warning("route_file: no channel for jid=%s", jid)
        return

    p = _pl.Path(file_path)
    if not p.exists():
        log.warning("route_file: file not found: %s", file_path)
        try:
            await ch.send_message(jid, f"[File not found: {p.name}]")
        except Exception:
            pass
        return

    file_size = p.stat().st_size
    if file_size > _MAX_FILE_BYTES:
        size_mb = file_size / (1024 * 1024)
        log.warning(
            "route_file: file %s is %.1f MB, exceeds %.0f MB limit — sending text notice",
            p.name, size_mb, _MAX_FILE_BYTES / (1024 * 1024),
        )
        try:
            await ch.send_message(
                jid,
                f"[File '{p.name}' ({size_mb:.1f} MB) exceeds the {_MAX_FILE_BYTES // (1024*1024)} MB "
                f"upload limit and could not be sent. Please reduce the file size.]",
            )
        except Exception:
            pass
        return

    await ch.send_file(jid, file_path, caption)

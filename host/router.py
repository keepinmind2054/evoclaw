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


async def route_outbound(jid: str, text: str) -> None:
    ch = find_channel(jid)
    if not ch:
        log.warning(f"No channel found for JID {jid}")
        return
    formatted = format_outbound(text)
    chunks = _split_message(formatted)
    for chunk in chunks:
        try:
            await ch.send_message(jid, chunk)
        except Exception as e:
            log.error(f"Failed to send message to {jid}: {e}")
            break

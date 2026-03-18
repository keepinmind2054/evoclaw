"""Channel registry"""
from typing import Protocol, Callable, Awaitable

class Channel(Protocol):
    name: str
    async def connect(self) -> None: ...
    async def send_message(self, jid: str, text: str) -> None: ...
    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
    async def disconnect(self) -> None: ...
    async def send_file(self, jid: str, file_path: str, caption: str = "") -> None:
        """Send a file/document to the chat. Override in channel implementations."""
        await self.send_message(jid, f"📎 File: {file_path}
{caption}".strip())

_registry: dict[str, type] = {}

def register_channel_class(name: str, cls: type) -> None:
    _registry[name] = cls

def get_channel_class(name: str):
    return _registry.get(name)

def get_registered_channel_names() -> list[str]:
    return list(_registry.keys())

# Phase 3: Matrix channel support
try:
    from .matrix_channel import MatrixChannel, MatrixMessage, MatrixRoom
except ImportError:
    pass

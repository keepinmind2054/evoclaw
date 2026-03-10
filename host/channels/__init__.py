"""Channel registry"""
from typing import Protocol, Callable, Awaitable

class Channel(Protocol):
    name: str
    async def connect(self) -> None: ...
    async def send_message(self, jid: str, text: str) -> None: ...
    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
    async def disconnect(self) -> None: ...

_registry: dict[str, type] = {}

def register_channel_class(name: str, cls: type) -> None:
    _registry[name] = cls

def get_channel_class(name: str):
    return _registry.get(name)

def get_registered_channel_names() -> list[str]:
    return list(_registry.keys())

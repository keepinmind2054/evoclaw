"""Path remapping for renamed core files in the Evoclaw skills engine."""

from pathlib import Path

from .constants import EVOCLAW_DIR
from .state import read_state, write_state


def load_path_remap() -> dict[str, str]:
    """Load the path remap table from state."""
    try:
        state = read_state()
        return state.path_remap or {}
    except FileNotFoundError:
        return {}


def record_path_remap(old_path: str, new_path: str) -> None:
    """Record that a file was renamed from old_path to new_path."""
    state = read_state()
    if not state.path_remap:
        state.path_remap = {}
    state.path_remap[old_path] = new_path
    write_state(state)


def resolve_path_remap(path: str, remap: dict[str, str]) -> str:
    """
    Resolve a path through the remap table, following chains.
    Returns the final resolved path.
    """
    visited = set()
    current = path
    while current in remap:
        if current in visited:
            break  # Cycle detection
        visited.add(current)
        current = remap[current]
    return current

"""
NanoClaw IPC Bridge — send messages through NanoClaw's file-based IPC watcher.

NanoClaw polls  {NANOCLAW_DATA_DIR}/ipc/{NANOCLAW_GROUP_FOLDER}/messages/
every 1 second and forwards any JSON file with
  {"type": "message", "chatJid": "<jid>", "text": "<text>"}
to the target chat.  We write atomically via a .tmp rename so NanoClaw
never sees a partial file.

Environment variables
---------------------
NANOCLAW_DATA_DIR      – absolute path to NanoClaw's data/ directory
                         (shared filesystem mount, e.g. /app/nanoclaw/data)
NANOCLAW_GROUP_FOLDER  – folder name registered in NanoClaw for this group
                         (e.g. "telegram_mygroup")
NANOCLAW_DISCORD_JID   – default target chat JID (e.g. "dc:1234567890")
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("NANOCLAW_DATA_DIR", "")
_GROUP_FOLDER = os.environ.get("NANOCLAW_GROUP_FOLDER", "")
_DEFAULT_JID = os.environ.get("NANOCLAW_DISCORD_JID", "")

# Pre-compute the IPC directory path (components are module-level constants).
# The directory is created lazily on first send(); not at import time, so that
# a missing NANOCLAW_DATA_DIR at startup does not crash the module.
_IPC_DIR: Path | None = (
    Path(_DATA_DIR) / "ipc" / _GROUP_FOLDER / "messages"
    if _DATA_DIR and _GROUP_FOLDER
    else None
)


def is_configured() -> bool:
    """Return True when all required env vars are set."""
    return bool(_DATA_DIR and _GROUP_FOLDER and _DEFAULT_JID)


def send(text: str, *, chat_jid: Optional[str] = None) -> bool:
    """
    Write *text* as an IPC message file for NanoClaw to pick up.

    Returns True on success, False if the bridge is not configured or the
    write fails (error is logged but not raised).
    """
    jid = chat_jid or _DEFAULT_JID
    ipc_dir = _IPC_DIR
    if not (ipc_dir and jid):
        logger.warning(
            "nanoclaw_bridge: not configured "
            "(NANOCLAW_DATA_DIR=%r NANOCLAW_GROUP_FOLDER=%r NANOCLAW_DISCORD_JID=%r)",
            _DATA_DIR, _GROUP_FOLDER, _DEFAULT_JID,
        )
        return False

    filename = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    payload = json.dumps({"type": "message", "chatJid": jid, "text": text}, ensure_ascii=False)
    tmp_path = ipc_dir / (filename + ".tmp")
    final_path = ipc_dir / filename
    try:
        ipc_dir.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.rename(final_path)          # atomic on POSIX + same-fs
    except OSError as exc:
        logger.error("nanoclaw_bridge: write failed for %s: %s", final_path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    logger.debug("nanoclaw_bridge: queued %s → %s", filename, jid)
    return True

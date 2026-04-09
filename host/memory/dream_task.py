"""DreamTask -- Background memory consolidation during idle periods.

Inspired by Claude Code's DreamTask (tengu_onyx_plover).
When a group has been idle for DREAM_IDLE_THRESHOLD_MINUTES (default: 15),
runs a focused LLM pass to reorganize and improve hot memory quality.

The dream pass:
1. Extracts and deduplicates key facts (user names, preferences, ongoing work)
2. Organizes into sections: [User Context] [Ongoing Tasks] [Key Facts] [Preferences]
3. Removes conversational noise, timestamps, duplicate entries
4. Flags anything that seems outdated with a note
5. Outputs the reorganized memory only (no commentary)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Minimum memory size (bytes) to bother dreaming about.
# Very short memories don't benefit from consolidation.
_MIN_MEMORY_BYTES = 200

_DREAM_SYSTEM_PROMPT = """\
You are a memory consolidation system. Your job is to reorganize and improve \
a persistent memory file for an AI assistant, making it cleaner and more \
useful for future conversations.

Organize the output into these sections (omit empty sections):

[User Context]
Key facts about users: names, roles, relationships, communication style.

[Ongoing Tasks]
Active projects, pending work, deadlines, blockers.

[Key Facts]
Important decisions, configurations, system details, established conventions.

[Preferences]
User-stated preferences, workflow choices, tool preferences, formatting rules.

RULES:
- Output ONLY the reorganized memory content, nothing else
- Deduplicate: merge entries that say the same thing
- Be concise: each bullet point should be <= 20 words
- Remove conversational noise, greetings, and filler
- Remove timestamps older than 7 days (keep the fact, drop the date)
- If something seems outdated or contradicted by newer info, add (outdated?) tag
- Preserve all genuinely useful information -- do not discard real facts
- Use bullet points (- prefix) within each section
- Target size: under {target_words} words
- Never invent or hallucinate information not in the original"""

_DREAM_USER_PROMPT = """\
Reorganize and consolidate the following memory file. \
Preserve all important facts, remove noise and duplicates.

---MEMORY START---
{memory}
---MEMORY END---

Output the consolidated memory:"""


async def should_dream(jid: str, last_active_ts: float, idle_minutes: int = 15) -> bool:
    """Return True if the group has been idle long enough to trigger a dream pass.

    Parameters:
        jid: Group identifier (for logging)
        last_active_ts: Unix timestamp of last message activity
        idle_minutes: Minutes of idle time required before dreaming
    """
    if last_active_ts <= 0:
        return False
    elapsed_minutes = (time.time() - last_active_ts) / 60.0
    return elapsed_minutes >= idle_minutes


async def run_dream_pass(jid: str, current_memory: str, api_key: str = "", model: str = "") -> str:
    """Run a focused LLM consolidation pass on hot memory.

    Returns the consolidated memory string, or the original memory if
    the LLM call fails or produces invalid output.
    """
    if not current_memory or not current_memory.strip():
        return current_memory

    original_bytes = len(current_memory.encode("utf-8"))
    if original_bytes < _MIN_MEMORY_BYTES:
        logger.debug("dream_task: memory too short for %s (%d bytes), skipping", jid, original_bytes)
        return current_memory

    # Auto-detect API key from environment if not provided
    if not api_key:
        api_key = (
            os.environ.get("GOOGLE_API_KEY", "").split(",")[0].strip()
            or os.environ.get("CLAUDE_API_KEY", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", "")
        )

    if not api_key:
        logger.warning("dream_task: no API key available for group=%s, skipping dream pass", jid)
        return current_memory

    # Reuse the LLM calling infrastructure from memory_compress
    from .memory_compress import _detect_backend, _call_llm

    backend = _detect_backend(api_key, model)

    # Target ~800 words for an 8KB memory (generous but encourages consolidation)
    target_words = max(200, original_bytes // 10)
    system_prompt = _DREAM_SYSTEM_PROMPT.format(target_words=target_words)
    user_prompt = _DREAM_USER_PROMPT.format(memory=current_memory)

    try:
        result = await _call_llm(backend, api_key, model, system_prompt, user_prompt)
    except Exception as exc:
        logger.warning("dream_task: LLM call failed for group=%s: %s", jid, exc)
        return current_memory

    if not result or not result.strip():
        logger.warning("dream_task: empty LLM response for group=%s", jid)
        return current_memory

    consolidated = result.strip()
    consolidated_bytes = len(consolidated.encode("utf-8"))

    # Validate: reject if suspiciously tiny or much larger than original
    if consolidated_bytes < 20:
        logger.warning(
            "dream_task: consolidated result too small (%d bytes) for group=%s, keeping original",
            consolidated_bytes, jid,
        )
        return current_memory

    if consolidated_bytes > original_bytes * 1.5:
        logger.warning(
            "dream_task: consolidated (%d bytes) much larger than original (%d bytes) for group=%s, "
            "keeping original",
            consolidated_bytes, original_bytes, jid,
        )
        return current_memory

    reduction_pct = 100 * (1 - consolidated_bytes / max(original_bytes, 1))
    logger.info(
        "dream_task: group=%s consolidated %d -> %d bytes (%.0f%% reduction)",
        jid, original_bytes, consolidated_bytes, reduction_pct,
    )
    return consolidated


class DreamScheduler:
    """Tracks last-active timestamp per group and triggers dream passes.

    Integrated into main.py's message loop:
    - touch(jid): called on every incoming message to update last-active time
    - tick(): called periodically (every 60s) to trigger dream for idle groups
    - is_dreaming(jid): returns True if a dream pass is in progress for this group
    """

    def __init__(self, idle_minutes: int = 15, enabled: bool = True):
        self._idle_minutes = idle_minutes
        self._enabled = enabled
        # jid -> last message timestamp (Unix seconds)
        self._last_active: dict[str, float] = {}
        # jid -> True while a dream pass is running (prevents concurrent dreams)
        self._dreaming: dict[str, bool] = {}
        # jid -> last dream timestamp (prevents re-dreaming too quickly)
        self._last_dream: dict[str, float] = {}
        # Minimum time between dream passes for the same group (1 hour)
        self._dream_cooldown = 3600.0
        # MEM-05 (dream GC): store strong references to running dream tasks so
        # the event loop cannot garbage-collect and silently cancel them before
        # they finish.  Tasks are removed from the set in _run_dream's finally
        # block via a done-callback added at creation time.
        self._dream_tasks: set[asyncio.Task] = set()
        # MEM-05 (write-write race): per-jid asyncio.Lock held by _run_dream
        # while it reads + writes hot memory.  patch_hot_memory() in MemoryBus
        # should acquire get_hot_lock(jid) before modifying the same file so
        # the two operations are mutually exclusive.
        self._hot_locks: dict[str, asyncio.Lock] = {}

    def touch(self, jid: str) -> None:
        """Update last-active time for a group. Call on every incoming message."""
        self._last_active[jid] = time.time()

    def is_dreaming(self, jid: str) -> bool:
        """Return True if a dream pass is currently in progress for this group."""
        return self._dreaming.get(jid, False)

    async def tick(self) -> None:
        """Check all tracked groups and trigger dream passes for idle ones.

        Called periodically (e.g., every 60 seconds) from the message loop.
        """
        if not self._enabled:
            return

        now = time.time()

        for jid, last_active in list(self._last_active.items()):
            # Skip if already dreaming
            if self._dreaming.get(jid):
                continue

            # Skip if dreamed recently (cooldown)
            last_dream = self._last_dream.get(jid, 0.0)
            if now - last_dream < self._dream_cooldown:
                continue

            # Check if idle long enough
            if not await should_dream(jid, last_active, self._idle_minutes):
                continue

            # Trigger dream pass in background.
            # MEM-05 (dream GC): keep a strong reference to the task in
            # self._dream_tasks so the event loop cannot garbage-collect and
            # silently cancel it.  The done-callback removes the reference
            # once the task finishes (whether successfully or with an error).
            self._dreaming[jid] = True
            task = asyncio.create_task(
                self._run_dream(jid),
                name=f"dream-{jid}",
            )
            self._dream_tasks.add(task)
            task.add_done_callback(self._dream_tasks.discard)

    async def _run_dream(self, jid: str) -> None:
        """Execute a dream pass for a single group."""
        try:
            from .hot import get_hot_memory, update_hot_memory

            # MEM-05 (write-write race): hold the per-jid hot lock for the
            # entire read-consolidate-write cycle so that patch_hot_memory()
            # (which must also acquire get_hot_lock(jid)) cannot interleave
            # and overwrite our consolidated result, or we theirs.
            async with self.get_hot_lock(jid):
                current_memory = get_hot_memory(jid)
                if not current_memory or len(current_memory.encode("utf-8")) < _MIN_MEMORY_BYTES:
                    logger.debug("dream_task: memory too short for %s, skipping dream", jid)
                    return

                logger.info("dream_task: starting dream pass for group=%s (%d bytes)",
                            jid, len(current_memory.encode("utf-8")))

                consolidated = await run_dream_pass(jid, current_memory)

                # Only update if the dream actually changed something
                if consolidated != current_memory:
                    update_hot_memory(jid, consolidated)
                    logger.info("dream_task: dream pass complete for group=%s, memory updated", jid)
                else:
                    logger.debug("dream_task: dream pass for group=%s produced no changes", jid)

            self._last_dream[jid] = time.time()

        except Exception as exc:
            logger.error("dream_task: dream pass failed for group=%s: %s", jid, exc)
        finally:
            self._dreaming[jid] = False

    def get_hot_lock(self, jid: str) -> asyncio.Lock:
        """Return the per-jid asyncio.Lock used to serialise hot-memory writes.

        Both _run_dream() and MemoryBus.patch_hot_memory() must acquire this
        lock before reading or writing a group's hot memory so that a dream
        consolidation and a live patch cannot clobber each other.

        dict.setdefault() is atomic in CPython (GIL-protected), so concurrent
        callers will always receive the same Lock object for a given jid.
        """
        return self._hot_locks.setdefault(jid, asyncio.Lock())

    def remove_group(self, jid: str) -> None:
        """Clean up tracking state for a deregistered group."""
        self._last_active.pop(jid, None)
        self._dreaming.pop(jid, None)
        self._last_dream.pop(jid, None)
        self._hot_locks.pop(jid, None)

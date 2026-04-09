"""DreamTask -- Background memory consolidation during idle periods.

Inspired by Claude Code's DreamTask (tengu_onyx_plover).
When a group has been idle for DREAM_IDLE_THRESHOLD_MINUTES (default: 15),
runs a focused LLM pass to reorganize and improve hot memory quality.

The dream pass:
1. Reads warm logs (today + yesterday) verbatim and injects them as RECENT ACTIVITY
2. Extracts and deduplicates key facts (user names, preferences, ongoing work)
3. Organizes into typed sections: [Decisions] [User Context] [Ongoing Tasks]
   [Key Facts] [Preferences] [Problems Solved]
4. Removes conversational noise, timestamps, duplicate entries
5. Flags anything that seems outdated with a note
6. Outputs the reorganized memory only (no commentary)
7. Writes each typed section as a separate SharedMemoryStore entry
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Minimum memory size (bytes) to bother dreaming about.
# Very short memories don't benefit from consolidation.
_MIN_MEMORY_BYTES = 200

# Keywords used for classifying warm log entries by type
_TYPE_KEYWORDS: dict[str, list[str]] = {
    "decisions": [
        "decided", "decision", "agreed", "agreement", "chose", "choice",
        "will use", "going to", "we'll", "confirmed", "resolved to",
    ],
    "preferences": [
        "prefer", "preference", "like", "dislike", "want", "don't want",
        "always", "never", "favorite", "favourite", "rather",
    ],
    "problems": [
        "error", "bug", "issue", "problem", "fail", "broken", "crash",
        "exception", "not working", "wrong", "fix", "debug",
    ],
    "facts": [
        "is", "are", "version", "api", "endpoint", "config", "key",
        "token", "url", "path", "port", "database", "schema",
    ],
    "technical": [
        "code", "function", "class", "module", "import", "install",
        "deploy", "build", "test", "commit", "branch", "docker",
    ],
}

_DREAM_SYSTEM_PROMPT = """\
You are a memory consolidation system. Your job is to reorganize and improve \
a persistent memory file for an AI assistant, making it cleaner and more \
useful for future conversations.

Please organize your memory consolidation into these sections (omit empty sections):

[Decisions]
Choices made, agreements reached, directions confirmed.

[User Context]
Who the user is, their goals, roles, relationships, communication style.

[Ongoing Tasks]
Active projects, pending work, deadlines, blockers.

[Key Facts]
Important technical or factual information, configurations, system details, \
established conventions.

[Preferences]
User's stated preferences, workflow choices, tool preferences, formatting rules.

[Problems Solved]
Issues that were resolved, bugs fixed, workarounds found.

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

{recent_activity_section}\
---MEMORY START---
{memory}
---MEMORY END---

Output the consolidated memory:"""

# Map dream section names to SharedMemoryStore-compatible memory_type values
_SECTION_TO_MEMORY_TYPE: dict[str, str] = {
    "decisions": "decisions",
    "user context": "user_context",
    "ongoing tasks": "ongoing_tasks",
    "key facts": "key_facts",
    "preferences": "preferences",
    "problems solved": "problems_solved",
}


def _classify_entry(user_text: str, bot_text: str) -> tuple[str, float]:
    """Classify a warm log entry by type and estimate importance.

    Returns (type_name, importance) where type_name is one of:
    decisions, preferences, problems, facts, technical, general.
    Importance is a float in [0.0, 1.0].
    """
    combined = (user_text + " " + bot_text).lower()

    scores: dict[str, int] = {}
    for type_name, keywords in _TYPE_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in combined)
        if count > 0:
            scores[type_name] = count

    if not scores:
        return "general", 0.3

    best_type = max(scores, key=lambda k: scores[k])
    # Importance: normalize hit count, cap at 1.0
    importance = min(1.0, 0.3 + scores[best_type] * 0.1)
    return best_type, importance


def _parse_warm_entry_block(block: str) -> Optional[dict]:
    """Parse a single warm log block of the form:
        ### HH:MM
        User: ...
        Bot: ...

    Returns a dict with keys: time, user, bot, or None if unparseable.
    """
    block = block.strip()
    if not block:
        return None

    lines = block.splitlines()
    time_str = ""
    user_text = ""
    bot_text = ""

    for line in lines:
        line_stripped = line.strip()
        if line_stripped.startswith("### "):
            time_str = line_stripped[4:].strip()
        elif line_stripped.startswith("User:"):
            user_text = line_stripped[5:].strip()
        elif line_stripped.startswith("Bot:"):
            bot_text = line_stripped[4:].strip()

    if not time_str and not user_text and not bot_text:
        return None

    return {"time": time_str, "user": user_text, "bot": bot_text}


def parse_warm_entries(jid: str, days: int = 2) -> list[dict]:
    """Read today's and yesterday's warm log entries for a group.

    Warm log format (from warm.py):
        ### HH:MM
        User: <preview>
        Bot: <preview>

    The entries are fetched from the group_warm_logs SQLite table via the
    existing db.get_warm_logs_for_date() helper (one row per conversation
    exchange, written by warm.append_warm_log()).

    Parameters:
        jid:   Group JID used as the key in the warm_logs DB table.
        days:  Number of past days to fetch (default 2 = today + yesterday).

    Returns:
        List of dicts with keys:
            time       (str)   -- HH:MM timestamp
            user       (str)   -- user message preview
            bot        (str)   -- bot message preview
            type       (str)   -- classified type
                                  (decisions/preferences/problems/facts/technical/general)
            importance (float) -- estimated importance score 0.0-1.0
    """
    from .. import db

    entries: list[dict] = []

    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    dates_to_fetch = [today, yesterday] if days >= 2 else [today]

    for log_date in dates_to_fetch:
        try:
            rows = db.get_warm_logs_for_date(jid, log_date)
        except Exception as exc:
            logger.warning(
                "dream_task: failed to fetch warm logs for jid=%s date=%s: %s",
                jid, log_date, exc,
            )
            continue

        for row in rows:
            raw_content = row.get("content", "")
            if not raw_content:
                continue

            # Each DB row contains one entry block written by warm.append_warm_log()
            parsed = _parse_warm_entry_block(raw_content)
            if parsed is None:
                # Fallback: treat entire content as a single blob
                parsed = {"time": "", "user": raw_content[:200], "bot": ""}

            entry_type, importance = _classify_entry(parsed["user"], parsed["bot"])
            entries.append({
                "time": parsed["time"],
                "user": parsed["user"],
                "bot": parsed["bot"],
                "type": entry_type,
                "importance": importance,
            })

    return entries


def _build_recent_activity_section(entries: list[dict]) -> str:
    """Group warm entries by type and format as a RECENT ACTIVITY block.

    Returns a formatted string ready to prepend to the dream prompt,
    or an empty string if there are no entries.
    """
    if not entries:
        return ""

    # Group entries by type, preserving insertion order within each group
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        t = entry.get("type", "general")
        groups.setdefault(t, []).append(entry)

    # Display order (omit groups with no entries)
    display_order = ["decisions", "preferences", "problems", "facts", "technical", "general"]

    lines = ["== RECENT ACTIVITY =="]
    for group_type in display_order:
        if group_type not in groups:
            continue
        section_name = group_type.capitalize()
        lines.append(f"\n[{section_name}]")
        for entry in groups[group_type]:
            ts = entry.get("time", "")
            user_snippet = entry.get("user", "")[:120]
            if ts:
                lines.append(f"- {ts}: {user_snippet}")
            else:
                lines.append(f"- {user_snippet}")

    lines.append("")  # blank line before MEMORY block
    return "\n".join(lines) + "\n"


def _parse_typed_sections(consolidated: str) -> dict[str, str]:
    """Parse the LLM output into a dict of section_name -> section_content.

    Looks for headers of the form [Section Name] at the start of a line and
    collects the content between consecutive headers.

    Returns a dict mapping lowercase section names to their content strings.
    """
    pattern = re.compile(r"^\[([^\]]+)\]", re.MULTILINE)
    matches = list(pattern.finditer(consolidated))

    if not matches:
        return {}

    sections: dict[str, str] = {}
    for i, match in enumerate(matches):
        section_name = match.group(1).strip().lower()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(consolidated)
        content = consolidated[start:end].strip()
        if content:
            sections[section_name] = content

    return sections


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

    Injects today's and yesterday's warm log entries as a RECENT ACTIVITY
    section prepended to the dream prompt so they enter the consolidation pass.

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

    # Fix 2: Fetch warm entries and build RECENT ACTIVITY section
    warm_entries = parse_warm_entries(jid, days=2)
    recent_activity_section = _build_recent_activity_section(warm_entries)
    if warm_entries:
        logger.info(
            "dream_task: injecting %d warm entries into dream prompt for group=%s",
            len(warm_entries), jid,
        )

    # Target ~800 words for an 8KB memory (generous but encourages consolidation)
    target_words = max(200, original_bytes // 10)
    # Fix 3: Use typed output prompt
    system_prompt = _DREAM_SYSTEM_PROMPT.format(target_words=target_words)
    user_prompt = _DREAM_USER_PROMPT.format(
        recent_activity_section=recent_activity_section,
        memory=current_memory,
    )

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
        # they finish.  Tasks are removed from the set via a done-callback
        # added at creation time.
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

                    # Fix 4: write typed section blocks to SharedMemoryStore
                    await self._write_typed_blocks(jid, consolidated)
                else:
                    logger.debug("dream_task: dream pass for group=%s produced no changes", jid)

            self._last_dream[jid] = time.time()

        except Exception as exc:
            logger.error("dream_task: dream pass failed for group=%s: %s", jid, exc)
        finally:
            self._dreaming[jid] = False

    async def _write_typed_blocks(self, jid: str, consolidated: str) -> None:
        """Parse typed [Section] blocks from the consolidated memory and write
        each as a separate SharedMemoryStore entry.

        This implements GAP-11 Fix 4: typed output blocks -> SharedMemoryStore.
        """
        sections = _parse_typed_sections(consolidated)
        if not sections:
            logger.debug(
                "dream_task: no typed sections found in consolidated memory for jid=%s", jid,
            )
            return

        try:
            import sqlite3
            from .. import db as host_db
            from .memory_bus import SharedMemoryStore

            # Resolve the on-disk db path so SharedMemoryStore can open its own connection
            db_path: str = ""
            try:
                db_conn = host_db.get_db()
                row = db_conn.execute("PRAGMA database_list").fetchone()
                db_path = row[2] if row else ""
            except Exception:
                pass

            conn_for_store = (
                sqlite3.connect(db_path, check_same_thread=False)
                if db_path
                else host_db.get_db()
            )
            store = SharedMemoryStore(conn_for_store, db_path=db_path)

            written = 0
            for section_name, content in sections.items():
                memory_type = _SECTION_TO_MEMORY_TYPE.get(section_name, section_name.replace(" ", "_"))
                # Compose the stored content with a clear type header so callers
                # know which section each entry came from.
                tagged_content = f"[{section_name.title()}]\n{content}"
                store.write(
                    content=tagged_content,
                    agent_id=jid,
                    scope="private",
                    importance=0.7,  # dream-consolidated memories are moderately high importance
                )
                written += 1
                logger.debug(
                    "dream_task: wrote typed block section=%r memory_type=%r for jid=%s",
                    section_name, memory_type, jid,
                )

            logger.info(
                "dream_task: wrote %d typed memory blocks to SharedMemoryStore for jid=%s",
                written, jid,
            )
        except Exception as exc:
            logger.warning(
                "dream_task: failed to write typed blocks to SharedMemoryStore for jid=%s: %s",
                jid, exc,
            )

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

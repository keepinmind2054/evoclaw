"""Hot memory management — MEMORY.md per group (8KB limit)."""
from __future__ import annotations
import logging
import re
import time
from .. import db

log = logging.getLogger(__name__)

HOT_MEMORY_MAX_BYTES = 8 * 1024  # 8KB

# Keywords that boost a section's importance score.
_IMPORTANCE_KEYWORDS = re.compile(
    r"\b(CRITICAL|DECISION|PREFERENCE|IMPORTANT|URGENT|TODO|FIXME)\b",
    re.IGNORECASE,
)

# Timestamp-like patterns used to detect recency cues in a section.
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"  # ISO date e.g. 2024-03-15
    r"|\d{1,2}/\d{1,2}/\d{2,4}"  # US date e.g. 3/15/24
    r"|\b(?:today|yesterday|just now|recently)\b",
    re.IGNORECASE,
)


def _safe_truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes without splitting a
    multi-byte character.

    Python's ``bytes[:n].decode("utf-8", errors="ignore")`` silently drops
    any partial multi-byte sequence that straddles the cut point, which is
    correct but easy to miss.  This helper makes the intent explicit and
    ensures we never exceed the byte limit.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Walk back from the cut point until we land on a valid UTF-8 boundary.
    # UTF-8 continuation bytes have the form 10xxxxxx (0x80–0xBF).
    cut = max_bytes
    while cut > 0 and (encoded[cut] & 0xC0) == 0x80:
        cut -= 1
    return encoded[:cut].decode("utf-8", errors="ignore")


class MemoryStack:
    """Token-budget-aware memory injection controller (inspired by mempalace layers).

    Instead of injecting the full MEMORY.md unconditionally (~2000 tokens at
    8 KB), ``wake_up()`` splits the file into sections, scores each section by
    recency and importance, then greedily selects the highest-scoring sections
    that fit within a configurable token budget.  This typically cuts startup
    token cost 60-70% while retaining the most relevant memories.
    """

    DEFAULT_BUDGET = 1200  # tokens (~4800 chars at 4 chars/token heuristic)
    CHARS_PER_TOKEN = 4    # rough heuristic

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def wake_up(
        self,
        agent_id: str,
        memory_md_path: str,
        token_budget: int = DEFAULT_BUDGET,
    ) -> str:
        """Read MEMORY.md, split into sections, score by recency + importance,
        return top-N sections that fit within *token_budget*.

        Args:
            agent_id:        Agent identifier (used only for logging).
            memory_md_path:  Absolute path to the MEMORY.md file.
            token_budget:    Maximum tokens to return (default 1200 ≈ 4800 chars).

        Returns:
            A string containing the selected sections joined by newlines.
            Returns an empty string if the file does not exist or is empty.
        """
        try:
            with open(memory_md_path, encoding="utf-8", errors="ignore") as fh:
                raw = fh.read()
        except FileNotFoundError:
            log.debug("MemoryStack.wake_up: no MEMORY.md for agent=%s", agent_id)
            return ""
        except OSError as exc:
            log.warning("MemoryStack.wake_up: cannot read %s: %s", memory_md_path, exc)
            return ""

        if not raw.strip():
            return ""

        sections = self._split_sections(raw)
        scored = [(self._score_section(s), s) for s in sections]
        scored.sort(key=lambda t: t[0], reverse=True)

        char_budget = token_budget * self.CHARS_PER_TOKEN
        selected: list[str] = []
        used = 0
        for score, section in scored:
            cost = len(section) + 1  # +1 for the joining newline
            if used + cost > char_budget:
                continue
            selected.append(section)
            used += cost

        # Re-join in original document order for coherent output.
        original_order = [s for s in sections if s in selected]
        result = "\n".join(original_order)
        log.debug(
            "MemoryStack.wake_up: agent=%s selected %d/%d sections (%d chars, budget %d tokens)",
            agent_id,
            len(selected),
            len(sections),
            used,
            token_budget,
        )
        return result

    def render_for_prompt(self, memories: list, max_chars: int = 3000) -> str:
        """Format Memory objects as compact inject-ready string with source labels.

        Args:
            memories:   List of Memory dataclass instances (must have .source,
                        .scope, and .content attributes).
            max_chars:  Hard character cap for the rendered output.

        Returns:
            A newline-separated string of labelled memory lines that fits within
            *max_chars*.
        """
        lines: list[str] = []
        total = 0
        for m in memories:
            label = f"[{m.source.upper()}|{m.scope}]"
            line = f"{label} {m.content}"
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line) + 1
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _split_sections(self, text: str) -> list[str]:
        """Split *text* into logical sections.

        Sections are delimited by ``##`` Markdown headers or ``---``
        horizontal-rule separators.  The delimiter is kept at the start of
        its section so each section is self-contained.
        """
        # Split on lines that start a new ## header or a --- separator.
        parts = re.split(r"(?m)(?=^##\s|^---\s*$)", text)
        # Filter out empty / whitespace-only parts.
        return [p for p in parts if p.strip()]

    def _score_section(self, section: str) -> float:
        """Return a relevance score in [0.0, 1.0] for *section*.

        Scoring rules:
        - Sections containing importance keywords (CRITICAL, DECISION,
          PREFERENCE, IMPORTANT, URGENT, TODO, FIXME) score 0.9.
        - Sections containing date/recency cues score 0.7 (recency boost).
        - All other sections score 0.5 (neutral default).

        Multiple rules can apply; the highest applicable score wins.
        """
        score = 0.5  # neutral default

        if _TIMESTAMP_RE.search(section):
            score = max(score, 0.7)

        if _IMPORTANCE_KEYWORDS.search(section):
            score = max(score, 0.9)

        return score


def get_hot_memory(jid: str, token_budget: int | None = None) -> str:
    """Return the hot memory content for a group, or empty string.

    Args:
        jid:          Group / agent JID used to look up the stored content.
        token_budget: Optional token budget.  When provided, the content is
                      filtered through ``MemoryStack.wake_up()`` so that only
                      the highest-scoring sections within the budget are
                      returned.  When *None* the full stored content is
                      returned (original behaviour).
    """
    row = db.get_hot_memory(jid)
    if not row:
        return ""
    if token_budget is None:
        return row

    # Token-budget path: write the raw content to a temporary file so that
    # MemoryStack.wake_up() can read it via its file-based API.
    import os
    import tempfile

    stack = MemoryStack()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(row)
        tmp_path = tmp.name
    try:
        return stack.wake_up(agent_id=jid, memory_md_path=tmp_path, token_budget=token_budget)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def update_hot_memory(jid: str, content: str) -> None:
    """Update hot memory for a group, enforcing 8KB limit.

    Bug fixed (p14b-1): previous code used
    ``encoded[:MAX].decode("utf-8", errors="ignore")`` which silently
    discards partial multi-byte characters at the boundary.  We now walk
    back to a clean UTF-8 boundary before decoding.
    """
    encoded = content.encode("utf-8")
    if len(encoded) > HOT_MEMORY_MAX_BYTES:
        content = _safe_truncate_utf8(content, HOT_MEMORY_MAX_BYTES)
        log.warning("hot_memory: content truncated to 8KB for jid=%s", jid)
    db.set_hot_memory(jid, content)
    log.debug("hot_memory: updated for jid=%s (%d bytes)", jid, len(content.encode()))

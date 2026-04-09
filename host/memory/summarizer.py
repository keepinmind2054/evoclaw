"""
Memory Summarizer — Phase 2 of UnifiedClaw architecture

Automatically compresses conversation history into MEMORY.md hot memory.

Two main operations:
1. summarize_session(): After a conversation ends, summarize key learnings
2. compress_memory(): When MEMORY.md approaches size limit, compress it

Usage:
    summarizer = MemorySummarizer()
    patch = await summarizer.summarize_session(
        agent_id="mybot",
        messages=[{"role": "user", "content": "..."}, ...],
    )
    await memory_bus.patch_hot_memory(agent_id, patch)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

COMPRESS_THRESHOLD = 6144   # Start compressing at 6KB
MAX_MEMORY_BYTES = 8192     # Hard limit 8KB
MAX_CONV_CHARS = 4000       # Truncate long conversations before LLM

_SUMMARIZE_PROMPT = (
    "You are a memory distillation system. Extract key facts from a "
    "conversation as concise bullet points for a persistent memory file.\n\n"
    "Rules:\n"
    "- Extract only genuinely useful, long-term facts\n"
    "- Be extremely concise (each bullet <= 15 words)\n"
    "- Use format: bullet [fact] one per line\n"
    "- Maximum 5 bullet points per session\n"
    "- If nothing important: output: bullet [no significant new information]\n\n"
    "Conversation:\n{conversation}\n\n"
    "Existing memory (do not repeat):\n{existing_memory}\n\n"
    "Output ONLY bullet points:"
)

_COMPRESS_PROMPT = (
    "Compress this memory file to under {max_words} words while preserving all key facts.\n\n"
    "Rules:\n"
    "- Merge duplicate/redundant facts\n"
    "- Remove outdated information\n"
    "- Keep bullet point format\n\n"
    "Memory:\n{memory}\n\n"
    "Output compressed memory ONLY:"
)

# Minimum length (chars) for a valid summarization result.
_MIN_SUMMARY_CHARS = 5
# Regex: a valid summary line starts with a bullet marker or a dash.
_BULLET_RE = re.compile(r"^\s*[-•*]|^\s*bullet\s", re.MULTILINE | re.IGNORECASE)


def _looks_like_summary(text: str) -> bool:
    """Return True if *text* looks like it contains at least one bullet point.

    This is a lightweight sanity check to reject obvious garbage (e.g. an
    API error message or an empty-ish response) before storing into memory.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_SUMMARY_CHARS:
        return False
    return bool(_BULLET_RE.search(stripped))


class MemorySummarizer:
    """
    LLM-powered memory summarization and compression.

    Supports Gemini (default), Claude, and OpenAI-compatible APIs.
    Falls back to a simple stub if no LLM is available.
    """

    def __init__(
        self,
        google_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
    ):
        self._google_key = (
            google_api_key
            or os.environ.get("GOOGLE_API_KEY", "").split(",")[0].strip()
        )
        self._anthropic_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._openai_key = openai_api_key or os.environ.get("OPENAI_API_KEY", "")
        self._openai_base = openai_base_url or os.environ.get("OPENAI_BASE_URL", "")

    async def summarize_session(
        self,
        agent_id: str,
        messages: list,
        existing_memory: str = "",
        max_messages: int = 20,
        kg=None,
        jid: str = "",
    ) -> str:
        """
        Summarize a conversation session into bullet points for MEMORY.md.

        Returns a patch string to append to MEMORY.md.

        Bug fixed (p14b-12): LLM output is now validated before use.  If the
        response does not look like a bullet-point list (e.g. it is an error
        message, empty, or pure whitespace) we fall back to the stub summary
        rather than storing garbage into MEMORY.md.

        Args:
            agent_id:       ID of the agent whose session is being summarized.
            messages:       List of message dicts with "role" and "content" keys.
            existing_memory: Current MEMORY.md content (to avoid repetition).
            max_messages:   Maximum number of recent messages to include.
            kg:             Optional EvoKnowledgeGraph instance. When provided,
                            entities and facts are extracted and populated into
                            the graph after summarization.
            jid:            Group JID used as the KG namespace key. Required
                            when kg is provided.
        """
        if not messages:
            return ""

        recent = messages[-max_messages:]
        lines = []
        for m in recent:
            role = str(m.get("role", "?")).upper()
            body = str(m.get("content", ""))[:200]
            lines.append(role + ": " + body)
        conversation_text = "\n".join(lines)[:MAX_CONV_CHARS]

        prompt = _SUMMARIZE_PROMPT.format(
            conversation=conversation_text,
            existing_memory=existing_memory[:500] if existing_memory else "(empty)",
        )

        result = await self._call_llm(prompt, max_tokens=200)

        # Validate LLM output before trusting it.
        if not result or not _looks_like_summary(result):
            logger.warning(
                "MemorySummarizer: LLM output failed validation for %s, using fallback",
                agent_id,
            )
            return self._fallback_summarize(agent_id, messages)

        date_str = time.strftime("%Y-%m-%d")
        patch = "\n## Session " + date_str + " [" + agent_id + "]\n" + result.strip() + "\n"
        logger.info("MemorySummarizer: session summarized for %s (%d chars)", agent_id, len(patch))

        # Populate KG with entities and facts extracted from this summary.
        if kg is not None and (jid or agent_id):
            await self.extract_entities_from_summary(result.strip(), jid or agent_id, kg)

        return patch

    async def extract_entities_from_summary(self, summary_text: str, jid: str, kg) -> None:
        """
        Call LLM to extract entities and facts from a session summary.
        Populates the EvoKnowledgeGraph. Errors are logged, never raised.
        """
        if not summary_text.strip():
            return

        prompt = f"""Extract entities and facts from this text as JSON only (no markdown):
{{
  "entities": [{{"name": "string", "type": "person|place|project|tool|concept"}}],
  "facts": [{{"subject": "string", "predicate": "string", "object": "string", "confidence": 0.0-1.0}}]
}}

Text:
{summary_text[:2000]}"""

        try:
            response = await self._call_llm(prompt, max_tokens=400)
            if not response:
                logger.warning("KG: entity extraction got no LLM response for %s", jid)
                return
            data = json.loads(response)

            for entity in data.get("entities", []):
                kg.add_entity(entity["name"], entity.get("type", "general"), jid)

            for fact in data.get("facts", []):
                kg.add_triple(
                    fact["subject"], fact["predicate"], fact["object"], jid,
                    confidence=float(fact.get("confidence", 0.8))
                )
            logger.info("KG: extracted %d entities, %d facts from summary for %s",
                        len(data.get("entities", [])), len(data.get("facts", [])), jid)
        except json.JSONDecodeError as e:
            logger.warning("KG: entity extraction JSON parse failed: %s", e)
        except Exception as e:
            logger.warning("KG: entity extraction failed: %s", e)

    async def compress_memory(
        self,
        agent_id: str,
        memory_content: str,
        target_bytes: int = 4096,
    ) -> str:
        """
        Compress MEMORY.md when it approaches the size limit.

        Bug fixed (p14b-13): the previous code truncated the prompt input to
        3000 chars (``memory_content[:3000]``) but then returned the LLM's
        output as the *entire* replacement for the original content.  This
        silently discarded every byte past the 3000-char slice — the LLM
        never saw the tail of the memory, so it could not have preserved it.
        We now pass the full content to the LLM (up to ``MAX_MEMORY_BYTES``
        which is the hard cap) and only fall back to truncation when the LLM
        is unavailable.

        Bug fixed (p14b-14): the compressed result is now validated.  If the
        LLM returns something larger than the original or suspiciously small
        (< 10 chars) we fall back to tail-truncation rather than storing the
        bad output.
        """
        current_size = len(memory_content.encode("utf-8"))
        if current_size < COMPRESS_THRESHOLD:
            return memory_content

        max_words = target_bytes // 6
        # Pass the full memory content — do NOT truncate the input here.
        # The prompt token limit is handled by the LLM itself; we send up to
        # MAX_MEMORY_BYTES worth of content which is only 8 KB.
        prompt = _COMPRESS_PROMPT.format(
            memory=memory_content[:MAX_MEMORY_BYTES],
            max_words=max_words,
        )

        result = await self._call_llm(prompt, max_tokens=600)
        if not result:
            logger.warning("MemorySummarizer: LLM unavailable for %s, truncating", agent_id)
            return self._truncate_memory(memory_content, target_bytes)

        compressed = result.strip()
        new_size = len(compressed.encode("utf-8"))

        # Validate: reject if suspiciously empty or larger than the original.
        if new_size < 10 or new_size >= current_size:
            logger.warning(
                "MemorySummarizer: compress output invalid for %s "
                "(original=%d bytes, compressed=%d bytes), truncating instead",
                agent_id, current_size, new_size,
            )
            return self._truncate_memory(memory_content, target_bytes)

        reduction = 100 * (1 - new_size / max(current_size, 1))
        logger.info(
            "MemorySummarizer: compressed %s memory %d->%d bytes (%.0f%% reduction)",
            agent_id, current_size, new_size, reduction,
        )
        return compressed

    async def should_compress(self, memory_content: str) -> bool:
        """Check if memory content needs compression."""
        return len(memory_content.encode("utf-8")) >= COMPRESS_THRESHOLD

    # ── LLM backends ─────────────────────────────────────────────────────

    async def _call_llm(self, prompt: str, max_tokens: int = 300) -> Optional[str]:
        """Try available LLM backends in order. Returns None if all fail."""
        if self._google_key:
            result = await self._call_gemini(prompt, max_tokens)
            if result:
                return result
        if self._anthropic_key:
            result = await self._call_claude(prompt, max_tokens)
            if result:
                return result
        if self._openai_key or self._openai_base:
            result = await self._call_openai(prompt, max_tokens)
            if result:
                return result
        logger.warning("MemorySummarizer: no LLM available")
        return None

    async def _call_gemini(self, prompt: str, max_tokens: int) -> Optional[str]:
        try:
            import urllib.request
            import json as _json
            import asyncio
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-2.0-flash:generateContent?key=" + self._google_key
            )
            payload = _json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1},
            }).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=10)
            )
            data = _json.loads(response.read())
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:
            logger.debug("Gemini summarization failed: %s", exc)
            return None

    async def _call_claude(self, prompt: str, max_tokens: int) -> Optional[str]:
        try:
            import urllib.request
            import json as _json
            import asyncio
            payload = _json.dumps({
                "model": "claude-3-haiku-20240307",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self._anthropic_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=10)
            )
            data = _json.loads(response.read())
            return data["content"][0]["text"]
        except Exception as exc:
            logger.debug("Claude summarization failed: %s", exc)
            return None

    async def _call_openai(self, prompt: str, max_tokens: int) -> Optional[str]:
        try:
            import urllib.request
            import json as _json
            import asyncio
            base = self._openai_base or "https://api.openai.com"
            payload = _json.dumps({
                "model": "gpt-4o-mini",
                "max_tokens": max_tokens,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": prompt}],
            }).encode()
            req = urllib.request.Request(
                base + "/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self._openai_key,
                },
                method="POST",
            )
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=10)
            )
            data = _json.loads(response.read())
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.debug("OpenAI summarization failed: %s", exc)
            return None

    # ── Fallbacks ─────────────────────────────────────────────────────────

    def _fallback_summarize(self, agent_id: str, messages: list) -> str:
        """Stub entry when LLM is unavailable."""
        date_str = time.strftime("%Y-%m-%d %H:%M")
        n_msgs = len(messages)
        return (
            "\n## Session " + date_str + "\n"
            "- [" + str(n_msgs) + " messages — LLM summarization unavailable]\n"
        )

    @staticmethod
    def _truncate_memory(content: str, target_bytes: int) -> str:
        """Keep the most recent content when truncating."""
        encoded = content.encode("utf-8")
        if len(encoded) <= target_bytes:
            return content
        truncated = encoded[-target_bytes:].decode("utf-8", errors="ignore")
        nl = truncated.find("\n")
        if nl > 0:
            truncated = truncated[nl:]
        return "<!-- memory compressed -->\n" + truncated

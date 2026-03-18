"""
Memory Summarizer — Phase 2 of UnifiedClaw architecture

Automatically compresses conversation history into MEMORY.md hot memory.

Two main operations:
1. summarize_session(): After a conversation ends, summarize key learnings
2. compress_memory(): When MEMORY.md approaches size limit, compress it

Uses the same LLM provider as the agent (Gemini by default).

Usage:
    summarizer = MemorySummarizer(api_key=os.environ["GOOGLE_API_KEY"])
    
    # After session ends
    patch = await summarizer.summarize_session(
        agent_id="mybot",
        messages=[{"role": "user", "content": "..."}, ...],
        existing_memory="..."
    )
    await memory_bus.patch_hot_memory(agent_id, patch)
    
    # Compress when MEMORY.md is getting large
    compressed = await summarizer.compress_memory(agent_id, memory_content)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Prompt for session summarization
_SUMMARIZE_PROMPT = """You are a memory distillation system. Your job is to extract key facts from a conversation and format them as concise bullet points for a persistent memory file.

Rules:
- Extract only genuinely useful, long-term facts (preferences, decisions, important context)
- Skip trivial exchanges and greetings
- Be extremely concise (each bullet <= 15 words)
- Use format: "* [fact]" one per line
- Maximum 5 bullet points per session
- If nothing important happened, output: "* [no significant new information]"

Conversation to summarize:
{conversation}

Existing memory (for deduplication -- don't repeat what's already there):
{existing_memory}

Output ONLY the bullet points, nothing else:"""

# Prompt for memory compression
_COMPRESS_PROMPT = """You are a memory compression system. Compress this memory file to under {max_words} words while preserving all important facts.

Rules:
- Merge duplicate or redundant facts
- Remove outdated information (marked with old dates)
- Keep the most important context
- Maintain bullet point format
- Preserve specific facts (names, decisions, preferences, technical details)

Memory to compress:
{memory}

Output the compressed memory ONLY, no explanation:"""


class MemorySummarizer:
    """
    LLM-powered memory summarization and compression.
    
    Supports Gemini (default), Claude, and OpenAI-compatible APIs.
    Falls back gracefully to a simple keyword extraction if LLM unavailable.
    """

    MAX_MEMORY_BYTES = 8192   # 8KB hot memory limit
    COMPRESS_THRESHOLD = 6144  # Start compressing at 6KB (75% full)
    MAX_CONVERSATION_CHARS = 4000  # Truncate long conversations before sending to LLM

    def __init__(
        self,
        google_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
    ):
        self._google_key = google_api_key or os.environ.get("GOOGLE_API_KEY", "").split(",")[0].strip()
        self._anthropic_key = anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._openai_key = openai_api_key or os.environ.get("OPENAI_API_KEY", "")
        self._openai_base = openai_base_url or os.environ.get("OPENAI_BASE_URL", "")

    async def summarize_session(
        self,
        agent_id: str,
        messages: list[dict],
        existing_memory: str = "",
        max_messages: int = 20,
    ) -> str:
        """
        Summarize a conversation session into bullet points for MEMORY.md.
        
        Args:
            agent_id:        Agent identifier (for logging)
            messages:        List of {"role": "user"/"assistant", "content": "..."}
            existing_memory: Current MEMORY.md content (for deduplication)
            max_messages:    Maximum recent messages to consider
            
        Returns:
            Patch string to append to MEMORY.md, or empty string if nothing to add.
        """
        if not messages:
            return ""

        # Format conversation
        recent = messages[-max_messages:]
        conversation_text = "
".join(
            f"{m.get('role', '?').upper()}: {str(m.get('content', ''))[:200]}"
            for m in recent
        )[:self.MAX_CONVERSATION_CHARS]

        prompt = _SUMMARIZE_PROMPT.format(
            conversation=conversation_text,
            existing_memory=existing_memory[:500] if existing_memory else "(empty)",
        )

        result = await self._call_llm(prompt, max_tokens=200)
        if not result:
            return self._fallback_summarize(messages)

        # Format as dated patch
        date_str = time.strftime("%Y-%m-%d")
        patch = f"
## Session {date_str} [{agent_id}]
{result.strip()}
"
        logger.info(f"MemorySummarizer: session summarized for {agent_id} ({len(patch)} chars)")
        return patch

    async def compress_memory(
        self,
        agent_id: str,
        memory_content: str,
        target_bytes: int = 4096,
    ) -> str:
        """
        Compress MEMORY.md content when it approaches the size limit.
        
        Args:
            agent_id:       Agent identifier (for logging)
            memory_content: Current full MEMORY.md content
            target_bytes:   Target size after compression
            
        Returns:
            Compressed memory content.
        """
        current_size = len(memory_content.encode("utf-8"))
        if current_size < self.COMPRESS_THRESHOLD:
            return memory_content  # No compression needed

        max_words = target_bytes // 6  # Rough estimate: 6 bytes per word average
        prompt = _COMPRESS_PROMPT.format(
            memory=memory_content[:3000],
            max_words=max_words,
        )

        result = await self._call_llm(prompt, max_tokens=600)
        if not result:
            # Fallback: simple truncation keeping recent content
            logger.warning(f"MemorySummarizer: LLM compression failed for {agent_id}, using truncation")
            return self._truncate_memory(memory_content, target_bytes)

        compressed = result.strip()
        new_size = len(compressed.encode("utf-8"))
        logger.info(
            f"MemorySummarizer: compressed {agent_id} memory "
            f"{current_size}>{new_size} bytes "
            f"({100*(1-new_size/current_size):.0f}% reduction)"
        )
        return compressed

    async def should_compress(self, memory_content: str) -> bool:
        """Check if memory content needs compression."""
        return len(memory_content.encode("utf-8")) >= self.COMPRESS_THRESHOLD

    async def _call_llm(self, prompt: str, max_tokens: int = 300) -> Optional[str]:
        """Call available LLM API. Returns None if all fail."""
        # Try Gemini first (free tier)
        if self._google_key:
            result = await self._call_gemini(prompt, max_tokens)
            if result:
                return result

        # Try Claude
        if self._anthropic_key:
            result = await self._call_claude(prompt, max_tokens)
            if result:
                return result

        # Try OpenAI-compatible
        if self._openai_key or self._openai_base:
            result = await self._call_openai(prompt, max_tokens)
            if result:
                return result

        logger.warning("MemorySummarizer: no LLM available for summarization")
        return None

    async def _call_gemini(self, prompt: str, max_tokens: int) -> Optional[str]:
        """Call Gemini Flash API."""
        try:
            import urllib.request
            import json as _json
            import asyncio

            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={self._google_key}"
            )
            payload = _json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1},
            }).encode()

            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=10)
            )
            data = _json.loads(response.read())
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            logger.debug(f"Gemini summarization failed: {e}")
            return None

    async def _call_claude(self, prompt: str, max_tokens: int) -> Optional[str]:
        """Call Claude API."""
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
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=10)
            )
            data = _json.loads(response.read())
            return data["content"][0]["text"]
        except Exception as e:
            logger.debug(f"Claude summarization failed: {e}")
            return None

    async def _call_openai(self, prompt: str, max_tokens: int) -> Optional[str]:
        """Call OpenAI-compatible API."""
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
                f"{base}/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._openai_key}",
                },
                method="POST",
            )
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=10)
            )
            data = _json.loads(response.read())
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            logger.debug(f"OpenAI summarization failed: {e}")
            return None

    def _fallback_summarize(self, messages: list[dict]) -> str:
        """Simple keyword-based fallback when LLM is unavailable."""
        # Just note that a session occurred
        date_str = time.strftime("%Y-%m-%d %H:%M")
        n_msgs = len(messages)
        return f"
## Session {date_str}
* [{n_msgs} messages exchanged -- LLM summarization unavailable]
"

    @staticmethod
    def _truncate_memory(content: str, target_bytes: int) -> str:
        """Keep the most recent content when truncating."""
        encoded = content.encode("utf-8")
        if len(encoded) <= target_bytes:
            return content
        # Keep last target_bytes worth of content
        truncated = encoded[-target_bytes:].decode("utf-8", errors="ignore")
        # Try to start at a newline boundary
        newline_pos = truncated.find("
")
        if newline_pos > 0:
            truncated = truncated[newline_pos:]
        return "<!-- memory compressed -->
" + truncated

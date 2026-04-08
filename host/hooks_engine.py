"""
hooks_engine.py — Full Hook Lifecycle System for evoclaw
Ported from Claude Code Extended Source (hooks.ts + hooksConfigManager.ts)

Hook Events:
  PreToolUse       — Before tool execution; exit 2 = block tool call
  PostToolUse      — After tool result; exit 2 = show stderr to model
  Stop             — Before session ends; exit 2 = force model to continue
  UserPromptSubmit — Before processing user prompt; exit 2 = block & erase
  PreCompact       — Before context compaction; exit 2 = block compaction
  PostCompact      — After compaction completes (informational)
  SessionStart     — On session initialization
  SessionEnd       — On session teardown

Exit Code Semantics (from hooksConfigManager.ts lines 26-153):
  0  → Success; use stdout for context injection
  2  → Blocking: show stderr to model; block tool/prompt/compaction
  N  → Non-blocking error: show stderr to user only; continue

Author: evoclaw harness engineering (2026-04-02)
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import logging
log = logging.getLogger(__name__)


# ── Exit code constants ──────────────────────────────────────────────────────

EXIT_SUCCESS = 0        # Continue; stdout used as context
EXIT_BLOCK = 2          # Blocking: stderr → model; block operation
# Other exit codes → non-blocking: stderr → user only


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class HookDefinition:
    """Single hook command definition."""
    type: str                   # 'command' | 'prompt' | 'http'
    command: str = ""           # Shell command to run (type=command)
    timeout: int = 30           # Seconds
    shell: str = "bash"
    async_run: bool = False     # Fire-and-forget
    once: bool = False          # Remove after first run
    matcher: str = ""           # Optional tool name filter (e.g. "Bash", "Write")
    prompt: str = ""            # LLM prompt (type=prompt, future)
    url: str = ""               # HTTP endpoint (type=http, future)


@dataclass
class HookMatcher:
    """Hook matcher: optional tool filter + list of hook definitions."""
    matcher: str = ""           # Tool/event filter; empty = match all
    hooks: list[HookDefinition] = field(default_factory=list)


@dataclass
class HookResult:
    """Result from a single hook execution."""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    hook_name: str = ""
    command: str = ""
    # Parsed JSON fields
    continue_: bool = True          # JSON: "continue": false → stop processing
    decision: str = ""              # JSON: "approve" | "block"
    reason: str = ""                # JSON: "reason"
    updated_input: Optional[dict] = None   # JSON: hookSpecificOutput.updatedInput
    additional_context: str = ""           # JSON: hookSpecificOutput.additionalContext
    permission_decision: str = ""          # JSON: "allow" | "deny" | "ask"
    new_custom_instructions: str = ""      # PreCompact: stdout → instructions
    updated_mcp_output: Any = None         # PostToolUse: replace MCP output


@dataclass
class AggregatedHookResult:
    """Aggregated results from all hooks for an event."""
    blocking_errors: list[str] = field(default_factory=list)
    prevent_continuation: bool = False
    stop_reason: str = ""
    permission_behavior: str = ""    # "allow" | "deny" | "ask"
    additional_contexts: list[str] = field(default_factory=list)
    updated_input: Optional[dict] = None
    updated_mcp_output: Any = None
    new_custom_instructions: str = ""

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocking_errors) or self.prevent_continuation


# ── Hook Registry ─────────────────────────────────────────────────────────────

class HookRegistry:
    """
    Load and store hook definitions from config.

    Config format (JSON or dict):
    {
      "PreToolUse": [
        { "matcher": "Bash", "hooks": [{"type": "command", "command": "validate.sh"}] }
      ],
      "Stop": [
        { "hooks": [{"type": "command", "command": "format.sh", "timeout": 60}] }
      ]
    }
    """

    VALID_EVENTS = {
        "PreToolUse", "PostToolUse", "Stop", "StopFailure",
        "UserPromptSubmit", "PreCompact", "PostCompact",
        "SessionStart", "SessionEnd",
    }

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookMatcher]] = {}

    def load_from_dict(self, config: dict) -> None:
        """Load hooks from a config dictionary."""
        for event, matchers in config.items():
            if event not in self.VALID_EVENTS:
                log.warning("hooks_engine: unknown hook event %r — skipping", event)
                continue
            if not isinstance(matchers, list):
                continue
            parsed: list[HookMatcher] = []
            for m in matchers:
                if not isinstance(m, dict):
                    continue
                matcher_str = m.get("matcher", "")
                hook_defs: list[HookDefinition] = []
                for h in m.get("hooks", []):
                    if not isinstance(h, dict):
                        continue
                    hook_defs.append(HookDefinition(
                        type=h.get("type", "command"),
                        command=h.get("command", ""),
                        timeout=int(h.get("timeout", 30)),
                        shell=h.get("shell", "bash"),
                        async_run=bool(h.get("async", False)),
                        once=bool(h.get("once", False)),
                        matcher=matcher_str,
                        prompt=h.get("prompt", ""),
                        url=h.get("url", ""),
                    ))
                parsed.append(HookMatcher(matcher=matcher_str, hooks=hook_defs))
            self._hooks[event] = parsed

    def load_from_file(self, path: str | Path) -> None:
        """Load hooks from a JSON file."""
        p = Path(path)
        if not p.exists():
            return
        try:
            config = json.loads(p.read_text(encoding="utf-8"))
            self.load_from_dict(config)
            log.info("hooks_engine: loaded hooks from %s", path)
        except Exception as exc:
            log.warning("hooks_engine: failed to load hooks from %s: %s", path, exc)

    def get_matchers(self, event: str) -> list[HookMatcher]:
        return self._hooks.get(event, [])

    def has_hooks(self, event: str) -> bool:
        return bool(self._hooks.get(event))


# Global default registry (can be replaced)
_registry: HookRegistry = HookRegistry()


def get_registry() -> HookRegistry:
    return _registry


def set_registry(registry: HookRegistry) -> None:
    global _registry
    _registry = registry


# ── Core hook executor ────────────────────────────────────────────────────────

async def _run_hook_command(
    hook: HookDefinition,
    hook_input: dict,
    hook_name: str,
    env_extra: Optional[dict] = None,
) -> HookResult:
    """
    Execute a single hook command. Serialize hook_input as JSON to stdin.
    Returns HookResult with parsed exit code, stdout, stderr, and JSON fields.
    """
    if not hook.command:
        return HookResult(hook_name=hook_name, command=hook.command)

    t0 = time.monotonic()
    env = {**os.environ, **(env_extra or {})}

    try:
        proc = await asyncio.create_subprocess_shell(
            hook.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdin_data = json.dumps(hook_input).encode()
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(stdin_data),
                timeout=hook.timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("hooks_engine: hook %r timed out after %ds", hook_name, hook.timeout)
            return HookResult(
                exit_code=1,
                stderr=f"Hook timed out after {hook.timeout}s",
                hook_name=hook_name,
                command=hook.command,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        exit_code = proc.returncode or 0
        stdout = stdout_b.decode(errors="replace").strip()
        stderr = stderr_b.decode(errors="replace").strip()
        duration_ms = int((time.monotonic() - t0) * 1000)

        result = HookResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            hook_name=hook_name,
            command=hook.command,
        )

        # Parse JSON from stdout if available
        if stdout:
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, dict):
                    result.continue_ = parsed.get("continue", True)
                    result.decision = parsed.get("decision", "")
                    result.reason = parsed.get("reason", "")
                    specific = parsed.get("hookSpecificOutput", {})
                    if isinstance(specific, dict):
                        result.updated_input = specific.get("updatedInput")
                        result.additional_context = specific.get("additionalContext", "")
                        result.permission_decision = specific.get("permissionDecision", "")
                        result.new_custom_instructions = specific.get("newCustomInstructions", "")
                        result.updated_mcp_output = specific.get("updatedMCPToolOutput")
            except json.JSONDecodeError:
                # stdout is plain text context (exit 0)
                pass

        log.debug(
            "hooks_engine: hook=%r cmd=%r exit=%d dur=%dms",
            hook_name, hook.command, exit_code, duration_ms,
        )
        return result

    except Exception as exc:
        log.warning("hooks_engine: hook %r raised: %s", hook_name, exc)
        return HookResult(
            exit_code=1,
            stderr=str(exc),
            hook_name=hook_name,
            command=hook.command,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )


def _matches_tool(matcher: str, tool_name: str) -> bool:
    """Check if a hook matcher applies to the given tool name."""
    if not matcher:
        return True  # Empty matcher = match all
    # Support glob-like "Bash(git *)" → extract tool part
    base = matcher.split("(")[0].strip()
    return base == tool_name or base == "*"


async def _execute_event_hooks(
    event: str,
    hook_input: dict,
    tool_name: str = "",
    registry: Optional[HookRegistry] = None,
    env_extra: Optional[dict] = None,
) -> AggregatedHookResult:
    """
    Execute all hooks for a given event, aggregating results.
    Runs matching hooks concurrently; non-async hooks run with gather.
    """
    reg = registry or _registry
    matchers = reg.get_matchers(event)
    agg = AggregatedHookResult()

    if not matchers:
        return agg

    # Filter matchers by tool name
    applicable: list[tuple[HookMatcher, HookDefinition]] = []
    for m in matchers:
        if not _matches_tool(m.matcher, tool_name):
            continue
        for h in m.hooks:
            if h.type == "command":
                applicable.append((m, h))
            # TODO: type=prompt → LLM evaluation, type=http → HTTP POST

    if not applicable:
        return agg

    # Run all applicable hooks concurrently
    hook_name_fmt = f"{event}:{tool_name}" if tool_name else event
    tasks = [
        _run_hook_command(h, hook_input, hook_name_fmt, env_extra)
        for _, h in applicable
        if not h.async_run
    ]

    # Fire-and-forget async hooks
    for _, h in applicable:
        if h.async_run:
            def _hook_task_done(t):
                if not t.cancelled() and t.exception():
                    log.error("async hook '%s' failed: %s", hook_name_fmt, t.exception())
            task = asyncio.create_task(_run_hook_command(h, hook_input, hook_name_fmt, env_extra))
            task.add_done_callback(_hook_task_done)

    results: list[HookResult] = await asyncio.gather(*tasks, return_exceptions=False)

    for result in results:
        if result.exit_code == EXIT_BLOCK:
            # Blocking error: stderr shown to model; operation blocked
            err_msg = result.stderr or f"Hook {result.hook_name!r} blocked (exit 2)"
            agg.blocking_errors.append(err_msg)
            log.info("hooks_engine: %s blocked by hook %r: %s", event, result.hook_name, err_msg)

        elif result.exit_code != EXIT_SUCCESS:
            # Non-blocking: log to user only
            log.warning("hooks_engine: hook %r non-blocking error (exit %d): %s",
                        result.hook_name, result.exit_code, result.stderr)

        # Aggregate JSON fields from successful/non-blocking hooks
        if not result.continue_:
            agg.prevent_continuation = True
            if result.reason:
                agg.stop_reason = result.reason

        if result.decision == "block":
            agg.blocking_errors.append(result.reason or f"Blocked by {result.hook_name!r}")
            agg.permission_behavior = "deny"
        elif result.decision == "approve":
            agg.permission_behavior = "allow"

        if result.additional_context:
            agg.additional_contexts.append(result.additional_context)

        if result.updated_input is not None:
            agg.updated_input = result.updated_input

        if result.updated_mcp_output is not None:
            agg.updated_mcp_output = result.updated_mcp_output

        if result.new_custom_instructions:
            agg.new_custom_instructions = result.new_custom_instructions

        if result.permission_decision:
            agg.permission_behavior = result.permission_decision

    return agg


# ── Public API ────────────────────────────────────────────────────────────────

async def run_pre_tool_use(
    tool_name: str,
    tool_input: dict,
    tool_use_id: str = "",
    session_id: str = "",
    registry: Optional[HookRegistry] = None,
    env_extra: Optional[dict] = None,
) -> AggregatedHookResult:
    """
    PreToolUse hook — fires before tool execution.
    Exit 2 → tool call is blocked; blocking_errors contains reason for model.
    Returns AggregatedHookResult; check .is_blocked before executing.
    """
    hook_input = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
        "session_id": session_id,
    }
    result = await _execute_event_hooks(
        "PreToolUse", hook_input, tool_name=tool_name,
        registry=registry, env_extra=env_extra,
    )
    return result


async def run_post_tool_use(
    tool_name: str,
    tool_input: dict,
    tool_response: Any,
    tool_use_id: str = "",
    session_id: str = "",
    registry: Optional[HookRegistry] = None,
    env_extra: Optional[dict] = None,
) -> AggregatedHookResult:
    """
    PostToolUse hook — fires after tool execution completes.
    Exit 2 → stderr shown to model (blocking pattern).
    Can modify MCP tool output via hookSpecificOutput.updatedMCPToolOutput.
    """
    hook_input = {
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": tool_response if isinstance(tool_response, (str, dict, list)) else str(tool_response),
        "tool_use_id": tool_use_id,
        "session_id": session_id,
    }
    result = await _execute_event_hooks(
        "PostToolUse", hook_input, tool_name=tool_name,
        registry=registry, env_extra=env_extra,
    )
    return result


async def run_stop_hooks(
    last_assistant_message: str = "",
    stop_hook_active: bool = False,
    session_id: str = "",
    registry: Optional[HookRegistry] = None,
    env_extra: Optional[dict] = None,
) -> AggregatedHookResult:
    """
    Stop hook — fires right before Claude concludes its response.
    Exit 2 → stderr shown to model; conversation continues.
    Use for: code formatting, final validation, cost reporting.
    """
    hook_input = {
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
        "last_assistant_message": last_assistant_message,
        "session_id": session_id,
    }
    result = await _execute_event_hooks(
        "Stop", hook_input,
        registry=registry, env_extra=env_extra,
    )
    return result


async def run_user_prompt_submit(
    prompt: str,
    session_id: str = "",
    registry: Optional[HookRegistry] = None,
    env_extra: Optional[dict] = None,
) -> AggregatedHookResult:
    """
    UserPromptSubmit hook — fires when user submits a prompt.
    Exit 2 → block processing; erase original prompt; show stderr to user.
    Exit 0 → stdout shown to Claude as additional context.
    """
    hook_input = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
        "session_id": session_id,
    }
    result = await _execute_event_hooks(
        "UserPromptSubmit", hook_input,
        registry=registry, env_extra=env_extra,
    )
    return result


async def run_pre_compact(
    trigger: str = "auto",
    custom_instructions: Optional[str] = None,
    session_id: str = "",
    registry: Optional[HookRegistry] = None,
    env_extra: Optional[dict] = None,
) -> AggregatedHookResult:
    """
    PreCompact hook — fires before context compaction.
    Exit 0 → stdout appended as custom compact instructions.
    Exit 2 → block compaction entirely.
    """
    hook_input = {
        "hook_event_name": "PreCompact",
        "trigger": trigger,
        "custom_instructions": custom_instructions,
        "session_id": session_id,
    }
    result = await _execute_event_hooks(
        "PreCompact", hook_input,
        registry=registry, env_extra=env_extra,
    )
    return result


async def run_post_compact(
    trigger: str = "auto",
    compact_summary: str = "",
    session_id: str = "",
    registry: Optional[HookRegistry] = None,
    env_extra: Optional[dict] = None,
) -> AggregatedHookResult:
    """
    PostCompact hook — fires after compaction completes (informational).
    """
    hook_input = {
        "hook_event_name": "PostCompact",
        "trigger": trigger,
        "compact_summary": compact_summary,
        "session_id": session_id,
    }
    result = await _execute_event_hooks(
        "PostCompact", hook_input,
        registry=registry, env_extra=env_extra,
    )
    return result


async def run_session_start(
    session_id: str = "",
    registry: Optional[HookRegistry] = None,
    env_extra: Optional[dict] = None,
) -> AggregatedHookResult:
    """SessionStart hook — fires on session initialization."""
    hook_input = {"hook_event_name": "SessionStart", "session_id": session_id}
    return await _execute_event_hooks("SessionStart", hook_input,
                                      registry=registry, env_extra=env_extra)


async def run_session_end(
    session_id: str = "",
    exit_reason: str = "",
    registry: Optional[HookRegistry] = None,
    env_extra: Optional[dict] = None,
) -> AggregatedHookResult:
    """SessionEnd hook — fires on session teardown (fire-and-forget safe)."""
    hook_input = {
        "hook_event_name": "SessionEnd",
        "session_id": session_id,
        "exit_reason": exit_reason,
    }
    return await _execute_event_hooks("SessionEnd", hook_input,
                                      registry=registry, env_extra=env_extra)


# ── Convenience: format blocking errors for LLM ──────────────────────────────

def format_hook_block_for_model(result: AggregatedHookResult, tool_name: str) -> str:
    """Format a blocked hook result as a tool_result error message for the model."""
    errors = "\n".join(f"- {e}" for e in result.blocking_errors)
    return (
        f"【HOOK BLOCKED】Tool call '{tool_name}' was blocked by a PreToolUse hook.\n"
        f"Reason:\n{errors}\n\n"
        "Please adjust your approach and try a different method."
    )


def format_stop_hook_feedback(result: AggregatedHookResult) -> str:
    """Format stop hook blocking errors as a system message for the model."""
    errors = "\n".join(f"- {e}" for e in result.blocking_errors)
    return (
        f"【STOP HOOK】Before concluding, address the following:\n{errors}\n\n"
        "Please fix these issues and respond again."
    )


# ── Load hooks from environment / CLAUDE.md ──────────────────────────────────

def load_hooks_from_env(group_folder: Optional[str] = None) -> HookRegistry:
    """
    Load hook definitions from:
    1. EVOCLAW_HOOKS_CONFIG env var (JSON string)
    2. {group_folder}/.claude/hooks.json file
    3. {group_folder}/hooks.json file
    """
    registry = HookRegistry()

    # 1. Env var
    env_config = os.environ.get("EVOCLAW_HOOKS_CONFIG", "")
    if env_config:
        try:
            config = json.loads(env_config)
            registry.load_from_dict(config)
            log.info("hooks_engine: loaded hooks from EVOCLAW_HOOKS_CONFIG env var")
        except Exception as exc:
            log.warning("hooks_engine: failed to parse EVOCLAW_HOOKS_CONFIG: %s", exc)

    # 2. Group folder hooks files
    if group_folder:
        for candidate in [
            Path(group_folder) / ".claude" / "hooks.json",
            Path(group_folder) / "hooks.json",
        ]:
            if candidate.exists():
                registry.load_from_file(candidate)
                break

    return registry

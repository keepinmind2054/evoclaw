"""Anthropic Claude agentic loop for the EvoClaw agent runner."""
import json, os, time, random, uuid, traceback
from pathlib import Path

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from _constants import (
    _MAX_TOOL_RESULT_CHARS, _MAX_HISTORY_MESSAGES,
)
from _utils import _log, _llm_call_with_retry, _KeyPool
from _constants import _ACTION_CLAIM_RE
from _tools import _messages_sent_via_tool
# execute_tool will be imported lazily to avoid circular imports:
# from _registry import execute_tool

WORKSPACE = "/workspace/group"

CLAUDE_TOOL_DECLARATIONS = [
    {"name": "Bash", "description": "Execute a bash command in /workspace/group.", "input_schema": {"type": "object", "properties": {"command": {"type": "string", "description": "The bash command to run"}}, "required": ["command"]}},
    {"name": "Read", "description": "Read a file from the filesystem.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string", "description": "Absolute path to the file"}}, "required": ["file_path"]}},
    {"name": "Write", "description": "Write content to a file.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}},
    {"name": "Edit", "description": "Find and replace a string in a file.", "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["file_path", "old_string", "new_string"]}},
    {"name": "mcp__evoclaw__send_message", "description": "Send a message to the user.", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}, "sender": {"type": "string"}}, "required": ["text"]}},
    {"name": "mcp__evoclaw__schedule_task", "description": "Schedule a task.", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "schedule_type": {"type": "string"}, "schedule_value": {"type": "string"}, "context_mode": {"type": "string"}}, "required": ["prompt", "schedule_type", "schedule_value"]}},
    {"name": "mcp__evoclaw__list_tasks", "description": "List all scheduled tasks for this group.", "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "mcp__evoclaw__cancel_task", "description": "Cancel (delete) a scheduled task by its ID.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "mcp__evoclaw__pause_task", "description": "Pause a scheduled task (it will not run until resumed).", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to pause"}}, "required": ["task_id"]}},
    {"name": "mcp__evoclaw__resume_task", "description": "Resume a previously paused scheduled task.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "string", "description": "The task ID to resume"}}, "required": ["task_id"]}},
    {"name": "Glob", "description": "Find files matching a glob pattern (supports ** recursive).", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "Grep", "description": "Search file contents with regex. Returns filename:line:content.", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "WebFetch", "description": "Fetch a URL and return its content as plain text.", "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "mcp__evoclaw__run_agent", "description": "Spawn a subagent in an isolated Docker container to handle a subtask. Blocks until complete (up to 300s) and returns its output.", "input_schema": {"type": "object", "properties": {"prompt": {"type": "string", "description": "The task for the subagent"}, "context_mode": {"type": "string", "description": "isolated or group"}}, "required": ["prompt"]}},
    {"name": "mcp__evoclaw__send_file", "description": "Send a file to the user. Write the file to /workspace/group/output/ first, then call this tool.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string", "description": "The chat JID to send the file to"}, "file_path": {"type": "string", "description": "Absolute container path to the file"}, "caption": {"type": "string", "description": "Optional caption"}}, "required": ["file_path"]}},
    {"name": "mcp__evoclaw__reset_group", "description": "Clear the failure counter for a group, unfreezing it if it was locked in cooldown. Use when a group is stuck and not responding.", "input_schema": {"type": "object", "properties": {"jid": {"type": "string", "description": "The JID of the group to reset, e.g. tg:8259652816"}}, "required": ["jid"]}},
    {"name": "mcp__evoclaw__start_remote_control", "description": "Start a Claude Code remote-control session. The host spawns `claude remote-control` and sends the URL back to this chat. Use when the user wants to update code or restart EvoClaw.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string"}, "sender": {"type": "string"}}, "required": []}},
    {"name": "mcp__evoclaw__self_update", "description": "Pull the latest EvoClaw code from git and restart the host process. Use when the user asks to update, upgrade, or restart EvoClaw.", "input_schema": {"type": "object", "properties": {"chat_jid": {"type": "string"}}, "required": []}},
]


def run_agent_claude(client_holder, model: str, system_instruction: str, user_message: str, chat_jid: str, conversation_history: list = None, pool: "_KeyPool | None" = None, apply_key_fn=None, max_iter: int = 20, group_folder: str = "") -> str:
    """
    Anthropic Claude agentic loop.
    client_holder: a one-element list [client] so key rotation can swap the client mid-loop.
    conversation_history: 最近的對話記錄，以原生 multi-turn 格式注入。
    pool/apply_key_fn: optional key pool for automatic rotation on rate-limit errors.
    max_iter: maximum number of agentic loop iterations (default 20; caller sets based on task complexity).
    group_folder: path to the group folder (used for MEMORY.md tracking).
    """
    from _registry import execute_tool  # lazy import to break circular dep
    import re as _re_claude
    messages = []
    # P15A-FIX-8: inject conversation history preserving structured content.
    # The old code used msg.get("content","") which evaluates to "" for list-typed
    # content (tool_use/tool_result blocks), silently dropping those messages.
    # Use the raw content value directly; skip only genuinely empty values.
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str) and not content.strip():
                continue
            if content is not None and content != [] and content != "":
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    MAX_ITER = max_iter
    final_response = ""
    _memory_written = False  # True once agent writes to MEMORY.md this session
    _memory_path_str = f"{WORKSPACE}/MEMORY.md"  # BUG-FIX #424: group_folder is a name, not a path; use WORKSPACE
    _tool_fail_counter: dict = {}  # (tool_name, args_hash) -> consecutive_fail_count
    _MAX_CONSECUTIVE_TOOL_FAILS = 3
    _retry_warning: str = ""  # injected before next LLM call when tool retries detected
    _turns_since_notify = 0   # turns since last mcp__evoclaw__send_message call
    _only_notify_turns = 0    # consecutive turns with ONLY send_message (no real work)
    # Tools that represent actual work (not just reporting)
    _SUBSTANTIVE_TOOLS_CLAUDE = frozenset([
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch",
        "mcp__evoclaw__run_agent",
    ])
    # Extended fake-status regex covering Claude's common hallucination patterns
    # P15A-FIX-1: added English fake-done patterns (previously only in OpenAI loop)
    _FAKE_STATUS_RE = _re_claude.compile(
        r'\*\([^)]*\)\*'           # *(正在執行...)*
        r'|\*\[[^\]]*\]\*'          # *[running...]*
        r'|✅\s*Done'              # ✅ Done
        r'|✅\s*完成'              # ✅ 完成
        r'|【[^】]*(?:已|正在|將|完成|處理|執行)[^】]*】'   # 【已完成】
        r'|（[^）]{2,30}(?:已|正在|處理|執行)[^）]{0,20}）' # （已完成）
        r'|(?:I\s+have\s+(?:completed|finished|executed|run|written))'  # English fake-done
        r'|(?:Task\s+(?:is\s+)?(?:complete|done|finished))'            # Task complete
        r'|(?:Successfully\s+(?:completed|executed|ran|written))',      # Successfully executed
        _re_claude.DOTALL | _re_claude.IGNORECASE,
    )

    for n in range(MAX_ITER):
        _log("🧠 LLM →", f"turn={n} provider=claude")
        _claude_msgs = messages  # capture current snapshot for lambda
        response = _llm_call_with_retry(lambda: client_holder[0].messages.create(
            model=model,
            max_tokens=4096,
            system=system_instruction,
            tools=CLAUDE_TOOL_DECLARATIONS,
            messages=_claude_msgs,
        ), pool=pool, apply_key_fn=apply_key_fn)
        _log("🧠 LLM ←", f"stop={response.stop_reason}")

        # Add assistant response to history
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Collect all text blocks
            # BUG-P26B-1: block.text may be None even when the attribute exists
            # (e.g. a text content block returned with a null value by the API).
            # Joining None would raise TypeError; guard with an explicit None check.
            final_response = " ".join(
                block.text for block in response.content
                if hasattr(block, "text") and block.text is not None
            )
            # ── Fake status detection on end_turn (no tool calls made) ─────────
            _fake_hits = _FAKE_STATUS_RE.findall(final_response)
            if _fake_hits and n < MAX_ITER - 1:
                _log("⚠️ FAKE-STATUS", f"Claude wrote {len(_fake_hits)} fake status indicator(s) without tool calls")
                messages.append({
                    "role": "user",
                    "content": (
                        "【系統警告】你剛才的回覆包含假狀態指示（例如 ✅ Done 或 *(正在執行...)* ），但沒有呼叫任何工具。"
                        "請立刻使用 Bash tool 或其他工具實際執行所需命令，不要只是描述或假裝完成。"
                    ),
                })
                final_response = ""
                continue

            # ── Semantic cross-validation: action claim without any tool call ──
            # If the agent's text claims it completed an action (using common
            # completion verbs) but did NOT call any tools this turn, inject a
            # verification demand.  This catches hallucinations that slip past the
            # syntactic _FAKE_STATUS_RE patterns above.
            _had_tool_calls_this_turn = any(
                hasattr(b, "type") and b.type == "tool_use"
                for b in response.content
            )
            if not _had_tool_calls_this_turn and _ACTION_CLAIM_RE.search(final_response) and n < MAX_ITER - 1:
                _log("⚠️ SEMANTIC-FAKE", "Claude claims action complete but called no tools this turn")
                messages.append({
                    "role": "user",
                    "content": (
                        "【系統驗證】你的回應中聲稱已執行了某項操作，但本輪沒有呼叫任何工具。"
                        "請實際使用對應工具（Read/Write/Edit/Bash）執行並確認，不要只是聲明已完成。"
                    ),
                })
                final_response = ""
                continue
            break

        if response.stop_reason != "tool_use":
            # P15A-FIX-2: handle max_tokens gracefully.
            # If Claude hit max_tokens while mid-tool-call, the assistant message
            # (already appended above) may contain tool_use blocks.  If we simply
            # break, the next API call will fail with a 400 because the history has
            # an assistant tool_use without a matching tool_result.  Detect this
            # case and execute any pending tool_use blocks so history stays valid.
            _pending_tool_uses = [b for b in response.content if hasattr(b, "type") and b.type == "tool_use"]
            if _pending_tool_uses and response.stop_reason == "max_tokens":
                _log("⚠️ MAX-TOKENS", f"Claude hit max_tokens with {len(_pending_tool_uses)} pending tool_use block(s) — executing to keep history valid")
                _partial_results = []
                for _tb in _pending_tool_uses:
                    try:
                        _tr = execute_tool(_tb.name, _tb.input, chat_jid)
                    except Exception as _te:
                        _tr = f"[Tool error: {_te}]"
                    _tr_str = str(_tr)
                    if len(_tr_str) > _MAX_TOOL_RESULT_CHARS:
                        _half = _MAX_TOOL_RESULT_CHARS // 2
                        _head = _tr_str[:_half]
                        _tail = _tr_str[-_half:]
                        _omitted = len(_tr_str) - _MAX_TOOL_RESULT_CHARS
                        _tr_str = _head + f"\n[... {_omitted} chars omitted (middle truncated to preserve head+tail) ...]\n" + _tail
                    _partial_results.append({"type": "tool_result", "tool_use_id": _tb.id, "content": _tr_str})
                messages.append({"role": "user", "content": _partial_results})
            # Unexpected / terminal stop reason — collect text and exit
            # BUG-P26B-1: same None guard as in the end_turn branch above.
            final_response = " ".join(
                block.text for block in response.content
                if hasattr(block, "text") and block.text is not None
            )
            _log("⚠️ UNEXPECTED-STOP", f"Claude stop_reason={response.stop_reason} — exiting loop")
            break

        # Execute all tool calls
        tool_results = []
        _tool_names_this_turn: set = set()
        for block in response.content:
            if block.type == "tool_use":
                _tool_names_this_turn.add(block.name)
                try:
                    result = execute_tool(block.name, block.input, chat_jid)
                except Exception as e:
                    result = f"[Tool error: {e}]"
                    _log("❌ TOOL-EXC", f"Tool {block.name} raised exception: {e}")
                # Truncate large tool results before adding to history
                result_str = str(result)
                if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                    half = _MAX_TOOL_RESULT_CHARS // 2
                    head = result_str[:half]
                    tail = result_str[-half:]
                    omitted = len(result_str) - _MAX_TOOL_RESULT_CHARS
                    result_str = head + f"\n[... {omitted} chars omitted (middle truncated to preserve head+tail) ...]\n" + tail
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })
                # Fix 4 (STABILITY_ANALYSIS 3.5): detect repeated identical-args tool failures
                _fail_key = (block.name, hash(str(block.input)[:200]))
                _is_failure = (result_str.startswith("\u2717") or result_str.startswith("[ERROR]") or result_str.startswith("Error:"))
                if _is_failure:
                    _tool_fail_counter[_fail_key] = _tool_fail_counter.get(_fail_key, 0) + 1
                    if _tool_fail_counter[_fail_key] >= _MAX_CONSECUTIVE_TOOL_FAILS:
                        _retry_warning = (
                            f"【系統警告】工具 `{block.name}` 以相同參數已連續失敗 {_tool_fail_counter[_fail_key]} 次。"
                            f"請立即更換策略：嘗試不同的方法、參數或工具。不要繼續重試相同的失敗操作。"
                        )
                        _log("⚠️ RETRY-LOOP", f"Tool {block.name} failed {_tool_fail_counter[_fail_key]} times consecutively — injecting warning")
                elif result_str.startswith("\u2713") or result_str.startswith("[OK]"):
                    _tool_fail_counter.pop(_fail_key, None)
                # Track MEMORY.md writes
                if not _memory_written and block.name in {"Write", "Edit", "Bash"}:
                    _block_args = str(block.input) if block.input else ""
                    if "MEMORY.md" in _block_args or _memory_path_str in _block_args:
                        _memory_written = True
                        _log("🧠 MEMORY-WRITE", f"Claude updated MEMORY.md via {block.name} on turn {n}")

        # P15A-FIX-3: if stop_reason was tool_use but we found zero tool_use blocks,
        # the response is malformed.  Breaking here leaves the last assistant message
        # (already appended) without a paired tool_result, corrupting history for any
        # future continuation.  Inject a synthetic error tool_result for each tool_use
        # block actually present (if any), then break cleanly.
        if not tool_results:
            _stray_tool_uses = [b for b in response.content if hasattr(b, "type") and b.type == "tool_use"]
            if _stray_tool_uses:
                _log("⚠️ EMPTY-RESULTS", f"stop_reason=tool_use but 0 results collected; {len(_stray_tool_uses)} orphan tool_use blocks — injecting error results")
                _err_results = [
                    {"type": "tool_result", "tool_use_id": b.id, "content": "[System error: tool_use block not executed]"}
                    for b in _stray_tool_uses
                ]
                messages.append({"role": "user", "content": _err_results})
            break

        # ── Milestone Enforcer: anti-fabrication (same logic as OpenAI loop) ──
        _sent_message_this_turn = "mcp__evoclaw__send_message" in _tool_names_this_turn
        _did_real_work = bool(_tool_names_this_turn & _SUBSTANTIVE_TOOLS_CLAUDE)

        if _sent_message_this_turn and _did_real_work:
            _turns_since_notify = 0
            _only_notify_turns = 0
        elif _sent_message_this_turn and not _did_real_work:
            _only_notify_turns += 1
            _log("⚠️ FAKE-PROGRESS", f"Claude called only send_message (no real work) — streak={_only_notify_turns}")
            if _only_notify_turns >= 2 and n < MAX_ITER - 2:
                _log("🚨 FAKE-PROGRESS", f"Injecting anti-fabrication warning after {_only_notify_turns} fake-report turns")
                # BUG-FIX: Anthropic API requires every tool_result.tool_use_id to match
                # a real tool_use block from the preceding assistant message.  A synthetic
                # "__system__" id that has no matching tool_use causes a 400 validation
                # error that crashes the loop.  Inject the warning as a follow-up *user*
                # message instead — it is semantically equivalent and always API-valid.
                messages.append({"role": "user", "content": tool_results})
                messages.append({
                    "role": "user",
                    "content": (
                        "【系統警告】你已連續多輪只呼叫 send_message，沒有呼叫任何實質工具（Bash、Read、Write、run_agent 等）。"
                        "立刻停止假報告。你的下一步必須是：呼叫 Bash tool 執行指令、Read 讀取檔案、或 mcp__evoclaw__run_agent 委派任務。"
                    ),
                })
                # Skip the normal messages.append below since we already appended above
                tool_results = None  # sentinel: messages already appended
        else:
            _only_notify_turns = 0  # reset streak when doing real work silently
            _turns_since_notify += 1
            if _turns_since_notify >= 5 and n < MAX_ITER - 2:
                _log("⏰ MILESTONE", f"Claude: no send_message for {_turns_since_notify} turns — injecting reminder")
                # BUG-FIX: same issue — inject as a separate user message, not a
                # tool_result with a fake tool_use_id that Anthropic would reject.
                messages.append({"role": "user", "content": tool_results})
                messages.append({
                    "role": "user",
                    "content": (
                        f"⏰ 你已執行 {_turns_since_notify} 輪未向用戶回報進度。"
                        "請在繼續工作的同時，用 mcp__evoclaw__send_message 發送一條簡短的進度更新（1-2 句話）。"
                        "注意：只有在呼叫了 Bash/Read/Write 等實質工具之後才需要回報，不要虛報進度。"
                    ),
                })
                _turns_since_notify = 0
                tool_results = None  # sentinel: messages already appended

        if tool_results is not None:
            messages.append({"role": "user", "content": tool_results})

        # Fix 4 (STABILITY_ANALYSIS 3.5): inject retry warning as a user message
        if _retry_warning:
            messages.append({"role": "user", "content": _retry_warning})
            _retry_warning = ""

        # P15A-FIX-4: Trim history to prevent unbounded growth while preserving
        # tool_use / tool_result pairs.  The naive slice messages[:1]+messages[-(N-1):]
        # can cut between an assistant message with tool_use blocks and the following
        # user message with tool_result blocks, producing a 400 from the Anthropic API.
        # Instead, after slicing, advance the tail start until it begins on a message
        # that is NOT a user tool_result (i.e. its content is not a list starting with
        # a tool_result type), ensuring the pair is never split.
        if len(messages) > _MAX_HISTORY_MESSAGES:
            _keep_head = messages[:1]  # first user message always preserved
            _tail_start = len(messages) - (_MAX_HISTORY_MESSAGES - 1)
            # Advance _tail_start past any orphaned user tool_result messages
            while _tail_start < len(messages):
                _tm = messages[_tail_start]
                _tc = _tm.get("content", "")
                # A user message whose content is a list of tool_result dicts
                if isinstance(_tc, list) and _tc and isinstance(_tc[0], dict) and _tc[0].get("type") == "tool_result":
                    _tail_start += 1
                else:
                    break
            messages = _keep_head + messages[_tail_start:]

        # ── MEMORY.md reminder on penultimate turn ───────────────────────────
        if not _memory_written and n == MAX_ITER - 2:
            _log("⚠️ MEMORY-REMIND", f"MEMORY.md not updated by turn {n} — injecting CRITICAL reminder")
            messages.append({
                "role": "user",
                "content": (
                    f"【CRITICAL 系統警告】你在本 session 中尚未更新 MEMORY.md（{_memory_path_str}）。\n"
                    "這是倒數第二輪。你必須在結束前執行以下操作：\n"
                    "1. 使用 Write/Edit 工具更新 MEMORY.md\n"
                    "2. 在 `## 任務記錄 (Task Log)` 區段追加今日任務摘要\n"
                    "3. 若 `## 身份 (Identity)` 有新發現（弱點、原則），同步更新\n"
                    "格式：`[YYYY-MM-DD] <做了什麼、關鍵決策、解決方法>`"
                ),
            })

        # ── Penultimate-turn send_message enforcer (Claude) ──────────────────
        # Mirror of the OpenAI loop's enforcer: for Level A (MAX_ITER=6) the
        # normal milestone (5 silent turns AND n < MAX_ITER-2) can never fire,
        # so we add an explicit penultimate check here.
        if _turns_since_notify > 0 and n == MAX_ITER - 2:
            _log("⏰ MILESTONE-FINAL", f"Claude: no send_message in {_turns_since_notify} turns, penultimate turn {n} — injecting CRITICAL send reminder")
            messages.append({
                "role": "user",
                "content": (
                    "【CRITICAL 系統警告】你尚未向用戶發送任何回應（send_message）。\n"
                    f"這是倒數第二輪（turn {n+1}/{MAX_ITER}），下一輪是最後一輪。\n"
                    "你必須在下一輪立刻呼叫 mcp__evoclaw__send_message 把結果告知用戶，"
                    "否則用戶將看到「處理完成，但未能產生文字回應」錯誤。\n"
                    "不要再執行其他工具——把你已掌握的資訊直接發送出去。"
                ),
            })
            _turns_since_notify = 0

    # If the loop exhausted MAX_ITER without an end_turn, return whatever we have.
    # Avoids returning silent empty string to the host.
    if not final_response:
        _log("⚠️ LOOP-EXHAUST", f"Claude agent loop hit MAX_ITER={MAX_ITER} without end_turn — no final text collected")
    if not final_response or not final_response.strip():
        final_response = "（處理完成，但未能產生文字回應，請重新詢問。）"
    return final_response

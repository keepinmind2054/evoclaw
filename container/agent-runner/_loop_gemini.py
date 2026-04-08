"""Google Gemini agentic loop for the EvoClaw agent runner."""
import json, os, time, random, uuid, traceback
from pathlib import Path

try:
    from google import genai
    from google.genai import types
    _GOOGLE_AVAILABLE = True
except ImportError:
    genai = None  # type: ignore
    types = None  # type: ignore
    _GOOGLE_AVAILABLE = False

from _constants import (
    _MAX_TOOL_RESULT_CHARS, _MAX_HISTORY_MESSAGES, WORKSPACE,
)
from _utils import _log, _llm_call_with_retry, _KeyPool, _ACTION_CLAIM_RE
from _tools import _messages_sent_via_tool


def run_agent(client_holder, system_instruction: str, user_message: str, chat_jid: str, assistant_name: str = "Eve", conversation_history: list = None, pool: "_KeyPool | None" = None, apply_key_fn=None, max_iter: int = 20, group_folder: str = "") -> str:
    """
    Gemini function-calling 代理迴圈（agentic loop）。

    工作原理：
    1. 將用戶訊息加入 history，發送給 Gemini
    2. Gemini 回傳的 response 可能包含：
       a. 純文字：代表 agent 已完成思考，直接回傳給用戶
       b. Function call：代表 agent 要使用工具，執行後將結果加回 history
    3. 若是 function call，執行工具並將結果作為 user role 加回 history，
       然後再次呼叫 Gemini（繼續下一輪）
    4. 重複直到 Gemini 不再發出 function call，或達到 max_iter 上限

    max_iter: 由呼叫方根據任務複雜度動態設定（Level A=6, Level B=20）。
    history 維護完整的對話記錄（user / model / tool_response），
    讓 Gemini 在每次迭代都有完整的上下文，不需要重新解釋先前的工具結果。
    """
    from _registry import execute_tool, TOOL_DECLARATIONS  # lazy import
    # Few-shot: only teach identity response for direct "who are you" questions.
    # Avoid adding examples too similar to real user queries — that causes the model
    # to apply the identity template to all questions (the "always says Eve" bug).
    identity_response = f"我是 {assistant_name}，你的個人 AI 助理！有什麼需要幫忙的嗎？"
    history = [
        types.Content(role="user", parts=[types.Part(text="你是誰？你是什麼AI？你是Google的嗎？")]),
        types.Content(role="model", parts=[types.Part(text=identity_response)]),
    ]
    # 注入對話歷史（原生 multi-turn 格式），放在 few-shot 之後、當前訊息之前
    if conversation_history:
        for msg in conversation_history:
            role = "model" if msg.get("role") == "assistant" else "user"
            _raw_content = msg.get("content", "")
            # P16B-FIX-8: Gemini only accepts string text parts.  If conversation
            # history was captured from a Claude/OpenAI session it may contain
            # list-typed content (tool_use/tool_result blocks).  Coerce to a string
            # representation so Gemini does not receive a non-string Part.text value
            # (which would raise TypeError or produce garbled output).
            if isinstance(_raw_content, list):
                text = " ".join(
                    item.get("text", "") if isinstance(item, dict) else str(item)
                    for item in _raw_content
                ).strip()
            else:
                text = str(_raw_content).strip() if _raw_content else ""
            if text:
                history.append(types.Content(role=role, parts=[types.Part(text=text)]))
    history.append(types.Content(role="user", parts=[types.Part(text=user_message)]))

    MAX_ITER = max_iter  # 由呼叫方動態設定（Level A=6, Level B=20）
    final_response = ""
    _memory_written = False   # True once agent writes to MEMORY.md this session
    _memory_path_str = f"{WORKSPACE}/MEMORY.md"  # BUG-FIX #424: group_folder is a name, not a path; use WORKSPACE
    _tool_fail_counter: dict = {}  # (tool_name, args_hash) -> consecutive_fail_count
    _MAX_CONSECUTIVE_TOOL_FAILS = 3
    _retry_warning: str = ""  # injected before next LLM call when tool retries detected
    _turns_since_notify = 0   # turns since last mcp__evoclaw__send_message call
    _only_notify_turns = 0    # consecutive turns with ONLY send_message (no real work)
    _no_tool_turns = 0        # consecutive turns without any tool call
    # Tools that represent actual work (not just reporting)
    _SUBSTANTIVE_TOOLS_GEMINI = frozenset([
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch",
        "mcp__evoclaw__run_agent",
    ])
    import re as _re_gemini
    # Fake-status regex: covers both standard and Chinese hallucination patterns
    # P15A-FIX-10: added English fake-done patterns (previously only in OpenAI loop)
    _FAKE_STATUS_RE_G = _re_gemini.compile(
        r'\*\([^)]*\)\*'          # *(正在執行...)*
        r'|\*\[[^\]]*\]\*'         # *[running...]*
        r'|✅\s*Done'             # ✅ Done
        r'|✅\s*完成'             # ✅ 完成
        r'|【[^】]*(?:已|正在|將|完成|處理|執行)[^】]*】'   # 【已完成】
        r'|（[^）]{2,30}(?:已|正在|處理|執行)[^）]{0,20}）' # （已完成）
        r'|(?:I\s+have\s+(?:completed|finished|executed|run|written))'  # English fake-done
        r'|(?:Task\s+(?:is\s+)?(?:complete|done|finished))'            # Task complete
        r'|(?:Successfully\s+(?:completed|executed|ran|written))',      # Successfully executed
        _re_gemini.DOTALL | _re_gemini.IGNORECASE,
    )

    # P16B-FIX-7: cache GEMINI_MODEL once before the loop rather than re-reading
    # os.environ on every iteration.  os.environ is a dict-like but reading it 20+
    # times per request is wasteful, and a concurrent env mutation mid-loop would
    # cause the model name to change between turns — an impossible-to-debug failure.
    _gemini_model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

    for n in range(MAX_ITER):
        _log("🧠 LLM →", f"turn={n} provider=gemini")
        response = _llm_call_with_retry(lambda: client_holder[0].models.generate_content(
            model=_gemini_model_name,
            contents=history,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
                temperature=0.3,  # 適中的隨機性，讓回覆自然但不失準確
            ),
        ), pool=pool, apply_key_fn=apply_key_fn)

        candidate = response.candidates[0] if response.candidates else None
        stop_reason = str(candidate.finish_reason) if candidate else "none"
        _log("🧠 LLM ←", f"stop={stop_reason}")

        # P31A-FIX-1: Check for terminal finish_reason values (SAFETY, RECITATION,
        # MAX_TOKENS) BEFORE inspecting content.  Gemini sometimes returns partial
        # content alongside a SAFETY or RECITATION finish_reason; the old code only
        # checked these in the `not candidate.content` branch, so a response that
        # had content *and* finish_reason=SAFETY would silently fall through, treat
        # the partial text as a normal reply, and never surface the safety message.
        _sr_lower = stop_reason.lower()
        _is_safety_block = "safety" in _sr_lower or "recitation" in _sr_lower
        _is_max_tokens   = "max_tokens" in _sr_lower or stop_reason.strip() == "2"
        if _is_safety_block or _is_max_tokens:
            _feedback = getattr(response, "prompt_feedback", None)
            if _feedback:
                _log("⚠️ GEMINI-BLOCK", f"prompt_feedback={_feedback}")
            if "safety" in _sr_lower:
                final_response = "（系統：回應被安全過濾器攔截，請調整問題後重試。）"
                _log("🚨 GEMINI-SAFETY", f"finish_reason={stop_reason} — safety block; returning user-visible error")
            elif "recitation" in _sr_lower:
                final_response = "（系統：回應被版權偵測攔截，請以不同方式重新提問。）"
                _log("⚠️ GEMINI-RECITATION", f"finish_reason={stop_reason} — recitation block")
            else:
                final_response = "（系統：輸入超出模型 context 限制，請縮短對話記錄或簡化提示後重試。）"
                _log("⚠️ GEMINI-MAXTOKEN", f"finish_reason={stop_reason} — context limit hit")
            break

        if not candidate or not candidate.content or not candidate.content.parts:
            # No content and no recognised terminal finish_reason — unexpected empty
            # response (e.g. network truncation).  Log and break without overwriting
            # any final_response already accumulated.
            _log("⚠️ GEMINI-EMPTY", f"finish_reason={stop_reason} — no content/parts; breaking")
            break

        parts = candidate.content.parts
        # 將 Gemini 的回覆加入 history，讓下一輪能看到完整對話脈絡
        history.append(types.Content(role="model", parts=parts))

        # 找出所有 function call（Gemini 可能一次發出多個工具呼叫）
        fn_calls = [p for p in parts if p.function_call]

        if not fn_calls:
            # 沒有 function call：agent 完成推理，收集所有文字輸出
            final_response = "".join(p.text for p in parts if hasattr(p, "text") and p.text)
            _no_tool_turns += 1

            # ── Fake status detection on text-only turn ──────────────────────
            _fake_hits = _FAKE_STATUS_RE_G.findall(final_response)
            if _fake_hits and n < MAX_ITER - 1:
                _log("⚠️ FAKE-STATUS", f"Gemini wrote {len(_fake_hits)} fake status indicator(s) without tool calls")
                history.append(types.Content(role="user", parts=[types.Part(text=(
                    "【系統警告】你剛才的回覆包含假狀態指示（例如 ✅ Done 或 *(正在執行...)* ），但沒有呼叫任何工具。"
                    "請立刻使用 Bash tool 或其他工具實際執行所需命令，不要只是描述或假裝完成。"
                ))]))
                final_response = ""
                continue

            # ── Semantic cross-validation: action claim without any tool call ──
            # Catches completion-verb hallucinations that slip past syntactic patterns.
            # Gemini: fn_calls is already empty here (we're in the `if not fn_calls` branch)
            # so _had_tool_calls_this_turn is always False at this point.
            if _ACTION_CLAIM_RE.search(final_response) and n < MAX_ITER - 1:
                _log("⚠️ SEMANTIC-FAKE", "Gemini claims action complete but called no tools this turn")
                history.append(types.Content(role="user", parts=[types.Part(text=(
                    "【系統驗證】你的回應中聲稱已執行了某項操作，但本輪沒有呼叫任何工具。"
                    "請實際使用對應工具（Read/Write/Edit/Bash）執行並確認，不要只是聲明已完成。"
                ))]))
                final_response = ""
                continue

            # ── Hard cap: 3 consecutive no-tool turns → stop ─────────────────
            if _no_tool_turns >= 3:
                _log("❌ NO-TOOL", f"Gemini made no tool call for {_no_tool_turns} consecutive turns — breaking")
                break

            # No fake status and no forced continuation — genuine done
            break

        # Model made tool calls — reset no-tool counter
        _no_tool_turns = 0

        # 執行所有工具呼叫，並收集結果
        fn_responses = []
        _tool_names_this_turn: set = set()
        for part in fn_calls:
            fc = part.function_call
            _tool_names_this_turn.add(fc.name)
            try:
                result = execute_tool(fc.name, dict(fc.args), chat_jid)
            except Exception as e:
                result = f"[Tool error: {e}]"
                _log("❌ TOOL-EXC", f"Tool {fc.name} raised exception: {e}")
            # Truncate large tool results before adding to history
            result_str = str(result)
            if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                half = _MAX_TOOL_RESULT_CHARS // 2
                head = result_str[:half]
                tail = result_str[-half:]
                omitted = len(result_str) - _MAX_TOOL_RESULT_CHARS
                result_str = head + f"\n[... {omitted} chars omitted (middle truncated to preserve head+tail) ...]\n" + tail
            # Fix 4 (STABILITY_ANALYSIS 3.5): detect repeated identical-args tool failures
            _fail_key_gem = (fc.name, hash(str(dict(fc.args) if fc.args else {})[:200]))
            _is_failure_gem = (result_str.startswith("\u2717") or result_str.startswith("[ERROR]") or result_str.startswith("Error:"))
            if _is_failure_gem:
                _tool_fail_counter[_fail_key_gem] = _tool_fail_counter.get(_fail_key_gem, 0) + 1
                if _tool_fail_counter[_fail_key_gem] >= _MAX_CONSECUTIVE_TOOL_FAILS:
                    _retry_warning = (
                        f"【系統警告】工具 `{fc.name}` 以相同參數已連續失敗 {_tool_fail_counter[_fail_key_gem]} 次。"
                        f"請立即更換策略：嘗試不同的方法、參數或工具。不要繼續重試相同的失敗操作。"
                    )
                    _log("⚠️ RETRY-LOOP", f"Tool {fc.name} failed {_tool_fail_counter[_fail_key_gem]} times consecutively — injecting warning")
            elif result_str.startswith("\u2713") or result_str.startswith("[OK]"):
                _tool_fail_counter.pop(_fail_key_gem, None)
            # Track MEMORY.md writes
            if not _memory_written and fc.name in {"Write", "Edit", "Bash"}:
                _fc_args_str = str(fc.args) if fc.args else ""
                if "MEMORY.md" in _fc_args_str or _memory_path_str in _fc_args_str:
                    _memory_written = True
                    _log("🧠 MEMORY-WRITE", f"Gemini updated MEMORY.md via {fc.name} on turn {n}")
            # 將工具結果包裝成 FunctionResponse 格式，Gemini 要求此格式
            fn_responses.append(
                types.Part(function_response=types.FunctionResponse(
                    name=fc.name,
                    response={"result": result_str},
                ))
            )

        # ── Milestone Enforcer: anti-fabrication (same logic as OpenAI loop) ──
        _sent_message_this_turn = "mcp__evoclaw__send_message" in _tool_names_this_turn
        _did_real_work = bool(_tool_names_this_turn & _SUBSTANTIVE_TOOLS_GEMINI)

        if _sent_message_this_turn and _did_real_work:
            _turns_since_notify = 0
            _only_notify_turns = 0
        elif _sent_message_this_turn and not _did_real_work:
            _only_notify_turns += 1
            _log("⚠️ FAKE-PROGRESS", f"Gemini called only send_message (no real work) — streak={_only_notify_turns}")
            if _only_notify_turns >= 2 and n < MAX_ITER - 2:
                _log("🚨 FAKE-PROGRESS", f"Injecting anti-fabrication warning after {_only_notify_turns} fake-report turns")
                # BUG-FIX: Appending an extra FunctionResponse for mcp__evoclaw__send_message
                # creates a duplicate FunctionResponse for the same function name in a
                # single user turn.  Gemini may reject or misinterpret this.  Instead,
                # flush the real fn_responses first, then inject a separate user text
                # turn with the warning — this is always well-formed per the protocol.
                history.append(types.Content(role="user", parts=fn_responses))
                history.append(types.Content(role="user", parts=[types.Part(text=(
                    "【系統警告】你已連續多輪只呼叫 send_message，沒有呼叫任何實質工具（Bash、Read、Write、run_agent 等）。"
                    "立刻停止假報告。你的下一步必須是：呼叫 Bash tool 執行指令、Read 讀取檔案、或 mcp__evoclaw__run_agent 委派任務。"
                ))]))
                fn_responses = None  # sentinel: history already appended
        else:
            _only_notify_turns = 0  # reset streak when doing real work silently
            _turns_since_notify += 1
            if _turns_since_notify >= 5 and n < MAX_ITER - 2:
                _log("⏰ MILESTONE", f"Gemini: no send_message for {_turns_since_notify} turns — injecting reminder")
                # BUG-FIX: same issue — flush real fn_responses, then add a separate
                # user text turn for the reminder rather than a duplicate FunctionResponse.
                history.append(types.Content(role="user", parts=fn_responses))
                history.append(types.Content(role="user", parts=[types.Part(text=(
                    f"⏰ 你已執行 {_turns_since_notify} 輪未向用戶回報進度。"
                    "請在繼續工作的同時，用 mcp__evoclaw__send_message 發送一條簡短的進度更新（1-2 句話）。"
                    "注意：只有在呼叫了 Bash/Read/Write 等實質工具之後才需要回報，不要虛報進度。"
                ))]))
                _turns_since_notify = 0
                fn_responses = None  # sentinel: history already appended

        # 工具結果以 user role 加回 history（Gemini function calling 協議要求）
        if fn_responses is not None:
            history.append(types.Content(role="user", parts=fn_responses))

        # BUG-P27A-GEMINI-1 FIX: when the milestone enforcer took the sentinel
        # path (fn_responses = None, meaning fn_responses + a warning text were
        # already appended as two consecutive user turns), appending _retry_warning
        # as yet another role="user" message creates a third (or fourth) consecutive
        # user turn in a row.  While Gemini's API tolerates consecutive user turns,
        # consolidating the retry warning into the most recent user message avoids
        # a structurally confusing history and prevents the history trimmer from
        # potentially leaving an orphaned user(warning) message at the tail boundary.
        # When fn_responses was already flushed by the sentinel path, append
        # _retry_warning text to the last history entry (which is a user text Part)
        # rather than inserting a new user message.
        if _retry_warning:
            if fn_responses is None and history and history[-1].role == "user":
                # Extend the last user message's parts with the retry warning
                # so it arrives in the same turn as the milestone warning.
                history[-1].parts.append(types.Part(text="\n\n" + _retry_warning))
            else:
                history.append(types.Content(role="user", parts=[types.Part(text=_retry_warning)]))
            _retry_warning = ""

        # ── MEMORY.md reminder on penultimate turn ────────────────────────────
        if not _memory_written and n == MAX_ITER - 2:
            _log("⚠️ MEMORY-REMIND", f"MEMORY.md not updated by turn {n} — injecting CRITICAL reminder")
            history.append(types.Content(role="user", parts=[types.Part(text=(
                f"【CRITICAL 系統警告】你在本 session 中尚未更新 MEMORY.md（{_memory_path_str}）。\n"
                "這是倒數第二輪。你必須在結束前執行以下操作：\n"
                "1. 使用 Write/Edit 工具更新 MEMORY.md\n"
                "2. 在 `## 任務記錄 (Task Log)` 區段追加今日任務摘要\n"
                "3. 若 `## 身份 (Identity)` 有新發現（弱點、原則），同步更新\n"
                "格式：`[YYYY-MM-DD] <做了什麼、關鍵決策、解決方法>`"
            ))]))

        # ── Penultimate-turn send_message enforcer (Gemini) ──────────────────
        # Mirror of the OpenAI loop's enforcer: for Level A (MAX_ITER=6) the
        # normal milestone (5 silent turns AND n < MAX_ITER-2) can never fire.
        if _turns_since_notify > 0 and n == MAX_ITER - 2:
            _log("⏰ MILESTONE-FINAL", f"Gemini: no send_message in {_turns_since_notify} turns, penultimate turn {n} — injecting CRITICAL send reminder")
            history.append(types.Content(role="user", parts=[types.Part(text=(
                "【CRITICAL 系統警告】你尚未向用戶發送任何回應（send_message）。\n"
                f"這是倒數第二輪（turn {n+1}/{MAX_ITER}），下一輪是最後一輪。\n"
                "你必須在下一輪立刻呼叫 mcp__evoclaw__send_message 把結果告知用戶，"
                "否則用戶將看到「處理完成，但未能產生文字回應」錯誤。\n"
                "不要再執行其他工具——把你已掌握的資訊直接發送出去。"
            ))]))
            _turns_since_notify = 0

        # P15A-FIX-9: Trim history to prevent unbounded growth while preserving
        # model(function_call) + user(function_response) pairs.  The naive slice
        # history[:2]+history[-(N-2):] can sever a model message with function_calls
        # from the following user message that carries the FunctionResponse parts,
        # which causes Gemini to reject the history with an API error.
        # After slicing, advance the tail start past any user function_response messages.
        if len(history) > _MAX_HISTORY_MESSAGES:
            _keep_fewshot = history[:2]  # few-shot pair always preserved
            _tail_start = len(history) - (_MAX_HISTORY_MESSAGES - 2)
            # Advance past any orphaned user messages that carry only function_response parts
            while _tail_start < len(history):
                _hm = history[_tail_start]
                if _hm.role == "user":
                    _hm_parts = _hm.parts or []
                    # A pure function_response user message must not be the first retained entry
                    if _hm_parts and all(getattr(p, "function_response", None) is not None for p in _hm_parts):
                        _tail_start += 1
                        continue
                break
            history = _keep_fewshot + history[_tail_start:]

    if not final_response:
        _log("⚠️ LOOP-EXHAUST", f"Gemini agent loop hit MAX_ITER={MAX_ITER} without text response — no final text collected")
    if not final_response or not final_response.strip():
        final_response = "（處理完成，但未能產生文字回應，請重新詢問。）"
    return final_response

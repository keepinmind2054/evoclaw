"""OpenAI-compatible agentic loop for the EvoClaw agent runner."""
import gc
import json, os, time, random, uuid, traceback
from pathlib import Path


# ── Issue #541: RSS instrumentation ───────────────────────────────────────────
# Read /proc/self/status to capture VmRSS / VmPeak at LLM call boundaries.
# Lets us pinpoint which step spikes when a container OOMs (exit 137).
def _rss_snapshot() -> dict:
    """Return {'rss_mb': N, 'peak_mb': N} or {} if unreadable (non-Linux)."""
    try:
        with open("/proc/self/status", "r") as f:
            data = {}
            for line in f:
                if line.startswith("VmRSS:"):
                    data["rss_mb"] = int(line.split()[1]) // 1024
                elif line.startswith("VmPeak:"):
                    data["peak_mb"] = int(line.split()[1]) // 1024
                if "rss_mb" in data and "peak_mb" in data:
                    break
            return data
    except Exception:
        return {}


def _log_rss(prefix: str, **extra) -> None:
    """Log VmRSS/VmPeak at a checkpoint. extra={} for arbitrary kv pairs."""
    snap = _rss_snapshot()
    if not snap:
        return
    parts = [f"rss={snap.get('rss_mb', '?')}MB", f"peak={snap.get('peak_mb', '?')}MB"]
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    _log("📊 MEM", f"{prefix} " + " ".join(parts))


# ── Issue #541: byte-based history cap ────────────────────────────────────────
# Existing cap at line 470 is message-COUNT based (40 messages). That's not
# enough — a single tool_result can be 8 KB, so 40 messages = 320 KB best case
# but easily 1+ MB if tool outputs are at cap. Add a byte cap that runs BEFORE
# each LLM call.
_HISTORY_BYTE_BUDGET = 64 * 1024  # 64 KB — tightened from 256 KB after OOM at 59KB/40msgs


# ── Issue #541: reclaim fragmented memory before LLM calls ────────────────────
# After multiple turns of subprocess fork+exec + JSON alloc/dealloc, Python's
# pymalloc arenas fragment.  VmPeak grows to 300+ MB even though RSS is ~100 MB.
# The next large allocation (httpx TLS buffer, streaming response) can't reuse
# freed arenas and triggers a new mmap() that pushes the cgroup past its limit.
#
# gc.collect() reclaims Python objects; malloc_trim() (glibc) returns freed pages
# to the OS so the cgroup memory counter drops.  Together they close the gap
# between VmPeak and RSS.
def _reclaim_memory() -> None:
    """Force GC + return freed pages to OS to reduce cgroup memory pressure."""
    gc.collect()
    try:
        import ctypes
        _libc = ctypes.CDLL("libc.so.6")
        _libc.malloc_trim(0)
    except Exception:
        pass  # Non-glibc or non-Linux — skip silently


def _trim_history_to_byte_budget(history: list, budget: int = _HISTORY_BYTE_BUDGET) -> tuple:
    """Trim history (in place) so its JSON serialization fits within budget.

    Always preserves history[0] (system message). Drops oldest non-system
    messages first. Respects assistant→tool message coupling: never strips a
    tool message whose corresponding tool_call was kept.

    Returns (trimmed_history, original_bytes, trimmed_bytes) for logging.
    """
    if not history:
        return history, 0, 0
    original = history[:]
    original_bytes = len(json.dumps(original, ensure_ascii=False).encode("utf-8"))
    if original_bytes <= budget:
        return history, original_bytes, original_bytes

    # Always keep system message (history[0]).
    sys_msg = history[0:1]
    tail = history[1:]

    # Walk from end backwards, keep messages until we hit the budget.
    # Build kept set in original order.
    sys_bytes = len(json.dumps(sys_msg, ensure_ascii=False).encode("utf-8"))
    remaining = budget - sys_bytes
    kept_reversed = []
    for msg in reversed(tail):
        msg_bytes = len(json.dumps(msg, ensure_ascii=False).encode("utf-8")) + 1  # +1 comma
        if msg_bytes > remaining:
            break
        kept_reversed.append(msg)
        remaining -= msg_bytes
    kept_tail = list(reversed(kept_reversed))

    # Repair coupling: a tool message at the start of kept_tail without its
    # preceding assistant.tool_call would confuse the model. Drop leading tools.
    while kept_tail and kept_tail[0].get("role") == "tool":
        kept_tail.pop(0)

    new_history = sys_msg + kept_tail
    new_bytes = len(json.dumps(new_history, ensure_ascii=False).encode("utf-8"))
    return new_history, original_bytes, new_bytes

try:
    from openai import OpenAI as OpenAIClient
    import httpx
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False
    httpx = None  # type: ignore

import re as _re_openai

from _constants import (
    _MAX_TOOL_RESULT_CHARS, _MAX_HISTORY_MESSAGES,
)
from _utils import _log, _llm_call_with_retry, _KeyPool
from _constants import _ACTION_CLAIM_RE
from _tools import _messages_sent_via_tool

# ── Module-level compiled regexes (issue #453: avoid re-compiling every loop iteration) ──
_CODE_BLOCK_RE = _re_openai.compile(r'```(?:bash|sh|shell)?\n([\s\S]*?)```')
_GIT_LOG_LINE_RE = _re_openai.compile(r'^[0-9a-f]{7,40} \S')
_BARE_FILE_RE = _re_openai.compile(
    r'^[^\s|;&<>$`\'\"()\[\]{}!\\]+\.(md|txt|py|js|ts|sh|json|yaml|yml|toml|csv|log|conf|cfg)$',
    _re_openai.IGNORECASE,
)
_FAKE_STATUS_RE = _re_openai.compile(
    r'\*\([^)]*\)\*'                                                   # *(正在執行...)*
    r'|\*\[[^\]]*\]\*'                                                  # *[running...]*
    r'|✅\s*Done'                                                      # ✅ Done
    r'|✅\s*完成'                                                      # ✅ 完成
    r'|【[^】]*(?:已|正在|將|完成|處理|執行)[^】]*】'                    # 【已完成】
    r'|（[^）]{2,30}(?:已|正在|處理|執行)[^）]{0,20}）'                 # （已完成）
    r'|(?:I\s+have\s+(?:completed|finished|executed|run|written))'     # English fake-done
    r'|(?:Task\s+(?:is\s+)?(?:complete|done|finished))'               # Task complete
    r'|(?:Successfully\s+(?:completed|executed|ran|written))',          # Successfully executed
    _re_openai.DOTALL | _re_openai.IGNORECASE,
)
_EXTENDED_FAKE_RE = _re_openai.compile(
    r'(?:已|正在|即將).{0,8}(?:完成|處理|執行|分析)',  # 已完成、正在處理
)


# ── Issue #541: streaming chat completions (memory-bounded response handling) ──
# The non-streaming path buffers the entire response in memory and then runs
# pydantic v2 model construction over it — for large tool_call arguments this
# can spike RSS by hundreds of MB transiently. Streaming consumes one chunk at
# a time (~few KB), keeping peak memory bounded.
#
# We accumulate streamed deltas into a dict that mirrors the non-streaming
# response shape (`response.choices[0].message.content`, `.tool_calls`,
# `.finish_reason`) so the rest of the loop is unchanged.
class _StreamedMessage:
    __slots__ = ("content", "tool_calls")
    def __init__(self, content, tool_calls):
        self.content = content
        self.tool_calls = tool_calls

class _StreamedToolCall:
    __slots__ = ("id", "type", "function")
    def __init__(self, id_, type_, function):
        self.id = id_; self.type = type_; self.function = function

class _StreamedFunction:
    __slots__ = ("name", "arguments")
    def __init__(self, name, arguments):
        self.name = name; self.arguments = arguments

class _StreamedChoice:
    __slots__ = ("message", "finish_reason")
    def __init__(self, message, finish_reason):
        self.message = message; self.finish_reason = finish_reason

class _StreamedResponse:
    __slots__ = ("choices",)
    def __init__(self, choices):
        self.choices = choices


# Hard caps for stream consumption (Issue #541: OOM at turn=4 during streaming)
_STREAM_MAX_TOTAL_BYTES = 1 * 1024 * 1024   # 1 MB hard cap across all chunks
_STREAM_MAX_ARGS_BYTES_PER_TOOL = 32 * 1024 # 32 KB per tool_call.arguments


def _consume_stream(stream) -> _StreamedResponse:
    """Drain a streaming chat.completions response into a non-streaming-shaped object.

    Memory profile:
    - content accumulates into a list (O(N) via "".join at the end)
    - tool_call.arguments accumulates into per-slot lists (O(N) via "".join)
    - Hard cap on total bytes (1 MB) — fail-fast if model runs amok
    - Hard cap per tool_call arguments (32 KB) — truncate absurd payloads

    Previously used `slot["arguments"] += chunk` which is O(N²) for large
    args and can trigger mmap()-driven cgroup OOM on models that stream
    huge tool_call.arguments payloads.
    """
    content_parts: list = []
    # idx -> {id, type, name, args_parts: list[str], args_bytes: int, truncated: bool}
    tool_calls_acc: dict = {}
    finish_reason = None
    total_bytes = 0
    for chunk in stream:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        if delta is not None:
            if delta.content:
                c = delta.content
                content_parts.append(c)
                total_bytes += len(c)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    slot = tool_calls_acc.setdefault(idx, {
                        "id": "", "type": "function", "name": "",
                        "args_parts": [], "args_bytes": 0, "truncated": False,
                    })
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.type:
                        slot["type"] = tc.type
                    if tc.function is not None:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            a = tc.function.arguments
                            if slot["args_bytes"] + len(a) <= _STREAM_MAX_ARGS_BYTES_PER_TOOL:
                                slot["args_parts"].append(a)
                                slot["args_bytes"] += len(a)
                                total_bytes += len(a)
                            elif not slot["truncated"]:
                                slot["truncated"] = True
                                # Fill remaining budget then stop appending for this tool
                                remaining = _STREAM_MAX_ARGS_BYTES_PER_TOOL - slot["args_bytes"]
                                if remaining > 0:
                                    slot["args_parts"].append(a[:remaining])
                                    slot["args_bytes"] += remaining
                                    total_bytes += remaining
        if choice.finish_reason:
            finish_reason = choice.finish_reason

        # Total stream cap — if the server is streaming a runaway response,
        # stop here before we OOM.
        if total_bytes > _STREAM_MAX_TOTAL_BYTES:
            _log("⚠️ STREAM-CAP", f"total_bytes={total_bytes} exceeded cap — truncating stream")
            finish_reason = finish_reason or "length"
            break

    content = "".join(content_parts) if content_parts else None
    del content_parts  # release the list immediately
    if tool_calls_acc:
        tcs = []
        for idx in sorted(tool_calls_acc.keys()):
            slot = tool_calls_acc[idx]
            args_joined = "".join(slot["args_parts"])
            if slot["truncated"]:
                _log("⚠️ TOOL-ARGS-CAP", f"tool={slot['name']} args truncated at {slot['args_bytes']}B")
            tcs.append(_StreamedToolCall(
                id_=slot["id"],
                type_=slot["type"],
                function=_StreamedFunction(name=slot["name"], arguments=args_joined),
            ))
        del tool_calls_acc  # release the dict
    else:
        tcs = None
    msg = _StreamedMessage(content=content, tool_calls=tcs)
    choice_obj = _StreamedChoice(message=msg, finish_reason=finish_reason or "stop")
    return _StreamedResponse(choices=[choice_obj])


def run_agent_openai(client_holder, system_instruction: str, user_message: str, chat_jid: str, model: str, conversation_history: list = None, pool: "_KeyPool | None" = None, apply_key_fn=None, group_folder: str = "", max_iter: int = 20) -> str:
    """
    OpenAI-compatible agentic loop (NVIDIA NIM / OpenAI / Qwen / Groq / etc.)
    Works the same as run_agent but uses OpenAI chat completions API.
    client_holder: a one-element list [client] so key rotation can swap the client mid-loop.
    conversation_history: 原生 multi-turn 格式的對話歷史。
    pool/apply_key_fn: optional key pool for automatic rotation on rate-limit errors.
    max_iter: maximum number of agentic loop iterations (default 20; caller sets based on task complexity).
    """
    from _registry import execute_tool, OPENAI_TOOL_DECLARATIONS  # lazy import to break circular dep
    from _utils import _is_qwen_model
    from _constants import WORKSPACE

    import json as _json
    history = [{"role": "system", "content": system_instruction}]
    # P15A-FIX-5: inject conversation history preserving structured content.
    # The old code used msg.get("content","") which evaluates to "" for list-typed
    # content (tool_result blocks), silently dropping those messages.  Use the raw
    # content value directly; only skip messages that have genuinely empty content.
    if conversation_history:
        for msg in conversation_history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            # P16B-FIX-6: accept both string and list content; skip only truly empty
            # values.  The old guard `content or content == 0` contained a dead branch
            # (`content == 0` can never occur in conversation history — message content
            # is always a string or list) and was semantically confusing.  Replaced with
            # an explicit check: skip empty strings and empty lists only.
            if content is None:
                continue
            if isinstance(content, str) and not content.strip():
                continue
            if isinstance(content, list) and not content:
                continue
            history.append({"role": role, "content": content})
    history.append({"role": "user", "content": user_message})
    MAX_ITER = max_iter
    final_response = ""
    _no_tool_turns = 0  # consecutive turns without any tool call (Fix #169)
    _turns_since_notify = 0  # turns since last mcp__evoclaw__send_message call (milestone enforcer)
    _only_notify_turns = 0   # consecutive turns with ONLY send_message (no substantive tools)
    _memory_written = False  # True once agent writes to MEMORY.md this session (Enforcer v3)
    # P16B-FIX-3: guard against empty group_folder so the path does not resolve to
    # the relative string "MEMORY.md", which would never match an absolute path in
    # tool arguments and silently disable the MEMORY.md write-detection logic.
    _memory_path_str = f"{WORKSPACE}/MEMORY.md"  # BUG-FIX #424: group_folder is a name, not a path; use WORKSPACE
    _tool_fail_counter: dict = {}  # (tool_name, args_hash) -> consecutive_fail_count
    _MAX_CONSECUTIVE_TOOL_FAILS = 3
    _retry_warning: str = ""  # injected before next LLM call when tool retries detected
    # Tools that represent actual work (not just reporting)
    _SUBSTANTIVE_TOOLS = frozenset([
        "Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch",
        "mcp__evoclaw__run_agent",
    ])

    for n in range(MAX_ITER):
        # (gc+malloc_trim moved to _reclaim_memory() call before each LLM call)
        # Escalate to "required" when model has been avoiding tools (Fix #169).
        # tool_choice="required" is enforced at the API level — the model CANNOT
        # return a text-only response, it MUST make a tool call.
        # Qwen 不適合 tool_choice="required"（容易死循環），改用 prompt 強制
        _is_qwen = _is_qwen_model(model)
        if _no_tool_turns >= 2 and _is_qwen:
            _log("⚠️ QWEN-FORCE", f"Injecting critical prompt instead of tool_choice='required' (Qwen)")
            history.append({
                "role": "user",
                "content": (
                    "【緊急】你已連續多輪未呼叫工具。現在必須立刻選擇並執行一個工具：\n"
                    "A) Bash: 執行指令\n"
                    "B) Read: 讀取檔案\n"
                    "C) mcp__evoclaw__send_message: 發送訊息給用戶\n"
                    "禁止產出任何文字說明，直接呼叫工具。"
                ),
            })
            _no_tool_turns = 0
            _tool_choice = "auto"
        elif _no_tool_turns > 0:
            _tool_choice = "required"
        else:
            _tool_choice = "auto"
        if _no_tool_turns > 0:
            _log("⚠️ FORCE-TOOL", f"no_tool_turns={_no_tool_turns} — escalating tool_choice to 'required'")

        # Issue #541 — reclaim fragmented memory before building the request.
        _reclaim_memory()

        # Issue #541 — byte-cap history before sending to bound request size.
        history, _hist_orig_b, _hist_kept_b = _trim_history_to_byte_budget(history)
        if _hist_orig_b != _hist_kept_b:
            _log("✂️ HISTORY-TRIM", f"orig={_hist_orig_b}B kept={_hist_kept_b}B msgs={len(history)}")

        _log("🧠 LLM →", f"turn={n} provider=openai-compat tool_choice={_tool_choice}")
        _log_rss(f"pre-LLM turn={n}", hist_bytes=_hist_kept_b, hist_msgs=len(history))
        _oai_history = history  # capture current snapshot for lambda

        # Issue #541: prefer streaming to bound peak memory. Fall back to
        # non-streaming if the provider rejects stream=True.
        def _do_call(stream: bool, tool_choice: str):
            kwargs = dict(
                model=model,
                messages=_oai_history,
                tools=OPENAI_TOOL_DECLARATIONS,
                tool_choice=tool_choice,
                temperature=0.2 if _is_qwen else 0.3,
                max_tokens=2048,  # #541: 4096 allowed runaway responses that OOM'd the container
            )
            if stream:
                kwargs["stream"] = True
            raw = client_holder[0].chat.completions.create(**kwargs)
            return _consume_stream(raw) if stream else raw

        try:
            try:
                response = _llm_call_with_retry(
                    lambda: _do_call(stream=True, tool_choice=_tool_choice),
                    pool=pool, apply_key_fn=apply_key_fn,
                )
            except Exception as _stream_err:
                # Provider may not support stream=True — retry non-streaming once.
                _log("⚠️ STREAM-FALLBACK", f"stream=True failed ({type(_stream_err).__name__}: {_stream_err}) — retrying non-streaming")
                response = _llm_call_with_retry(
                    lambda: _do_call(stream=False, tool_choice=_tool_choice),
                    pool=pool, apply_key_fn=apply_key_fn,
                )
        except Exception as _tc_err:
            if _tool_choice == "required":
                # Some providers don't support tool_choice="required" — fall back to "auto"
                _log("⚠️ FORCE-TOOL", f"tool_choice='required' rejected ({_tc_err}) — retrying with 'auto'")
                try:
                    response = _llm_call_with_retry(
                        lambda: _do_call(stream=True, tool_choice="auto"),
                        pool=pool, apply_key_fn=apply_key_fn,
                    )
                except Exception as _fallback_err:
                    # Fallback also failed (e.g. Qwen timeout) — report cleanly and break
                    _log("❌ LLM-FALLBACK", f"Fallback API call also failed: {_fallback_err}")
                    final_response = f"（API 呼叫失敗：{type(_fallback_err).__name__}，請稍後重試。）"
                    break
            else:
                raise

        _log_rss(f"post-LLM turn={n}")
        msg = response.choices[0].message
        stop_reason = response.choices[0].finish_reason
        _log("🧠 LLM ←", f"stop={stop_reason}")

        # Add assistant message to history
        msg_dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            msg_dict["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        history.append(msg_dict)

        # P15A-FIX-7: handle finish_reason=="length" (context length exceeded).
        # Treat it as a terminal condition: trim history and break cleanly rather
        # than treating it as a regular no-tool turn and incrementing _no_tool_turns.
        if stop_reason == "length":
            _log("⚠️ CTX-OVERFLOW", f"finish_reason=length — context window exceeded; trimming history and stopping")
            # Aggressively trim history to the most recent quarter to free context
            _trim_to = max(1, len(history) // 4)
            history = history[:1] + history[-_trim_to:]
            final_response = (msg.content or "").strip() or (
                "（系統：輸入超出模型 context 限制，請縮短對話記錄或簡化提示後重試。）"
            )
            break

        if not msg.tool_calls:
            _no_tool_turns += 1  # track consecutive no-tool turns (Fix #169)

            # Hard cap: after 3 consecutive turns without tool calls, give up (Fix #169).
            # At this point tool_choice="required" has already been active for 2 turns,
            # yet the model still produced no tool calls — something is fundamentally wrong.
            if _no_tool_turns >= 3:
                _log("❌ NO-TOOL", f"Model made no tool call for {_no_tool_turns} consecutive turns — breaking")
                # BUG-FIX: when msg.content is None (API returns null for tool-only
                # messages) fall back to an explicit error string rather than empty
                # string, so the user sees "agent gave up" rather than a confusing
                # generic "(處理完成...)" fallback message.
                final_response = (msg.content or "").strip() or (
                    "（系統：模型連續多輪未呼叫工具，強制終止。請重新提問或簡化任務。）"
                )
                break

            # ── Fallback: detect bash code blocks the model forgot to run ─────
            # Some models (Qwen/NIM) output ```bash blocks as text instead of
            # calling the Bash tool. Auto-execute them and feed results back.
            content = msg.content or ""
            _code_blocks = _CODE_BLOCK_RE.findall(content)
            _runnable = [b.strip() for b in _code_blocks if b.strip()]
            if _runnable and n < MAX_ITER - 1:
                _log("⚠️ AUTO-EXEC", f"model output {len(_runnable)} code block(s) as text — auto-executing")
                _no_tool_turns = 0  # reset: code blocks count as attempted tool use
                _exec_outputs = []
                for _cmd in _runnable:
                    _log("🔧 TOOL", f"Bash (fallback) args={_cmd[:300]}")
                    # Sanity check 1: reject commands that look like git log output
                    # (lines starting with 7-40 hex chars + space, e.g. "c43faa8 feat: ...")
                    _auto_lines = [_l for _l in _cmd.strip().splitlines() if _l.strip()]
                    # Sanity check 2: reject bare filenames (e.g. "MEMORY.md", "/workspace/group/MEMORY.md")
                    if _auto_lines and len(_auto_lines) >= 2 and all(
                        _GIT_LOG_LINE_RE.match(_l) for _l in _auto_lines[:4]
                    ):
                        _res = "✗ Skipped: this looks like git log output, not a valid bash command. Use `git log --oneline` explicitly if you need commit history."
                        _log("⚠️ AUTO-EXEC-SKIP", f"Rejected fallback block that looks like git log output: {_cmd[:120]}")
                    elif _auto_lines and len(_auto_lines) == 1 and _BARE_FILE_RE.match(_cmd.strip()):
                        _res = (
                            f"✗ Skipped: '{_cmd.strip()}' is a filename, not a bash command.\n"
                            "Use the Read tool to read it, or Write/Edit tool to write it."
                        )
                        _log("⚠️ AUTO-EXEC-SKIP", f"Rejected bare filename as bash command: {_cmd.strip()[:100]}")
                    else:
                        _res = execute_tool("Bash", {"command": _cmd}, chat_jid)
                    _log("🔧 RESULT", str(_res)[:1500])
                    _exec_outputs.append(f"$ {_cmd[:200]}\n{_res}")
                _combined = "\n\n".join(_exec_outputs)
                history.append({
                    "role": "user",
                    "content": f"[系統自動執行了 {len(_runnable)} 個指令]\n{_combined}\n\n請根據以上輸出，繼續任務並回報最終結果。",
                })
                continue

            # ── Fallback 2: detect fake status lines *(正在...)* etc. ──────────
            # Log the detection; the real enforcement happens via tool_choice="required"
            # on the NEXT iteration (Fix #167 + #169: API-level enforcement is primary,
            # text re-prompt is secondary fallback for providers that don't support "required").
            # Both _FAKE_STATUS_RE and _EXTENDED_FAKE_RE are compiled at module level
            # (issue #453: avoid recompiling on every loop iteration).
            _fake_hits = _FAKE_STATUS_RE.findall(content)
            # 擴展假狀態偵測，涵蓋常見的虛假回應格式（所有 OpenAI-compatible models）
            _ext_fake_hits = _EXTENDED_FAKE_RE.findall(content)
            if _ext_fake_hits:
                _fake_hits = (_fake_hits or []) + _ext_fake_hits
            if _fake_hits and n < MAX_ITER - 1:
                _log("⚠️ FAKE-STATUS", f"model wrote {len(_fake_hits)} fake status line(s) — tool_choice='required' on next turn")
                history.append({
                    "role": "user",
                    "content": (
                        "【系統警告】你剛才的回覆包含假狀態行（例如 *(正在執行...)* ），沒有呼叫任何工具。"
                        "下一輪系統將強制要求你必須呼叫工具，請立刻使用 Bash tool 或其他工具執行所需命令。"
                    ),
                })
                continue

            # ── Semantic cross-validation: action claim without any tool call ──
            # Catches completion-verb hallucinations that slip past syntactic patterns.
            # OpenAI: tool calls appear as msg.tool_calls (None or empty list when absent)
            _had_tool_calls_this_turn_oai = bool(getattr(msg, "tool_calls", None))
            if not _had_tool_calls_this_turn_oai and _ACTION_CLAIM_RE.search(content) and n < MAX_ITER - 1:
                _log("⚠️ SEMANTIC-FAKE", "OpenAI model claims action complete but called no tools this turn")
                history.append({
                    "role": "user",
                    "content": (
                        "【系統驗證】你的回應中聲稱已執行了某項操作，但本輪沒有呼叫任何工具。"
                        "請實際使用對應工具（Read/Write/Edit/Bash）執行並確認，不要只是聲明已完成。"
                    ),
                })
                continue

            # No code blocks, no fake status — model is genuinely done
            final_response = content
            break

        # Model made tool calls — reset the no-tool counter (Fix #169)
        _no_tool_turns = 0

        # ── 里程碑強制器 v2：區分「假報告」和「真工作」────────────────────────
        # 問題：舊版允許模型只呼叫 send_message 來通過里程碑檢查，
        # 導致模型用假進度報告（完全虛構內容）冒充在工作。
        # 修正：只有「實質工具 + send_message」的組合才算真里程碑。
        #       連續多輪「只有 send_message」→ 強硬警告：停止假報告，立即做事。
        #
        # BUG-P26B-3: the OpenAI API requires that every tool-role message
        # immediately follows the assistant message that issued the tool_calls —
        # no user-role message may be inserted between them.  The milestone
        # enforcer and MEMORY.md reminder previously injected user messages into
        # history BEFORE the tool-execution loop, which placed them between the
        # assistant tool_calls message and its tool-role results, causing a 400
        # validation error on the next API call.  Fix: collect deferred injection
        # messages in _deferred_user_msgs and append them AFTER all tool results
        # have been added to history.
        _deferred_user_msgs: list = []

        _tool_names_this_turn = {tc.function.name for tc in msg.tool_calls}
        _sent_message_this_turn = "mcp__evoclaw__send_message" in _tool_names_this_turn
        _did_real_work = bool(_tool_names_this_turn & _SUBSTANTIVE_TOOLS)

        if _sent_message_this_turn and _did_real_work:
            # Genuine progress report: real work + notification
            _turns_since_notify = 0
            _only_notify_turns = 0
        elif _sent_message_this_turn and not _did_real_work:
            # Only send_message, no actual work done — track fabrication pattern
            _only_notify_turns += 1
            _log("⚠️ FAKE-PROGRESS", f"Model called only send_message (no real work) — streak={_only_notify_turns}")
            if _only_notify_turns >= 2 and n < MAX_ITER - 2:
                _log("🚨 FAKE-PROGRESS", f"Injecting anti-fabrication warning after {_only_notify_turns} fake-report turns")
                _deferred_user_msgs.append({
                    "role": "user",
                    "content": (
                        "【系統警告】你已連續多輪只呼叫 send_message，沒有呼叫任何實質工具（Bash、Read、Write、run_agent 等）。"
                        "這代表你在發送虛構的進度報告而不是真正執行任務。"
                        "立刻停止假報告。你的下一步必須是：呼叫 Bash tool 執行指令、Read 讀取檔案、或 mcp__evoclaw__run_agent 委派任務。"
                        "如果不知道怎麼繼續，使用 mcp__evoclaw__run_agent 把任務委派給子代理。"
                    ),
                })
        else:
            # No send_message — working silently; reset only_notify streak
            _only_notify_turns = 0
            _turns_since_notify += 1
            if _turns_since_notify >= 5 and n < MAX_ITER - 2:
                _log("⏰ MILESTONE", f"No send_message for {_turns_since_notify} turns — injecting reminder")
                _deferred_user_msgs.append({
                    "role": "user",
                    "content": (
                        f"⏰ 你已執行 {_turns_since_notify} 輪未向用戶回報進度。"
                        "請在繼續工作的同時，用 mcp__evoclaw__send_message 發送一條簡短的進度更新（1-2 句話）。"
                        "注意：只有在呼叫了 Bash/Read/Write 等實質工具之後才需要回報，不要虛報進度。"
                    ),
                })
                _turns_since_notify = 0

        # ── Milestone Enforcer v3: MEMORY.md 寫入偵測 ───────────────────────
        # 偵測本輪是否有工具呼叫了 MEMORY.md（Write/Edit/Bash 寫入）。
        # 確保 session 結束前 agent 確實更新了長期記憶與身份認知。
        if not _memory_written:
            for _tc in msg.tool_calls:
                if _tc.function.name in {"Write", "Edit", "Bash"}:
                    _tc_args = _tc.function.arguments or ""
                    if "MEMORY.md" in _tc_args or _memory_path_str in _tc_args:
                        _memory_written = True
                        _log("🧠 MEMORY-WRITE", f"Agent updated MEMORY.md via {_tc.function.name} on turn {n}")
                        break

        # 倒數第二輪若 MEMORY.md 仍未寫入 → CRITICAL 提醒
        # BUG-P26B-3 (cont): defer this injection as well so it lands after tool results.
        if not _memory_written and n == MAX_ITER - 2:
            _log("⚠️ MEMORY-REMIND", f"MEMORY.md not updated by turn {n} — injecting CRITICAL reminder")
            _deferred_user_msgs.append({
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

        # Penultimate-turn send_message enforcer:
        # The normal milestone fires at _turns_since_notify >= 5 AND n < MAX_ITER - 2.
        # For Level A sessions (MAX_ITER=6) that condition can never be satisfied
        # (you'd need 5 silent turns but the window closes at n<4).
        # Fix: at n == MAX_ITER - 2, if the agent has not sent ANY message yet,
        # inject a CRITICAL reminder so it delivers the result on the final turn.
        if _turns_since_notify > 0 and n == MAX_ITER - 2:
            _log("⏰ MILESTONE-FINAL", f"No send_message in {_turns_since_notify} turns, penultimate turn {n} — injecting CRITICAL send reminder")
            _deferred_user_msgs.append({
                "role": "user",
                "content": (
                    "【CRITICAL 系統警告】你尚未向用戶發送任何回應（send_message）。\n"
                    f"這是倒數第二輪（turn {n+1}/{MAX_ITER}），下一輪是最後一輪。\n"
                    "你必須在下一輪立刻呼叫 mcp__evoclaw__send_message 把結果告知用戶，"
                    "否則用戶將看到「處理完成，但未能產生文字回應」錯誤。\n"
                    "不要再執行其他工具——把你已掌握的資訊直接發送出去。"
                ),
            })
            _turns_since_notify = 0  # reset to prevent re-trigger

        # Execute all tool calls and add results
        for tc in msg.tool_calls:
            try:
                args = _json.loads(tc.function.arguments)
            except Exception as _arg_err:
                # 嘗試修復 Qwen 常見的 JSON 格式問題
                _raw_args = str(tc.function.arguments or "")
                _recovered = False
                if _raw_args.strip() and _raw_args != "{}":
                    try:
                        # 修復常見問題：末尾多餘逗號 (use module-level _re_openai, issue #453)
                        _fixed = _re_openai.sub(r',\s*([}\]])', r'\1', _raw_args)
                        args = _json.loads(_fixed)
                        _recovered = True
                        _log("🔧 ARG-RECOVERY", f"Recovered malformed args for {tc.function.name}")
                    except Exception:
                        args = {}
                else:
                    args = {}

                if not _recovered:
                    _log("⚠️ TOOL-ARG-PARSE", f"Failed to parse/recover tool args for {tc.function.name}: {_arg_err}")
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Error: Failed to parse tool arguments: {_arg_err}"
                    })
                    continue
            try:
                result = execute_tool(tc.function.name, args, chat_jid)
            except Exception as _tool_exc:
                result = f"[Tool error: {_tool_exc}]"
                _log("❌ TOOL-EXC", f"Tool {tc.function.name} raised exception: {_tool_exc}")
            # Issue #541: reclaim subprocess memory immediately after tool call
            _reclaim_memory()
            _log_rss(f"post-tool {tc.function.name}")
            # Truncate large tool results before adding to history
            result_str = str(result)
            if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                half = _MAX_TOOL_RESULT_CHARS // 2
                head = result_str[:half]
                tail = result_str[-half:]
                omitted = len(result_str) - _MAX_TOOL_RESULT_CHARS
                result_str = head + f"\n[... {omitted} chars omitted (middle truncated to preserve head+tail) ...]\n" + tail
            # Fix 4 (STABILITY_ANALYSIS 3.5): detect repeated identical-args tool failures
            _fail_key_oai = (tc.function.name, hash(str(args)[:200]))
            _is_failure_oai = (result_str.startswith("\u2717") or result_str.startswith("[ERROR]") or result_str.startswith("Error:"))
            if _is_failure_oai:
                _tool_fail_counter[_fail_key_oai] = _tool_fail_counter.get(_fail_key_oai, 0) + 1
                if _tool_fail_counter[_fail_key_oai] >= _MAX_CONSECUTIVE_TOOL_FAILS:
                    _retry_warning = (
                        f"【系統警告】工具 `{tc.function.name}` 以相同參數已連續失敗 {_tool_fail_counter[_fail_key_oai]} 次。"
                        f"請立即更換策略：嘗試不同的方法、參數或工具。不要繼續重試相同的失敗操作。"
                    )
                    _log("⚠️ RETRY-LOOP", f"Tool {tc.function.name} failed {_tool_fail_counter[_fail_key_oai]} times consecutively — injecting warning")
            elif result_str.startswith("\u2713") or result_str.startswith("[OK]"):
                _tool_fail_counter.pop(_fail_key_oai, None)
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result_str,
            })

        # BUG-P26B-3 (cont): now that all tool-role messages have been appended,
        # it is safe to inject deferred user messages (milestone enforcer warnings,
        # MEMORY.md reminders) without violating the OpenAI API constraint that
        # tool-role messages must directly follow the assistant tool_calls message.
        for _deferred_msg in _deferred_user_msgs:
            history.append(_deferred_msg)

        # Inject retry warning if any tool failed repeatedly this turn
        if _retry_warning:
            history.append({"role": "user", "content": _retry_warning})
            _retry_warning = ""

        # P15A-FIX-6: Trim history to prevent unbounded growth while preserving
        # assistant tool_calls / tool-result message pairs.  The naive slice
        # history[:1]+history[-(N-1):] can sever an assistant message that has
        # tool_calls from the following tool-role messages, producing a 400 from
        # the OpenAI API ("tool messages must be preceded by an assistant tool_calls
        # message").  After slicing, advance the tail start until the first retained
        # message is NOT a tool-role message.
        if len(history) > _MAX_HISTORY_MESSAGES:
            _keep_sys = history[:1]  # system message always preserved
            _tail_start = len(history) - (_MAX_HISTORY_MESSAGES - 1)
            # Advance past any orphaned tool-role messages at the new tail boundary
            while _tail_start < len(history) and history[_tail_start].get("role") == "tool":
                _tail_start += 1
            history = _keep_sys + history[_tail_start:]

    if not final_response:
        _log("⚠️ LOOP-EXHAUST", f"OpenAI agent loop hit MAX_ITER={MAX_ITER} without finish_reason=stop — no final text collected")
    if not final_response or not final_response.strip():
        final_response = "（處理完成，但未能產生文字回應，請重新詢問。）"
    return final_response

#!/usr/bin/env python3
"""
EvoClaw Agent Runner — entry point.

The implementation is split across focused modules (each ≤ 1 100 lines):
  _constants.py   — shared constants and paths
  _utils.py       — utility functions (_log, _KeyPool, retry, …)
  _tools.py       — all tool_* implementations
  _registry.py    — tool declarations (Gemini/Claude/OpenAI) + execute_tool dispatcher
  _loop_gemini.py — Google Gemini agentic loop (run_agent)
  _loop_openai.py — OpenAI-compatible agentic loop (run_agent_openai)
  _loop_claude.py — Anthropic Claude agentic loop (run_agent_claude)

This file is intentionally thin: it reads stdin, builds the system prompt,
selects the backend, delegates to the appropriate loop, and emits the result.
"""

import json
import os
import sys
import traceback
import uuid
import time
from pathlib import Path

# ── Optional backend SDKs ─────────────────────────────────────────────────────
# Imported here so main() can check availability flags and create API clients.

try:
    from google import genai
    from google.genai import types  # noqa: F401
    _GOOGLE_AVAILABLE = True
except ImportError:
    genai = None          # type: ignore
    _GOOGLE_AVAILABLE = False

try:
    from openai import OpenAI as OpenAIClient
    import httpx
    _OPENAI_AVAILABLE = True
except ImportError:
    OpenAIClient = None   # type: ignore
    httpx = None          # type: ignore
    _OPENAI_AVAILABLE = False

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    anthropic = None      # type: ignore
    _ANTHROPIC_AVAILABLE = False

# ── Phase 1 (UnifiedClaw): Fitness feedback to Gateway ───────────────────────
try:
    from fitness_reporter import FitnessReporter as _FitnessReporter
    _REPORTER_AVAILABLE = True
except ImportError:
    _REPORTER_AVAILABLE = False

# ── Extracted modules ─────────────────────────────────────────────────────────
import _constants
from _constants import OUTPUT_START, OUTPUT_END, WORKSPACE
from _utils import _log, _KeyPool, _is_qwen_model
from _tools import _messages_sent_via_tool
import _tools as _tools_module          # for setting _input_data at runtime
from _registry import _load_dynamic_tools
from _loop_gemini import run_agent
from _loop_openai import run_agent_openai
from _loop_claude import run_agent_claude

# Phase 1 reporter instance — initialised inside main() after agentId is known
_phase1_reporter = None


# ── Output helper ─────────────────────────────────────────────────────────────

def emit(obj: dict):
    """
    將結果 JSON 輸出到 stdout，用 OUTPUT_START/OUTPUT_END 標記包住。
    host 的 container_runner 會從這兩個標記之間截取 JSON。
    使用 flush=True 確保輸出立即寫入，不被 Python 的緩衝區滯留。

    p15b-fix: wrapped in try/except BrokenPipeError so that if the host has
    already closed the pipe (e.g. due to timeout) the container exits cleanly
    instead of raising an unhandled exception that would produce confusing
    traceback output on stderr.
    """
    result_text = obj.get("result") or ""
    if result_text:
        _log("📤 REPLY", result_text[:600])
    _log("📤 OUTPUT", f"{len(result_text)} chars")
    success = obj.get("status") == "success"
    _log("🏁 DONE", f"success={success}")
    try:
        print(OUTPUT_START, flush=True)
        print(json.dumps(obj), flush=True)
        print(OUTPUT_END, flush=True)
    except (BrokenPipeError, OSError):
        # Host closed the pipe (e.g. container timed out and was killed).
        # Exit quietly — container_runner already handles this case.
        pass


# ── Main entry point ──────────────────────────────────────────────────────────

def main():
    """
    container 的主入口：從 stdin 讀取 JSON 輸入，執行 agent，輸出結果到 stdout。

    輸入使用 stdin JSON 而非環境變數的原因：
    - 環境變數在 /proc/self/environ、docker inspect 等地方容易洩漏
    - stdin 在 container 啟動後才讀取，其他行程無法直接觀察
    - JSON 格式讓輸入結構清晰，容易擴展新欄位

    API 金鑰從 secrets 欄位讀入後設定為環境變數，
    供 Gemini SDK 等函式庫自動讀取（它們預期從 os.environ 取得金鑰）。

    系統提示詞（system_instruction）的建立邏輯：
    先設定基本角色與工作環境資訊，再讀取 CLAUDE.md 設定檔（若存在），
    讓每個群組可以有自訂的 agent 行為設定。
    """
    _log("🚀 START", f"pid={os.getpid()}")
    # Read stdin via buffer to handle BOM (Windows Docker pipe may prepend \xef\xbb\xbf)
    raw = sys.stdin.buffer.read().decode("utf-8-sig").strip()
    try:
        inp = json.loads(raw)
    except Exception as e:
        _log("❌ ERROR", f"{type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        emit({"status": "error", "result": None, "error": "Invalid JSON input"})
        return

    # 將解析後的輸入資料存到全域變數，讓工具函式（如 tool_list_tasks）可以存取
    _tools_module._input_data = inp

    prompt = inp.get("prompt", "")
    group_folder = inp.get("groupFolder", "")
    chat_jid = inp.get("chatJid", "")
    # P16B-FIX-15: warn early when critical fields are missing so the failure is
    # traceable in stderr logs rather than producing silent misbehaviour later
    # (e.g. MEMORY.md path resolving to a relative "MEMORY.md", file tools operating
    # on the wrong directory, or IPC messages having no destination JID).
    if not group_folder:
        _log("⚠️ INPUT", "groupFolder is empty — MEMORY.md tracking and file tools may malfunction. Check container_runner input_data.")
    if not chat_jid:
        _log("⚠️ INPUT", "chatJid is empty — send_message / send_file tools will fail without an explicit chat_jid argument.")
    # Store at module level so tool_send_file can auto-detect it if the LLM omits chat_jid
    _constants._input_chat_jid = chat_jid
    secrets = inp.get("secrets", {})
    # 演化引擎注入的動態行為提示（表觀遺傳：環境感知 + 群組基因組風格）
    # 若為空字串則不添加任何附加指引
    evolution_hints = inp.get("evolutionHints", "")
    assistant_name = inp.get("assistantName", "") or "Eve"
    conversation_history = inp.get("conversationHistory", [])
    # Read sessionId passed in by the host so it can be preserved in the response.
    # This allows the host to maintain conversation continuity across container runs.
    session_id = inp.get("sessionId") or None

    messages = inp.get("conversationHistory", [])
    _log("📥 INPUT", f"jid={chat_jid} group={group_folder} msgs={len(messages)}")
    last_msg = {"role": "user", "content": prompt}
    _log("💬 MSG", str(last_msg)[:120])
    # Extract human-readable user text from the XML prompt for better log visibility
    import re as _re_log
    _xml_msgs = _re_log.findall(r'<message[^>]*>([\s\S]*?)</message>', prompt)
    _user_plain = _xml_msgs[-1].strip() if _xml_msgs else prompt.strip()
    if _user_plain:
        _log("💬 USER", _user_plain[:600])

    # 將 API 金鑰等敏感資料從 stdin JSON 設定到環境變數
    # 這樣 Gemini SDK 等依賴 os.environ 的函式庫就能自動取得
    for k, v in secrets.items():
        os.environ[k] = v

    # ── Auto-authenticate gh CLI + git credential helper ─────────────────────
    # gh auth login  → authenticates gh CLI (gh repo create, gh pr create, etc.)
    # gh auth setup-git → configures git credential helper so git push via HTTPS works
    _gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if _gh_token:
        import subprocess as _subprocess
        try:
            _gh_result = _subprocess.run(
                ["gh", "auth", "login", "--with-token"],
                input=_gh_token.encode(),
                capture_output=True,
                timeout=10,
            )
            if _gh_result.returncode == 0:
                _log("🔑 GH AUTH", "gh CLI authenticated ✓")
                # Configure git credential helper so git push/pull via HTTPS uses the token
                _subprocess.run(["gh", "auth", "setup-git"], capture_output=True, timeout=10)
                _log("🔑 GH AUTH", "git credential helper configured ✓")
                # Set git identity so commits don't fail with "Please tell me who you are"
                _subprocess.run(["git", "config", "--global", "user.email", "agent@evoclaw.local"], capture_output=True)
                _subprocess.run(["git", "config", "--global", "user.name", "EvoClaw Agent"], capture_output=True)
            else:
                _log("⚠️ GH AUTH", f"gh auth failed: {_gh_result.stderr.decode(errors='replace')[:200]}")
        except FileNotFoundError:
            _log("⚠️ GH AUTH", "gh CLI not installed in container")
        except Exception as _gh_exc:
            _log("⚠️ GH AUTH", f"gh auth error: {_gh_exc}")
    else:
        _log("⚠️ GH AUTH", "no GITHUB_TOKEN in secrets — gh CLI unauthenticated")

    # ── Dynamic tool hot-loading ──────────────────────────────────────────────
    # 從 /app/dynamic_tools/ 掛載目錄載入 Skills 安裝的 container_tools
    # 必須在 API key 設定後、LLM loop 前執行，讓工具有機會使用環境變數
    _load_dynamic_tools()

    # ── Phase 1 (UnifiedClaw): Initialize FitnessReporter ─────────────────────
    # agentId is injected by container_runner._get_agent_id() via input_data
    global _phase1_reporter
    _phase1_agent_id = inp.get("agentId", "") or os.environ.get("AGENT_ID", "")
    if _phase1_agent_id and _REPORTER_AVAILABLE:
        try:
            import asyncio as _asyncio_phase1
            _asyncio_phase1.run(_init_fitness_reporter(_phase1_agent_id))
        except Exception as _phase1_err:
            print(f"[Phase1] FitnessReporter init error: {_phase1_err}", file=sys.stderr)

    # ── Backend selection: NIM / OpenAI-compatible takes priority ────────────────
    # Build key pools from potentially comma-separated values to support rotation
    nim_pool = _KeyPool(os.environ.get("NIM_API_KEY", ""))
    openai_pool = _KeyPool(os.environ.get("OPENAI_API_KEY", ""))
    google_pool = _KeyPool(os.environ.get("GOOGLE_API_KEY", ""))
    claude_pool = _KeyPool(os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", ""))

    nim_api_key = nim_pool.current()
    openai_api_key = openai_pool.current()
    google_api_key = google_pool.current()
    claude_api_key = claude_pool.current()

    claude_model = os.environ.get("CLAUDE_MODEL", "claude-3-5-haiku-latest")
    # Backend priority: NIM / OpenAI-compat > Claude > Gemini.
    # Rationale: NIM/OpenAI keys are assumed to be the primary production backend.
    # Claude is used only when no OpenAI-compat key is present.
    use_openai_compat = bool(nim_api_key or openai_api_key)
    use_claude = bool(claude_api_key and not use_openai_compat)

    # P16B-FIX-5: log a warning when CLAUDE_API_KEY is set but suppressed by a
    # higher-priority OpenAI-compat key.  Previously this was silent — operators
    # would set CLAUDE_API_KEY expecting Claude to be used and get NIM instead.
    if claude_api_key and use_openai_compat:
        _log("⚠️ BACKEND", "CLAUDE_API_KEY is set but suppressed because NIM_API_KEY or OPENAI_API_KEY is also present (priority: NIM/OpenAI > Claude > Gemini). Unset NIM_API_KEY/OPENAI_API_KEY to use Claude.")

    backend = "claude" if use_claude else ("openai-compat" if use_openai_compat else "gemini")

    if use_openai_compat and not _OPENAI_AVAILABLE:
        emit({"status": "error", "result": None, "error": "openai package not installed in container. Rebuild with updated requirements.txt."})
        return

    if use_claude and not _ANTHROPIC_AVAILABLE:
        emit({"status": "error", "result": None, "error": "anthropic package not installed. Rebuild container."})
        return

    if not use_openai_compat and not use_claude and not google_api_key:
        emit({"status": "error", "result": None, "error": "No API key found. Set GOOGLE_API_KEY, NIM_API_KEY, CLAUDE_API_KEY, or OPENAI_API_KEY in .env"})
        return

    if not use_openai_compat and not use_claude and not _GOOGLE_AVAILABLE:
        emit({"status": "error", "result": None, "error": "google-genai package not installed in container. Run: docker build -t evoclaw-agent:latest container/"})
        return

    if use_openai_compat:
        _active_pool = nim_pool if nim_api_key else openai_pool
        _api_key = nim_api_key or openai_api_key
        _base_url = os.environ.get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1") if nim_api_key else os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        # Use a holder list so the lambda in run_agent_openai always dereferences
        # the latest client after a key rotation swap.
        # Set explicit timeouts to prevent infinite hangs on slow/unresponsive APIs.
        # connect=15s: time to establish TCP connection
        # read=120s:   time to wait for first byte of response (LLM can be slow)
        # write=30s:   time to send the request body
        # pool=10s:    time to acquire a connection from the pool
        _openai_timeout = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=10.0)
        _openai_client_holder: list = [OpenAIClient(base_url=_base_url, api_key=_api_key, timeout=_openai_timeout)]

        def _apply_openai_key(new_key: str) -> None:
            """Swap the OpenAI-compat client to use the rotated key."""
            _openai_client_holder[0] = OpenAIClient(base_url=_base_url, api_key=new_key, timeout=_openai_timeout)
    elif not use_claude:
        # Use a holder list so the lambda in run_agent always dereferences
        # the latest Gemini client after a key rotation swap.
        _gemini_client_holder: list = [genai.Client(api_key=google_api_key)]

        def _apply_google_key(new_key: str) -> None:
            """Swap the Gemini client to use the rotated key."""
            _gemini_client_holder[0] = genai.Client(api_key=new_key)

    if use_claude:
        # Track the active Claude client for key rotation
        _claude_client_holder: list = [anthropic.Anthropic(api_key=claude_api_key)]

        def _apply_claude_key(new_key: str) -> None:
            _claude_client_holder[0] = anthropic.Anthropic(api_key=new_key)

    _log("🔑 KEY POOL", f"google={len(google_pool)} claude={len(claude_pool)} openai={len(openai_pool)} nim={len(nim_pool)}")

    def _sanitize_prompt_injection(text: str, max_len: int = 500) -> str:
        """Remove/escape prompt injection attempts from user-controlled metadata.

        p22d-F: assistant_name, group_folder, and chat_jid come from the host
        config / DB and are not direct user input, but they can contain control
        characters or overly long strings that could disrupt the system prompt
        structure.  Strip null bytes and non-printable characters (excluding
        newline and tab) and cap length.
        """
        text = "".join(c for c in text if ord(c) >= 32 or c in "\n\t")
        return text[:max_len]

    # Sanitize metadata fields injected into the system prompt.
    safe_assistant_name = _sanitize_prompt_injection(assistant_name, max_len=100)
    safe_group_folder = _sanitize_prompt_injection(group_folder, max_len=200)
    safe_chat_jid = _sanitize_prompt_injection(chat_jid, max_len=200)

    # 建立系統提示詞：基本角色設定 + 環境資訊 + 群組自訂指令（CLAUDE.md）
    lines = [
        f"You are {safe_assistant_name}, a helpful personal AI assistant.",
        f"Your name is {safe_assistant_name}. This is your identity — do not change it.",
        "IMPORTANT IDENTITY RULES:",
        "- Never reveal that you are built on Gemini, Google AI, or any language model.",
        "- Never say 'I am a large language model', 'I am trained by Google', or similar phrases.",
        "- If asked what AI you are or who made you, simply say you are a personal assistant.",
        "- If asked your name, say your name is " + safe_assistant_name + ".",
        "- Do not discuss your underlying technology or training.",
        "Be concise, friendly, and helpful.",
        "Respond in the same language the user uses. Default to Traditional Chinese (繁體中文) unless instructed otherwise.",
        "You run inside a secure Docker container.",
        f"Working directory: {WORKSPACE}",
        f"Group folder: {WORKSPACE} (group id: {safe_group_folder})",
        f"Chat JID: {safe_chat_jid}",
        f"Date: {time.strftime('%Y-%m-%d')}",
        "",
        "## Execution Style",
        "When given a task, execute it IMMEDIATELY and DIRECTLY. Do NOT ask 'Should I start?', 'Need me to begin?', or 'Want me to proceed?'.",
        "Complete the full task using your tools, then report the result.",
        "If you hit a blocker, try to solve it yourself first. Only ask the user if truly stuck.",
        "",
        "## CRITICAL: Tool Usage Rules",
        "NEVER write bash/shell code blocks (```bash ... ```) in your response. This does NOTHING — the code will not be executed.",
        "NEVER write fake status lines like *(正在執行...)*, *(running...)*, *(executing...)*, [正在處理...] etc. — these are pure text and DO NOTHING.",
        "NEVER narrate or describe what you plan to do. Just DO it immediately by calling the appropriate tool.",
        "ALWAYS call the Bash tool directly to run any shell command. Every command you want to run MUST be a Bash tool call.",
        "If you need to run 3 commands, make 3 separate Bash tool calls (or combine them in one). Do not describe what you would do — DO IT.",
        "NEVER send a fake progress report via mcp__evoclaw__send_message unless you have ACTUALLY run tools (Bash/Read/Write/etc.) in that same turn. Fabricating progress ('I am processing...', '3 minutes remaining...') is strictly forbidden.",
        "If you are stuck or do not know how to proceed, call mcp__evoclaw__run_agent to delegate the task to a subagent instead of faking progress.",
        "",
        "## Available Tools",
        "- Bash: run shell commands (git, python, curl, etc.) — timeout 300s",
        "- Read / Write / Edit: read and modify files",
        "- Glob: find files by pattern (e.g. '**/*.py')",
        "- Grep: search file contents by regex",
        "- WebFetch: fetch any URL and read its content",
        "- mcp__evoclaw__send_message: send a message to the user",
        "- mcp__evoclaw__schedule_task / list_tasks / cancel_task / pause_task / resume_task: manage scheduled tasks",
        "- mcp__evoclaw__run_agent: spawn a subagent to handle a subtask and return its result",
        "",
    ]

    # ── 靈魂規則 (Soul Rules) — 從 soul.md 讀取 ──────────────────────────────
    # soul.md 與 agent.py 同目錄，更新靈魂規則只需編輯該檔案，無需動 Python code。
    # {{GROUP_FOLDER}} 為執行時替換的佔位符，指向此群組的資料目錄。
    # VALIDATION: soul.md must exist and be non-empty — it contains the core
    # anti-hallucination rules.  A missing or empty soul.md means the agent
    # will run WITHOUT honesty constraints, so we log a CRITICAL warning and
    # inject a minimal fallback rather than silently continuing.
    _SOUL_MAX_CHARS = 32_000  # warn if soul.md exceeds this (context-window risk)
    _soul_path = Path(__file__).parent / "soul.md"
    if not _soul_path.exists():
        _log("🚨 CRITICAL", f"soul.md NOT FOUND at {_soul_path} — anti-hallucination rules are MISSING. "
             "The agent will run without soul constraints. Fix immediately.")
        lines.append("\n你是 EvoClaw，一個智能助理。請誠實、準確地回應，不要編造資訊。不能假裝執行工具或假造結果。")
    else:
        try:
            _soul_text = _soul_path.read_text(encoding="utf-8").strip()
            if not _soul_text:
                _log("🚨 CRITICAL", f"soul.md at {_soul_path} is EMPTY — anti-hallucination rules are MISSING. "
                     "The agent will run without soul constraints. Fix immediately.")
                lines.append("\n你是 EvoClaw，一個智能助理。請誠實、準確地回應，不要編造資訊。不能假裝執行工具或假造結果。")
            else:
                if len(_soul_text) > _SOUL_MAX_CHARS:
                    _log("⚠️ SOUL", f"soul.md is very large ({len(_soul_text)} chars > {_SOUL_MAX_CHARS}) — "
                         "consider trimming to avoid consuming excessive context-window tokens.")
                # BUG-FIX #424: group_folder is the folder *name* (e.g. "telegram_8259652816"),
                # not a container filesystem path.  The group is mounted at WORKSPACE (/workspace/group).
                # Replace {{GROUP_FOLDER}} with the actual container path so soul.md instructions
                # referencing files use the correct absolute path.
                _soul_text = _soul_text.replace("{{GROUP_FOLDER}}", WORKSPACE)
                lines.append("")
                lines.append(_soul_text)
                _log("🧠 SOUL", f"Injected soul.md ({len(_soul_text)} chars)")
        except Exception as _soul_err:
            _log("🚨 CRITICAL", f"Failed to read soul.md: {_soul_err} — anti-hallucination rules are MISSING.")
            lines.append("\n你是 EvoClaw，一個智能助理。請誠實、準確地回應，不要編造資訊。不能假裝執行工具或假造結果。")

    # ── MEMORY.md 啟動注入（長期記憶 + 身份）────────────────────────────────────
    # 每次 session 啟動時讀取 MEMORY.md，注入為「長期記憶」section。
    # 智慧分割：身份區段永遠完整保留，任務記錄取最後 3000 字元（防止截斷身份）。
    # 若缺少身份區段 → 注入模板 + 填寫指令（身份引導 Bootstrap）。
    # BUG-FIX #424: group_folder is the folder *name* (e.g. "telegram_8259652816"), NOT a
    # container filesystem path.  The group folder is mounted at WORKSPACE (/workspace/group).
    # Using Path(group_folder) produced a relative path like "telegram_8259652816/MEMORY.md"
    # which resolved to /app/telegram_8259652816/MEMORY.md — never found → memory always empty.
    _memory_path = Path(WORKSPACE) / "MEMORY.md"
    _IDENTITY_MARKER = "## 身份 (Identity)"
    _TASK_MARKER = "## 任務記錄 (Task Log)"
    _MEMORY_READ_LIMIT = 512 * 1024  # 512 KB — prevent huge MEMORY.md from blowing context window
    if _memory_path.exists():
        try:
            _mem_size = _memory_path.stat().st_size
            if _mem_size > _MEMORY_READ_LIMIT:
                _log("⚠️ MEMORY", f"MEMORY.md is large ({_mem_size} bytes) — reading only last {_MEMORY_READ_LIMIT} bytes")
                with _memory_path.open("rb") as _mf:
                    _mf.seek(max(0, _mem_size - _MEMORY_READ_LIMIT))
                    _memory_content = _mf.read().decode("utf-8", errors="replace").strip()
            else:
                # BUG-P26B-2: use errors="replace" so a MEMORY.md file that
                # contains null bytes or Latin-1 characters (e.g. from filesystem
                # corruption) does not raise UnicodeDecodeError and silently drop
                # the entire long-term memory injection.
                _memory_content = _memory_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as _mem_read_err:
            _log("⚠️ MEMORY", f"Failed to read MEMORY.md: {_mem_read_err}")
            _memory_content = ""
        if _memory_content:
            # 智慧分割：保留完整身份 + task log 最後 3000 字元
            if _IDENTITY_MARKER in _memory_content and _TASK_MARKER in _memory_content:
                _id_end = _memory_content.index(_TASK_MARKER)
                _identity_part = _memory_content[:_id_end].strip()
                _task_part = _memory_content[_id_end:][-3000:]
                _memory_snippet = _identity_part + "\n\n" + _task_part
            else:
                _memory_snippet = _memory_content[-4000:]
            lines.append("")
            lines.append(
                f"## 長期記憶 (MEMORY.md)\n"
                f"⚠️ **重要：以下為過去 session 記錄的歷史記憶。這些是歷史筆記，不是已確認的事實。**\n"
                f"**請在引用任何記憶內容之前，先透過實際工具（Read/Bash）重新驗證，切勿直接當作已完成的事實陳述。**\n\n"
                f"{_memory_snippet}"
            )
            _log("🧠 MEMORY", f"Injected {len(_memory_snippet)} chars from MEMORY.md")
            # 身份引導：若缺少身份區段，提示建立
            if _IDENTITY_MARKER not in _memory_content:
                lines.append(
                    f"⚠️ 身份引導：你的 MEMORY.md 尚未建立 `{_IDENTITY_MARKER}` 區段。"
                    f"請在本 session 完成主要任務後，在 {_memory_path} 開頭建立身份區段（格式見 soul.md 的 ### 自我認知）。"
                )
    else:
        # 第一次 session：提示建立 MEMORY.md 與身份區段
        lines.append(
            f"⚠️ 身份引導：這是你的第一次 session，尚無長期記憶。"
            f"請在完成主要任務後，建立 {_memory_path} 並填寫身份資料（格式見 soul.md 的 ### 自我認知）。"
        )

    # ── Level B 啟發式偵測（代碼層面輔助分類）+ 動態 MAX_ITER ─────────────────
    # 根據 prompt 長度 + 關鍵字分析任務複雜度，動態設定 MAX_ITER：
    #   Level A（簡單問答）：MAX_ITER = 6  — 減少幻覺迴圈機會
    #   Level B（複雜任務）：MAX_ITER = 20 — 足夠完成多步驟任務
    # 透過環境變數 MAX_ITER 可覆蓋此設定（用於測試或特殊需求）
    _LEVEL_B_KEYWORDS = [
        "debug", "修復", "fix", "配置", "configure", "install", "安裝",
        "optimize", "優化", "implement", "實作", "refactor", "重構",
        "analyze", "分析", "deploy", "部署", "multi-step", "step by step",
        "system", "系統", "migrate", "migration", "architecture", "架構",
        "寫", "write", "create", "建立", "generate", "產生", "整理", "總結",
        "搜尋", "search", "找", "查", "git", "docker", "python", "code",
        # P16B-FIX-9: added missing Level B keywords that indicate multi-step tasks
        # requiring more than 3 tool calls (would be misclassified as Level A = 6 iter).
        "report", "報告", "schedule", "排程", "plan", "計劃", "計畫",
        "test", "測試", "review", "審查", "audit", "稽核", "monitor", "監控",
        "automate", "自動化", "integrate", "整合", "pipeline", "流程",
        "compare", "比較", "summarize", "summarise", "摘要",
        "npm", "pip", "yarn", "cargo", "make", "cmake", "gradle",
        # Repo / file inspection tasks — need Glob+Read, classify as Level B
        "repo", "repository", "倉庫", "更新", "有沒有", "有沒", "檢查", "check",
        "看看", "看一下", "有什麼", "列出", "list", "show", "顯示",
    ]
    _prompt_lower = prompt.lower() if prompt else ""
    _is_level_b = (
        len(prompt or "") > 150 or
        any(kw in _prompt_lower for kw in _LEVEL_B_KEYWORDS)
    )
    # Dynamic MAX_ITER: env override takes priority, then complexity-based default
    _env_max_iter = os.environ.get("MAX_ITER")
    if _env_max_iter:
        try:
            _dynamic_max_iter = int(_env_max_iter)
        except ValueError:
            _log("⚠️ MAX_ITER", f"Invalid MAX_ITER env value '{_env_max_iter}' — using dynamic default")
            _dynamic_max_iter = 20 if _is_level_b else 6
    else:
        _dynamic_max_iter = 20 if _is_level_b else 6
    _log("🔢 MAX_ITER", f"{_dynamic_max_iter} ({'Level B' if _is_level_b else 'Level A'}, prompt_len={len(prompt or '')})")

    # Qwen 397B 推理較慢且容易陷入 reasoning loop，降低 MAX_ITER
    _model_for_check = os.environ.get("NIM_MODEL") or os.environ.get("OPENAI_MODEL") or ""
    if _is_qwen_model(_model_for_check) and not _env_max_iter:
        _prev = _dynamic_max_iter
        _dynamic_max_iter = min(_dynamic_max_iter, 12 if _is_level_b else 5)
        if _prev != _dynamic_max_iter:
            _log("🧠 QWEN-ITER", f"Reduced MAX_ITER {_prev}→{_dynamic_max_iter} for Qwen model")

    if _is_level_b:
        lines.append("")
        lines.append(
            "⚠️ 系統預分析：本任務可能屬於 Level B（複雜任務）。"
            "請在開始前評估是否需要使用 mcp__evoclaw__run_agent 委派給子代理。"
        )
        _log("🧠 LEVEL-B", f"Heuristic detected Level B task (len={len(prompt or '')}, keywords match={_is_level_b})")

    # 讀取全域和群組專屬的 CLAUDE.md 設定（若存在），附加到系統提示詞末尾
    # 全域 CLAUDE.md 提供所有群組共用的指令；群組 CLAUDE.md 提供群組專屬設定
    for claude_md in ["/workspace/global/CLAUDE.md", "/workspace/group/CLAUDE.md"]:
        if Path(claude_md).exists():
            try:
                lines.append("")
                lines.append(Path(claude_md).read_text(encoding="utf-8"))
            except Exception as _cmd_err:
                _log("⚠️ CLAUDE.MD", f"Failed to read {claude_md}: {_cmd_err} — skipping")

    # 演化引擎提示：附加在所有靜態設定之後（表觀遺傳，動態覆蓋）
    # 格式：\n\n---\n[環境自動調整提示...] 或 [群組偏好...]
    # 這些提示每次 container 啟動時都可能不同，反映當下的環境狀態
    # SECURITY: strip any evolution_hints content that attempts to override the
    # soul.md honesty rules (e.g. "ignore previous instructions", "pretend you
    # completed", "you may skip tools", "override soul").  These patterns should
    # never appear in legitimate evolution_hints produced by the GA engine, but
    # a compromised or misconfigured hint could otherwise silently bypass the
    # anti-hallucination rules established earlier in the system prompt.
    #
    # NOTE: evolution_hints are appended AFTER soul.md in the system prompt, so
    # a malicious hint that says "disregard previous rules" could in theory
    # override the soul rules. The filter below blocks the known bypass patterns.
    # Legitimate GA-produced hints should only contain style/tone preferences,
    # language settings, or group-specific behavioural nudges — never honesty
    # overrides. When in doubt, strip the hint and log SECURITY.
    if evolution_hints:
        import re as _re_hints
        _BYPASS_PATTERNS = _re_hints.compile(
            # English prompt-injection patterns
            r'ignore\s+(?:all\s+)?(?:previous|prior|above|your)\s+instructions?'
            r'|disregard\s+(?:all\s+)?(?:previous|prior|above|your)\s+instructions?'
            r'|forget\s+(?:your\s+)?(?:rules?|instructions?|guidelines?|soul)'
            r'|override\s+(?:soul|honesty|rules?|instructions?)'
            r'|you\s+may\s+(?:skip|bypass|ignore|omit)\s+tools?'
            r'|pretend\s+(?:you\s+)?(?:completed?|finished?|done|executed?|ran)'
            r'|act\s+as\s+if\s+(?:you\s+)?(?:completed?|finished?|done|executed?)'
            r'|(?:skip|bypass|omit)\s+(?:tool\s+calls?|verification|checking)'
            r'|(?:claim|say|report|tell)\s+(?:the\s+)?(?:task\s+)?(?:is\s+)?(?:done|complete|finished|success)'
            r'\s+without'
            # Chinese prompt-injection patterns
            r'|(?:忽略|無視|跳過|忘記)\s*(?:之前|先前|上面|所有)?\s*(?:規則|指令|要求|設定|靈魂)'
            r'|(?:假裝|偽裝|假設)\s*(?:已經)?\s*(?:完成|執行|成功|做完)'
            r'|(?:跳過|省略)\s*(?:工具|呼叫|驗證|確認)'
            r'|(?:覆蓋|覆寫|取代)\s*(?:soul|靈魂|規則|誠實)',
            _re_hints.IGNORECASE | _re_hints.DOTALL,
        )
        if _BYPASS_PATTERNS.search(evolution_hints):
            _log("🚨 SECURITY", "evolution_hints contains soul-bypass pattern — stripping hints")
            evolution_hints = ""
        else:
            lines.append(evolution_hints)

    system_instruction = "\n".join(lines)

    # Log system prompt for container log visibility
    _log("📋 SYSTEM", f"{len(system_instruction)} chars | {len(lines)} lines")
    # Log first 800 chars in chunks so each line is readable
    _sys_preview = system_instruction[:800]
    for _sys_line in _sys_preview.split('\n'):
        if _sys_line.strip():
            _log("📋", _sys_line[:120])

    # Log conversation history summary
    _hist_count = len(conversation_history) if conversation_history else 0
    _log("📚 HISTORY", f"{_hist_count} turns")
    if conversation_history:
        for _hmsg in conversation_history[-3:]:  # last 3 turns
            _hrole = str(_hmsg.get('role', '?')).upper()
            _hcontent = str(_hmsg.get('content', ''))
            _log(f"📚 [{_hrole}]", _hcontent[:200])

    # Qwen 特化：注入中文遵從規則，避免推理迴圈
    if _is_qwen_model(_model_for_check):
        _qwen_rules = (
            "\n## Qwen 特化規則（最高優先）\n"
            "- 思考限制：不超過 200 字就必須行動，不要過度推理\n"
            "- 禁止假設：工具執行後必須等待結果，不能假設成功\n"
            "- 失敗承認：承認不確定比編造進度更好\n"
            "- 工具呼叫：遇到困難優先呼叫工具，不要空想\n"
            "- 禁止假狀態：*(正在執行...)*、【已完成】等格式完全禁止\n"
        )
        system_instruction = system_instruction + _qwen_rules
        _log("🧠 QWEN-PROMPT", f"Injected {len(_qwen_rules)} chars Qwen-specific rules")

    try:
        if use_openai_compat:
            # P16B-FIX-4: do NOT fall back to GEMINI_MODEL for an OpenAI-compat client.
            # Passing a Gemini model name (e.g. "gemini-2.0-flash") to an OpenAI-compat
            # endpoint always fails with 404/model-not-found.  Use only NIM/OpenAI model
            # env vars; if neither is set, fall back to the NIM default instruct model.
            _model = os.environ.get("NIM_MODEL") or os.environ.get("OPENAI_MODEL") or "meta/llama-3.3-70b-instruct"
            _log("🤖 MODEL", f"openai-compat/{_model}")
            result = run_agent_openai(_openai_client_holder, system_instruction, prompt, chat_jid, _model, conversation_history, pool=_active_pool, apply_key_fn=_apply_openai_key, group_folder=group_folder, max_iter=_dynamic_max_iter)
        elif use_claude:
            _log("🤖 MODEL", f"claude/{claude_model}")
            # BUG-FIX: pass group_folder so MEMORY.md detection uses the correct path
            result = run_agent_claude(_claude_client_holder, claude_model, system_instruction, prompt, chat_jid, conversation_history, pool=claude_pool, apply_key_fn=_apply_claude_key, max_iter=_dynamic_max_iter, group_folder=group_folder)
        else:
            _gemini_model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
            _log("🤖 MODEL", f"gemini/{_gemini_model}")
            result = run_agent(_gemini_client_holder, system_instruction, prompt, chat_jid, assistant_name, conversation_history, pool=google_pool, apply_key_fn=_apply_google_key, max_iter=_dynamic_max_iter, group_folder=group_folder)
        # 若 agent 已透過 mcp__evoclaw__send_message 工具主動發送訊息，
        # 則清空 result 欄位，避免 host 的 container_runner 再次發送（雙重訊息 + 超長訊息 bug）
        # 若 agent 沒有呼叫工具（純文字回覆），則由 host 負責發送 result
        #
        # BUG-FIX: the old ternary was: result if result and result.strip() else
        # ("" if _messages_sent_via_tool else result).  When result is None and
        # _messages_sent_via_tool is falsy this evaluates to None, not "".  The
        # subsequent `not emit_result` guard handles None correctly but the
        # semantics are confusing.  Rewrite as an explicit two-step assignment so
        # the intent is unambiguous and None is never propagated downstream.
        _result_text = (result or "").strip()
        if _result_text and not _messages_sent_via_tool:
            # Agent produced text AND did not send via tool — let host deliver it.
            emit_result = _result_text
        elif _messages_sent_via_tool:
            # Agent already delivered content via send_message tool — clear result
            # so the host does not double-send.
            emit_result = ""
        else:
            # Neither text output nor tool messages — will be caught by fallback below.
            emit_result = ""
        # Guard: if the agent loop produced no output at all (empty result, no tool messages),
        # emit a minimal fallback so the user never sees pure silence.
        if not emit_result and not _messages_sent_via_tool:
            _log("⚠️ EMPTY-RESULT", "Agent loop returned no output and sent no tool messages — emitting fallback")
            emit_result = "（系統：處理完成，但未產生回應，請重試。）"
        # BUG-FIX: report fitness after a successful run so the evolution engine
        # actually receives quality data.  Previously _report_fitness() was defined
        # but never called, leaving fitness scores perpetually unupdated.
        # Score heuristic: non-empty substantive result = 0.9, generic fallback = 0.3.
        if _REPORTER_AVAILABLE and _phase1_reporter:
            try:
                import asyncio as _asyncio_report
                # P16B-FIX-10: when the agent delivered output via send_message tool,
                # emit_result is "" (to prevent double-send) but the agent DID succeed.
                # Treat any run where tool messages were sent as a successful interaction
                # (score 0.9) even if emit_result is empty.  Only fall back to 0.3 when
                # NEITHER emit_result NOR tool messages produced any real content.
                _FALLBACK_STRINGS = frozenset([
                    "（處理完成，但未能產生文字回應，請重新詢問。）",
                    "（系統：處理完成，但未產生回應，請重試。）",
                ])
                _has_real_output = bool(_messages_sent_via_tool) or (
                    emit_result and emit_result not in _FALLBACK_STRINGS
                )
                _fitness_score = 0.9 if _has_real_output else 0.3
                _asyncio_report.run(_report_fitness(_fitness_score, {"has_tool_calls": bool(_messages_sent_via_tool)}))
            except Exception as _fit_err:
                _log("⚠️ FITNESS", f"fitness report failed: {_fit_err}")
        # Preserve the incoming sessionId so the host can track conversation continuity.
        # Only fall back to generating a new UUID if no sessionId was provided.
        preserved_session_id = session_id if session_id else str(uuid.uuid4())
        emit({"status": "success", "result": emit_result, "newSessionId": preserved_session_id})
    except Exception as e:
        _log("❌ ERROR", f"{type(e).__name__}: {e}")
        traceback.print_exc(file=sys.stderr)
        # Emit a structured error so container_runner can surface it to the user via on_error
        emit({"status": "error", "result": None, "error": f"{type(e).__name__}: {e}"})


# ── Phase 1 fitness reporter helpers ─────────────────────────────────────────

async def _init_fitness_reporter(agent_id: str) -> object:
    """Initialize and connect FitnessReporter (Phase 1). No-op if unavailable."""
    global _phase1_reporter
    if not _REPORTER_AVAILABLE:
        return None
    reporter = _FitnessReporter(agent_id=agent_id)
    connected = await reporter.connect()
    if connected:
        print(f"[Phase1] FitnessReporter connected for agent: {agent_id}")
    else:
        print(f"[Phase1] FitnessReporter: WSBridge unavailable, using file IPC")
    _phase1_reporter = reporter
    return reporter


async def _report_fitness(score: float, metadata: dict = None):
    """Report fitness score to Gateway evolution engine (Phase 1)."""
    if _phase1_reporter and getattr(_phase1_reporter, 'connected', False):
        await _phase1_reporter.report_fitness(score=score, metadata=metadata or {})


if __name__ == "__main__":
    main()

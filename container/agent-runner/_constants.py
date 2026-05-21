"""Shared constants for the EvoClaw agent runner."""

# container 輸出的邊界標記，host 用這兩個字串從 stdout 截取 JSON 結果
# 必須與 container_runner.py 中定義的常數完全一致
OUTPUT_START = "---EVOCLAW_OUTPUT_START---"
OUTPUT_END = "---EVOCLAW_OUTPUT_END---"

# IPC 目錄路徑（由 host 透過 Docker volume mount 對應到 data/ipc/<folder>/）
IPC_MESSAGES_DIR = "/workspace/ipc/messages"  # agent 發送訊息給用戶
IPC_TASKS_DIR = "/workspace/ipc/tasks"        # agent 建立排程任務
IPC_RESULTS_DIR = "/workspace/ipc/results"

# agent 的工作目錄，對應到 host 的 groups/<folder>/ 目錄
WORKSPACE = "/workspace/group"

# Allowed top-level prefixes for file-system tool operations.
# Paths must resolve inside one of these directories to be accepted.
_ALLOWED_PATH_PREFIXES = (
    "/workspace/",   # covers /workspace/group, /workspace/ipc, /workspace/project, etc.
)

# Module-level chat JID — populated from input JSON so tool_send_file can auto-detect it
_input_chat_jid: str = ""

# History / tool-result size limits (P9A)
_MAX_TOOL_RESULT_CHARS = 4000  # ~4KB per tool result
_MAX_HISTORY_MESSAGES = 20     # max messages in history (was 40; #541 OOM at turn=7 with 40 msgs/59KB)

# BUG-P21-1 / BUG-P21-4: Module-level action-claim regex with stricter structure.
# The old pattern matched single Chinese characters like 已/完成/成功 as standalone
# tokens, causing false positives on normal sentences such as "我已了解您的問題".
# The new pattern requires 已+specificVerb or verb+了/完成 structures, reducing noise.
import re as _re_module_level
_ACTION_CLAIM_RE = _re_module_level.compile(
    r'(?:'
    r'已(?:完成|修復|修正|部署|更新|新增|刪除|建立|創建|執行|運行|安裝|設定|配置|提交|推送|合併)'
    r'|(?:完成|修復|修正|部署|更新|新增|刪除|建立|創建|執行|運行|安裝|設定|配置|提交|推送|合併)了'
    r'|(?:successfully|completed|deployed|fixed|updated|committed|pushed|merged)\s+(?:the\s+)?(?:fix|update|feature|change|patch|code|file)'
    r')',
    _re_module_level.IGNORECASE,
)


def is_unverified_action_claim(text: str, substantive_action_count: int) -> bool:
    """Decide whether a text-only model turn claims action completion *without* having
    done any real tool work in the current run.

    BUG FIX (記憶查看器 issue follow-up): the SEMANTIC-FAKE / FAKE-STATUS checks in
    `_loop_gemini.py`, `_loop_openai.py`, and `_loop_claude.py` previously wiped
    ``final_response`` whenever the regex matched **on a text-only turn**.  In every
    healthy multi-turn agentic run the final turn is *always* text-only by design
    (it summarises the work done in the preceding tool turns), so the legitimate
    closing sentence — ``"MCP 安全檢查 skill 已建立完成"``, ``"已更新 README.md"``,
    ``"Successfully completed the fix"`` — matched the regex and was wiped.  The
    loop then ran until MAX_ITER and emitted ``"（處理完成，但未能產生文字回應，
    請重新詢問。）"`` to the user despite the agent having actually completed the
    work.

    Fix: only treat the claim as *unverified* when ``substantive_action_count == 0``,
    i.e. the agent has not executed a single substantive tool (Bash/Read/Write/Edit/
    Glob/Grep/WebFetch/run_agent) in the entire run.  When real work happened the
    completion claim is, by definition, *verified by execution* — let it through.
    """
    if substantive_action_count > 0:
        return False
    return bool(_ACTION_CLAIM_RE.search(text))

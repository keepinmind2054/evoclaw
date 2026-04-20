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

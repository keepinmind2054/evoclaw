# Changelog

All notable changes to EvoClaw will be documented in this file.

## [1.5.0] — 2026-03-10

### New Features

**Agent 新工具：Glob、Grep、WebFetch**
- `Glob` — 用 glob 模式搜尋檔案（支援 `**` 遞迴），例如 `**/*.py` 找出所有 Python 檔
- `Grep` — 用正規表達式搜尋檔案內容，回傳 `檔名:行號:內容` 格式；支援 `include` 過濾副檔名
- `WebFetch` — 抓取任意 URL 內容並轉換為純文字（自動去除 HTML 標籤），最多回傳 12000 字元

**任務管理工具完整化：pause_task / resume_task**
- Agent 可以透過 `mcp__evoclaw__pause_task` 暫停排程任務
- Agent 可以透過 `mcp__evoclaw__resume_task` 恢復已暫停的任務
- 三種工具宣告（Gemini、OpenAI-compatible、Claude）同步更新

**Bash 逾時提升：60s → 300s**
- 支援長時間操作：`git clone`、`pip install`、`npm install`、`docker build` 等

**修正 Anthropic import**
- 補上缺少的 `import anthropic` 及 `_ANTHROPIC_AVAILABLE` 旗標定義
- 解決使用 Claude 後端時的 `NameError` 崩潰

**主動執行 System Prompt**
- 系統提示詞新增「Execution Style」區塊：agent 直接執行任務，不詢問「需要我開始嗎？」
- 列出所有可用工具及說明

**全域 CLAUDE.md 更新**
- 新增「Execution Style」指令段落
- 更新「What You Can Do」完整列出所有工具

### Bug Fixes
- 修正 `_ANTHROPIC_AVAILABLE` 未定義導致 Claude 後端啟動時 `NameError`

---

## [1.4.0] — 2026-03-10

### New Features

**Dashboard 全面重設計 — SPA 架構**
- 從靜態自動刷新頁面升級為單頁應用程式（SPA），左側邊欄導航
- 6 個功能分頁：狀態監控、日誌查看、Agent 管理、系統設定、對話訊息、進化引擎
- SSE（Server-Sent Events）即時日誌串流，0.5 秒推送間隔
- 日誌等級過濾（ALL / DEBUG / INFO / WARNING / ERROR）、暫停/繼續
- Container 停止按鈕（`docker stop`）
- 排程任務直接編輯（修改 schedule_value、取消任務）
- `.env` 查看與編輯（敏感欄位自動遮罩）
- `CLAUDE.md` 多群組編輯器
- 新增 API 端點：`/api/stats`, `/api/agents`, `/api/containers`, `/api/health`, `/api/tasks`, `/api/messages`, `/api/immune`, `/api/task-run-logs`, `/api/evolution/genome`, `/api/evolution/log`, `/api/logs/stream` (SSE), `/api/env`, `/api/claude-mds`

**對話訊息完整紀錄**
- Bot 回應（Docker container 輸出）現在寫入 DB（`is_bot_message=True`）
- `on_output` callback 和 `_ipc_route_fn` 均補上 `db.store_message()` 呼叫
- Dashboard 💬 分頁可查看完整對話（用戶訊息 + Bot 回覆）

**Telegram 頻道重試機制**
- `connect()` 加入最多 3 次重試，指數退避（2s → 4s）
- 偵測到 `Conflict`（另一個 Bot 實例在跑）時立即顯示清楚錯誤，不重試
- 每次失敗後清理殘留的 `_app` 物件再重試

**日誌環形緩衝區**（`host/log_buffer.py`，新檔案）
- 記憶體內環形緩衝區（最多 2000 筆），捕獲所有 Python log records
- 每筆含單調遞增 idx，供 SSE 串流高效查詢新條目
- `get_error_count()` 供 Dashboard topbar 顯示錯誤計數

**Active Container 追蹤**
- `container_runner.py` 新增 `_active_containers` dict + `get_active_containers()`
- 每個 container 啟動時登記，結束時（finally 區塊）自動清除
- Dashboard 狀態監控分頁即時顯示正在運行的 Agent

**頻道載入順序修正**
- `main.py` 改為 `connect()` 成功後才 `register_channel()` + `append()`
- 避免連線失敗的 channel 殘留在已載入列表

### Bug Fixes
- 修正頻道連線失敗時仍被加入 `_loaded_channels`（現改為 connect 成功後才加入）

---

## [1.3.0] — 2026-03-10

### New Features

**Web Dashboard** (`host/dashboard.py`, default port 8765)
- Pure Python stdlib — no external dependencies
- Dark theme dashboard with 9 sections: Groups, Scheduled Tasks, Task Run Logs, Sessions, Messages, Evolution Stats, Evolution Log, Immune Threats
- HTTP Basic Auth via env vars `DASHBOARD_USER` / `DASHBOARD_PASSWORD`
- `/health` endpoint — checks DB + Docker, returns JSON 200/503
- `/metrics` endpoint — Prometheus-format row counts
- Auto-refresh every 10 seconds

**Web Portal** (`host/webportal.py`, default port 8766)
- Browser-based chat interface (polling-based, no WebSocket dependency)
- Group selector, scrollable chat, 1-second polling
- `deliver_reply()` function for pushing bot responses to browser

**Evolution Process Logging**
- New `evolution_log` DB table records every evolution event with full before/after genome snapshot
- 5 event types: `genome_evolved`, `genome_unchanged`, `cycle_start`, `cycle_end`, `skipped_low_samples`
- Dashboard shows last 30 evolution events with color-coded event types

**Agent Tools: list_tasks + cancel_task**
- Container agent can now call `list_tasks()` to see all scheduled tasks
- Container agent can call `cancel_task(task_id)` to cancel a task
- Scheduled tasks are exposed to the agent via `scheduledTasks` in the input payload

**Task Status Tracking**
- Scheduled tasks now have a `status` field: `active`, `error`, `cancelled`
- Orphan tasks (empty `chat_jid`) auto-cleaned on startup
- Scheduler marks tasks `error` when group not found — prevents infinite retry loop

**IPC chat_jid Defensive Fallback**
- `ipc_watcher.py` now resolves `chat_jid` from registered groups by folder name if missing from payload
- Makes old Docker images compatible without rebuild

### New Environment Variables

```
DASHBOARD_PORT=8765        # Web dashboard port
DASHBOARD_USER=admin       # Dashboard Basic Auth username
DASHBOARD_PASSWORD=        # Dashboard Basic Auth password (empty = no auth)
WEBPORTAL_ENABLED=false    # Enable browser chat interface
WEBPORTAL_PORT=8766        # Web portal port
WEBPORTAL_HOST=127.0.0.1  # Web portal bind host
```

### Bug Fixes

- Fixed `[DEBUG]` log messages using `log.info()` instead of `log.debug()`
- Fixed `_running` flags that were set True but never reset to False (changed to `while True:`)
- Fixed `IPC_POLL_INTERVAL` env var parsing (now uses `_env_int()` helper)
- Fixed scheduler infinite warning loop for tasks with empty `chat_jid`
- Fixed `conversation_history` kwarg compatibility in container runner

---

## [1.2.0](https://github.com/qwibitai/evoclaw/compare/v1.1.6...v1.2.0)

[BREAKING] WhatsApp removed from core, now a skill. Run `/add-whatsapp` to re-add (existing auth/groups preserved).
- **fix:** Prevent scheduled tasks from executing twice when container runtime exceeds poll interval (#138, #669)

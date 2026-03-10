# Changelog

本檔案記錄 EvoClaw 專案的所有重要變更。

格式基於 [Keep a Changelog](https://keepachangelog.com/)，
版本號遵循 [語意化版本](https://semver.org/)。

## [1.7.0] - 2026-03-11

### Added
- Superpowers skills integration: 12 installable skill packages from KeithKeepGoing/superpowers
  - superpowers-brainstorming: Design-first gate before any implementation
  - superpowers-dispatching-parallel-agents: Parallel agent dispatch for independent tasks
  - superpowers-executing-plans: Sequential plan execution with checkpoints
  - superpowers-finishing-a-development-branch: Branch completion workflow
  - superpowers-receiving-code-review: Technical evaluation of code review feedback
  - superpowers-requesting-code-review: Dispatch code-reviewer subagent
  - superpowers-subagent-driven-development: Fresh subagent per task with 2-stage review
  - superpowers-systematic-debugging: 4-phase root cause investigation (Iron Law)
  - superpowers-test-driven-development: RED-GREEN-REFACTOR cycle, test first (Iron Law)
  - superpowers-using-git-worktrees: Isolated workspace per feature branch
  - superpowers-verification-before-completion: Evidence-based completion gate (Iron Law)
  - superpowers-writing-plans: Bite-sized implementation plan writing
- Each skill installable via /apply_skill <skill-name> IPC command (main group only)
- Skill files added to docs/superpowers/<name>/SKILL.md

---

## [1.6.1] — 2026-03-10

### Bug Fixes

- **Health Monitor 接入主流程**：`host/health_monitor.py` 的 `health_monitor_loop()` 已加入 `main.py` 的 `asyncio.gather()`，系統啟動後健康監控正式生效（之前程式存在但從未被呼叫）。同時在頂層 import 補上 `from .health_monitor import health_monitor_loop`。
- **WebPortal `deliver_reply()` 修正**：`_process_group_messages()` 的 `on_output()` 回呼現在也呼叫 `deliver_reply(jid, text)`，確保一般訊息（非 IPC 觸發）的 Bot 回覆也能即時推送到 WebPortal 瀏覽器聊天介面。之前 `deliver_reply()` 只在 `_ipc_route_fn()` 中被呼叫，造成正常對話流程的回覆無法顯示在 WebPortal 中。

### Tests

- **新增 `tests/test_core.py`**（30+ 測試案例）：補齊之前缺乏的核心功能測試覆蓋率：
  - **DB 層**（12 tests）：`init_database()`、`store_message()`、`get_new_messages()`（時間戳篩選、`is_bot_message` 排除）、`get_state()`/`set_state()`、`registered_groups` CRUD、task CRUD（create/update/delete/due 查詢）、`session` 儲存、`record_evolution_run()`
  - **Router**（4 tests）：channel 註冊與 `find_channel()` 查找、`format_messages()` 結構化、`route_outbound()` 轉發、無 channel 時不崩潰
  - **IPC Scheduler**（6 tests）：`_compute_next_run()` — interval、once（過去/未來）、cron、無效輸入、未知類型
  - **IPC 權限**（3 tests）：`_require_own_or_main()` — 自己群組放行、主群組跨群組放行、非主群組跨群組 PermissionError
  - **Health Monitor**（4 tests）：`get_health_status()` 回傳格式、`_should_send_warning()` 冷卻機制、`health_monitor_loop()` 停止事件響應
  - **Dev Log**（3 tests）：`_write_dev_log()`/`get_dev_logs()` 寫入讀取、offset 增量、不存在 session 回傳空列表

---

## [1.6.0] — 2026-03-10

### Features

- **DevEngine Dashboard 全面現代化**：`host/dashboard.py` 🛠️ DevEngine 分頁完整重寫，新增以下功能：
  - **Prompt 輸入表單**：直接在 Dashboard 輸入需求 + 選擇模式（auto/interactive）並點擊「▶ 開始建立」啟動 DevEngine，無需透過聊天。對應新 API：`POST /api/dev/start`（自動建立 DevSession 並寫入 IPC 觸發 pipeline）。
  - **7 階段動態 Badge 指示器**：每個 stage（Analyze/Design/Implement/Test/Review/Document/Deploy）顯示即時狀態：⬜ 待處理 → ⏳ 進行中（含 pulse 動畫）→ ⏸ 已暫停 → ✅ 完成。
  - **即時執行日誌終端機**：黑色背景終端機風格，每 2 秒輪詢 `/api/dev/log/<session_id>?offset=N`，自動捲動至最新日誌，依日誌類型著色（成功綠、錯誤紅、暫停黃）。
  - **互動模式確認面板**：session 狀態為 `paused` 時自動出現「▶ 繼續下一階段」+「✕ 停止」按鈕，無需回到聊天室操作。
  - **Toast 通知系統**：取代原 `showMsg()` 一次性訊息，改為右下角堆疊式 Toast，支援成功（綠）/錯誤（紅）/資訊（藍）三種樣式，3.5 秒後自動淡出。
  - **進度條動畫**：CSS `transition: width 0.5s` 讓進度條平滑更新；刷新頻率從 6 秒縮短至 4 秒，活躍 session 日誌終端另有 2 秒獨立輪詢。
  - **Session 分區**：執行中/已暫停的 session 在頂部「Active」卡片顯示，歷史 session（completed/failed/cancelled）分開列表顯示。
- **DevEngine 日誌寫入**：`host/dev_engine.py` 每個 stage 開始/完成/失敗時呼叫 `_write_dev_log()`，將帶時間戳的日誌追加至 `data/dev_logs/<session_id>.log`，供 Dashboard 終端機即時讀取。新增公開函式 `get_dev_logs(session_id, offset)` 回傳增量日誌行。
- **新 API**：
  - `GET /api/dev/log/<session_id>?offset=N` — 回傳從第 N 行起的新日誌行（JSON 字串陣列）
  - `POST /api/dev/start` — 從 Dashboard 建立並觸發 DevEngine session

---

## [1.5.1] — 2026-03-10

### Removed

- **清除死碼**：刪除 `host/web_dashboard.py`（aiohttp 版儀表板，從未被 import）、`host/dashboard_charts.py`（未使用的設定 dict）、整個 `host/stages/` 目錄（8 個全是 TODO 佔位符的 stage 模組，已由 `dev_engine.py` 取代）。

### Bug Fixes

- **`register_channel` 命名衝突**：`host/channels/__init__.py` 的 `register_channel(name, cls)` 改名為 `register_channel_class(name, cls)`，消除與 `host/router.py` 中 `register_channel(ch)`（接受實例）的命名衝突。更新 `host/channels/gmail_channel.py`、`discord_channel.py`、`slack_channel.py`，改以 `from . import register_channel_class as register_channel` 匯入。更新 `skills/` 文件。

### Features

- **Skills Engine IPC 整合**：`host/ipc_watcher.py` 新增 3 種 IPC 訊息類型，讓 agent 可以直接管理 Skill Plugins：
  - `apply_skill`：安裝 Skill Plugin（**僅主群組可用**），payload: `{"type":"apply_skill","skill_path":"skills/add-slack.md","requestId":"r1"}`
  - `uninstall_skill`：移除 Skill Plugin（**僅主群組可用**），payload: `{"type":"uninstall_skill","skill_name":"add-slack","requestId":"r2"}`
  - `list_skills`：列出已安裝的 Skills（任何群組均可查詢），payload: `{"type":"list_skills","requestId":"r3"}`
  - 結果寫入 `data/ipc/<group>/results/<requestId>.json`，同時透過 `route_fn` 發送通知訊息。

### Tests

- **`tests/test_dev_engine.py` 完整重寫**：改用新 API（`DevSession`、`engine.start()`、`engine.run()`、`load_session()`）；移除對已刪除的 `DevContext`、`run_pipeline()`、`engine.context` 的所有引用。新增 `in_memory_db` fixture 確保測試隔離（不污染真實 DB）；用 `unittest.mock.patch` mock LLM 呼叫與 Docker（`_run_llm_stage`、`_deploy_files`），測試不需要 Docker 或 API key 即可執行。

---

## [1.5.0] — 2026-03-10

### Features

- **DevEngine 完整實作**：`host/dev_engine.py` 全面重寫，實現真正的 LLM 驅動 7 階段開發流程（Analyze → Design → Implement → Test → Review → Document → Deploy）。前 6 個階段各自以精心設計的 prompt 呼叫 LLM（透過 `run_container_agent()`），每個階段以前一階段的 artifact 為輸入；Stage 7（Deploy）在 host 進程直接解析 `--- FILE: path ---` 區塊並寫入磁碟。
- **auto / interactive 雙模式**：`auto` 模式全自動跑完所有 7 個階段；`interactive` 模式每個階段完成後暫停，等待用戶確認後繼續。支援 session resume（跳過已完成的階段）。
- **Session 持久化**：新增 `dev_sessions` 資料表至 SQLite（`host/db.py`），完整記錄 session 狀態、每個階段的 artifact 內容、錯誤訊息。
- **IPC 觸發**：`host/ipc_watcher.py` 新增 `dev_task` IPC 訊息類型，agent 可透過寫入 IPC 目錄的方式觸發或 resume DevEngine session。
- **Dashboard 🛠️ DevEngine 分頁**：`host/dashboard.py` 新增第 7 個側邊欄分頁，顯示所有 session 的進度條（n/7 完成）、各階段 artifact 預覽、Resume / Cancel 操作按鈕。新增 API：`/api/dev/sessions`、`/api/dev/session`、POST `/api/dev/resume`、POST `/api/dev/cancel`。

### Bug Fixes

- 修正 `dev_engine.py` 錯誤 import：`from host.container import run_in_container` → `from .container_runner import run_container_agent`
- 修正 `db.get_connection()` → `db.get_db()`（不存在的方法）
- 移除所有 stage 方法中的硬編碼字串輸出（原先為 TODO 佔位符）
- 修正 Stage 5（Review）錯誤地使用 immune system 來審查程式碼 — 改為真正的 LLM code review

---

## [1.4.3] — 2026-03-10

### Features

- **Subagent 親子關係追蹤**：`container_runner.py` 新增 `parent_container` 欄位，記錄每個 container 的父 container 名稱（`None` = 主 agent，有值 = subagent）。
- **即時 Container 活動狀態**：`container_runner.py` 新增 `current_activity` 欄位，非 Windows 路徑改用串流 stderr（逐行讀取 `_log()` 輸出），即時更新 dashboard 顯示 agent 正在執行的動作。
- **Dashboard 容器層級顯示**：`dashboard.py` 的「Active Agent Containers」表格新增 Activity 欄位、按親子關係排序（主 agent 在上，subagent 以 ↳ 縮排在其下），並以不同徽章顏色區分 subagent（黃色）/ scheduled（紫色）/ message（藍色）。

---

## [1.4.2] — 2026-03-10

### Bug Fixes

- **Docker Desktop 日誌空白修正**：`agent.py` 新增 `_log()` 工具函式，在關鍵節點寫入 stderr 進度訊息（啟動、呼叫 LLM、工具呼叫、完成）。修正 Docker Desktop 日誌介面在 container 執行期間顯示空白的問題（根本原因：整個執行過程直到最後才有任何輸出）。

---

## [1.4.1] — 2026-03-10

### Bug Fixes

- **Docker Desktop 日誌顯示修正**：`container_runner.py` 新增 `-e PYTHONUNBUFFERED=1` 至 docker run 指令。修正在無 TTY 模式（`-i`）下 Python stdout 緩衝導致 Docker Desktop 日誌介面無法即時顯示 container 輸出的問題。

---

## [1.6.0] - 2026-03-10

### Added
- **Subagent support** (`mcp__evoclaw__run_agent`): agents can now spawn isolated subagents to handle subtasks and receive results synchronously
  - Parent agent calls `mcp__evoclaw__run_agent(prompt, context_mode)` and blocks until the subagent completes (up to 300s)
  - Subagent runs in a fully isolated Docker container with its own context
  - Results are returned directly to the parent agent via IPC result files
  - `context_mode`: `"isolated"` (fresh context, default) or `"group"` (with conversation history)

### Changed
- `container_runner.py`: IPC volume now includes `results/` subdirectory for subagent result passing
- `ipc_watcher.py`: handles new `spawn_agent` IPC message type

---

## [Unreleased]

### 新增
- **健康監控系統** (`host/health_monitor.py`)
  - 即時監控 Container 排隊數量（警告閾值：10，嚴重閾值：50）
  - 追蹤最近 5 分鐘錯誤率
  - 監控記憶體使用量（警告閾值：500MB）
  - 群組活躍度追蹤
  - 任務狀態分佈統計
  - Docker 守護程序與資料庫健康檢查
  - 自動告警機制（防重複通知，30 分鐘冷卻）

- **免疫系統增強** (`host/evolution/immune.py`)
  - 新增 10 種 injection pattern，總計達 22 種（+83%）
  - 加強英文攻擊檢測：開發者模式、管理員模式、繞過限制等
  - 加強中文攻擊檢測：身份否認、安全解除、模式切換等
  - 提升對多語言混合攻擊的檢測能力

- **測試框架** (`tests/`)
  - 新增免疫系統增強測試套件 (`test_immune_enhanced.py`)
  - 18 個測試用例涵蓋英文/中文攻擊模式
  - 正常對話放行測試
  - 邊界條件測試

- **資料庫優化** (`scripts/add_indexes_migration.py`)
  - 新增 13 個效能索引
  - 涵蓋 messages、evolution_runs、scheduled_tasks 等主要表
  - 預期查詢速度提升 50-90%

- **文檔系統**
  - 新增 `CHANGELOG.md` 記錄所有變更
  - 新增 `RELEASE.md` 發布流程規範
  - 更新 `README.md` 加入健康監控說明

### 改進
- **README.md**
  - 更新功能特色清單
  - 新增健康監控系統章節
  - 更新專案結構圖
  - 補充免疫系統增強說明

- **系統穩定性**
  - 優化資料庫查詢效能
  - 增強安全防護能力
  - 提升系統可監控性

### 修復
- 修正部分 injection pattern 匹配過於寬鬆的問題
- 優化健康監控的資源使用

---

## [1.2.0] - 2026-02-15

### 新增
- Web Dashboard 6 個完整分頁
- SSE 即時日誌串流
- Docker 容器管理功能
- 任務 CRUD 操作介面
- 環境變數編輯器
- 免疫威脅監控面板

### 改進
- Dashboard 效能優化
- 日誌緩衝區大小調整
- UI/UX 微調

---

## [1.1.0] - 2026-01-20

### 新增
- 進化引擎完整實現
- 適應度追蹤系統
- 群組基因組演化
- 免疫系統（12 種 injection pattern）
- 24 小時演化週期

### 改進
- 多模型支援優化
- 容器啟動速度提升

---

## [1.0.0] - 2025-12-01

### 新增
- 初始版本發布
- 支援 Telegram、Discord、Slack、Gmail
- Docker 容器隔離
- 排程任務系統
- 多模型支援（Gemini、OpenAI 相容、Claude）
- 群組隔離與記憶
- Web Portal 瀏覽器介面

---

## 版本說明

### 語意化版本
- **MAJOR.MINOR.PATCH** (例如：1.2.3)
- **MAJOR**：不相容的 API 變更
- **MINOR**：向後相容的功能新增
- **PATCH**：向後相容的問題修正

### 標記說明
- `[Unreleased]` - 尚未發布的變更
- 日期格式：`YYYY-MM-DD`

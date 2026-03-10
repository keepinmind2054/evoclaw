# Changelog

本檔案記錄 EvoClaw 專案的所有重要變更。

格式基於 [Keep a Changelog](https://keepachangelog.com/)，
版本號遵循 [語意化版本](https://semver.org/)。

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

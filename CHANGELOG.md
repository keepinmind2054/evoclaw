# Changelog

本檔案記錄 EvoClaw 專案的所有重要變更。

格式基於 [Keep a Changelog](https://keepachangelog.com/)，
版本號遵循 [語意化版本](https://semver.org/)。

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

# Release Notes

## v1.7.0 — Superpowers Skills Integration (2026-03-11)

This release integrates the [Superpowers](https://github.com/KeithKeepGoing/superpowers) methodology as installable skills packages into EvoClaw's Skills Engine.

### What's New

#### 12 Superpowers Skills as Installable Packages

All 12 Superpowers workflow skills are now packaged for EvoClaw's skills_engine, installable via the `/apply_skill` IPC command from the main group.

| Skill | Purpose |
|---|---|
| superpowers-brainstorming | Design-first gate before any implementation |
| superpowers-dispatching-parallel-agents | Parallel agent dispatch for independent domains |
| superpowers-executing-plans | Sequential plan execution with review checkpoints |
| superpowers-finishing-a-development-branch | Verify → choose merge/PR/keep/discard |
| superpowers-receiving-code-review | Technical evaluation, not performative agreement |
| superpowers-requesting-code-review | Dispatch code-reviewer subagent after each task |
| superpowers-subagent-driven-development | Fresh subagent per task + 2-stage review |
| superpowers-systematic-debugging | 4-phase root cause analysis (Iron Law: no fix without root cause) |
| superpowers-test-driven-development | RED-GREEN-REFACTOR (Iron Law: test first always) |
| superpowers-using-git-worktrees | Isolated workspace with clean baseline verification |
| superpowers-verification-before-completion | Evidence-based gate before claiming completion |
| superpowers-writing-plans | Bite-sized atomic task plans with TDD steps |

#### Installation

From the main group, send:
```
/apply_skill superpowers-brainstorming
```
Or via IPC:
```json
{"type": "apply_skill", "skill": "superpowers-brainstorming"}
```

#### Architecture

Each skill package follows the structure:
```
skills/superpowers-<name>/
  manifest.yaml          # Skills engine metadata
  add/
    docs/superpowers/
      <name>/
        SKILL.md         # Full skill instructions
```

### Previous Release

See CHANGELOG.md for full history.

---

## v1.6.1 — 2026-03-10

> Bug Fix 版：Health Monitor 接入、WebPortal 修正、核心測試補齊

### Bug Fixes

**Health Monitor 終於上線**

`health_monitor.py` 一直存在但從未被啟動。本版本將 `health_monitor_loop()` 接入 `asyncio.gather()`，現在每 60 秒自動檢查：Container 排隊、錯誤率、記憶體用量（閾值 500MB 警告）、DB 大小（閾值 100MB 警告）。

**WebPortal 回覆修正**

一般對話（非 IPC 觸發）的 Bot 回覆現在也會推送到 WebPortal（`http://localhost:8766`）。之前只有 IPC 路徑有呼叫 `deliver_reply()`，正常聊天流程的回覆不會顯示在瀏覽器。

### Tests

新增 `tests/test_core.py`，30+ 個測試案例覆蓋：DB 層（12）、Router（4）、IPC Scheduler（6）、IPC 權限（3）、Health Monitor（4）、Dev Log（3）。

### 升級方式

```bash
git pull
python run.py
```

---

## v1.6.0 — 2026-03-10

> DevEngine Dashboard 全面現代化 — 直接從 Dashboard 啟動、監控、控制開發流程

### 新功能

**1. 直接從 Dashboard 啟動 DevEngine**

不再需要透過聊天室輸入 IPC 指令。在 🛠️ DevEngine 分頁直接輸入需求、選擇模式，點擊按鈕即可啟動。

**2. 7 階段動態 Badge 指示器**

每個 stage 即時顯示 ⬜ 待處理 / ⏳ 進行中（閃爍動畫）/ ⏸ 暫停 / ✅ 完成。

**3. 即時執行日誌終端機**

黑色背景、等寬字體，每 2 秒自動輪詢新日誌，自動捲動。範例：

```
[14:23:01] 🚀 DevEngine 啟動（mode=auto）
[14:23:02] 🔧 [ANALYZE] 開始執行...
[14:23:18] ✅ [ANALYZE] 完成（1247 字元）
```

**4. 互動模式確認面板**

Interactive mode 下，每個 stage 完成後 Dashboard 自動出現確認面板，選擇繼續或停止。

**5. Toast 通知系統**

右下角堆疊式 Toast，支援成功/錯誤/資訊，3.5 秒後自動淡出。

### 新 API

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/dev/start` | 從 Dashboard 建立並觸發 DevEngine session |
| GET  | `/api/dev/log/<id>?offset=N` | 取得增量日誌行（JSON 字串陣列） |

### 升級方式

```bash
git pull
python run.py
```

---

## v1.5.1 — 2026-03-10

> 程式碼品質大清理：死碼移除、命名衝突修正、Skills Engine 整合、測試重寫

### 主要變更

**死碼清理**
- 刪除 `host/web_dashboard.py`（438 行 aiohttp 版儀表板，從未被任何模組 import）
- 刪除 `host/dashboard_charts.py`（37 行，只有設定 dict，從未被 import）
- 刪除整個 `host/stages/` 目錄（8 個 stage 模組，全部是 `return "TODO: ..."` 佔位符，已由 v1.5.0 DevEngine 完整取代）

**`register_channel` 命名衝突修正**

`channels/__init__.py` 的 `register_channel(name, cls)` 與 `router.py` 的 `register_channel(ch)` 語義完全不同（前者接受 class，後者接受 instance），原本共用同名造成混淆。本版本將前者改名為 `register_channel_class()`，消除歧義。

**Skills Engine IPC 整合**

Skills Engine（`skills_engine/`）現在已接入 IPC 訊息流，agent 可在對話中直接管理 Skill Plugins：

```json
// 安裝 skill（主群組限定）
{"type":"apply_skill","skill_path":"skills/add-slack.md","requestId":"r1"}

// 移除 skill（主群組限定）
{"type":"uninstall_skill","skill_name":"add-slack","requestId":"r2"}

// 列出已安裝 skills（任何群組）
{"type":"list_skills","requestId":"r3"}
```

結果寫入 `data/ipc/<group>/results/<requestId>.json`。

**測試重寫**

`tests/test_dev_engine.py` 完整重寫以對應 v1.5.0 引入的新 DevEngine API，不再測試已刪除的 `DevContext`、`run_pipeline()` 等舊介面。

### 升級方式

```bash
git pull
python run.py
```

不需重建 Docker image。

---

## v1.5.0 — 2026-03-10

> DevEngine：7 階段 LLM 驅動自動化開發引擎

### 新功能

**DevEngine 完整實作**

EvoClaw 現在可以真正地自主開發軟體功能。告訴 agent 你想要什麼，DevEngine 會自動完成需求分析、架構設計、程式碼撰寫、測試、審查、文件，並把檔案寫到磁碟。

**7 個 LLM 驅動階段：**

| 階段 | 說明 |
|------|------|
| 🔍 Analyze | 理解需求，定義功能範疇 |
| 📐 Design | 設計架構、API 簽名、資料流 |
| 💻 Implement | 撰寫完整 Python 程式碼 |
| 🧪 Test | 撰寫 pytest 測試案例 |
| 🔎 Review | LLM 安全審查 + 品質把關（PASS/FAIL） |
| 📝 Document | 產生 README 章節與 CHANGELOG |
| 🚀 Deploy | 解析 `--- FILE: path ---` 區塊，寫入磁碟 |

**觸發方式：**

透過 IPC（請 agent 寫入到 tasks/ 目錄）：
```json
{"type":"dev_task","prompt":"Add a metrics endpoint","mode":"auto"}
```

Resume 暫停的 session：
```json
{"type":"dev_task","session_id":"dev_1712345678_abc","prompt":""}
```

**Dashboard 整合：**
- 新 🛠️ DevEngine 分頁（第 7 個）
- Session 進度條（n/7 完成）
- 各階段 Artifact 預覽、Resume / Cancel 按鈕

### Bug Fixes

- 修正 dev_engine.py 的 3 個嚴重錯誤（import 路徑、DB API、stage 邏輯）
- Review 階段不再錯誤地使用 immune system

### 升級方式

```bash
git pull
python run.py
```

不需重建 Docker image（所有修改均在 host 端）。

---

## v1.4.3 — 2026-03-10

> Subagent 親子層級追蹤 + 即時 Container 活動捕捉

### 新功能

**Dashboard 現在可以看到 Subagent 在幹嘛**

過去 Dashboard 只能看到有幾個 container 在跑，無法分辨哪些是主 agent、哪些是 subagent，也看不到 container 目前正在執行什麼動作。

這次全面改善：

**① 親子關係追蹤**
`_active_containers` 新增 `parent_container` 欄位。主 agent 為 `None`，subagent 記錄父 container 名稱。`ipc_watcher` 在 `spawn_agent` 時自動找出父 container 並傳入。

**② 即時 stderr 串流**
`container_runner.py` 非 Windows 路徑改用串流讀取 stderr（`_stream_stderr()`），不再等 container 結束才讀取。`agent.py` 的每一行 `_log()` 輸出（Input received → Calling LLM → Tool: Bash → Done）即時更新至 `current_activity` 欄位。

**③ Dashboard 視覺化層級**
「Active Agent Containers」表格：
- 新增 *Activity* 欄：即時顯示最新 `_log()` 訊息
- Subagent 以 `↳` 縮排顯示在父 container 下方
- 色碼徽章：🟡 subagent / 🟣 scheduled / 🔵 message

### 升級方式

```bash
git pull
python run.py
```

不需重建 Docker image（所有修改均在 host 端）。

---

## v1.4.2 — 2026-03-10

> Docker Desktop 日誌即時顯示修正（完整版）

### 修正內容

**Docker Desktop container log 在執行過程中空白**

`agent.py` 整個執行過程（等待 LLM 回應）都沒有寫任何東西到 stdout/stderr，導致 Docker Desktop 日誌介面完全空白。

新增 `_log()` 函式，在以下節點寫入 stderr 進度訊息：
- `[evoclaw] Input received, parsing...`
- `[evoclaw] JSON parsed OK`
- `[evoclaw] Starting | group=xxx | backend=gemini`
- `[evoclaw] Calling LLM API...`
- `[evoclaw] Tool: Bash(command=ls -la)` (每次工具呼叫)
- `[evoclaw] Done | status=success`

### 升級方式

```bash
git pull
docker build -t evoclaw-agent container/
python run.py
```

*需要重建 Docker image*（修改在 container/agent-runner/agent.py）

---

## v1.4.1 — 2026-03-10

> Docker Desktop 日誌即時顯示修正

### 修正內容

**Docker Desktop container log 看不到輸出**

`docker run -i`（無 TTY）模式下，Python stdout 預設為完全緩衝，container 執行過程中 Docker Desktop 日誌介面顯示空白。

加入 `PYTHONUNBUFFERED=1` 環境變數後，Python 每行輸出立即 flush，Docker Desktop 可即時看到 container 執行狀態。

### 升級方式

```bash
git pull
python run.py
```

不需要重建 Docker image（此為 host 端設定，透過環境變數傳入 container）。

---

## v1.6.0 — Subagent Support

### What's New

Agents can now spawn subagents — isolated Docker containers that handle specific subtasks and return results to the parent agent.

### New Tool: `mcp__evoclaw__run_agent`

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `prompt` | string | ✅ | The task for the subagent to execute |
| `context_mode` | string | ❌ | `isolated` (default) or `group` |

**Returns:** The subagent's final text output (blocks up to 300s)

### Use Cases
- Parallel research: spawn multiple subagents for different topics
- Task delegation: hand off complex subtasks without polluting parent context
- Isolated code execution: run risky operations in a throwaway container

### How It Works
1. Parent agent calls `mcp__evoclaw__run_agent(prompt="...")`
2. IPC request written to `ipc/tasks/` → picked up by `ipc_watcher`
3. Host spawns new Docker container with the subagent prompt
4. Subagent runs its agentic loop and produces a final response
5. Result written to `ipc/<group>/results/{request_id}.json`
6. Parent agent receives result (polls file, timeout 300s)

---

# EvoClaw 發布流程規範

本文件規範 EvoClaw 專案的發布流程，確保每次發布都經過完整測試與審查。

---

## 發布週期

- **主要版本 (Major)**：每 3-6 個月，包含重大功能更新或不相容變更
- **次要版本 (Minor)**：每月或每兩月，包含新功能新增
- **修補版本 (Patch)**：視需要發布，主要用於錯誤修正

---

## 發布前檢查清單

### 1. 代碼品質檢查
- [ ] 所有 Python 檔案通過 `python -m py_compile` 檢查
- [ ] 執行所有測試用例並確保通過
  ```bash
  python -m tests.test_immune_enhanced
  ```
- [ ] 檢查是否有未提交的代碼變更
- [ ] 確認 `CHANGELOG.md` 已更新

### 2. 功能驗證
- [ ] Web Dashboard 正常運作（port 8765）
- [ ] Web Portal 正常運作（port 8766）
- [ ] 健康監控系統正常運行
- [ ] 免疫系統能正確檢測 injection 攻擊
- [ ] 排程任務正常執行
- [ ] 容器隔離機制正常

### 3. 文檔檢查
- [ ] `README.md` 已更新最新版本資訊
- [ ] `CHANGELOG.md` 已記錄所有變更
- [ ] 必要時更新 `docs/` 目錄下的技術文檔

### 4. 效能與安全
- [ ] 資料庫索引已建立
- [ ] 記憶體使用量在正常範圍
- [ ] 無明顯的資源洩漏
- [ ] 免疫 pattern 已更新最新版本

---

## 發布流程

### 步驟 1：準備發布分支
```bash
# 切換到 main 分支
git checkout main

# 拉取最新代碼
git pull origin main

# 建立發布分支（例如：release-1.3.0）
git checkout -b release-1.3.0
```

### 步驟 2：更新版本號
在以下位置更新版本號：
- `CHANGELOG.md` - 更新標題日期
- `README.md` - 如有版本提及則更新

### 步驟 3：最終測試
```bash
# 執行所有測試
python -m pytest tests/ -v

# 或執行特定測試
python -m tests.test_immune_enhanced
```

### 步驟 4：提交發布
```bash
# 提交所有變更
git add .
git commit -m "release: 準備發布 v1.3.0"

# 推送到遠端
git push origin release-1.3.0
```

### 步驟 5：建立 Pull Request
- 在 GitHub 建立 PR：`release-1.3.0` → `main`
- 標題格式：`Release v1.3.0`
- 描述中包含：
  - 主要變更項目
  - 測試結果
  - 已知問題（如有）

### 步驟 6：代碼審查
- 至少需要 1 位審查者批准
- 確認所有 CI 檢查通過
- 解決所有審查意見

### 步驟 7：合併與發布
```bash
# 合併到 main 分支
git checkout main
git pull origin main
git merge release-1.3.0
git push origin main

# 建立 Git Tag
git tag -a v1.3.0 -m "Release version 1.3.0"
git push origin v1.3.0
```

### 步驟 8：GitHub Release
1. 前往 GitHub 專案頁面
2. 點擊「Releases」→「Create a new release」
3. 選擇標籤 `v1.3.0`
4. 填寫發布說明（從 `CHANGELOG.md` 複製）
5. 標記為最新發布
6. 點擊「Publish Release」

### 步驟 9：清理
```bash
# 刪除發布分支
git branch -d release-1.3.0
git push origin --delete release-1.3.0
```

---

## 緊急發布流程

遇到嚴重錯誤需要緊急發布時：

### 步驟 1：建立熱修復分支
```bash
git checkout main
git checkout -b hotfix-1.2.1
```

### 步驟 2：修復問題
- 只修復緊急問題
- 避免加入新功能
- 最小化變更範圍

### 步驟 3：快速測試
- 執行相關測試
- 驗證問題已修復
- 確認無新問題引入

### 步驟 4：發布
```bash
git add .
git commit -m "fix: 緊急修復 [問題描述]"
git push origin hotfix-1.2.1

# 建立 PR 並標記為緊急
# 合併立即可執行
```

---

## 版本命名規範

遵循 [語意化版本 2.0.0](https://semver.org/)：

格式：`MAJOR.MINOR.PATCH`

- **MAJOR**：不相容的 API 變更
  - 移除或修改現有功能
  - 改變預設行為導致不相容
  
- **MINOR**：向後相容的功能新增
  - 新增功能
  - 改進現有功能（向後相容）
  
- **PATCH**：向後相容的問題修正
  - 錯誤修復
  - 效能優化
  - 文檔更新

### 範例
- `1.2.3` - 第 1 版第 2 次次要更新的第 3 次修補
- `2.0.0` - 第 2 版主版本（可能包含不相容變更）
- `1.3.0` - 第 1 版第 3 次次要更新

---

## 發布說明範本

```markdown
## [版本號] - YYYY-MM-DD

### 新增
- 功能描述

### 改進
- 改進描述

### 修復
- 修復描述

### 安全性
- 安全性相關更新

### 已知問題
- 已知問題描述
```

---

## 發布後任務

### 立即可做
- [ ] 確認 GitHub Release 已正確建立
- [ ] 檢查 CI/CD 流水線是否成功
- [ ] 通知用戶群體（如有需要）

### 24 小時內
- [ ] 監控錯誤回報
- [ ] 收集用戶反饋
- [ ] 確認無重大問題

### 一週內
- [ ] 整理發布反饋
- [ ] 規劃下一版本
- [ ] 更新開發路線圖

---

## 聯絡方式

如有發布相關問題，請：
1. 查閱本文件
2. 檢查 GitHub Issues
3. 聯繫維護者

---

## 附錄：常用命令

```bash
# 查看當前版本
git describe --tags --always

# 查看版本歷史
git log --oneline --decorate

# 比較版本差異
git diff v1.2.0..v1.3.0

# 建立新版本標籤
git tag -a v1.3.0 -m "Release version 1.3.0"

# 推送標籤
git push origin v1.3.0

# 刪除標籤（本地）
git tag -d v1.3.0

# 刪除標籤（遠端）
git push origin --delete v1.3.0
```

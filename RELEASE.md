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

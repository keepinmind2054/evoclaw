# EvoClaw 歷史規劃與分析文件歸檔計畫書 (Docs Archiving Plan)

本計畫書旨在清理 `docs/` 根目錄，將歷史規劃、一次性分析報告及過期計畫移入專屬的歸檔目錄 `docs/archive/` 中，以維持文檔目錄的清爽度，僅保留核心架構與開發說明的現行有效文檔。

---

## 1. 歸檔檔案對照表 (Archiving Mapping)

| 原文檔路徑 | 歸檔後路徑 | 類型說明 |
|---|---|---|
| `docs/ANALYSIS_REPORT_2026.md` | `docs/archive/ANALYSIS_REPORT_2026.md` | 2026 舊分析報告 |
| `docs/DOCS_AUDIT.md` | `docs/archive/DOCS_AUDIT.md` | 文檔審查記錄 |
| `docs/EXECUTION_PLAN.md` | `docs/archive/EXECUTION_PLAN.md` | 歷史執行計畫 |
| `docs/FLUIDITY_AND_DAYTONA_PLAN.md` | `docs/archive/FLUIDITY_AND_DAYTONA_PLAN.md` | 歷史流暢度計畫 |
| `docs/PRODUCT_STRATEGY.md` | `docs/archive/PRODUCT_STRATEGY.md` | 產品策略 |
| `docs/SPEED_UP_IMPLEMENTATION_PLAN.md` | `docs/archive/SPEED_UP_IMPLEMENTATION_PLAN.md` | 加速實作計畫 |
| `docs/SPEED_UP_TASK_BREAKDOWN.md` | `docs/archive/SPEED_UP_TASK_BREAKDOWN.md` | 加速任務細分 |
| `docs/STABILITY_ANALYSIS.md` | `docs/archive/STABILITY_ANALYSIS.md` | 歷史穩定性分析 |

## 2. 任務清單與狀態追蹤 (Task List)

| 步驟 | 任務內容 | 具體指令 / 操作 | 狀態 |
|---|---|---|---|
| 1 | 建立歸檔資料夾 | `mkdir docs/archive` | ⏳ 待處理 |
| 2 | 移動文件 | 使用 `git mv` 移動上述對照表中的 8 個歷史檔案。 | ⏳ 待處理 |
| 3 | 切換特徵分支提交變更 | 切換至 `feature/repo-improvements-20260625` 特徵分支 <br> 將歸檔變更併入 PR 提交並 push。 | ⏳ 待處理 |
| 4 | 切回原分支並同步 | 切回原分支 `docs/encoding-audit-agent-reading` 並 checkout 最新 docs。 | ⏳ 待處理 |

# EvoClaw 專案文檔與代碼同步計畫書 (Sync Docs Plan)

本計畫書旨在確保遠端倉庫中的文檔（如 `docs/SELF_UPDATE.md`）與新實作的程式碼（SQLite 控制信號與 AI-Fix 安全加固）完全同步，並統一推送到 PR 分支進行合併。

---

## 1. 任務清單與狀態追蹤 (Task List)

| 步驟 | 任務內容 | 具體指令 / 操作 | 狀態 |
|---|---|---|---|
| 1 | 更新 `docs/SELF_UPDATE.md` | 使用新實作的 SQLite 通訊與安全過濾邏輯更新文檔。 | ✅ 已完成 |
| 2 | 切回特徵分支 | `git checkout feature/repo-improvements-20260625` | ⏳ 待處理 |
| 3 | 將更新的文檔同步到特徵分支 | `git checkout docs/encoding-audit-agent-reading -- docs/` | ⏳ 待處理 |
| 4 | 暫存與提交變更 | `git add .` <br> `git commit -m "docs: sync SELF_UPDATE.md and add sync_docs_plan"` | ⏳ 待處理 |
| 5 | 推送至遠端倉庫 | `git push origin feature/repo-improvements-20260625` | ⏳ 待處理 |
| 6 | 切回原分支 | `git checkout docs/encoding-audit-agent-reading` | ⏳ 待處理 |
| 7 | 將文檔 checkout 回原分支保留 | `git checkout feature/repo-improvements-20260625 -- docs/` | ⏳ 待處理 |

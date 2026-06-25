# EvoClaw 專案代碼提交與 PR 合併計畫書 (Commit & Merge Plan)

本計畫書旨在將本地實作的架構重構與安全改善項目（包括 Web Portal Session 清理、SQLite 控制信號、容器動態記憶體調整、AI Auto-Patch 靜態安全審查防禦等）提交並同步至遠端倉庫，並透過 GitHub CLI (`gh`) 建立 Issue、PR 並進行合併。

---

## 1. 任務清單與狀態追蹤 (Task List)

| 步驟 | 任務內容 | 具體指令 / 操作 | 狀態 |
|---|---|---|---|
| 1 | 建立特徵分支 | `git checkout -b feature/repo-improvements-20260625` | ⏳ 待處理 |
| 2 | 暫存與提交代碼 | `git add .` <br> `git commit -m "refactor: implement session cleanup, sqlite signaling, dynamic memory cap, and security validator"` | ⏳ 待處理 |
| 3 | 推送至遠端倉庫 | `git push origin feature/repo-improvements-20260625` | ⏳ 待處理 |
| 4 | 開立 GitHub Issue | `gh issue create --title "..." --body "..."` | ⏳ 待處理 |
| 5 | 建立 Pull Request | `gh pr create --title "..." --body "..."` | ⏳ 待處理 |
| 6 | 合併 Pull Request | `gh pr merge --merge` | ⏳ 待處理 |
| 7 | 切回原分支並同步 | `git checkout docs/encoding-audit-agent-reading` <br> `git pull` | ⏳ 待處理 |

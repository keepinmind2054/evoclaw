## v1.5.0 — 2026-03-10

### Agent 能力大幅強化

這個版本讓 agent 從「問答機器」升級為「主動執行者」。

#### 新工具

| 工具 | 功能 |
|------|------|
| `Glob` | 用 glob 模式尋找檔案（支援 `**` 遞迴） |
| `Grep` | 用正規表達式搜尋檔案內容 |
| `WebFetch` | 抓取任意 URL，自動將 HTML 轉為純文字 |
| `pause_task` | 暫停指定排程任務 |
| `resume_task` | 恢復已暫停的排程任務 |

#### 完整工具清單（v1.5.0）

*檔案系統*：`Bash`(300s) · `Read` · `Write` · `Edit` · `Glob`★ · `Grep`★

*網路*：`WebFetch`★

*排程*：`send_message` · `schedule_task` · `list_tasks` · `cancel_task` · `pause_task`★ · `resume_task`★

★ = 本版新增

#### 行為改進

*主動執行*：agent 收到任務後直接執行，不再詢問「需要我開始嗎？」

*Bug 修正*：修正 Claude 後端 `_ANTHROPIC_AVAILABLE` NameError。

---

# EvoClaw Release Notes

## v1.4.0 — 2026-03-10

> Dashboard 全面重設計 + Bot 訊息紀錄 + Telegram 重試機制

---

### 🚀 主要更新

#### Dashboard 升級為 SPA

Web Dashboard（`http://localhost:8765`）從靜態自動刷新頁面完全重寫為**單頁應用程式（SPA）**：

```
├── 📊 狀態監控   Container 狀態、Active Agent、記憶體、Session、健康檢查、免疫威脅
├── 📋 日誌查看   SSE 即時日誌串流、等級過濾、暫停/繼續
├── 🤖 Agent 管理 停止 Container、排程任務 CRUD
├── ⚙️ 系統設定  .env 編輯器、CLAUDE.md 多檔編輯器
├── 💬 對話訊息  完整對話紀錄（用戶 + Bot），依群組篩選
└── 🧬 進化引擎  群組基因組、演化歷程日誌
```

#### Bot 回應現在寫入資料庫

之前 Bot 的回覆只發送到 Telegram，不儲存。現在 `on_output` 和 `_ipc_route_fn` 都會呼叫 `db.store_message(is_bot_message=True)`，讓 Dashboard 💬 分頁顯示完整對話。

#### Telegram 頻道重試

連線時遇到 `httpx.ReadError`（網路中斷）會自動重試最多 3 次。若偵測到 `Conflict`（另一個 Bot 實例在跑），會立即顯示清楚錯誤訊息。

---

### 📦 升級方式

```bash
git pull
# 若有修改 container/agent-runner/agent.py：
docker build -t evoclaw-agent container/agent-runner/
python run.py
```

Dashboard 會自動在 `http://localhost:8765` 啟動。

---

### ⚠️ 注意事項

- Bot 回應紀錄**僅在升級後的新訊息**開始記錄，舊的對話不會補填
- 如果 Telegram 顯示 `Conflict detected`，表示有另一個 `python run.py` 實例在跑，停掉重啟即可
- Dashboard 的 `.env` 編輯器修改後需要重啟 `python run.py` 才會生效

---

### 🔄 完整 Changelog

詳見 [CHANGELOG.md](CHANGELOG.md)

---

## v1.3.0 — 2026-03-10

> 進化引擎日誌 + 排程任務管理 + Bug 修正

### 主要更新
- `evolution_log` 資料表記錄每次演化事件（基因組前後快照）
- Agent 工具新增 `list_tasks` / `cancel_task`
- 排程任務 `status` 欄位（active / error / cancelled）
- `chat_jid` 防禦性回退（IPC watcher）
- Orphan task 啟動清理
- Web Dashboard 初始版本（port 8765）
- Web Portal 瀏覽器聊天介面（port 8766）

---

## v1.2.0

> WhatsApp 改為可選 Skill

WhatsApp 從核心移除，改為 Skill 系統提供。執行 `/add-whatsapp` 重新加入（現有 auth/群組設定保留）。

# EvoClaw 專案架構重構與安全改善計畫書

本計畫書針對《EvoClaw 專案優劣勢與架構代碼審查報告》中所列出的劣勢與安全風險，提出具體的代碼重構與實作改善方案。

---

## 1. 改善項目與重構方案

### 項目 A: Web Portal 記憶體 Session 洩漏改善 (背景主動清理) - [已完成]
* **當前問題**：無活動 Session 清理依賴於 API 請求時的「惰性回收」，缺乏背景主動清理機制。
* **重構方案**：
  1. 在 `host/webportal.py` 的 `start_webportal` 函數中，利用 `stop_event` 啟動一個定時 10 分鐘（600 秒）主動移除非活動 session 的背景 asyncio 協程任務 `_session_cleanup_loop`。
  2. 每隔 600 秒，在鎖 `_sessions_lock` 保護下調用 `_expire_sessions()` 清理非活動 Session。

### 項目 B: 檔案 Flag 控制訊號改善 (SQLite 狀態表通訊) - [進行中]
* **當前問題**：重啟、重新加載等控制信號使用 `.flag` 檔案讀寫，在 Windows 下經常因檔案鎖定 (File Locking) 導致 `PermissionError`。
* **重構方案**：
  1. 利用已有的 SQLite `router_state` 表（鍵值 store）來代替檔案系統 flag。
  2. 當 Web Portal 或是 Dashboard 發出重啟請求時，呼叫 `db.set_state("control:restart", "1")`。
  3. `host/main.py` 的主輪詢循環中，同時相容讀取 `.flag` 檔案與查詢 `db.get_state("control:restart")`。偵測到後重置狀態並重啟。
  4. 同樣地，將 `refresh_groups`、`reset_group`、`self_update` 等信號也相容資料庫狀態鍵。
  5. 修改 `host/ipc_watcher.py` 及 `host/channels/telegram_channel.py` 中的控制信號寫入端，改為主要寫入 DB，並在 try-except 容錯中嘗試寫入檔案系統 flag。

### 項目 C: 容器資源配額動態調整 (Dynamic Memory Capping) - [待實作]
* **當前問題**：統一限制為 512MB 記憶體，在處理複雜數據分析、多輪工具調用或超長 context 時容易引發 Docker 137 OOM 崩潰。
* **重構方案**：
  1. 在 `host/container_runner.py` 中，依據 Prompt 內容分析是否符合 Level B 任務（比照 `agent.py` 的關鍵字判定）或是否為排程任務 (`is_scheduled_task`)。
  2. 如果判定為 Level B 複雜任務或排程任務，動態將 `--memory` 與 `--memory-swap` 配額提升到 `1024m`。
  3. 提供 `CONTAINER_MEMORY_MAX`（預設 `2048m`）作為天花板，防止 AI 惡意引導耗盡主機記憶體。

### 項目 D: AI Auto-Patch (自動修復) 安全加固與防禦 - [待實作]
* **當前問題**：若關閉人工審查，AI 可能在自動產生的 Unified Diff 中寫入越權代碼或後門，造成代碼注入風險。
* **重構方案**：
  1. 在 `host/self_update_ai_fix.py` 中，引入靜態代碼安全審查過濾器 `_validate_patch_content(diff_text: str) -> bool`。
  2. 審查規則：
     - 拒絕任何試圖在 `host/` 下引入 `socket`/`urllib`/`requests`/`httpx` 連網、修改 `_tools.py` 敏感部分、或呼叫 `subprocess`/`os.system` 執行子進程的 patch。
     - 拒絕任何包含惡意 shell 特殊字元的變更，且阻攔 `shell=True` 的代碼。
     - 阻攔任何包含 `ssrf`、`bypass_ssrf`、`authorization` 等安全敏感關鍵字的修改。
  3. 如果檢測到不安全代碼，**即使 `AUTO_UPDATE_AI_FIX_REQUIRE_HUMAN_APPROVE` 被設為 false，也必須強制中斷自動更新，並發出 Critical 警報**，要求管理員手動介入。

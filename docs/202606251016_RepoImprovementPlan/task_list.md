# EvoClaw 專案架構與安全改善任務清單 (Task List)

本檔案用於追蹤各個改善項目的實作與測試狀態。

| ID | 改善項目 | 目標檔案 | 具體描述 | 狀態 |
|---|---|---|---|---|
| A | Web Session 背景清理 | `host/webportal.py` | 利用 stop_event 啟動定時 10 分鐘主動清理不活動 session 的協程任務。 | ✅ 已完成 |
| B1 | SQLite 控制信號 (主輪詢) | `host/main.py` | 同時相容檔案系統旗標與 SQLite 的 router_state。 | ✅ 已完成 |
| B2 | SQLite 控制信號 (IPC 寫入) | `host/ipc_watcher.py` | 將 `refresh_groups`、`reset_group` 以及 `restart_host` 的寫入改用 DB，備用檔案寫入。 | ✅ 已完成 |
| B3 | SQLite 控制信號 (Self-Update) | `host/ipc_watcher.py` | 將 `self_update.flag` 改為寫入 SQLite 鍵 `control:self_update`，備用檔案寫入並防錯。 | ✅ 已完成 |
| B4 | SQLite 控制信號 (TG 寫入) | `host/channels/telegram_channel.py` | 將 `/restart` 指令的重啟信號改為主要寫入 DB，備用檔案寫入並防錯。 | ✅ 已完成 |
| C | 容器資源配額動態調整 | `host/container_runner.py` | 根據 Prompt 與任務屬性動態識別 Level B/排程任務，提升記憶體限額至 1024m。 | ✅ 已完成 |
| D1 | AI Fix 靜態安全過濾器 | `host/self_update_ai_fix.py` | 實作 `_validate_patch_content(diff)` 過濾連網、子進程與安全敏感關鍵字。 | ✅ 已完成 |
| D2 | AI Fix 安全防禦流程 | `host/ipc_watcher.py` | 在偵測到 security_violation 時強制中斷更新流程並發出 Critical 警報。 | ✅ 已完成 |
| E | 驗證與重新測試 | 全案 | 執行 pytest 確保全案 47 個測試檔案 (617 個測試案例) 100% 通過，並驗證 PM2 重啟。 | ✅ 已完成 |

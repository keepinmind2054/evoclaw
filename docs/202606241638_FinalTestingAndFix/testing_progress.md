# EvoClaw 功能掃描、User Story 定義與測試追蹤表

本報告記錄了對 EvoClaw 所有核心功能進行掃描、定義 User Story 與預期行為，並建立測試追蹤表的過程。

## 1. User Story 與預期行為定義

### US-1: 訊息輪詢與游標安全 (Message Polling & Rollback)
- **描述**：作為系統管理者，我希望主機（Host）能安全、不漏失地輪詢與處理各聊天管道的新訊息。
- **預期行為**：
  1. 主機以異步循環輪詢 SQLite 資料庫，各 JID 有獨立游標防止訊息漏失。
  2. 訊息處理成功後，推進游標；若處理失敗（容器崩潰或超時），游標回滾（Rollback），確保下次重新處理。

### US-2: 免疫系統防禦 (Immune System & Anti-Spam)
- **描述**：作為系統管理者，我希望系統能自動阻擋 Prompt 注入攻擊與惡意垃圾訊息（Spam），以保障系統安全。
- **預期行為**：
  1. 靜態檢測輸入中是否含有中英文 Prompt 注入模式（Jailbreak），偵測到威脅時寫入記錄。
  2. 透過重複訊息的哈希追蹤（Spam），阻擋重複頻率過高的垃圾訊息。
  3. 連續偵測到 5 次威脅時，自動將發送者標記為黑名單（Block）進行全面阻擋。

### US-3: Docker 容器安全沙箱與併發管理 (Docker Sandboxing & Concurrency)
- **描述**：作為系統管理者，我希望 AI 代理的執行環境完全受限隔離，防止惡意指令外洩機密或耗盡系統資源。
- **預期行為**：
  1. 容器網路預設關閉（`--network none`），並限額資源（CPU、記憶體限額、PIDs 限制防 Fork 炸彈）。
  2. 拋棄所有 Linux 能力（`--cap-drop ALL`），且禁止權限提升。
  3. API 密鑰等 Secrets 透過 stdin JSON 傳入，在主機內存中隨即抹除。
  4. 當全域容器併發數達到 `MAX_CONCURRENT_CONTAINERS` 時，新任務會自動進入佇列等待。

### US-4: IPC Watcher 命令監聽與自我更新 (IPC Communication & Self Update)
- **描述**：作為系統開發者，我希望沙箱容器與主機之間能通過 IPC 管道安全地進行通信，且系統能安全執行自我更新。
- **預期行為**：
  1. 使用 `inotify`（Linux）或輪詢監聽 IPC 目錄的建立與移動事件。
  2. 採用「先寫入臨時文件再重命名」的原子寫入操作，防止 Race Condition。
  3. 嚴格校驗 JID 歸屬權，防範跨聊天室越權訊息傳送。
  4. 自我更新支援 worktree 沙盒測試門檻（Test Gate），測試失敗時自動 Rollback，且不寫入重啟 flag。

### US-5: 任務排程與進化引擎 (Scheduler & Evolutionary Daemon)
- **描述**：作為用戶，我希望能夠排程執行定時任務（Task），並且系統會根據我的對話反饋自動優化代理的回應風格。
- **預期行為**：
  1. 支援 cron/interval/once 任務，間隔任務基於預定時間計算，避免時間漂移。
  2. 任務連續失敗達到閾值時自動暫停，防止崩潰循環。
  3. 定期運行遺傳演化算法，根據對話適應度分數微調 JID 基因，且基因會過濾注入詞。

### US-6: 動態技能 2.0 系統 (Dynamic Skills Hot-plugging)
- **描述**：作為開發者，我希望能為代理熱插拔安裝新功能（Skills），而不需要重構 Docker 映像檔。
- **預期行為**：
  1. 解析 `manifest.yaml`，檢查依賴衝突與執行漂移檢測（三路合併）。
  2. 安裝過程發生異常時自動 Rollback。
  3. 將技能工具代碼動態掛載至容器目錄中，容器啟動時動態載入。

### US-7: 多層記憶與知識圖譜 (Multi-layer Memory & EvoKnowledgeGraph)
- **描述**：作為用戶，我希望 AI 代理能擁有跨對話的記憶力，並能在事實衝突時自動更新記憶。
- **預期行為**：
  1. 熱記憶（8KB limit）每次隨 System Instruction 注入 LLM。
  2. 向量記憶支援語義檢索，在缺庫時自動降級。
  3. `EvoKnowledgeGraph` 時間知識圖譜儲存事實三元組，當新事實與舊事實衝突時自動使舊事實失效（更新 `valid_to`），支援歷史時間點查詢。

### US-8: Web Dashboard 與 Portal 安全性 (Web Dashboards & Security)
- **描述**：作為管理者，我希望有可視化的儀表板監控狀態，並且 Web Portal 有足夠的安全防禦。
- **預期行為**：
  1. SSE 串流日誌在主機停止時優雅關閉（Stop Watcher）。
  2. Web Portal 實作 CSRF Token 驗證以防範跨站攻擊，對 Basic Auth 使用 HMAC 等時比較防範時序攻擊。

---

## 2. 狀態追蹤表

| US ID | 功能描述 | 預期行為 | 測試狀態 | 錯誤記錄 | 修復狀態 |
|---|---|---|---|---|---|
| **US-1** | 訊息輪詢與游標安全 | 獨立 JID 游標，錯誤時回滾 | ✅ 測試通過 (PASS) | 無 | 已修復 |
| **US-2** | 免疫系統防禦 | Prompt 注入靜態檢測，連續 5 次封鎖，Spam 檢測 | ✅ 測試通過 (PASS) | 無 | 已修復 |
| **US-3** | Docker 安全沙箱與併發 | CAP-DROP, stdin 密鑰，併發 Queue，OOM 診斷 | ✅ 測試通過 (PASS) | Windows 環境下的 Mock 與 Docker 呼叫路徑不相容 | 已修復 (Mock 覆蓋平台分支) |
| **US-4** | IPC Watcher 命令監聽與自我更新 | Inotify 監聽，原子寫入，JID 權限校驗，自我更新 Test Gate | ✅ 測試通過 (PASS) | 1. Windows 的 `Path.rename` 會因目標已存在而拋出 `FileExistsError`；2. IPC 錯誤移動測試的 glob 檔名比對不符合 timestamped 特性；3. 在全部測試執行時，其他測試 reload `host.config` 導致自我更新測試的 monkeypatch 失效（測試污染）。 | 已修復 (使用 `replace` 進行跨平台原子寫入，修正 glob 匹配模式，並在測試中同時對 `host.ipc_watcher.config` 進行 monkeypatch 覆寫) |
| **US-5** | 任務排程與進化引擎 | 防排程漂移，失敗暫停，進化基因微調與過濾 | ✅ 測試通過 (PASS) | 1. `evolve_genome` 測試因 P29a 重構 `db.upsert_group_genome_with_event` 導致 `upsert_genome` 被廢棄且測試未更新，出現 `KeyError`；2. 異步任務排程 Mock `db.async_get_due_tasks` 缺失導致 MagicMock await 異常。 | 已修復 (Mock `upsert_group_genome_with_event` 以及 `async_get_due_tasks` 的 AsyncMock 呼叫) |
| **US-6** | 動態技能 2.0 系統 | 三路合併，熱掛載工具，安裝出錯 Rollback | ✅ 測試通過 (PASS) | 無 | 已修復 |
| **US-7** | 多層記憶與知識圖譜 | 熱記憶限制，向量降級，時間知識圖譜衝突更新 | ✅ 測試通過 (PASS) | SQLite 擴充屬性與 JID 連接次數問題 | 已修復 |
| **US-8** | Web UI 安全性 | SSE 優雅關閉，CSRF Token，Basic Auth 等時比較 | ✅ 測試通過 (PASS) | 無 | 已修復 |

---

## 3. 單元測試概況

本輪測試共執行 44 個單元測試檔案，執行結果如下：
- **通過數量**: 605 / 605 個測試案例 (100% PASS)
- **主要修正明細**:
  - [tests/test_channel_token_revocation.py](file:///D:/AI_Agent_Dev/evoclaw/tests/test_channel_token_revocation.py): 修復 Discord 的 `abc.Messageable` 屬性 mock 與 Telegram `Forbidden` 捕獲中斷 logic。
  - [tests/test_ipc_backpressure.py](file:///D:/AI_Agent_Dev/evoclaw/tests/test_ipc_backpressure.py): 修正 `process_ipc_dir` 呼叫未定義變數 `route_fn` 的 typo。
  - [tests/test_json_logging.py](file:///D:/AI_Agent_Dev/evoclaw/tests/test_json_logging.py): 在 reload `host.main` 前先 reload `host.config`，確保環境變數生效。
  - [tests/test_evolution.py](file:///D:/AI_Agent_Dev/evoclaw/tests/test_evolution.py): 模擬 `db.upsert_group_genome_with_event` 攔截演化更新以修復 `KeyError`。
  - [tests/test_evolution_safety.py](file:///D:/AI_Agent_Dev/evoclaw/tests/test_evolution_safety.py): 修正 `_run_evolve_and_capture` 攔截 `upsert_group_genome_with_event`。
  - [tests/test_infrastructure.py](file:///D:/AI_Agent_Dev/evoclaw/tests/test_infrastructure.py): 
    - 修正 `fake_process` 和 `slow_process` 參數為 `*args, **kwargs` 避免 `TypeError`；
    - `test_ipc_valid_message_is_deleted` 加上 `in_memory_db` 以正確初始化 SQLite 連線；
    - 更新 `TestStopContainerAwaitsProcWait`之斷言，以符合真實的 `docker stop` 優雅關閉行為；
    - 將 `mock_queue.enqueue_task` 變更為 `AsyncMock` 以防 MagicMock 呼叫失敗；
    - 將 `db.async_get_due_tasks` mock 為 `AsyncMock`；
    - 在安全測試中加入對 Windows 端 `subprocess.run` 的 mock 以阻斷實際的 `docker run` 進程。
  - [tests/test_ipc_watcher_safety.py](file:///D:/AI_Agent_Dev/evoclaw/tests/test_ipc_watcher_safety.py): 將 `host/ipc_watcher.py` 中 Windows 上會報 `FileExistsError` 的 `Path.rename` 修改為跨平台的 `Path.replace` 原子寫入。
  - [tests/test_self_update_test_gate.py](file:///D:/AI_Agent_Dev/evoclaw/tests/test_self_update_test_gate.py): 在 `_patch_base_dir` 中同時對 `host.ipc_watcher.config` 進行 monkeypatch 覆寫，確保即使發生模組 reload，測試路徑亦能保持一致，避免測試污染。

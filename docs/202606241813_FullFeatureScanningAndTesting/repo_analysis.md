# EvoClaw 專案架構與進階技術分析報告

本報告針對 `D:\AI_Agent_Dev\evoclaw` 專案進行了全面的代碼審查與架構梳理。以下為詳細技術分析：

---

## 1. 目錄結構與主要模組職責

EvoClaw 是一個基於主機-容器（Host-Container）隔離架構、具備自我演化能力與進階記憶系統的 AI Agent 協同框架。其主要目錄職責劃分如下：

- **`host/`（主機管理端）**：
  框架的核心大腦。負責監聽外部訊息通道、SQLite 輪詢與狀態持久化、Docker 容器生命週期的調度、IPC 管道監控、任務排程，以及執行遺傳算法以優化 Agent 行為的進化守護進程（Evolutionary Daemon）。
- **`container/`（代理執行端）**：
  沙箱執行環境。主要包含 `agent-runner/`，為 Agent 提供強硬化的運行隔離（包括網路隔離、資源限額、Linux 權限丟棄等）。內置 `agent.py` 及與 LLM 交互的 Agentic Loop（支援 Gemini、Claude、OpenAI），並向 LLM 暴露安全的系統工具（`_tools.py`）。
- **`skills_engine/`（Skills 2.0 系統）**：
  技能插件管理系統。負責熱安裝、更新與解除安裝技能。技能描述以自然語言定義在 `SKILL.md`，其對應的工具可以直接熱插入（Hot-Swap）到容器的動態工具目錄中，實現無需重構鏡像的即時擴展。
- **`skills/` & `host/skills/`**：
  存放框架預裝或動態生成的技能包（如自然語言指引與對應的 `handler.py` 腳本）。
- **`groups/`**：
  存放各個群組（不同聊天室 JID）的持久化資料，包括獨立的配置、環境變數與長期記憶 `MEMORY.md`。
- **`dynamic_tools/`**：
  動態掛載至沙箱容器中的工具代碼目錄，提供技能的運行時環境。

---

## 2. Host 端核心組件深入分析

### 2.1 訊息循環與 SQLite 輪詢機制 (`host/main.py`)
- **輪詢核心**：在背景循環 `_message_loop` 中，主機以固定間隔（預設 2 秒）遍歷所有已註冊的群組，使用 `db.async_get_new_messages([jid], cursor)` 從 SQLite 中撈取尚未處理的新訊息。
- **Per-JID 游標機制**：為了防止群組 A 的成功處理將全域游標推進，導致群組 B 尚未處理的訊息被靜默丟棄，主機為每個 JID 維護獨立的 `_per_jid_cursors` 游標。
- **Rollback 安全保證**：游標的更新採用**「先執行、後確認」**策略。撈取出的訊息交給容器處理，只有當容器執行成功且回傳有效結果時，才會調用 `on_success` 推進該 JID 的游標。若容器崩潰或超時，游標不前進，下次輪詢時該訊息將被重新處理，確保訊息不漏失。
- **控制旗標監控**：輪詢中會監控 `refresh_groups.flag`（重新加載群組）、`self_update.flag`（熱重啟與代碼更新）、`reset_group.flag`（重置失敗冷卻）等文件，以實現非直接耦合的跨任務控制。
- **高可用領導者閘門 (Leader-gate)**：整合領導者選舉機制（`_leader.is_leader`），只有獲取鎖的 Host 實例才會執行訊息輪詢，未獲得領導權的實例則進入暫停等待狀態。

### 2.2 免疫系統與防 Prompt 注入機制 (`host/evolution/immune.py`)
- **雙重威脅檢測**：
  1. **Prompt Injection 偵測**：預編譯多組正則表達式（`INJECTION_PATTERNS`），針對中英文越獄攻擊（如 "ignore previous instructions"、"忘記你的系統提示"、"切換到開發者模式" 等）進行靜態檢測。
  2. **垃圾訊息（Spam）偵測**：當訊息傳入時，系統會計算其內容的 SHA-256 哈希值（保護隱私且防哈希碰撞攻擊），調用 `_track_message` 寫入免疫庫。接著分析該發送者在近 1 小時內重複發送此哈希的頻率。若次數超過 `SPAM_THRESHOLD`（預設 10 次），判定為惡意 Spam 攻擊。
- **自動封鎖（免疫記憶）**：當檢測到上述威脅時，主機會調用 `_record_threat` 記錄威脅。一旦同一個發送者累積的威脅次數達到 `THREAT_BLOCK_THRESHOLD`（預設 5 次），該發送者將在 SQLite 中被標記為 block 狀態，其後的對話均被直接阻擋。
- **Fail-Secure 容錯安全設計**：若遇資料庫暫時性鎖定（DB Locked），為了可用性會靜默放行；但若遇到未預期的資料庫嚴重損壞或系統異常，則基於「安全失效」原則直接拒絕訊息通過。

### 2.3 GroupQueue 併發調度與 Docker 容器生命週期管理
- **併發調度 (`host/group_queue.py`)**：
  - **優先級順序**：排程任務（Task）優先於訊息回覆（Message）。因為排程任務存在記憶體中且對時間敏感（防排程漂移），而訊息存於 DB 中可稍後重撈。
  - **槽位限制**：實現群組級隔離（單一群組同時只能有一個容器執行，防併發寫衝突）與全域最大執行數 `MAX_CONCURRENT_CONTAINERS` 限制。當全域併發滿載時，群組將被放進 FIFO 的 `_waiting_groups` 等待佇列中。
  - **電路斷路器與退避**：失敗時以指數退避（`BASE_RETRY_SECS * 2^(retry_count - 1)`）排程重試。當退避計數大於 0 時，會阻擋新容器啟動，防止在環境故障時形成緊密重試的無限循環。
- **Docker 容器生命週期管理 (`host/container_runner.py`)**：
  - **數據傳入與 Secrets 安全**：API 密鑰、對話歷史與熱記憶以 **JSON stdin**（而非環境變數）寫入容器，防範敏感資訊在 `docker inspect` 中洩漏。寫入後，隨即調用 `stdin.close()` 並在主機內存中將 secrets 欄位清零（Zero secrets from memory）。
  - **沙箱硬化配置**：
    - 網路隔離：預設為 `--network none`，完全阻斷聯網（可配置為 bridge 直連 LLM）。
    - 權限丟棄與提升限制：使用 `--cap-drop ALL` 與 `--security-opt no-new-privileges:true`。
    - 資源限額：利用 `--pids-limit` 防範 Fork 炸彈，並配置 `--memory`、`--cpus` 限制，及 `--tmpfs /tmp:size=...` 限制臨時文件大小以防 overlay 爆發。
    - 優先被殺：設置 `--oom-score-adj 500`，在主機記憶體不足時优先犧牲容器以存活 Host 進程。
  - **輸出截斷防 OOM**：限制讀取容器 stdout 大小為 `_MAX_OUTPUT_SIZE` (2MB)。若容器惡意寫入大量垃圾數據，Host 會主動截斷並清空管道，防止主機內存耗盡。
  - **真 OOM 診斷**：當容器遭遇結束代碼 137 時，Host 執行 `docker inspect` 查詢 `State.OOMKilled` 是否為 true。若是，會向用戶發送精準的「記憶體不足」中文警示，引導調高配置，而非模糊的 Crash 日誌。
  - **Windows 進程 Pipe 死結處理**：針對 Windows 上 `asyncio` 子進程與 Docker 交談易死結的系統缺陷，若偵測為 Win32 系統，自動 fallback 使用 `asyncio.to_thread` 在獨立線程中同步執行 `subprocess.run`。

### 2.4 IPC 監視器 (IPC Watcher) 與自我更新 (`host/ipc_watcher.py`)
- **高效監聽**：在 Linux 下優先啟用 `inotify-simple`，監聽 `/ipc/<folder>/messages` 與 `/ipc/<folder>/tasks` 目錄的 `CREATE`/`MOVED_TO` 事件，將處理延遲降至 <20ms；在非 Linux 或 inotify 失敗時自動切換回 `IPC_POLL_INTERVAL` 的 Polling 輪詢。
- **原子性操作**：所有寫入 IPC 目錄的動作皆採用 **"write temp + rename"** 機制，防止讀寫競態導致主機讀取到半寫入的損毀 JSON。
- **IPC 命令分流**：
  - `message`: 傳送訊息給用戶。特別校驗 JID 歸屬權，非主群組無法跨 JID 傳訊（防跨群組訊息注入）。
  - `schedule_task`/`update_task`: 更新與新增 DB 任務，使用 `_require_own_or_main` 校驗。
  - `spawn_agent`: 建立並執行子 Agent，維護 Dashboard 上父子容器的關係樹。

### 2.5 排程器 (Scheduler) 與進化守護進程 (Evolutionary Daemon)
- **排程器 (`host/task_scheduler.py`)**：
  - **防重複派發**：派發任務時，在 SQLite 中原子地將狀態設為 `running`，並配合超時機制（`_TASK_TIMEOUT_SECS`），防範容器掛起時再次被重複派發。
  - **防排程漂移與防歷史積壓**：對於 `interval` 任務，下一次的 `next_run` 是基於任務的「預定執行時間」而非「實際結束時間」計算，防止執行時長累積造成的時間偏移；若開機時發現下一個點已在過去，則迴圈遞增至未來時間，防止開機時連續派發任務。
  - **排程斷路器**：當排程任務連續失敗次數達到 `_MAX_TASK_FAILURES` 時，自動將任務 `paused` 並記錄錯誤日誌，等待手動修復。
- **進化守護進程 (`host/evolution/daemon.py`)**：
  - **演化主迴圈**：每小時/每天在獨立的 Task 中執行。分析過去 7 天內有執行記錄的 JID，當滿足最少樣本數 `MIN_SAMPLES` 時，調用 `compute_fitness` 計算適應度，並調整該 JID 的基因參數（如回答風格、長度限制等參數組）。
  - **自動修復基因組**：演化前會自動檢驗現有基因組的完整性，損壞則自動重置為默認值，保障穩定性。
  - **定期維護**：在演化結束後，自動執行 SQLite 的 log 清理（保留 30 天）並調用 `PRAGMA wal_checkpoint(TRUNCATE)` 以截斷 WAL 檔案，防止磁碟空間爆滿。
  - **三層記憶同步**：在 evolution daemon 中定期調用暖記憶同步：每 3 小時執行 `run_micro_sync` 提煉暖日誌，每 7 天執行 `run_weekly_compound` 進行深度蒸餾並同步至 `MEMORY.md` 中。

---

## 3. 進階核心機制深度分析 (New Subsystems)

### 3.1 多實例高可用與領導者選舉 (`host/leader_election.py`)
- **SQLite心跳同步**：在多實例部署中，通過資料庫 `leader_election` 表進行協同。Leader 實例每 10 秒（`HEARTBEAT_INTERVAL`）更新一次心跳時間戳。
- **心跳容錯與自我降級**：當 Standby 偵測到 Leader 的最後心跳時間超過 30 秒（`LEASE_TIMEOUT`）時，會嘗試原子地爭奪租約。Leader 實例在心跳寫入遭遇 DB Locked 等暫時性錯誤時會進行重試，若連續失敗達 3 次（`_MAX_CONSECUTIVE_HEARTBEAT_FAILURES`）則觸發自我降級，釋放租約並重新尋求鎖，避免系統雙重啟動 (Split-Brain) 故障。
- **線程池安全**：所有 SQLite commit 與 fetchone 操作皆包裝於 `asyncio.to_thread` 線程中執行，避免資料庫暫時性無回應阻塞 Asyncio 事件循環。

### 3.2 自我更新 AI 自動修復 (`host/self_update_ai_fix.py`)
- **沙盒測試門檻 (Test Gate)**：系統在 `git worktree` 沙盒目錄下執行 `pytest`。當測試失敗時，會自動擷取 stdout/stderr 及錯誤日誌，呼叫大語言模型 (LLM) 對原始碼進行診斷。
- **雜湊保護與安全限制**：AI 被允許針對錯誤產生 Patch (Unified Diff) 並套用至 worktree，且僅限修改 `host/`、`container/agent-runner/` 等非敏感路徑。`tests/` 下的所有測試代碼均有 Hash 驗證，任何變更都會引發 Patch 拒絕，防範 AI 惡意削弱測試套件。

### 3.3 系統健康監控 (`host/health_monitor.py`)
- **定期健康指標檢測**：背景線程每 60 秒對系統的多個健康指標（包括 Container 隊列堆積、5 分鐘內錯誤率、RSS 記憶體佔用、messages.db 的磁碟大小以及 JID 的活躍度）進行檢查。
- **異步警報消息佇列 (Alert Queue)**：警報資訊不與主程式強耦合，而是透過 `asyncio.Queue` 非同步緩衝發送，支援 10 分鐘（`_ALERT_COOLDOWN_S`）的警報冷卻防刷。liveness 檢測透過定時寫入時間戳，確保監控守護進程自身沒有掛起。

### 3.4 Model Context Protocol 伺服器 (`host/mcp_server.py`)
- **stdio JSON-RPC 端點**：基於 MCP 協議標準，使用 stdin/stdout 作為傳輸媒介，向 LLM 提供了 `evoclaw_get_logs`、`evoclaw_list_groups`、`evoclaw_db_query` 等診斷工具。
- **唯讀 SQLite 限制 (Authorizer)**：為了安全性，`evoclaw_db_query` 不僅以 `mode=ro` 唯讀模式開啟 SQLite，更註冊了 `sqlite3.set_authorizer` 唯讀授權器，除了 `SELECT` 與相關輔助函數外，阻斷一切修改或結構變更操作，保證診斷管道絕對安全。

### 3.5 全生命週期勾子系統 (`host/hooks_engine.py`)
- **9 大核心生命週期事件**：在主機-沙箱運作的關鍵交點（如 `PreToolUse` 工具執行前、`Stop` 會話結束前、`PreCompact` 上下文壓縮前等）提供了事件插槽。
- **自定義阻斷與修改**：支援呼叫外部 Shell 指令或 python 腳本。回傳 exit code 2 時觸發**硬性阻斷 (Hard Blocking)**，能強制攔截 LLM 惡意呼叫的工具、阻斷 Prompt 或是阻止 context 壓縮，並能透過 stdio JSON 串流向系統注入變更後的 `updatedInput` 與 `additionalContext`。

---

## 4. Container 端代理執行邏輯與沙箱環境配置

### 4.1 代理執行核心邏輯 (`container/agent-runner/agent.py`)
- **內存最佳化（Memory-OOM-Fix）**：為避免載入多個巨大 LLM SDK 造成的 OOM（每個庫佔 50-200MB 內存），代碼不再於頂層 import SDK，而是**在確定 LLM 後端後才惰性導入（lazy import）**，可節約 100-300MB 的沙箱 RAM。
- **系統提示詞（System Instruction）動態裝配**：
  - **誠實與防幻覺誠實守則**：將 `soul.md` 注入至系統提示中，強迫 AI 誠實記錄工具狀態，不得編造 progress 或假設成功。
  - **長期記憶注入與智慧分割**：讀取 `MEMORY.md` 作為長期記憶。為了防止大記憶爆上下文，系統上限設為 512KB，且自動保留完整的 `## 身份 (Identity)` 部分，但對 `## 任務記錄 (Task Log)` 進行智慧分割，僅截取最後 3000 字元。
  - **啟發式迭代限制（Level A/B Classification）**：根據 prompt 字數與關鍵字分類任務複雜度。簡單的 Level A 問答限制為最多迴圈 `MAX_ITER = 6` 次以防 AI 碎念與幻覺；複雜任務 Level B 放行至 `MAX_ITER = 20` 次。若為 Qwen模型，則注入特化提示以防止陷入 reasoning loop，並自動限製最大迭代次數。
  - **演化提示過濾（Bypass filter）**：當 Host 進化出的行為基因 `evolutionHints` 注入時，容器端會使用正則表達式檢索是否包含越獄詞（如無視 soul 誠實規則等），若有則強行過濾，以防 Host 端基因組受到惡意 prompt 污染而越權。
- **自動驗證**：若 secrets 包含 GitHub token，Agent 自動執行 `gh auth login` 與 `gh auth setup-git`，完成 git 與 gh CLI 的認證，讓 Agent 可以直接在沙箱內對 GitHub 執行寫入推送。
- **防止重複發送**：維護 `_messages_sent_via_tool`。若 Agent 已經利用 `send_message` 工具主動發送過結果，則在 emit 時清空 `result` 返回，避免 Host 再次發送造成重複。

### 4.2 工具集安全與 SSRF 防護 (`container/agent-runner/_tools.py`)
- **`tool_web_fetch` 的 SSRF 與 DNS Rebinding 二層防禦**：
  - **第一層：DNS 解析與黑名單過濾**：拒絕 localhost 與 google metadata 主機。對 URL 的 Hostname 解析出 A/AAAA 記錄後，若發現 any 記錄屬於私有、本地、保留或 169.254 的中繼服務，立即阻斷。在 HTTP 重定向時，對新 URL 執行相同的 check，如有 parse 異常或檢測失敗，基於 **Fail-Safe** 原則直接阻斷重定向。
  - **第二層：DNS 重新綁定防禦（DNS Rebinding Protection）**：為了解決 pre-flight 檢查與 TCP connect 間的 TOCTOU 時間差攻擊，在 open 期間 monkey-patch 了 socket 庫的 `create_connection` 方法。在 TCP 通訊開口建立的瞬間，再次對最終連接的 IP 地址進行 IP 範圍驗證，判定為私有 IP 時強行中斷 socket 連接。
  - **線程安全鎖**：由於 monkey-patch 是模組級全域修改，為防止並發 fetch時產生 Race Condition，系統採用 `_SSRF_PATCH_LOCK` 鎖對 socket patch 進行串行化。
- **`tool_send_file` 沙箱逃逸路徑防護**：
  - 對發送路徑執行 `_resolve_container_path` 校驗，限制傳送檔案只能位於 `/workspace` 或群組掛載目錄中，防止 AI 發送容器外的敏感系統檔案。

---

## 5. 動態技能與五層記憶系統

EvoClaw 具備動態技能熱插拔與層次分明的五層記憶架構：
- **Skills 2.0 系統**：解析技能 `manifest.yaml` 並實施漂移檢測與 three-way merge，自動建立備份。安裝成功的 Container 動態掛載 `/app/dynamic_tools`，由 `agent.py` 惰性載入，免除重新建置 Docker 映像檔的成本。
- **記憶體階層架構**：
  - Hot Memory：8KB 限制，動態注入 System Prompt。
  - PalaceStore：宮殿記憶，藉由 topic/namespace 進行二階層分類與提取。
  - Vector Memory：向量記憶，利用 `sqlite-vec` 與嵌入模型檢索語意。
  - Shared Memory：跨 Agent 協同記憶（private/project/shared 範疇控制）。
  - Cold Memory：歷史全文檢索 (FTS5) 配合時間衰減係數。
  - EvoKnowledgeGraph：事實三元組知識圖譜，實施「衝突失效」防範矛盾，並支援時間旅行 (Time Travel) 歷史追溯。

---

以上為 EvoClaw 的詳細架構與技術分析報告。本框架在進程隔離、記憶管理、動態技能熱插拔、跨平台死結防範及安全防禦等方面皆有非常紮實、嚴謹的工程實作。

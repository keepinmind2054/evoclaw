# EvoClaw 專案架構與技術分析報告

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

### 2.4 IPC 監視器 (IPC Watcher) (`host/ipc_watcher.py`)
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
  - **定期維護**：在演化結束後，自動執行 SQLite 的 log 清理（保留 30 定）並調用 `PRAGMA wal_checkpoint(TRUNCATE)` 以截斷 WAL 檔案，防止磁碟空間爆滿。
  - **三層記憶同步**：在 evolution daemon 中定期調用暖記憶同步：每 3 小時執行 `run_micro_sync` 提煉暖日誌，每 7 天執行 `run_weekly_compound` 進行深度蒸餾並同步至 `MEMORY.md` 中。

---

## 3. Container 端代理執行邏輯與沙箱環境配置

### 3.1 代理執行核心邏輯 (`container/agent-runner/agent.py`)
- **內存最佳化（Memory-OOM-Fix）**：為避免載入多個巨大 LLM SDK 造成的 OOM（每個庫佔 50-200MB 內存），代碼不再於頂層 import SDK，而是**在確定 LLM 後端後才惰性導入（lazy import）**，可節約 100-300MB 的沙箱 RAM。
- **系統提示詞（System Instruction）動態裝配**：
  - **誠實與防幻覺誠實守則**：將 `soul.md` 注入至系統提示中，強迫 AI 誠實記錄工具狀態，不得編造 progress 或假設成功。
  - **長期記憶注入與智慧分割**：讀取 `MEMORY.md` 作為長期記憶。為了防止大記憶爆上下文，系統上限設為 512KB，且自動保留完整的 `## 身份 (Identity)` 部分，但對 `## 任務記錄 (Task Log)` 進行智慧分割，僅截取最後 3000 字元。
  - **啟發式迭代限制（Level A/B Classification）**：根據 prompt 字數與關鍵字分類任務複雜度。簡單的 Level A 問答限制為最多迴圈 `MAX_ITER = 6` 次以防 AI 碎念與幻覺；複雜任務 Level B 放行至 `MAX_ITER = 20` 次。若為 Qwen 模型，則注入特化提示以防止陷入 reasoning loop，並自動限製最大迭代次數。
  - **演化提示過濾（Bypass filter）**：當 Host 進化出的行為基因 `evolutionHints` 注入時，容器端會使用正則表達式檢索是否包含越獄詞（如無視 soul 誠實規則等），若有則強行過濾，以防 Host 端基因組受到惡意 prompt 污染而越權。
- **自動驗證**：若 secrets 包含 GitHub token，Agent 自動執行 `gh auth login` 與 `gh auth setup-git`，完成 git 與 gh CLI 的認證，讓 Agent 可以直接在沙箱內對 GitHub 執行寫入推送。
- **防止重複發送**：維護 `_messages_sent_via_tool`。若 Agent 已經利用 `send_message` 工具主動發送過結果，則在 emit 時清空 `result` 返回，避免 Host 再次發送造成重複。

### 3.2 工具集安全與 SSRF 防護 (`container/agent-runner/_tools.py`)
- **`tool_web_fetch` 的 SSRF 與 DNS Rebinding 二層防禦**：
  - **第一層：DNS 解析與黑名單過濾**：拒絕 localhost 與 google metadata 主機。對 URL 的 Hostname 解析出 A/AAAA 記錄後，若發現 any 記錄屬於私有、本地、保留或 169.254 的中繼服務，立即阻斷。在 HTTP 重定向時，對新 URL 執行相同的 check，如有 parse 異常或檢測失敗，基於 **Fail-Safe** 原則直接阻斷重定向。
  - **第二層：DNS 重新綁定防禦（DNS Rebinding Protection）**：為了解決 pre-flight 檢查與 TCP connect 間的 TOCTOU 時間差攻擊，在 open 期間 monkey-patch 了 socket 庫的 `create_connection` 方法。在 TCP 通訊開口建立的瞬間，再次對最終連接的 IP 地址進行 IP 範圍驗證，判定為私有 IP 時強行中斷 socket 連接。
  - **線程安全鎖**：由於 monkey-patch 是模組級全域修改，為防止並發 fetch 時產生 Race Condition，系統採用 `_SSRF_PATCH_LOCK` 鎖對 socket patch 進行串行化。
- **`tool_send_file` 沙箱逃逸路徑防護**：
  - 對發送路徑執行 `_resolve_container_path` 校驗，限制傳送檔案只能位於 `/workspace` 或群組掛載目錄中，防止 AI 發送容器外的敏感系統檔案。

---

## 4. Skills 2.0 系統與技能載入機制 (`skills_engine/`)

Skills 2.0 允許將自然語言指引與對應的代碼工具封裝成技能包進行動態部署：
- **安裝生命週期 (`apply.py`)**：
  - 解析技能包的 `manifest.yaml`，驗證依賴（`dependencies`）與衝突（`conflicts`）。
  - 對 modifies 檔案執行**漂移檢測 (Drift Detection)**：對比當前文件與 `.evoclaw/base` 中儲存的初始雜湊值，若發生變更則啟用 three-way merge 機制合併更改。
  - 安裝前自動為所有修改路徑生成備份。若安裝中途崩潰或出錯，立即還原備份（Rollback）。
- **Container 工具熱插拔（Dynamic Tools Hot-loading）**：
  - 如果技能包定義了 `container_tools`，安裝時會將其寫入 `data/dynamic_tools/`。
  - Host 會將此目錄直接掛載到沙箱容器的 `/app/dynamic_tools/`。
  - 容器啟動時，`agent.py` 調用 `_load_dynamic_tools()` 動態載入此目錄下的 python 代碼並註冊至 Tool Registry 中。**這使得新技能的代碼工具不需要重新構建 Docker 鏡像即可被 LLM 即時識別與調用。**

---

## 5. 五層記憶系統與 EvoKnowledgeGraph 的實作與交互

EvoClaw 採用了層次分明的五層記憶架構：

1. **Hot Memory (熱記憶 - `hot.py`)**：
   - 載體為每群組目錄下的 `MEMORY.md`。限制在 8KB。
   - 每次容器啟動時，其身份區與最新任務摘要都會被智慧提取並注入 LLM 的系統提示中。
2. **PalaceStore (宮殿記憶 - `palace_store.py`)**：
   - 層級化記憶。利用關鍵字匹配將記憶自動分類到二級架構中：`namespace` (如 technical, planning) 與 `topic_tag` (如 decisions, problems)。這讓 Agent 能針對特定主題進行高效的檢索與總結。
3. **Vector Memory (向量記憶 - `memory_bus.py`)**：
   - 提供語意搜尋。採用 `sqlite-vec` 將知識進行向量化存儲與查詢（預設使用 Gemini Embedding API，無網路/金鑰時降級為 TF-IDF 相似度檢索）。
4. **Shared Memory (共享記憶 - `memory_bus.py`)**：
   - 跨 Agent 共享 SQLite 存儲。Agent 在寫入記憶時，可設定 scope 為 `private` (僅自己)、`project` (同項目成員) 或 `shared` (所有人)，實現跨 Agent 的知識同步。
5. **Cold Memory (冷記憶 - `search.py`)**：
   - 歷史對話沉澱與歸檔。支持 SQLite FTS5 全文檢索與**時間衰減算法 (Time Decay)**（越久遠的記憶分數越低）。
6. **EvoKnowledgeGraph (時間知識圖譜 - `knowledge_graph.py`)**：
   - 將 facts 儲存為 `(subject, predicate, object)` 三元組，並包含 `valid_from` 與 `valid_to` 的時間有效區間。
   - **事實衝突失效**：若謂詞屬於強更新謂詞（如 `is`、`has_role`），寫入新三元組時，若偵測到與已存在的舊事實衝突，會自動將舊事實的 `valid_to` 設定為當前時間，令其失效。
   - 支持時間旅行（Time Travel）查詢：可透過傳入 `as_of` 歷史時間戳記查詢過去某時間點的有效事實圖譜。

---

## 6. Web Dashboard 與 Web Portal 實作

### 6.1 Web Dashboard (port 8765) (`host/dashboard.py`)
- **無依賴 SPA**：完全基於 Python 標準庫 `http.server.ThreadingHTTPServer` 實作的單頁應用儀表板。
- **SSE 即時日誌**：採用 Server-Sent Events (SSE) 技術將 Host 日誌實時串流到前端。為了防止關聯連接在主機關閉時掛起，配備了背景 Stop Watcher 線程。一旦接收到主進程的 stop 信號，會自動切斷 SSE 鏈接並優雅關閉服務。
- **主要板塊**：包括主機/容器運行狀態、 active agents 列表、實時日誌流（支援層級過濾）、任務 CRUD 管理、配置環境變量與編輯 `CLAUDE.md` 等。

### 6.2 Web Portal (port 8766) (`host/webportal.py`)
- **無狀態輪詢 chat端**：stdlib 實現的 Web 聊天終端。使用 HTTP Polling 代替複雜的 WebSocket，簡化部署。
- **內存回收與 TTL 限額**：Session 與歷史消息存於內存中，為了防止大會話消耗主機內存，限制最大 sessions 數為 500、最大 message 數為 200，且在每次 API 請求時惰性回收超過 1 小時無活動的 session。
- **安全防護 (Hardened Security)**：
  - **防時序攻擊**：使用 `hmac.compare_digest()` 對 Basic Auth 密碼進行等時比較。
  - **防跨站請求偽造 (CSRF)**：在 session 建立時簽發一個獨一無二的 CSRF token，要求後續 POST 請求必須在 `X-CSRF-Token` 標頭中回傳。這防範了瀏覽器在使用 Basic Auth 登錄後自動附帶 credentials 導致的跨站偽造攻擊。
  - **輸入大小限制**：拒絕負數 Content-Length，且限制最大 post body 為 64KB，防範內存溢出攻擊。

---

以上為 EvoClaw 的詳細架構與技術分析報告。本框架在進程隔離、記憶管理、動態技能熱插拔、跨平台死結防範及安全防禦等方面皆有非常紮實、嚴謹的工程實作。

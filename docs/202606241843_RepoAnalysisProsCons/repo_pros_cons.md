# EvoClaw 專案優劣勢與架構代碼審查報告

本報告針對 EvoClaw 的代碼庫（`D:\AI_Agent_Dev\evoclaw`）進行全面的優劣勢分析、安全風險評估與架構妥協性審查。

---

## 1. 優勢分析 (Strengths / Pros)

### 1.1 主機-容器沙箱硬化隔離 (Hardened Sandboxing)
* **細粒度資源配額**：容器運行時限制了 CPU、Memory（預設 512MB）、PIDs 限制（預設 256，防範 Fork 炸彈），並拋棄所有 Linux 能力（`--cap-drop ALL`），設定 `no-new-privileges`，提供高度安全的代碼執行環境。
* **Secrets 安全管道**：API 密鑰等敏感資料採用 **stdin JSON** 寫入，且寫入後 Host 立即在內存中將 secrets 抹除（Zero memory），避免在環境變數或宿主機內存中洩漏。
* **精準 OOM 診斷**：能捕捉 Docker 容器 Exit Code 137 並反查 `State.OOMKilled`，若為 True 則向用戶顯示直觀的中文記憶體超限引導，大幅提升運維體驗（UX）。

### 1.2 訊息輪詢與游標設計 (Reliable Polling & Rollback)
* **Per-JID 獨立游標**：主機為每個群組（JID）維護獨立的 SQLite 訊息游標，防止群組間互相干擾。
* **Rollback 安全游標**：採用「先執行、後確認」更新游標。容器順利完成並回傳後，游標才向前推進；若容器掛起或崩潰，游標回滾，確保訊息處理「不漏失」。

### 1.3 深度記憶系統與進化基因 (Multi-Layer Memory & Evolutionary Loop)
* **五層記憶架構**：從隨提示詞注入的 Hot 記憶（8KB limit）、宮殿記憶、向量記憶、私有/共享記憶到全文檢索 Cold 記憶，設計層次分明。
* **事實衝突失效 (EvoKnowledgeGraph)**：時間知識圖譜儲存事實三元組，當新事實與舊事實衝突時，自動將舊事實的 `valid_to` 設定為當前時間令其失效，避免 LLM 在上下文發生事實矛盾。
* **進化提示過濾 (Anti-Jailbreak Genome)**：當 Host 自動演化出群組基因組（genome）注入容器時，容器端會以正則過濾 jailbreak 詞，防止 AI 的行為風格提示受到惡意注入攻擊。

### 1.4 全生命週期勾子與 MCP 整合 (Plugin & Hook Ecosystem)
* **全生命週期勾子**：`hooks_engine.py` 提供了 PreToolUse、Stop 等 9 個事件，支援 exit code 2 阻斷並修改輸入/輸出。
* **MCP stdio 安全唯讀防禦**：MCP 伺服器在對外暴露唯讀 SQLite 診斷工具時，不僅以唯讀模式連結，更在連接中設定 `sqlite3.set_authorizer`，除了 `SELECT` 以外阻斷一切 DDL/DML，從資料庫層級保證唯讀診斷的安全。

### 1.5 跨平台死結防範 (Cross-Platform Resiliency)
* **Windows asyncio 管道修正**：針對 Windows 上 `asyncio` 子進程與 Docker 交談易死結的系統缺陷，若偵測為 Win32 系統，自動 fallback 使用 `asyncio.to_thread` 在獨立線程中同步執行 `subprocess.run`，大幅增強在 Windows 環境下的穩定性。

---

## 2. 劣勢與潛在風險分析 (Weaknesses / Cons / Risks)

### 2.1 AI Auto-Patch (自動修復) 存在越權與代碼注入風險 (Supply Chain Risk)
* **代碼注入風險**：`self_update_ai_fix.py` 允許 LLM 產生 Unified Diff 並在 worktree 中自動套用修復。雖然對 `tests/` 測試代碼有 Hash 保護（防 AI 修改測試以取巧通過），但 AI **可以自由修改 `host/` 和 `container/` 程式碼**。
* **漏洞場景**：若 AI 在 Patch 中刻意寫入後門（例如修改 `host/ipc_watcher.py` 繞過權限檢查），且若部署配置關閉了 `REQUIRE_HUMAN_APPROVE`，這些後門將在自動 fast-forward merge 後被直接執行。這構成了嚴重的供應鏈安全隱患。

### 2.2 SQLite 高併發下的鎖定與 Standby 搶佔衝突 (Database Lock & High-Load)
* **DB Locked 瓶頸**：在高併發（多個群組同時跑，且 IPC 密集寫入任務/結果）下，SQLite 即使開啟 WAL 模式，依然容易發生 `database is locked`。
* **領導者選舉Churning**：`LeaderElection` 的 heartbeat 機制若因 SQLite 鎖定時間過長（超過 5 秒 DB 操作超時），可能會導致心跳寫入失敗，進而引發 standby 實例誤判定 Leader 死亡並強行搶佔租約，造成多個 Host 之間的 Split-Brain 或頻繁切換。

### 2.3 記憶體與 Session 資源的「惰性回收」洩漏風險 (Resource Leakage)
* **無背景定時清理**：Web Portal 的會話回收是在每次 HTTP API 請求時**「順便惰性回收」**超過 1 小時無活動的 session。
* **漏洞場景**：若有掃描器或惡意使用者對 Web Portal 進行大規模 Basic Auth 時序攻擊或掃描，但沒有後續請求，這些一次性 Session 將在 Host 內存中堆積。若沒有下一個合法請求來觸發惰性回收， Host 的記憶體將持續攀升，造成資源洩漏風險。

### 2.4 鬆耦合的「檔案 Flag」控制機制在 Windows 下的不穩定性 (File Lock Instability)
* **File Lock 衝突**：系統中重啟、重置、刷新群組的控制，主要依賴於向 `DATA_DIR` 寫入 `.flag` 檔案（如 `restart.flag`），並由主迴圈輪詢讀取後刪除。
* **問題點**：在 Windows 平台上，若讀寫發生的時間點重合，常會因為檔案鎖定（File Locking）導致 `PermissionError` 或無法刪除檔案，這會使 Host 的控制信號重複觸發或失效。

### 2.5 容器 512MB 記憶體限額的潛在 OOM 瓶頸 (Conservative Memory Cap)
* **OOM 隱憂**：為了修復 `tool_grep` 的記憶體洩漏，容器記憶體限額被從 2g 大幅下調至 512m。
* **問題點**： steady state 下 SDK 載入雖然夠用，但若 Agent 執行了需要較多內存的任務（例如載入大型 pandas 數據表、利用 `tool_bash` 調用編譯器、或是 Gemini/Claude 的 Context 對話歷史極長導致 runtime 內存大幅增加），512m 會成為硬傷，頻繁觸發 137 OOM 殺死容器。

---

## 3. 架構改進建議 (Recommendations)

1. **AI Auto-Patch 實施沙箱代碼審查與硬性人工確認**：
   * 強烈建議**永遠不要**在生產環境中將 `AUTO_UPDATE_AI_FIX_REQUIRE_HUMAN_APPROVE` 設為 `false`。
   * AI 產生的 Unified Diff 應該在 PR 中通過自動化靜態代碼分析（SAST，如 Bandit/Ruff）掃描，檢測是否包含可疑的 `subprocess`、網絡調用或權限繞過。
2. **改為背景定時 Task 清理 Session**：
   * 將 Web Portal 的惰性清理改為背景的定時 asyncio Task（例如每 10 分鐘執行一次），確保無活動的 Session 能夠被主動且即時地從記憶體中抹除。
3. **改用訊號或資料庫機制替代檔案 Flag 控制**：
   * 重啟、重置等控制信號，可考慮直接透過 SQLite 的單行狀態表更新，或是使用 `asyncio.Queue` 異步通訊，以避免 Windows 下檔案讀寫鎖定衝突。
4. **容器資源限額動態調整**：
   * 允許根據 JID 任務等級（Level A/B）動態調整 `docker run` 的 `--memory` 限額。例如 Level A 分配 512m，Level B (複雜任務) 自動放寬至 1g 或 2g，以平衡資源保護與執行成功率。

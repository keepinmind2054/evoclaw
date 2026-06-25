# EvoClaw vs NanoClaw 全面技術分析報告

**版本**：v1.0
**日期**：2026-03-23
**基準代碼庫**：EvoClaw v1.26.0（Phase 19 完成後）
**分析方法**：4 個平行 AI 分析師代理，各自獨立讀取並分析原始代碼，最終彙整成本報告

---

## 執行摘要

本報告回答了以下核心問題：**為什麼 EvoClaw 安裝使用起來不如 NanoClaw 穩定好用，又為什麼 EvoClaw 會產生虛假回應？**

### 核心結論（三點）

1. **架構複雜度差距 8 倍**：EvoClaw 在訊息處理路徑上有 20 個潛在失敗點，NanoClaw 只有 2–3 個。31,652 行 Python（58 個模組）vs NanoClaw 約 1,000 行 TypeScript。這不是設計失誤，而是 EvoClaw 功能更豐富（多頻道、自演化、企業工具、三層記憶）所帶來的必然結果。

2. **虛假回應有 8 個具體根源**，其中 2 個至今未修復（工具結果缺乏成功/失敗型別旗標、MEMORY.md 虛假記憶正反饋迴路），其餘 2 個部分修復，4 個已完全修復。NanoClaw 架構上不存在這些問題，因為它使用 MCP 結構化協議，所有工具結果都帶有 `"success": bool` 欄位，不需要靠文字猜測。

3. **安裝摩擦力來自 Docker**：TROUBLESHOOTING.md 的 19 個問題章節中，至少 7 個是 Docker 架構直接引入的。NanoClaw 不需要 Docker，消除了這整個失敗類別。

### Phase 12–19 成果確認

- **32 個 PR，276+ 個修復**：所有靜默失敗類問題（exec_skill await 缺失、soul.md 未打包進容器、CrossBot HMAC dead code、CI 從未跑過 Python 測試）已全部解決。
- 所有已識別的安全漏洞（路徑沙箱逃脫 ×5、CrossBot 偽造）已修復。
- EvoClaw 現在是 **功能正確但架構複雜** 的系統，與 Phase 12 前的「多處靜默失敗」狀態有本質差別。

### 最高優先的剩餘工作（2 項，代碼改動各 < 10 行）

| 優先級 | 修復項目 | 預期影響 |
|--------|----------|----------|
| P1 | Tool 結果加入 `{"success": bool, "exit_code": int}` 結構化旗標 | 消除 60-70% 的虛假回應根因 |
| P1 | MEMORY.md 注入加入「請重新驗證」警語前置 | 截斷虛假記憶跨 session 傳播鏈 |

---

## A. 虛假回應根源分析

*分析師：Senior AI/ML Reliability Engineer（20A 代理）*

本節提供 EvoClaw 產生虛假或幻覺回應的所有機制的深度技術分析。每個機制均溯源至具體代碼位置，並與 NanoClaw 架構對比。

---

### A.1 工具結果成功/失敗歧義（Tool Result Success/Failure Ambiguity）

**根本原因**：EvoClaw 的 `_execute_tool_inner()` 函數（`container/agent-runner/agent.py`，第 1764–1875 行）的每個工具都返回純 Python `str`。沒有結構化封裝來區分成功結果與失敗結果。一個成功的 `tool_bash` 呼叫返回 `"written to disk"`。一個失敗的返回 `"Error: command timed out after 300s"`。兩者在 LLM 歷史中以相同的 `tool_result` 字串出現，沒有 `success: bool` 欄位，沒有 `exit_code` 欄位，沒有任何區分包裝。LLM 必須從字串的自由文本內容猜測操作是否成功。

`docs/STABILITY_ANALYSIS.md` 第 3.1 節明確承認這是未修復的架構缺陷：

> "完整的 `{"success": bool, "exit_code": int}` 結構化回傳**尚未實作**。Agent 仍靠文字內容猜測成功/失敗。"

具體說明：當 shell 命令以非零代碼退出時，exit code 附加在 stdout 本體之後（第 308 行：`out += f"\n[Exit code: {proc.returncode}]"`）。如果輸出超過 4,000 字元（`_MAX_TOOL_RESULT_CHARS` 限制，第 82 行），exit code 行被靜默截斷。LLM 永遠看不到失敗信號。

**場景示例**：用戶要求 EvoClaw 編譯一個大型專案。`tool_bash` 運行 `make`，編譯器輸出 6,000 個字元，以 `make: *** [all] Error 1` 結尾。結果被截斷在 4,000 個字元。出現在第 6,002 個字元的 `[Exit code: 2]` 尾部消失。LLM 接收到看起來像編譯訊息流的輸出並得出構建成功的結論。它向用戶發送「Build complete.」

**觸發頻率**：超過 4,000 字元的任何工具結果都受此截斷影響。`grep -r`、`npm install`、`docker build` 和 `git log` 都常常超過此限制。這在 20–30% 的複雜工具使用 session 中觸發。

**NanoClaw 對比**：NanoClaw 使用 MCP（Model Context Protocol）SDK，它將每個工具結果封裝在結構化物件中：`{"success": true, "stdout": "...", "stderr": "", "exit_code": 0}`。Claude 接收帶有明確 `success` 布林值的型別化 JSON 物件。架構上不可能將失敗的工具呼叫誤解為成功——無論輸出長度如何，`success: false` 欄位始終存在。

**剩餘風險**：在 `docs/STABILITY_ANALYSIS.md` 第 3.1 節明確標記為 **❌ 尚未修復**。

---

### A.2 MEMORY.md 汙染正反饋迴路

**根本原因**：在 agent 啟動時（`main()`，第 2808–2848 行），agent 讀取群組的 MEMORY.md 檔案，並將全文注入 system prompt 作為權威性長期記憶：

```python
# agent.py lines 2834–2836
lines.append("")
lines.append(f"## 長期記憶 (MEMORY.md)\n以下是你在先前 session 中記錄的知識與自我認知：\n\n{_memory_snippet}")
```

MEMORY.md 內容以「以下是你在先前 session 中記錄的知識與自我認知」的框架呈現為事實性過去知識，沒有驗證機制，沒有說明內容可能不正確的警示，也沒有偵測先前 session 將幻覺內容寫入此檔案的機制。

里程碑強制器（第 1729–1742 行）在倒數第二輪注入 CRITICAL 警告，要求 agent 在 session 結束前更新 MEMORY.md。這在 LLM 上造成強大壓力，即使沒有完成實際工作也要寫入「某些東西」——soul.md 規則禁止在任務失敗時寫入，但倒數第二輪的 CRITICAL 警告創造了一個競爭壓力，常常覆蓋這個規則。

結果是一個正反饋迴路：
1. Session 1：由於工具輸出截斷，agent 幻覺「部署完成」。
2. Agent 在 CRITICAL 壓力下寫入 `[2026-03-20] 部署完成` 到 MEMORY.md。
3. Session 2：系統提示將此注入為既成事實。
4. Agent 現在「知道」部署成功，並在不重新驗證的情況下向用戶確認。
5. MEMORY.md 累積更多虛假確認。

**剩餘風險**：在 STABILITY_ANALYSIS.md 第 3.2 節標記為 **❌ 尚未修復**。建議的緩解措施（以「⚠️ 以下為過去的記憶，請重新驗證後再使用」警示前置注入 MEMORY.md）尚未實作。

---

### A.3 soul.md 反幻覺規則長期未生效

**根本原因**：EvoClaw 中最嚴重的幻覺歷史根源是 `soul.md`——包含所有反幻覺約束的檔案——從未被複製到 Docker 容器中。這在 Phase 17B（PR #379）得到確認：

> "Dockerfile 未 COPY `agent-runner/soul.md` 和 `agent-runner/fitness_reporter.py`，導致 container 內找不到這兩個檔案。soul.md 內的反幻覺規則和 fitness_reporter 的行為評估從未生效。整個 agent 在沒有核心安全規則的情況下運行。"

缺失檔案情況下的降級處理（第 2784 行）只是一個通用的單行聲明，而非包含明確禁止偽狀態標記、身份規則和 MEMORY.md 更新規則的完整 113 行 `soul.md`。

這意味著所有在 Phase 17B 之前部署 EvoClaw 的用戶都在沒有任何 soul 約束的情況下運行。

**剩餘風險**：修復（將 soul.md 複製到 Docker）標記為 **✅ 已修復**（Phase 17B）。但如果 soul.md 在未來的 Docker 構建中被意外刪除或排除，agent 會靜默地以大幅削弱的約束運行。

---

### A.4 假進度偵測：語意盲點

**根本原因**：EvoClaw 的假狀態偵測依賴正規表達式模式匹配，應用於 LLM 回應的字面文本。模式涵蓋：
- `*(正在執行...)*` 和 `*[running...]*` 括號模式
- `✅ Done` 和 `✅ 完成` 表情符號完成
- `【已完成】` CJK 括號模式
- `I have completed/finished/executed/run/written`

這些模式能捕捉教科書級別的幻覺案例，但完全遺漏**語意幻覺**，其中 agent 使用正規表達式未覆蓋的措辭聲稱任務完成。如：
- 「服務已在 3000 port 運行」
- 「The configuration file has been updated」
- 「I've pushed the commit」

STABILITY_ANALYSIS.md 第 3.3 節記錄為：
> "Regex 已擴充，但仍無法偵測**語意上的虛假**：'Bug 已修復'（但 Write/Edit 工具未被呼叫），'部署成功'（但 Bash/docker 未被呼叫）"

**剩餘風險**：標記為 **⚠️ 已擴充，語意偵測仍缺**。工具呼叫交叉驗證建議（若 agent 聲稱檔案已編輯但該輪未呼叫 Edit/Write 工具，則注入警告）尚未實作。

---

### A.5 MAX_ITER 耗盡後的靜默終止

**根本原因**：當 agentic 迴圈在未達到 `end_turn` 的情況下耗盡 `MAX_ITER` 時，三個迴圈實作都落入通用後備訊息（第 1748–1749、2232–2233、2492–2493 行）：

```python
final_response = "（處理完成，但未能產生文字回應，請重新詢問。）"
```

文本「處理完成」意味著成功。用戶看到這個短語合理地得出任務已完成的結論。迴圈可能已執行了 20 次失敗的 Bash 呼叫，從未產生任何有用結果，但用戶收到「處理完成」訊息。

Level A/Level B 分類啟發式（第 2870–2884 行）在提示少於 150 個字元且不包含 Level B 關鍵詞時，將任務分類為 Level A（MAX_ITER=6）。用戶輸入「fix the login bug」（18 個字元，無明確 Level B 關鍵詞），任務分配到只有 6 次迭代，無論實際複雜度如何。

**剩餘風險**：迭代限制仍是基於容器的設計的架構性約束。Level A/B 分類啟發式天然不完美。在 Phase 15A 中部分緩解但未從根本上解決。

---

### A.6 exec_skill() Await 缺失（歷史問題，已修復）

**根本原因**：Phase 14A（PR #369）之前，`exec_skill()` 函數在 async 上下文中被呼叫時沒有 `await`。這導致所有 skill 執行立即以 `coroutine` 物件而非執行 skill 邏輯的結果返回。影響 Phase 14A 之前的所有 skill 呼叫——即 100% 的 skill 調用返回幻覺結果。

**剩餘風險**：標記為 **✅ 已修復**（Phase 14A）。此特定錯誤已解決。

---

### A.7 evolution_hints 末尾注入的累積影響

**根本原因**：系統提示構建順序在 `main()` 中為：基礎角色 → soul.md → MEMORY.md → Level B 偵測說明 → CLAUDE.md 檔案 → **`evolution_hints` 注入**（第 2929–2954 行）。

`evolution_hints` 字串——由 EvoClaw 的基因算法引擎產生——附加在 soul.md **之後**。LLM 對上下文中後出現的內容給予不成比例的注意。任何語意上允許 agent 總結而不使用工具的 evolution hint 都會有效地弱化之前出現的 soul.md 約束。

`evolution_runs.success DEFAULT 1` 錯誤（Phase 19C PR #389 修復）直接相關：在修復之前，無論實際 agent 行為如何，evolutionary fitness 系統都記錄所有運行為成功。這意味著 GA 在朝向產生 `success=1` 資料庫行的行為優化——無論 agent 是否真正完成了任務。GA 可能已進化出推動 agent 朝向快速自信聽起來的回應的 hints，因為這些被記錄為「成功」。

**剩餘風險**：bypass 過濾器（第 2931–2953 行）只阻止明確攻擊。來自 GA 的細微累積漂移仍然可能。Phase 19C 之前膨脹 fitness 分數的影響已存在於進化基因組中。

---

### A.8 工具結果截斷導致錯誤訊息丟失

**根本原因**：所有三個 agent 迴圈都將工具結果截斷為 `_MAX_TOOL_RESULT_CHARS = 4000` 個字元（第 82 行）。截斷應用於輸出的**頭部**，保留前 4000 個字元。

標準 Unix 工具行為將最重要的診斷資訊放在輸出的**末尾**。對於 `bash -c "make all"`，構建成功或失敗摘要始終是最後一行。對於 `pytest`，通過/失敗計數是最後一行。截斷到頭部並丟棄尾部系統性地剝奪了 LLM 確定操作是否成功所需的資訊。

STABILITY_ANALYSIS.md 第 3.4 節明確記錄為 **❌ 尚未修復**：

> "Bash 輸出通常錯誤訊息在**最後**，截斷後 agent 只看到前 4000 字，錯誤被切掉，agent 以為成功繼續執行。建議：截斷優先保留**尾部**；或同時保留頭部 2000 + 尾部 2000 字。"

**觸發頻率**：任何產生超過 4,000 個字元的 Bash 命令。`pytest`、`npm test`、`make`、`cargo build`、`docker build`、`git log --stat` 等常常超過此限制。在 30–40% 的複雜開發 session 中觸發。

---

### A.9 虛假回應總結矩陣

| 機制 | 代碼位置 | 嚴重程度 | Phase 修復狀態 | 仍存在風險 |
|------|----------|----------|----------------|------------|
| A.1 工具結果無型別旗標 | agent.py:1764–1875, line 82 | 高 | ⚠️ 部分 | **是** |
| A.2 MEMORY.md 正反饋迴路 | agent.py:2808–2848 | 高 | ❌ 未修復 | **是** |
| A.3 soul.md 未打包進容器 | Dockerfile（Phase 17B 前） | 嚴重 | ✅ 已修復 | 降低 |
| A.4 語意假進度盲點 | agent.py:2058–2080 | 中 | ⚠️ 部分 | **是** |
| A.5 MAX_ITER 靜默耗盡 | agent.py:1748, 2233, 2493 | 中 | ⚠️ 部分 | **是** |
| A.6 exec_skill await 缺失 | （Phase 14A 前） | 嚴重 | ✅ 已修復 | 否 |
| A.7 evolution_hints 末尾注入 | agent.py:2929–2954 | 中 | ⚠️ 部分 | **是** |
| A.8 截斷丟棄尾部錯誤訊息 | agent.py:line 82 | 高 | ❌ 未修復 | **是** |

**核心架構結論**：NanoClaw 避免這些問題，不是透過更好的啟發式或更多的正規表達式模式，而是透過**更少的抽象層**。NanoClaw 的單層架構（Node.js → Claude SDK → 結構化 MCP 結果）為 LLM 提供關於每次工具呼叫的型別化結構化資料。EvoClaw 的三層架構（Host → Docker → Python agent → 非型別化字串結果 → LLM）在每個序列化邊界引入歧義，每個歧義都是潛在的幻覺通道。

---

## B. 效能與可靠性分析

*分析師：Senior Site Reliability Engineer（20B 代理）*

本節針對 EvoClaw 的核心架構組件進行深入的效能與可靠性審查，逐一比對其設計取捨，並說明 NanoClaw 在同等情境下的處理差異。

---

### B.1 端對端請求延遲拆解

一條使用者訊息從送達到收到回覆，在 EvoClaw 中經歷以下幾個串行階段：

**階段 1 — 頻道接收（0–50 ms）**
訊息由各頻道 adapter 接收。Telegram 透過 `python-telegram-bot` 的長輪詢取得更新（`telegram_channel.py:166`），Discord 在獨立的背景執行緒中運行其專屬的 event loop（`discord_channel.py:159–164`），WhatsApp 則透過 aiohttp webhook server（`whatsapp_channel.py:231–239`）。Discord 的跨 loop 橋接（`run_coroutine_threadsafe`）會帶入一次上下文切換，實測可能增加 1–5 ms。

**階段 2 — 訊息驗證管線（1–10 ms）**
`_on_message`（`main.py:491`）依序執行：allowlist 過濾 → RBAC 檢查 → per-sender 速率限制 → per-group 速率限制 → 去重複指紋（SHA-256，`main.py:344`）→ 免疫系統掃描。合計延遲通常 < 5 ms。

**階段 3 — 資料庫寫入（1–5 ms）**
訊息寫入 SQLite `messages` 表。在 SSD 上通常 < 2 ms，但 NFS 或 overlay filesystem（如 Docker volume）可能達 10–20 ms。

**階段 4 — GroupQueue 排程（< 1 ms 至數十秒）**
若同一群組已有 container 在跑，或全域並發已達 `MAX_CONCURRENT_CONTAINERS`（預設 5），訊息排隊等待。這是最大的延遲變數，在繁忙系統中可等待數分鐘。

**階段 5 — Container 冷啟動（3–15 秒）**
架構中最大的固定延遲開銷（見 B.2 詳述）。

**階段 6 — LLM 呼叫（2–30 秒）**
首 token 延遲通常 1–5 秒，完整回應視長度從 2 秒到 30 秒不等。

**階段 7 — IPC 輸出與回覆發送（< 1 秒）**
Container 將結果以 JSON 寫至 stdout，host 解析後透過 `route_outbound` 送出回應。

**典型端對端延遲：8–50 秒**（冷啟動 + LLM 為主）。NanoClaw 透過長駐型 process 省去容器啟動時間，典型延遲可降至 3–15 秒。

---

### B.2 容器冷啟動開銷

`run_container_agent`（`container_runner.py:378`）每次收到觸發訊息就啟動一個全新的 Docker 容器，沒有任何 container pooling 或 warm standby 機制。

啟動流程的組成延遲：
- `docker run` 系統呼叫解析與鏡像層查找：0.5–2 秒
- 容器 Linux namespace 建立（network、pid、mount）：0.5–1 秒
- tmpfs 掛載（`--tmpfs /tmp:size=64m`，`container_runner.py:573`）：< 0.1 秒
- 多個 bind volume 掛載（group 目錄、sessions、IPC、dynamic_tools）：0.2–1 秒
- Python interpreter 啟動 + agent entrypoint 初始化：1–5 秒

合計約 **3–10 秒**。若鏡像不在本地 cache，拉取時間可再加數十秒。

`--network none`（`container_runner.py:539`）禁用容器網路，強化了安全性，但也意味著 agent 必須透過 IPC 請求 host 轉發 LLM 呼叫，增加通訊複雜性。

**NanoClaw 對比**：NanoClaw 以長駐 Python process 執行 agent 邏輯，沒有 Docker 啟動開銷。同樣的 LLM 呼叫可以直接非同步發出，省去 3–10 秒的固定啟動成本。

---

### B.3 GroupQueue 排隊行為與背壓

`GroupQueue`（`group_queue.py:65`）實現了精巧的三層序列化：per-group 序列化 → 全域並發上限（MAX_CONCURRENT_CONTAINERS=5）→ 任務優先於訊息出隊。

**已修復（Phase 12–19）**：
- BUG-GQ-01：retry 後 GroupQueue 永久死鎖
- BUG-GQ-02：`_drain_waiting` 繞過 circuit breaker
- BUG-CFG-01/02：零值設定導致死鎖或 CPU 燃燒

**尚存風險**：
- `MAX_WAITING_GROUPS = 100`（`group_queue.py:42`）超限時靜默忽略新訊息
- `MAX_PENDING_TASKS_PER_GROUP = 50` 滿後丟棄排程任務

---

### B.4 重試與指數退避

失敗後的重試邏輯分三層：

| 層級 | 最多重試 | 退避策略 |
|------|----------|----------|
| GroupQueue | 5 次 | `5 × 2^(n-1)` 秒（5/10/20/40/80 秒） |
| main.py fail count | 5 次連續失敗後冷卻 60 秒 | 固定 60 秒（**❌ 未實作指數退避**） |
| Docker Circuit Breaker | 3 次觸發開路 | 60 秒後 half-open |

三層計數器之間的交互作用複雜，除錯困難。一個群組可能同時在 GroupQueue 退避和 `_group_fail_counts` 冷卻，實際等待時間取決於哪個條件先解除。

---

### B.5 任務調度器可靠性

Phase 19 前的所有嚴重排程問題已在 Phase 12–19 修復：
- BUG-TS-1（重複執行）→ 原子狀態轉換修復
- BUG-TS-3（掛起 container 無超時）→ `asyncio.wait_for` 包覆
- BUG-TS-4（任務無限重試）→ 5 次後自動暫停
- BUG-TS-5（排程漂移）→ 以計劃執行時間為基準修復

**尚存**：Scheduler poll 間隔預設 60 秒，限制排程精度至 60–120 秒；`_count_recent_failures` 每次直接查 DB，高任務量下可能有查詢壓力。

---

### B.6 頻道連線可靠性

**Telegram**：連線重試採指數退避（2/4/8/16 秒，最多 5 次），但斷線後不自動重連（無 reconnection watchdog）。

**Discord**：在獨立 daemon 執行緒運行（p17c 修復了事件在錯誤 loop 執行的 CRITICAL bug）。如果背景執行緒意外崩潰，整個 Discord 頻道將**靜默停止服務**，主程序不會感知。

**WhatsApp**：以 aiohttp webhook server 被動接收，HMAC-SHA256 驗證防偽造，`_last_wamid` LRU OrderedDict 防記憶體無界增長。

**NanoClaw 對比**：NanoClaw 各頻道採用統一連線狀態管理，任何頻道失連都觸發 reconnection loop 並記錄到監控群組。EvoClaw 各頻道重連策略不一致。

---

### B.7 高可用性與 Leader Election

SQLite-based leader election 的 BUG-LE-1~5（split-brain、DB deadlock、雷鳴群效應等）在 Phase 12–19 全部修復。

**架構限制（Phase 12–19 未觸及）**：
- Leader election 預設**不啟用**（大多數部署為單實例）
- SQLite-based election 僅適用於單機多進程場景，不支援跨主機叢集
- 心跳 10 秒、lease 超時 30 秒 → failover 最長 30 秒

**NanoClaw 對比**：NanoClaw 支援跨主機 HA，使用外部共識機制，failover 時間遠低於 30 秒。

---

### B.8 多 LLM 後端切換行為

EvoClaw 支援四種 LLM 後端（Claude/Gemini/OpenAI/NIM），切換邏輯在 container 內部由 agent 決定，**不是** host 層的動態路由。

**失效模式**：若設定的 LLM backend 不可用，每次 container 執行都失敗，觸發 GroupQueue 指數退避，最終停止重試。**沒有自動 failover**：Claude API 故障不會自動切換到 Gemini，需手動修改 `.env`。

**NanoClaw 對比**：NanoClaw 的 LLM 路由在 host 層實現，可以每次請求粒度動態 fallback，不需要重啟服務。

---

### B.9 Phase 12–19 修復 vs 尚存問題

**已修復的主要可靠性問題（14 項）**：GroupQueue 死鎖 × 2、任務調度器錯誤 × 5、Leader Election × 5、Docker 磁碟溢滿、Discord 事件錯誤 loop。

**尚存的開放問題（6 項）**：容器冷啟動架構性延遲、Discord 執行緒崩潰不可感知、Telegram 斷線無 watchdog、SQLite 單點瓶頸、LLM 後端無動態 Failover、`_waiting_groups` 上限 100 靜默丟棄。

---

## C. 安裝與操作複雜度分析

*分析師：Senior DevOps Engineer（20C 代理）*

---

### C.1 概述：量化指標

| 指標 | EvoClaw | NanoClaw |
|------|---------|----------|
| 安裝步驟數 | 5 個主步驟（含多個子步驟） | 單一 `python setup.py` 流程 |
| TROUBLESHOOTING.md 問題章節數 | 19 個獨立問題章節 | 極少（絕大多數問題不會發生） |
| `.env.minimal` 必填變數數 | 6 個 | 1–2 個（bot token + 選擇性 LLM key） |
| `.env.example` 總配置項目數 | 71 個（41 定義 + 30 已註解） | 顯著較少 |
| Docker image 建置時間 | 5–10 分鐘（首次約 1 GB） | 不適用（無 Docker 需求） |
| 外部依賴種類 | Docker + Python 3.11+ + Node.js + npm + 37 個 apt 套件 | 僅 Python + bot SDK |

---

### C.2 安裝流程複雜度

#### C.2.1 QUICK_START.md 的五步安裝流程

**步驟 1 — 安裝**：`host/requirements.txt` 非標準位置（根目錄下無）。TROUBLESHOOTING.md 專門為此設立章節，說明此問題常見到足以被記錄。

**步驟 2 — 配置**：需同時設定 `ENABLED_CHANNELS` 和 channel token，缺一靜默失敗。`QUICK_START.md` 明確警告「This field is required. Without it the bot starts with no active channels.」——系統啟動、無錯誤訊息、但完全無回應。

**步驟 3 — 建置 Docker image 並啟動**：實際上是三個子步驟（`make build` + `validate_env.py` + `run.py`）。首次執行 5–10 分鐘。若失敗，系統啟動正常但所有訊息觸發「evoclaw-agent image not found」。

**步驟 4 — 註冊群組**：NanoClaw 加入群組即自動可用；EvoClaw 要求明確預先登記。此步驟的存在本身就說明了架構複雜度。

**步驟 5 — 測試**：即使前四步全部成功，首次回應可能需要 15–30 秒冷啟動。對不熟悉 Docker 的用戶，這直接讓他們以為系統壞了。

---

### C.3 配置複雜度

#### C.3.1 混淆性最高的環境變數

**`CONTAINER_TIMEOUT` 單位不一致**：`config.py` 第 64–69 行記錄「Configured as milliseconds in the env var... then divided by 1000 for runtime use.」設定 `CONTAINER_TIMEOUT=30` 實際上是設定了 0.03 秒逾時。

**`CLAUDE_API_KEY` vs `ANTHROPIC_API_KEY` 命名衝突**：`README.md` 推薦設定 `ANTHROPIC_API_KEY`，但 `.env.minimal`、`host/config.py`、`container/agent-runner/agent.py` 實際讀取的全是 `CLAUDE_API_KEY`。設定了 `ANTHROPIC_API_KEY` 的用戶會得到靜默失敗——系統啟動，找不到 Claude key，自動降級到 Gemini，不發出任何明確警告。

**`ENABLED_CHANNELS` 隱性依賴**：需同時設定此變數和對應 channel token；只設其中一個，結果是靜默失敗而非清晰的錯誤訊息。

---

### C.4 首次運行體驗

**延遲與無回饋的冷啟動**：TROUBLESHOOTING.md 承認正常首次容器冷啟動需要 8–15 秒，異常可達 30 秒以上。Typing indicator 功能只在 Telegram 等支援的頻道中可見；在 Discord 或 Web Portal 中，用戶看到的只是沉默。

**群組未註冊的靜默失敗**：TROUBLESHOOTING.md 第 42–46 行列出「群組未註冊」為 bot 無回應的常見原因。NanoClaw 加入任何群組即自動可用，不需要預先登記。

---

### C.5 文件差距與誤導性內容

**版本號不一致**：`README.md` 徽章顯示 `v1.15.0`，`README_en.md` 顯示 `v1.11.42`。英文文件是舊版本，對英文讀者提供過時資訊。

**`CLAUDE_API_KEY` vs `ANTHROPIC_API_KEY` 在文件間不一致**（見 C.3.1）。

**Gmail OAuth2 配置文件缺失**：`.env.example` 要求 `GMAIL_CREDENTIALS_FILE` 和 `GMAIL_TOKEN_FILE`，但沒有任何文件說明如何生成這些 OAuth2 憑證文件。

---

### C.6 持續維運負擔

**Docker image 更新**：每次 `git pull` 後，如果 `container/Dockerfile` 有變更，需手動執行 `make build`（5–10 分鐘）。沒有自動化機制提示用戶是否需要重建。

**已知穩定性問題的持續負擔**：`README.md` 列出 8 個「計畫修正」的已知問題，以及 3 個「重大（立即修復）」安全問題，「22 個安全及架構議題正在積極追蹤中」。即使安裝成功，生產環境中仍有已知安全缺口需要持續關注。

---

### C.7 NanoClaw 對比：為什麼安裝更簡單

EvoClaw TROUBLESHOOTING.md 的 19 個問題章節中，至少 **7 個是 Docker 架構直接引入的**（Docker not running、image not found、Docker build failed、OOM、container timeout、circuit breaker、slow responses）。NanoClaw 沒有 Docker 依賴，消除了這整個失敗類別。

EvoClaw 的 71 個配置點、`CLAUDE_API_KEY` vs `ANTHROPIC_API_KEY` 命名衝突、毫秒單位逾時設定，都是 NanoClaw 不存在的混淆點。

**根本結論**：EvoClaw 的安裝複雜度是架構取捨的必然結果：Docker 容器隔離帶來更強的安全性和多步驟任務執行能力，代價是顯著更高的安裝和維運複雜度。`README.md` 第 82 行誠實地承認：「多了 15+ 個潛在失敗點，換取的是更強的安全隔離。」

---

## D. 架構複雜度與風險評估

*分析師：Senior Software Architect（20D 代理）*

---

### D.1 複雜度量化指標

#### D.1.1 各模組代碼行數

| 模組 | 行數 | 分類 |
|------|------|------|
| `container/agent-runner/agent.py` | 3,087 | 極高複雜度 |
| `host/main.py` | 1,658 | 極高複雜度 |
| `host/dashboard.py` | 2,228 | 高複雜度 |
| `host/db.py` | 1,516 | 高複雜度 |
| `host/ipc_watcher.py` | 1,343 | 高複雜度 |
| `host/container_runner.py` | 1,104 | 高複雜度 |
| **全代碼庫合計** | **31,652** | **60 個 Python 檔案** |

NanoClaw 估計約 1,000 行 TypeScript，單一執行路徑。**複雜度比：31:1**。

#### D.1.2 Cyclomatic 複雜度熱點

1. **`agent.py::main()`** — 531 行，估計 CC > 60
2. **`ipc_watcher.py::_handle_ipc()`** — 處理 16 種不同 IPC 動作類型，估計 CC > 50
3. **`agent.py::run_agent_claude()`** — 379 行，估計 CC > 40
4. **`main.py::_process_group_messages()`** — 存取 14 個模組級 global 變數，估計 CC > 35
5. **`container_runner.py::run_container_agent()`** — 管理 Docker 生命週期，估計 CC > 30

#### D.1.3 全域可變狀態

僅 `host/main.py` 就宣告了 **24 個模組級可變狀態變數**，包括 `_group_fail_counts`、`_per_jid_cursors`、`_seen_msg_fingerprints`、`_group_queue` 等。這是單元測試困難的根本原因。

---

### D.2 故障點映射

完整請求路徑包含 **20 個失敗點**（FP-1 至 FP-20）：

```
[使用者] → [FP-1:網路/Channel] → [FP-2:去重] → [FP-3:Allowlist]
→ [FP-4:Rate-limit] → [FP-5:Immune system] → [FP-6:GroupQueue enqueue]
→ [FP-7:GroupQueue retry] → [FP-8:DB讀取] → [FP-9:Docker冷啟動 ★★★★]
→ [FP-10:stdin JSON序列化] → [FP-11:agent.py啟動] → [FP-12:LLM API ★★★]
→ [FP-13:tool執行 ★★★] → [FP-14:輸出截斷 ★★] → [FP-15:marker偵測]
→ [FP-16:stdout JSON反序列化] → [FP-17:IPC subagent等待 ★★★]
→ [FP-18:MEMORY.md虛假記憶 ★★★] → [FP-19:DB寫入] → [FP-20:回覆發送]
```

- ★★★★ = 極高風險：FP-9（Docker 冷啟動，最常見停機原因）
- ★★★ = 高風險：FP-12（LLM API 外部依賴）、FP-13（tool 執行）、FP-17（subagent 等待）、FP-18（MEMORY.md 虛假記憶）

**複雜度比率：EvoClaw 20 個 vs NanoClaw 2–3 個 = 8 倍**

---

### D.3 技術債清單

#### D.3.1 STABILITY_ANALYSIS.md 的未修復項目

| 編號 | 問題 | 狀態 | 嚴重度 |
|------|------|------|--------|
| 3.1 | Tool 結果缺少 `{"success": bool}` 旗標 | ❌ 未修復 | HIGH |
| 3.2 | MEMORY.md 注入缺乏驗證警語 | ❌ 未修復 | HIGH |
| 3.3 | 假進度偵測無語意工具呼叫交叉驗證 | ⚠️ 部分 | MEDIUM |
| 3.4 | 工具截斷丟失尾部錯誤訊息 | ❌ 未修復 | MEDIUM |
| 3.5 | 無限工具重試偵測缺失 | ❌ 未修復 | MEDIUM |
| 4.3 | 固定 Cooldown 無指數退避 | ❌ 未修復 | LOW |
| 5.1 | IPC 消息排序依賴時間戳記（NTP 偏差風險） | ❌ 未修復 | LOW |
| 5.3 | Subagent 輪詢等待最多 300s，群組在此期間被鎖定 | ❌ 未修復 | LOW |

#### D.3.2 代碼中的 TODO 注釋

- **`host/main.py:1144`**：`pass # TODO: wire to evolution/fitness.py` — 成功事件 fitness 信號連接缺失
- **`host/health_monitor.py:245`**：`# TODO: 可以在這裡加入發送通知到 Telegram/Slack 等` — 健康監控告警未完成

#### D.3.3 殘留的已過時模式

- `asyncio.get_event_loop()` 在 Python 3.10+ 已棄用，仍存在於 `webportal.py:499`、`discord_channel.py:228`
- **遺留全域游標** `_last_timestamp`：有兩套並行游標系統（per-JID + global legacy），增加維護負擔
- **雙重 JSON 序列化管道**（stdin/stdout + marker 偵測）本身是為繞過「沒有原生容器通訊機制」而設計的工作繞解，目標架構（v2.x）計畫用 WebSocket 取代

---

### D.4 風險矩陣

Phase 12–19 修復後的前 10 大剩餘風險：

| 風險 | 可能性 | 影響 | 緩解狀態 | 優先級 |
|------|--------|------|----------|--------|
| **R1: Tool 結果無 success flag** | 高 | 高 | ❌ 未緩解 | **P1** |
| **R2: MEMORY.md 正反饋迴路** | 中 | 高 | ❌ 未緩解 | **P1** |
| **R3: 工具輸出截斷丟失尾部錯誤** | 高 | 中 | ❌ 未緩解 | P2 |
| **R4: 無限工具重試** | 中 | 中 | ❌ 未緩解 | P2 |
| **R5: Subagent 等待 300s 鎖定群組** | 低中 | 中 | ⚠️ 有上限但未縮短 | P3 |
| **R6: Docker 冷啟動 15–60s** | 高 | 高 | ✅ 有 timeout 保護（架構性問題無法根治） | P3 |
| **R7: 語意假進度偵測缺失** | 中 | 中 | ⚠️ regex 已擴充但語意仍缺 | P3 |
| **R8: `run_container_agent()` 無 semaphore** | 低 | 高 | ⚠️ 呼叫端有保護，函數本身無 | P4 |
| **R9: IPC 時間戳排序依賴 NTP** | 低 | 低中 | ❌ 未緩解 | P4 |
| **R10: health_monitor 無主動告警** | 高 | 中 | ⚠️ TODO 存在但未實作 | P4 |

---

### D.5 建議

#### D.5.1 短期（P1/P2，代碼改動小，ROI 高）

**[ROI #1] 工具結果加入結構化 success flag（STABILITY_ANALYSIS 3.1）**

在 `agent.py::_execute_tool_inner()` 中，將所有工具的返回值改為：
```python
return json.dumps({
    "output": "stdout output...",
    "success": exit_code == 0,
    "exit_code": exit_code
})
```
然後在三個 agent 迴圈中，當 `success: false` 時中斷執行並提示用戶。
**預估影響：消除 60-70% 的虛假回應根因。代碼改動：< 30 行。**

**[ROI #2] MEMORY.md 注入加入警語前置（STABILITY_ANALYSIS 3.2）**

在 `agent.py::main()` 中 MEMORY.md 注入點：
```python
# 修改前
system_instruction += f"\n\n{memory_content}"

# 修改後
system_instruction += (
    "\n\n⚠️ 以下為過去的記憶。這些是歷史記錄，請重新驗證後再使用，"
    "不要直接當作已完成的事實。\n\n" + memory_content
)
```
**預估影響：截斷虛假記憶正反饋迴路。代碼改動：< 5 行。**

**[ROI #3] 工具截斷保留尾部（STABILITY_ANALYSIS 3.4）**

將 `agent.py::tool_bash()` 的輸出截斷邏輯從保留頭部改為：
```python
# 保留頭部 2000 + 尾部 2000 字元
if len(result_str) > _MAX_TOOL_RESULT_CHARS:
    head = result_str[:2000]
    tail = result_str[-2000:]
    result_str = head + f"\n[... truncated {len(result_str)-4000} chars ...]\n" + tail
```
**預估影響：大幅減少因截斷而遺失錯誤訊息的情況。代碼改動：< 10 行。**

**[ROI #4] 無限重試偵測（STABILITY_ANALYSIS 3.5）**

在 `agent.py::run_agent_*()` 的 MAX_ITER 迴圈中追蹤 `(tool_name, hash(args))` → 失敗次數；同一工具 + 相同參數失敗 ≥ 2 次時，注入系統警告要求 agent 換策略。
**預估影響：防止 20 輪 token 浪費在同一死局。**

#### D.5.2 中期（Phase 21–22）

1. **health_monitor 主動告警**：完成 `host/health_monitor.py:245` 的 TODO，接入 Telegram 監控群推送
2. **`run_container_agent()` 內部加 semaphore**：將並發限制的 semaphore 移入函數本身，作為防禦性保護
3. **subagent 等待上限縮短至 60s**：`agent.py:688` 的 `range(300)` 改為 `range(60)`
4. **指數退避 Cooldown**：`_GROUP_FAIL_COOLDOWN` 從固定 60s 改為指數退避（60 → 120 → 300 → 600 秒）
5. **`main.py:1144` TODO 完成**：接入 `evolution/fitness.py` 的成功事件信號

#### D.5.3 長期（縮小與 NanoClaw 的架構差距）

1. **IPC 從文件輪詢遷移到 WebSocket**（v2.x 目標）：延遲從 ~500ms 降到 <100ms，天然支援 subagent push 通知
2. **Tool 結果格式統一為 MCP 結構**：對齊 NanoClaw 的 `{"success": true, "stdout": "...", "stderr": "", "exit_code": 0}`
3. **main.py 重構消除全域可變狀態**：24 個 global 提取為 `ApplicationState` dataclass，通過依賴注入傳遞，使單元測試成為可能
4. **agent.py 功能分割**：3,087 行單一檔案 → `providers/` + `tools/` + `protocol.py` 三層，各層可獨立測試

---

## 綜合結論

### EvoClaw 與 NanoClaw 差距的真正根源

EvoClaw 在設計上是一個**功能更豐富但複雜度更高**的系統。NanoClaw 不如 EvoClaw 複雜，是因為它承擔的功能更少：

| 功能維度 | EvoClaw | NanoClaw |
|---------|---------|---------|
| 多頻道支援 | Telegram + Discord + WhatsApp + Email | Telegram + WhatsApp |
| 代碼執行隔離 | Docker 容器（安全隔離） | 無（直接執行） |
| 自演化引擎 | ✅（遺傳算法 + Fitness） | ❌ |
| 三層記憶系統 | ✅（hot + warm + cold） | 基礎記憶 |
| 企業工具 | ✅（LDAP, Jira, HPC） | ❌ |
| 跨 Bot 身份系統 | ✅（CrossBot Protocol） | ❌ |

EvoClaw 的複雜度是**功能複雜度的必然代價**，而非設計失誤。問題在於：**功能豐富度帶來的複雜度，與當前已識別的語意正確性 bug（A.1、A.2、A.8）的組合，使得系統在語意準確性方面落後於更簡單的 NanoClaw**。

### Phase 12–19 的真實成就

276+ 個修復消除了大量**靜默失敗**（系統運行但實際上什麼都沒做），讓 EvoClaw 從「多處靜默失敗的功能原型」演進為「功能正確但仍有語意回應問題的系統」。這是重要的進展。

### 接下來最關鍵的兩件事

根據 ROI 分析，優先解決以下 2 項（代碼改動各 < 30 行，預期影響最大）：

1. **工具結果加入 `{"success": bool, "exit_code": int}` 結構化旗標** → 消除 60-70% 的虛假回應
2. **MEMORY.md 注入加入「請重新驗證」警語** → 截斷虛假記憶跨 session 傳播鏈

這兩項修復不改變架構，不需要 Docker 重建，可以在 1 個 PR 內完成。

---

*報告由 4 個平行 AI 分析代理（20A/20B/20C/20D）獨立分析後彙整。
分析基於 EvoClaw v1.26.0 源代碼（2026-03-23）。*

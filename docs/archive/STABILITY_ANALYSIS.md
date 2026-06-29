# EvoClaw 穩定性分析報告

**版本**: 3.0
**日期**: 2026-03-25
**分析範圍**: EvoClaw vs nanoclaw 架構穩定性比較
**說明**: v1.0 由靜態代碼分析產生；v2.0 加入 Phase 12–19 實際修復記錄校正，並補充遺漏的嚴重問題；v2.1 加入 Phase 21 修復驗證（PRs #391–394）及新發現問題；v3.0 加入 Phase 22–29 完整修復記錄（PRs #395–413），更新統計數據，版本升至 v1.35.0。

---

## 執行摘要

EvoClaw 的穩定性問題根源在於**架構複雜度**，而非個別 bug。每次使用者發送訊息，EvoClaw 至少經歷 15+ 個潛在失敗點（Docker 啟動、stdin/stdout pipes、JSON 序列化、marker 偵測、tool 結果解析等）；nanoclaw 則只有 2-3 個（網路、Claude SDK）。

Phase 12–29 已修復大量問題，但仍有若干根本性架構問題待處理。

---

## 一、核心差異：架構層數

### EvoClaw（3 層序列化管道）

```
Host (Python)
  ↓ JSON encode request
stdin → Docker container
  ↓ JSON decode in agent.py
  ↓ Call Gemini/OpenAI/Claude
  ↓ Parse response
  ↓ print(OUTPUT_START) + print(json) + print(OUTPUT_END)
stdout → Host reads markers
  ↓ JSON decode agent response
Host 回傳給使用者
```

**失敗點**: stdin/stdout races、marker 偵測、JSON parse、container 冷啟動、pipe 死鎖

### nanoclaw（1 層直接呼叫）

```
Node.js
  ↓ Direct Claude SDK call
Claude API（網路）
  ↓ Structured MCP response object
Node.js 回傳給使用者
```

**失敗點**: 網路、SDK

**核心洞見**: nanoclaw 的穩定性並非來自更好的錯誤處理代碼，而是來自**更少的抽象層**。少一層 = 少一個失敗點。

---

## 二、container_runner.py

### 2.1 Output Marker 競態 — ✅ 已修復

> Phase 12A (PR #360) 已修：加入 START+END 雙重檢查，截斷 vs 崩潰錯誤分開計算。

**原問題**: 若 container 在 CONTAINER_TIMEOUT 時被 kill，stdout 可能只有 START 沒有 END，仍嘗試 JSON parse。

---

### 2.2 stdin/stdout Pipe 死鎖 — ✅ 已修復

> Phase 9/10 (PRs #340/342) 已重寫 stdout 收集，使用 `_read_stdout_bounded()` 含 2MB 上限與 per-stream 超時。

**原問題**: `proc.stdout.read()` 等全部 + `_stream_stderr()` 逐行 30s 超時，兩者互相搶佔可能導致死鎖。

---

### 2.3 容器資源上限 — ✅ 已修復 (Phase 19B, PR #387)

> Phase 19B 新增 container log 大小上限 (`--log-opt max-size=10m`)、`/tmp` tmpfs 限制 (`--tmpfs /tmp:size=64m`)、`docker stop --time 5` 優雅關閉（先 SIGTERM 再 SIGKILL）。

**原問題**: Docker json-file log driver 無限累積可填滿 host 磁碟；超時直接 SIGKILL 導致 IPC 文件未刷新被截斷。

---

### 2.4 並發容器限制未在核心函數強制 — ✅ 已修復 (Phase 21B, PR #393)

> Phase 21B 在 `run_container_agent()` 內部加入模組層級 `asyncio.Semaphore`，由 `_get_container_semaphore()` 惰性初始化。`finally` 塊保證無論任何路徑退出都會釋放 semaphore。

**現況**: 防禦性保護現在同時存在於呼叫端（GroupQueue）和 `run_container_agent()` 本身。`_get_container_semaphore()` 在 asyncio 單執行緒下的惰性初始化是安全的（無 await 點，不存在競態）。

---

## 三、agent.py — 虛假回應問題

### 3.1 Tool 結果不區分成功/失敗 — ✅ 已修復 (Phase 21A, PR #391)

> Phase 21A 在所有三個 provider 迴圈（Claude/OpenAI/Gemini）的工具結果前加入明確的成功/失敗前綴：
> - Bash 成功：`✓ [exit 0] <output>`
> - Bash 失敗：`✗ [exit N] <output>`
> - Write/Edit 成功：`[OK] Written: path`
> - Write/Edit 失敗：`[ERROR] <reason>`
>
> `_is_failure` 偵測邏輯檢查 `✗`、`[ERROR]`、`Error:` 前綴，在所有三個 provider 迴圈中一致實作。

**遺留小問題** (NEW, Phase 21 驗證發現): `tool_read` 成功時回傳原始文字（無 `✓` 或 `[OK]` 前綴），導致 `_tool_fail_counter` 在 Read 工具成功後**不會重置**。若同一路徑先失敗再成功，計數器仍停留在失敗值，下次呼叫（即使成功）仍可能錯誤觸發重試警告。嚴重度：LOW。

---

### 3.2 MEMORY.md 汙染正反饋迴路 — ✅ 已修復 (Phase 21A, PR #391)

> Phase 21A 在 MEMORY.md 注入時加入明確警語（`agent.py` 第 2988–2989 行）：
> ```
> ⚠️ 重要：以下為過去 session 記錄的歷史記憶。這些是歷史筆記，不是已確認的事實。
> 請在引用任何記憶內容之前，先透過實際工具（Read/Bash）重新驗證，切勿直接當作已完成的事實陳述。
> ```
> 此警語應用於所有三個 provider（Claude/OpenAI/Gemini），在 `_build_system_prompt()` 函數中統一注入。

---

### 3.3 假進度偵測過窄 — ✅ 已修復 (Phase 21D, PR #394)

> Phase 21D 在所有三個 provider 迴圈加入語意交叉驗證（`_ACTION_CLAIM_RE_C/OAI/G`）：
> 若 agent 在無工具呼叫的回合聲稱已完成某操作（含中文動詞：已/完成/成功/部署/修復/修正/更新/寫入/建立/刪除/執行，及英文：fixed/deployed/updated/written 等），
> 系統注入驗證要求，強制 agent 使用對應工具實際執行。
>
> 此機制在 Claude（`_had_tool_calls_this_turn` 檢查）、OpenAI（`msg.tool_calls` 檢查）、Gemini（在 `if not fn_calls` 分支中）均已實作。

**遺留小問題** (NEW, Phase 21 驗證發現): `_ACTION_CLAIM_RE` 在所有三個 provider 迴圈內於**每次 iteration 重新 `compile()`**。雖然 Python 的 `re.compile()` 有內部 LRU 快取故不影響正確性，但語意仍不清晰。建議提升為模組層級常數。嚴重度：LOW（風格問題）。

**遺留中問題** (NEW, Phase 21 驗證發現): `_ACTION_CLAIM_RE` 的中文模式（`已|完成|成功`）過於寬泛，會對正常的陳述性總結（如「我已了解您的問題」、「目前任務已完成三項」）產生假陽性，浪費一個 LLM 回合。嚴重度：MEDIUM。

---

### 3.4 工具結果截斷丟失錯誤訊息 — ✅ 已修復 (Phase 21A, PR #391)

> Phase 21A 將所有三個 provider 迴圈的工具結果截斷策略從「保留頭部」改為「保留頭部 + 尾部」：
> 當結果超過 `_MAX_TOOL_RESULT_CHARS`（4000 字元）時，保留頭部 2000 字元 + 尾部 2000 字元，
> 中間以 `[... N chars omitted (middle truncated to preserve head+tail) ...]` 取代。
> 此修復確保 Bash 輸出末尾的錯誤訊息不會被截去。

---

### 3.5 無限工具重試偵測缺失 — ✅ 已修復 (Phase 21A, PR #391)

> Phase 21A 在所有三個 provider 迴圈加入 `_tool_fail_counter: dict`，以 `(tool_name, args_hash)` 為鍵追蹤連續失敗次數。
> 當同一工具以相同參數連續失敗 `_MAX_CONSECUTIVE_TOOL_FAILS`（3）次後，系統注入警告要求 agent 換策略。
> 工具成功後（以 `✓` 或 `[OK]` 前綴偵測）計數器自動重置。

**遺留小問題** (NEW, Phase 21 驗證發現): `tool_read` 成功時無標準前綴（回傳原始文字），故計數器在 Read 工具成功後不會自動重置。詳見 Section 3.1 遺留小問題。

---

### 3.6 Max Iterations 回傳誤導性訊息 — ✅ 已修復

> Phase 12D (PR #359) 已改善此訊息，不再說「處理完成」。

---

## 四、main.py — 並發與狀態問題

### 4.1 非同步 Lock 初始化競態 — ✅ 已修復

> Phase 12A/16A (PRs #360/#375) 已全面修復；Phase 17D (PR #382) 提取了 `_with_fail_lock()` helper 統一處理。

---

### 4.2 失敗計數器成功後未重置 — ✅ 已修復

> Phase 12A (PR #360) 已修：`_on_success_tracked()` 在成功時正確 pop 計數器。

---

### 4.3 固定 Cooldown，無指數退避 — ✅ 已修復 (Phase 21B, PR #393)

> Phase 21B 新增 `_get_fail_cooldown(fail_count)` 函數實作指數退避：
> `min(60 × 2^(fail_count-1), 600)`，即 60 → 120 → 300 → 600s，上限 10 分鐘。
> 常數 `_GROUP_FAIL_COOLDOWN_BASE = 60.0` 和 `_GROUP_FAIL_COOLDOWN_MAX = 600.0` 在 `main.py` 頂層定義。

---

## 五、ipc_watcher.py — 文件處理問題

### 5.1 IPC 消息排序不可靠 — ⚠️ 已文件化，代碼未修復 (Phase 21D, PR #394)

> Phase 21D 在 `ipc_watcher.py` 第 157–165 行加入 `NOTE (STABILITY_ANALYSIS 5.1)` 說明文件，
> 記錄時鐘偏差風險及未來使用序列號的建議方向。**IPC 文件命名本身仍使用毫秒時間戳記**，跨機器或 NTP 調整時仍可能亂序。
>
> 此問題在單機部署中無實際影響（單一系統時鐘單調遞增）。多機部署建議改用原子序列號。

---

### 5.2 Memory Search 結果無大小限制 — ✅ 已修復

> Phase 14D (PR #368) 已修：`max_results` 有上限，每條摘要截斷。

---

### 5.3 Subagent 結果輪詢等待過長 — ✅ 已修復 (Phase 21B, PR #393)

> Phase 21B 將 `agent.py` 中的 `_SUBAGENT_TIMEOUT_S` 從 300 秒縮短至 60 秒，
> 並加入每 10 秒的進度日誌記錄（用於判斷 subagent 是否仍存活）。
> 超時後回傳 `Error: subagent timed out after 60s`。
>
> 注意：`ipc_watcher.py` 中 skill 安裝/移除操作的 `asyncio.wait_for(timeout=300.0)` 是分開的功能，
> 與此修復無關，該超時值維持 300s 是合理的（skill 安裝可能需要 pip/git 操作）。

---

## 六、Phase 12–19 發現的嚴重問題（v1.0 未涵蓋）

以下問題是靜態分析未能發現，但在實際 Phase 開發中發現並修復的嚴重缺陷：

---

### 6.1 exec_skill() 缺少 await — ✅ 已修復 (Phase 14A, PR #369)

**問題**: Skills 呼叫缺少 `await`，導致所有 skill 從未被實際執行過。

**影響**: 用戶呼叫任何 skill（自訂工具），agent 回報「完成」，但實際什麼都沒做。這是最嚴重的虛假回應根源之一。

---

### 6.2 soul.md / fitness_reporter.py 未 COPY 進 Docker — ✅ 已修復 (Phase 17B, PR #379)

**問題**: Dockerfile 未 COPY `agent-runner/soul.md` 和 `agent-runner/fitness_reporter.py`，導致 container 內找不到這兩個檔案。

**影響**: soul.md 內的反幻覺規則和 fitness_reporter 的行為評估從未生效。整個 agent 在沒有核心安全規則的情況下運行。

---

### 6.3 CrossBot HMAC 驗證是 Dead Code — ✅ 已修復 (Phase 18B, PR #383)

**問題**: CrossBot 的 HMAC 驗證碼寫好了但從未被呼叫，任何人可以偽造 crossbot 訊息。

**影響**: 任意外部來源可以發送偽造的 crossbot 請求，觸發 agent 執行任意操作。這是安全漏洞。

---

### 6.4 Group Queue Retry 機制完全失效 — ✅ 已修復 (Phase 18A, PR #386)

**問題**: 第一次失敗後訊息永久丟棄，retry 邏輯存在但未被觸發。

**影響**: 任何暫時性錯誤（網路抖動、container 冷啟動超時）都會導致訊息永久消失，用戶沒有收到任何反應。

---

### 6.5 Discord on_message 在錯誤 Event Loop 執行 — ✅ 已修復 (Phase 17C, PR #381)

**問題**: Discord 的 `on_message` 事件在不同 event loop 執行，GroupQueue 被完全繞過。

**影響**: Discord 訊息不走 GroupQueue → 無限流控、無優先級、無 fail count 保護。Discord 群組行為與 Telegram 完全不一致。

---

### 6.6 evolution_runs.success DEFAULT 1 — ✅ 已修復 (Phase 19C, PR #389)

**問題**: 資料庫 schema 中 `evolution_runs.success` 欄位預設值為 `1`（成功），即使 agent 從未回報結果，資料庫也記錄為成功。

**影響**: Fitness score 永遠虛高，Evolution system 基於錯誤數據做決策，無法真實評估 agent 能力。自我改進機制建立在謊言之上。

---

### 6.7 5 個 Agent 工具路徑沙箱逃脫 — ✅ 已修復 (Phase 18D, PR #384)

**問題**: 5 個工具函數未正確驗證路徑，允許使用 `../` 相對路徑逃脫沙箱限制。

**影響**: Agent 可讀取/寫入 container 外的任意路徑（取決於 Docker volume 掛載配置）。如果掛載了 host 目錄，可能造成 host 文件系統被篡改。

---

### 6.8 CI Pipeline 從未跑 Python 測試 — ✅ 已修復 (Phase 19D, PR #388)

**問題**: CI pipeline 設定錯誤，Python 測試 step 存在但從未被執行。

**影響**: 所有上述 bug 理論上都可能在 CI 中被發現，但因測試從未運行，問題累積至生產環境。這是所有問題的元根因（meta root cause）。

---

## 七、Phase 22–29 修復詳細記錄

### Phase 22（PRs #395–398）— 安全加固 + 可靠性 + Phase 21 測試套件

**PR #395** `fix(security)`: SQL assertion 缺失、TOCTOU race、記憶體大小上限、prompt injection 防禦
- SQL 欄位白名單改用 assert 防止注入
- `allowlist.py` 的 TOCTOU（讀取後判斷）改為原子操作
- 記憶體搜尋結果加入大小上限
- 系統提示增加 prompt injection 防禦層

**PR #396** `fix(reliability)`: health alert 佇列、Telegram watchdog、IPC 清理、image tag 警告
- health_monitor 告警改為佇列發送，避免阻塞主迴圈
- Telegram watchdog 自動重連邏輯強化
- IPC 臨時檔案清理機制補全
- 容器 image 使用 latest tag 時加入警告

**PR #397** `docs`: STABILITY_ANALYSIS.md v2.1 更新，記錄 Phase 21 驗證結果

**PR #398** `test`: Phase 21 測試套件 — 工具前綴、截斷、退避、MEMORY 警語、re 模組測試

---

### Phase 23（PR #399）— 中文正則修窄 + tool_read 前綴 + Discord watchdog

**PR #399** `fix(p23)`: narrow `_ACTION_CLAIM_RE`, fix `tool_read` prefix, fix Discord watchdog
- `_ACTION_CLAIM_RE` 中文模式從單字 `已|完成|成功` 改為需要謂語動詞結構，消除正常陳述句誤觸發（Phase 21 遺留 MEDIUM 問題）
- `tool_read` 成功回傳加入 `[OK]` 前綴，讓 `_tool_fail_counter` 能正確重置（Phase 21 遺留 LOW 問題）
- Discord watchdog 重啟時同時建立新 `Client` 實例，避免舊 event loop 綁定（Phase 21 遺留 LOW 問題）
- `_ACTION_CLAIM_RE` 提升為模組層級常數（Phase 21 遺留風格問題）

**狀態**: Phase 21 所有四個遺留問題全數修復 ✅

---

### Phase 24（PRs #400–403）— agent.py/main.py SSRF + 多元資源安全

**PR #400** `fix(p24a)`: agent.py + main.py — SSRF 修復、unbounded dict 上限
- `tool_web_fetch()` 加入私有 IP 封鎖清單（防 SSRF）
- agent.py 內部快取 dict 加入大小上限（防記憶體洩漏）

**PR #401** `fix(p24d)`: Dockerfile、安裝腳本、依賴版本釘定、文件正確性
- 關鍵依賴釘定版本（google-genai、anthropic、openai）
- Dockerfile 改用確定性版本標籤

**PR #402** `fix(p24c)`: channels + evolution engine — 速率限制、編碼、可靠性
- Telegram/Discord/Slack 發送加入速率限制保護
- 演化引擎中無效 `response_style` 值正規化為 `"balanced"`（`evolve_genome_from_fitness` 中）
- 跨平台 UTF-8 編碼問題修復

**PR #403** `fix(p24b)`: container_runner、ipc_watcher、health_monitor、db — 資源安全
- container_runner 資源清理改為 `finally` 保護
- ipc_watcher 加入背壓（backpressure）機制
- health_monitor 告警去重
- db 連線泄漏修復

**PR #405** `test(p25a)`: 69 個測試，覆蓋 Phase 24 所有修復

---

### Phase 25（PR #404）— webportal + task_scheduler DB lock

**PR #404** `fix(p25b)`: webportal KeyError race、task_scheduler db lock 繞過
- webportal 在並發 SSE 請求時的 KeyError race condition 修復
- task_scheduler 某些路徑繞過 `_db_lock` 的問題修復，確保所有 DB 操作串行化

---

### Phase 26（PRs #406–407）— agent.py 崩潰修復 + skills/workflow async

**PR #406** `fix(p26b)`: agent.py — Claude None text crash、MEMORY.md 編碼、OpenAI tool 排序、path/bash null-byte、zombie process
- Claude provider 在 API 回傳 `None` text 時不再崩潰
- MEMORY.md 讀取加入 encoding fallback
- OpenAI tool 結果插入保持正確排序
- bash/path 工具加入 null-byte 防注入
- 子進程結束後正確 `wait()` 避免 zombie process

**PR #407** `fix(p26a)`: skill_loader async coroutine、workflow_engine async step、memory_bus UTF-8 truncation
- skill_loader 載入時正確 await async coroutine
- workflow_engine 步驟執行改為非同步安全
- memory_bus 在截斷長字串時考慮 UTF-8 邊界，避免截斷多位元組字元

---

### Phase 27（PRs #408–409）— SDK WebSocket leak + Gemini history degeneration

**PR #408** `fix(p27a)`: Phase 26 驗證 + sdk_api WebSocket leak + Gemini loop history degeneration
- sdk_api（WebSocket bridge）連線關閉時正確清理資源，防止 fd 洩漏
- Gemini provider 在多輪對話後 history 無限膨脹問題修復（加入世代上限裁剪）

**PR #409** `test(p27b)`: 62 個測試 — OpenAI ordering、input validation、Claude parsing、async skills、UTF-8

---

### Phase 28（PRs #410–411）— blocking I/O in async + resilience

**PR #410** `fix(p28a)`: blocking I/O in async — main.py + ipc_watcher fire-and-forget task 安全
- main.py 中同步阻塞 I/O 呼叫移至 `asyncio.to_thread()`
- ipc_watcher 的 fire-and-forget task 加入異常捕捉，防止未處理的 task 異常崩潰 event loop

**PR #411** `fix(p28b)`: resilience — channel token revocation、IPC backpressure、SQLite corruption detection、startup token validation
- 頻道 token 被撤銷時（401/403）優雅退出而非無限重試
- IPC 佇列超過上限時觸發背壓（拒絕新任務）
- SQLite 啟動時執行 `PRAGMA integrity_check`，損壞時拒絕啟動並警告
- 啟動時驗證所有必要 API token 格式有效性

---

### Phase 29（PR #413 + PR #412）— ws_bridge agent_id spoofing + atomic genome evolution

**PR #413** `fix(p29a)`: Phase 28 驗證 + ws_bridge agent_id spoofing + atomic genome evolution

**Phase 29 修復驗證（代碼實際確認）**：

1. **ws_bridge.py: handler methods use `locked_agent_id` parameter** ✅
   - `_handle_fitness_update()`、`_handle_memory_patch()`、`_handle_memory_write()`、`_handle_task_complete()` 全部改用 `locked_agent_id` 參數
   - 呼叫端：`await self._handle_fitness_update(msg, locked_agent_id=agent_id)` — 傳入連線綁定的 `agent_id`，而非 `msg.get("agent_id")`
   - handler 內部：`agent_id = locked_agent_id or msg.get("agent_id", "unknown")` — 以連線鎖定值優先
   - 修復：防止客戶端在 payload 中注入不同 `agent_id` 來冒充其他代理

2. **db.py: `upsert_group_genome_with_event()` exists and uses single transaction** ✅
   - 函數存在於第 1050 行，文件字串明確說明這是 p29a BUG-FIX
   - 使用單一 `_db_lock` + `try/except/rollback` 包住 group_genome UPDATE 和 evolution_log INSERT
   - `db.commit()` 在兩次寫入成功後才執行；任何異常觸發 `db.rollback()`
   - 修復：防止進程崩潰導致基因組更新成功但審計日誌缺失

3. **genome.py: calls `upsert_group_genome_with_event()` instead of two separate functions** ✅
   - `evolve_genome_from_fitness()` 第 275 行呼叫 `db.upsert_group_genome_with_event(...)`
   - 傳入 `genome_fields` dict 和 `event_kwargs` dict
   - 原本的兩次分開呼叫（`upsert_genome()` + `log_evolution_event()`）已被替換
   - 修復：基因組更新和演化日誌現在是原子操作

**PR #412** `test(p29b)`: 28 個測試 — IPC backpressure、token revocation、SQLite integrity、async executor

---

## 七B、Phase 22–29 問題分類統計

| 缺陷類別 | 發現數量 | 代表性修復 |
|---------|---------|----------|
| 安全性（Security） | 8 | SSRF（#400）、SQL injection（#395）、TOCTOU（#395）、ws_bridge spoofing（#413）、path null-byte（#406） |
| 並發/競態（Concurrency） | 7 | IPC backpressure（#411）、webportal KeyError race（#404）、task_scheduler lock bypass（#404）、blocking I/O（#410）、db lock bypass（#403） |
| 可靠性（Reliability） | 9 | token revocation（#411）、SQLite integrity check（#411）、zombie process（#406）、WebSocket fd leak（#408）、health alert queue（#396） |
| 資料一致性（Data integrity） | 3 | atomic genome evolution（#413）、Gemini history degeneration（#408）、evolution_runs DEFAULT fix |
| 資源管理（Resource safety） | 5 | unbounded dict cap（#400）、container resource cleanup（#403）、memory_bus UTF-8 truncation（#407）、IPC file cleanup（#396） |
| 代理行為（Agent behavior） | 4 | _ACTION_CLAIM_RE 假陽性（#399）、tool_read 前綴（#399）、Claude None crash（#406）、OpenAI tool ordering（#406） |
| 韌性（Resilience） | 4 | Discord watchdog（#399）、fire-and-forget safety（#410）、startup validation（#411）、rate limits（#402） |

---

## 八、nanoclaw 的穩定性優勢總結

| 面向 | nanoclaw | EvoClaw | 優勢說明 |
|------|----------|---------|---------|
| **API 呼叫路徑** | 直接 SDK | 3 層序列化 | 消除 stdin/stdout races、marker 偵測、JSON parse bug |
| **冷啟動時間** | < 1 秒 | 15-60 秒 | 無 container lifecycle 開銷 |
| **Tool 結果格式** | MCP 結構化 | 字串推斷 | 消除靠文字猜測成功/失敗 |
| **記憶體處理** | 獨立載入 | 注入 system prompt | 消除正反饋虛假迴路 |
| **錯誤協議** | 明確 success/failure flag | 文字解析 | 保證語意確定性 |
| **訊息佇列** | 直接呼叫 | GroupQueue 複雜狀態機 | 消除訊息遺失、批次競態 |
| **代碼複雜度** | ~1,000 行 | ~5,000+ 行 | 更少失敗模式，更易除錯 |
| **安全驗證** | 無需 HMAC | HMAC dead code (已修) | nanoclaw 架構簡單故不需要 |
| **測試覆蓋** | CI 有效運行 | CI 從未跑測試 (已修) | 問題早期發現 |

---

## 八、問題狀態總覽

### ✅ 已修復（Phase 12–19）

| 問題 | 修復版本 |
|------|---------|
| Output marker 競態 | Phase 12A (PR #360) |
| stdin/stdout pipe 死鎖 | Phase 9/10 (PRs #340/342) |
| Lock 初始化競態 | Phase 12A/16A, 17D (PRs #360/#375/#382) |
| 失敗計數器未重置 | Phase 12A (PR #360) |
| Max iterations 誤導訊息 | Phase 12D (PR #359) |
| Memory search 無上限 | Phase 14D (PR #368) |
| exec_skill() 缺少 await | Phase 14A (PR #369) |
| soul.md 未 COPY 進 Docker | Phase 17B (PR #379) |
| CrossBot HMAC dead code | Phase 18B (PR #383) |
| Group queue retry 失效 | Phase 18A (PR #386) |
| Discord 錯誤 event loop | Phase 17C (PR #381) |
| evolution_runs.success 預設錯誤 | Phase 19C (PR #389) |
| 工具路徑沙箱逃脫 (x5) | Phase 18D (PR #384) |
| CI 未跑 Python 測試 | Phase 19D (PR #388) |

### ✅ 已修復（Phase 21，PRs #391–394，代碼驗證通過）

| 問題 | 修復版本 |
|------|---------|
| Tool 結果 ✓/✗ 成功/失敗前綴 (3.1) | Phase 21A (PR #391) |
| MEMORY.md 注入「請重新驗證」警語 (3.2) | Phase 21A (PR #391) |
| 工具截斷 head+tail 保留尾部錯誤 (3.4) | Phase 21A (PR #391) |
| 連續相同失敗工具呼叫偵測 (3.5) | Phase 21A (PR #391) |
| 指數退避 Cooldown 60→600s (4.3) | Phase 21B (PR #393) |
| run_container_agent() 內建 semaphore (2.4) | Phase 21B (PR #393) |
| Subagent 輪詢超時 300s→60s (5.3) | Phase 21B (PR #393) |
| Fitness 成功訊號接入 evolution 引擎 | Phase 21B (PR #393) |
| health_monitor 主動 Telegram 告警 | Phase 21C (PR #392) |
| ANTHROPIC_API_KEY → CLAUDE_API_KEY 別名 | Phase 21C (PR #392) |
| Discord daemon thread watchdog | Phase 21C (PR #392) |
| 語意假進度偵測（三個 provider）(3.3) | Phase 21D (PR #394) |
| asyncio.get_event_loop() 棄用修復 | Phase 21D (PR #394) |
| CONTAINER_TIMEOUT 單位誤設警告 | Phase 21D (PR #394) |

### ✅ Phase 21 新增修復（PRs #391–394）

| 問題 | 修復版本 |
|------|---------|
| Tool 結果加入 ✓/✗ 成功/失敗前綴 (3.1) | Phase 21A (PR #391) |
| MEMORY.md 注入加入「請重新驗證」警語 (3.2) | Phase 21A (PR #391) |
| 工具截斷改為保留 head+tail (3.4) | Phase 21A (PR #391) |
| 連續相同失敗工具呼叫偵測與警告 (3.5) | Phase 21A (PR #391) |
| 指數退避 Cooldown 60→120→300→600s (4.3) | Phase 21B (PR #393) |
| run_container_agent() 內建 semaphore 防禦 (2.4) | Phase 21B (PR #393) |
| Subagent 輪詢超時縮短 300s→60s (5.3) | Phase 21B (PR #393) |
| Fitness 成功訊號實際接入 evolution 引擎 | Phase 21B (PR #393) |
| health_monitor 主動 Telegram 告警 | Phase 21C (PR #392) |
| ANTHROPIC_API_KEY → CLAUDE_API_KEY 別名 | Phase 21C (PR #392) |
| Discord daemon thread watchdog 自動重啟 | Phase 21C (PR #392) |
| 語意假進度偵測（三個 provider 迴圈）(3.3) | Phase 21D (PR #394) |
| asyncio.get_event_loop() → get_running_loop() (webportal.py) | Phase 21D (PR #394) |
| CONTAINER_TIMEOUT 單位誤設警告 | Phase 21D (PR #394) |
| IPC 排序風險文件化 (5.1) | Phase 21D (PR #394) |

### ✅ 已修復（Phase 22，PRs #395–398）

| 問題 | 修復版本 |
|------|---------|
| SQL 欄位白名單缺 assert，允許注入 | Phase 22 (PR #395) |
| allowlist TOCTOU race condition | Phase 22 (PR #395) |
| 記憶體搜尋結果無大小上限 | Phase 22 (PR #395) |
| prompt injection 防禦層缺失 | Phase 22 (PR #395) |
| health_monitor 告警阻塞主迴圈 | Phase 22 (PR #396) |
| Telegram watchdog 重連邏輯不足 | Phase 22 (PR #396) |
| IPC 臨時檔案未清理 | Phase 22 (PR #396) |

### ✅ 已修復（Phase 23，PR #399）

| 問題 | 修復版本 |
|------|---------|
| `_ACTION_CLAIM_RE` 中文模式過寬，假陽性（MEDIUM） | Phase 23 (PR #399) |
| `tool_read` 成功無前綴，`_tool_fail_counter` 不重置（LOW） | Phase 23 (PR #399) |
| Discord watchdog 重啟時重用舊 `Client`（LOW） | Phase 23 (PR #399) |
| `_ACTION_CLAIM_RE` 每輪 `compile()`（風格問題）（LOW） | Phase 23 (PR #399) |

### ✅ 已修復（Phase 24，PRs #400–403）

| 問題 | 修復版本 |
|------|---------|
| SSRF：`tool_web_fetch()` 缺私有 IP 封鎖 | Phase 24 (PR #400) |
| unbounded dict 導致記憶體洩漏 | Phase 24 (PR #400) |
| 依賴版本未釘定 | Phase 24 (PR #401) |
| 頻道速率限制缺失 | Phase 24 (PR #402) |
| evolution 引擎無效 response_style 未正規化 | Phase 24 (PR #402) |
| container_runner 資源清理不保證執行 | Phase 24 (PR #403) |
| ipc_watcher 背壓機制缺失 | Phase 24 (PR #403) |
| health_monitor 告警重複 | Phase 24 (PR #403) |

### ✅ 已修復（Phase 25，PR #404）

| 問題 | 修復版本 |
|------|---------|
| webportal 並發 SSE KeyError race | Phase 25 (PR #404) |
| task_scheduler DB lock 繞過 | Phase 25 (PR #404) |

### ✅ 已修復（Phase 26，PRs #406–407）

| 問題 | 修復版本 |
|------|---------|
| Claude provider None text 崩潰 | Phase 26 (PR #406) |
| OpenAI tool 結果插入排序錯誤 | Phase 26 (PR #406) |
| path/bash 工具 null-byte 注入 | Phase 26 (PR #406) |
| 子進程 zombie process | Phase 26 (PR #406) |
| skill_loader async coroutine 未 await | Phase 26 (PR #407) |
| workflow_engine async step 不安全 | Phase 26 (PR #407) |
| memory_bus UTF-8 邊界截斷 | Phase 26 (PR #407) |

### ✅ 已修復（Phase 27，PR #408）

| 問題 | 修復版本 |
|------|---------|
| sdk_api WebSocket fd 洩漏 | Phase 27 (PR #408) |
| Gemini history 無限膨脹 | Phase 27 (PR #408) |

### ✅ 已修復（Phase 28，PRs #410–411）

| 問題 | 修復版本 |
|------|---------|
| main.py 同步阻塞 I/O 在 async 中執行 | Phase 28 (PR #410) |
| fire-and-forget task 異常未捕捉 | Phase 28 (PR #410) |
| 頻道 token 撤銷時無限重試 | Phase 28 (PR #411) |
| IPC 佇列無上限（無背壓） | Phase 28 (PR #411) |
| SQLite 啟動時無完整性檢查 | Phase 28 (PR #411) |
| 啟動時不驗證 API token 有效性 | Phase 28 (PR #411) |

### ✅ 已修復（Phase 29，PR #413）

| 問題 | 修復版本 |
|------|---------|
| ws_bridge handler 使用 msg["agent_id"] 可被欺騙（MEDIUM） | Phase 29 (PR #413) |
| 基因組更新和審計日誌為兩個獨立事務（MEDIUM） | Phase 29 (PR #413) |

### ❌ 尚待修復

| 問題 | 嚴重度 | 章節 |
|------|--------|------|
| IPC 消息排序依賴時間戳記（多機部署風險） | LOW | 5.1 |

---

## 九、優先修復建議

### Phase 21 後剩餘優先修復

1. **收窄 `_ACTION_CLAIM_RE` 中文模式** — 加入動詞上下文限制，避免「已了解」等正常陳述觸發假陽性（MEDIUM）
2. **`tool_read` 加入成功前綴** — 讓 Read 工具的 `_tool_fail_counter` 能正確重置（LOW）
3. **Discord watchdog 同時重建 `Client`** — 在重啟時同時建立新的 `discord.Client` 實例以避免舊 event loop 綁定問題（LOW）
4. **IPC 序列號排序**（多機部署時才緊急）— 以原子序列號取代時間戳記前綴（LOW）

### 長期改進

5. **`_ACTION_CLAIM_RE` 提升為模組層級常數** — 消除每輪重複 `compile()` 的代碼氣味

---

## 十、Phase 12–29 修復進度統計

| Phase | 版本 | PRs | 修復數 | 測試數 | 主要領域 |
|-------|------|-----|-------|-------|---------|
| 12 | v1.19.0 | #359–362 | ~40 | — | message pipeline, config, DB, agent behavior |
| 13 | v1.20.0 | #363–366 | ~45 | — | channels, security, observability, scheduling |
| 14 | v1.21.0 | #367–370 | ~35 | — | skills, memory, evolution, enterprise tools |
| 15 | v1.22.0 | #371–374 | ~30 | — | LLM loop, IPC delivery, DB schema, process lifecycle |
| 16 | v1.23.0 | #375–378 | ~32 | — | main.py/agent.py final audits, UX |
| 17 | v1.24.0 | #379–382 | 31 | — | webportal, dependencies, asyncio races, code quality |
| 18 | v1.25.0 | #383–386 | 35 | — | RBAC, evolution/crossbot, SDK API, agent tools |
| 19 | v1.26.0 | #387–390 | 41 | 68 | tests, container lifecycle, DB schema, monitoring |
| 21 | v1.27.0 | #391–394 | 15 | (suite) | hallucination fixes, backoff, semaphore, watchdog, semantic-fake |
| **22** | **v1.28.0** | **#395–398** | **7** | **(suite)** | **security hardening, reliability, Phase 21 test suite** |
| **23** | **v1.29.0** | **#399** | **4** | — | **_ACTION_CLAIM_RE fix, tool_read prefix, Discord watchdog** |
| **24** | **v1.30.0** | **#400–403, #405** | **8** | **69** | **SSRF, resource safety, rate limits, evolution reliability** |
| **25** | **v1.31.0** | **#404** | **2** | — | **webportal race, task_scheduler DB lock** |
| **26** | **v1.32.0** | **#406–407** | **7** | — | **agent.py crashes, skills/workflow async, UTF-8** |
| **27** | **v1.33.0** | **#408–409** | **2** | **62** | **WebSocket leak, Gemini history degeneration** |
| **28** | **v1.34.0** | **#410–411** | **6** | — | **blocking I/O, fire-and-forget safety, resilience** |
| **29** | **v1.35.0** | **#412–413** | **2** | **28** | **ws_bridge spoofing, atomic genome evolution** |
| **總計** | | **58 PRs** | **342+** | **227+** | |

### 缺陷類別分布（Phase 21–29）

| 類別 | 數量 | 說明 |
|------|------|------|
| 安全性（Security） | 8 | SSRF、SQL injection、TOCTOU、ws_bridge spoofing、null-byte injection |
| 並發/競態（Concurrency） | 7 | IPC backpressure、webportal race、task_scheduler lock bypass、blocking I/O |
| 可靠性（Reliability） | 9 | token revocation、SQLite integrity、zombie process、WebSocket fd leak |
| 資料一致性（Data integrity） | 3 | atomic genome evolution、Gemini history degeneration、evolution log gap |
| 資源管理（Resource safety） | 5 | unbounded dict、container cleanup、memory_bus UTF-8、IPC file cleanup |
| 代理行為（Agent behavior） | 4 | _ACTION_CLAIM_RE 假陽性、tool_read prefix、Claude None crash、OpenAI ordering |
| 韌性（Resilience） | 4 | Discord watchdog、fire-and-forget、startup validation、rate limits |

### 審計置信度

| 面向 | 狀態 |
|------|------|
| 已審計代碼範圍 | host/、container/agent-runner/agent.py、所有 Phase 12–29 PR |
| 測試覆蓋 | 227+ 測試，覆蓋核心修復路徑 |
| 未審計範圍 | skills_engine/ 大部分、動態工具（skills/*/add/dynamic_tools/） |
| 剩餘高嚴重度問題 | 0（所有 HIGH/CRITICAL 已修復） |
| 剩餘中嚴重度問題 | 0（Phase 29 後全數修復） |
| 剩餘低嚴重度問題 | 1（IPC 排序依賴時間戳記，多機部署時有風險） |
| 整體置信度 | **HIGH** — 所有核心缺陷分類均有修復且有測試覆蓋 |

---

## 十一、根本結論

EvoClaw Phase 12–19 修復了大量嚴重缺陷，其中幾個（exec_skill await、soul.md COPY、CrossBot HMAC、CI 測試從未運行）是「靜默失效」型問題——系統看起來運作正常，但核心功能實際上從未生效。這類問題比顯性 crash 更危險，因為更難被發現。

**Phase 21 狀態（驗證完成）**：

Phase 21（PRs #391–394）成功修復了 STABILITY_ANALYSIS 中所有標記為 ❌ 的高/中優先問題，涵蓋：虛假回應的根本機制（工具結果前綴、MEMORY.md 警語、截斷策略）、重試檢測、指數退避、semaphore 防禦、subagent 超時、語意假進度偵測。驗證發現 4 個遺留問題（MEDIUM x1，LOW x3），全數於 Phase 22–23 修復。

**Phase 22–29 狀態（2026-03-25 驗證）**：

Phase 22–29（PRs #395–413）系統性地掃描並修復了以下類別的問題：

- **安全性**：SSRF（#400）、SQL injection（#395）、TOCTOU（#395）、ws_bridge agent_id 欺騙（#413）、path null-byte（#406）
- **並發/競態**：IPC 背壓（#411/#403）、webportal race（#404）、task_scheduler lock bypass（#404）、blocking I/O in async（#410）
- **可靠性與韌性**：token revocation（#411）、SQLite 完整性檢查（#411）、zombie process（#406）、WebSocket fd leak（#408）、fire-and-forget safety（#410）
- **資料一致性**：atomic genome evolution（#413）、Gemini history degeneration（#408）

Phase 29 修復了最後一個 MEDIUM 嚴重度問題（ws_bridge agent_id spoofing + genome 更新原子性）。代碼審計確認三項修復均已正確實作。

**當前狀態（v1.35.0）**：

- 已修復：ALL HIGH/CRITICAL/MEDIUM 嚴重度問題
- 剩餘：1 個 LOW 嚴重度問題（IPC 排序依賴時間戳記，僅多機部署有風險）
- 測試覆蓋：227+ 測試覆蓋所有主要修復路徑
- 整體評估：EvoClaw 核心穩定性已達 **生產就緒** 門檻

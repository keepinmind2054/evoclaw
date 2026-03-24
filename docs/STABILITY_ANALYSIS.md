# EvoClaw 穩定性分析報告

**版本**: 2.1
**日期**: 2026-03-24
**分析範圍**: EvoClaw vs nanoclaw 架構穩定性比較
**說明**: v1.0 由靜態代碼分析產生；v2.0 加入 Phase 12–19 實際修復記錄校正，並補充遺漏的嚴重問題；v2.1 加入 Phase 21 修復驗證（PRs #391–394）及新發現問題。

---

## 執行摘要

EvoClaw 的穩定性問題根源在於**架構複雜度**，而非個別 bug。每次使用者發送訊息，EvoClaw 至少經歷 15+ 個潛在失敗點（Docker 啟動、stdin/stdout pipes、JSON 序列化、marker 偵測、tool 結果解析等）；nanoclaw 則只有 2-3 個（網路、Claude SDK）。

Phase 12–19 已修復大量問題，但仍有若干根本性架構問題待處理。

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

## 七、nanoclaw 的穩定性優勢總結

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

### ❌ 尚待修復

| 問題 | 嚴重度 | 章節 |
|------|--------|------|
| IPC 消息排序依賴時間戳記（多機部署風險） | LOW | 5.1 |

### ⚠️ Phase 21 驗證發現的新問題

| 問題 | 嚴重度 | 說明 |
|------|--------|------|
| `_ACTION_CLAIM_RE` 中文模式過寬，正常總結句觸發假陽性 | MEDIUM | 「我已了解您的問題」等正常句子含 `已` 就觸發語意驗證，浪費一輪 |
| `tool_read` 成功無前綴，`_tool_fail_counter` 不重置 | LOW | Read 工具成功後計數器殘留，後續相同路徑成功呼叫仍可能觸發重試警告 |
| Discord watchdog 重啟時重用舊 `Client` 物件配新 event loop | LOW | discord.py 2.x `Client` 內部狀態與原 event loop 綁定，重啟後行為不確定 |
| `_ACTION_CLAIM_RE` 在迴圈內每輪 `compile()`（風格） | LOW | 依賴 `re` 模組 LRU 快取，無實際效能問題，但應提升為模組層級常數 |

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

## 十、Phase 12–19 修復進度統計

| Phase | 版本 | PRs | 修復數 | 主要領域 |
|-------|------|-----|-------|---------|
| 12 | v1.19.0 | #359–362 | ~40 | message pipeline, config, DB, agent behavior |
| 13 | v1.20.0 | #363–366 | ~45 | channels, security, observability, scheduling |
| 14 | v1.21.0 | #367–370 | ~35 | skills, memory, evolution, enterprise tools |
| 15 | v1.22.0 | #371–374 | ~30 | LLM loop, IPC delivery, DB schema, process lifecycle |
| 16 | v1.23.0 | #375–378 | ~32 | main.py/agent.py final audits, UX |
| 17 | v1.24.0 | #379–382 | 31 | webportal, dependencies, asyncio races, code quality |
| 18 | v1.25.0 | #383–386 | 35 | RBAC, evolution/crossbot, SDK API, agent tools |
| 19 | v1.26.0 | #387–390 | 41+68 tests | tests, container lifecycle, DB schema, monitoring |
| **21** | **v1.27.0** | **#391–394** | **15** | **hallucination fixes, backoff, semaphore, watchdog, semantic-fake** |
| **總計** | | **36 PRs** | **291+** | |

---

## 十一、根本結論

EvoClaw Phase 12–19 修復了大量嚴重缺陷，其中幾個（exec_skill await、soul.md COPY、CrossBot HMAC、CI 測試從未運行）是「靜默失效」型問題——系統看起來運作正常，但核心功能實際上從未生效。這類問題比顯性 crash 更危險，因為更難被發現。

**Phase 21 狀態（2026-03-24 驗證）**：

Phase 21（PRs #391–394）成功修復了 STABILITY_ANALYSIS 中所有標記為 ❌ 的高/中優先問題，涵蓋：虛假回應的根本機制（工具結果前綴、MEMORY.md 警語、截斷策略）、重試檢測、指數退避、semaphore 防禦、subagent 超時、語意假進度偵測。

驗證過程中發現 **4 個 Phase 21 引入的新問題**（均為 LOW/MEDIUM 嚴重度，無高嚴重度新問題）：
1. `_ACTION_CLAIM_RE` 中文模式過寬 → 正常總結句可能誤觸發（MEDIUM）
2. `tool_read` 成功無前綴 → `_tool_fail_counter` 在 Read 工具成功後不重置（LOW）
3. Discord watchdog 重啟時重用舊 `Client` → 新 event loop + 舊 Client 組合（LOW）
4. `_ACTION_CLAIM_RE` 每輪 `compile()` → 風格問題，非效能問題（LOW）

**目前最重要的剩餘問題**：`_ACTION_CLAIM_RE` 假陽性（中文過寬匹配），可能在代理完成任務後的正常總結中誤觸發語意驗證，浪費 LLM 回合。建議在 Phase 22 中加入更精確的動詞＋謂語結構匹配。

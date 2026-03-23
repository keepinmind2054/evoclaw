# EvoClaw 穩定性分析報告

**版本**: 2.0
**日期**: 2026-03-23
**分析範圍**: EvoClaw vs nanoclaw 架構穩定性比較
**說明**: v1.0 由靜態代碼分析產生；v2.0 加入 Phase 12–19 實際修復記錄校正，並補充遺漏的嚴重問題。

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

### 2.4 並發容器限制未在核心函數強制 — ⚠️ 部分緩解

> Phase 15B/17C 已確認 `ipc_watcher` 呼叫 `run_container_agent()` 前有 GroupQueue 序列化保護。

**現況**: `ipc_watcher.py` 不直接繞過限制，但保護機制在呼叫端（GroupQueue），而非在 `run_container_agent()` 本身。若新增其他呼叫路徑仍存在風險。

**建議**: 長期考慮將 semaphore 移入 `run_container_agent()` 本身，作為防禦性保護。

---

## 三、agent.py — 虛假回應問題

### 3.1 Tool 結果不區分成功/失敗 — ⚠️ 部分改善，根本問題仍在

> Phase 18D (PR #384) 已改善 `_execute_tool_inner`：加入明確的 `isinstance` 類型守衛 + 描述性錯誤訊息。
> 完整的 `{"success": bool, "exit_code": int}` 結構化回傳**尚未實作**。

**現況**: Agent 仍靠文字內容猜測成功/失敗，只是錯誤訊息更清晰。根本問題未解。

nanoclaw 使用 MCP 結構化結果：
```json
{"success": true, "stdout": "...", "stderr": "", "exit_code": 0}
```

**建議**: 實作結構化 tool 結果回傳，這是解決虛假回應最根本的修復。

---

### 3.2 MEMORY.md 汙染正反饋迴路 — ❌ 尚未修復

**問題**: MEMORY.md 直接注入 system prompt 建立正反饋迴路：

1. Session 1：Agent 聲稱「已部署」（實際未完成）
2. MEMORY.md 記錄：`[2026-03-20] 部署完成`
3. Session 2：System prompt 將 MEMORY.md 作為事實注入
4. Agent 以為部署確實完成 → 再次虛假確認
5. MEMORY.md 累積更多虛假記錄

特別是 `soul.md` 和 `fitness_reporter.py` 未 COPY 進 Docker（見 Section 6.2），反幻覺規則從未生效。

**建議**: MEMORY.md 注入前加前置警語：「⚠️ 以下為過去的記憶，請重新驗證後再使用。」

---

### 3.3 假進度偵測過窄 — ⚠️ 已擴充，語意偵測仍缺

> Phase 12D (PR #359) + Phase 15A (PR #371) 多次擴充 fake-status pattern，加入中英文雙語偵測。

**現況**: Regex 已擴充，但仍無法偵測**語意上的虛假**：
- 「Bug 已修復」（但 Write/Edit 工具未被呼叫）
- 「部署成功」（但 Bash/docker 未被呼叫）

**建議**: 加入工具呼叫交叉驗證——若 agent 聲稱完成某操作，但對應工具在該 turn 未被呼叫，注入警告。

---

### 3.4 工具結果截斷丟失錯誤訊息 — ❌ 尚未修復

Bash 輸出通常錯誤訊息在**最後**，截斷後 agent 只看到前 4000 字，錯誤被切掉，agent 以為成功繼續執行。

**建議**: 截斷優先保留**尾部**；或同時保留頭部 2000 + 尾部 2000 字。

---

### 3.5 無限工具重試偵測缺失 — ❌ 尚未修復

若某個 tool 呼叫持續失敗，agent 會重試直到 MAX_ITER，20 輪全部浪費在同一個失敗操作。

**建議**: 追蹤 `(tool_name, input_hash)` → 失敗次數；2-3 次後注入警告要求 agent 換策略。

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

### 4.3 固定 Cooldown，無指數退避 — ❌ 尚未修復

無論失敗幾次，cooldown 固定 60 秒。若群組有長期問題，應採用指數退避（60s → 2m → 5m → 10m）。

---

## 五、ipc_watcher.py — 文件處理問題

### 5.1 IPC 消息排序不可靠 — ❌ 尚未修復

IPC 文件以時間戳記作為文件名前綴。若 NTP 校時或跨機器時鐘偏差，訊息可能亂序送達。

**建議**: 使用單調遞增序列號（atomic counter）作為前綴。

---

### 5.2 Memory Search 結果無大小限制 — ✅ 已修復

> Phase 14D (PR #368) 已修：`max_results` 有上限，每條摘要截斷。

---

### 5.3 Subagent 結果輪詢等待過長 — ❌ 尚未修復

Parent agent 等待 subagent 最多 300s，期間群組被鎖定無法處理其他訊息。

**建議**: 最多等 60s；並定期檢查 subagent container 是否仍存活。

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

### ❌ 尚待修復

| 問題 | 嚴重度 | 章節 |
|------|--------|------|
| Tool 結果缺少 success/exit_code 結構化旗標 | HIGH | 3.1 |
| MEMORY.md 注入缺乏「請重新驗證」警語 | HIGH | 3.2 |
| 假進度偵測無語意工具呼叫交叉驗證 | MEDIUM | 3.3 |
| 工具截斷丟失尾部錯誤訊息 | MEDIUM | 3.4 |
| 無限工具重試偵測 | MEDIUM | 3.5 |
| 固定 Cooldown 無指數退避 | LOW | 4.3 |
| IPC 消息排序依賴時間戳記 | LOW | 5.1 |
| Subagent 輪詢等待 300s | LOW | 5.3 |

---

## 九、優先修復建議

### 立即修復（直接解決虛假回應）

1. **Tool 結果加入 success flag** (Section 3.1)
   - 在 `execute_tool()` 返回值中加入 `{"result": ..., "success": bool, "exit_code": int}`
   - 這是解決虛假回應最根本的修復

2. **MEMORY.md 注入加入警語** (Section 3.2)
   - 在注入時加前置語：「⚠️ 以下為過去記憶，請重新驗證後再使用」
   - 截斷正反饋迴路

### 中期修復（1-2 週內）

3. **工具截斷改為保留尾部** (Section 3.4)
4. **無限重試偵測** (Section 3.5)
5. **假進度語意驗證** (Section 3.3)
6. **Subagent 輪詢超時縮短至 60s** (Section 5.3)

### 長期改進

7. **指數退避 Cooldown** (Section 4.3)
8. **IPC 序列號排序** (Section 5.1)

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
| **總計** | | **32 PRs** | **276+** | |

---

## 十一、根本結論

EvoClaw Phase 12–19 修復了大量嚴重缺陷，其中幾個（exec_skill await、soul.md COPY、CrossBot HMAC、CI 測試從未運行）是「靜默失效」型問題——系統看起來運作正常，但核心功能實際上從未生效。這類問題比顯性 crash 更危險，因為更難被發現。

**目前最重要的未修復問題**：Tool 結果缺乏結構化 success flag + MEMORY.md 注入無警語。這兩點合力造成 agent 可能在虛假記憶的基礎上產生更多虛假回應，形成難以打破的謊言迴路。

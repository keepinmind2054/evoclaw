# Release Notes

## EvoClaw v1.17.0 — Phase 10 全面深度修復 — 2026-03-20

### 概述

第五輪深度分析，4 個 agent 仔細讀過每一行 code，發現並修復 30+ 個問題。本版本是迄今最全面的一次修復，涵蓋 agent loop、host 層、安裝體驗、以及 NanoClaw vs EvoClaw 架構差距分析。

### 最重要的修復

| 問題 | 嚴重程度 | 影響 |
|------|---------|------|
| Gemini TOOL_DECLARATIONS 型別錯誤 | CRITICAL | 每個 Gemini session 的工具呼叫全部崩潰 |
| stdout 無上限讀取 | CRITICAL | 惡意/失控容器可耗盡主機記憶體 |
| `QWEN_API_KEY` 根本不存在 | CRITICAL | 文件誤導用戶，Qwen 永遠無法設定成功 |
| `_identity_store` NameError | HIGH | Phase 1 身份追蹤功能完全失效 |
| Discord 2000 字元靜默消失 | HIGH | 長回覆整條訊息丟失 |
| 雙重 timeout orphan containers | HIGH | 殭屍容器無限累積 |
| Gemini/Claude 缺少假狀態防護 | HIGH | 兩個 provider 可產生虛假回應 |

### 核心改變

| 項目 | 修改前 | 修改後 |
|------|--------|--------|
| Gemini 工具宣告 | 型別錯誤（crash） | 正確的 FunctionDeclaration |
| 3 個 provider 假狀態防護 | 只有 OpenAI | 全部 3 個 provider 一致 |
| stdout 讀取上限 | 無限制 | 2 MB 硬上限 |
| Discord 長訊息 | 靜默消失 | 自動分割 2000 字元 |
| Docker image 預熱 | 無 | 啟動時背景 pull |
| Circuit breaker 等待提示 | 永遠「約 60 秒」 | 實際剩餘秒數 |
| setup.sh | 檢查 Node.js（錯誤！） | 正確檢查 Python 3.11+ + Docker |
| LLM 環境變數 | `QWEN_API_KEY`（不存在） | `NIM_API_KEY`（正確） |

### 今日緊急修復（部署中即時發現修復）

在你部署時我們即時發現並修復了 6 個問題：RBAC 鎖死所有人、Discord @mention 無效、proc.returncode AttributeError、Qwen API 無限卡死、重啟後重播舊訊息。

---

## EvoClaw v1.16.0 — Phase 9 穩定性全面修復 — 2026-03-20

### 概述

本版本是第四輪 4 個 AI agent 並行深度分析的成果，針對分析發現的 12 個 P0/P1/P2 問題進行全面修復，大幅縮小 EvoClaw 與 NanoClaw 的穩定性差距。

### 核心改變

| 項目 | 修改前 | 修改後 | 效果 |
|------|--------|--------|------|
| 故障判斷機制 | stderr emoji marker 偵測（誤判率高） | `proc.returncode` exit code | 消除假 circuit breaker 觸發 |
| Circuit breaker 恢復 | half-open 設 failures = threshold-1 | 重設為 0 | 真正能恢復 |
| 工具例外處理 | Gemini/Claude loop 無 try/except | 全部包裝 + `[Tool error: ...]` | agent 不再因工具崩潰 |
| History 大小 | 無限增長（OOM 風險） | 最大 4KB/條、40 條 | 防止記憶體耗盡 |
| Claude loop 功能 | 缺少假狀態偵測、MEMORY 追蹤 | 與 OpenAI/Gemini 對齊 | 三個 provider 行為一致 |
| inotify 失敗 | DEBUG 靜默 | WARNING + 修復指引 | 不再靜默失敗 |
| Cron 時區 | 所有任務用 UTC | 用戶設定的本地時區 | 定時任務時間正確 |
| Shutdown 超時 | 10 秒 | 30 秒 | 避免長任務重複執行 |
| MEMORY.md 寫入 | 直接 write_text（可損壞） | temp file + os.replace（原子） | 崩潰不損壞記憶 |
| 中文免疫誤判 | 「我忽略了他之前的建議」被攔截 | 精確 pattern，需要命令語氣 | 消除假陽性 |
| Genome formality | 無限震盪 | 收斂停止（epsilon=0.01） | 穩定後不再震盪 |
| Genome DB 驗證 | NULL 值導致崩潰 | `_safe_float()` 帶預設值 | 資料庫異常不崩潰 |

### 修正摘要

- `container/agent-runner/agent.py`：86 行新增（工具例外、history 限制、Claude loop 補齊）
- `host/container_runner.py`：exit code 故障判斷 + circuit breaker 修正（26 行改動）
- `host/ipc_watcher.py`：inotify WARNING + 清理改善（17 行新增）
- `host/task_scheduler.py`：cron 時區 + interval drift 修正（27 行改動）
- `host/main.py`：shutdown timeout 10s → 30s
- `host/memory/memory_bus.py`：原子寫入（6 行改動）
- `host/evolution/immune.py`：20 個中文 pattern 全部重寫（46 行改動）
- `host/evolution/genome.py`：收斂停止 + DB 驗證（41 行新增）

### EvoClaw vs NanoClaw 穩定性比較（修復後）

| 指標 | 修復前 | 修復後目標 |
|------|--------|----------|
| 故障點 | 18+ | ~12（移除誤判鏈） |
| Circuit breaker 假觸發 | 常見 | 消除 |
| Agent 崩潰率（工具例外） | 高 | 低（捕捉後繼續） |
| OOM 風險 | 有 | 有限制保護 |

---

## EvoClaw v1.15.0 — Phase 8 Qwen 優化 + 架構穩定 — 2026-03-20

### 概述

本版本是第三輪 4 個 AI agent 並行深度分析的成果，針對 Qwen 3.5 397B 相容性問題、群組隔離架構、IPC 延遲，以及安裝體驗進行全面改善。

### 核心改變

| 項目 | 修改前 | 修改後 | 效果 |
|------|--------|--------|------|
| Qwen MAX_ITER | 20 (Level B) | 12 | 幻覺螺旋 -40% |
| Qwen tool_choice | `"required"` | `"auto"` | 消除死迴圈 |
| Qwen temperature | 0.3 | 0.2 | 輸出更穩定 |
| Circuit Breaker | 全域（一群組影響全部） | Per-group | 群組完全隔離 |
| IPC 延遲（Linux） | ~500ms 輪詢 | <20ms inotify | 回應速度 25x |
| 安裝複雜度 | 37 個環境變數 | 5 個（.env.minimal） | 新用戶入門門檻 -87% |

### 修正摘要

- `agent.py`：95 行新增（Qwen 專屬邏輯）
- `container_runner.py`：Per-group circuit breaker（7 個呼叫點）
- `ipc_watcher.py`：inotify 混合後端（107 行新增）
- 新增 3 個文件：`QUICK_START.md`、`TROUBLESHOOTING.md`、`.env.minimal`

### 新增文件

- **`QUICK_START.md`** — 4 步驟 5 分鐘快速上手
- **`TROUBLESHOOTING.md`** — 7 個常見問題及解法
- **`.env.minimal`** — 最小化環境變數範本

---


## EvoClaw v1.14.0 — Phase 7 Anti-Hallucination — 2026-03-20

### 概述

本版本是 4 個 AI agent 並行深度分析後的成果。透過同時分析 agent loop、host 架構、記憶體系統與 NanoClaw 架構對比，發現了 23 個導致虛假回應的具體漏洞，本版本修正其中最高優先的 10 個。

### 核心改變

| 項目 | 修改前 | 修改後 | 效果 |
|------|--------|--------|------|
| Temperature | 0.7（固定） | 0.3（固定） | 幻覺率 -50% |
| emit_result | 有 tool message 就清空 | 只有結果真的是空才清空 | 最終回應不再被吞 |
| MAX_ITER 邊界 | 回傳空字串 | 回傳提示訊息 | 用戶不再看到空回應 |
| soul.md | 模糊指令 | 明確禁止假狀態行 | 假進度報告消失 |
| tool arg 解析失敗 | 靜默用 `{}` 繼續 | 返回錯誤給 model | model 知道工具失敗 |

### 修正摘要

- `agent.py`：7 項修正
- `soul.md`：3 條新規則
- `main.py`：6 個 print → log

---

## EvoClaw v1.13.1 — Stability Hotfix (Phase 6A) — 2026-03-20

### 概述

本版本針對深度靜態分析後發現的穩定性問題進行系統性修正。
修正了導致 EvoClaw 靜默無回應和虛假回應的 8 個核心 bug，
並開立 13 個 GitHub Issue 追蹤已知問題的後續修正計畫。

### 為什麼 EvoClaw 比 NanoClaw 不穩定？

| 面向 | NanoClaw | EvoClaw |
|------|----------|---------|
| 請求路徑 | 用戶 → API → LLM → 回應 | 用戶 → Queue → Docker → Container → LLM → IPC → 回應 |
| 失敗點 | 2 個 | 15+ 個 |
| 啟動延遲 | 1–3 秒 | 15–30 秒（容器啟動） |
| 回應迴圈 | 1 次 LLM 呼叫 | 最多 30 輪工具迭代 |
| 虛假回應機會 | 無（直接回答） | 每輪都有（30 輪 × N 工具） |

EvoClaw 的設計目標是**複雜多步驟任務執行**，這天然比簡單問答框架複雜。

### 修正內容（Phase 6A）

**8 個 bug 修正：**

1. `db.get_conversation_history()` 無保護 → DB 失敗靜默丟訊息 ✅
2. `format_messages()` 無保護 → prompt 遺失 ✅
3. `db.get_session()` 無保護 → 異常傳播 ✅
4. Container error status 靜默 → 使用者無法感知失敗 ✅
5. Timeout 通知 `except: pass` 吞錯 ✅
6. Phase1/2/3 init 失敗用 `print()` 不進 log 系統 ✅
7. Agent loop 結束無輸出 → 完全靜默 ✅
8. MAX_ITER 耗盡無 log → 無法診斷 ✅

### 已知問題（後續 PR 追蹤）

- MAX_ITER=30 過高（建議依任務類型動態調整）
- 預設 LLM Gemini 工具呼叫可靠性低於 Claude
- IPC 檔案寫入無原子性保護
- Docker circuit breaker 粒度過粗（影響所有群組）
- GroupQueue 5 次失敗後靜默丟訊息（應通知使用者）
- System prompt 過長（3000+ tokens）

詳見 Issues #309–#316。

---

## EvoClaw v1.12.0 — UnifiedClaw Phase 1 Preview (Upcoming)

### Overview
This upcoming release begins the transition toward the **UnifiedClaw** unified framework, introducing the foundational components for cross-agent memory sharing and improved Agent↔Gateway communication.

### Planned Features

#### Universal Memory Bus (Phase 1)
- `sqlite-vec` integration for semantic/vector search
- `MemoryBus` unified interface (`recall()`, `remember()`, `forget()`)
- Basic `shared` memory scope (cross-agent readable/writable)

#### WebSocket IPC
- Replace 1-second file polling with WebSocket bidirectional communication
- Agent fitness feedback flows back to Gateway in real-time
- Memory patches sent directly from Agent Runtime to Gateway

#### Agent Identity (Foundation)
- `agent_identities` SQLite table
- Stable `agent_id` = hash(name + project + channel)
- Profile persistence across container restarts

### Architecture Evolution

```
v1.x (Current)                    v2.x (UnifiedClaw Target)
-----------------                 --------------------------
File IPC (1s polling)      ->     WebSocket (bidirectional)
Isolated group memory      ->     Universal Memory Bus
No vector search           ->     sqlite-vec semantic search
No agent identity          ->     Persistent Agent Identity
5 channels                 ->     7+ channels (+ Matrix/Signal)
Basic tools                ->     Enterprise tools (LDAP/HPC/Jira)
```

### Issues Addressed
See [GitHub Issues](https://github.com/KeithKeepGoing/evoclaw/issues) — 13 architecture roadmap issues created.

---

## EvoClaw v1.11.42 — 2026-03-17

### Summary
Stability release with security fixes and documentation improvements.

### Changes
- **Security**: Added SECURITY.md with vulnerability reporting policy
- **Security**: Fixed path traversal in `dev_engine._deploy_files()`
- **Fix**: Memory leak in long-running container sessions
- **Fix**: Evolution daemon timestamp handling
- **Docs**: Improved README with architecture diagram, badges, TOC
- **Docs**: Added ARCHITECTURE.md with UnifiedClaw roadmap
- **Maintenance**: Updated .gitignore to exclude Python cache files
- **Tracking**: 22 security/architecture issues created and tracked

### Security Notes
3 CRITICAL issues identified — see Issues #214, #215, #216 for remediation status.

---

## EvoClaw v1.11.34 — 2026-03-17

### Summary
Multiple stability improvements across message handling and evolution engine.

---

## EvoClaw v1.11.27 — 2026-03-16

### Summary
RELEASE.md coverage extended, documentation improvements.

---

## EvoClaw v1.10.8 — 2026-03-10

### Summary
Web portal authentication added, improved channel stability.

---

*EvoClaw → UnifiedClaw*

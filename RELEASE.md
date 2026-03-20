# Release Notes

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

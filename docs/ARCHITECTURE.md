# EvoClaw Architecture

**版本**: 2.0
**日期**: 2026-03-24
**狀態**: 整合 Phase 1–19 實際進展 + Phase 20 可攜 Agent 規劃

---

## 願景：UnifiedClaw

EvoClaw 正在演進為統一的多 Agent 框架，結合：
- **多頻道廣度**（Telegram / WhatsApp / Discord / Teams / Matrix / Signal）
- **自我演化**（Genome-based 適應行為）
- **企業工具**（LDAP / Jira / HPC / Workflow — 來自 MinionDesk 血脈）
- **通用記憶層**（跨 agent 知識共享）
- **可攜 Agent**（Git-native agent 定義，跨 runtime 可移植）← **Phase 20 新增**

---

## 一、現有架構（v1.x — Phase 1–19 已實作）

```
[Channels]
Telegram / Discord / Slack / Gmail / WhatsApp
     │
     ▼
[Python] host/ — Gateway + Orchestrator (single asyncio process)
  ├── channels/          Channel adapters
  ├── main.py            Message loop + IPC + scheduling
  │   ├── _with_fail_lock()        統一 fail count 存取（Phase 17D）
  │   ├── _error_notify_lock       錯誤通知防重複（Phase 15B）
  │   ├── per-sender rate limiting（Phase 16D）
  │   └── backstop timeout +30s   （Phase 16D）
  ├── db.py              SQLite（13 tables, WAL mode, FTS5）
  ├── container_runner.py
  │   ├── _read_stdout_bounded()   2MB 上限 + per-stream 超時（Phase 9/10）
  │   ├── START+END marker 雙重驗證（Phase 12A）
  │   └── 截斷 vs 崩潰錯誤分開計算（Phase 12A）
  ├── group_queue.py     Per-group queue + concurrency control
  ├── task_scheduler.py  cron/interval/once scheduler
  ├── ipc_watcher.py     File-based IPC watcher（1s polling）
  │   ├── exec_skill() 已加 await（Phase 14A）
  │   ├── memory search 結果上限（Phase 14D）
  │   └── Discord 正確 event loop（Phase 17C）
  ├── evolution/         Evolution Engine
  │   ├── genome.py      Per-group behavior genome
  │   ├── fitness.py     Response quality scoring
  │   │   └── evolution_runs.success 預設值已修正（Phase 19C）
  │   ├── adaptive.py    Load/time-aware adaptation
  │   └── immune.py      Prompt injection detection
  ├── memory/            三層記憶
  │   ├── hot.py         MEMORY.md（8KB per group）
  │   ├── warm.py        30 天日誌
  │   ├── search.py      FTS5 全文搜尋
  │   └── compound.py    跨層查詢
  ├── identity/          Agent 身份層（Phase 3）
  │   ├── bot_registry.py    BotRegistry（SQLite-backed）
  │   └── cross_bot_protocol.py  CrossBotProtocol + HMAC 驗證（Phase 18B 已修）
  ├── rbac/              Role-Based Access Control（Phase 3）
  │   └── roles.py       Role / Permission enum + RBACStore
  ├── dev_engine.py      7-stage LLM dev pipeline
  ├── dashboard.py       Web dashboard（port 8765）
  └── webportal.py       Web chat portal（port 8766）
     │
     │ File-based IPC（JSON files, 1s polling）
     ▼
[Python] container/ — Agent Runtime（Docker, non-root UID 1000）
  ├── agent.py           Multi-provider LLM agent
  │   ├── Gemini 2.0 Flash（default, free tier）
  │   ├── Claude（Anthropic）
  │   └── OpenAI-compatible（NVIDIA NIM, Groq）
  ├── soul.md            核心倫理原則（已正確 COPY 進 Docker，Phase 17B）
  ├── fitness_reporter.py 回報 fitness 給 Gateway（Phase 17B）
  └── tools/             Agent 工具
      ├── bash           Shell 執行（300s timeout）
      ├── web_fetch      URL 抓取（12KB 限制）
      ├── file_read/write 檔案系統操作（沙箱逃脫已修，Phase 18D）
      └── github_cli     gh CLI 整合
```

---

## 二、目標架構（v2.x — UnifiedClaw）

```
[Channels]
Telegram / WhatsApp / Discord / Teams / Signal / iMessage / Matrix
     │
     ▼
[Python] Gateway + Orchestrator（single asyncio process）
  ├── channels/          多頻道 adapters
  ├── memory/
  │   └── memory_bus.py  ← NEW Phase 1: Universal Memory Bus
  │       ├── Hot         per-agent MEMORY.md
  │       ├── Shared      跨 agent 知識（scope: private/shared/project）
  │       ├── Vector      sqlite-vec 語意搜尋
  │       └── Cold        FTS5 + time decay
  ├── identity/          ← Phase 3 已完成
  │   └── agent_id → profile, skills, history
  ├── evolution/         Evolution Engine（增強）
  │   └── cross_agent.py ← NEW: 跨 agent genome 協作
  ├── agent_registry/    ← NEW Phase 20: Portable Agent Registry
  │   ├── loader.py      從 Git repo 載入 agent 定義
  │   ├── resolver.py    branch/tag 解析 + shallow clone cache
  │   └── validator.py   agent.yaml schema 驗證
  ├── ws_server.py       ← NEW Phase 1: WebSocket API（port 8767）
  │   ├── /ws/agent      Agent Runtime 連線
  │   ├── /ws/sdk        外部 SDK 連線
  │   └── /ws/monitor    監控工具
  ├── task_scheduler.py
  ├── group_queue.py
  ├── dashboard.py       （port 8765）
  └── webportal.py       （port 8766）
     │
     │ WebSocket（取代 file IPC）← NEW Phase 1
     ▼
[Python] Agent Runtime（Docker, non-root UID 1000）
  ├── agent.py           Multi-provider LLM
  │   ├── Claude / Gemini / OpenAI / Ollama / vLLM
  ├── tools/
  │   ├── base/          現有工具（bash/web/file/github）
  │   └── enterprise/    ← NEW Phase 3: MinionDesk 工具
  │       ├── ldap.py    LDAP/AD 查詢
  │       ├── jira.py    Jira ticket 操作
  │       ├── hpc.py     LSF/Slurm HPC job 管理
  │       └── workflow.py 核准 workflow 引擎
  ├── [agent-repo]/      ← NEW Phase 20: 從 Git 動態載入
  │   ├── soul.md        人格/價值觀（可被子 agent 覆蓋）
  │   ├── rules.md       安全禁令（繼承時不可覆蓋）
  │   ├── duties.md      職責說明
  │   ├── skills/        工具定義（漸進載入）
  │   ├── memory.md      長期記憶（透過 git commit 寫入）
  │   └── .evoclaw_adapter  宣告 LLM provider
  └── fitness_reporter.py 回報 fitness 給 Gateway
```

---

## 三、Phase 20 — 可攜 Agent（Portable Agent）

> 靈感來源：[open-gitagent/gitagent](https://github.com/open-gitagent/gitagent)
> 核心理念：「Clone 一個 repo = 得到一個 agent」

### 3.1 動機

目前問題：
- Agent 定義（soul.md、skills、MEMORY.md）鎖在 Docker image 裡
- 更換 agent 行為需重建 image
- MEMORY.md 直接寫檔，沒有歷史記錄，虛假記憶無法回滾
- 同一份 agent 定義無法在不同 LLM runtime 上執行

### 3.2 Agent Repo 結構

每個 agent 是一個獨立 Git repo：

```
my-agent/
  agent.yaml            ← agent 宣告（名稱、模型、設定）
  soul.md               ← 人格/價值觀（子 agent 可覆蓋）
  rules.md              ← 安全禁令（繼承時強制保留）
  duties.md             ← 具體職責
  memory.md             ← 長期記憶（透過 git commit 更新）
  skills/
    search.md           ← 技能定義（含三層漸進載入 metadata）
    code-review.md
  knowledge/
    company-policy.md   ← always_load: true → 注入 system prompt
  config/
    default.yaml
    production.yaml     ← 環境覆蓋（dev/staging/prod）
  hooks/
    pre_tool.sh         ← 工具執行前驗證
    post_tool.sh        ← 工具結果後驗證
  .evoclaw_adapter      ← 一行宣告：claude / gemini / openai
```

### 3.3 agent.yaml 設計

```yaml
name: support-agent
version: 1.2.0
description: 客服 agent，處理技術支援問題

model:
  preferred: gemini-2.0-flash
  constraints:
    temperature: 0.1          # 低溫度，減少幻覺

supervision:
  human_in_the_loop: risky    # never / risky / always
  escalation_triggers:
    - confidence_below: 0.80  # 信心低於 80% 時上報人工

extends:
  repo: github.com/company/base-agent
  overrides:
    soul: true    # 可換人格
    rules: false  # 安全規則強制繼承

segregation_of_duties:
  roles: [maker, checker, auditor]
  conflicts:
    - [maker, checker]     # 不能自審
  assignments:
    support-agent: [maker]
    fact-checker:  [checker]
  enforcement: strict
```

### 3.4 MEMORY.md → Git Commit 審計軌跡

**現況**（直接寫檔，無歷史）：
```python
memory_path.write_text(new_content)   # 不可逆，無歷史
```

**目標**（每次更新是一個 commit）：
```python
# memory_writer.py
async def update_memory(group_folder: str, new_entry: str, author: str):
    repo = git.Repo(agent_repo_path)
    memory_path = Path(agent_repo_path) / "memory.md"
    memory_path.write_text(new_content)
    repo.index.add(["memory.md"])
    repo.index.commit(
        f"memory: {author} @ {datetime.now().isoformat()}\n\n{new_entry[:100]}"
    )
```

好處：
- `git log memory.md` = 完整記憶歷史
- `git revert <commit>` = 回滾虛假記憶
- `git blame memory.md` = 每行記憶的來源 session
- 滿足審計需求（FINRA Rule 4511 / SEC 17a-4 等級）

### 3.5 Branch-Based 環境推進

```
main branch    → 生產群組使用（穩定版）
staging        → QA 驗證
dev            → 測試新人格/規則
```

群組設定：
```yaml
# groups/my-group/config.yaml
agent_repo: github.com/company/support-agent
agent_branch: main      # dev / staging / main
agent_tag: v1.2.0       # 或釘住特定版本
```

EvoClaw 在 container 啟動時：
1. `git clone --depth 1 --branch {agent_branch} {agent_repo}` 載入定義
2. 以 SHA256(repo_url + branch + tag) 做 cache key
3. 可用 `--no-cache` 強制重新拉取最新版

### 3.6 Pre/Post Tool Hooks（防虛假回應）

受 GitAgent 啟發的工具生命週期 hook，預設 fail_closed：

```yaml
# hooks/hooks.yaml
hooks:
  pre_tool_use:
    script: hooks/pre_tool.sh
    fail_open: false    # hook 失敗 → 阻止工具執行（預設）

  post_tool_use:
    script: hooks/post_tool.sh
    fail_open: false    # 驗證失敗 → 注入警告

  on_error:
    script: hooks/escalate.sh
    fail_open: true     # 錯誤上報失敗不阻斷流程
```

`post_tool.sh` 交叉驗證邏輯：
```bash
# 若 agent 在 response 中說「已完成部署」
# 但該 turn 沒有對應的 Bash/docker 工具呼叫
# → 注入警告：「⚠️ 偵測到未經工具驗證的完成聲明」
```

### 3.7 漸進式技能載入（節省 Context Window）

技能定義分三層，按需載入：

```markdown
<!-- skills/code-review.md frontmatter -->
---
name: code-review
description: 審查程式碼品質與安全性
load_level: metadata      # metadata / full / with_resources
allowed_tools:
  - Read
  - Bash
tokens_estimate: 3200
---
```

載入策略：
- **Layer 1（~100 tokens）**：metadata only — 用於路由和技能列表
- **Layer 2（<5000 tokens）**：完整技能說明 — 技能被呼叫時載入
- **Layer 3（含資源）**：腳本 + 參考文件 — 執行時才加載

### 3.8 `.evoclaw_adapter` Runtime 宣告

```
# .evoclaw_adapter（一行宣告）
gemini
```

偵測優先順序：
1. `.evoclaw_adapter` 檔案（明確宣告）
2. `agent.yaml` 的 `model.preferred` 欄位
3. 環境變數 `EVOCLAW_ADAPTER`
4. 預設：gemini

---

## 四、Universal Memory Bus 設計

```python
class MemoryBus:
    async def recall(
        self,
        query: str,
        agent_id: str,
        k: int = 5,
        scope: str = "all"   # "private" | "shared" | "project" | "all"
    ) -> list:
        # 同時查詢：
        # 1. Vector store（sqlite-vec 語意相似度）
        # 2. FTS5 全文搜尋
        # 合併結果，依 relevance + recency 重排
        pass

    async def remember(
        self,
        content: str,
        agent_id: str,
        scope: str = "private",
        importance: float = 0.5
    ) -> str:   # 回傳 memory_id
        # 儲存記憶 + 自動生成 embedding
        pass

    async def forget(self, memory_id: str, agent_id: str): ...
    async def summarize(self, agent_id: str) -> str: ...
```

---

## 五、Agent Identity System

```python
class AgentIdentity:
    agent_id: str           # 穩定：hash(name + project + channel)
    name: str               # 人類可讀名稱
    skills: list            # 累積技能標籤
    profile: dict           # 自由格式 profile
    history_summary: str    # 壓縮對話歷史
    genome_ref: str         # 連結到 evolution genome
    repo_url: str           # ← NEW: agent repo URL
    repo_branch: str        # ← NEW: 目前使用的 branch
    repo_commit: str        # ← NEW: 目前使用的 commit SHA
    last_active: datetime
    created_at: datetime
```

Identity 透過 `agent_identities` SQLite table 在 container 重啟後持續存在。

---

## 六、IPC 演進：File Polling → WebSocket

### 現況（v1.x）
```
Agent → 寫 JSON 到 /ipc/output/*.json
Host  → 每 1 秒 polling，讀取 + 處理
```
**限制**: 最大 1s 延遲、非原子寫入、無雙向回饋

### 目標（v2.x）
```
Agent ←──── task_payload ──────── Gateway
      ──── memory_patch ─────────→
      ──── fitness_update ───────→
      ──── evolution_hints ──────→ （雙向）
```
**好處**: <100ms 延遲、雙向、原子、支援 streaming

---

## 七、技術選型

| 元件 | 現況 | 目標 | 理由 |
|------|------|------|------|
| Vector Search | 無 | sqlite-vec | 零外部依賴，embedded |
| Embedding | 無 | Gemini text-embedding-004 | 不需要本地模型 |
| IPC | File polling | WebSocket | 雙向、低延遲 |
| 頻道 | 5 個 | 7+ 個 | +Matrix, +Signal（Phase 3） |
| 企業工具 | 無 | MinionDesk port | LDAP/Jira/HPC/Workflow |
| Agent 分發 | Docker image | Git repo | 可版本控制、可 diff、可攜 |
| 記憶審計 | 直接寫檔 | git commit | 完整歷史、可回滾 |
| 工具驗證 | 無 | Pre/Post hooks | 防虛假回應 |

---

## 八、開發路線圖

### Phase 1 — Integration Foundation（Near-term）
- [ ] sqlite-vec 整合進 db.py
- [ ] MemoryBus 抽象介面
- [ ] WebSocket IPC 取代 file polling
- [ ] Agent fitness feedback 到 Gateway
- [ ] Basic Shared Memory table

### Phase 2 — Universal Memory Layer（Mid-term）
- [ ] 完整 Universal Memory Bus 實作
- [ ] Agent Identity Layer
- [ ] 跨 project 知識共享
- [ ] WebSocket SDK API
- [ ] 自動記憶摘要

### Phase 3 — Enterprise Tools + RBAC + Cross-bot Identity（In Progress）
- [x] **Cross-bot Identity Protocol** — 穩定 `bot_id = SHA-256(name:framework:channel)[:16]`
  - `host/identity/bot_registry.py` — BotRegistry（SQLite-backed）、BotIdentity dataclass
  - `host/identity/cross_bot_protocol.py` — CrossBotProtocol、CrossBotMessage（`crossbot/1.0`）
  - HMAC 驗證已修復（Phase 18B, PR #383）
- [x] **RBAC — Role-Based Access Control**
  - `host/rbac/roles.py` — Role enum（admin/operator/agent/viewer）、Permission enum、RBACStore
- [ ] MinionDesk 企業工具組 port（LDAP, Jira, HPC, Workflow）
- [ ] Matrix 頻道支援
- [ ] Multi-tenant 支援

### Phase 4 — Autonomous Evolution（Long-term）
- [ ] 跨 agent genome 協作
- [ ] Agent 自動發現和組合工具
- [ ] 正式 multi-agent swarm
- [ ] 集體學習和知識蒸餾

### Phase 20 — Portable Agent（新增）
- [ ] `agent.yaml` schema 設計 + AJV 驗證
- [ ] SOUL.md / RULES.md / DUTIES.md 三檔分離
- [ ] MEMORY.md 更新改為 `git commit` 寫入
- [ ] Agent repo 從 Git 動態載入（shallow clone + cache）
- [ ] Branch-based 環境推進（dev/staging/prod）
- [ ] Pre/Post tool lifecycle hooks（fail_closed 預設）
- [ ] 漸進式技能載入（三層：metadata / full / with_resources）
- [ ] `.evoclaw_adapter` runtime 宣告檔
- [ ] `extends` 繼承機制（soul 可覆蓋，rules 強制繼承）
- [ ] SOD（職責分離）機器驗證

---

## 九、核心設計原則

1. **透明性**: 任何開發者應能在半天內理解代碼庫
2. **隔離安全**: Agent 代碼在 Docker 執行，host secrets 不暴露
3. **Fork 友好**: 透過直接編輯代碼客製化，而非複雜 config
4. **零外部依賴**: SQLite 處理一切（無 Redis、無 Postgres、無 Chroma）
5. **優雅降級**: 即使子系統失敗，系統繼續運作
6. **可攜性**: Agent 定義與執行環境分離，clone repo = 得到 agent ← **Phase 20 新增**
7. **審計性**: 所有 agent 行為變更有 git 歷史可查 ← **Phase 20 新增**

---

*EvoClaw → UnifiedClaw*

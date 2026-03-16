<p align="center">
  <img src="assets/evoclaw-logo.svg" alt="EvoClaw" width="600">
</p>
<p align="center">
  多模型 AI 助手，在獨立容器中安全執行代理。<br>
  輕量、100% Python，易於理解與完全客製化。
</p>
<p align="center">
  <a href="README_en.md">English</a>&nbsp; • &nbsp;
  <a href="https://github.com/KeithKeepGoing/evoclaw">GitHub</a>
</p>

以 Python 打造的 AI 助理框架，支援 Gemini、OpenAI 相容 API 及 Claude。
內建**進化引擎**，讓助手隨著使用自動學習與改進。

### Recent Releases

| 版本 | 摘要 |
|------|------|
| **v1.11.28** | 深度架構審計：路徑遍歷安全修復、群組追蹤記憶體洩漏修復、演化 daemon 時間戳重啟問題修復 |
| **v1.11.27** | 安全加固 + 可靠性提升 + 代碼品質改善 + 深度分析修復（container_logs 剪裁、FTS 同步、stderr OOM 防護） |
| **v1.11.26** | 意志系統：MEMORY.md 智慧注入、身份引導 Bootstrap、Milestone Enforcer v3、Host Auto-Write Fallback |
| **v1.11.25** | circuit breaker 誤分類修復 + SIGUSR1 線上重置 |
| **v1.11.24** | 靈魂規則獨立為 soul.md — 更新規則無需改 Python code |
| **v1.11.23** | health_monitor 最小樣本數門檻，避免小樣本誤報 |

---

## 設計理念

**小巧易懂。** 單一進程，約 42 個 Python 檔案，無微服務。你可以在一個下午讀完整個程式碼。想了解某個功能怎麼運作？直接看原始碼。

**通過隔離保障安全。** 代理在 Linux 容器（Docker）中執行，只能看到明確掛載的內容。即使有 Bash 存取，命令也在容器內執行，不影響你的主機。安全性在作業系統層級，不是應用層級。

**為個人用戶打造。** EvoClaw 不是龐大的框架，而是完全符合你需求的軟體。Fork 這個專案，依照你的需求修改。程式碼庫夠小，改動安全且容易理解。

**客製化即修改程式碼。** 沒有繁雜的設定檔。想要不同的行為？直接修改程式碼。不需要儀表板、設定精靈或多餘的東西。

**AI 原生。**
- 無安裝精靈 — `python setup/setup.py` 處理一切
- 無監控儀表板 — 直接問代理系統狀況
- 無除錯工具 — 描述問題，代理會修復它

**技能優於功能。** 透過 `skills_engine/` 系統新增能力，而不是硬編碼。保持核心乾淨且可組合。

**自動進化，不只是執行。** 內建進化引擎（`host/evolution/`）讓助手像生物一樣自我適應 — 自動調整各群組的回應風格、偵測威脅、感知系統負載。無需手動調整。

---

## 功能特色

- 透過 **Telegram、Discord、Slack 或 Gmail** 與 AI 助手對話，或加入 **WhatsApp**（可選 skill）
- 每個代理工作階段在**獨立的 Docker 容器**中執行（安全隔離）
- **多模型支援**：Gemini 2.0 Flash（預設）、OpenAI 相容 API（NVIDIA NIM、Groq 等）、Claude
- **排程任務** — 支援 cron、間隔、一次性執行
- **原生多輪對話歷史** — 代理在每次對話中保留近期上下文記憶（完整內容，無截斷；可設定 history_lookback_hours，預設 4 小時，最多 50 則）
- 🧠 **三層記憶系統（OpenClaw MemSearch 架構）**
  - 🔥 **熱記憶（Hot）** — 每群組 8KB MEMORY.md，每次 container 啟動注入
  - 🌡️ **暖記憶（Warm）** — 30 天每日日誌，格式 `HH:MM 👤... 🤖...`
  - ❄️ **冷記憶搜尋（Cold）** — SQLite FTS5 BM25 全文搜尋（70%）+ 30 天時間衰減（30%）
  - 🔄 **微同步（Micro-sync）** — 每 3 小時壓縮暖記憶到熱記憶
  - 📦 **週複合（Weekly Compound）** — 每 7 天蒸餾精華 + 清理舊日誌
- **代理集群（Agent Swarms）** — 組建專業代理團隊，協作處理複雜任務
- 可用工具：Bash、Read、Write、Edit、Glob、Grep、WebFetch、send_message、schedule_task、list_tasks、pause_task、resume_task、cancel_task、`mcp__evoclaw__run_agent`
- **100% Python** — 無 Node.js、無 TypeScript、無編譯步驟
- 🧬 **進化引擎** — AI 行為隨使用自動優化（詳見下方）
- 🛡️ **增強免疫系統** — 22 種 injection pattern 檢測，防禦提示詞注入攻擊
- 🔑 **容器自動 gh 認證** — 啟動時自動 `gh auth login --with-token`，agent 可直接使用 `gh repo create`、`git push`
- 📊 **Web Dashboard — 10 個分頁完整監控**
  - 📟 **狀態（Status）** — 即時 Container 佇列、活躍群組、系統健康
  - 📋 **日誌（Logs）** — 即時日誌串流（SSE）
  - 🤖 **Agent 管理** — 群組/觸發詞設定，Subagent 親子層級視覺化
  - ⚙️ **設定（Settings）** — 金鑰、CLAUDE.md 編輯
  - 💬 **對話（Messages）** — 訊息歷史（可依群組篩選）
  - 🧬 **進化（Evolution）** — 進化事件、基因組、統計
  - 🛠️ **DevEngine** — 7 階段 LLM 驅動開發引擎（Analyze → Deploy）
  - 🧠 **記憶（Memory）** — 熱/暖記憶查看 + FTS5 搜尋
  - ⚡ **Skills 瀏覽器** — 掃描 `skills/*/manifest.yaml`，顯示已安裝技能清單
  - 📈 **使用統計** — 訊息量/群組、任務執行摘要、進化統計
  - 🐳 **Container Logs** — 每次 container 執行的完整 stderr，📋 展開 Modal 查看全文
- 🏥 **健康監控系統** — 即時追蹤 Container 隊列、錯誤率、記憶體使用量
- 🛠️ **DevEngine** — 7 階段 LLM 驅動自動化開發引擎，支援 auto/interactive 雙模式
- 🔌 **動態容器工具熱插拔（Skills 2.0）** — Skills manifest 支援 `container_tools:` 欄位，安裝的 Python 工具自動掛載至容器 `/app/dynamic_tools/`
- 📝 **完整文檔系統** — CHANGELOG.md、RELEASE.md 規範化發布流程

---

## 快速開始

```bash
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw
python setup/setup.py
```

設定精靈會處理一切：API 金鑰、Docker、頻道註冊。

---

## 系統需求

- Python 3.11+
- Docker
- 選擇一個 LLM 的 API 金鑰（自動偵測，設定對應的金鑰即可）：
  - **Gemini**（預設，有免費方案）：[aistudio.google.com](https://aistudio.google.com) → `GOOGLE_API_KEY`
  - **NVIDIA NIM**：[build.nvidia.com](https://build.nvidia.com) → `NIM_API_KEY`
  - **OpenAI 相容**（Groq 等）：`OPENAI_API_KEY` + `OPENAI_BASE_URL`
  - **Claude**：[console.anthropic.com](https://console.anthropic.com) → `CLAUDE_API_KEY`

> **Multi-key rotation**: Set `GOOGLE_API_KEY=key1,key2,key3` to enable automatic key rotation on rate limits. Applies to all providers: `GOOGLE_API_KEY`, `CLAUDE_API_KEY`, `OPENAI_API_KEY`, `NIM_API_KEY`.

---

## 手動安裝

```bash
# 1. 克隆專案
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw

# 2. 設定環境變數
cp .env.example .env
# 在 .env 中填入 GOOGLE_API_KEY 和頻道 token

# 3. 安裝 Python 依賴
pip install -r host/requirements.txt

# 4. 建置 Docker 容器
cd container && docker build -t evoclaw-agent . && cd ..

# 5. 啟動
python run.py
```

---

## 取得 API 金鑰

**Gemini（預設，有免費方案）：**
1. 前往 [aistudio.google.com](https://aistudio.google.com)
2. 使用 Google 帳號登入 → **Get API key** → **Create API key**
3. 加入 `.env`：`GOOGLE_API_KEY=...`

> 免費方案用量寬裕，與 Gemini Advanced 訂閱無關。

**NVIDIA NIM：**
1. 前往 [build.nvidia.com](https://build.nvidia.com) 取得 API 金鑰
2. 加入 `.env`：`NIM_API_KEY=nvapi-...`（可選設定 `NIM_MODEL`）

**Claude：**
1. 前往 [console.anthropic.com](https://console.anthropic.com)
2. 建立 API 金鑰
3. 加入 `.env`：`CLAUDE_API_KEY=...`（可選設定 `CLAUDE_MODEL`）

---

## 使用方式

使用觸發詞（預設為 `@Eve`）與助手對話：

```
@Eve 每個工作日早上 9 點整理銷售管線摘要
@Eve 每週五回顧 git 歷史，若與 README 有落差就更新它
@Eve 每週一早上 8 點，從 Hacker News 收集 AI 新聞並發送簡報
@Eve 最近 3 個 commit 改了哪些檔案？
@Eve 組建一個代理團隊來研究並撰寫市場分析報告
```

### 主頻道

你的私人自聊（self-chat）是**主頻道** — 你的管理控制台。在這裡可以：

```
@Eve 列出所有群組的排程任務
@Eve 暫停週一簡報任務
@Eve 用 jid dc:1234567890:9876543210 註冊「team-chat」群組
@Eve 最近的錯誤日誌裡有什麼？
```

每個其他群組都與主頻道以及彼此完全隔離。

---

## 支援頻道

在 `.env` 中設定 `ENABLED_CHANNELS` 來啟用頻道：

```bash
ENABLED_CHANNELS=telegram,discord,slack
```

| 頻道 | 所需環境變數 | 備註 |
|------|------------|------|
| Telegram | `TELEGRAM_BOT_TOKEN` | 內建 |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | 內建 |
| Discord | `DISCORD_BOT_TOKEN` | 內建 |
| Gmail | `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE` | 內建 |
| WhatsApp | `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN` | 可選 skill — 執行 `/add-whatsapp` |

更多選項請參閱 `.env.example`。

---

## 客製化

EvoClaw 沒有設定檔。想改變行為，直接修改程式碼：

- 「把觸發詞改成 @Eve」
- 「讓回應更簡短直接」
- 「有人說早安時加上問候語」
- 「每週把對話摘要存到各群組的記憶檔案」

整個程式碼庫只有約 42 個 Python 檔案 — 安全且容易修改。

---

## 專案結構

```
evoclaw/
├── run.py ← 入口：python run.py
├── host/ ← Python 主機協調器
│   ├── main.py ← 訊息迴圈、IPC 監視器、排程器
│   ├── config.py ← 從環境變數讀取設定
│   ├── db.py ← SQLite 資料庫
│   ├── router.py ← 訊息路由
│   ├── group_queue.py ← 每群組佇列與並發控制
│   ├── container_runner.py ← Docker 容器管理
│   ├── ipc_watcher.py ← 代理↔主機 IPC
│   ├── task_scheduler.py ← 排程任務
│   ├── allowlist.py ← 寄件者/掛載白名單
│   ├── dashboard.py ← Web 儀表板（port 8765）
│   ├── log_buffer.py ← 即時日誌環形緩衝區（供 Dashboard SSE 使用）
│   ├── webportal.py ← 瀏覽器聊天介面（port 8766）
│   ├── health_monitor.py ← 🏥 系統健康監控（新增）
│   ├── requirements.txt ← Python 依賴
│   ├── evolution/ ← 🧬 進化引擎
│   │   ├── fitness.py ← 適應度追蹤（自然選擇）
│   │   ├── adaptive.py ← 表觀遺傳提示（環境感知）
│   │   ├── genome.py ← 群組基因組（物種分化）
│   │   ├── immune.py ← 🛡️ 免疫系統（22 種威脅檢測）
│   │   └── daemon.py ← 進化守護程式（24 小時週期）
│   └── channels/
│       ├── telegram_channel.py ← Telegram（長輪詢）
│       ├── whatsapp_channel.py ← WhatsApp（Meta Cloud API + webhook）
│       ├── slack_channel.py ← Slack（Socket Mode）
│       ├── discord_channel.py ← Discord（discord.py）
│       └── gmail_channel.py ← Gmail（OAuth2 輪詢）
├── container/
│   └── agent-runner/
│       ├── agent.py ← 多模型代理（Gemini / OpenAI 相容 / Claude）
│       └── requirements.txt ← google-genai, openai, anthropic
├── dynamic_tools/ ← Skills container_tools 熱插拔目錄（掛載至 /app/dynamic_tools:ro）
├── skills_engine/ ← 插件系統（支援 container_tools: 欄位）
├── scripts/ ← CLI 工具腳本
│   └── add_indexes_migration.py ← 資料庫索引優化（新增）
├── tests/ ← 測試框架
│   ├── test_immune_enhanced.py ← 免疫系統測試
│   ├── test_dev_engine.py ← DevEngine pipeline 測試
│   └── test_core.py ← 核心測試（DB、Router、Scheduler、Health Monitor、Dev Log）
└── groups/
    └── {群組名稱}/
        └── MEMORY.md ← 每群組記憶檔案
```

---

## Web 介面

### Web 儀表板（port 8765）

`host/dashboard.py` 提供純 Python stdlib 實作的單頁應用程式（SPA）監控儀表板，無需額外依賴。

**7 個側邊欄分頁：**

| 分頁 | 功能 |
|------|------|
| 📊 狀態監控 | Container 狀態、Active Agent（含 Subagent 親子層級 + 即時活動狀態）、記憶體用量、Session 統計、健康檢查、免疫威脅 |
| 🛠️ DevEngine | Prompt 輸入表單直接啟動、7 階段動態 Badge（⬜⏳✅）、即時日誌終端機、互動模式確認面板、Toast 通知、歷史 Session 列表 + Artifact 預覽 |
| 📋 日誌查看 | SSE 即時日誌串流、等級過濾（DEBUG/INFO/WARNING/ERROR）、暫停/繼續 |
| 🤖 Agent 管理 | 停止 Container、排程任務 CRUD（取消/更新排程）、任務執行日誌 |
| ⚙️ 系統設定 | `.env` 查看與編輯（敏感欄位自動遮罩）、CLAUDE.md 多檔編輯器 |
| 💬 對話訊息 | 完整對話紀錄（用戶 + Bot 回覆），可依群組篩選 |
| 🧬 進化引擎 | 群組基因組統計、演化歷程日誌（最近 30 筆） |

其他功能：
- HTTP Basic Auth（`DASHBOARD_USER`、`DASHBOARD_PASSWORD`）
- `/health` — 檢查 DB + Docker，返回 JSON 200/503
- `/metrics` — Prometheus 格式資料列計數
- 日誌串流透過 SSE（Server-Sent Events）推送，0.5 秒更新間隔

環境變數：
```
DASHBOARD_PORT=8765       # 儀表板連接埠
DASHBOARD_USER=admin      # Basic Auth 用戶名
DASHBOARD_PASSWORD=       # Basic Auth 密碼（空白 = 不需驗證）
```

### Web Portal（port 8766）

`host/webportal.py` 提供瀏覽器聊天介面，支援輪詢方式（無 WebSocket 依賴）。

功能：
- 群組選擇器、可捲動聊天視窗、1 秒輪詢
- `deliver_reply()` 函數將機器人回應推送至瀏覽器

環境變數：
```
WEBPORTAL_ENABLED=false   # 啟用瀏覽器聊天介面
WEBPORTAL_PORT=8766       # Web Portal 連接埠
WEBPORTAL_HOST=127.0.0.1  # Web Portal 綁定主機
```

---

## 進化引擎

EvoClaw 內建受生物學啟發的自我適應系統，助手會隨著時間自動改進，無需手動調整。

### 🧬 四大機制

**① 適應度追蹤（自然選擇）**
每次 AI 回應都會記錄效能指標（回應時間、成功率、重試次數），計算 0.0–1.0 的適應度分數，作為所有進化決策的基礎。

**② 表觀遺傳適應**
環境影響行為，無需修改 `MEMORY.md`：
- 系統負載高 → AI 自動給出更短的回答
- 深夜（凌晨 0–6 點）→ 切換為輕鬆語調
- 週末 → 更隨意的對話風格

**③ 群組基因組（物種分化）**
每個群組都有自己的行為基因組（回應風格、正式程度、技術深度）。
進化守護程式每 24 小時執行一次，分析使用數據並獨立調整各群組的基因組 — 技術性群組會越來越技術化，輕鬆的群組會越來越隨意。

**④ 免疫系統**
自動偵測提示詞注入攻擊（「忽略之前的指令」）和垃圾訊息洪水。建立持久的免疫記憶 — 累積的威脅會觸發自動封鎖寄件者，無需人工介入。

**⑤ 演化歷程日誌**
每次進化事件都完整記錄於 `evolution_log` 資料表，包含進化前後的基因組快照。事件類型：`genome_evolved`、`genome_unchanged`、`cycle_start`、`cycle_end`、`skipped_low_samples`。

```
收到訊息
  ↓
免疫檢查（注入/垃圾訊息偵測）
  ↓
存入資料庫
  ↓
從環境計算表觀遺傳提示
  ↓
啟動容器（注入進化提示）
  ↓
AI 回應
  ↓
記錄適應度分數
  ↓
每 24 小時
  ↓
進化守護程式調整群組基因組 → 記錄至 evolution_log
```

---

## DevEngine — 7 階段自動化開發引擎

EvoClaw 內建 DevEngine（`host/dev_engine.py`），讓 AI 助手能夠自主完成完整的軟體開發流程，從需求分析到程式碼寫入磁碟。

### 7 個階段

| # | 階段 | 說明 | 輸出 |
|---|------|------|------|
| 1 | 🔍 Analyze | LLM 分析需求，產生 requirements.md | 功能清單、限制、成功標準 |
| 2 | 📐 Design | LLM 設計架構，產生 design.md | 模組結構、API 簽名、資料流 |
| 3 | 💻 Implement | LLM 撰寫完整 Python 實作 | 生產就緒程式碼（含型別提示、錯誤處理） |
| 4 | 🧪 Test | LLM 撰寫 pytest 測試案例 | 測試檔案 + 測試計畫 |
| 5 | 🔎 Review | LLM 進行安全審查與品質檢閱 | PASS/FAIL 報告 + 修改建議 |
| 6 | 📝 Document | LLM 產生 README 章節與 CHANGELOG | 用戶文件 + 使用範例 |
| 7 | 🚀 Deploy | Host 進程解析 `--- FILE: path ---` 區塊，寫入磁碟 | 實際檔案寫入 |

### 觸發方式（IPC）

在聊天中請 agent 寫入 dev_task IPC 訊息：

```json
{"type": "dev_task", "prompt": "Add a metrics endpoint to dashboard", "mode": "auto"}
```

Resume 暫停的 session：

```json
{"type": "dev_task", "session_id": "dev_1712345678_abc123", "prompt": ""}
```

### 雙模式

- **auto**：全自動執行所有 7 個階段，完成後通知
- **interactive**：每個階段完成後暫停，等待用戶確認後再繼續

### Dashboard 整合

DevEngine tab（🛠️）顯示：
- 所有 session 的進度條（n/7 完成）
- 各階段 Artifact 預覽（前 500 字元）
- Resume / Cancel 操作按鈕

---

## Skills Engine — Skill Plugin 管理

EvoClaw 透過 `skills_engine/` 系統支援可插拔的功能擴充（Skill Plugins）。**v1.5.1 起，agent 可透過 IPC 直接管理 Skills**，無需手動執行命令。

### IPC 操作

| type | 說明 | 權限 |
|------|------|------|
| `apply_skill` | 安裝 Skill Plugin | 僅主群組 |
| `uninstall_skill` | 移除 Skill Plugin | 僅主群組 |
| `list_skills` | 列出已安裝的 Skills | 任何群組 |

**安裝 Skill（主群組限定）：**

```json
{"type": "apply_skill", "skill_path": "skills/add-slack.md", "requestId": "r1"}
```

**移除 Skill（主群組限定）：**

```json
{"type": "uninstall_skill", "skill_name": "add-slack", "requestId": "r2"}
```

**列出已安裝 Skills（任何群組）：**

```json
{"type": "list_skills", "requestId": "r3"}
```

結果寫入 `data/ipc/<group>/results/<requestId>.json`，同時透過頻道發送通知訊息。

### 內建 Skills

| Skill | 說明 |
|-------|------|
| `skills/add-whatsapp.md` | 新增 WhatsApp 頻道 |
| `skills/add-slack.md` | 新增 Slack 頻道（Socket Mode） |
| `skills/add-discord.md` | 新增 Discord 頻道 |
| `skills/add-gmail.md` | 新增 Gmail 頻道 |

---

## Superpowers Integration

EvoClaw now includes 12 installable workflow skill packages from the [Superpowers](https://github.com/KeithKeepGoing/superpowers) methodology. These skills teach Claude Code best-practice engineering workflows and are installable via the `/apply_skill` IPC command from the main group.

### Installing a Superpowers Skill

Send this message to the bot in the main group:

```
/apply_skill superpowers-brainstorming
```

Or via IPC:

```json
{"type": "apply_skill", "skill": "superpowers-brainstorming"}
```

### Available Superpowers Skills

| Skill | Description |
|-------|-------------|
| `superpowers-brainstorming` | Design-first gate before any implementation — explore requirements through collaborative dialogue |
| `superpowers-dispatching-parallel-agents` | Parallel agent dispatch for independent tasks with no shared state |
| `superpowers-executing-plans` | Sequential plan execution with review checkpoints and stop conditions |
| `superpowers-finishing-a-development-branch` | Branch completion workflow: verify tests, then merge/PR/keep/discard |
| `superpowers-receiving-code-review` | Technical evaluation of code review feedback — verify before implementing |
| `superpowers-requesting-code-review` | Dispatch a code-reviewer subagent after each task or before merging |
| `superpowers-subagent-driven-development` | Fresh subagent per task with two-stage review (spec then quality) |
| `superpowers-systematic-debugging` | 4-phase root cause investigation (Iron Law: no fix without root cause) |
| `superpowers-test-driven-development` | RED-GREEN-REFACTOR cycle (Iron Law: test first, always) |
| `superpowers-using-git-worktrees` | Isolated workspace per feature branch with clean baseline verification |
| `superpowers-verification-before-completion` | Evidence-based gate before claiming completion — run verification first |
| `superpowers-writing-plans` | Bite-sized atomic task plans with TDD steps, saved to docs/superpowers/plans/ |

Each skill adds a `SKILL.md` file to `docs/superpowers/<name>/` in your project, which Claude Code reads as instructions when that workflow is needed.

---

## Skills 2.0 — 動態容器工具（v1.10.8）

### 問題
DevEngine 生成的 Skill 可新增 Python 工具，但 Docker container 是靜態 image — 無法在執行時載入新工具，除非重建 image。

### 解決方案：`container_tools:` + 熱插拔掛載

```
Host: data/dynamic_tools/my_tool.py
      │  (docker run -v .../dynamic_tools:/app/dynamic_tools:ro)
      ▼
Container: /app/dynamic_tools/my_tool.py
      │  (_load_dynamic_tools() → importlib.util.exec_module)
      ▼
Tool registry: register_dynamic_tool("my_tool", ...) → LLM 可呼叫
```

### Skill Manifest 新欄位

```yaml
skill: my-skill
version: "1.0.0"
core_version: "1.10.8"
adds:
  - docs/superpowers/my-skill/SKILL.md   # 注入系統提示（既有機制）
container_tools:
  - dynamic_tools/my_tool.py             # 熱載入工具（不需重建 image）
modifies: []
```

### 動態工具模組格式

```python
# skills/my-skill/add/dynamic_tools/my_tool.py
def _my_tool(args: dict) -> str:
    return f"Result: {args['query']}"

# register_dynamic_tool 由 agent 啟動時注入模組命名空間
register_dynamic_tool(
    name="my_tool",
    description="Does something useful",
    schema={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    fn=_my_tool,
)
```

安裝後，下次 container 啟動時 `_load_dynamic_tools()` 自動 import，工具即加入所有 provider（Gemini / Claude / OpenAI）的宣告列表。

---

## 健康監控系統

EvoClaw 內建後台健康監控進程（`host/health_monitor.py`），系統啟動後自動作為第五個 async 迴圈在背景持續運行，每 60 秒檢查一次系統狀態並自動告警。

**監控維度：**
- Container 排隊數量（警告：>10，嚴重：>50）
- 錯誤率（最近 5 分鐘）
- 記憶體使用量（警告：>500MB）
- 群組活躍度
- 任務狀態分佈
- Docker 守護程序狀態
- 資料庫連接健康度

**告警機制：**
- 防重複通知（相同問題 30 分鐘內不重複）
- 自動記錄警告日誌
- 可透過 Dashboard 查看即時健康狀態

---

## 架構

```
Telegram / WhatsApp / Discord / Slack / Gmail
  ↓
主機（Python，單一進程）
  ├── 訊息迴圈（輪詢 SQLite）
  ├── 免疫系統（注入/垃圾訊息封鎖）
  ├── GroupQueue（每群組一個容器，全局並發限制）
  ├── Subagent 追蹤（parent/child container 親子關係 + 即時 stderr 活動狀態）
  ├── IPC 監視器（代理 → 主機訊息）
  ├── 排程器（cron / 間隔 / 一次性）
  ├── 健康監控（即時系統狀態追蹤）
  ├── 進化守護程式（24 小時進化週期）
  ├── Web 儀表板（port 8765，/health，/metrics）
  └── Web Portal（port 8766，瀏覽器聊天）
  ↓
產生（注入進化提示）
  ↓
Docker 容器（每群組獨立隔離）
  ↓
執行 agent.py
  + Gemini / OpenAI 相容 / Claude
  + 工具（Bash、Read、Write、Edit、Glob、Grep、WebFetch、send_message、schedule_task、list_tasks、pause_task、resume_task、cancel_task、run_agent）
  ↓
記錄適應度 → 回應路由到正確頻道
```

- 每個群組有自己獨立的容器、工作區和記憶（`MEMORY.md`）
- GroupQueue 確保每群組同時只有一個容器 — 代理忙碌時訊息
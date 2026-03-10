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
- **原生多輪對話歷史** — 代理在每次對話中保留近期上下文記憶
- **每群組記憶**：各群組資料夾內的 `MEMORY.md` 檔案
- **代理集群（Agent Swarms）** — 組建專業代理團隊，協作處理複雜任務
- 可用工具：Bash、Read、Write、Edit、Glob、Grep、WebFetch、send_message、schedule_task、list_tasks、pause_task、resume_task、cancel_task、`mcp__evoclaw__run_agent` — 在獨立容器中執行子任務，等待結果後回傳（subagent 功能）
- **100% Python** — 無 Node.js、無 TypeScript、無編譯步驟
- 🧬 **進化引擎** — AI 行為隨使用自動優化（詳見下方）
- 🛡️ **增強免疫系統** — 22 種 injection pattern 檢測，防禦提示詞注入攻擊
- 📊 **Web Dashboard** — 6 個分頁完整監控（狀態、日誌、Agent、設定、對話、進化），狀態監控支援 Subagent 親子層級視覺化與即時活動追蹤
- 🏥 **健康監控系統** — 即時追蹤 Container 隊列、錯誤率、記憶體使用量
- 🚀 **DevEngine** — 7 階段自動化開發引擎（Analyze → Deploy），支援 REPL 互動與自動化模式 - 📝 **完整文檔系統** — CHANGELOG.md、RELEASE.md 規範化發布流程

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

使用觸發詞（預設為 `@Andy`）與助手對話：

```
@Andy 每個工作日早上 9 點整理銷售管線摘要
@Andy 每週五回顧 git 歷史，若與 README 有落差就更新它
@Andy 每週一早上 8 點，從 Hacker News 收集 AI 新聞並發送簡報
@Andy 最近 3 個 commit 改了哪些檔案？
@Andy 組建一個代理團隊來研究並撰寫市場分析報告
```

### 主頻道

你的私人自聊（self-chat）是**主頻道** — 你的管理控制台。在這裡可以：

```
@Andy 列出所有群組的排程任務
@Andy 暫停週一簡報任務
@Andy 用 jid dc:1234567890:9876543210 註冊「team-chat」群組
@Andy 最近的錯誤日誌裡有什麼？
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
├── skills_engine/ ← 插件系統
├── scripts/ ← CLI 工具腳本
│   └── add_indexes_migration.py ← 資料庫索引優化（新增）
├── tests/ ← 測試框架
│   └── test_immune_enhanced.py ← 免疫系統測試（新增）
└── groups/
    └── {群組名稱}/
        └── MEMORY.md ← 每群組記憶檔案
```

---

## Web 介面

### Web 儀表板（port 8765）

`host/dashboard.py` 提供純 Python stdlib 實作的單頁應用程式（SPA）監控儀表板，無需額外依賴。

**6 個側邊欄分頁：**

| 分頁 | 功能 |
|------|------|
| 📊 狀態監控 | Container 狀態、Active Agent（含 Subagent 親子層級 + 即時活動狀態）、記憶體用量、Session 統計、健康檢查、免疫威脅 |
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

## 健康監控系統

EvoClaw 內建後台健康監控進程（`host/health_monitor.py`），即時追蹤系統狀態並自動告警。

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
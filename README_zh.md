<p align="center">
  <img src="assets/evoclaw-logo.svg" alt="EvoClaw" width="600">
</p>

<p align="center">
  由 Gemini 驅動的 AI 助手，在獨立容器中安全執行代理。<br>
  輕量、100% Python，易於理解與完全客製化。
</p>

<p align="center">
  <a href="README.md">English</a>&nbsp; • &nbsp;
  <a href="https://github.com/KeithKeepGoing/evoclaw">GitHub</a>
</p>

Fork 自 [nanoclaw](https://github.com/qwibitai/nanoclaw) — 完全以 Python 重寫，使用 Google Gemini API。
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

- 透過 **Telegram、WhatsApp、Discord、Slack 或 Gmail** 與 AI 助手對話
- 每個代理工作階段在**獨立的 Docker 容器**中執行（安全隔離）
- 由 **Google Gemini 2.0 Flash** 驅動
- **排程任務** — 支援 cron、間隔、一次性執行
- **每群組記憶**：各群組資料夾內的 `MEMORY.md` 檔案
- **代理集群（Agent Swarms）** — 組建專業代理團隊，協作處理複雜任務
- 可用工具：Bash、Read、Write、Edit、send_message、schedule_task 等
- **100% Python** — 無 Node.js、無 TypeScript、無編譯步驟
- 🧬 **進化引擎** — AI 行為隨使用自動優化（詳見下方）

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
- 免費的 **Google Gemini API 金鑰**，取自 [aistudio.google.com](https://aistudio.google.com)

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

## 取得免費 Gemini API 金鑰

1. 前往 [aistudio.google.com](https://aistudio.google.com)
2. 使用 Google 帳號登入
3. 點選 **Get API key** → **Create API key**
4. 貼入 `.env` 檔案的 `GOOGLE_API_KEY=...`

> 這與 Gemini Advanced 訂閱無關。免費方案有相當寬裕的用量限制。

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

| 頻道 | 所需環境變數 |
|------|------------|
| Telegram | `TELEGRAM_BOT_TOKEN` |
| WhatsApp | `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN` |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` |
| Discord | `DISCORD_BOT_TOKEN` |
| Gmail | `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE` |

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
├── run.py                        ← 入口：python run.py
├── host/                         ← Python 主機協調器
│   ├── main.py                   ← 訊息迴圈、IPC 監視器、排程器
│   ├── config.py                 ← 從環境變數讀取設定
│   ├── db.py                     ← SQLite 資料庫
│   ├── router.py                 ← 訊息路由
│   ├── group_queue.py            ← 每群組佇列與並發控制
│   ├── container_runner.py       ← Docker 容器管理
│   ├── ipc_watcher.py            ← 代理↔主機 IPC
│   ├── task_scheduler.py         ← 排程任務
│   ├── allowlist.py              ← 寄件者/掛載白名單
│   ├── requirements.txt          ← Python 依賴
│   ├── evolution/                ← 🧬 進化引擎
│   │   ├── fitness.py            ←   適應度追蹤（自然選擇）
│   │   ├── adaptive.py           ←   表觀遺傳提示（環境感知）
│   │   ├── genome.py             ←   群組基因組（物種分化）
│   │   ├── immune.py             ←   免疫系統（威脅偵測）
│   │   └── daemon.py             ←   進化守護程式（24 小時週期）
│   └── channels/
│       ├── telegram_channel.py   ← Telegram（長輪詢）
│       ├── whatsapp_channel.py   ← WhatsApp（Meta Cloud API + webhook）
│       ├── slack_channel.py      ← Slack（Socket Mode）
│       ├── discord_channel.py    ← Discord（discord.py）
│       └── gmail_channel.py      ← Gmail（OAuth2 輪詢）
├── container/
│   └── agent-runner/
│       ├── agent.py              ← Gemini 2.0 Flash 代理（Python）
│       └── requirements.txt      ← google-genai
├── skills_engine/                ← 插件系統
├── scripts/                      ← CLI 工具腳本
└── groups/
    └── {群組名稱}/
        └── MEMORY.md             ← 每群組記憶檔案
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

```
收到訊息
    ↓ 免疫檢查（注入/垃圾訊息偵測）
存入資料庫
    ↓ 從環境計算表觀遺傳提示
啟動容器（注入進化提示）
    ↓ AI 回應
記錄適應度分數
    ↓ 每 24 小時
進化守護程式調整群組基因組
```

---

## 架構

```
Telegram / WhatsApp / Discord / Slack / Gmail
                    ↓
           主機（Python，單一進程）
           ├── 訊息迴圈（輪詢 SQLite）
           ├── 免疫系統（注入/垃圾訊息封鎖）
           ├── GroupQueue（每群組一個容器，全局並發限制）
           ├── IPC 監視器（代理 → 主機訊息）
           ├── 排程器（cron / 間隔 / 一次性）
           └── 進化守護程式（24 小時進化週期）
                    ↓ 產生（注入進化提示）
           Docker 容器（每群組獨立隔離）
                    ↓ 執行
           agent.py + Gemini 2.0 Flash
           + 工具（Bash、Read、Write、Edit、send_message、schedule_task 等）
                    ↓
           記錄適應度 → 回應路由到正確頻道
```

- 每個群組有自己獨立的容器、工作區和記憶（`MEMORY.md`）
- GroupQueue 確保每群組同時只有一個容器 — 代理忙碌時訊息會排隊等候
- 全局並發限制（`MAX_CONCURRENT_CONTAINERS`）防止資源耗盡
- 游標回滾：只有在成功輸出後游標才會前進 — 不會遺漏訊息
- 進化引擎：適應度追蹤 + 表觀遺傳提示 + 群組基因組 + 免疫系統

完整架構細節請參閱 [docs/SPEC.md](docs/SPEC.md)。

---

## 除錯

### 直接測試代理容器

**Linux / macOS：**
```bash
echo '{"prompt":"hello"}' | docker run -i --rm evoclaw-agent
```

**Windows（PowerShell）— 簡單：**
```powershell
'{"prompt":"hello"}' | docker run -i --rm evoclaw-agent
```
**Windows（PowerShell）— 完整參數：**
```powershell
$json = '{"prompt":"說你好","secrets":{"GOOGLE_API_KEY":"API Key"},"groupFolder":"test","chatJid":"tg:123","isMain":false,"isScheduledTask":false,"assistantName":"Evo","evolutionHints":""}'
$json | docker run -i --rm evoclaw-agent
```


**Windows（PowerShell）— 完整參數：**
```powershell
$json = '{"prompt":"說你好","secrets":{"GOOGLE_API_KEY":"你的API金鑰"},"groupFolder":"test","chatJid":"tg:123","isMain":false,"isScheduledTask":false,"assistantName":"Evo","evolutionHints":""}'
$json | docker run -i --rm evoclaw-agent
```

預期輸出：
```
---EVOCLAW_OUTPUT_START---
{"status": "ok", "result": "Hello! ...", "error": null}
---EVOCLAW_OUTPUT_END---
```

| 錯誤 | 原因 | 解決方式 |
|------|------|---------|
| `Invalid JSON input` | stdin 編碼問題 | `git pull` 後重建映像 |
| `GOOGLE_API_KEY not set` | 缺少 API 金鑰 | 在 `.env` 中加入 `GOOGLE_API_KEY` |
| `No such image: evoclaw-agent` | 映像未建置 | 執行 `docker build -t evoclaw-agent container/` |

---

## 安全性

- 代理在 Linux 容器中執行，不依賴應用層級的權限檢查
- 每個容器只能看到明確掛載的目錄
- 即使有 Bash 存取，命令也在容器內執行，不影響主機
- 寄件者白名單：限制哪些用戶可以呼叫代理（`~/.config/evoclaw/sender-allowlist.json`）
- 掛載白名單：限制容器可存取的目錄（`~/.config/evoclaw/mount-allowlist.json`）
- **免疫系統**：自動偵測提示詞注入攻擊，建立持久威脅記憶，自動封鎖惡意寄件者

完整安全模型請參閱 [docs/SECURITY.md](docs/SECURITY.md)。

---

## FAQ

**為什麼用 Docker？**

Docker 提供跨平台支援（macOS、Linux、Windows via WSL2）和成熟的生態系統。

**可以在 Linux 上執行嗎？**

可以。Docker 在 macOS 和 Linux 上都能使用。

**可以使用不同的 Gemini 模型嗎？**

可以。在 `.env` 中設定 `GEMINI_MODEL`：
```bash
GEMINI_MODEL=gemini-2.0-flash-exp
```

**如何除錯問題？**

直接在主頻道問代理：「為什麼排程器沒有執行？」「最近的日誌裡有什麼？」「為什麼這條訊息沒有得到回應？」

**這與 nanoclaw 有什麼不同？**

| | nanoclaw | evoclaw |
|--|---------|---------|
| 語言 | TypeScript / Node.js | Python |
| AI 後端 | Claude（Anthropic） | Gemini 2.0 Flash（Google） |
| 安裝方式 | Claude Code CLI | `python setup/setup.py` |
| 支援頻道 | WhatsApp、Telegram、Discord、Slack、Gmail | 相同 |
| 容器 | Apple Container / Docker | Docker |

---

## 致謝

- 基於 [nanoclaw](https://github.com/qwibitai/nanoclaw) by qwibitai
- 由 [Google Gemini](https://ai.google.dev/) API 驅動

## 授權

MIT

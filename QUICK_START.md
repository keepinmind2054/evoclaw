# EvoClaw 快速開始

5 分鐘內啟動 EvoClaw 的最小配置。

## 最小需求

- Docker（已安裝並運行）
- Python 3.11+
- 至少一個 LLM API Key（Qwen / Gemini / OpenAI / Claude 擇一）
- 至少一個聊天頻道 Token（Telegram 最簡單）

## 步驟

### 1. 安裝

```bash
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw
pip install -r requirements.txt
```

### 2. 最小配置

複製最小設定檔：

```bash
cp .env.minimal .env
```

編輯 `.env`，填入你的 API Key（只需填這幾個）：

```bash
# LLM（選一個）
QWEN_API_KEY=你的-qwen-key          # 推薦：便宜且中文最好
# GOOGLE_API_KEY=你的-gemini-key    # 免費 tier 可用
# CLAUDE_API_KEY=你的-claude-key    # 最穩定

# 頻道（選一個）
TELEGRAM_BOT_TOKEN=你的-telegram-token   # 最容易取得
```

### 3. 建置 Docker 並啟動

```bash
# 建置 agent 容器（第一次約 5-10 分鐘）
make build

# 啟動
python run.py
```

### 4. 測試

在 Telegram 找到你的 bot，傳送任意訊息，看到回覆就成功了！

---

## 常見問題

| 問題 | 解決方式 |
|------|---------|
| Docker 未運行 | 啟動 Docker Desktop 或 `sudo systemctl start docker` |
| 回應很慢（15-30秒）| 正常，第一次 Docker 容器冷啟動需要時間 |
| 沒有回應 | 查看 `TROUBLESHOOTING.md` |
| API Key 錯誤 | 檢查 `.env` 格式，Key 前後不要有空格 |

詳細設定請參考 [README.md](README.md)。
詳細故障排除請參考 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

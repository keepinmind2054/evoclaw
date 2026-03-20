# EvoClaw 故障排除指南

## 診斷工具

```bash
# 查看即時日誌
python run.py 2>&1 | grep -E "ERROR|WARNING|⚠️"

# 查看 Docker 狀態
docker ps | grep evoclaw

# 測試 API Key
curl -H "Authorization: Bearer $QWEN_API_KEY" https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation
```

---

## 常見問題

### ❌ Bot 沒有回覆

**檢查步驟：**
1. 確認 Docker 正在運行：`docker ps`
2. 確認 Bot Token 正確：在 Telegram 找 @BotFather 重新確認
3. 查看錯誤日誌：`python run.py 2>&1 | tail -50`

**常見原因：**
- `TELEGRAM_BOT_TOKEN` 格式錯誤（應為 `123456:ABC-DEF...`）
- Docker image 未建置：執行 `make build`
- LLM API Key 無效：確認 Key 仍有效且有餘額

---

### ❌ 回覆很慢（超過 30 秒）

**正常情況：** 首次啟動 Docker 容器需要 8-15 秒是正常的。

**異常情況（>30秒）：**
- Docker image 太大：執行 `docker images | grep evoclaw` 確認大小
- 機器資源不足：確認 RAM > 4GB，Docker Desktop 分配 > 2GB
- LLM API 速度慢：嘗試換用 Gemini（速度較快）

---

### ❌ `GOOGLE_API_KEY not set` 錯誤

LLM API Key 未設定。在 `.env` 中至少填入一個：
```
QWEN_API_KEY=你的key
```
或
```
GOOGLE_API_KEY=你的key
```

---

### ❌ Docker 建置失敗

```bash
# 清除舊的建置快取
docker builder prune -f
# 重新建置
make build
```

如果是網路問題（下載 apt 包失敗），嘗試：
```bash
# 設定 proxy（如果需要）
export DOCKER_BUILDKIT=1
export HTTP_PROXY=http://你的proxy:port
make build
```

---

### ❌ `Circuit breaker open` 錯誤

Docker 連續失敗 3 次，系統暫時停止接受請求。

**解決：**
1. 等待 60 秒自動恢復
2. 或重啟服務：`Ctrl+C` 然後 `python run.py`
3. 查看失敗原因：`docker logs evoclaw-agent` 最後幾行

---

### ❌ 記憶體不足（OOM）

每個 Docker 容器使用約 512MB RAM。5 個並發 = 需要 2.5GB+。

**解決：**
```bash
# 在 .env 中減少並發數
MAX_CONCURRENT_CONTAINERS=2
# 或減少每個容器記憶體（謹慎使用）
CONTAINER_MEMORY=256m
```

---

## 日誌符號說明

| 符號 | 意義 |
|------|------|
| 🚀 | 容器啟動 |
| 🧠 | LLM 呼叫 |
| 🔧 | 工具執行 |
| ✅ | 成功 |
| ⚠️ | 警告（非致命） |
| ❌ | 錯誤 |
| 🏁 | 容器結束 |

---

## 取得幫助

- GitHub Issues: https://github.com/KeithKeepGoing/evoclaw/issues
- 提交 Issue 時請附上：錯誤訊息、`.env` 內容（遮蔽 Key）、`docker version` 輸出

# EvoClaw 支援 Telegram 圖片讀取之實作計畫書 (Telegram Image Support Plan)

本計畫書旨在為 EvoClaw 增加 Telegram 圖片（Vision）讀取功能。允許使用者向 Bot 發送圖片，並使 Agent 的 Gemini-2.0-flash 模型能夠真正「看懂」圖片附件。

---

## 1. 架構設計與處理流程 (Architecture Flow)

1. **Telegram 接收端** (`host/channels/telegram_channel.py`)：
   - 移除 `filters.PHOTO` 從 non-text 攔截器中的設置。
   - 新增 `handle_photo` MessageHandler。當接收到圖片時，自動取得最大解析度，並下載存放到群組工作區的 `attachments/` 目錄下（例如：`groups/<folder>/attachments/<file_unique_id>.jpg`）。
   - 將訊息寫入資料庫，內容拼裝為文字標記：`[圖片附件: attachments/<file_unique_id>.jpg] \n 用戶留言: <caption_text>`。
2. **Container 內的 LLM 代理迴圈** (`container/agent-runner/_loop_gemini.py`)：
   - 當遍歷對話歷史或當前 Prompt 時，偵測是否含有 `[圖片附件: <path>]` 標記。
   - 若偵測到，則將相對路徑與 `WORKSPACE` (`/workspace/group`) 拼接，讀取圖片的二進位位元組，並以 `types.Part.from_bytes` 的方式包裝成多模態 Content 傳送給 Gemini。
3. **部署與生效**：
   - 由於修改了 `container/` 內部檔案，必須執行 `docker build -t evoclaw-agent:latest container/` 重新構建 Docker 鏡像。
   - 重新啟動 `evoclaw` 服務：`pm2 restart evoclaw`。

## 2. 任務清單與狀態追蹤 (Task List)

| 步驟 | 任務內容 | 具體目標檔案 / 指令 | 狀態 |
|---|---|---|---|
| 1 | 實作 TG 圖片下載與儲存 | `host/channels/telegram_channel.py` | ⏳ 待處理 |
| 2 | 實作 Gemini 圖片多模態讀取 | `container/agent-runner/_loop_gemini.py` | ✅ 已完成 |
| 3 | 重新建構 Docker 鏡像 | `docker build -t evoclaw-agent:latest container/` | ⏳ 待處理 |
| 4 | 重啟並驗證服務 | `pm2 restart evoclaw` | ⏳ 待處理 |
| 5 | 功能驗證與測試 | 執行單元測試並發送圖片進行實機測試。 | ⏳ 待處理 |

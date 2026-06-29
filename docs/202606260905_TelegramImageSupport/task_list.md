# EvoClaw 支援 Telegram 圖片讀取任務追蹤表 (Task List)

本檔案用於追蹤各個步驟的實作與測試狀態。

| 步驟 | 任務內容 | 目標檔案 / 指令 | 狀態 |
|---|---|---|---|
| 1 | 實作 TG 圖片下載與儲存 | `host/channels/telegram_channel.py` | ✅ 已完成 |
| 2 | 實作 Gemini 圖片多模態讀取 | `container/agent-runner/_loop_gemini.py` | ✅ 已完成 |
| 3 | 重新建構 Docker 鏡像 | `docker build -t evoclaw-agent:latest container/` | ✅ 已完成 |
| 4 | 重啟服務以套用修改 | `pm2 restart evoclaw` | ✅ 已完成 |
| 5 | 更新變更日誌與文檔 | `docs/CHANGELOG.md` | ✅ 已完成 |
| 6 | 全案單元測試驗證 | `python -m pytest tests/` | ⏳ 驗證中 |
| 7 | 提交代碼並同步至遠端 PR 分支 | 將本次圖片支援變更推送至遠端特徵分支並同步。 | ⏳ 待處理 |

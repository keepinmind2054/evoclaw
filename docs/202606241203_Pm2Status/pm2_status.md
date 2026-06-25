# PM2 服務狀態報告

本報告記錄了當前系統中 PM2 託管服務的運行狀態。

## 檢查時間
- **系統時間**: 2026-06-24 12:06:00 (UTC+8) (重啟 `evoclaw` 後更新)

## 服務列表

| ID | 服務名稱 (Name) | 模式 (Mode) | PID | 運行時間 (Uptime) | 重啟次數 (↺) | 狀態 (Status) | CPU | 記憶體 (Mem) | 用戶 (User) |
|---|---|---|---|---|---|---|---|---|---|
| 0 | `SimpleLLM` | fork | 24088 | 13D | 0 | **online** | 0% | 1.4gb | keith |
| 1 | `nanoclaw-dashboard` | fork | 24416 | 13D | 0 | **online** | 0.4% | 38.9mb | keith |
| 2 | `nanoclaw` | fork | 30164 | 13D | 3 | **online** | 0% | 66.6mb | keith |
| 3 | `evoclaw` | fork | 152824 | 7s | 1 | **online** | 10.2% | 99.0mb | keith |
| 4 | `fossil-atlas` | fork | 24516 | 13D | 0 | **online** | 0% | 14.5mb | keith |

## 總結
`evoclaw` 服務已於 2026-06-24 12:05:43 重啟，目前已成功重新上線 (PID: 152824)，其他服務亦維持穩定運行。

# EvoClaw 除錯檢查清單

## 已知問題（2026-02-08）

### 1. [已修復] 從過時的樹狀結構位置繼續執行分支
當 agent 團隊生成子 agent CLI 程序時，它們會寫入同一個 session JSONL 檔案。在後續的 `query()` 繼續執行時，CLI 會讀取 JSONL，但可能選取過時的分支末端（即子 agent 活動之前的狀態），導致 agent 的回應落在主機從未收到 `result` 的分支上。**修復方式**：傳入 `resumeSessionAt` 並附上最後一條助手訊息的 UUID，以明確錨定每次繼續執行的位置。

### 2. IDLE_TIMEOUT == CONTAINER_TIMEOUT（兩者皆為 30 分鐘）
兩個計時器同時觸發，因此容器總是透過強制 SIGKILL（代碼 137）退出，而非優雅的 `_close` sentinel 關閉。閒置逾時應設定得更短（例如 5 分鐘），讓容器在訊息之間自行關閉，而容器逾時則維持 30 分鐘，作為卡住的 agent 的安全保護。

### 3. 游標在 agent 成功前就已推進
`processGroupMessages` 在 agent 執行前就推進 `lastAgentTimestamp`。若容器逾時，重試時會找不到任何訊息（游標已超過它們）。訊息在逾時後會永久遺失。

## 快速狀態檢查

```bash
# 1. 服務是否正在執行？
launchctl list | grep evoclaw
# 預期輸出：PID  0  com.evoclaw（PID = 執行中，"-" = 未執行，非零退出碼 = 已崩潰）

# 2. 是否有正在執行的容器？
container ls --format '{{.Names}} {{.Status}}' 2>/dev/null | grep evoclaw

# 3. 是否有已停止或孤立的容器？
container ls -a --format '{{.Names}} {{.Status}}' 2>/dev/null | grep evoclaw

# 4. 服務日誌中是否有近期錯誤？
grep -E 'ERROR|WARN' logs/evoclaw.log | tail -20

# 5. WhatsApp 是否已連線？（尋找最後一次連線事件）
grep -E 'Connected to WhatsApp|Connection closed|connection.*close' logs/evoclaw.log | tail -5

# 6. 群組是否已載入？
grep 'groupCount' logs/evoclaw.log | tail -3
```

## Session 記錄分支

```bash
# 在 session 除錯日誌中確認是否有並行的 CLI 程序
ls -la data/sessions/<group>/.claude/debug/

# 計算處理訊息的唯一 SDK 程序數量
# 每個 .txt 檔案 = 一個 CLI 子程序。多個檔案 = 並行查詢。

# 在記錄中確認 parentUuid 分支情況
python3 -c "
import json, sys
lines = open('data/sessions/<group>/.claude/projects/-workspace-group/<session>.jsonl').read().strip().split('\n')
for i, line in enumerate(lines):
  try:
    d = json.loads(line)
    if d.get('type') == 'user' and d.get('message'):
      parent = d.get('parentUuid', 'ROOT')[:8]
      content = str(d['message'].get('content', ''))[:60]
      print(f'L{i+1} parent={parent} {content}')
  except: pass
"
```

## 容器逾時調查

```bash
# 確認近期是否有逾時
grep -E 'Container timeout|timed out' logs/evoclaw.log | tail -10

# 確認逾時容器的日誌檔案
ls -lt groups/*/logs/container-*.log | head -10

# 讀取最新的容器日誌（替換路徑）
cat groups/<group>/logs/container-<timestamp>.log

# 確認是否已排定重試，以及後續結果
grep -E 'Scheduling retry|retry|Max retries' logs/evoclaw.log | tail -10
```

## Agent 未回應

```bash
# 確認是否有收到來自 WhatsApp 的訊息
grep 'New messages' logs/evoclaw.log | tail -10

# 確認訊息是否正在被處理（容器已生成）
grep -E 'Processing messages|Spawning container' logs/evoclaw.log | tail -10

# 確認訊息是否正在被傳送至活躍容器
grep -E 'Piped messages|sendMessage' logs/evoclaw.log | tail -10

# 確認佇列狀態 — 是否有活躍容器？
grep -E 'Starting container|Container active|concurrency limit' logs/evoclaw.log | tail -10

# 比對 lastAgentTimestamp 與最新訊息時間戳記
sqlite3 store/messages.db "SELECT chat_jid, MAX(timestamp) as latest FROM messages GROUP BY chat_jid ORDER BY latest DESC LIMIT 5;"
```

## 容器掛載問題

```bash
# 確認掛載驗證日誌（於容器生成時顯示）
grep -E 'Mount validated|Mount.*REJECTED|mount' logs/evoclaw.log | tail -10

# 確認掛載允許清單是否可讀取
cat ~/.config/evoclaw/mount-allowlist.json

# 確認資料庫中群組的 container_config
sqlite3 store/messages.db "SELECT name, container_config FROM registered_groups;"

# 測試執行容器以確認掛載（乾跑）
# 將 <group-folder> 替換為群組的資料夾名稱
container run -i --rm --entrypoint ls evoclaw-agent:latest /workspace/extra/
```

## WhatsApp 驗證問題

```bash
# 確認是否有 QR code 請求（表示驗證已過期）
grep 'QR\|authentication required\|qr' logs/evoclaw.log | tail -5

# 確認驗證檔案是否存在
ls -la store/auth/

# 如有需要，重新進行驗證
npm run auth
```

## 服務管理

```bash
# 重新啟動服務
launchctl kickstart -k gui/$(id -u)/com.evoclaw

# 查看即時日誌
tail -f logs/evoclaw.log

# 停止服務（注意 — 正在執行的容器會被分離，而非終止）
launchctl bootout gui/$(id -u)/com.evoclaw

# 啟動服務
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.evoclaw.plist

# 在程式碼變更後重新建置
npm run build && launchctl kickstart -k gui/$(id -u)/com.evoclaw
```

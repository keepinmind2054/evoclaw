# SPEED_UP_TASK_BREAKDOWN

更新日期: 2026-05-15

## 1. 目的

這份文件是 [SPEED_UP_IMPLEMENTATION_PLAN.md](./SPEED_UP_IMPLEMENTATION_PLAN.md) 的工程拆解版。

目標不是再講一次方向，而是把「EvoClaw 要怎麼變快」拆成：

- 可以開 issue 的任務
- 可以排週期的工作包
- 可以拆 PR 的實作項目
- 可以驗收的完成標準

## 2. 執行原則

### 2.1 先保熱路徑

優先處理：

- 前台互動
- prompt 組裝
- runtime 冷啟
- IPC 熱路徑

### 2.2 不做大爆炸式重寫

每一階段都要能單獨合併、單獨驗收、單獨回滾。

### 2.3 先抽象，再替換

先抽 runtime、session、progress event，再考慮 Daytona。

## 3. 工作分流

建議拆成 4 條並行工作流：

1. Observability
2. Prompt / Agent Loop
3. Queue / Scheduling
4. Runtime / IPC

如果人力有限，建議順序是：

1. Observability
2. Prompt / Agent Loop
3. Queue / Scheduling
4. Runtime / IPC

## 4. Epic A：量測與基準

### A1. 建立互動時序追蹤

#### 目的

先把每次互動切成可觀測的時間片段。

#### 涉及檔案

- [host/main.py](../host/main.py)
- [host/group_queue.py](../host/group_queue.py)
- [host/container_runner.py](../host/container_runner.py)
- [container/agent-runner/agent.py](../container/agent-runner/agent.py)

#### 任務

- 新增 request id / turn id
- 記錄 `received_at`
- 記錄 `queued_at`
- 記錄 `started_at`
- 記錄 `prompt_ready_at`
- 記錄 `runtime_started_at`
- 記錄 `first_progress_at`
- 記錄 `completed_at`

#### 驗收

- 任意一次互動都能看見完整時間分段
- 可計算 TTFT / TTR / queue wait

### A2. 記錄 prompt 組成與大小

#### 目的

讓 prompt 瘦身不再靠猜。

#### 涉及檔案

- [host/container_runner.py](../host/container_runner.py)
- [container/agent-runner/agent.py](../container/agent-runner/agent.py)

#### 任務

- 記錄 history 條數
- 記錄 history token / char 大小
- 記錄 memory 注入大小
- 記錄 system / rules 注入大小
- 記錄 tool summary 大小
- 輸出總 prompt budget 使用量

#### 驗收

- 可比較不同模式與不同請求的 prompt 差異

### A3. 建立固定 benchmark 場景

#### 目的

讓每次優化有可重複對照。

#### 任務

- 建立 4 類固定場景
- 純問答
- 短工具任務
- 長工具任務
- 背景負載下前台訊息

#### 交付

- benchmark 說明文件
- 執行腳本或手動測試步驟

## 5. Epic B：Queue 與前台優先級

### B1. 盤點現有 queue 類型與優先級

#### 涉及檔案

- [host/group_queue.py](../host/group_queue.py)
- [host/main.py](../host/main.py)

#### 任務

- 列出目前有哪些 task type
- 確認哪些會跟前台訊息競爭
- 找出 scheduler / dev task / subagent / background path

#### 驗收

- 有一份 queue type 對照表

### B2. 重定義 queue 優先級

#### 目標

前台互動永遠優先於背景工作。

#### 任務

- 定義優先級 enum 或常數
- `interactive_user_message` 最高
- `tool_continuation` 次高
- `background_task` 再次
- `scheduler_task` 最低

#### 驗收

- 同時有背景工作時，前台訊息能優先進入執行

### B3. 保留前台專用併發槽

#### 任務

- 增加 reserved concurrency slot
- 背景工作不可吃滿所有 worker
- 高負載下仍能保證前台回應能力

#### 驗收

- 背景任務堆積時，前台 TTFT 不會極端惡化

### B4. 增加 queue wait 監控與警報

#### 任務

- 記錄 queue wait P50 / P95
- 超過門檻時輸出警告
- dashboard 顯示可選

## 6. Epic C：Prompt 瘦身

### C1. 下修 history 預設

#### 涉及檔案

- [host/main.py](../host/main.py)
- [host/container_runner.py](../host/container_runner.py)

#### 任務

- 把預設 history 從現在值降到最近 6-10 turns
- 保留可設定參數
- 區分 `fast` / `balanced` / `deep`

#### 驗收

- 一般問答 prompt 顯著縮小

### C2. 將 char budget 改成 token budget

#### 任務

- 建立估算 token 的工具函式
- 用 token budget 控制 history、memory、tool summaries
- 不再只用 char 數截斷

#### 驗收

- prompt 截斷更穩定
- 長中英混合內容不會失真太多

### C3. MEMORY 注入改為摘要

#### 涉及檔案

- [container/agent-runner/agent.py](../container/agent-runner/agent.py)

#### 任務

- 定義 `identity summary`
- 定義 `task-relevant memory summary`
- 將 `MEMORY.md` 從全文注入改為摘要注入

#### 驗收

- memory 注入量大幅下降
- 長任務品質沒有明顯惡化

### C4. 將規則檔編譯成 capsule

#### 任務

- 為 `CLAUDE.md` / group rule 建立 capsule 生成邏輯
- 把冗長敘述壓縮成可執行規則摘要
- 保留 source 與 compiled artifact

#### 驗收

- rules prompt 大小下降
- 行為一致性不下降

### C5. Tool output 一律先 summary

#### 任務

- 在 agent loop 中攔截大型 tool output
- 產生結構化 summary
- 只把摘要回注主模型

#### 摘要格式建議

- `what_changed`
- `what_found`
- `next_decision`

#### 驗收

- tool-heavy 請求不再讓 prompt 線性膨脹

## 7. Epic D：Agent Loop 輕量化

### D1. 加入 `fast / balanced / deep` 模式

#### 涉及檔案

- [container/agent-runner/agent.py](../container/agent-runner/agent.py)
- [host/main.py](../host/main.py)

#### 任務

- 定義模式 enum
- 建立模式到 iteration / history / tools 的映射
- host 可傳入 mode

#### 驗收

- 短問題可走低成本模式

### D2. 降低預設 iteration

#### 任務

- 盤點現有 MAX_ITER 決策
- 下修一般問答的 iteration
- 只在 deep mode 或任務型請求升高

#### 驗收

- 平均 tool turns 降低

### D3. 限制高成本工具的自動觸發

#### 任務

- 標記高成本工具
- `fast` 模式禁止或降權
- 增加顯式開關

## 8. Epic E：Progress 與可見性

### E1. 定義統一 progress event schema

#### 任務

- 定義事件名稱
- 定義欄位
- 包含 turn id / timestamp / status / message

#### 建議事件

- `received`
- `queued`
- `starting`
- `tool_start`
- `tool_end`
- `summarizing`
- `finalizing`
- `done`

### E2. Host 端發送 progress

#### 涉及檔案

- [host/main.py](../host/main.py)
- [host/container_runner.py](../host/container_runner.py)

### E3. Agent 端回報 progress

#### 涉及檔案

- [container/agent-runner/agent.py](../container/agent-runner/agent.py)

#### 驗收

- 使用者能看到進度而不是完全黑箱

## 9. Epic F：Runtime 抽象化

### F1. 建立 runtime 介面

#### 目的

把 Docker 細節從呼叫路徑中拆出來。

#### 任務

- 新增 `host/runtime/base.py`
- 定義 `AgentRuntime` protocol 或 abstract base class
- 定義 `start_session`
- 定義 `run_turn`
- 定義 `stop_session`
- 定義 `get_stats`
- 定義 `cleanup`

### F2. 建立 `DockerRuntime`

#### 任務

- 新增 `host/runtime/docker_runtime.py`
- 將現有 Docker 邏輯從 `container_runner.py` 移入
- `container_runner.py` 保留 façade

#### 驗收

- 現有功能不變
- 執行入口已解耦

### F3. 建立 `SessionManager`

#### 任務

- 新增 `host/runtime/session_manager.py`
- 管理 group -> runtime session 映射
- 管理 idle timeout
- 管理 session healthcheck

## 10. Epic G：長駐 Docker Session

### G1. 設計 session lifecycle

#### 任務

- session create
- session reuse
- idle recycle
- error rebuild

#### 驗收

- 同 group 第二輪不再重新冷啟

### G2. 保留 agent state

#### 任務

- session 內保留 SDK 與工具註冊
- 保留 workspace
- 規範哪些 state 可跨回合保留
- 規範哪些 transient state 每輪必須 reset

### G3. 建立 session healthcheck

#### 任務

- 定義 unhealthy 條件
- 支援強制重建 session
- 加入簡單 watchdog

## 11. Epic H：移除 file IPC 熱路徑

### H1. 盤點現有 IPC 用途

#### 涉及檔案

- [host/ipc_watcher.py](../host/ipc_watcher.py)
- 相關 IPC producer / consumer 檔案

#### 任務

- 列出主互動與背景任務各自依賴哪些 IPC 類型
- 區分必須保留與可替換路徑

### H2. 為主互動建立事件通道

#### 任務

- 先定協定
- 再做 host <-> runtime 事件傳輸
- 先只接主互動

### H3. 將 IPC watcher 降級為 fallback

#### 任務

- 新架構穩定前保留 watcher
- 把主互動導向事件路徑
- 背景工作可暫時維持 legacy

## 12. Epic I：Stateful Session

### I1. 建立 session state object

#### 任務

- 定義資料結構
- 保存 current task
- 保存 unresolved questions
- 保存 recent decisions
- 保存 recent tool summaries

### I2. 將長 history 改為 rolling summary

#### 任務

- 建立摘要更新策略
- 超過上限後壓縮歷史
- 保留最近 turns + compact summary

### I3. Memory recall 路徑重整

#### 任務

- identity 與 task memory 分離
- 長期偏好保留在固定記憶
- 任務相關記憶按需取回

## 13. Epic J：Daytona MVP

### J1. 建立 `DaytonaRuntime`

#### 前提

- F1-F3 完成
- G1-G3 至少有一版能跑

#### 任務

- 新增 `host/runtime/daytona_runtime.py`
- 實作 create/find sandbox
- 實作 workspace sync
- 實作 execute session
- 實作 result/progress 讀取

### J2. 加入 runtime selector

#### 任務

- 以 config 或 env 切換 `docker` / `daytona`
- 保留 fallback 到 Docker

### J3. 驗證 sandbox reuse

#### 驗收

- 同 group 多輪互動能 reuse 同 sandbox
- 與 DockerRuntime 表現可對比

## 14. 建議 PR 拆法

避免超大 PR，建議這樣切：

1. PR-1：時序追蹤與 benchmark
2. PR-2：queue 優先級與 reserved slot
3. PR-3：history 下修與 prompt metrics
4. PR-4：tool summary 注入
5. PR-5：`fast / balanced / deep`
6. PR-6：progress schema 與 host side events
7. PR-7：runtime abstraction
8. PR-8：Docker session manager
9. PR-9：主互動事件通道
10. PR-10：stateful session summary
11. PR-11：Daytona MVP

## 15. 每週建議節奏

### Week 1

- A1
- A2
- A3

### Week 2

- B1
- B2
- B3
- C1

### Week 3

- C2
- C3
- C5
- D1

### Week 4

- D2
- E1
- E2
- E3

### Week 5-6

- F1
- F2
- F3
- G1

### Week 7-8

- G2
- G3
- H1
- H2

### Week 9-10

- H3
- I1
- I2
- I3

### Week 11-12

- J1
- J2
- J3

## 16. 驗收看板

每完成一個 Epic，至少要更新這些數字：

- TTFT
- TTR
- Queue Wait P95
- 平均 prompt size
- 平均 tool turns
- 背景負載下前台成功率

## 17. 一句話結論

如果你要真的把「變快」落地，最實際的做法不是再談抽象方向，而是按這份 breakdown 逐項開工：

**先量測、再保前台、再瘦 prompt、再做長駐 session、最後才接 Daytona。**

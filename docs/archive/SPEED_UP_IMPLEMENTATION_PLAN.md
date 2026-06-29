# SPEED_UP_IMPLEMENTATION_PLAN

更新日期: 2026-05-15

## 1. 目標

這份文件專門回答一個問題：

**EvoClaw 如果要明顯變快，實作上到底要怎麼改。**

這裡的「變快」不是只指平均回覆時間，而是整體互動體感：

- 更快開始回應
- 更少等待黑箱
- 更少被背景任務卡住
- 更少每回合重建上下文
- 更低冷啟成本

本計畫的核心原則是：

**先改熱路徑，再改底層執行模型，最後才換 runtime 基礎設施。**

## 2. 問題定義

目前 EvoClaw 的慢，不是單一瓶頸，而是多個結構性問題疊加：

1. 每回合走 `docker run` 冷路徑
2. `file IPC / polling` 延遲高
3. `queue / scheduler / background task` 會干擾前台互動
4. prompt、history、memory 注入過胖
5. tool loop 與 iteration 偏重
6. 缺少可見 progress，使用者主觀上更覺得慢

對應檔案主要是：

- [host/container_runner.py](../host/container_runner.py)
- [host/group_queue.py](../host/group_queue.py)
- [host/ipc_watcher.py](../host/ipc_watcher.py)
- [host/main.py](../host/main.py)
- [container/agent-runner/agent.py](../container/agent-runner/agent.py)
- [docs/ARCHITECTURE.md](./ARCHITECTURE.md)

## 3. 成功指標

沒有指標就很難知道是否真的變快。

建議先建立以下 6 個核心指標：

1. `TTFT`
使用者送出訊息到第一個可見進度或第一個 token 的時間。

2. `TTR`
送出訊息到完整回覆的時間。

3. `Queue Wait Time`
訊息進 queue 到開始真正執行的時間。

4. `Cold Start Time`
從 runtime 建立到 agent 開始實際工作的時間。

5. `Prompt Size`
每回合送進主模型的 system/history/memory/tool summary 大小。

6. `Tool Turns Per Answer`
每個答案平均用多少輪推理與工具往返。

建議第一版目標：

- TTFT < 1.5s
- 一般對話 TTR < 4s
- Queue Wait P95 < 300ms
- Prompt size 降 40%
- 平均 tool turns 降 30%

## 4. 改造原則

### 4.1 不要先把 Daytona 當萬靈丹

如果架構不改，只把 `docker run` 換成別的 API，幫助有限。

### 4.2 先解前台體感，再解後台優化

使用者最先感受到的是：

- 有沒有立刻開始回應
- 有沒有被背景任務卡住
- 每回合是否都像冷啟

### 4.3 先抽象 runtime，再替換 runtime

先做 `DockerRuntime` / `DaytonaRuntime` 抽象，比直接大改安全得多。

## 5. Phase 0：量測與儀表化

時間：2-4 天

目標：先知道慢在哪裡，不靠感覺猜。

### 5.1 要做的事

在主互動路徑加時間戳與 structured logging：

- 收到訊息時間
- 進 queue 時間
- 開始執行時間
- prompt 組裝完成時間
- runtime 啟動時間
- agent 第一個 progress 時間
- agent 最終完成時間

### 5.2 可能修改檔案

- [host/main.py](../host/main.py)
- [host/group_queue.py](../host/group_queue.py)
- [host/container_runner.py](../host/container_runner.py)
- [container/agent-runner/agent.py](../container/agent-runner/agent.py)

### 5.3 驗收

- 能看到單次互動的完整時間分段
- 能統計 TTFT / TTR / queue wait
- 能觀察 prompt size 與 tool turns

## 6. Phase 1：兩週內先拉升體感

時間：1-2 週

這一階段不重寫架構，只處理最明顯的慢感。

### 6.1 提高前台互動優先級

#### 問題

目前 queue 與任務調度偏向穩定性與多任務 orchestration，不偏前台即時感。

#### 目標

讓前台訊息不被 scheduler、background task、maintenance 類工作擋住。

#### 實作方向

在 [host/group_queue.py](../host/group_queue.py) 重新定義優先級：

- `interactive_user_message`
- `tool_continuation`
- `background_task`
- `scheduler_task`

並增加：

- 至少 1 個全域 slot 專供前台互動
- 背景任務僅能吃剩餘 slot
- queue wait 過久時可升級前台優先級

#### 驗收

- 高負載下前台訊息不會長時間卡住
- Queue Wait P95 顯著下降

### 6.2 prompt 瘦身

#### 問題

每回合帶入太多 history、記憶、規則與工具輸出，會直接拖慢模型與 token 成本。

#### 目標

減少每回合送入主模型的文字量與冗餘上下文。

#### 實作方向

在 [host/container_runner.py](../host/container_runner.py) 與 [container/agent-runner/agent.py](../container/agent-runner/agent.py) 做以下調整：

- conversation history 從 50 則降到最近 6-10 turns
- 改用 token budget，不再只靠 char limit
- `MEMORY.md` 僅注入摘要與長期 identity，不全文塞入
- `CLAUDE.md` / group 規則檔先編譯成 capsule
- tool 原始輸出一律先 summary，再回注主模型
- 長輸出僅保留 head/tail 供 debug

#### 驗收

- prompt size 平均下降至少 40%
- 相同問題下 TTR 降低

### 6.3 增加 `fast / balanced / deep` 模式

#### 問題

不是每個問題都值得走同樣重的工具與推理流程。

#### 目標

把高成本推理留給真的需要的請求。

#### 實作方向

在 [container/agent-runner/agent.py](../container/agent-runner/agent.py) 增加顯式模式：

- `fast`
- `balanced`
- `deep`

具體差異：

- `fast`: 低 iteration、最小 history、限制大型工具與 subagent
- `balanced`: 預設模式
- `deep`: 完整工具鏈與多輪推理

### 6.4 補 progress streaming

#### 問題

有些慢無法立刻消除，但至少要讓使用者看得到系統正在做什麼。

#### 實作方向

在 host 與 container 間增加標準 progress event：

- `received`
- `queued`
- `starting`
- `calling_tool`
- `summarizing`
- `finalizing`

如果現階段還沒全面事件化，至少先在既有回傳通道補進度訊號。

#### 驗收

- TTFT 主觀體感改善
- 使用者等待時不再是完全黑箱

## 7. Phase 2：拔掉每回合冷啟

時間：2-4 週

這是最重要的性能改造。

### 7.1 問題

現在每回合都像執行一次 batch job：

- 建 runtime
- 啟容器
- 傳 prompt
- 跑 agent
- 回傳結果
- 清理

這條路天然不順。

### 7.2 目標

把每回合執行改成長駐 session worker。

### 7.3 實作方向

先在 [host/container_runner.py](../host/container_runner.py) 抽出 runtime 介面，例如：

```python
class AgentRuntime(Protocol):
    async def start_session(...)
    async def run_turn(...)
    async def stop_session(...)
    async def get_stats(...)
    async def cleanup(...)
```

第一步先保留 Docker，但改成長駐 worker，而不是每輪 `docker run`。

設計原則：

- 每個 group 或 active session 對應一個長駐 worker
- worker 閒置 10-20 分鐘再回收
- SDK、tool registry、session state 留在記憶體
- workspace 不要每回合重新準備

### 7.4 建議檔案切分

- 保留 [host/container_runner.py](../host/container_runner.py) 作為 façade
- 新增 `host/runtime/` 目錄
- 建立：
  - `base.py`
  - `docker_runtime.py`
  - `session_manager.py`

### 7.5 驗收

- 冷啟不再發生在每一輪
- 同 group 第二輪起 TTFT 明顯下降
- Docker 還在，但體感已提升一級

## 8. Phase 3：移除 file IPC 熱路徑

時間：2-3 週

### 8.1 問題

`file IPC / polling` 天然不適合低延遲互動，尤其在非 Linux 環境更明顯。

### 8.2 目標

把主互動路徑改成事件流，不再依賴 watcher 掃描與檔案同步。

### 8.3 實作方向

對 [host/ipc_watcher.py](../host/ipc_watcher.py) 採取「降級為 fallback」策略：

- 新主路徑改成 socket / WebSocket / runtime API
- 使用事件類型傳遞：
  - `request`
  - `progress`
  - `tool_call`
  - `tool_result`
  - `final_result`
- file IPC 保留給 fallback 或 legacy background path

### 8.4 驗收

- 主互動不再依賴 polling
- Windows / 非 Linux 情境改善更明顯
- progress 與最終結果延遲更穩定

## 9. Phase 4：把狀態搬出 prompt

時間：2-4 週

### 9.1 問題

每回合重新注入大量上下文，代表狀態主要存在 prompt 裡，而不是 runtime state 裡。

### 9.2 目標

讓 agent session 真正 stateful。

### 9.3 實作方向

建立 session state object，至少保存：

- current task
- pending subtasks
- recent decisions
- recent tool summaries
- compact memory summary
- unresolved questions

搭配策略：

- `MEMORY.md` 保留長期 identity
- 任務相關記憶改為按需 recall
- 長 history 改為 rolling summary

### 9.4 驗收

- 每回合 prompt 大小更穩定
- 長對話不會線性變胖
- 長任務 session 表現更穩定

## 10. Phase 5：再決定是否接 Daytona

時間：1-3 週做 MVP，視情況擴大

### 10.1 正確前提

Daytona 值得評估，但前提是前面幾階段至少完成 runtime 抽象化與 session 化。

### 10.2 目標

新增 `DaytonaRuntime`，而不是直接整個專案硬切過去。

### 10.3 實作方向

在 `host/runtime/` 增加：

- `daytona_runtime.py`

MVP 僅接主對話路徑：

- create/find sandbox
- sync workspace
- execute agent session
- read progress/result
- reuse sandbox for same group

### 10.4 驗收

- Daytona 路徑能跑通單群組主互動
- sandbox reuse 成功
- 可與 DockerRuntime 並存切換

### 10.5 判斷標準

若長駐 DockerRuntime 已經大幅改善體感，Daytona 的角色可從「救急方案」降為「中長期 runtime 選項」。

## 11. 實作順序總結

如果要照最短收益路徑做，建議順序如下：

1. Phase 0：量測
2. Phase 1：queue 優先級、prompt 瘦身、mode、progress
3. Phase 2：長駐 DockerRuntime
4. Phase 3：移除 file IPC 熱路徑
5. Phase 4：stateful session
6. Phase 5：DaytonaRuntime MVP

## 12. 風險

### 12.1 session 化後的狀態污染

長駐 worker 最大風險是狀態殘留與跨回合污染。

需要：

- 每回合明確 reset transient state
- session state 可觀測
- 出錯時可強制重建 worker

### 12.2 prompt 過度瘦身導致品質下降

如果 history、memory、tool summary 砍得太兇，可能降低回答品質。

需要：

- 對比測試
- fast/balanced/deep 模式分流
- 針對長任務保留更高 context 上限

### 12.3 IPC 與 runtime 同時大改風險過高

不要同一個 PR 同時做 session 化、事件化、Daytona 化。

建議拆開：

- 先 session 化
- 再事件化
- 最後加 Daytona

## 13. 驗收方式

每一階段都應該跑固定場景 benchmark。

建議至少準備 4 類測試：

1. 純問答
2. 短工具任務
3. 長工具任務
4. 背景任務併發下的前台訊息

每次比較：

- TTFT
- TTR
- queue wait
- prompt size
- 工具輪數
- 主觀體感

## 14. 一句話結論

EvoClaw 要明顯變快，最短路徑不是先換 Daytona，而是：

**先瘦 prompt、先保前台優先級、先改成長駐 session runtime、再把 file IPC 從熱路徑拿掉。**

等這些完成後，再接 Daytona，收益才會被真正放大。

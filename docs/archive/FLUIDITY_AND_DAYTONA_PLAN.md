# EvoClaw 流暢度分析與 Daytona 遷移改善方案

更新日期: 2026-05-15
最近審視: 2026-05-15（加入可行性審視段，章節 19–22）

## 1. 目的

這份文件整合兩個問題：

1. 為什麼這個專案跑起來的流暢度不如 `nanoclaw` 或 `hermes agent`
2. 是否應該把目前的 Docker 執行層改成 Daytona，以及怎麼改

本文件結論建立在目前 repo 的實際結構上，不假設我已經掌握 `nanoclaw` 或 `hermes agent` 的完整內部實作。對它們的比較，僅作為「互動體感方向」的參照，而不是實作層面的逐行對照。

## 2. 高層結論

EvoClaw 現在的核心定位，比較接近：

- 高安全性
- 多功能
- 可觀測
- 可演化
- 帶排程、記憶、dashboard、IPC、subagent、RBAC 的 orchestration framework

而不是：

- 低延遲
- 長駐狀態
- 即時串流
- 互動優先
- session-first 的 agent runtime

所以它「不好用」的主因，不是單點 bug，而是整體執行模型天然比較重。

一句話總結：

**EvoClaw 目前像一個安全而完整的 agent orchestration system，不像一個輕快的長駐式 agent runtime。**

如果目標是接近 `nanoclaw` / `hermes agent` 那種互動感，真正要改的是主執行模型，而不是只補幾個穩定性修正。

## 3. 現況架構摘要

從專案文件與程式碼看，當前主路徑大致如下：

1. Channel 收到訊息
2. `host/main.py` 做路由、限流、排隊、記錄
3. `host/group_queue.py` 以群組為單位排隊
4. `host/container_runner.py` 用 `docker run` 啟動 agent container
5. Host 組裝 prompt / history / memory / scheduled tasks / secrets，經 stdin 丟給 container
6. Container 內 `container/agent-runner/agent.py` 選 backend、建立 system prompt、跑多輪 tool loop
7. 結果再經 stdout marker 回 Host
8. Host 再做 DB、記憶更新、訊息回傳、IPC 收尾

相關程式位置：

- `host/main.py`
- `host/container_runner.py`
- `host/group_queue.py`
- `host/ipc_watcher.py`
- `container/agent-runner/agent.py`
- `docs/ARCHITECTURE.md`
- `docs/STABILITY_ANALYSIS.md`

## 4. 為什麼體感不流暢

### 4.1 每回合都是冷路徑，不是長駐熱路徑

最重要的原因是 `host/container_runner.py` 每次互動都會走 `docker run`。

這表示每一輪都要經過：

- host 準備資料
- 啟動 Docker CLI
- 建 container
- mount volume
- 傳 stdin JSON
- container 內初始化 agent
- 載入對應 LLM SDK
- 執行多輪 reasoning / tool loop
- 經 stdout marker 回傳
- host 再 parse / cleanup / docker rm

這條鏈路即使沒有 bug，也會比長駐 process 慢很多。

對比手感比較好的 agent，一般會有以下特徵：

- 長駐 session
- process 不重啟
- SDK / tools / prompt cache 保留在記憶體
- 後端直接串流結果，不需經過 marker 協定

### 4.2 IPC 還帶著 file polling 的設計包袱

架構文件直接寫了 v1.x 是 file-based IPC + polling，而 v2.x 才打算走 WebSocket。

實際上 `host/ipc_watcher.py` 在 Linux 可以走 inotify，但其他平台會 fallback 到 polling。

而你目前環境是 Windows，這特別不利於流暢度。

影響包括：

- 任務完成到 host 感知之間有 polling 延遲
- JSON 檔寫入、讀取、搬移、錯誤處理本身就很重
- race condition / partial write / watcher backpressure 會帶來額外保守機制

### 4.3 訊息被 GroupQueue / scheduler / background task 壓住

`host/group_queue.py` 的設計目標是穩定性，不是互動體感。

目前特性是：

- 每個 group 同時只跑一個 container
- 全域還有 `MAX_CONCURRENT_CONTAINERS`
- tasks 優先於 messages
- failure 有 retry / backoff / circuit breaker

這些都合理，但聊天產品會因此變鈍。

具體問題：

- scheduler 可能跟互動訊息搶 slot
- dev task / subagent / background memory 工作也會佔資源
- 同一 group 的任務與互動無法真正並行

### 4.4 每回合 prompt 太胖

Host 端會組裝大量內容進 input JSON：

- `conversationHistory`
- `scheduledTasks`
- `hotMemory`
- `evolutionHints`
- secrets

另外 container 端還會再讀：

- `MEMORY.md`
- `global/CLAUDE.md`
- `group/CLAUDE.md`
- system prompt 強化規則
- Qwen 特例規則

這種設計讓 agent 比較不容易失憶，但代價是：

- token 大
- 首 token 慢
- tool 決策變繞
- 每輪都在重建大 prompt

### 4.5 工具循環是多輪 reasoning-heavy 設計

目前 agent 不是「一次生成」，而是 multi-turn tool loop。

且 `MAX_ITER` 是根據 prompt 長度與關鍵字做 heuristic，Level B 預設可到 20 輪。

這很適合做複雜 coding / debugging / orchestration，但不適合每個互動都追求快感。

### 4.6 預設 backend 選擇偏成本/兼容，不偏最佳互動體感

目前 backend priority 是：

- NIM / OpenAI-compatible
- Claude
- Gemini

如果沒有前兩者，就落到 Gemini default。這比較像成本/可用性導向的策略，不是「互動俐落度優先」。

### 4.7 系統把 correctness 放在 UX 前面

從 `STABILITY_ANALYSIS.md` 看得很明顯，近幾個 phase 大量工作都在修：

- race condition
- pipe / marker 問題
- backpressure
- watchdog
- token revocation
- DB integrity
- UTF-8 / OOM / timeout / zombie process

這表示系統的大量設計負擔都在「避免錯、避免掛、避免誤導」。
這是好事，但也代表它不是從頭以「極致順手」為第一優先。

## 5. 與 nanoclaw / hermes 類產品的體感差異

這裡只做合理推斷，不聲稱我已審完它們內部實作。

流暢度較好的 agent，常見特徵通常是：

- 長駐 runtime
- 直接 SDK call
- 輕量 prompt
- 較少 orchestration hop
- token / progress streaming
- 互動任務與背景任務明確分流

EvoClaw 目前則是：

- 每輪 container job
- host 與 runtime 分離
- file IPC 歷史包袱
- prompt 重
- queue / scheduler / memory / watchdog 都在同條主路徑附近

所以體感慢是預期結果，不是意外。

## 6. 根因分類

### 6.1 執行模型問題

- 每回合 `docker run`
- process 非長駐
- SDK 無法持續熱身
- 工具與對話狀態缺少 session retention

### 6.2 通訊模型問題

- stdin/stdout marker 協定
- file IPC / polling
- 非 event-stream 原生

### 6.3 狀態管理問題

- 過多狀態透過 prompt 重建
- 長期記憶與當前工作狀態沒有明確分層
- conversation history 以「重放文字」為主，不是「結構化對話狀態」

### 6.4 調度策略問題

- 任務優先於訊息
- 背景任務與互動訊息共用資源池
- group-level serialization 過於保守

### 6.5 產品層 UX 問題

- 真正的 token streaming 不完整
- 工具進度不夠可見
- 缺少 fast / deep 模式
- 缺少前台互動與後台任務分層

## 7. 如果不改底層，只能做到什麼程度

如果保留「每輪 Docker container + host orchestration + file IPC 主架構」，可以做的優化主要是：

- queue 優先級調整
- prompt 瘦身
- history / memory summarization
- fast / balanced / deep 模式
- 減少 tool result 回注
- 更好的 streaming UX

這些都會有幫助，但上限有限。

因為最大成本仍然是：

- 冷啟
- IPC
- 重 prompt
- 多 hop

## 8. Daytona 是否適合作為替代執行層

結論：

**適合，但不能當成 Docker CLI 的 drop-in replacement。**

### 8.1 Daytona 的定位

根據官方資料，Daytona 是：

- secure and elastic infrastructure for running AI-generated code
- 以 sandbox 為核心
- 提供 filesystem / process / git / code interpreter / desktop automation
- 支援程式化建立、停止、重啟、封存、刪除 sandbox

官方資料：

- GitHub: https://github.com/daytonaio/daytona
- Docs: https://www.daytona.io/docs/
- Sandbox management: https://www.daytona.io/docs/sandbox-management/

### 8.2 Daytona 為什麼符合這個案子想要的方向

它能提供你目前最缺的幾件事：

1. **長駐 sandbox**
   可用 create / stop / start / archive 管理，不必每輪重建執行環境。

2. **直接 process / fs / git API**
   很多現在靠 Docker mount + stdin/stdout + file IPC 做的事，可以走 SDK。

3. **更像 stateful workspace，而不是 batch job**
   這跟「好用 agent」的方向一致。

### 8.3 Daytona 不是「完全脫離 Docker」

這點要講清楚。

官方文件顯示：

- Daytona built on OCI / Docker compatibility
- 開源自架本身是 Docker Compose
- snapshots 也支援 Docker / OCI image

所以你如果問的是：

**「我能不能不要在 EvoClaw 裡直接維護 Docker CLI batch lifecycle？」**

答案是：可以，用 Daytona 很合理。

但如果問的是：

**「整個底層完全不再和 Docker 生態有關？」**

答案是：不是 Daytona 的主要用法。

## 9. Daytona 對這個專案的實際收益

### 9.1 最有價值的收益

1. 移除每回合 `docker run` 冷啟
2. 讓每個 group/session 有長駐 workspace
3. 讓 agent 執行環境具備持久檔案系統
4. 讓 process / fs / git 可以 API 化
5. 讓 IPC 從檔案輪詢逐步轉成 event / SDK call

### 9.2 哪些收益不是自動得到的

即便換 Daytona，下列問題仍要自己做：

- GroupQueue 優先級
- prompt 瘦身
- history / memory state 化
- front/background 任務分流
- token / progress streaming

也就是說：

**Daytona 解的是執行層與 session 模型，不會自動修正產品層設計。**

## 10. 不建議怎麼換

### 10.1 不建議直接把 Docker 全刪掉

因為目前大量穩定性設計都綁在 `container_runner.py` 路徑。
一口氣全改，風險太高。

### 10.2 不建議把 Docker CLI 字串直接改成 Daytona API

這樣會保留原有 batch 思維，收益很小。

正確做法是：

- 抽象 runtime interface
- 保留 DockerRuntime
- 新增 DaytonaRuntime
- 逐步切換主對話路徑

### 10.3 不建議一開始就遷 scheduler / subagent / memory bus

先把主互動路徑做好，再處理背景任務。

## 11. 建議的完整改善計畫

以下分成 4 個 phase。

### 11A. Phase 0: 先量化，不要先猜

時間：3-5 天

先加監測，否則後面所有優化都是憑感覺。

#### 指標

- TTFT: 使用者送出到第一個 token / 第一個進度事件
- TTR: 使用者送出到完整答案
- Cold start: runtime 啟動到 agent 開始處理
- Queue wait time: 進 queue 到真正開始執行
- Prompt size: system/history/memory chars 或 tokens
- Tool turns per answer
- Sandbox/container reuse rate

#### 目標

- TTFT < 1.5s
- 一般問答 TTR < 4s
- 複雜任務 first progress < 2s
- P95 queue wait < 300ms
- prompt 平均大小下降 40%

### 11B. Phase 1: 不改底層的快速止血

時間：1-2 週

#### 1. 調整 GroupQueue 優先級

把互動訊息設為最高優先。

建議改成：

- interactive user message
- tool continuation
- subagent continuation
- scheduled task / dev task

並保留至少 1 個全域 slot 給互動訊息，不讓 scheduler 吃光。

#### 2. prompt 瘦身

直接調整 policy：

- `MEMORY.md` 注入從 4000 chars 降到 1000-1500 chars
- `CLAUDE.md` 不全文注入，改為編譯後摘要 capsule
- conversation history 改為 token budget 控制
- 最近對話只保留 6-10 turns
- 舊對話先 summary，再注入

#### 3. 新增 `fast / balanced / deep` 模式

建議：

- `fast`: `MAX_ITER=4`，禁用 subagent，history 最小化
- `balanced`: 現行預設
- `deep`: 開完整 reasoning / tool loop

#### 4. 工具結果統一 summary 回注

原則：

- raw output 不直接塞回主模型 history
- 只回傳 summary: changed / found / next
- 大輸出只保留 debug head/tail

#### 5. Windows 環境止血

如果短期仍要在 Windows 跑：

- 建議 host 改跑在 Linux / WSL2
- 或先把 `IPC_POLL_INTERVAL` 調低到 0.2-0.3s 過渡

### 11C. Phase 2: 執行層重構

時間：3-6 週

這是最關鍵的一層。

#### 1. 抽象 runtime interface

先從 `host/container_runner.py` 抽出介面：

```python
class AgentRuntime(Protocol):
    async def create_session(...)
    async def run_turn(...)
    async def stop_session(...)
    async def delete_session(...)
    async def get_stats(...)
```

#### 2. 保留 `DockerRuntime`

先把現在的 `container_runner.py` 包成 `DockerRuntime`，功能不變。

#### 3. 新增 `DaytonaRuntime`

用 Daytona sandbox 取代 `docker run` batch model。

基本設計：

- 一個 group 對應一個 sandbox
- sandbox workdir 對應 group workspace
- agent runner 以長駐 process 或可重複呼叫 process command 執行
- 檔案同步、process 執行、git 操作都改走 Daytona SDK

#### 4. 主對話路徑先切 Daytona

先只切最重要的：

- interactive user messages

保留 background task 先走 DockerRuntime，降低風險。

### 11D. Phase 3: 拔掉 file IPC，改 event / SDK 模型

時間：3-5 週

#### 1. 拔掉主路徑對 `ipc_watcher.py` 的依賴

把以下互動改為 SDK / runtime 內事件：

- task payload
- tool progress
- result delivery
- memory update
- subagent return

#### 2. file IPC 只留 fallback

保留檔案式 IPC 作為：

- disaster fallback
- offline 模式
- legacy compatibility

但不再作為主路徑。

#### 3. 讓 Host 能直接拿 progress stream

理想狀態：

- model started
- tool started
- tool finished
- summary ready
- final answer

都變成 event，不再等整包完成。

### 11E. Phase 4: 真正做成 stateful agent runtime

時間：4-8 週

#### 1. session 長駐

不要每回合重跑完整 agent 初始化。

改成：

- sandbox 長駐
- agent process 長駐
- session state 留在 runtime memory

#### 2. prompt-first 改成 state-first

目前很多資訊靠 prompt 重建。
未來應改成：

- identity state
- current task state
- pending actions
- memory summary
- unresolved questions

這些由 runtime state 維護，而不是全文重播。

#### 3. 將 MEMORY / CLAUDE / soul 轉成 capsule

建立一個 build step：

- source markdown
- compile to policy capsule
- inject compact form only

#### 4. 前台聊天與後台任務完全分離

建立不同 worker pool：

- interactive pool
- background task pool
- subagent pool
- summarization pool

避免聊天被背景任務卡死。

## 12. Daytona 遷移的實際技術設計

### 12A. 遷移目標

只替換執行層，不一開始就推翻整個 Host。

保留：

- `host/main.py`
- `host/group_queue.py`
- `host/task_scheduler.py`
- `host/db.py`
- channels

替換：

- `host/container_runner.py` 的底層執行模型

### 12B. 建議的 sandbox 模型

#### 單位

- 1 個 group = 1 個 Daytona sandbox

#### 狀態

- active: 近期有互動，長駐
- stopped: 閒置但保留檔案系統
- archived: 久未使用，降成本

#### 對應資料

- `sandbox_id`
- `group_folder`
- `last_active_at`
- `runtime_state`
- `sandbox_backend = docker | daytona`

可新增資料表或放進現有 state store。

### 12C. 最小可行 DaytonaRuntime

第一版只做 5 件事：

1. create/find sandbox
2. sync workspace
3. execute agent turn
4. collect stdout/stderr/result
5. stop/delete sandbox

第一版先容忍：

- 還沒有真正 token stream
- 還沒有 process 常駐
- 還沒有完全去除 legacy prompt 組裝

目標是先證明：

**同一 sandbox 反覆執行，比每輪 `docker run` 快。**

### 12D. 第二版 DaytonaRuntime

完成後要升級成：

- sandbox reuse
- process reuse
- stateful session
- event streaming
- direct file/process API

## 13. 具體工程拆解

### 13A. 第 1 週

- 加 latency / prompt / queue metrics
- 實作 fast/balanced/deep 模式
- 調整 GroupQueue 優先級
- 限縮 memory/history 注入

交付：

- metrics dashboard
- P50/P95 latency baseline

### 13B. 第 2 週

- 抽 `AgentRuntime` 介面
- 把現有 Docker 路徑包成 `DockerRuntime`
- 寫 runtime adapter tests

交付：

- Host 對 Docker 路徑不再直接依賴實作細節

### 13C. 第 3-4 週

- 新增 `DaytonaRuntime` MVP
- 做 sandbox create/find/delete
- 跑通單輪互動訊息

交付：

- 單一 group 可用 Daytona 路徑回應

### 13D. 第 5-6 週

- 做 sandbox reuse
- 補 runtime state persistence
- 將主對話路徑切到 Daytona

交付：

- 主互動路徑不再每回合 `docker run`

### 13E. 第 7-8 週

- 移除主路徑 file IPC
- 增加 progress events
- subagent 路徑改 API 化

交付：

- `ipc_watcher.py` 不再處於主互動關鍵路徑

### 13F. 第 9-12 週

- process 常駐
- stateful session
- prompt capsule 化
- background pool 與 interactive pool 分離

交付：

- 體感大幅改善

## 14. 預期效益排序

若只看體感收益，排序如下：

1. **每回合 Docker batch -> 長駐 sandbox session**
2. **file IPC/polling -> event / API**
3. **prompt 瘦身**
4. **interactive 優先級**
5. **streaming UX**
6. **background 與 foreground 分流**

## 15. 風險與代價

### 15.1 引入 Daytona 會新增平台依賴

你要接受：

- Daytona SDK / API
- sandbox lifecycle 管理
- 新的部署與觀測面

### 15.2 不是一天完成

這不是「改幾行 Docker 指令」。
它本質上是 runtime model migration。

### 15.3 要有 fallback

建議長期保留：

- `DockerRuntime`
- feature flag
- per-group backend selection

避免 Daytona 出現平台問題時整站回不來。

## 16. 最務實的決策建議

### 如果你只想短期變順

先做：

- Phase 0
- Phase 1

不要立刻碰 Daytona。

### 如果你想中長期把 EvoClaw 變成真正好用的 agent

要做：

- runtime adapter
- DaytonaRuntime
- session-based sandbox reuse
- event 化通訊

### 如果你問「值不值得改 Daytona」

我的結論是：

**值得，但前提是你把它視為『執行模型升級』，不是『Docker 指令替換』。**

## 17. 最終結論

EvoClaw 目前流暢度不如 `nanoclaw` / `hermes agent`，核心不是模型差，而是：

- 執行模型太重
- 主路徑太長
- IPC 太舊
- prompt 太胖
- queue 與 background 任務壓住互動體感

若要真正變好用，關鍵路線是：

1. 先做觀測與 prompt/queue 快修
2. 抽 runtime interface
3. 把主執行層從 Docker batch 模式升級為 Daytona sandbox session
4. 把檔案 IPC 改成 event / API
5. 最終把系統做成長駐 stateful agent runtime

## 18. 外部參考

以下是本分析用到的 Daytona 官方資料：

- Daytona GitHub: https://github.com/daytonaio/daytona
- Daytona Docs: https://www.daytona.io/docs/
- Sandbox Management: https://www.daytona.io/docs/sandbox-management/
- Python Sandbox SDK: https://www.daytona.io/docs/en/python-sdk/sync/sandbox/
- Go SDK: https://www.daytona.io/docs/en/go-sdk/daytona/
- OSS Deployment: https://www.daytona.io/docs/ja/oss-deployment/
- Snapshots: https://www.daytona.io/docs/en/snapshots/

---

## 19. 可行性審視（2026-05-15 加入）

以下章節由 Claude Code session 對照當前 repo `keepinmind2054/evoclaw` HEAD `e999603` 之後的 main 進行驗證，列出每個 phase 的事實確認、實際估時、與原計畫的落差。

### 19.1 事實主張對照

| 文件主張 | 對照位置 | 驗證結果 |
|---|---|---|
| 每輪 `docker run` 冷啟 | `host/container_runner.py:623` 起的 `_run_docker` | ✓ 屬實 |
| File IPC + polling，Windows fallback | `host/ipc_watcher.py:162` `_IPC_MAX_FILES_PER_CYCLE`，Linux inotify 條件分支 | ✓ 屬實 |
| GroupQueue 序列化 + tasks 優先 | `host/group_queue.py`；`MAX_CONCURRENT_CONTAINERS` 與 task-vs-message 排序 | ✓ 屬實 |
| Prompt 胖 — stdin JSON 帶 history/memory/hints/secrets | `host/container_runner.py:582` 起的 input 組裝 | ✓ 屬實 |
| Multi-turn loop MAX_ITER=20 (Level B) | `container/agent-runner/agent.py:457` heuristic | ✓ 屬實 |
| Backend priority NIM/OpenAI > Claude > Gemini | `container/agent-runner/agent.py:220, 620` | ✓ 屬實 |
| Daytona 自架 = Docker Compose | 官方 `oss-deployment` 文件 | ✓ 屬實 — 換層抽象，未脫離 Docker 生態 |

文件對 code 的描述全部屬實，無幻覺。

### 19.2 Phase 可行性與時程實算

| Phase | 文件估 | 實算估 | 偏差 | 風險 | 主要不確定性 |
|---|---|---|---|---|---|
| 0 — Metrics | 3-5 天 | **3-5 天** | 持平 | 低 | dashboard 已存在，加 timing 注入即可 |
| 1 — Queue priority + prompt slim + fast/balanced/deep | 1-2 週 | **2-3 週** | +50% | 低 | code 局部改；fast 模式需要新 flag 在 stdin payload 與 agent.py 兩端 |
| 2 — AgentRuntime interface + Daytona MVP | 3-6 週 | **4-8 週** | +33% | **高** | (a) Daytona OSS 自架 = Docker Compose stack 新元件；(b) workspace 同步機制要寫；(c) `container_runner.py` 安全 invariant 多（path traversal guard, env shadow, oom_score_adj, pids limit, tmpfs）需逐項移植到 Daytona |
| 3 — Kill file IPC | 3-5 週 | **6-8 週** | +60% | **高** | IPC schema 已有 backpressure / retry / security guards / per-cycle cap。事件化要 bridge legacy。Scheduler / subagent 同條路徑會被牽動 |
| 4 — Long-lived runtime + capsule prompt | 4-8 週 | **8-12 週** | +50% | **最高** | (a) agent.py 主迴圈大改；(b) session state 持久化需要新 storage schema；(c) prompt compile pipeline 是新子系統；(d) 4 個 worker pool 對 host event loop 影響面廣 |

**文件估**: 12-21 週。**實算**: 20-31 週。**樂觀 30-50%**。

### 19.3 文件漏寫的關鍵風險

1. **Daytona OSS 沒擺脫 Docker** — 官方 OSS 部署是 Docker Compose stack，目前 EvoClaw 部署是 pm2 + `docker run`。換 Daytona 等於再加一層編排，運維面變大不變小。若不走 Daytona Cloud SaaS，就少了 Daytona 最大的「免運維」賣點。

2. **Windows host 上的 Daytona 體驗未驗證** — Daytona 主推 Linux 雲端。本地 Windows 開發環境跑 Daytona OSS（Docker Desktop on WSL2 中又跑一層 Docker Compose 中再起 sandbox）的延遲、穩定性無第一手資料。MVP 階段建議先在 Linux/WSL2 host 驗 PoC。

3. **無 regression 安全網** — repo 內 `python-tests` workflow 持續 hang 到 6h timeout（pre-existing）。這麼大規模的 runtime refactor 沒有自動 regression net 是高風險。建議 Phase 2 啟動前先補 `python-tests` 修綠（或起碼讓 critical-path 測試獨立執行）。

4. **沒提到今天剛修的 issue** — `#583` (MEMORY.md XML leak)、`#584` (self_update 幻覺)、`#585` (repo 分析 parrot README)、`#586` (introspection OOM loop)、`#587` (#538 mitigation A+C)、`#588`–`#594` 都在 2026-05-14/15 merged。其中一部分「不順」感是這些 bug 造成的；流暢度量測（Phase 0）應在這些修完之後重新 baseline，避免錯把已修 bug 算進 runtime 模型問題。

5. **#538 OOM 沒列為前置條件** — 文件假設「流暢度」與「OOM 頻率」可獨立處理。實際上 OOM-kill 是流暢度殺手（每次 OOM = 整輪訊息丟失 + 用戶看到錯誤訊息）。Phase 0 metrics 應同時量 TTFT/TTR 與 OOM rate，否則「流暢度改善」可能被 OOM 抵消。

### 19.4 修正後的建議

**立即啟動（低風險、解 50-70% 流暢度體感）**:
- Phase 0 (3-5 天) — metrics 是無腦贏；先取得 baseline
- Phase 1 (2-3 週) — queue priority、prompt slim、fast/balanced/deep 局部改、無架構風險

**緩做（高風險、需先驗證）**:
- Phase 2-4（Daytona + IPC + stateful runtime）— 5-7 個月實際投入
- 前置條件：(a) Phase 0 metrics 有真實數字、(b) `python-tests` 修綠或補關鍵 regression、(c) Linux/WSL2 PoC 驗過 Daytona 體感勝過 `docker run`

**該砍**:
- Daytona OSS 自架方案 — 換湯不換藥（仍 Docker Compose）；要嘛走 Daytona Cloud SaaS 賭依賴，要嘛別走 Daytona。中間態最差。

## 20. 12 個月決策節點

不要把整個 Phase 0–4 當成一條必走的路。每個 phase 完成後設一個 go/no-go：

| 節點 | 判斷依據 | Go 條件 | No-go 條件 |
|---|---|---|---|
| Phase 0 → 1 | metrics baseline | TTFT > 3s 或 OOM rate > 1/hr | 兩者都已達標，停在 Phase 0 觀察 |
| Phase 1 → 2 | Phase 1 後體感量測 | TTFT 仍 > 1.5s 或用戶仍抱怨 | TTFT < 1.5s 且 4 週無新流暢度抱怨，停 Phase 1 |
| Phase 2 → 3 | DaytonaRuntime MVP 與 DockerRuntime A/B | Daytona 路徑 TTFT 至少快 30% | Daytona 路徑慢於或等於 Docker，砍 Daytona、繼續 Docker + 改 IPC |
| Phase 3 → 4 | IPC 事件化後 P95 latency | P95 仍 > 2s | P95 < 1.5s 且用戶滿意，停在 Phase 3 |

每個節點 no-go 都是合法選項，不是失敗。

## 21. 與其他文件的對照

- `docs/PRODUCT_STRATEGY.md` 提出產品定位（群組型自治平台），技術路線必須服務此定位。若產品定位確定不打 coding agent 戰場，則 Phase 4「長駐 stateful runtime」的優先級高於「streaming UX」（後者偏 coding agent UX）。
- `docs/ARCHITECTURE.md` 描述當前 v1.x file IPC 與計畫中 v2.x WebSocket。本文件 Phase 3 與該計畫對齊但更具體。
- `docs/STABILITY_ANALYSIS.md` 列舉穩定性修補。Phase 0 metrics 不應只量延遲，也應記錄穩定性事件（OOM、container kill、IPC backpressure），否則「快」可能犧牲「穩」。

## 22. 一句話結論

原文件分析準確、方向正確。**Phase 0 + Phase 1 立刻做，Phase 2-4 緩做且設明確 no-go 節點。** Daytona 不是 silver bullet，是「runtime 模型升級」的一個候選實作；若 Phase 1 已解大半問題，Phase 2-4 可暫不啟動。

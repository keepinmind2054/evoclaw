# EvoClaw 殺出重圍的產品戰略

更新日期: 2026-05-15
最近審視: 2026-05-15（加入可行性審視段，章節 14–17）

## 1. 結論先講

EvoClaw 不應該繼續朝「另一個更快的通用 coding agent」發展。

那條路線會直接和 `nanoclaw`、`hermes agent`、Claude Code 類產品正面競爭，而 EvoClaw 現階段的系統優勢不在那裡。它的強項不是極致輕快的單回合互動，而是：

- 長期運行
- 多群組 / 多任務 orchestration
- 記憶、規則、排程、治理
- 可觀測、可管理、可演化

因此 EvoClaw 的正確發展方向，不是成為更像 IDE coding assistant 的產品，而是成為：

**可長期運行、可治理、可多角色協作的群組型自治 Agent 平台。**

你前一輪提到的 `1 + 2`，應該直接定義成產品主軸：

1. 群組型自治 Agent 平台
2. 長任務運營型 Agent

這兩條不是分開做，而是要合併成同一個核心敘事：

**EvoClaw = Persistent Multi-Agent Operations Platform**

更貼近中文產品語言可以表述為：

**EvoClaw 是一個可長期運行的群組自治 Agent 作業平台，能在多人環境中持續執行任務、記住脈絡、遵守規則，並被管理者治理。**

## 2. 為什麼不能正面拼通用 coding agent

如果產品敘事停留在「會聊天、會改 code、會跑工具」，EvoClaw 會很難殺出來，原因有三個：

1. 體感上，現有架構不是為最低延遲設計
2. 市場上，通用 coding agent 的期待值已被更輕的產品定義
3. 差異化上，EvoClaw 的稀缺能力其實在自治、治理、長任務，不在單回合聊天

前一份技術分析 (`docs/FLUIDITY_AND_DAYTONA_PLAN.md`) 已經指出，目前流暢度受限於幾個結構性問題：

- 每回合 `docker run` 的冷啟路徑
- file IPC / polling
- queue 與 scheduler 對前台互動的干擾
- prompt 過胖
- 長工具鏈與多輪 reasoning loop

這些都可以改善，但即使改善後，EvoClaw 最有機會建立護城河的，仍然不是「和所有 coding agent 比誰更像 IDE 裡的即時助手」。

## 3. 正確定位

### 3.1 產品一句話

**一個可長期運行、可管理、可多角色協作的群組自治 Agent 平台。**

### 3.2 三個核心標籤

產品訊息需要收斂成三個很容易理解的詞：

- `Persistent`：不是一次性問答，而是持續運行
- `Multi-Agent`：不是單 agent，而是可角色分工
- `Governable`：不是黑盒，而是可管理、可審計、可控

這三個標籤比「更快」、「更聰明」、「更多功能」更有辨識度。

### 3.3 產品類別

EvoClaw 不應定義成：

- AI 聊天機器人
- coding assistant
- Discord bot framework

更適合的類別是：

- Agent Operating System
- Multi-Agent Automation Platform
- Autonomous Group Agent Runtime

如果對外敘事要更務實，可以用：

**Self-hostable Agent Operations Platform for Teams and Communities**

## 4. 核心主軸：把 1 + 2 合併成真正的戰場

### 4.1 主軸一：群組型自治 Agent 平台

這條線代表 EvoClaw 不是服務單一使用者，而是服務一個群組、一個社群、一個團隊空間。

典型能力包括：

- 群組長期記憶
- 多角色 agent 協作
- 群規 / 回應規則 / 權限邏輯
- 排程與持續巡檢
- 對話之外的背景任務
- 管理者介面與操作記錄

這會讓 EvoClaw 和一般「聊天機器人」或「單人 coding agent」拉開距離。

### 4.2 主軸二：長任務運營型 Agent

這條線代表 EvoClaw 不是只在有人發問時才動，而是能持續執行任務與回報。

典型能力包括：

- 每日 / 每週巡檢
- 情報蒐集與整理
- 長步驟工作流
- 背景執行後再回報
- 多來源資訊整合
- 狀態追蹤與失敗重試

這是一般即時對話 agent 不一定擅長、但 EvoClaw 很適合發展的方向。

### 4.3 為什麼 1 + 2 要綁在一起

如果只有群組自治，產品容易被理解成「比較複雜的聊天 bot」。

如果只有長任務運營，產品又容易被理解成「工作流排程器加 LLM」。

把兩者綁起來，才會形成真正的差異化：

**在群組 / 團隊場景中，長期自治地執行任務、累積記憶、接受治理、持續產出價值。**

這是比「更像另一個 coding agent」更有防守力的定位。

## 5. 目標客群

不要一開始把所有人都當目標。應先鎖定最可能持續使用、且最能體現 EvoClaw 優勢的客群。

### 5.1 第一優先客群

- Telegram / Discord 社群經營者
- 小型遠端團隊
- 需要長期整理資訊的研究型團隊
- 自架工具接受度高的技術型社群

這些人共同特徵是：

- 不是只想問答
- 願意設規則
- 需要持續運行
- 願意接受 dashboard / 管理介面

### 5.2 第二優先客群

- 內容社群營運者
- 顧問 / 研究 / 投資情報團隊
- AI native startup 內部營運團隊

### 5.3 暫時不要優先的客群

- 只想要最快聊天回覆的一般使用者
- 純 IDE coding assistant 使用者
- 對部署與設定零容忍的非技術客群

## 6. 差異化主張

EvoClaw 要能用一句話說清楚，自己和主流 agent 有什麼不一樣。

### 6.1 不要主打的差異

以下敘事不夠強，也容易被更成熟產品壓過去：

- 我們也能改 code
- 我們也有工具調用
- 我們也支援多模型
- 我們也能聊天

### 6.2 應該主打的差異

真正值得打的差異是：

- `長期自治`：不是問一次答一次，而是持續幫你做事
- `群組原生`：不是只對一個人服務，而是服務一個共同空間
- `治理能力`：不是不可控黑盒，而是可設規則、可追蹤、可管理
- `可自架`：不是只能用 SaaS，而是能掌握資料與執行環境

### 6.3 對外敘事範例

可以考慮以下方向：

`EvoClaw is not just an AI agent that answers. It is an agent system that keeps working.`

中文版本：

`EvoClaw 不是只會回答的 AI，而是一個會持續運作、可被治理的群組自治 Agent 系統。`

## 7. 產品線設計

不要只交付 framework，要交付「可直接用的方案包」。

### 7.1 優先做的 3 個 Solution Pack

#### A. Community Manager Agent

適用於 Telegram / Discord 社群。

能力包含：

- 自動整理群聊重點
- FAQ 回覆建議
- 公告整理
- 每日摘要
- 關鍵議題追蹤
- 管理者輔助

#### B. Research Ops Agent

適用於研究、投資、知識工作團隊。

能力包含：

- 追蹤主題
- 每日彙整資訊
- 長期建立知識庫
- 定時生成 brief
- 異常或新訊號提醒

#### C. Team Operations Agent

適用於小型團隊協作。

能力包含：

- 任務提醒
- 例行巡檢
- 文件整理
- issue / commit / release note 彙整
- 團隊知識脈絡保留

### 7.2 為什麼要先做方案包

如果只賣平台，使用者要先理解太多架構概念，導入成本高。

如果先給 solution pack，使用者看到的是：

- 具體場景
- 立即價值
- 可複製模板

這會比單純展示框架能力更容易獲得 adoption。

## 8. 產品取捨

要殺出重圍，不能什麼都做。下面是建議的取捨。

### 8.1 應優先投入

- 長期自治能力
- 多群組 / 多任務管理
- 背景任務與前台對話分離
- dashboard / 可觀測性
- rule / memory / identity 管理
- 部署與模板化

### 8.2 應降優先

- 和 IDE coding assistant 的正面功能對齊
- 大量泛用聊天功能
- 沒有場景指向的 agent 花式能力
- 只服務單一使用者的互動設計

### 8.3 需要補最低門檻

雖然不該正面拼通用 agent，但這些底線還是要補，不然使用者根本不會留下：

- 前台互動流暢度
- token / progress streaming
- 更簡單的安裝與部署
- 更清楚的執行可見性
- fast / balanced / deep 模式切換

## 9. 與技術規劃的銜接

前一份文件 `docs/FLUIDITY_AND_DAYTONA_PLAN.md` 解的是「怎麼變順、要不要用 Daytona、底層如何改」。

這份產品戰略解的是「就算底層變順了，EvoClaw 應該往哪裡打」。

兩份文件的關係應該是：

- 技術規劃負責把體感拉到可用門檻
- 產品戰略負責決定不要把可用門檻浪費在錯誤戰場

換句話說：

- **技術改善是必要條件**
- **產品定位才是勝負手**

## 10. 6 個月發展路線

### Phase 1：重新定義產品敘事（第 1-4 週）

目標：把 EvoClaw 從「功能很多的 agent 專案」改成「定位清楚的產品」。

要做的事：

- 確認對外一句話定位
- 內部文件統一 `Persistent / Multi-Agent / Governable`
- 官網 / README / Demo 敘事全部收斂
- 明確放棄「另一個通用 coding agent」的表達

交付物：

- 新版 README
- Landing page 文案
- 3 個場景說明頁

### Phase 2：補足可試用門檻（第 2-8 週）

目標：讓使用者第一次用，不會先被笨重體感趕走。

要做的事：

- 依技術規劃先做流暢度快修
- 提升前台互動優先級
- prompt 瘦身
- progress / token streaming
- 安裝部署簡化

核心衡量：

- 首次回應速度
- 安裝成功率
- 首次設定完成率

### Phase 3：推出 2-3 個 solution pack（第 2-4 個月）

目標：從平台能力轉成具體價值。

優先建議：

- Community Manager Agent
- Research Ops Agent
- Team Operations Agent

核心衡量：

- 每個方案是否能在 30 分鐘內部署
- 是否能在 3 天內產出明顯價值
- 是否能形成可重複使用模板

### Phase 4：強化長期運行能力（第 3-5 個月）

目標：把 EvoClaw 從「能跑」升級到「適合長期掛著」。

要做的事：

- 背景任務與前台對話分離
- 更好的 runtime session 保持
- 多 agent 分工與狀態顯示
- 失敗重試與觀測強化
- 管理者治理流程完善

### Phase 5：驗證真正 PMF 訊號（第 5-6 個月）

目標：找出哪個場景最有機會成為主產品。

要看的指標：

- 每週活躍群組數
- 每群背景任務執行次數
- 每群每週被主動打開 dashboard 的次數
- retention
- 管理者設定完成率
- 模板重複部署數

## 11. 商業模式方向

商業化不要太晚想，因為產品定位會直接影響技術路線。

### 11.1 最合理的三種模式

#### A. Open-source core + hosted management

開源核心，收費在：

- 管理介面
- 託管 runtime
- 團隊管理能力
- 可觀測與治理套件

#### B. Self-hosted enterprise

適合重視資料與合規的團隊，收費在：

- 企業部署
- 管理功能
- 進階權限治理
- 支援服務

#### C. Solution pack / template marketplace

如果場景跑出來，可以賣：

- 產業模板
- agent pack
- workflow pack
- 管理策略包

### 11.2 與 Daytona / runtime 路線的關係

如果未來真的往 Daytona 或長駐 sandbox runtime 發展，就有條件延伸成：

- hosted sandbox
- managed session runtime
- team agent hosting

但這要建立在產品定位已經清楚，不然只會變成更昂貴的基礎設施負擔。

## 12. 最重要的三個戰略原則

### 原則一：不要做另一個更快的 Claude Code

那會把你帶去最擁擠、最難贏、而且不符合現有資產的戰場。

### 原則二：用「群組自治 + 長任務運營」組成真正差異化

這是你指定的 `1 + 2`，也是這份策略的核心。

它讓 EvoClaw 從聊天工具，升級成持續運作的 agent 系統。

### 原則三：技術改善是為了支撐定位，不是取代定位

流暢度一定要補，但補流暢度不是為了證明自己也能當通用 coding agent，而是為了讓核心產品價值能被真正使用。

## 13. 一句話版結論

EvoClaw 最有機會殺出重圍的方式，不是做成「另一個更快的 agent」，而是做成：

**一個可長期運行、可治理、可多角色協作的群組自治 Agent 平台，專門處理群組場景中的長任務與持續運營。**

---

## 14. 可行性審視（2026-05-15 加入）

本章對照當前 repo `keepinmind2054/evoclaw` 主分支 HEAD `e999603` 之後的 code，逐項驗證戰略賣點背後的實作支撐度。

### 14.1 三個核心標籤的 code 支撐

| Strategy 賣點 | Code 現況 | Gap |
|---|---|---|
| **Persistent (長駐)** | `host/db.py` 內 `scheduled_tasks` 表存在；`host/task_scheduler.py` 60s 輪詢；支援 cron / interval / once | session continuity 弱 — 每 turn cold container（見 `FLUIDITY_AND_DAYTONA_PLAN.md` Phase 4） |
| **Multi-Agent** | `container/agent-runner/_tools.py` 提供 `mcp__evoclaw__run_agent` 工具；可 spawn subagent | 實際生產 workflow 沒見到多 agent 協作的成熟用例，多用於 single-agent + 偶發 subagent delegation |
| **Governable** | `host/identity/bot_registry.py` + `host/rbac/roles.py` 已存；dashboard `host/dashboard.py` 在 port 8765 | Admin UX 簡陋（基本 CRUD 介面，缺 audit log timeline、規則 DSL 編輯器、訊息搜尋） |
| **Group-native** | `registered_groups` table；per-group folder + `MEMORY.md` + `CLAUDE.md`；JID 格式 `tg:` / `wa:` / `dc:` 等 | ✓ 真有，是 EvoClaw 強項 |
| **5-layer Memory** | README 宣稱 Hot → PalaceStore → Vector → Shared → Cold + KnowledgeGraph | `host/memory/` 有 `hot.py`、`warm.py`、`memory_bus.py`、`summarizer.py`、`vector_ingestor.py`、`dream_task.py` — 大部分零件在，但 PalaceStore / KnowledgeGraph 對應檔案薄弱，README 用詞超過 code 現況（參考 issue #585 已開）|
| **Skills 2.0** | README 宣稱熱抽換容器工具 | `data/dynamic_tools` mount 機制存在；實際 skill marketplace / 載入流程是 minimal viable |
| **DevEngine 7 階段** | README 宣稱 | `host/dev_engine` 不存在；`tests/test_dev_engine.py` 有測但對應實作待考 — README aspirational |

**結論**: 群組原生 + 排程 + 多後端 + RBAC + dashboard 真有，是強項。但 5-layer memory / KnowledgeGraph / DevEngine / Skills 2.0 在 README 中的呈現超過 code 的成熟度。戰略文件若繼續引用這些賣點，必須補 code 或調整 README（與 `#585` 同一條線）。

### 14.2 Solution Pack 可行性

每個 Pack 的「30 分鐘部署、3 天看到價值」標準很激進。對照零件存量：

| Pack | 既有零件支持度 | 缺什麼 | 估開發量 |
|---|---|---|---|
| Community Manager | `host/channels/telegram_channel.py` + `discord_channel.py` + `task_scheduler` + `host/evolution/immune.py` (anti-injection)；訊息存 SQLite | 對話摘要 prompt 範本；群規 DSL（目前只有 free-form `CLAUDE.md`）；FAQ store；公告整理工作流 | **50-80 小時** |
| Research Ops | `WebFetch` tool + `task_scheduler` + `host/memory` + `host/memory/summarizer` | RSS / source 訂閱機制（目前沒）；brief 模板；信號偵測規則；長期知識庫的 indexing pipeline | **80-150 小時** |
| Team Operations | `host/enterprise/jira_connector.py` + GitHub 認證 + `task_scheduler` | issue 彙整 prompt；release-note 模板；commit/PR 抓取流程；團隊知識保留 schema | **60-100 小時** |

3 個 Pack 合計 **190-330 小時** (5-8 週純開發)。Strategy 把這個排在第 2-4 個月，時程合理但要全職投入。

**「30 分鐘內部署」風險**: 目前 setup 流程是 `python setup/setup.py` + 編輯 `.env` (今天剛擴成 57+ vars 見 PR #582) + `python scripts/register_group.py` + `docker build`。新手照做超過 30 分鐘無懸念。若要達標需做 setup wizard CLI 或 web onboarding flow，這本身是 30-50 小時工程。

### 14.3 商業模式時程的合理性

文件 11.1 列三種模式 (OSS core + hosted management、self-hosted enterprise、template marketplace)，但放在 6 個月路線的後半。

實算：

- OSS core + hosted management 需要：(a) 穩定的 hosted runtime infra (=Phase 2-4 of fluidity plan, 5-7 個月)、(b) 計費 + 訂閱系統（新建）、(c) onboarding flow、(d) 客服。**12 個月內可成形是樂觀**。
- Self-hosted enterprise 需要：(a) 企業 packaging (helm chart / docker-compose distro)、(b) SSO / SAML、(c) audit log、(d) license server。**現有 RBAC + identity 是好起點但企業特性差距大**。
- Solution Pack marketplace 需要：(a) 至少 3 個 reference pack 上線且有真實 user、(b) pack 安裝協議、(c) 收費機制。**最依賴前面 Phase 跑通**。

**結論**: 商業模式正確，但時程估計過於樂觀。6 個月內可做到的：定位收斂 + 1-2 個 Pack 上線 + 早期 user 訪談。**6 個月內到收費階段不現實**，建議調整為「6 個月驗 PMF，12-18 個月開始收費」。

### 14.4 文件漏寫的關鍵風險

1. **「不拼 coding agent」與既有 code framing 的矛盾** — README 大量內容是 coding 場景（DevEngine 7 階段、Skills 熱抽換、agent.py tool loop 預設值針對 code task）。要轉成「群組自治」敘事必須砍 README 大半篇幅、重寫 onboarding、調整 default prompts。Phase 1 (1-4 週) 內要做完這些工程量不小，估 **40-60 小時**。

2. **OOM / 流暢度問題還在** — 2026-05-13 telegram transcript 顯示 2 小時內 8 次 OOM。產品戰略文件假設「使用者進來」後體驗順暢，但目前真實情況是用戶看到 `⚠️ 記憶體不足` 訊息頻率不低。今天 PR #587 / #588 / #589 / #591 / #594 修了根因的一部分，但 24 小時 baseline 還沒回收。Phase 2「補足可試用門檻」是整個戰略能落地的前置條件，不是並行項目。

3. **「30 分鐘部署」與當前 setup 矛盾** — 上一節已說。要達標需先做 setup wizard。

4. **OSS competitor 已存在** — Strategy 提的「Persistent Multi-Agent Operations Platform」品類正在被多個 OSS 專案搶（LangGraph 的 long-running agents、AutoGen team mode、CrewAI flows、OpenHands persistent sessions）。文件沒分析 EvoClaw vs 這些的差異。**Governable + Self-hostable + Group-native 是值得打的角度，但需要明確 positioning vs 各 OSS 比較**。

5. **「群組」=「Telegram / Discord 群」 的範式可能受限** — 企業客戶用 Slack / Teams / Lark 居多；Telegram / Discord 偏 indie / 社群。客群選擇影響商業天花板。文件鎖定 Telegram / Discord 是合理的 beachhead，但商業化時要評估是否擴到企業 IM。

### 14.5 修正後的優先序

立即啟動（無風險、立即見效）：
- **改 README + landing 敘事** — 1 週工程量，砍 coding-agent 賣點、補 Persistent/Multi-Agent/Governable 三標籤。Zero code risk。

緊接著（順著今天技術修補）：
- **流暢度量測 baseline** — `FLUIDITY_AND_DAYTONA_PLAN.md` Phase 0，3-5 天。產品戰略不能在沒有 baseline 的情況下談「使用者體驗順暢」。
- **Phase 1 流暢度快修** — 2-3 週。為 Solution Pack 上線打底。

緩做：
- **Solution Pack 開發** — 等 Phase 1 完成 + setup wizard 有雛形再啟動。否則第一個 user 進來看到 OOM 就跑。
- **商業模式具體化** — 6 個月後再談。先驗 PMF 訊號。

該砍：
- 12 個月內到「收費」的計畫 — 過於樂觀，建議調為「12-18 個月」。
- 「3 個 Solution Pack 同時做」— 集中資源做 1 個，跑通後複製。建議先做 Community Manager（門檻最低、即時回饋最快）。

## 15. 與技術文件的對照節點

| 戰略需求 | 對應技術項 (FLUIDITY plan) | 是否阻擋 |
|---|---|---|
| 「使用者進來不會被笨重體感趕走」 | Phase 0 (metrics) + Phase 1 (quick wins) | 是 — 必須先做 |
| 「長期掛著」 | Phase 4 (long-lived runtime) | 中 — 不做也能跑，但流暢度有上限 |
| 「多 agent 分工」 | 已存 (`run_agent` IPC)，但需 streaming UX 才好用 | 否 — Phase 3 (event IPC) 之後再強化 |
| 「治理 / RBAC / audit」 | 已存基礎；需 dashboard UX 強化 | 否 — 獨立 sprint 即可 |
| 「Solution Pack 30 分鐘部署」 | 不在 fluidity plan 範圍 — 需獨立 setup wizard 工作 | 是 — 影響 Phase 3 of strategy |

## 16. 6 個月路線的修正版

不改大方向，調整時程與並行度：

| 月份 | 戰略 Phase | 技術 Phase | 主要交付 |
|---|---|---|---|
| Month 1 | Strategy Phase 1 (敘事收斂) | Fluidity Phase 0 + Phase 1 | 新 README + landing；metrics baseline；prompt slim；fast/balanced/deep |
| Month 2 | Strategy Phase 2 (可試用門檻) | Fluidity Phase 1 收尾 | setup wizard MVP；TTFT < 2s baseline 達標 |
| Month 3 | Strategy Phase 3 (1st Solution Pack — Community Manager) | (持平) | Community Manager v1 上線 |
| Month 4 | Strategy Phase 3 (Pack 反饋迭代) | Fluidity Phase 2 啟動條件評估 | 5-10 個真實 user；OSS reference deployment |
| Month 5 | Strategy Phase 4 (長期運行強化) | Fluidity Phase 2-3（若 go） | 背景/前台分流；可選的 Daytona PoC |
| Month 6 | Strategy Phase 5 (PMF 訊號驗證) | (持平) | retention + 使用量數據；下半年計畫制定 |

**關鍵差異**: 不在 6 個月內 ship 3 個 Pack；不在 6 個月內談收費；不在 6 個月內完成 Daytona 遷移。一切收斂在「敘事 + 流暢度 + 1 個跑得通的 Pack」三件事。

## 17. 一句話結論

原文件方向正確，戰略選擇合理。**但時程估計過樂觀，且假設了「使用者體驗順暢」的前置條件尚未達成。** 立即啟動的是「重 README + 流暢度 metrics + 1 個 Solution Pack」三件套，其餘緩做或砍掉。

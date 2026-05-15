# EvoClaw 執行計畫（6 個月）

更新日期: 2026-05-15
依據: `docs/FLUIDITY_AND_DAYTONA_PLAN.md`、`docs/PRODUCT_STRATEGY.md`

## 0. 一句話目標

**6 個月內把 EvoClaw 從「功能很多但體感笨重的 agent framework」轉成「定位清楚、體感達標、有 1 個 Solution Pack 跑通的群組型自治 Agent 平台」。**

## 1. 約束條件（影響所有時程估算）

- **單人開發**（假設 keith 全職投入；非全職則時程 ×2-3）
- **Windows host + pm2**（部署環境，影響 IPC poll vs inotify）
- **無有效 regression CI**（python-tests workflow 永遠 hang 到 6h timeout — pre-existing，未在本計畫處理）
- **#538 OOM 真實頻率未 baseline**（2026-05-13 transcript 2h 內 8 次 — 已部分緩解 by #587）
- **README 已知超出實作**（DevEngine / 5-layer memory / Skills 2.0 / Agent Swarms 等賣點需在 Strategy Phase 1 處理）
- **Branch protection 無 required check**，可 squash merge anyway（紀錄於 `memory/reference_ci_state.md`）

## 2. 全局排序原則

1. **無風險高回報優先** — 改 README、加 metrics、prompt slim、queue priority
2. **修現存 bug > 建新功能** — `#538` D-H、setup wizard 在新 Pack 之前
3. **單一 Pack 跑通 > 三個 Pack 並行** — Community Manager 為 beachhead
4. **量測 → 改善 → 再量測** — 不憑感覺
5. **Daytona / Phase 2+ runtime refactor 緩做** — 只在 Phase 1 解不掉問題時啟動

## 3. Sprint 規劃（雙週為單位）

### Sprint 1 — Week 1-2 — 敘事收斂 + 量測
**主軸**: Strategy Phase 1 + Fluidity Phase 0

**新 issue 待開**:
| Issue 標題 | 說明 |
|---|---|
| `docs: rewrite README around Persistent / Multi-Agent / Governable` | 砍 coding-agent rhetoric、補三標籤、列實作對應 |
| `docs: audit README claims vs code` | 逐項 grep 確認 `5-layer memory`、`Agent Swarms`、`DevEngine 7-stage`、`Skills 2.0` 的對應實作；缺的標 `(planned)` 或刪 |
| `obs: add latency / prompt-size / queue-wait metrics to dashboard` | TTFT、TTR、cold-start、queue wait、prompt size、tool turns；export 到現有 dashboard |
| `obs: add OOM rate counter to dashboard` | 從 #538 引伸；container exit 137 計次 |

**Gate (Sprint 1 結束)**:
- ✓ 新 README PR merged
- ✓ Metrics dashboard 顯示 TTFT/TTR/queue wait/prompt size 即時數字
- ✓ 取得 baseline（至少 24h 數據）

**Kill 條件**: 若 README rewrite 卡 1 週還沒過，砍 audit 範圍、只重 frontmatter。

---

### Sprint 2 — Week 3-4 — Phase 1 快修
**主軸**: Fluidity Phase 1（不改底層）

**新 issue 待開**:
| Issue 標題 | 說明 |
|---|---|
| `perf(queue): interactive user messages get highest priority in GroupQueue` | `host/group_queue.py`；保留 1 全域 slot 給互動訊息，scheduler 不可獨吞 |
| `perf(prompt): MEMORY.md inject budget 4000 → 1500 chars` | `host/container_runner.py` prompt build；長期記憶改 summarizer pre-process |
| `perf(prompt): history 改 token budget + 6-10 turn 上限` | 老對話 summary 再注入 |
| `feat(agent): fast / balanced / deep 模式 flag` | `fast: MAX_ITER=4, no subagent, minimal history`；balanced 沿用；deep 開全功能。Channel layer 支援 `/fast` `/deep` 切換 |
| `perf(tools): tool result summary 不回注 raw output` | head/tail 限制 + summary line |
| `perf(ipc): Windows IPC_POLL_INTERVAL 1s → 300ms` | 短期 stopgap |

**Gate (Sprint 2 結束)**:
- TTFT P50 下降 ≥ 30%
- prompt 平均大小下降 ≥ 30%
- 24h 內無新 OOM-kill（依賴 #538 mitigations A+C 已 live + 本 sprint prompt slim）

**Kill 條件**: 若 TTFT 仍 > 3s 且 prompt 已減半，啟動 Sprint 3 的 streaming UX；否則延後 Sprint 3 直接進 Sprint 4。

---

### Sprint 3 — Week 5-6 — Streaming UX + Setup Wizard MVP
**主軸**: Strategy Phase 2 「補可試用門檻」

**新 issue 待開**:
| Issue 標題 | 說明 |
|---|---|
| `feat(channel): token / progress streaming to channel` | Telegram 邊 streaming 邊 edit message；Discord 同；fallback 為單訊息 |
| `feat(channel): tool-call progress events to user` | `📨 tool=bash`、`📨 tool=read` 進度行；可隱藏 |
| `feat(setup): one-command setup wizard CLI` | `python -m evoclaw setup` 引導 .env、register first group、build image、verify；目標 30 分鐘從零跑起 |
| `docs: getting-started speedrun` | < 5 頁、無 prerequisite assumption |

**Gate (Sprint 3 結束)**:
- 新 user 從 git clone 到第一個 group 回應 ≤ 30 分鐘（找 1 個 alpha tester 計時）
- TTFT < 1.5s（Sprint 2 已達 +1s 餘裕）

**Kill 條件**: 若 streaming UX 卡 backend API 限制（Gemini 2.0 Flash 串流穩定性），先 ship setup wizard 跳過 streaming，記為 tech debt。

---

### Sprint 4 — Week 7-10 — Community Manager Pack MVP
**主軸**: Strategy Phase 3 第一個 Solution Pack

**新 issue 待開**:
| Issue 標題 | 說明 |
|---|---|
| `pack(community): conversation digest skill` | 每 N 小時 / 每日對群組訊息產 summary；template 化、可調 |
| `pack(community): FAQ store + 自動回覆建議` | SQLite 表 + similarity match + manual approve |
| `pack(community): rule DSL` | 「禁言檢測」「topic drift 警告」「公告排程」用 declarative format（不是 free-form CLAUDE.md） |
| `pack(community): admin dashboard 介面` | 列規則、查 audit log、看 digest 歷史 |
| `docs: Community Manager Pack getting-started` | 場景案例、deploy steps、首 3 天會看到的價值 |

**Gate (Sprint 4 結束)**:
- 1 個真實 Telegram / Discord 群（自己或友人）跑 Community Manager 7 天
- 群管理者報告：至少 3 個價值點被感知到（如 digest、FAQ、topic drift）

**Kill 條件**: 若 admin dashboard 卡前端，先 ship CLI 介面、Pack 仍交付。

---

### Sprint 5 — Week 11-12 — Pack 反饋迭代 + Phase 2 評估
**主軸**: Strategy Phase 3 收尾 + Fluidity Phase 2 go/no-go

**Pack 反饋**:
- 真實 user 訪談 ≥ 2 次
- 修明顯 UX 問題（rule DSL 太硬、digest 太囉嗦等）

**Fluidity Phase 2 評估會** (1-day spike):
| 評估項 | go 條件 | no-go 條件 |
|---|---|---|
| TTFT 現況 | 仍 > 1.5s | < 1.5s — 不啟動 Phase 2 |
| Daytona Linux PoC | PoC 比 `docker run` 快 ≥ 30% | 持平或更慢 — 砍 Daytona、改 Phase 3 IPC |
| 維運能力 | 願意維護 Daytona OSS stack 或 Daytona Cloud 帳號 | 否 — 停在 Phase 1 |

**Gate (Sprint 5 結束)**:
- Community Manager v1.0 標籤
- Phase 2 啟動 OR 中止文件
- 第二個 Pack 候選決定（Research Ops vs Team Operations）

---

### Sprint 6 — Week 13-16 — 條件分支
**Path A — Phase 2 啟動了**:
| Issue 標題 | 說明 |
|---|---|
| `refactor: extract AgentRuntime protocol from container_runner` | DockerRuntime 包現有路徑；無行為變化 |
| `feat: DaytonaRuntime MVP — single group, single sandbox reuse` | Daytona Cloud 或 OSS（PoC 結果決定）；只走 interactive path |
| `bench: DockerRuntime vs DaytonaRuntime A/B on dev workload` | 量 TTFT、cold start、reuse rate |

**Path B — Phase 2 沒啟動，繼續 Pack**:
| Issue 標題 | 說明 |
|---|---|
| `pack(research): RSS source subscription` | feed parser + dedupe |
| `pack(research): daily brief generator` | template + memory tie-in |
| `pack(research): signal detection rules` | 變化偵測（新關鍵字、頻率突變） |

**Gate (Sprint 6 結束)**:
- Path A: DaytonaRuntime MVP 跑通單一群組
- Path B: Research Ops Pack alpha

---

### Sprint 7-8 — Week 17-20 — 強化 + PMF 訊號
**主軸**: Strategy Phase 4 「長期運行強化」 + Phase 5 「PMF 驗證」

**新 issue 待開**:
| Issue 標題 | 說明 |
|---|---|
| `perf(queue): split interactive pool vs background pool` | 互動訊息與 scheduled task 分池；可獨立調 concurrency |
| `obs(observability): per-group activity timeline + failure history` | dashboard 視圖；admin 看群組健康 |
| `feat(memory): background memory compaction` | summarizer 跑 dream task 不擋 interactive |
| `docs: PMF playbook` | 量哪些指標 → 哪些 user behavior 等於「值得收費」 |

**Gate (Sprint 8 結束 — 5 個月節點)**:
- 至少 3 個真實 user / 群組持續使用 ≥ 4 週
- 每群每週至少 1 次主動打開 dashboard
- ≥ 1 個 user 願意付費試用（如果走 hosted 模式）

---

### Sprint 9-10 — Week 21-24 — 下半年計畫 + 收尾
**主軸**: 看 PMF 訊號決定下一階段

**A. PMF 強訊號 (≥ 5 個真實 retention user)**:
- 啟動商業模式 spike（OSS core + hosted vs self-hosted enterprise）
- Pack marketplace 雛形

**B. PMF 弱訊號 (2-3 個 user，不 retention)**:
- 訪談找出 churn 原因
- Pack 二次迭代
- 暫不啟動商業化

**C. PMF 無訊號 (0-1 個 user)**:
- 重新檢視定位 — 是否該再縮小客群（如「只服務 Discord 開發者社群」）
- 或承認個人專案級數，停止「殺出重圍」野心

## 4. 6-month milestone 一頁版

| 月份 | 主軸 | 必交付 |
|---|---|---|
| Month 1 | 敘事 + Metrics + 快修 | 新 README；TTFT baseline；prompt slim live |
| Month 2 | Streaming + Setup wizard | 30-min new-user onboarding 達標；TTFT < 1.5s |
| Month 3 | Community Manager Pack MVP | 1 個 Pack 在真實群跑 7 天 |
| Month 4 | Pack 反饋 + Phase 2 go/no-go | Pack v1.0；Daytona 決定 |
| Month 5 | 第 2 個 Pack OR DaytonaRuntime MVP；前後台分流 | 依 sprint 5 分支；觀察期開始 |
| Month 6 | PMF 訊號驗證；下半年計畫 | PMF 報告；走 / 改 / 停 三選一 |

## 5. Issue 開列順序（前 4 sprint 共 18 個）

依照 Sprint 1-4 列出立刻要開的 issue：

1. `docs: README rewrite — Persistent / Multi-Agent / Governable`
2. `docs: audit README claims vs code` (#585 衍生)
3. `obs: latency / prompt-size / queue-wait metrics dashboard`
4. `obs: OOM rate counter` (#538 衍生)
5. `perf(queue): interactive priority + dedicated slot`
6. `perf(prompt): MEMORY.md budget 4000→1500`
7. `perf(prompt): history token-budget + 6-10 turn cap`
8. `feat(agent): fast/balanced/deep mode flag`
9. `perf(tools): tool result summary, no raw output reinject`
10. `perf(ipc): Windows IPC_POLL_INTERVAL 1s→300ms` (stopgap)
11. `feat(channel): token/progress streaming`
12. `feat(channel): tool-call progress events`
13. `feat(setup): one-command setup wizard CLI`
14. `docs: getting-started speedrun`
15. `pack(community): conversation digest skill`
16. `pack(community): FAQ store + auto-suggest`
17. `pack(community): rule DSL`
18. `pack(community): admin dashboard for rules + audit log`
19. `docs: Community Manager Pack getting-started`

## 6. 風險暫存（risk register）

| 風險 | 觸發點 | 緩解 |
|---|---|---|
| **OOM 再次飆高** | Sprint 2 後 24h OOM 不降 | 啟動 #538 mitigation D（agent self-throttle prompt） |
| **streaming UX backend 限制** | Sprint 3 卡 Gemini/NIM API | 先 ship setup wizard、streaming 列 tech debt |
| **Pack 真實 user 找不到** | Sprint 4 結束無 7-day live run | 自架測試群，先驗 mechanics；user 推到 Sprint 5 |
| **Daytona 對 EvoClaw 沒收益** | Sprint 5 PoC 持平或更慢 | 砍 Phase 2，直接做 Phase 3 (file IPC → event) |
| **python-tests CI 永遠紅** | 每個 PR | 已知，不卡 merge；改動風險靠 manual smoke + 局部 unit test 補 |
| **單人開發頻寬不足** | 任一 sprint 落後 ≥ 50% | 砍 scope 不延 timeline；保 Sprint 4 Community Manager Pack 為核心 |
| **#585 README rhetoric 與新敘事衝突** | Sprint 1 | 同 PR 一起改 README + 補實作對應或砍賣點 |

## 7. 不做清單（明確砍掉）

- ❌ Daytona OSS 自架 — 換湯不換藥（仍 Docker Compose）；要走就走 Daytona Cloud
- ❌ 3 個 Solution Pack 同時開發 — 集中 Community Manager
- ❌ 6 個月內收費 — PMF 都還沒看到
- ❌ 修 python-tests CI hang — 範圍太大，繞過走 manual + unit test
- ❌ 與 Claude Code / LangGraph / OpenHands 正面 feature 對齊 — `docs/PRODUCT_STRATEGY.md` 明確不打 coding agent 戰場
- ❌ Agent Swarms / DevEngine 7-stage 等 README 賣點繼續宣傳 — Sprint 1 內處理（補實作 or 撤宣傳）

## 8. 衡量成功的單一指標

**Month 6 結束時：是否有 ≥ 3 個非自己的真實 user / 群組持續使用 Community Manager Pack ≥ 4 週。**

達標 → 進下半年商業模式 spike
未達 → 訪談 + 收斂 + 再給 3 個月，或承認個人專案規模

## 9. 與既有 issue 對照

未關閉的相關 issue：
- `#538` — OOM 架構（A+C done, D-H 緩做）→ 落在 Sprint 7+ 或更後
- `#585` — README parrot 問題 → Sprint 1 一併處理
- 其他都已關閉

## 10. 變更紀錄

| 日期 | 變更 | 理由 |
|---|---|---|
| 2026-05-15 | 初版 | 依 `docs/FLUIDITY_AND_DAYTONA_PLAN.md` §19-22 + `docs/PRODUCT_STRATEGY.md` §14-17 audit 結論合併 |

每完成一個 sprint 應更新此檔案 §3 標 ✓ / ✗ 與實際時程。

# EvoClaw Self-Update & Restart

更新日期: 2026-05-15

本文件集中說明 EvoClaw 如何拉取新程式碼、測試、重啟。原本散在 `host/auto_update.py`、`host/ipc_watcher.py`、`host/main.py` 的 code comment 與多個 CHANGELOG 條目；此文件是單一權威來源。

## 1. 核心設計：os.execv，不是 pm2 restart

所有自動更新/重啟路徑最終都呼叫 **`os.execv`**（`host/main.py` 結尾），原地替換 process image。

設計理由（Issue #530，code comment 於 `host/main.py`）：

- **os.execv 保 pm2 supervisor PID 穩定** — pm2 看到單一長駐 worker，restart counter 與 uptime 統計合理。
- **不假設 pm2 在 PATH 上** — EvoClaw 可能直接被啟動 debug，或 pm2 daemon 已 crash。`pm2 restart` 在這些情境會 hang 或失敗，更新卡住。
- **pm2 `autorestart: true` 仍是 crash safety net** — 若 os.execv 本身失敗或替換後 process 啟動即 crash，pm2 會 respawn。

⚠️ 若想「改成 pm2 restart」，先讀 Issue #530 討論。

`pm2 restart evoclaw`（手動）是**另一回事** — 殺 process 重起、新 PID。只用於手動 host-only 部署，見 §6。

## 2. 四種觸發路徑

| 路徑 | 觸發方式 | Gate | 是否拉 code |
|---|---|---|---|
| `/update` slash command | Telegram owner 打字 | `OWNER_IDS`（平台驗證的 user id） | 是 |
| `auto_update_loop` | 排程每 `AUTO_UPDATE_INTERVAL_SECS` 秒 `git fetch` | `AUTO_UPDATE_ENABLED=true` | 是（behind 時） |
| `mcp__evoclaw__self_update` IPC | agent 呼叫工具 | `SELF_UPDATE_TOKEN`（legacy 自然語言路徑） | 是 |
| `mcp__evoclaw__restart_host` IPC | agent 呼叫工具 / `/restart` slash | `OWNER_IDS`（slash 路徑） | **否** — 只重啟 |

關於 `/update` vs 自然語言「請更新」的差異見 §5。

## 3. 自動更新流程（worktree 沙盒，Issue #569）

`/update`、`auto_update_loop`、`self_update` IPC 最終都進 `host/ipc_watcher.py:_run_self_update` → `_run_self_update_worktree`：

```
1. git worktree prune                      清掉死 worktree 記錄
2. 移除上次殘留的 worktree（若存在）
3. git fetch origin <AUTO_UPDATE_BRANCH>
4. git rev-list --count HEAD..FETCH_HEAD    已是最新 → 結束（不重啟）
5. git worktree add --detach <WORKTREE_DIR> FETCH_HEAD   隔離 checkout
6. 在 worktree 內跑 AUTO_UPDATE_TEST_CMD（預設 pytest）
7a. test 過 → git merge --ff-only FETCH_HEAD（主 repo）
              → 寫 self_update.flag
              → 寫 restart_notify.json（Issue #579）
              → main loop 偵測 flag → os.execv 重啟
7b. test 失敗 → git worktree remove --force
               → 主 repo 完全不動（無 race，無需 reset --hard）
               → 若 AUTO_UPDATE_AI_FIX_ENABLED，進 §4 AI patch
```

關鍵不變量：**測試在 worktree 沙盒跑，失敗時主 repo 一行都沒動**。這取代了 #569 之前的 `git pull` + `git reset --hard` rollback（race-prone）。

相關設定（`.env`）：

- `AUTO_UPDATE_ENABLED` — 是否開排程更新迴圈（預設 false）
- `AUTO_UPDATE_INTERVAL_SECS` — fetch 間隔（最小 60）
- `AUTO_UPDATE_BRANCH` — 追蹤的分支（預設 main）
- `AUTO_UPDATE_TEST_CMD` — test gate 指令（預設 `pytest -x --timeout=60 -q tests/`）
- `AUTO_UPDATE_USE_WORKTREE` — true 走沙盒（預設），false 走 legacy in-place
- `AUTO_UPDATE_WORKTREE_DIR` — 沙盒路徑（預設 `$DATA_DIR/auto_update_worktree`）

## 4. AI 自動修補（Issue #570）

當 worktree test gate 失敗且 `AUTO_UPDATE_AI_FIX_ENABLED=true`：

```
1. 收集失敗 test 輸出
2. 呼叫 LLM 產生 unified diff 嘗試修復
3. 在 worktree 內 apply patch，重跑 test
4. 成功 →
     AUTO_UPDATE_AI_FIX_REQUIRE_HUMAN_APPROVE=true（預設） → 開 PR 等人審
     false → ff-merge 進 main
5. 達 AUTO_UPDATE_AI_FIX_MAX_RETRIES（預設 3）仍失敗 → 放棄、主 repo 不動
```

安全限制（`host/self_update_ai_fix.py`）：

- `tests/` 受 hash 保護 — 修改測試的 patch 一律拒絕
- patch 只能動 `host/`、`container/agent-runner/`、`scripts/`、`docs/`

## 5. 兩個 flag 的語義

| Flag 檔 | 作用 | 誰寫 |
|---|---|---|
| `<DATA_DIR>/self_update.flag` | 拉 code 後重啟 | `_run_self_update_worktree` / `_inplace` |
| `<DATA_DIR>/restart.flag` | **不拉 code**，只重啟（reload `.env`、解卡死 channel、套用新 agent image） | `mcp__evoclaw__restart_host` IPC handler |

兩者都被 `host/main.py` 的 main loop 偵測，都設 `_self_update_requested=True`，走同一條 os.execv 路徑 — lifecycle 相同，pm2 看到一個穩定 PID。

`<DATA_DIR>/restart_notify.json`（Issue #579）— 在寫 flag 的同時寫入，記 originating chat jid + source label + 起始 timestamp。重啟後 main loop 讀取並 unlink，回送 `✅ EvoClaw 已重啟完成（耗時 Ns）` 給原 chat。Best-effort，失敗不擋重啟。

## 6. `/update` vs 自然語言「請更新」

- `/update` slash command（Issue #577）— **繞過 agent loop**。`OWNER_IDS` gate。0 LLM 呼叫、0 token gate、立即執行。**routine ops 用這個**。
- 自然語言「請更新後台」— 走 agent → legacy `tool_self_update` IPC → 需 `SELF_UPDATE_TOKEN`。未設 token 時 host 回 `❌ self_update 已停用`。

Issue #584 修過一個相關 bug：legacy 路徑被拒後，agent 曾幻覺出「已重啟 / 更新已觸發」假訊息。`container/agent-runner/soul.md` 的 `IPC enqueue ≠ 成功` 段現禁止此行為。詳見 #584。

## 7. 手動部署流程（路徑 4 之外的 plain 手動）

非透過 EvoClaw 自身、而是 operator 直接操作時：

**Host-only 改**（`host/*.py`、`scripts/*`、`docs/*`、`.env.example`、`.github/*`）：
```
git pull
pm2 restart evoclaw
```
這是真正的 pm2 動作 — 殺 process、新 PID。

**Agent image 改**（`container/agent-runner/*`、`container/Dockerfile`）：
```
git pull
docker build -t evoclaw-agent:latest container/
pm2 restart evoclaw
```

## 8. 已知缺口：self-update 不 rebuild agent image

⚠️ **重要**：§2 的四種自動路徑、以及 `/update` slash command，**都不會 rebuild `evoclaw-agent:latest` image**。

- self_update 只做 `git merge` + os.execv 重啟 **host process**。
- agent container 是從 `evoclaw-agent:latest` image spawn 的；image 是 build 時 `COPY` 進去的 `container/agent-runner/*.py` 與 `soul.md`。
- 因此若一次更新含 `container/agent-runner/` 變更，host code 會生效但 **agent 仍跑舊 image**，直到有人手動 `docker build`。

目前緩解：依賴 operator 看 CHANGELOG 的「Image rebuild required: YES」標記後手動 build。

未來改善候選（尚未實作）：

- self_update 偵測 `container/` 是否有 diff → 自動觸發 `docker build`
- 或把 image build 納入 worktree test gate 之後的步驟

在實作之前，**含 `container/` 變更的更新一律需要手動 `docker build -t evoclaw-agent:latest container/`**。

## 9. 相關 Issue / PR 索引

| Issue/PR | 內容 |
|---|---|
| #530 | self_update 基礎 + os.execv 設計 + auto_update 排程 |
| #569 | worktree 沙盒測試（取代 in-place pull + reset --hard） |
| #570 | AI 自動修補 worktree test 失敗 |
| #573 | `restart_host` tool — 純 os.execv 不拉 code |
| #575 | auto_update 記錄 git fetch stderr |
| #577 | `/update` `/restart` slash command（繞過 agent loop） |
| #579 | post-restart notify 回 originating chat |
| #584 | 修 legacy self_update 路徑幻覺成功訊息 |

## 10. 相關程式位置

| 檔案 | 角色 |
|---|---|
| `host/auto_update.py` | `auto_update_loop` 排程 fetch |
| `host/ipc_watcher.py:_run_self_update` | worktree test gate + flag 寫入 |
| `host/ipc_watcher.py:_write_restart_notify` | restart_notify.json |
| `host/main.py`（main loop） | flag 偵測 → `_self_update_requested` |
| `host/main.py`（結尾） | os.execv 區塊 + 設計理由註解 |
| `host/self_update_ai_fix.py` | AI patch 流程 |
| `host/channels/telegram_channel.py` | `/update` `/restart` slash handler |

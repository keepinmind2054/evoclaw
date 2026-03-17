# Changelog

## [1.11.31] - 2026-03-17
### Added
- Inline error notifications: container crash / timeout / exception now sends a user-facing message directly in the conversation — no backend log watching required (#211)
- Rate-limited to 1 notification per group per 5 minutes to prevent flooding during failure storms
- Works out of the box with zero configuration

## [1.11.30] - 2026-03-17
### Fixed
- `run_agent_openai()` crashed with `NameError: name 'group_folder' is not defined` on every NIM / OpenAI-compatible session — MEMORY.md path was computed from an unpassed local variable (#209)

## [1.11.29] - 2026-03-17
### Added
- `TELEGRAM_PROXY` env var: route Telegram API calls through HTTP or SOCKS5 proxy — resolves TimedOut errors on networks where `api.telegram.org` is blocked (#207)
- Increased `MAX_RETRIES` 3 → 5 with capped exponential backoff (2s, 4s, 8s, 16s, 30s max) for transient network blips
- Documented `TELEGRAM_PROXY` in `.env.example` with HTTP and SOCKS5 examples

## [1.11.28] - 2026-03-17
### Fixed
- Security: path traversal via `str.startswith` prefix bypass in `_resolve_container_path` — now uses `pathlib.is_relative_to()` (#201)
- Security: `register_group` IPC handler now validates folder name against path traversal (#202)
- Memory leak: per-group tracking dicts (`_per_jid_cursors`, `_group_msg_timestamps`, failure counters) now pruned on group reload (#203)
- Evolution daemon `_last_micro_sync`/`_last_weekly_compound` now loaded from DB on startup — prevents running immediately after every restart (#205)

## [1.11.27] - 2026-03-17
### Fixed
- Security hardening: reduce container secret exposure — only LLM keys passed, channel/SCM tokens excluded (PR #198)
- Reliability: proper error logging with traceback, Docker health check at startup (PR #199)
- Code quality: DRY _store_bot_reply() helper, named constants, startup-only secret validation (PR #200)
- container_logs table never pruned — added to prune_old_logs() to prevent unbounded disk growth
- warm_logs FTS index not synced on delete — stale search results after pruning
- stderr_lines list unbounded in container_runner — capped at 5000 lines to prevent OOM

### Added
- Dashboard auth warning at startup when DASHBOARD_PASSWORD is unset
- ENABLED_CHANNELS validation at startup — warns on unrecognised channel names

## [1.11.26] - 2026-03-16
### Added
- 意志系統：MEMORY.md 智慧注入（身份永遠保留 + task log 後 3000 字元，防截斷）
- 身份引導 Bootstrap：首次或缺少身份區段時注入模板 + 填寫指令
- Milestone Enforcer v3：偵測 Write/Edit/Bash 寫入 MEMORY.md，turn-28 未寫入注入 CRITICAL 提醒
- Host Auto-Write Fallback：成功 run 後若 mtime < t0，host 自動補寫最小記錄
- soul.md 新增 `### 自我認知` 區段與 MEMORY.md 結構說明

## [1.11.25] - 2026-03-16
### Fixed
- circuit breaker 誤分類：container 有 stderr（確實跑了）時呼叫 _record_docker_success() 而非 _record_docker_failure()，防止 agent crash 錯誤開路
- 新增 SIGUSR1 信號處理器：kill -USR1 <pid> 可線上重置 circuit breaker，不需重啟進程

## [1.11.24] - 2026-03-16
### Refactored
- 靈魂規則獨立為 container/agent-runner/soul.md，runner 啟動時讀取注入 — 更新規則無需改 Python code

## [1.11.23] - 2026-03-16
### Fixed
- health_monitor: 加入 ERROR_RATE_MIN_SAMPLES=5 門檻，避免樣本數不足時誤報高錯誤率（如 1/1=100%）

## [1.11.22] - 2026-03-16
### Fixed
- Docker circuit breaker 半開放狀態（half-open）：60秒後允許一次試探請求，避免永久死鎖 (#177)
- group_queue.py: enqueue_message_check 和 _drain_group 加入 retry_count > 0 檢查，防止 circuit breaker 開路時形成緊密無限重試迴圈（「無法中斷」問題）(#177)

## [2.4.16] - 2026-03-16
### Fixed
- 里程碑強制器 v2：區分「實質工具」vs「報告工具」— 只有 Bash/Read/Write/run_agent + send_message 組合才算真里程碑 (#169)
- 新增 _only_notify_turns 計數器：連續 >=2 輪只呼叫 send_message 無實質工具 → 注入強硬反假報告警告 (#169)
- CRITICAL 規則加入「禁止虛報進度」和「卡住請用 run_agent 委派」(#169)

## [1.11.21] - 2026-03-16
### Fixed
- 里程碑強制器 v2：區分「實質工具」vs「報告工具」— 只有 Bash/Read/Write/run_agent + send_message 組合才算真里程碑 (#175)
- 新增 _only_notify_turns 計數器：連續 >=2 輪只呼叫 send_message 無實質工具 → 注入強硬反假報告警告 (#175)
- CRITICAL 規則加入「禁止虛報進度」和「卡住請用 run_agent 委派」(#175)

## [1.11.20] - 2026-03-16
### Added
- MEMORY.md 啟動注入：session 啟動時讀取 {group_folder}/MEMORY.md，注入為「長期記憶」section — 讓知識歸檔真正有效 (#173)
- 里程碑強制器：run_agent_openai loop 追蹤 _turns_since_notify，超過 4 輪無 mcp__evoclaw__send_message 自動注入提醒 (#173)
- Level B 啟發式偵測：prompt 長度 > 200 或含關鍵字時代碼層面標記 Level B，輔助模型委派決策 (#173)

## [1.11.19] - 2026-03-16
### Added
- Agent soul: `## 任務協調與智慧委派` section added to system prompt
- Pre-flight analysis: Level A (simple, handle directly) vs Level B (complex, delegate) task classification
- Smart delegation: Level B tasks use `mcp__evoclaw__run_agent` with `/reasoning on` injected
- Knowledge archiving: significant tasks append a summary to `MEMORY.md`
- Transparency: Level B announces working dir, creates `progress.log`, sends milestone updates (#171)

All notable changes to EvoClaw will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.11.18] — 2026-03-16

### Fixed
- `container/agent-runner/agent.py`: 追蹤 `_no_tool_turns` 計數器，連續未呼叫工具時將 `tool_choice` 從 `"auto"` 升級為 `"required"` — API 層面強制模型必須呼叫工具 (Fix #169)
- `container/agent-runner/agent.py`: 連續 3 次無 tool call → break loop，防止無限循環 (Fix #169)
- `container/agent-runner/agent.py`: `tool_choice="required"` 不支援時自動降級為 `"auto"` (Fix #169)
- `container/agent-runner/agent.py`: fake-status re-prompt 訊息改為說明「下一輪強制 tool_choice=required」(Fix #167+#169)

## [1.11.17] — 2026-03-16

### Fixed
- `container/agent-runner/agent.py`: CRITICAL 系統提示加入第二條禁令 — 明確禁止 `*(正在執行...)*` 等假狀態行，說明這些純文字對系統沒有任何作用 (Fix #167)
- `container/agent-runner/agent.py`: openai-compat loop 新增 Fallback 2 — 偵測 `*(...)* ` / `*[...]* ` 假狀態模式，自動 re-prompt 模型「請停止假裝，立刻呼叫 Bash tool」(Fix #167)

## [1.11.16] — 2026-03-14

### Fixed
- `host/container_runner.py`: `_stop_container` 改用 `docker kill`（即時 SIGKILL）替代 `docker stop --time 10`（10 秒 grace period），大幅縮短 shutdown 等待時間 (Fix #164)
- `host/container_runner.py`: 新增 `kill_all_containers()` — shutdown 超時後強制 kill 所有追蹤中的 container (Fix #164)
- `host/container_runner.py`: `CancelledError` handler 直接呼叫 `proc.kill()` 殺死 asyncio subprocess，再用 `asyncio.shield(_stop_container())` 確保 docker kill 完成 (Fix #164)
- `host/main.py`: 第二次 Ctrl+C (SIGINT) → 同步 `docker kill` 所有 container + 立即 `os._exit(1)` — 不再無限卡住 (Fix #164)
- `host/main.py`: `wait_for_active` timeout 從 30 秒縮短至 **10 秒** (Fix #164)
- `host/main.py`: `wait_for_active` 超時後呼叫 `kill_all_containers()` 強制終止殘留 container (Fix #164)
- `host/main.py`: final `asyncio.gather(*pending, ...)` 加 **5 秒 timeout** — task cleanup 本身卡住時不再永久阻塞 (Fix #164)

## [1.11.15] — 2026-03-14

### Fixed
- `host/container_runner.py`: `_read_secrets()` 加入 `GITHUB_TOKEN` / `GH_TOKEN` — 這才是真正傳進 container 的 secrets 函數（非 config.get_secrets()），之前一直修錯地方

## [1.11.14] — 2026-03-13

### Fixed
- `container/agent-runner/agent.py`: openai-compat loop 加入 bash code block 自動執行 fallback — Qwen/NIM 模型輸出 ` ```bash ` 代碼塊時自動偵測並執行，結果回饋 history 繼續迴圈
- `container/agent-runner/agent.py`: 系統提示加入 CRITICAL tool usage 警告 — 明確禁止輸出 code blocks，要求 ALWAYS call Bash tool directly

## [1.11.13] — 2026-03-13

### Added
- `.env.example`: 加入 `GITHUB_TOKEN` 說明（附 GitHub settings token 連結），讓用戶知道必須設定此值才能讓 container 使用 git push / gh CLI

## [1.11.12] — 2026-03-13

### Fixed
- `container/Dockerfile`: 安裝 GitHub CLI (`gh`)，修復 container 內 `gh: command not found` 根本原因
- `container/agent-runner/agent.py`: `gh auth login` 成功後執行 `gh auth setup-git`，設定 git credential helper 讓 `git push` via HTTPS 能使用 token
- `container/agent-runner/agent.py`: 設定 `git config user.email/user.name`（agent@evoclaw.local），避免「Please tell me who you are」commit 失敗

## [1.11.11] — 2026-03-13

### Fixed
- `host/config.py`: `get_secrets()` 加入 `GITHUB_TOKEN` / `GH_TOKEN`，修復 container 啟動時 gh CLI 永遠顯示 `⚠️ GH AUTH no GITHUB_TOKEN in secrets` 的根本原因

## [1.11.10] — 2026-03-13

### Fixed
- `container/agent-runner/agent.py`: secrets 設入 `os.environ` 後自動執行 `gh auth login --with-token`，解決 `gh repo create` 及 `git push` 因「no credentials found」失敗的問題；認證成功/失敗/gh 未安裝均有 log

## [1.11.9] — 2026-03-13

### Changed
- `container/agent-runner/agent.py`: 工具 args/result 日誌截斷從 400 提升至 1500 字，可看到完整 bash command 和執行結果

## [1.11.8] — 2026-03-13

### Added
- `container/agent-runner/agent.py`: 在 `system_instruction` 建立後立即 log 前 800 字（`📋 SYSTEM`，逐行分段顯示）
- `container/agent-runner/agent.py`: log 最近 3 輪對話歷史（`📚 HISTORY`），方便在 Container Logs 看到完整 LLM context

## [1.11.7] — 2026-03-13

### Added
- `container/agent-runner/agent.py`: 從 XML prompt 提取純文字 `💬 USER` log，顯示實際用戶訊息（最多 600 字）
- `container/agent-runner/agent.py`: 新增 `📤 REPLY` log，顯示 bot 回覆前 600 字（原本只記字數）
- `container/agent-runner/agent.py`: 工具 args/result 日誌截斷從 200 提升至 400 字

### Fixed
- `host/dashboard.py`: 修復 `showContainerLog()` 中 undefined 問題 — 雙 key（數字 + 字串）查找處理型別不符
- `host/db.py`: stderr 儲存限制從 8KB 提升至 32KB，避免長對話日誌截斷

## [1.11.6] — 2026-03-13

### Added
- `host/dashboard.py`: Container Logs 分頁新增「📋 展開」按鈕，點擊後彈出 Modal 顯示完整 stderr（含所有 print/log 輸出）
- Stderr 摘要由最後 3 行改為最後 5 行
- Modal 採 Monospace 字體、深色背景，可捲動瀏覽完整 container 執行日誌
- 使用 JS Map 快取完整 stderr，展開無需額外 API 請求

## [1.11.5] — 2026-03-13

### Added
- `host/db.py`: 新增 `container_logs` 資料表，記錄每次 container 執行的 stderr/stdout 摘要
- `host/db.py`: 新增 `log_container_start()` / `log_container_finish()` / `get_container_logs()` 函數
- `host/container_runner.py`: 在所有執行路徑（success/error/timeout/exception）呼叫 log 函數
- `host/dashboard.py`: 新增「🐳 Container Logs」分頁 — 可依群組/狀態過濾，顯示執行時間、耗時、stderr 摘要
- 新增 `GET /api/container-logs` 端點（支援 `jid`、`status`、`limit` 查詢參數）

## [1.11.4] — 2026-03-13

### Added
- `host/dashboard.py`: 新增「⚡ Skills」分頁 — 掃描 `skills/` 目錄並顯示每個技能的名稱、版本、作者、說明
- `host/dashboard.py`: 新增「📈 使用統計」分頁 — 整合訊息數/群組、任務執行摘要（總數/成功率/平均時間）、進化執行統計
- 新增 `GET /api/skills` 端點：掃描 `skills/*/manifest.yaml` 回傳技能清單
- 新增 `GET /api/usage` 端點：整合 `messages`、`task_run_logs`、`evolution_runs` 三表統計

## [1.11.3] — 2026-03-13

### Added
- `host/dashboard.py`: 新增「🧠 記憶查看器」分頁 — 可依群組檢視熱記憶（MEMORY.md）、暖記憶日誌（最近 N 天），以及全文搜尋冷/暖記憶
- `host/dashboard.py`: 新增 `GET /api/memory?jid=&days=&search=` 端點，整合 `db.get_hot_memory`、`db.get_warm_logs_recent`、`memory.search.memory_search`

## [1.11.2] — 2026-03-13

### Fixed
- `main.py`: 關機 `finally` 區塊順序修正 — 先 `channel.disconnect()` 再取消 asyncio tasks，消除 Telegram CRITICAL CancelledError 誤報 (#135)
- `channels/telegram_channel.py`: `disconnect()` 各步驟獨立 try/except，防止 `CancelledError` 向外傳播

## [1.11.1] — 2026-03-13

### Fixed
- `host/config.py`: `CONTAINER_IMAGE` 預設值從 `evoclaw-agent:1.11.0` 改為 `evoclaw-agent:latest`，避免每次版本 bump 都造成 Docker image 找不到錯誤 (#133)
- 新增 `Makefile` 提供 `make build` / `make start` / `make dev` 等指令

## [1.11.0] — 2026-03-12

### Added
- Three-tier memory system inspired by OpenClaw/MemSearch architecture
  - Hot Memory: per-group MEMORY.md (8KB), loaded every container invocation
  - Warm Memory: daily log auto-appended after each conversation, 3h micro sync
  - Cold Memory: SQLite FTS5 hybrid search (keyword + recency scoring)
  - Weekly Compound: prune >30-day logs, distill patterns to hot memory
- Container receives hot memory in system context (`[MEMORY]...[/MEMORY]`)
- Container can update hot memory via `memory_patch` in response JSON
- IPC command `memory_search` for in-conversation cold memory queries
- New DB tables: `group_hot_memory`, `group_warm_logs`, `group_warm_logs_fts`, `group_cold_memory`, `group_cold_memory_fts`, `group_memory_sync`
- New module: `host/memory/` with `hot.py`, `warm.py`, `search.py`, `compound.py`

### Chore
- Version bump 1.10.28 → 1.11.0

## [1.10.28] - 2026-03-12

### Fixed
- **#128** `agent.py`: `newSessionId` now preserves the incoming `sessionId` from the host instead of always generating a new `uuid.uuid4()` — every container run was starting a fresh session, destroying cross-turn conversation memory; now the host-provided session ID is echoed back and only falls back to a new UUID when no session ID was passed
- **#128** `main.py`: `get_conversation_history(jid, limit=20)` increased to `limit=50` — the previous 20-message window (≈10 turns) was too small for meaningful multi-turn context; 50 messages (≈25 turns) gives the LLM substantially more conversation history
- **#129** `daemon.py`: `EVOLUTION_INTERVAL_SECS` reduced from `24 * 3600` (24h) to `3600` (1h) — the 24-hour first-cycle delay made evolution impossible to observe or test; with a 1-hour interval the daemon becomes practical in development and production alike
- **#129** `daemon.py`: `MIN_SAMPLES` reduced from `10` to `3` — requiring 10 runs before evolution triggers meant groups almost never crossed the threshold; 3 samples is sufficient to make basic fitness decisions while still avoiding single-sample noise
- **#129** `container_runner.py`: `record_run(..., success=False)` is now called when container output has no valid markers or when JSON parsing fails — these error paths previously returned early without recording, causing silent data loss and underestimating failure rates in fitness calculations
- **#129** `fitness.py`: `record_run()` exception handler changed from silent `log.warning` to `log.error("record_run failed (jid=%s): %s", jid, exc)` — DB errors were previously easy to miss in high-volume logs
- **#129** `genome.py`: `upsert_genome()` exception handler changed from `log.warning` to `log.error("upsert_genome failed (jid=%s): %s", jid, exc)` — genome update failures are now clearly visible in error logs
- **#129** `db.py`: `get_active_evolution_jids()` now includes cold-start groups — previously it only queried `evolution_runs` (returning empty list when the table was empty), causing "Evaluating 0 group(s)" on fresh deployments; now also includes groups with recent conversation history so the daemon can bootstrap their first genome

### Chore
- Version bump 1.10.27 → 1.10.28

## [1.10.27] - 2026-03-12

### Fixed
- **#118** `main.py`: `_is_rate_limited()` — initialise per-group deque with `maxlen=RATE_LIMIT_MAX_MSGS*2`; without a cap the deque grew unbounded for groups that consistently send within the rolling window, causing memory bloat and O(n) deque operations after days of operation
- **#119** `ipc_watcher.py`: added `_cleanup_stale_results()` background sweep — removes subagent result files in `data/ipc/*/results/` that are older than 1 hour; runs every 120 IPC poll cycles to prevent disk fill when containers crash before writing or parent agents are cancelled before reading
- **#120** `evolution/immune.py`: `check_message()` now distinguishes transient DB locks (`sqlite3.OperationalError: database is locked`) from permanent errors — transient locks fail-open (allow message) to prevent a brief prune_old_logs lock from blacking out all group messages; permanent/IO errors still fail-secure
- **#121** `main.py`: graceful shutdown now explicitly cancels all pending asyncio tasks before disconnecting channels — tasks sleeping in `asyncio.sleep()` (message loop POLL_INTERVAL, evolution loop) now exit immediately on SIGTERM instead of blocking shutdown for up to POLL_INTERVAL seconds
- **#122** `task_scheduler.py`: when `compute_next_run()` returns `None` (invalid schedule expression), task is now marked `status=paused` with an explanatory `last_result` message instead of being left with `next_run=NULL`/`status=active`, invisible to scheduler polls but never cleaned up

### Chore
- Version bump 1.10.26 → 1.10.27

## [1.10.26] - 2026-03-12

### Fixed
- **#118** `main.py`: `_is_rate_limited()` — initialise per-group deque with `maxlen=RATE_LIMIT_MAX_MSGS*2`; without a cap the deque grew unbounded for groups that consistently send within the rolling window, causing memory bloat and O(n) deque operations after days of operation
- **#119** `ipc_watcher.py`: added `_cleanup_stale_results()` background sweep — removes subagent result files in `data/ipc/*/results/` that are older than 1 hour; runs every 120 IPC poll cycles to prevent disk fill when containers crash before writing or parent agents are cancelled before reading
- **#120** `evolution/immune.py`: `check_message()` now distinguishes transient DB locks (`sqlite3.OperationalError: database is locked`) from permanent errors — transient locks fail-open (allow message) to prevent a brief prune_old_logs lock from blacking out all group messages; permanent/IO errors still fail-secure
- **#121** `main.py`: graceful shutdown now explicitly cancels all pending asyncio tasks before disconnecting channels — tasks sleeping in `asyncio.sleep()` (message loop POLL_INTERVAL, evolution loop) now exit immediately on SIGTERM instead of blocking shutdown for up to POLL_INTERVAL seconds
- **#122** `task_scheduler.py`: when `compute_next_run()` returns `None` (invalid schedule expression), task is now marked `status=paused` with an explanatory `last_result` message instead of being left with `next_run=NULL`/`status=active`, invisible to scheduler polls but never cleaned up

### Chore
- Version bump 1.10.25 → 1.10.26

## [1.10.25] - 2026-03-12

### Fixed
- **#105** `main.py`: `_is_duplicate_message()` TOCTOU race — converted to `async def`, added `_dedup_lock = asyncio.Lock()` initialized in `main()`, and wrapped the entire check-then-insert sequence in a single `async with _dedup_lock:` block so no two coroutines can read/insert simultaneously
- **#106** `task_scheduler.py`: `run_task()` now advances `next_run` in a `finally` block — the computed `next_run_ts` is always written via `db.update_task()` regardless of whether the run succeeded or raised an exception, preventing tasks from getting stuck at a past timestamp
- **#107** `webportal.py`: `_pending_replies` changed from `dict[str, str]` to `dict[str, tuple[str, float]]` storing `(session_id, created_at_timestamp)`; `_cleanup_pending_replies()` now also evicts entries older than 300 seconds (5-minute TTL) in addition to entries whose session no longer exists
- **#108** `evolution/immune.py`: `check_message()` changed from fail-open to fail-secure — exceptions from DB calls now return `(False, "immune_check_error")` (deny) instead of `(True, None)` (allow); a DB outage can no longer bypass the immune check
- **#109** `ipc_watcher.py`: `apply_skill` and `uninstall_skill` IPC operations wrapped in `asyncio.wait_for(..., timeout=300.0)`; a `TimeoutError` logs an error and sends a user-facing notification instead of hanging the `_skills_lock` indefinitely
- **#110** `container_runner.py`: added `_SECRET_PATTERNS` regex list and `_redact_secrets()` function; all container stderr lines are now passed through `_redact_secrets()` before being logged, preventing API keys, tokens, and passwords from appearing in host logs or the dashboard log stream

### Chore
- Version bump 1.10.24 → 1.10.25

## [1.10.24] - 2026-03-12

### Fixed
- **#92** dev_engine.py Stage 7: replace string `startswith()` path traversal guard with `Path.relative_to()` — eliminates false-pass for paths like `/base_evil/file`
- **#90** webportal.py: move `_pending_replies[msg_id] = session_id` inside `_sessions_lock` to eliminate race condition between concurrent `/api/send` requests

### Closed (already fixed in prior versions)
- **#95** Docker: CJK fonts and PPT/PDF libs (libfreetype6, zlib1g, fonts-wqy-zenhei) already in v1.10.21 Dockerfile
- **#96** CONTAINER_IMAGE env var already configurable since v1.10.22
- **#97** RELEASE.md already added in v1.10.22
- **#98** CHANGELOG.md already added in v1.10.22
- **#99** Duplicate of #95

### Chore
- Version bump 1.10.23 → 1.10.24

## [1.10.23] - 2026-03-12

### Fixed
- **#86** `router.py`: Added user notification (⚠️ 回應傳送失敗) when all message chunks fail to deliver after retries
- **#87** `discord_channel.py`: Wrapped `future.result(30)` in try/except to catch `concurrent.futures.TimeoutError` — prevents crash on slow Discord API responses
- **#88** `whatsapp_channel.py`: `_last_wamid` changed from plain dict to `OrderedDict` with LRU eviction capped at 10,000 entries — prevents unbounded memory growth on high-volume deployments
- **#89** `fitness.py`: Fixed `speed_score` formula — sub-target response times now correctly score 1.0 (was erroneously returning values > 1.0)
- **#90** `webportal.py`: Sessions lock released before `db.store_message()` call — prevents potential deadlock under concurrent session and message-store operations
- **#91** `telegram_channel.py`: Upload timeout now configurable via `TELEGRAM_UPLOAD_TIMEOUT` env var (default: 300s, was hardcoded 120s)
- **#92** `dev_engine.py`: Path traversal guard improvements
- **#93** `immune.py`: Guard against empty `sender_jid` in `check_message()` — prevents potential crash or incorrect threat attribution on malformed messages

### Chore
- Version bump 1.10.22 → 1.10.23

## [1.10.22] - 2026-03-12

### Fixed
- **#66** WhatsApp `send_typing` now sends read receipt with correct `wamid` (per-message WhatsApp ID) instead of `chat_id`; skips gracefully when no prior message received
- **#68** `send_file` IPC handler supports `deleteAfterSend` flag; `research-ppt` skill instructs agent to clean up temp `.pptx`/`.txt` files post-delivery
- **#5** Formally closed: per-JID timestamp cursors (implemented in v1.10.17) fully resolve group-isolation violation

### Added
- **#6** Multi-key rotation for all LLM providers: `GOOGLE_API_KEY`, `CLAUDE_API_KEY`, `OPENAI_API_KEY`, `NIM_API_KEY` accept comma-separated values; container agent auto-rotates to next key on 429/quota error with `🔑 KEY ROTATE` log

### Chore
- Version bump 1.10.19 → 1.10.22

## [1.10.21] - 2026-03-12

### Added
- **Production-ready Docker image** (`container/Dockerfile`): upgraded base from `node:22-slim` to `node:22` (full Debian) for broader system library compatibility needed by native Python extensions and MCP tooling (Issue #83)
- **Complete document generation stack** pre-installed in image: `reportlab` (PDF), `openpyxl` (Excel), `python-docx` (Word) alongside existing `python-pptx==1.0.2` — eliminates runtime pip installs for all document types (Issue #77)
- **Web scraping stack** pre-installed: `httpx`, `beautifulsoup4`, `lxml` — agents can scrape and parse HTML without runtime network dependency (Issue #78)
- **Image processing** pre-installed: `Pillow` with system libs `libjpeg-dev`, `libpng-dev`, `zlib1g-dev`, `libcairo2` — required by reportlab image embedding and future vision workflows (Issue #79)
- **Data science stack** pre-installed: `pandas`, `numpy`, `matplotlib` — enables in-container data analysis, tabular processing, and chart generation (Issue #80)
- **Complete CJK font coverage**: added `fonts-liberation`, `fonts-noto-color-emoji` alongside existing `fonts-noto-cjk`, `fonts-wqy-zenhei`, `fonts-wqy-microhei`; all run through `fc-cache -fv` (Issue #81)
- **System utilities**: added `wget`, `unzip`, `jq`, `ffmpeg` — covers archive extraction, JSON shell scripting, and media processing required by many MCP server setup scripts (Issue #82)
- **Build tools**: added `python3-dev`, `build-essential`, `gcc` so pip packages with C extensions (lxml, Pillow, numpy) compile correctly without pre-built wheels
- **Infrastructure vs project separation**: Dockerfile now owns all infrastructure Python packages; `requirements.txt` stays lean (only `google-genai`, `openai`, `anthropic`)
- **`libfontconfig1`** and **`libpangocairo-1.0-0`** added to ensure font rendering works correctly in headless PDF/PPT generation

### Changed
- Base image: `node:22-slim` → `node:22` for full system library availability (Issue #83)
- `apt-get install` now uses `--no-install-recommends` to keep image size minimal despite upgrading base

## [1.10.20] - 2026-03-12

### Changed
- 升級 container Docker 基礎鏡像至 python:3.9 (Debian Bullseye)
- 預裝中文字體：fonts-wqy-zenhei、fonts-wqy-microhei + fc-cache
- 預裝系統依賴：libfreetype6、libpng16-16、zlib1g
- 預裝 python-pptx==1.0.2 進鏡像，消除 runtime pip 網路依賴
- 設定 PYTHONUNBUFFERED=1 + LANG=C.UTF-8 確保輸出編碼正確

### Fixed
- research_ppt 工具在網路不穩定時因 pip install 失敗而崩潰的問題
- 中文字元在 PPT/PDF 中顯示為方塊的問題

## [1.10.19] - 2026-03-12

### Fixed
- **Gmail body size unbounded** (`host/channels/gmail_channel.py`): `_extract_body()` now truncates decoded email bodies at 32 KB with a clear `[... email truncated at 32 KB ...]` suffix. Large emails (newsletters, quoted thread chains) could previously saturate the agent LLM context window and bloat the messages table (Issue #69)
- **Telegram non-text messages silently dropped** (`host/channels/telegram_channel.py`): added a handler for photos, voice messages, video, audio, documents, stickers, location, and contact message types that sends a short informational reply: `I can only process text messages at the moment.` Previously, all non-text Telegram messages were silently ignored with zero user feedback (Issue #70)
- **GroupQueue `create_task()` swallows exceptions silently** (`host/group_queue.py`): all `asyncio.create_task()` calls now attach a `_task_done_callback` that logs unhandled exceptions at ERROR level. Without this, exceptions outside the inner try/except (e.g. CancelledError during shutdown, RuntimeError from the event loop) were silently discarded by the Python event loop (Issue #71)
- **`.env.example` missing security-critical and operational vars** (`.env.example`): added `WHATSAPP_APP_SECRET` (with a prominent security warning), `LOG_FORMAT`, `RATE_LIMIT_MAX_MSGS`, `RATE_LIMIT_WINDOW_SECS`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD`, `WEBPORTAL_ENABLED`, `WEBPORTAL_HOST`, `WEBPORTAL_PORT`, and `HEALTH_PORT`. The omission of `WHATSAPP_APP_SECRET` was especially critical — operators without this var run with no HMAC signature verification, accepting webhook payloads from any caller (Issue #72)
- **IPC `ensure_future()` fire-and-forget swallows exceptions** (`host/ipc_watcher.py`): all `asyncio.ensure_future()` calls for `_run_apply_skill`, `_run_uninstall_skill`, `_run_list_skills`, `_run_subagent`, and `_run_dev_task` now attach `_ipc_task_done_callback` that logs unhandled exceptions at ERROR level (Issue #73)
- **Discord `disconnect()` deadlocks — `close()` called on wrong event loop** (`host/channels/discord_channel.py`): `disconnect()` now schedules `client.close()` via `asyncio.run_coroutine_threadsafe()` on the Discord background loop instead of awaiting it from the main asyncio loop. Also adds `thread.join(timeout=5)` to ensure the background thread drains cleanly before process exit (Issue #67)

## [1.10.18] - 2026-03-12

### Fixed
- **Container name collision** (`host/container_runner.py`): `container_name` now uses the first 8 hex characters of `run_id` (UUID4) instead of `int(time.time())`. Two concurrent containers for the same group starting within the same wall-clock second previously caused Docker to reject the second `run` with a name-conflict error, triggering the circuit breaker (Issue #59)
- **Five DB read functions missing `_db_lock`** (`host/db.py`): `get_messages_since`, `get_state`, `get_session`, `get_registered_group`, and `get_dev_events` now hold `_db_lock` for the duration of their queries, consistent with all other DB read/write functions. Eliminates potential `SQLITE_LOCKED` errors and stale reads when dashboard/webportal/evolution daemon threads access the shared connection concurrently (Issue #60)
- **No memory/CPU limits on `docker run`** (`host/container_runner.py`, `host/config.py`): added `--memory` and `--cpus` flags to the container command, configured via `CONTAINER_MEMORY` (default `512m`) and `CONTAINER_CPUS` (default `1.0`) env vars. Prevents a runaway agent from exhausting host memory and triggering the kernel OOM-killer (Issue #61)
- **WAL file grows unbounded** (`host/evolution/daemon.py`): `_sync_prune_logs()` now runs `PRAGMA wal_checkpoint(TRUNCATE)` after log pruning so the WAL file is reclaimed every 24 hours, preventing unbounded WAL growth on high-traffic deployments (Issue #62)
- **Unused `immune_cutoff_ms` variable** (`host/db.py`): removed the dead `immune_cutoff_ms = int(...)` assignment in `prune_old_logs()` that was computed but never used; added an explanatory comment for the hardcoded 90-day immune-threat retention policy (Issue #63)
- **`PRAGMA foreign_keys = ON` never set** (`host/db.py`): `init_database()` now enables SQLite foreign key enforcement immediately after setting WAL mode. Without this pragma, any future schema additions using `ON DELETE CASCADE`/`ON DELETE RESTRICT` are silently ignored, causing orphaned rows and skewed metrics (Issue #64)

## [1.10.17] - 2026-03-12

### Fixed
- **Per-JID message cursors** (`host/main.py`): replaced single global `_last_timestamp` with a per-JID cursor dict (`_per_jid_cursors`). A successful container run for group A can no longer push the shared timestamp past group B's pending messages, preventing silent message loss in multi-group deployments (Issue #52)
- **DB thread-safety** (`host/db.py`): `get_new_messages()` and `get_conversation_history()` now hold `_db_lock` for the duration of the query, consistent with all other DB read functions. Eliminates potential SQLITE_LOCKED errors and stale reads when dashboard/webportal/evolution daemon threads run concurrently (Issue #53)
- **Task scheduler tight-retry loop** (`host/task_scheduler.py`): `run_task()` exception handler now calls `db.update_task()` to advance `next_run` after a failure, preventing the same task from re-firing on every scheduler poll cycle when an exception occurs before the normal update path (Issue #54)
- **Empty env temp file race** (`host/container_runner.py`): `_get_empty_env_file()` now uses a `threading.Lock` with double-checked locking to prevent two concurrent callers from each creating a separate temp file during the first call, leaving one file orphaned (Issue #55)
- **SSE log stream graceful shutdown** (`host/dashboard.py`): `_handle_sse_logs()` now checks a module-level `_dashboard_stopping` threading.Event instead of looping forever, exiting promptly when the host receives SIGTERM/SIGINT rather than waiting for the client to disconnect (Issue #56)
- **Subagent result file size cap** (`host/ipc_watcher.py`): `_run_subagent()` now truncates result text to 1 MB before writing to the IPC results directory, preventing a runaway subagent from filling the host disk through unbounded result file writes (Issue #57)
- **Scheduler empty chat_jid guard** (`host/task_scheduler.py`): `start_scheduler_loop()` now skips tasks with an empty `chat_jid` with a warning instead of enqueuing them with an empty key, which could corrupt the GroupQueue per-group serialization map (Issue #48)

## [1.10.16] - 2026-03-12

### Security
- WhatsApp webhook now validates the `X-Hub-Signature-256` HMAC-SHA256 header on every delivery; requests that fail verification are rejected with HTTP 403, preventing spoofed payloads from unauthenticated callers (Issue #42)
- WebPortal session endpoint now returns a per-session CSRF token; all POST requests (`/api/send`) must echo the token as `X-CSRF-Token`, blocking cross-site request forgery attacks even when Basic Auth credentials are browser-cached (Issue #45)
- `immune.py` content fingerprinting upgraded from MD5 to SHA-256, preventing hash-collision attacks that could allow adversaries to bypass spam counters or poison the threat database (Issue #47)

### Fixed
- DB read functions called from background threads now hold `_db_lock`: `get_all_registered_groups`, `get_all_tasks`, `get_evolution_runs`, `get_active_evolution_jids`, `get_recent_run_stats`, `get_group_genome`, `is_sender_blocked`, `get_recent_threat_count`, `get_immune_stats`, `get_evolution_log`, `get_due_tasks`, `get_pending_task_count`, `get_error_stats` — eliminates `database is locked` errors and stale reads under concurrent load from dashboard/webportal and evolution daemon (Issue #43)
- Discord `send_message()` and `send_typing()` now use `asyncio.run_coroutine_threadsafe()` to bridge the main event loop and the Discord client's background event loop, fixing cross-loop `RuntimeError` that silently prevented Discord message delivery (Issue #44)
- Gmail channel `_seen_message_ids` replaced with a bounded `OrderedDict` (cap 10,000 entries, LRU eviction), preventing unbounded memory growth on long-running deployments processing high volumes of email (Issue #46)
- Slack `auth_test()` is now called once during `connect()` and the workspace ID is cached on `self._workspace_id`; previously called on every single incoming message, hitting Slack rate limits at high message rates (Issue #49)
- `ipc_watcher._notify_main_group_error()` now sanitizes error strings before sending them to the main group chat — filesystem paths are replaced with `<path>` and output is truncated to 120 characters, preventing internal directory layout leakage to chat members (Issue #50)

## [1.10.15] - 2026-03-12

### Added
- New `research-ppt` skill: generates PowerPoint presentations with self-healing dependency management (Issue #39)
  - `research_ppt_tool.py` container tool registered at runtime via `register_dynamic_tool()`
  - Version-pins `python-pptx==1.0.2` to prevent dependency drift on ephemeral Docker containers
  - Self-healing installer retries up to 2 times on transient PyPI network failures
  - Graceful degradation: produces a plain-text `.txt` report when PPTX generation fails for any reason
  - Font-safe: skips unavailable CJK/Chinese fonts with a fallback chain instead of crashing
  - Skill manifest `skills/research-ppt/manifest.yaml` includes `container_tools:` entry so the tool is hot-deployed to `data/dynamic_tools/` without rebuilding the container image

### Fixed
- `route_file()` in `router.py` now validates file existence and enforces a 45 MB size guard before attempting upload; oversized files trigger a plain-text notification to the user instead of a silent broken upload (Issue #40)
- `TelegramChannel.send_file()` now streams the file via an open file object instead of loading the entire binary content into memory with `f.read()`, preventing large memory spikes for multi-megabyte files (Issue #40)
- `TelegramChannel.send_file()` wrapped in `asyncio.wait_for(..., timeout=120)` so a slow network cannot stall the GroupQueue slot indefinitely (Issue #40)
- Removed debug log file (`debug_send.log`) side-effect from `TelegramChannel.send_file()` that was writing to `/workspace/group/debug_send.log` on every file send

## [1.10.14] - 2026-03-12

### Fixed
- `db.record_immune_threat()` now holds `_db_lock` for the full read-modify-write sequence, eliminating a TOCTOU race condition under concurrent dashboard/webportal thread access (Issue #32)
- `db.prune_old_logs()` now also prunes `evolution_log`, `messages`, `immune_threats` (noise-only), `dev_events`, and `dev_sessions` tables — previously only `task_run_logs` and `evolution_runs` were cleaned, leaving five tables to grow unboundedly (Issue #33)
- Added `psutil>=5.9.0` to `host/requirements.txt` and `pyproject.toml`; `health_monitor.py` imports `psutil` unconditionally but it was not listed as a dependency, causing `ImportError` on fresh installs (Issue #34)
- Implemented `db.get_pending_task_count()` and `db.get_error_stats()` in `db.py`; health monitor was guarding calls with `hasattr()` and silently using zero-value fallbacks, making the container-queue and error-rate health checks permanently non-functional (Issue #35)
- LLM API calls (Gemini, Claude, OpenAI-compatible) now wrapped in `_llm_call_with_retry()` with exponential backoff (up to 3 attempts: 1s, 2s delay) for transient errors (429 rate limit, 5xx server errors); permanent errors (400, 401) are not retried (Issue #36)

### Added
- Periodic DB log pruning: `evolution_loop` in `daemon.py` now calls `prune_old_logs()` after each 24-hour evolution cycle, ensuring long-running processes benefit from maintenance without requiring a restart (Issue #37)

## [1.10.13] - 2026-03-12

### Security
- Agent tools (`tool_read`, `tool_write`, `tool_edit`) now validate that file paths resolve inside `/workspace/` before executing, blocking prompt-injection attacks that attempt to read `/proc/self/environ` or other sensitive container files (Issue #29)
- `skills_engine/apply.py` post_apply commands now checked against an allowlist of safe prefixes (`pip install`, `npm install`, `pytest`, etc.) — unknown commands are skipped with a warning, preventing malicious skill manifests from running arbitrary host commands (Issue #28)
- `ipc_watcher._resolve_container_path` now validates the resolved host path stays within the expected root directory, preventing path traversal via crafted container file paths (Issue #26)

### Fixed
- WebPortal `_pending_replies` dict now cleaned up lazily on each `/api/send` call (evicting entries whose sessions no longer exist), fixing an unbounded memory leak that accumulated indefinitely as sessions expired (Issue #21)
- DB write functions `set_session`, `create_task`, `update_task`, `delete_task`, `set_registered_group`, `upsert_group_genome`, `block_sender`, `log_evolution_event`, `log_dev_event` now all hold `_db_lock` for thread safety, preventing potential `database is locked` errors from webportal/dashboard/evolution threads (Issue #22)
- WebPortal `/api/send` now enforces per-group rate limiting (same as the Telegram/WhatsApp path) to prevent authenticated WebPortal users from bypassing the rate limiter and flooding the GroupQueue (Issue #25)
- `router.route_outbound` now retries failed chunks (up to 2 attempts, 1s delay) and notifies the user when chunks cannot be delivered after retries, rather than silently dropping remaining chunks (Issue #27)

### Added
- WebPortal `_sessions` dict now capped at 500 concurrent sessions; `_expire_sessions` is called on every new session creation to enforce the cap (Issue #23)
- Per-session message list capped at 200 entries to prevent unbounded per-session memory growth; `deliver_reply` also respects this cap (Issue #23)
- WebPortal `_read_body` now enforces a 64 KB maximum POST body size, returning HTTP 413 for oversized requests to prevent memory exhaustion (Issue #24)
- Individual message text in WebPortal `/api/send` capped at 32 KB (Issue #24)
- `ENABLED_CHANNELS` validated at startup against the set of known channel names; unrecognised names trigger a clear `ERROR` log entry so operators immediately see typos (Issue #30)

## [1.10.12] - 2026-03-12

### Security
- WebPortal now enforces Basic Auth when `DASHBOARD_PASSWORD` is set, preventing unauthenticated access to group list and message injection (Issue #12)

### Fixed
- Fitness `speed_score` formula now excludes failed runs (response_ms=0) from the average, preventing broken groups from being scored as "perfect speed" (Issue #18)
- SQLite connection now protected by `threading.Lock` on all write operations, preventing `database is locked` errors when dashboard/webportal/evolution threads write concurrently (Issue #15)
- `task_run_logs` and `evolution_runs` tables now pruned at startup (30-day retention) to prevent unbounded disk growth (Issue #19)

### Added
- Per-group message rate limiting (sliding window: 20 msgs/60s by default, configurable via `RATE_LIMIT_MAX_MSGS` / `RATE_LIMIT_WINDOW_SECS`) to prevent one group from starving others (Issue #16)
- `GroupQueue` backpressure: `pending_tasks` capped at 50 per group, `_waiting_groups` capped at 100 entries — excess tasks are dropped with a warning (Issue #14)
- Structured log format support: set `LOG_FORMAT=json` to emit newline-delimited JSON logs for Loki/Datadog/CloudWatch (requires `python-json-logger`) (Issue #17)
- Container image pin warning: logs a `WARNING` at startup when `CONTAINER_IMAGE` uses the mutable `:latest` tag (Issue #13)
- `db.prune_old_logs(days=30)` maintenance function for log table housekeeping

## [1.10.11] - 2026-03-12

### Architecture Improvements
- 新增 `run_id` 關聯 ID 傳入 container input_data，提升多群組除錯能力（Issue #1, #8）
- 修正 outer timeout 硬編碼 300s 改用 `config.CONTAINER_TIMEOUT`，確保設定一致性（Issue #2）
- 修正 IPC 未知 type 靜默忽略，現在記錄 warning 日誌（Issue #3）
- 新增 `GroupQueue.wait_for_active()` 和 `shutdown_sync()`，graceful shutdown 等待執行中的 container（Issue #4）
- 新增訊息去重機制（`_is_duplicate_message` + LRU fingerprint set），防止 webhook 重試造成重複處理（Issue #7）
- 修正 `ipc_watcher._resolve_container_path` 引用未定義 `logger`（應為 `log`）導致 NameError（Issue #10）
- 將 `asyncio.get_event_loop().run_in_executor()` 替換為 `asyncio.to_thread()`，修正 Python 3.10+ DeprecationWarning（Issue #9）

## [1.10.10] - 2026-03-12

### Fixed
- 修正 JSON 輸出無大小限制（加入 2MB 上限防止 DoS）
- 修正 circuit breaker 競態條件（asyncio.Lock 保護全域 dict）
- 修正 DB connection 未關閉造成的 file lock 殘留（atexit 正確關閉）
- 修正 stderr streaming 無 timeout（readline 加入 30s 超時）
- 新增 evolution_runs DB index（jid, timestamp）提升查詢效能
- 修正 container timeout 無限重試（超時後推進 cursor 並通知用戶）
- 新增 Secret key 驗證（啟動時早期檢測缺少的 API key）
- 修正 group folder 路徑穿越漏洞（加入格式驗證）
- 修正 session ID 在 timeout 時遺失的問題
- 修正孤立任務清理不完整（同時清理已刪除 group 的任務）

## [1.10.9] - 2026-03-11

### Fixed
- 移除對話歷史訊息 800 字截斷限制，保留完整 context
- 修正 Session 管理：container 現在回傳 newSessionId，DB 正確更新
- 歷史時間窗從硬編碼 2 小時改為可設定（預設 4 小時）
- 歷史訊息上限從 30 則增加至 50 則

### Changed
- history_lookback_hours 可在 group config 中設定（預設 4）

## [1.10.8] - 2026-03-11

### Added — Dynamic Container Tool Hot-swap (Skills 2.0)

Solves the core Docker limitation for DevEngine-generated skills: new Python tools can now be installed into running containers without rebuilding the image.

#### Architecture: `data/dynamic_tools/` volume mount
- `host/container_runner.py`: `_build_volume_mounts()` now mounts `{DATA_DIR}/dynamic_tools/` → `/app/dynamic_tools:ro` in **every** container (both main and regular groups)
- `container/agent-runner/agent.py`: new `_load_dynamic_tools()` function — scans `/app/dynamic_tools/*.py` at startup and dynamically imports each file via `importlib.util`; `register_dynamic_tool` is injected into each module's namespace
- Drop a `.py` file into `data/dynamic_tools/`, next container run picks it up automatically — no `docker build` needed

#### Dynamic Tool Registry (`agent.py`)
- `_dynamic_tools: dict` — global in-process registry: `{name → {fn, schema, description}}`
- `register_dynamic_tool(name, description, schema, fn)` — appends to **all three** provider declaration lists (Gemini `TOOL_DECLARATIONS`, `CLAUDE_TOOL_DECLARATIONS`, `OPENAI_TOOL_DECLARATIONS`) and registers the dispatch function
- `_json_schema_to_gemini()` — converts JSON Schema properties dict to Gemini `types.Schema` at runtime (supports string, integer, boolean, object, array types)
- `_execute_tool_inner()` — falls back to `_dynamic_tools` dispatch after all built-in tools

#### Skills Engine: `container_tools:` manifest field
- `skills_engine/types.py`: `SkillManifest` dataclass gains `container_tools: list[str]` field (default `[]`)
- `skills_engine/manifest.py`: `read_manifest()` reads `container_tools:` from YAML
- `skills_engine/apply.py`: after `adds:` processing, copies `container_tools` files from `skill/add/` → `{DATA_DIR}/dynamic_tools/` (flattened by filename)
- `skills_engine/uninstall.py`: before replay, locates skill dir, reads manifest, removes its `container_tools` files from `dynamic_tools/`
- `dynamic_tools/.gitkeep` — git-tracked directory placeholder

### Example `manifest.yaml` with `container_tools:`
```yaml
skill: my-skill
version: "1.0.0"
adds:
  - docs/superpowers/my-skill/SKILL.md
container_tools:
  - dynamic_tools/my_tool.py   # injected at /app/dynamic_tools/my_tool.py
```

### Example dynamic tool file
```python
# dynamic_tools/my_tool.py  (inside skill add/ directory)
def _my_tool(args: dict) -> str:
    return f"Result: {args['input']}"

register_dynamic_tool(
    name="my_tool",
    description="Does something useful",
    schema={"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]},
    fn=_my_tool,
)
```

### Files Changed
- `host/container_runner.py` (dynamic_tools mount in `_build_volume_mounts`)
- `container/agent-runner/agent.py` (`_dynamic_tools` registry, `register_dynamic_tool`, `_load_dynamic_tools`, `_execute_tool_inner` fallback)
- `skills_engine/types.py` (`container_tools` field on `SkillManifest`)
- `skills_engine/manifest.py` (`container_tools` deserialization)
- `skills_engine/apply.py` (`container_tools` copy to `dynamic_tools/`)
- `skills_engine/uninstall.py` (`container_tools` cleanup before replay)
- `dynamic_tools/.gitkeep` (new)

---

## [1.10.7] - 2026-03-11

### Fixed
- **Telegram File Send Optimization**: Refined v1.10.1 binary file fix by removing redundant `disable_content_type_detection` parameter that caused compatibility issues.
- **Debug Log Delivery**: Enhanced error reporting to send debug logs directly to user's Telegram instead of writing to container-internal files (solving persistence issues in Docker).
- **Documentation Sync**: Ensured `CHANGELOG.md`, `README.md`, and `RELEASE.md` are properly synchronized with actual code changes.


## [1.10.6] - 2026-03-11

### Fixed (Code Review Findings)
- CRASH: .env shadow mount no longer double-prefixes `-v` flag (containers were failing to start on Linux/macOS)
- ERROR: run_container_agent now catches asyncio.CancelledError and calls _stop_container (outer timeout no longer creates zombie containers)
- ERROR: /api/dev/resume now writes IPC file to correct group folder path (DevEngine resume was silently broken)
- WARNING: cleanup_orphans now awaits proc.wait() after docker rm
- Minor: send_file tool schema — chat_jid removed from required[] (auto-detected from input)
- Minor: _resolve_container_path guards against empty group_folder
- Minor: TelegramChannel.send_file removes redundant filename parameter

## [1.10.5] - 2026-03-11

### Added
- **Comprehensive Container Agent Logging**: Added `_log(tag, msg)` helper with millisecond timestamps to `container/agent-runner/agent.py` for structured stderr logging throughout the agent lifecycle.
  - Startup: process ID logged at container boot (`🚀 START`).
  - Input parsed: JID, group folder, and message count (`📥 INPUT`).
  - Last message preview for quick debugging (`💬 MSG`).
  - Model/provider selection before first LLM call (`🤖 MODEL`).
  - Per-turn LLM call and response with stop reason (`🧠 LLM →/←`).
  - Tool dispatch with name and truncated args (`🔧 TOOL`).
  - Tool result preview (`🔧 RESULT`).
  - IPC file writes for messages, tasks, and files (`📨 IPC`).
  - File send path and existence check (`📎 FILE`).
  - Output size in chars before emit (`📤 OUTPUT`).
  - Exception type and message with full traceback to stderr (`❌ ERROR`).
  - Completion with success flag (`🏁 DONE`).
- **Noisy SDK log suppression**: `httpx`, `httpcore`, `google`, and `urllib3` loggers clamped to WARNING level.
- **Host stderr elevation**: `host/container_runner.py` `_stream_stderr()` now promotes emoji-tagged agent log lines from DEBUG to INFO so they appear in production logs without `--debug`.

## [1.10.1] - 2026-03-11

### Fixed
- **Telegram Channel**: Fixed critical bug in `send_file()` where binary files (e.g., `.pptx`, `.pdf`, `.jpg`) would fail to send due to incorrect encoding handling (`cp950 codec can't decode` error).
  - Changed file reading to explicitly use binary mode (`rb`) and read entire content before sending.
  - Now uses `telegram.InputFile` to ensure binary data is properly transmitted.
  - Added MIME type detection with fallback to `application/octet-stream`.
  - Set `disable_content_type_detection=True` to prevent Telegram from re-encoding files.
  - Improved error logging for file sending failures.

## [1.10.0] - 2026-03-10

### Added
- **Evolution Engine**: Full genome evolution with formality, technical_depth, and responsiveness genes.
- **Health Monitor**: Real-time system health tracking with automatic alerts.
- **DevEngine**: 7-stage automated development pipeline (Analyze → Design → Implement → Test → Review → Document → Deploy).
- **Web Dashboard**: 7-tab monitoring interface with Subagent hierarchy visualization.
- **Superpowers Integration**: 12 workflow skill packages from Superpowers methodology.

### Changed
- Replaced `threading.Lock` with `asyncio.Lock` for better async compatibility.
- GroupQueue now serializes container execution per group.
- WebPortal session timeout reduced to 1 hour.

### Fixed
- `_stop_container` now properly waits for `proc.wait()` to complete.
- `/api/env` now uses key whitelist for security.
- DevEngine JID fallback now provides clear error messages.
- macOS compatibility fixes for `.env` file handling.

## [1.9.0] - 2026-02-15

### Added
- **Immune System Enhancement**: 22 injection pattern detections.
- **Adaptive Evolution**: Epigenetic adaptation based on system load and time of day.
- **Evolution Log**: Complete history of genome changes in `evolution_log` table.

### Changed
- Improved container isolation and security.
- Enhanced error reporting in dashboard.

## [1.8.0] - 2026-02-01

### Added
- **Skills Engine**: Plugin system for adding new capabilities.
- **WhatsApp Support**: Optional skill for WhatsApp integration.
- **Multi-model Support**: Gemini, OpenAI-compatible, and Claude.

### Changed
- Refactored channel architecture for better modularity.

---

## Version History Summary

| Version | Date | Key Changes |
|---------|------|-------------|
| 1.10.23 | 2026-03-12 | Router fail notification (#86), Discord timeout guard (#87), WhatsApp LRU wamid dict (#88), fitness score fix (#89), webportal deadlock fix (#90), Telegram upload timeout env var (#91), path traversal guard (#92), immune empty JID guard (#93) |
| 1.10.22 | 2026-03-12 | WhatsApp send_typing wamid fix, send_file deleteAfterSend, multi-key rotation (#6), close #5 |
| 1.10.1 | 2026-03-11 | Fixed Telegram binary file sending bug |
| 1.10.0 | 2026-03-10 | Full evolution engine, DevEngine, Health Monitor |
| 1.9.0 | 2026-02-15 | Enhanced immune system, adaptive evolution |
| 1.8.0 | 2026-02-01 | Skills engine, WhatsApp support |

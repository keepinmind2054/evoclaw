## [1.21.0] — 2026-03-21

### Fixed (Phase 14: Skills, Memory & Enterprise — 57 fixes)

Four PRs covering the evolution system, enterprise connectors & container tools, skills engine, and memory system.

#### Evolution System (PR #367 — 4 fixes)

- **`immune.py` Chinese attack pattern false negatives (HIGH)** — 10 attack patterns missed Traditional/Simplified char variants (進/进, 從/从), word-order variants (現在你是 vs 你現在), missing space handling (AI 助手), and optional suffix/prefix coverage; all 42 attacks now detected with 0 false positives
- **`daemon.py` avg_ms calculation corrupted by failed runs (HIGH)** — failed runs and negative values included in average → genome evolved toward wrong response style; fixed to exclude failed/negative values
- **`fitness.py` final score not clamped (MEDIUM)** — corrupted DB row could produce score > 1.0; added clamp to [0.0, 1.0]
- **`fitness_reporter.py` WebSocket disconnect permanent (MEDIUM)** — no reconnect logic → all subsequent fitness data silently dropped; added reconnect with file fallback

#### Enterprise Connectors & Container Tools (PR #368 — 20 fixes)

- **`hpc_connector.py` job name newline injection into #SBATCH directives (CRITICAL)** — `job\n#SBATCH --wrap=rm -rf /` allowed arbitrary directive injection; added newline sanitization
- **`jira_connector.py` issue key path traversal in REST URL (CRITICAL)** — `../../../admin` in issue key escaped API base path; added strict issue key validation
- **`agent.py` `_tool_bash()` timeout killed only main process (CRITICAL)** — child process group (SSH/git subprocesses) leaked indefinitely; replaced with `os.killpg` to kill entire process group
- **`agent.py` `_tool_read()` symlink bypass (CRITICAL)** — symlink to `/etc/passwd` passed raw-string path check but read host files; added `os.path.realpath()` resolution before path validation
- **`hpc_connector.py` SSH BatchMode missing (HIGH)** — interactive prompt hung server; inherited stdin blocked subprocess; no output size limit on job output; all three addressed
- **`jira_connector.py` API token in public attribute (HIGH)** — token exposed via object inspection; no HTTP timeout; unbounded max_results pagination; all three addressed
- **`ldap_connector.py` bind password in public attribute (HIGH)** — password exposed via object inspection; stale connection not detected; no LDAP connection timeout; all three addressed
- **`workflow_engine.py` no dependency or cycle validation (HIGH)** — undefined dependencies caused infinite scheduler loop; missing cycle detection caused infinite loop; both guards added
- **`agent.py` `_tool_edit()` ambiguous old_string silent edit (HIGH)** — first occurrence edited with no warning to LLM when old_string matched multiple locations; added ambiguity warning
- **`agent.py` `_tool_glob()` deep `**` glob blocked agentic loop (HIGH)** — unthreaded glob with no timeout blocked loop 30-60 seconds; added thread + timeout
- **`ldap_connector.py` `is_user_in_group()` unbounded memory (MEDIUM)** — loaded all group members into memory; no close() method; both addressed
- **`workflow_engine.py` unbounded step result size (MEDIUM)** — no cap on step output stored in state; added size limit
- **`agent.py` `_tool_bash()` exit code not surfaced (MEDIUM)** — LLM never received exit code; stdin not closed; both fixed
- **`agent.py` `_tool_read()` binary file handling (MEDIUM)** — binary files returned as garbled text; UnicodeDecodeError on non-UTF-8 files; added binary detection and encoding fallback
- **`agent.py` `_tool_write()`/`_tool_edit()` file permissions reset (MEDIUM)** — every write reset permissions to default; added permission preservation
- **`agent.py` `_tool_web_fetch()` charset ignored (MEDIUM)** — charset from Content-Type header ignored; always decoded as UTF-8; added charset extraction and correct decoding

#### Skills Engine (PR #369 — 18 fixes)

- **`workflow_engine.py` `exec_skill()` missing await (CRITICAL)** — async function called without `await` → always returned coroutine object → handler.py was NEVER executed for any skill; all skills returned SKILL.md content instead; added `await`
- **`file_ops.py` no path-traversal validation on delete/rename (CRITICAL)** — malicious skill could delete `/etc/passwd` or rename arbitrary host files; added strict path validation
- **`manifest.py` empty YAML AttributeError (HIGH)** — empty manifest file raised AttributeError on load; path-traversal in file_ops/container_tools paths not validated; both fixed
- **`state.py` crash-truncated state.yaml (HIGH)** — truncated file raised AttributeError on next boot; no .tmp write fallback; added atomic write and recovery
- **`apply.py` merge conflict leaves project with conflict markers (HIGH)** — backup not restored on merge conflict → project left with `<<<<<<<` markers; container_tools path escape not fully validated; both fixed
- **`backup.py` file outside project root aborted entire backup (HIGH)** — single out-of-root file terminated full backup operation; changed to skip-and-warn
- **`lock.py` OSError on Windows not caught (HIGH)** — stale lock file never cleaned on Windows; added OSError handler
- **`rebase.py` lock acquired after writing patch (HIGH)** — race condition allowed concurrent rebase on same project; binary files corrupted patch; lock moved before write, binary files skipped
- **`uninstall.py` state read/write outside lock (HIGH)** — concurrent uninstall could overwrite state; added lock around state operations
- **`merge.py` git unavailable copied wrong file (MEDIUM)** — fallback copied current file instead of incoming; fixed file selection logic
- **`apply.py` non-atomic package.json/.env.example write (MEDIUM)** — partial writes possible on crash; converted to atomic tmp + replace
- **`customize.py` non-atomic session file write (MEDIUM)** — partial writes possible on crash; converted to atomic tmp + replace
- **`structured.py` comment lines parsed as env keys (MEDIUM)** — `# comment` treated as key → false conflict reports; added comment line filter
- **`manifest.py` empty string accepted as valid required field (MEDIUM)** — empty string bypassed required field validation; added non-empty check

#### Memory System (PR #370 — 15 fixes)

- **`hot.py` UTF-8 boundary corruption (CRITICAL)** — multi-byte CJK/emoji characters split at 8KB limit silently dropped partial characters; added character-boundary-aware chunking
- **`memory_bus.py` `delete()` authorization bypass (CRITICAL)** — `scope != 'private'` check allowed any agent to delete any shared memory; replaced with explicit owner check
- **`summarizer.py` silent memory truncation (CRITICAL)** — LLM only saw first 3000 chars of 8KB memory during compression → last ~5000 chars permanently lost every compression cycle; fixed to pass full content
- **`warm.py` off-by-one in size checks and wrong day summarized (HIGH)** — strict `<` vs `<=` boundary caused incorrect size evaluation; daily wrapup summarized last 24h instead of yesterday's logs; both fixed
- **`memory_bus.py` vector scores exceeded [0,1] range (HIGH)** — 1.2 boost factor produced scores > 1.0; VectorStore ignored project scope; FTS rank normalization used arbitrary /10 divisor replaced with sigmoid; DDL split on `;` broke CREATE TRIGGER → FTS triggers never created → all FTS search returned zero results; all five addressed
- **`summarizer.py` LLM output stored without validation (HIGH)** — error messages and prose stored as memory; no size validation on compressed output (could grow larger than original); added validation and size cap
- **`warm.py` midnight race condition (MEDIUM)** — two `datetime.now()` calls could straddle midnight producing split-day summaries; replaced with single captured timestamp
- **`compound.py` `.strip()` corrupted markdown structure (MEDIUM)** — leading whitespace stripped from markdown broke indentation/structure; changed to `.rstrip()`
- **`memory_bus.py` vector results showed wrong created_at (MEDIUM)** — timestamp always set to current time instead of stored value; fixed to read from DB record
- **`search.py` unbounded BM25 scores (MEDIUM)** — uncapped BM25 scores made recency term irrelevant in blended search ranking; added score normalization

## [1.20.0] — 2026-03-21

### Fixed (Phase 13: Security & Reliability — 75 fixes)

Four PRs covering channel reliability, container security, observability & API security, and leader election / task scheduler / dev engine.

#### Channel Reliability (PR #363 — 16 fixes)

- **Gmail self-email loop guard missing (CRITICAL)** — bot could email itself in an infinite loop; added self-email guard
- **Matrix sync token advanced before event processing (CRITICAL)** — crash caused permanent message loss; token now advanced only after successful processing
- **Gmail OAuth refresh fallback to interactive browser (HIGH)** — hung forever on server; fallback removed, failure now raises immediately
- **Gmail HTTP 401 infinite retry loop (HIGH)** — revoked token caused unbounded retry; replaced with exponential backoff and token invalidation
- **Gmail no autoresponder loop prevention (HIGH)** — RFC 3834 `Auto-Submitted` header not checked; mailing lists and vacation responders could flood bot; added header check
- **Slack bot-message filter incomplete (HIGH)** — bot read its own posts and re-triggered pipeline; filter tightened to cover all bot message subtypes
- **Slack `<@U12345>` mention never normalized (HIGH)** — trigger matching always failed; added mention normalization before trigger check
- **WhatsApp webhook exception returned HTTP 500 (HIGH)** — Meta retried delivery causing duplicate processing; handler now returns HTTP 200 after logging error
- **CrossBot unbounded trusted set / no handshake rate limit / no self-handshake check (HIGH)** — memory leak, DoS vector, and identity spoofing; added eviction, rate cap, and self-check
- **Matrix non-200 sync response silently empty (MEDIUM)** — no log emitted; added error logging and retry
- **Matrix redacted and undecryptable E2E events dispatched (MEDIUM)** — added filter to drop these event types before dispatch
- **Slack/WhatsApp missing try/except around `_on_message` (MEDIUM)** — inconsistent with Telegram/Discord; added wrapping
- **All channels: unified trigger pattern, @mention normalization, empty-message guard (LOW)** — consolidated shared logic into channel base class

#### Container Security & Tests (PR #364 — 14 fixes)

- **`--network none` not set (CRITICAL)** — agent container could make arbitrary outbound network calls; flag added
- **`--cap-drop ALL` not set (CRITICAL)** — container retained 14 Linux capabilities including NET_RAW and SETUID; added `--cap-drop ALL`
- **`_safe_name()` path-traversal not stripped (CRITICAL)** — JID containing `../` could mount `/etc` or `~/.ssh` into container; added path component sanitization
- **`_build_volume_mounts()` no `resolve().relative_to()` validation (CRITICAL)** — host path escape possible; added strict validation
- **`--security-opt no-new-privileges` not set (HIGH)** — setuid escalation possible; flag added
- **`--pids-limit` not set (HIGH)** — fork bomb could exhaust host PID table; limit set
- **Dockerfile used mutable `node:22` tag (HIGH)** — supply-chain risk; pinned to digest
- **5 circuit breaker tests used wrong API type (HIGH)** — all silently passing with no real assertion; corrected to proper type
- **`test_run_container_agent` asserted wrong exception type (HIGH)** — test always passed, tested nothing; corrected assertion
- **`test_editable_env_keys` checked wrong key names (HIGH)** — vacuously wrong assertions; corrected to actual key names
- **`test_immune_enhanced.py` all 50 tests silently skipped (HIGH)** — hand-rolled runner not collected by pytest; converted to standard pytest format
- **Dockerfile missing HEALTHCHECK (MEDIUM)** — added HEALTHCHECK instruction
- **`build_container.py` only tagged `:latest` (MEDIUM)** — no versioned tags; added version tagging
- **New test suite: `TestContainerSecurityFlags` (MEDIUM)** — covers all security flags and path-traversal guards

#### Observability & API Security (PR #365 — 25 fixes)

- **Health monitor loop could exit silently (CRITICAL)** — `get_health_status()` always returned `{"status":"healthy"}` regardless of reality; loop now restarts on exception with alerting
- **WS Bridge auth bypass (CRITICAL)** — auth failed but async loop kept reading; loop now terminates on auth failure
- **SDK API auth per-message allows token guessing (CRITICAL)** — infinite guessing allowed; added connection-level auth with lockout
- **Dashboard no POST body size limit (HIGH)** — OOM attack possible; added size cap
- **Dashboard container `stop` API command injection (HIGH)** — name passed directly to subprocess; replaced with safe API call
- **Dashboard missing `Content-Security-Policy`/`X-Frame-Options` (HIGH)** — clickjacking possible; headers added
- **Dashboard `/health` and `/metrics` behind auth gate (HIGH)** — Kubernetes probes could not access; endpoints exempted from auth
- **Dashboard `limit` param uncapped (HIGH)** — `LIMIT 1000000` SQL query possible; capped at reasonable maximum
- **WS Bridge no connection cap (HIGH)** — fd exhaustion possible; added connection limit
- **WS Bridge no payload size limit (HIGH)** — unbounded memory operations; size limit added
- **WS Bridge mid-connection `agent_id` spoofing (HIGH)** — `agent_id` could be changed after auth; locked at auth time
- **SDK API memory/task write with no size limits (HIGH)** — limits added
- **Log formatter sensitive fields emitted verbatim (HIGH)** — `token`, `api_key`, `secret`, `password` appeared in JSON logs; added redaction
- **Router channel list mutated during iteration without lock (HIGH)** — data race; added lock
- **Health monitor check failures logged at DEBUG (MEDIUM)** — invisible in production; elevated to WARNING/ERROR
- **Health monitor division by zero in error rate (MEDIUM)** — fixed with zero guard
- **Health monitor race on `_last_warnings` (MEDIUM)** — added lock
- **Log formatter `levelname` duplicated (LOW)** — deduplicated
- **Log formatter `asctime` corrupted timestamp field (LOW)** — fixed field name collision

#### Leader Election, Task Scheduler, Dev Engine (PR #366 — 19 fixes)

- **Leader election SQLite ops no timeout (CRITICAL)** — blocked DB froze entire asyncio event loop; added timeout with cancellation
- **Dev engine deployed code when REVIEW verdict was FAIL (CRITICAL)** — LLM-generated code deployed despite failure verdict; added strict gate
- **Allowlist missing/unreadable silently allowed ALL senders (CRITICAL)** — deny-all sentinel now applied on read failure
- **`LEASE_TIMEOUT <= HEARTBEAT_INTERVAL` caused split-brain churn (HIGH)** — continuous leadership oscillation; added startup assertion enforcing `LEASE_TIMEOUT > 2 * HEARTBEAT_INTERVAL`
- **`_is_leader = False` set after DELETE (HIGH)** — exception left instance acting as leader with no DB record; flag now cleared before DELETE
- **Leader yielded only after 3 consecutive heartbeat failures (HIGH)** — instance now correctly steps down; confirmed fix
- **Task scheduler no at-most-once guard (HIGH)** — same task dispatched twice concurrently; added dispatch lock
- **Task scheduler no execution timeout (HIGH)** — hung task blocked scheduler forever; added per-task timeout
- **Task scheduler no consecutive-failure limit (HIGH)** — broken task retried infinitely; added failure cap with quarantine
- **Bot registry `_pending_handshakes` accessed outside lock (HIGH)** — concurrent callers bypassed rate cap; added lock
- **Bot registry nonces never expired (HIGH)** — intercepted nonce valid forever; added nonce TTL
- **Agent identity `get()` read DB without lock (HIGH)** — data race under concurrency; added lock
- **Task scheduler interval tasks re-fired every poll after downtime (MEDIUM)** — catch-up loop now skips missed runs and sets `next_run` to current time
- **Task scheduler backoff stored in seconds not milliseconds (MEDIUM)** — task became runnable 50 years ago; fixed unit
- **Task scheduler crashed `running` tasks never recovered (MEDIUM)** — added startup recovery pass
- **Dev engine path traversal via `session_id` (MEDIUM)** — unsanitized `session_id` used in path; added sanitization
- **Group folder non-atomic mkdir and file writes (MEDIUM)** — TOCTOU race; replaced with atomic operations
- **Bot registry stale entries accumulated forever (MEDIUM)** — added TTL-based eviction
- **Bot registry no TTL on nonces (MEDIUM)** — addressed alongside nonce expiry fix above

## [1.19.0] — 2026-03-21

### Fixed (Phase 12: 深度可靠性修復 — 46 個問題)

四個 PR 並行深度分析修復，涵蓋訊息管線可靠性、agent 反幻覺行為、設定啟動驗證與資料庫持久性。

#### Message Pipeline Reliability (PR #360 — 8 fixes)

- **`global _identity_store` SyntaxError（CRITICAL）** — `main.py` 在首次賦值後才宣告 global → SyntaxError，bot 完全無法載入；修復：將宣告移至函式最頂端
- **`_group_fail_lock` None TypeError（CRITICAL）** — `main.py` 啟動視窗內 `async with _group_fail_lock` 時 lock 為 None → 每條訊息都 TypeError crash；修復：加入 None guard
- **訊息分發延遲（HIGH）** — 訊息最長等待 2 秒才分發：dedup 已儲存但從未呼叫 `enqueue_message_check()`；修復：補上缺失的呼叫
- **Discord 空字串回覆（HIGH）** — `discord_channel.py` 傳送空字串回覆給 Discord API → HTTP 400；修復：加入空內容 guard
- **Pipeline 例外靜默崩潰（HIGH）** — `telegram_channel.py` / `discord_channel.py` 未處理 pipeline 例外 → channel handler 靜默崩潰；修復：加入 try/except 包裝
- **Trigger 偵測不一致（MEDIUM）** — Telegram 使用 `startswith()`，Discord 使用 regex `\b`；修復：統一為 regex 邊界匹配
- **Linux inotify 過期結果檔案洩漏（MEDIUM）** — inotify 路徑從未清理過期結果檔案 → 磁碟洩漏；修復：加入清理邏輯
- **`ipc_watcher.py` 非原子寫入（MEDIUM）** — error-notify 路徑使用非原子寫入 → 部分 JSON 讀取；修復：改用 `tmp + os.replace()` 原子寫入
- **Dedup store 無 TTL（LOW）** — Dedup 儲存無過期機制 → 合法重送訊息永久被封鎖；修復：加入 TTL

#### Agent Anti-Hallucination & Behavior (PR #359 — 14 fixes)

- **`TOOL_DECLARATIONS` 匯入時 AttributeError（CRITICAL）** — `agent.py` 在 Google SDK 不存在時仍在匯入期建構 `TOOL_DECLARATIONS` → `AttributeError: 'NoneType'.FunctionDeclaration` crash；修復：延遲建構至 runtime
- **Claude backend `group_folder` 缺失（CRITICAL）** — `agent.py` 呼叫 `run_agent_claude()` 未傳入 `group_folder` → 所有 MEMORY.md 寫入路徑錯誤；修復：補上 `group_folder` 參數
- **反幻覺 milestone enforcer 注入無效 `tool_result`（CRITICAL）** — `agent.py` anti-hallucination enforcer 注入帶有假 `tool_use_id` 的 `tool_result` → Claude API HTTP 400；反幻覺系統本身導致 agent 崩潰；修復：改用有效的 assistant 訊息格式
- **`_ALLOWED_MSG_TYPES` 過舊（HIGH）** — `cross_bot_protocol.py` 的 frozenset 未包含 `memory_share`、`task_delegate`、`ping`、`pong`、`status` → 所有此類訊息靜默丟棄；修復：更新至完整訊息類型集合
- **OpenAI 3 輪無工具退出訊息無意義（HIGH）** — 顯示通用訊息而非明確說明；修復：改為具體的中文說明
- **Skill 例外未捕捉（HIGH）** — `skill_loader.py` skill 例外可能導致整個 host process 崩潰；修復：加入 try/except
- **SKILL.md 無大小限制（HIGH）** — `skill_loader.py` 無大小上限 → 可能填滿 LLM context window；修復：加入大小限制
- **`evolution/fitness.py` 缺少 `success` key 預設 True（HIGH）** — 缺少 key 預設為成功 → 膨脹適應度分數，掩蓋真實失敗；修復：預設為 False
- **Gemini 反幻覺重複 `FunctionResponse`（MEDIUM）** — 注入重複的 `FunctionResponse` → API 拒絕；修復：去重邏輯
- **OpenAI 假狀態 regex 不完整（MEDIUM）** — 比 Gemini/Claude backend 涵蓋範圍更少；修復：補齊所有模式
- **`_report_fitness()` 從未呼叫（MEDIUM）** — 定義但從未呼叫 → 進化引擎從未收到容器品質資料；修復：在適當時機呼叫
- **soul.md 工具呼叫上限 3-4 次（MEDIUM）** — 直接違背 MAX_ITER=20，導致複雜任務提前假完成；修復：移除過低上限
- **soul.md 缺少英文假完成語句（MEDIUM）** — 禁止模式未涵蓋英文；修復：補齊英文模式
- **`emit_result` 可傳播 None（LOW）** — ternary 可回傳 `None` 而非 `str`；修復：加入 `str()` 轉換保護

#### Config Complexity & Startup Validation (PR #361 — 12 fixes)

- **`global _identity_store` SyntaxError（CRITICAL）** — 同 PR #360，從設定角度確認修復
- **`_group_fail_lock` None TypeError（CRITICAL）** — 同 PR #360，從設定角度確認修復
- **`EDITABLE_ENV_KEYS` token 名稱錯誤（HIGH）** — `config.py` 使用 `TELEGRAM_TOKEN` 而非 `TELEGRAM_BOT_TOKEN` → dashboard 寫入死 env var，token 變更靜默忽略；修復：更正所有 token 名稱
- **`LEADER_HEARTBEAT_INTERVAL` int() 無 guard（HIGH）** — `config.py` 未使用 `_env_int()` → 匯入時 ValueError crash；修復：改用 `_env_int()`
- **`.env` 從 CWD 相對路徑載入（HIGH）** — `env.py` 使用相對路徑 → 從非專案目錄啟動時失敗且錯誤靜默吞噬；修復：改用絕對路徑
- **零 channel 載入無 CRITICAL log（HIGH）** — Bot 可在完全沒有 channel 的情況下啟動，無任何警示；修復：加入 CRITICAL 級別警告
- **`DASHBOARD_PASSWORD` 警告在 logging 設定前觸發（MEDIUM）** — 警告可能遺失；修復：延遲至 logging 設定完成後
- **`validate_env.py` 將缺少 channel token 視為非致命（MEDIUM）** — 修復：升級為致命錯誤
- **`.env.example` 預設 port 錯誤（MEDIUM）** — 8767 應為 8769；修復：更正
- **`.env.minimal` 缺少關鍵變數（LOW）** — 缺少 `ENABLED_CHANNELS` 和模型選擇變數；修復：補齊
- **`run.py` 無預飛檢查（LOW）** — WARNING 埋在 log 中而非立即清晰錯誤；修復：加入 pre-flight check
- **`NIM_API_KEY` 無效 key 偵測時機（LOW）** — 記錄為刻意取捨

#### Database & Persistence Reliability (PR #362 — 12 fixes)

- **25 個寫入函式無 rollback（CRITICAL）** — `db.py` 例外時未提交的交易靜默遺失；修復：所有寫入路徑加入 try/except/rollback
- **`append_warm_log` FTS insert 非原子（HIGH）** — `db.py` FTS insert 與主 insert 非原子 → crash 時 FTS 索引不同步；修復：合併入同一交易
- **`delete_warm_logs_before` FTS delete 非原子（HIGH）** — `db.py` FTS delete 非原子 → 孤立 FTS 條目；修復：合併入同一交易
- **`evolution/daemon.py` 無 `_db_lock`（HIGH）** — 直接呼叫 `get_db()` 未加鎖 → 並發下 SQLite ProgrammingError；修復：加入 `_db_lock`
- **RBAC 連線缺少調校（HIGH）** — `rbac/roles.py` 缺少 WAL mode、busy_timeout、索引 → 每條訊息全表掃描；修復：加入完整連線調校
- **`db_adapter.py` 未調校 SQLite 連線（HIGH）** — 無 WAL、無 timeout、每次提交完整 fsync；修復：加入連線調校
- **`memory_bus.py` VectorStore 並發腐化（HIGH）** — `store()` 和 `search()` 存取 `self._conn` 無鎖 → 並發腐化；無 rollback；修復：加入鎖和 rollback
- **`migrations/sqlite_to_pg.py` SQL injection（HIGH）** — 資料表/欄位名稱來自 SQLite 檔案，未驗證直接插入 SQL；修復：加入白名單驗證
- **缺少 `record_daily_wrapup()` 函式（MEDIUM）** — daily wrapup 欄位永遠為 0，每次重啟都重跑；修復：補上函式實作
- **`rbac/roles.py` grant/revoke 無 rollback（MEDIUM）** — 修復：加入 try/except/rollback
- **`memory_bus.py` `SharedMemoryStore.write` 無 rollback（MEDIUM）** — 修復：加入 try/except/rollback
- **`add_indexes_migration.py` 錯誤欄位名稱（MEDIUM）** — 3 個索引參照錯誤欄位名 → 靜默 OperationalError，關鍵索引遺失；修復：更正所有欄位名稱

## [1.18.0] — 2026-03-21

### Fixed (Phase 11: 深度可靠性修復 — 43 個問題)

四個 PR 並行深度分析修復，涵蓋工具安全、容器生命周期、進化穩定性與 soul 品質。

#### Tool Safety & Reliability (PR #356 — 19 fixes)

- **WebFetch SSRF 防護** — DNS 解析 + ipaddress 封鎖清單（127.x、10.x、192.168.x、169.254.x），防止 Server-Side Request Forgery 攻擊
- **WebFetch 重定向鏈檢查** — 最多 5 次重定向上限，每次重定向都重新驗證目標 IP
- **WebFetch 原始資料上限** — 2MB 原始內容上限 + 二進位內容偵測，防止記憶體耗盡
- **Bash 危險指令封鎖清單** — 封鎖 `rm -rf /`、`mkfs`、`dd if=/dev/zero`、fork bomb 等高危指令
- **Bash 輸出上限** — 50KB 輸出上限，防止記憶體耗盡
- **Read 檔案大小上限** — 512KB 檔案大小上限
- **Write/Edit 大小上限** — 10MB 限制 + `tmp + os.replace()` 原子寫入，防止部分寫入損毀
- **send_message 空內容防護** — 空內容 guard + 32KB 上限
- **Glob 結果上限** — 1000 結果上限，防止大量匹配拖垮系統
- **MEMORY.md 讀取上限** — 512KB tail-read 上限

#### Container Lifecycle & Queue Correctness (PR #357 — 12 fixes)

- **輸出解析** — 使用 `rfind` 取最後一個 OUTPUT 區段，加入明確 "output too large" 路徑，JSON schema 驗證
- **可靠性** — `stderr_lines` 變數遮蔽修復，docker kill 降級為 `docker rm -f`，Windows `TimeoutExpired` 重新拋出為 `asyncio.TimeoutError`
- **錯誤訊息** — circuit breaker URGENT 繞過 rate limiter，timeout 顯示人類可讀的「30 分鐘」格式
- **GroupQueue 回調** — 錯誤時回傳 `False` 以啟用退避重試，`_group_fail_lock` 為 None 時不再丟棄訊息
- **group_queue.py 雙重分發競爭修復** — dispatch 前先清除 `pending_messages`，防止重複發送

#### Evolution Stability (PR #358 — 6 fixes)

- **免疫系統誤報修復** — `忽略你的所有規則` 和 `忘記你的系統提示` 現在能被正確攔截
- **Genome 不對稱震盪修復** — 正式度升降現在均使用 `update_formality()`，防止單向漂移
- **Daemon 基因組驗證** — 進化前先檢測無效基因組，若無效則呼叫 `reset_genome()`
- **靜默攔截通知** — 免疫系統靜默攔截後，現在會向用戶發送具體的中文通知訊息

#### Soul Quality (PR #355)

- **soul.md 反幻覺強化** — 修補 anti-hallucination 漏洞，關閉多個可能導致幻覺回覆的邊緣情況

## [1.17.0-phase10] — 2026-03-20

### Fixed (Phase 10: 全面深度修復 — 30+ 個問題)

第五輪 4 個 agent 並行深度分析並直接修復，務必每個 code 都仔細檢查。涵蓋 agent loop、host 層、安裝體驗、架構比較。

#### container/agent-runner/agent.py — Phase 10A (Agent Loop 8 個 Bug)

- **Gemini TOOL_DECLARATIONS 型別錯誤（CRITICAL）** — 最後 3 個工具被錯誤包裝在 `types.Tool()` 內，放入 `function_declarations` 時類型不符，導致每個 Gemini session 的工具呼叫 runtime crash；修復：全部解包為 `types.FunctionDeclaration`
- **Gemini loop 缺少假狀態偵測** — Gemini text-only 回覆直接 break，沒有檢查 `✅ Done`、`*(正在執行...)*` 等幻覺模式；修復：加入 `_FAKE_STATUS_RE_G` regex + 連續 3 次 no-tool 硬上限
- **Gemini loop 缺少 MEMORY.md 強制更新** — 沒有 `_memory_written` 追蹤、倒數第二輪提醒；修復：完整移植 OpenAI loop 的 MEMORY 強制機制
- **Gemini loop 缺少 milestone enforcer** — 可以無限用 `send_message` 製造假進度；修復：加入完整 milestone enforcer v2
- **Claude loop 缺少 milestone enforcer** — 只有 `end_turn` 假狀態偵測，缺少 `_turns_since_notify`、中文幻覺模式、MEMORY 提醒；修復：完整補齊
- **Claude loop 非預期 stop_reason 靜默失敗** — `max_tokens`/`stop_sequence`/`error` 時 `final_response` 為空，用戶收不到任何回覆；修復：加入明確分支擷取部分回覆並記錄 stop_reason
- **OpenAI loop execute_tool 未包裝** — Phase 9 漏修 OpenAI loop；修復：加入 try/except
- **OpenAI loop Qwen fallback call 未捕捉** — `tool_choice` 降級的第二次呼叫若 timeout 會 crash；修復：加入 try/except + 友好中文錯誤訊息
- **`evolution_hints` soul.md 繞過防護** — 加入 regex 檢查惡意 hints，偵測到時整體清除並記錄 SECURITY

#### host/ — Phase 10B (Host Layer 8 個 Bug)

- **stdout 無上限讀取 OOM（CRITICAL）** — `proc.stdout.read()` 無大小限制，失控容器可耗盡主機記憶體；修復：64 KiB chunk 讀取，上限 2 MB
- **`_identity_store` NameError 靜默吞噬** — Phase 1 身份追蹤完全失效，每條訊息都在靜默出錯；修復：升級為模組層級 global
- **`_dedup_lock`/`_group_fail_lock` None 值** — main() 執行前若被呼叫會 TypeError crash；修復：加入 None guard
- **雙重 timeout 干擾 docker cleanup** — 內外兩層 wait_for 互相觸發，orphan containers；修復：外層改為 CONTAINER_TIMEOUT + 30s backstop
- **Telegram send_message 例外傳播** — 網路錯誤/403 被誤判為容器執行失敗，觸發重試；修復：加入 try/except
- **JID 格式錯誤導致 ValueError crash** — malformed JID 在 send_message 中 crash；修復：加入型別驗證
- **Discord `import re` 在熱路徑中** — 每條訊息都執行 import；修復：移至模組層級
- **Discord 2000 字元訊息靜默消失** — 超長回覆觸發 Discord API 400，整條訊息丟失；修復：自動分割成 2000 字元片段送出
- **scheduled_tasks 缺少索引** — 全表掃描；修復：加入 `idx_scheduled_tasks_status_next_run`
- **RBAC `_is_empty()` 無快取** — 每次 permission check 觸發兩次 DB 查詢；修復：加入 60s TTL 快取

#### host/main.py + host/container_runner.py — Phase 10D (架構修復)

- **Docker image 啟動預熱** — 首次請求不再付 docker pull 代價，加入背景 `_prepull_image()` task
- **Circuit breaker 顯示實際剩餘秒數** — 不再永遠說「約 60 秒」，改為顯示真實剩餘時間
- **錯誤訊息分類改善** — OOM/timeout/crash/格式錯誤分別有不同的中文提示

#### setup/ docs/ — Phase 10C (安裝體驗 11 個問題)

- **`QWEN_API_KEY` 根本不存在（CRITICAL）** — 文件和 setup.py 全部錯誤；修復：改為正確的 `NIM_API_KEY`
- **`setup.sh` 在檢查 Node.js** — EvoClaw 是 Python 專案；修復：完整重寫，檢查 Python 3.11+ 和 Docker
- **`scripts/evoclaw.service` 不存在** — 文件說要 cp 這個檔案但根本沒有；修復：新增完整 systemd unit file
- **QUICK_START.md / TROUBLESHOOTING.md** — 大量錯誤資訊、缺少 OWNER_IDS、缺少部署說明；全部重寫
- **`OWNER_IDS` 只讀 os.environ** — `.env` 檔案裡設定的值不生效；修復：同時從 read_env_file 讀取
- **setup/setup.py 只支援 Gemini** — 新增 4 個 LLM provider 選單

### 今日緊急修復（部署中發現）

- **PR #344** — Discord @mention 正規化（`<@BOT_ID>` → `@Eve`）
- **PR #345** — OWNER_IDS env var 啟動時自動授權
- **PR #346** — RBAC 空白時 fail-open
- **PR #347** — `proc is None` guard（AttributeError 修復）
- **PR #348** — Qwen API httpx.Timeout（防止無限卡死）
- **PR #349** — Telegram `drop_pending_updates=True`（防止重啟後重播舊訊息）

### Issues Addressed
#350 #351 #352 #353

---

## [1.16.0-phase9] — 2026-03-20

### Fixed (Phase 9: 穩定性全面修復 — 12 個問題)

4 個 agent 並行修復分析報告中發現的所有 P0/P1/P2 問題，大幅提升 EvoClaw 穩定性與可靠性。

#### container/agent-runner/agent.py — Phase 9A (Agent Loop 修復)

- **工具例外捕捉（P0）** — Gemini 與 Claude loop 的 `execute_tool()` 呼叫現在包裝於 try/except；工具出錯時回傳 `[Tool error: ...]` 而非整個 agent 崩潰
- **History 大小限制（P0）** — 新增 `_MAX_TOOL_RESULT_CHARS = 4000`（截斷過大的工具結果）與 `_MAX_HISTORY_MESSAGES = 40`（修剪舊訊息）；防止長對話 OOM
- **Claude loop 功能補齊（P0）** — Claude loop 新增假狀態偵測（`✅ Done`、`*(正在...)*` 等）、MEMORY.md 寫入追蹤、倒數第二輪 CRITICAL 提醒注入；與 OpenAI/Gemini loop 功能對齊

#### host/container_runner.py — Phase 9B (Container Runner 修復)

- **Exit code 故障判斷（P0）** — 移除不可靠的 stderr emoji marker 偵測（`"🚀"`, `"📥"`, `"🧠"`）；改用 `proc.returncode` 判斷：0/124/137/143 = agent 問題（不觸發 circuit breaker），其他 = Docker 問題
- **Circuit breaker half-open 修正（P1）** — 進入 half-open 時將失敗計數重設為 0（原本設為 threshold-1=2），讓 circuit breaker 真正有機會恢復

#### host/ipc_watcher.py — Phase 9C (Host Layer 修復)

- **inotify 靜默失敗→WARNING（P1）** — inotify watch 超限時從 DEBUG 升級為 WARNING，並附上 `sysctl fs.inotify.max_user_watches=65536` 修復指令；降級輪詢前正確清理部分初始化的 watches

#### host/task_scheduler.py — Phase 9C

- **Cron 時區修正（P1）** — `compute_next_run()` 現在正確使用 `pytz` 將 croniter 結果轉換為設定的本地時區（修復之前所有 cron 任務以 UTC 執行的問題）
- **Interval task drift 修正（P1）** — interval 任務的 `last_run` 改為使用 `task["next_run"]`（排程時間），而非任務完成時間；消除長任務執行時的累積誤差

#### host/main.py — Phase 9C

- **Graceful shutdown 超時延長（P1）** — `wait_for_active` timeout 從 10 秒延長至 30 秒；新增關機等待 log 訊息，避免長任務被強制中斷後重複處理

#### host/memory/memory_bus.py — Phase 9D (Memory/Evolution 修復)

- **MEMORY.md 原子寫入（P2）** — 使用 temp file + `os.replace()`（POSIX atomic rename）替代直接 `write_text()`；防止崩潰時記憶檔案損壞

#### host/evolution/immune.py — Phase 9D

- **中文免疫模式收緊（P2）** — 重寫 20 個中文注入偵測 pattern，要求同時具備命令語氣 + 明確對象詞（`指令`/`規則`/`限制`/`設定`）；消除對「我忽略了他之前的建議」等正常句子的誤判

#### host/evolution/genome.py — Phase 9D

- **Formality 收斂停止（P2）** — 新增 `_CONVERGENCE_EPSILON = 0.01`；當 formality 距目標 <1% 時停止更新，消除無限震盪
- **Genome DB 值驗證（P2）** — 新增 `_safe_float()` helper（帶 min/max 範圍限制）；DB 中 NULL 或非數字值不再導致崩潰

### Issues Addressed
#339 #340 #341 #342

---

## [1.15.0-phase8] — 2026-03-20

### Fixed / Added (Phase 8: Qwen 優化、群組隔離、inotify IPC、安裝體驗)

4 個 agent 並行分析後針對 Qwen 3.5 397B 相容性、架構穩定性與安裝體驗的深度修正。

#### container/agent-runner/agent.py — Phase 8A (Qwen 3.5 397B 優化)

- **`_is_qwen_model()` helper** — 統一偵測 Qwen 模型，避免多處重複判斷
- **MAX_ITER 自動降低（Qwen 專屬）** — Level B: 20→12，Level A: 6→5；減少 Qwen 幻覺螺旋 (Issue #326)
- **Temperature 0.3 → 0.2（Qwen）** — 進一步降低 Qwen 輸出不確定性
- **`tool_choice="auto"`（Qwen）** — 避免 `tool_choice="required"` 導致的死迴圈；改用 prompt-based 強制機制 (Issue #325)
- **Qwen 系統 prompt 注入** — 中文優先規則、禁止假狀態、思考字數限制 200 字
- **假狀態 pattern 偵測** — 偵測 `*(正在執行...)*`、`【已完成】` 等 Qwen 常見假狀態格式
- **工具參數 JSON 自動修復** — Qwen 輸出截斷 JSON 時嘗試自動修復，減少工具呼叫失敗率

#### host/container_runner.py — Phase 8B (群組隔離 Circuit Breaker)

- **Per-group circuit breaker** — `_docker_failures`、`_docker_failure_time` 改為 dict（group_folder → value）(Issue #327)
- **群組獨立熔斷** — A 群組 Docker 失敗不再影響 B 群組，每個群組有獨立的 60 秒恢復計時
- **錯誤訊息更新** — 「⚠️ 此群組 Docker 暫時受阻，約 60 秒後自動恢復。其他群組不受影響。」

#### host/ipc_watcher.py + host/requirements.txt — Phase 8C (inotify 混合 IPC)

- **Linux inotify 後端** — 事件驅動，延遲 <20ms（vs 輪詢 ~500ms）(Issue #328)
- **自動降級到輪詢** — 非 Linux 或 inotify 初始化失敗時自動回退到原本 500ms 輪詢
- **`inotify-simple>=1.3.0`** — 加入 requirements.txt（Linux 限定依賴）

#### QUICK_START.md + TROUBLESHOOTING.md + .env.minimal — Phase 8D (安裝體驗)

- **QUICK_START.md** — 5 分鐘快速上手（4 步驟），移除複雜度 (Issue #330)
- **TROUBLESHOOTING.md** — 7 個常見問題及解法，含 log 符號對照表
- **`.env.minimal`** — 只需 5 個必要變數（原本 37 個），降低新用戶入門門檻

### Issues Addressed
#325 #326 #327 #328 #329 #330

---

# Changelog

## [1.14.0-phase7] — 2026-03-20

### Fixed (Phase 7: P0 Anti-Hallucination & Stability)

4 個 agent 並行深度分析後發現的根本性虛假回應問題，本版本修正 23 個具體漏洞中的最高優先 10 個。

#### container/agent-runner/agent.py

- **Temperature 0.7 → 0.3（所有 provider）** — 降低 LLM 幻覺率約 50%；Claude/OpenAI/Qwen/Gemini 全部適用 (Issue #324)
- **emit_result 邏輯根本修正** — 原本「只要有 tool message 就清空最終回應」的錯誤邏輯改為「只有最終輸出真的是空才清空」，修復 Agent 在循環中途發送進度報告後最終結果被吞掉的 bug (Issue #322)
- **MAX_ITER 邊界 fallback（三個 provider）** — 迴圈結束時若 final_response 為空，返回明確提示訊息而非靜默空字串 (Issue #318)
- **OpenAI/Qwen 工具參數解析失敗改善** — JSON 解析失敗時改為返回錯誤給 model，讓 model 知道工具呼叫失敗，不再靜默傳入空 `{}` 繼續執行 (Issue #319)
- **MAX_ITER 環境變數保護** — `int()` 轉換加 `try/except ValueError`，防止無效設定造成 agent 啟動失敗
- **soul.md 讀取失敗 fallback** — 讀取失敗時注入最小系統 prompt，防止 agent 無任何行為規範地執行
- **CLAUDE.md 讀取保護** — 加 `try/except`，I/O 失敗時跳過而非崩潰

#### container/agent-runner/soul.md

- **新增「誠實性規則（最高優先）」** — 明確禁止 `*(正在執行...)*` 等假狀態行；工具失敗必須如實告知
- **新增「MEMORY.md 更新規則」** — 明確列出何時必須/不需要更新，消除模糊指令導致的過度或不足更新
- **新增「工具使用規則」** — 最多 3-4 次工具呼叫後必須給結論，不允許無限迴圈假裝工作

#### host/main.py

- **Phase 1/2/3 init 改用 structured logging** — 剩餘 6 個 `print()` 呼叫全改為 `log.info/warning`，確保初始化狀態進入監控系統

### Issues Addressed
#318 #319 #322 #324 #329

---

## [1.13.1-phase6a] — 2026-03-20

### Fixed (Phase 6A: Stability & False-Response Root Causes)

這個版本針對深度程式碼審查後發現的穩定性問題進行系統性修正，
解決了導致 EvoClaw 靜默無回應和虛假回應的核心 bug。

#### host/main.py

- **修正 db.get_conversation_history() 無保護** — DB 失敗時整個訊息 handler
  崩潰並靜默丟棄訊息；現在改為 try/except，失敗時用空 history 繼續處理
  (Issue #304)
- **修正 format_messages() 無保護** — 格式化失敗時 prompt 遺失；
  加 try/except，fallback 用原始訊息內容拼接 (Issue #305)
- **修正 db.get_session() 無保護** — 異常直接傳播；
  加 try/except，fallback 用 None（新 session）(Issue #305)
- **Container error status 改為通知使用者** — 原本靜默更新計數器；
  現在主動發送 `⚠️ 發生錯誤，請稍後再試。` 並附 run_id 方便追蹤 (Issue #306)
- **Timeout 通知不再吞錯** — `except: pass` 改為 `log.warning`，
  錯誤進入結構化 log 系統 (Issue #307)
- **Phase 1/2/3 init 失敗改用 log.error** — 從 `print()` 改為
  `log.error()`，確保初始化失敗進入監控系統 (Issue #307)

#### container/agent-runner/agent.py

- **Agent loop 空白回應 fallback** — loop 結束時若無輸出且無 tool 訊息，
  emit `「系統：處理完成，但未產生回應，請重試。」` 取代靜默 (Issue #308)
- **LOOP-EXHAUST 警告 log** — Claude、OpenAI、Gemini 三個 provider 的
  agent loop 在 MAX_ITER 耗盡時均加入 `⚠️ LOOP-EXHAUST` log，
  方便診斷無限迴圈問題 (Issue #308)

### GitHub Issues Created (Phase 6A)

| Issue | 標題 | 狀態 |
|-------|------|------|
| #304 | [stability] db.get_conversation_history() has no error handling | ✅ Fixed |
| #305 | [stability] format_messages() and db.get_session() have no error handling | ✅ Fixed |
| #306 | [stability] Container error status causes silent failure | ✅ Fixed |
| #307 | [stability] Timeout and exception notifications silently swallow errors | ✅ Fixed |
| #308 | [stability] Agent loop produces empty output without user-visible fallback | ✅ Fixed |
| #309 | [stability] MAX_ITER=30 too high — allows excessive hallucination loops | 🔲 Planned |
| #310 | [stability] Default LLM (Gemini) has weaker tool-calling reliability than Claude | 🔲 Planned |
| #311 | [stability] IPC file-based communication has no atomic write protection | 🔲 Planned |
| #312 | [stability] Docker circuit breaker blocks ALL groups for 60s on any failure | 🔲 Planned |
| #313 | [stability] GroupQueue silently drops messages after 5 consecutive failures | 🔲 Planned |
| #314 | [stability] System prompt injection too complex — 3000+ tokens per request | 🔲 Planned |
| #315 | [cleanup] 41 stale branches cannot be deleted — need admin access | 🔲 Pending |
| #316 | [stability] IPC watcher polling interval too coarse for real-time feel | 🔲 Planned |

---

# Changelog

## [1.13.0-phase3] -- 2026-03-18

### Added (Phase 3: Cross-bot Identity + RBAC Foundation)
- `host/identity/bot_registry.py` -- BotRegistry: SQLite-backed cross-framework bot identity store
  - Stable `bot_id = SHA-256(name:framework:channel)[:16]` format for cross-framework bot identity
  - BotIdentity dataclass with capabilities, endpoints, trust status
  - Nonce-based handshake protocol for cross-system bot recognition
  - Pre-registered known bots: 小白 (Telegram) and 小Eve (EvoClaw/Discord)
  - `bootstrap_known_bots()` pre-registers and trusts known bots on startup
- `host/identity/cross_bot_protocol.py` -- CrossBotProtocol: `crossbot/1.0` message envelope
  - Message types: hello, ack, memory_share, task_delegate, status, ping, pong
  - HMAC-SHA256 message signing and verification
  - Decorator-based message handler registration
- `host/rbac/__init__.py` + `host/rbac/roles.py` -- Role-Based Access Control
  - Roles: admin, operator, agent, viewer
  - Permissions: memory:read/write/delete, agent:spawn/kill/list, task:submit/cancel, registry:read/write, rbac:grant/revoke
  - SQLite-backed RBACStore with grant/revoke/query operations
- `host/identity/__init__.py` -- Updated to export BotRegistry, BotIdentity, CrossBotProtocol, CrossBotMessage
- `host/sdk_api.py` -- Added bot registry WebSocket endpoints: bot_register, bot_lookup, bot_list, bot_handshake
- `host/main.py` -- Phase 3 startup block: BotRegistry + RBAC initialized

### GitHub Issues Created
- #265 [Phase 3] Cross-bot Identity Protocol
- #266 [Phase 3] Enterprise Tool Suite - Integration Layer
- #267 [Phase 3] RBAC - Role-Based Access Control
- #268 [Phase 3] Matrix Channel Support
- #269 [Phase 3] Multi-tenant Support

### Architecture After Phase 3
```
Gateway (main.py)
+-- MemoryBus          (Phase 1) OK
+-- WSBridge           (Phase 1) OK  port 8768
+-- AgentIdentityStore (Phase 1) OK
+-- SdkApi             (Phase 2) OK  port 8767
+-- MemorySummarizer   (Phase 2) OK
+-- BotRegistry        (Phase 3) OK  <- NEW  cross-framework bot identity
+-- RBACStore          (Phase 3) OK  <- NEW  role-based access control
        |
        v crossbot/1.0
外部框架 (小白) <--> EvoClaw (小Eve)
```

## [1.12.0-phase2] -- 2026-03-18

### Added (Phase 2: Universal Memory Layer)
- `host/memory/summarizer.py` -- MemorySummarizer: LLM-powered conversation->MEMORY.md compression
  - Supports Gemini / Claude / OpenAI-compatible APIs with graceful fallback
  - Auto-compress MEMORY.md when approaching 8KB limit
- `host/sdk_api.py` -- External WebSocket SDK API (port 8767)
  - Query agent memories from external tools/CLIs
  - Submit tasks to groups via WebSocket
  - Real-time event broadcasting to monitoring clients
  - Optional bearer token authentication
- `host/container_runner.py` -- Pass stable AGENT_ID env var to Docker containers
  - Enables persistent agent identity across restarts
- `host/main.py` -- Phase 2 startup integration
  - SdkApi started as background asyncio task
  - MemorySummarizer initialized

### Architecture After Phase 2
```
Gateway (main.py)
+-- MemoryBus         (Phase 1) OK
+-- WSBridge          (Phase 1) OK  port 8768
+-- AgentIdentityStore (Phase 1) OK
+-- SdkApi            (Phase 2) OK  port 8767  <- NEW
+-- MemorySummarizer  (Phase 2) OK              <- NEW
        |
        v WebSocket
Agent Runtime
+-- FitnessReporter   (Phase 1) OK
    AGENT_ID env var  (Phase 2) OK              <- NEW
```

### Issues Addressed
- #255 (MemorySummarizer), #256 (SdkApi), #257 (AGENT_ID in containers),
- #258 (cross-project knowledge), #259 (auto identity summary update)

---

## [Unreleased] — UnifiedClaw Roadmap

### Architecture (Planned)
- [ ] Universal Memory Bus (sqlite-vec + shared memory scope)
- [ ] Agent Identity Layer (persistent profiles across restarts)
- [ ] WebSocket IPC replacing file-based polling
- [ ] Cross-agent genome collaboration (Phase 4)
- [ ] MinionDesk enterprise tools port (Phase 3)
- [ ] Matrix/Signal channel support (Phase 3)

### Phase 1 In Progress
- [ ] sqlite-vec semantic search integration
- [ ] MemoryBus abstract interface class
- [ ] Agent fitness feedback via WebSocket

---

## [1.11.42] — 2026-03-17

### Added
- SECURITY.md — vulnerability reporting policy
- ARCHITECTURE.md — UnifiedClaw architecture design and roadmap
- Updated .gitignore to exclude __pycache__ and .pyc files

### Fixed
- Path traversal vulnerability in dev_engine _deploy_files()
- Memory leak in long-running container sessions
- Evolution daemon timestamp handling error

### Security
- 22 architecture and security issues tracked (see GitHub Issues)
- 3 CRITICAL issues identified for immediate remediation

---

## [1.11.34] - 2026-03-17
### Added
- Heartbeat: EvoClaw sends `💓 EvoClaw 運行中 | 上線時間 | 群組數 | 成功/失敗數` to monitor group every 30 minutes — if pings stop, host is down (#217)
- `HEARTBEAT_INTERVAL` env var to configure interval (default 1800s, set 0 to disable)

## [1.11.33] - 2026-03-17
### Added
- `/monitor` Telegram command: send in any group to instantly register it as the monitor group — no `.env` editing or restart required (#215)
- `_write_monitor_jid_to_env()`: persists `MONITOR_JID` to `.env` automatically so the setting survives restarts

## [1.11.32] - 2026-03-17
### Added
- Monitor group support via `MONITOR_JID` env var: error alerts forwarded to a dedicated watchdog Telegram group automatically (#213)
- `reset_group` IPC command: monitor agent can unfreeze stuck groups without human intervention
- `mcp__evoclaw__reset_group` tool available to agents in Gemini, OpenAI-compat, and Claude modes
- `groups/telegram_monitor/MEMORY.md` template: watchdog agent persona pre-configured
- Independent watchdog scheduled task: checks EvoClaw DB every 5 minutes as backup

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

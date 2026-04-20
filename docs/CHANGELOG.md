## [1.27.9] — 2026-04-20

### Fixed
- **Telegram watchdog false-positive kills polling every 5 minutes in quiet groups.** `_last_poll_activity` was only updated on text message receipt, not on successful `getUpdates` calls. In quiet groups with no messages, the 300-second threshold was always hit, triggering a reconnect that often failed — leaving the bot permanently unreachable. Raised threshold to 1800s (30 min) and added a `TypeHandler(Update, ...)` at group=-1 that updates activity on ANY update type. (#541)

### Technical Details
- **Modified Files**: `host/channels/telegram_channel.py`
- **Breaking Changes**: None.

## [1.27.8] — 2026-04-16

### Fixed
- **OOM (exit 137) at openai-compat LLM call still triggers after #539.** Removing the HEALTHCHECK reduced cgroup pressure but the underlying issue remains: `chat.completions.create()` without `stream=True` buffers the entire response and constructs full pydantic v2 models, transiently spiking RSS by hundreds of MB. This made the openai-compat path the dominant OOM trigger after the healthcheck was removed (reproduced 2026-04-16 12:08:12, exit 137 within 4 seconds, zero MEMDEBUG samples). Three changes:
  1. **Streaming mode** in `_loop_openai.py:run_agent_openai()` — wraps `chat.completions.create(stream=True)` and accumulates chunks into a non-streaming-shaped object (`response.choices[0].message.content/tool_calls/finish_reason`) so downstream code is unchanged. Falls back to non-streaming if the provider rejects `stream=True`.
  2. **Byte-based history cap** (`_HISTORY_BYTE_BUDGET = 256 KB`) enforced BEFORE each LLM call, in addition to the existing 40-message count cap. Preserves system message + most recent fitting messages; drops orphan tool messages.
  3. **RSS instrumentation** at every LLM call boundary (`_log_rss(prefix)` reads `/proc/self/status` for VmRSS+VmPeak). If OOMs persist, logs will pinpoint exactly which step spiked. (#541)

### Technical Details
- **Modified Files**: `container/agent-runner/_loop_openai.py` (added `_rss_snapshot`, `_log_rss`, `_trim_history_to_byte_budget`, `_consume_stream` + `_StreamedMessage/Choice/Response/ToolCall/Function` shape-mirror classes; rewrote the LLM call site to use `_do_call(stream=...)` with stream-first + non-streaming fallback)
- **Image rebuild required**: `docker build -t evoclaw-agent:latest container/`
- **Breaking Changes**: None. `tool_choice="required"` retry path preserved. Existing message-count cap at line ~470 preserved.
- **Known limitation**: Some OpenAI-compatible endpoints may not stream tool_calls reliably. The non-streaming fallback covers this.

## [1.27.7] — 2026-04-16

### Fixed
- **Container OOM (exit 137) caused by HEALTHCHECK spawning a second Python process.** The Dockerfile HEALTHCHECK ran `python3 -c "import anthropic, openai; import google.genai"` every 30 seconds inside the same cgroup as the agent. Each healthcheck process loaded all three LLM SDKs (~80-100 MB), and when it coincided with the agent's LLM API call, combined memory exceeded the cgroup limit. Measured: agent steady-state 66 MiB → 147 MiB when healthcheck fires. Replaced with `HEALTHCHECK NONE` — agent containers are run-to-completion (seconds to minutes), not long-lived services. (#539)

### Technical Details
- **Modified Files**: `container/Dockerfile`
- **Breaking Changes**: None. Requires `docker build -t evoclaw-agent:latest container/` to rebuild the image.
- **Root cause evidence**: 4 historical OOM events all correlated with LLM API calls (the timing window where healthcheck + API call overlap). Retries succeeded because healthcheck happened to not be running during the retry.

## [1.27.6] — 2026-04-14

### Fixed
- **Telegram watchdog permanently dies after 5 failed reconnect attempts.** When the staleness watchdog in `host/channels/telegram_channel.py:_poll_watchdog` detected stale polling and attempted to reconnect, a transient network outage (e.g. DNS failure) would cause `connect()` to exhaust its 5-attempt retry limit and raise. The watchdog caught the exception, logged it, then **returned** — permanently killing the watchdog task. No new reconnect would ever be attempted, leaving the bot unreachable until a manual `pm2 restart`. Fixed by replacing the single-shot `connect()` call with an indefinite retry loop using exponential backoff (60s → 120s → ... cap at 900s). Conflict and auth-failure exceptions still exit immediately (unrecoverable). (#536)

### Technical Details
- **Modified Files**: `host/channels/telegram_channel.py` (`_poll_watchdog` method)
- **Breaking Changes**: None. The watchdog now survives network outages instead of permanently dying. Existing behaviour for Conflict/auth errors is unchanged (immediate exit with CRITICAL log).

## [1.27.5] — 2026-04-11

### Fixed
- **Transient console windows flashing on Windows during self-update and auto-update.** `host/ipc_watcher.py` (6 subprocess call sites: claude remote-control, git pull, git rev-parse, pytest gate, git reset --hard, pip install) and the new `host/auto_update.py` (git fetch, git rev-list) did not pass `creationflags=CREATE_NO_WINDOW` when spawning child processes. On Windows this caused a brief `cmd.exe` / `console` window to pop up for every spawn — now visible every hour once `AUTO_UPDATE_ENABLED=true` was rolled out in production. `host/container_runner.py` and `host/dashboard.py` already had this handled via a `_NO_WINDOW` constant; applied the same pattern to the two remaining modules. Linux behaviour unchanged (`_NO_WINDOW = 0` is a no-op). (#534)

### Technical Details
- **Modified Files**: `host/ipc_watcher.py` (added `_NO_WINDOW` constant + `creationflags` on all 6 `create_subprocess_exec` calls), `host/auto_update.py` (same, 2 calls).
- **Breaking Changes**: None.

## [1.27.4] — 2026-04-11

### Fixed
- **`AUTO_UPDATE_*` keys in `.env` silently ignored.** `host/config.py` read the new #530 auto-update keys via plain `os.environ.get(...)`, but EvoClaw's `.env` loader (`host/env.py:read_env_file`) is deliberately non-polluting — it returns a dict, it does not set `os.environ[k]`. Result: putting `AUTO_UPDATE_ENABLED=true` in `.env` had no effect; the loop stayed disabled until operators also set a shell env var. Fixed by applying the same two-level fallback pattern already used by `CONTAINER_MEMORY` / `ENABLED_CHANNELS` (env var → `.env` file → default) to all four `AUTO_UPDATE_*` keys. `_run_self_update` in `host/ipc_watcher.py` now reads `AUTO_UPDATE_TEST_CMD` via the config module (with env-var override still winning) instead of hitting `os.environ` twice. (#532)

### Technical Details
- **Modified Files**: `host/config.py`, `host/ipc_watcher.py`
- **Breaking Changes**: None. Shell env-var override still wins over `.env`; the only behaviour change is that operators who put `AUTO_UPDATE_*` in `.env` (as the #530 README said to) now actually get what they asked for.

## [1.27.3] — 2026-04-10

### Added
- **Test gate + scheduled auto-update trigger for `self_update`.** `_run_self_update` in `host/ipc_watcher.py` now runs a short pytest suite between `git pull` and writing `self_update.flag`; on non-zero exit it `git reset --hard`s back to the pre-pull SHA and aborts the update, so a broken commit on `main` can no longer roll straight into an `os.execv` restart. A new `host/auto_update.py` module adds an optional scheduled loop (gated on `AUTO_UPDATE_ENABLED=true`, default off) that `git fetch`es every `AUTO_UPDATE_INTERVAL_SECS` (default 3600) and invokes `_run_self_update` whenever local HEAD is behind `origin/<branch>`. Also documents — in an inline comment in `host/main.py` — why the restart continues to use `os.execv` rather than shelling out to `pm2 restart` (keeps supervisor PID stable, no dependency on pm2 being on PATH / daemon alive, pm2 `autorestart: true` remains the crash safety net). (#530)

### Technical Details
- **New Files**: `host/auto_update.py`, `tests/test_self_update_test_gate.py`
- **Modified Files**: `host/ipc_watcher.py` (test gate block after git pull), `host/config.py` (`AUTO_UPDATE_ENABLED`, `AUTO_UPDATE_INTERVAL_SECS`, `AUTO_UPDATE_BRANCH`, `AUTO_UPDATE_TEST_CMD`), `host/main.py` (wires `auto_update_loop` into the main gather, adds os.execv rationale comment)
- **New Env Vars**:
  - `AUTO_UPDATE_ENABLED` (default `false`) — enable the scheduled auto-update loop.
  - `AUTO_UPDATE_INTERVAL_SECS` (default `3600`, min `60`) — seconds between fetch checks.
  - `AUTO_UPDATE_BRANCH` (default `main`) — remote branch to track.
  - `AUTO_UPDATE_TEST_CMD` (default `pytest -x --timeout=60 -q tests/`) — test command run inside `_run_self_update` after `git pull`. Set to empty string to skip the gate (not recommended).
- **Breaking Changes**: None. Default behaviour (no env vars set) is unchanged except that manual IPC-triggered `self_update` calls now run the test gate too — updates that fail tests will be rolled back instead of restarting. This is the intended safety improvement.
- **Security**: The scheduled loop bypasses the `SELF_UPDATE_TOKEN` IPC gate intentionally. That token exists to block prompt-injection attacks flowing from an LLM agent into the IPC handler; the scheduled loop runs in trusted host code with no attacker-controlled input.

## [1.27.2] — 2026-04-10

### Changed
- **Lowered `CONTAINER_MEMORY` default from `2g` to `512m`.** Post-#527 analysis showed steady-state agent-process RSS is 80-150 MB (Python + one lazy-loaded LLM SDK + stdlib; no eager pandas/numpy/matplotlib imports). The 2 GB default was masking a single `tool_grep` bug (fixed in #527) and was never calibrated to real footprint. 512 MB leaves ~3× headroom over steady state while cutting the worst-case aggregate (with `MAX_CONCURRENT_CONTAINERS=5`) from 10 GB to 2.5 GB — comfortable on hosts with ≤16 GB RAM. (#528)

### Technical Details
- **Modified Files**: `host/config.py`
- **Breaking Changes**: Users running very long-context openai/claude sessions may want to override `CONTAINER_MEMORY=768m` in `.env`. Do NOT revert to `2g` — the comment in `config.py` now documents the full history.
- **Depends on**: #527 (tool_grep streaming fix). Without it, 512 MB is unsafe under wide-grep workloads.

## [1.27.1] — 2026-04-10

### Fixed
- **`tool_grep` OOM via unbounded stdout read.** `container/agent-runner/_tools.py` previously used `subprocess.run(capture_output=True)` and only truncated the grep output **after** the full stdout had been read into a Python string. A wide pattern against a repo-mounted workspace could produce hundreds of MB of matches and OOM the container (exit 137) before the 8 KB truncation line was ever reached — the 8 KB cap was an illusion. Rewrote `tool_grep` using `Popen` + chunked `read(4096)` that kills the grep process the moment the 8 KB byte budget is hit, so the hard cap is enforced at the kernel pipe level. A `threading.Timer` watchdog replaces the previous `run(timeout=30)` behavior. (#526)

### Technical Details
- **Modified Files**: `container/agent-runner/_tools.py`
- **New Files**: `tests/test_tool_grep_streaming.py` — regression test seeding ~200 KB of matching lines and asserting `tool_grep` returns < 9 KB, plus a Popen spy verifying `proc.kill()` is invoked when the budget is reached.
- **Breaking Changes**: None. Callers of `tool_grep` still see at most 8 KB plus a truncation marker; the marker text changed from `... (truncated, too many matches)` to `... (truncated at 8 KB — refine pattern or narrow include)` to hint at the actual constraint.

## [1.27.0] — 2026-04-10

### Added
- **Formal contribution workflow + CI enforcement.** `CONTRIBUTING.md` now documents the `issue → PR → merge → CHANGELOG → README` flow end-to-end, the PR template gains a **Linked Issue** slot plus checklist items, and a new `.github/workflows/pr-checks.yml` gates PRs on two rules: (1) PR body must contain `Closes/Fixes/Resolves/Refs #N` (escape hatch: `no-issue` label); (2) if the PR touches `host/`, `container/`, `scripts/`, `Makefile`, or `.env.example`, then `docs/CHANGELOG.md` must also be modified (escape hatch: `skip-changelog` label). The workflow is pure bash + `gh` CLI — no third-party actions. (#522)

### Technical Details
- **New Files**: `.github/workflows/pr-checks.yml`
- **Modified Files**: `.github/PULL_REQUEST_TEMPLATE.md`, `CONTRIBUTING.md`
- **New Labels (must be created manually in the repo settings or first use)**: `no-issue`, `skip-changelog`
- **Breaking Changes**: Open PRs that don't link an issue in the body will start failing CI after this merges. Fix by editing the PR body to include `Closes #N` (or add the `no-issue` label).

## [1.26.2] — 2026-04-09

### Fixed
- **Stale `data/` DB path references in three places** — `Makefile:48`, `host/db_adapter.py:27`, and `host/migrations/sqlite_to_pg.py:51` all defaulted to `data/evoclaw.db` / `data/messages.db`, a layout the live host code has not used since `STORE_DIR` was introduced. `make db` was broken for everyone on default paths, and the offline adapter/migrator tools only worked if the caller remembered to set `DB_PATH`/`SQLITE_PATH` by hand. All three now resolve `config.STORE_DIR` so they point at the same DB the running host uses (`%LOCALAPPDATA%\evoclaw\store\` on Windows, `./store/` elsewhere). (#519)

### Technical Details
- **Modified Files**: `Makefile`, `host/db_adapter.py`, `host/migrations/sqlite_to_pg.py`, `.env.example`
- **Makefile**: `db` target now shells out through a small Python one-liner so that `STORE_DIR` is resolved identically to the host process (handling the Windows `%LOCALAPPDATA%` default).
- **db_adapter.py / sqlite_to_pg.py**: Added `from host import config` at module level; both default `DB_PATH` / `SQLITE_PATH` now come from `config.STORE_DIR / "messages.db"`. Explicit env overrides still win.
- **.env.example**: Stale comment updated to describe the new default resolution.
- **Breaking Changes**: None. Anyone relying on the old default by pre-seeding a `data/` directory must now either set `DB_PATH` explicitly or move the file into `STORE_DIR`.

## [1.26.1] — 2026-04-09

### Fixed
- **`append_warm_log()` TypeError on every warm log write** — GAP-10 added `importance` and `memory_type` keyword args in `host/memory/warm.py` but `host/db.py` function signature and schema were never updated, causing all warm log writes to fail with `got an unexpected keyword argument 'importance'` (#509)
- **Embedding API 404 on every vector store write** — `text-embedding-004` model no longer exists in Gemini API; migrated to `gemini-embedding-001` (dim 768→3072) (#511)
- **Embedding always fails: GOOGLE_API_KEY not found** — `VectorStore.embed()` reads only `os.environ` but evoclaw loads secrets via `read_env_file()` without setting env vars; added `.env` fallback (#513)

### Technical Details
- **Modified Files**: `host/db.py`
- **Schema**: Added `importance REAL` and `memory_type TEXT` columns to `group_warm_logs`
- **Migration**: Idempotent `ALTER TABLE ADD COLUMN` runs on startup for existing databases
- **Breaking Changes**: None

## [1.26.0] — 2026-03-23

### 第 19 階段 — 測試、容器生命週期、DB 結構、日誌/監控（PRs #387–390）

#### PR #387 — fix(p19b): 容器生命週期、資源限制、IPC 清理稽核（7 項修復）
- **HIGH** 容器日誌無大小限制——Docker json-file 驅動程式無限累積日誌；加入 `--log-opt max-size=10m --log-opt max-file=2`
- **MEDIUM** 容器 `/tmp` 無界——overlay 寫入可能耗盡主機儲存空間；加入 `--tmpfs /tmp:size=64m,mode=1777`
- **MEDIUM** `_stop_container()` 立即發出 `docker kill`（SIGKILL）——agent 沒有時間刷新 IPC 檔案；改為先 `docker stop --time 5` 發出優雅的 SIGTERM
- **MEDIUM** `agent-browser` npm 套件未版本化——供應鏈風險；固定至 `agent-browser@1.1.5`
- **MEDIUM** 9 個 Python Dockerfile 套件無版本下限——構建不可重現；加入明確的 `>=` 下限
- **LOW** `build.sh` 未在構建後驗證映像是否存在——加入 `docker image inspect` 檢查
- **LOW** Dockerfile 中無明確的 `STOPSIGNAL SIGTERM`

#### PR #388 — fix(p19d): 日誌、健康檢查、CI 流水線、錯誤恢復稽核（6 項修復）
- **HIGH** `/health` 端點僅檢查 DB 和 Docker——零頻道部署返回 `"ok"`；加入 `channel_ok`、`leader`、`monitor_alive` 檢查；Docker 異常現在也設置 `status="degraded"`
- **HIGH** CI 流水線從未執行 Python 測試——`pytest tests/` 完全缺失於 `.github/workflows/ci.yml`；加入 Python 3.11 上的 `python-tests` 任務
- **MEDIUM** 健康監控異常以 DEBUG 級別記錄——提升至 `log.warning()`
- **MEDIUM** 排程解析失敗靜默返回 `None`——任務消失無任何日誌條目；三個排程類型分支現在均呼叫 `log.warning()`
- **LOW** systemd 單元缺少 `LimitNOFILE=65536`——多群組部署觸及核心 fd 限制，inotify 靜默停止
- **LOW** 2 個 IPC watcher 路徑中的 `except Exception: pass`——改為 `log.debug()`

#### PR #389 — fix(p19c): 資料庫結構完整性、遷移、查詢正確性稽核（11 項修復）
- **HIGH** `evolution_runs.success DEFAULT 1`——所有未明確設置成功標誌的列被計為成功；適應度分數被永久膨脹；改為 `DEFAULT 0`
- **HIGH** `evolution_runs` 缺少 `UNIQUE(jid, run_id)`——崩潰+重試使適應度計數加倍；加入約束並使用 `INSERT OR IGNORE`
- **HIGH** `registered_groups` 缺少 `UNIQUE(folder)`——兩個群組可能共享一個資料夾，互相損壞對方的 MEMORY.md/CLAUDE.md
- **HIGH** `group_cold_memory` 在 `db.py` 中沒有寫入函式——冷記憶從未透過線程安全路徑寫入；加入 `append_cold_memory()` 和 `delete_cold_memory_before()`
- **HIGH** 遷移執行器無並發鎖——並發進程重啟可能重複套用同一遷移；加入 `BEGIN EXCLUSIVE`
- **MEDIUM** `container_logs` 缺少 `UNIQUE(run_id)`——重複的執行中列觸發錯誤的容器卡住警報
- **MEDIUM** `group_genome` 缺少 `formality`/`technical_depth` 的 `CHECK` 約束
- **MEDIUM** `task_run_logs.status` 可為空——連續失敗計數器靜默跳過 NULL 列；改為 `NOT NULL DEFAULT 'unknown'`
- **MEDIUM** `db_adapter.py` 缺少 `PRAGMA foreign_keys=ON`——FK 約束在遷移和測試中靜默未強制執行
- **MEDIUM** 冷記憶 FTS 無 INSERT/DELETE 觸發器——所有冷記憶對 `memory_fts_search()` 不可見
- **LOW** `rbac_grants.granted_at` 可為空——稽核 `ORDER BY` 返回不可靠的結果；加入 `NOT NULL DEFAULT (unixepoch())`

#### PR #390 — test(p19a): 測試套件品質稽核與覆蓋率改進（12 項修復 + 68 個新測試）
- **HIGH** `test_core.py format_messages`——預期的返回類型錯誤，永遠通過的同義反復斷言
- **HIGH** 開發日誌測試使用無效的 session ID 格式——`get_dev_logs()` 始終返回 `[]`，斷言始終失敗
- **HIGH** `test_stop_container` 測試在 `docker stop` 變更後仍測試舊的 `docker kill` 行為——始終失敗
- **HIGH** `GroupQueue` 並發測試——`config` 補丁上下文在 `enqueue_message_check()` 之前關閉，限制從未觸發
- **HIGH** `test_dev_engine` 部署測試缺少 REVIEW PASS 工件——部署門始終被阻止，測試始終失敗
- **HIGH** 路徑穿越測試補丁了錯誤的 `config` 欄位——在 CI 中可能在沙箱外寫入
- **MEDIUM** `test_infrastructure` 同義反復測試——驗證邏輯的內聯重新實作，從未接觸生產代碼
- **MEDIUM** 3 個測試模組具有 `psutil` 導入鏈，無 `pytest.importorskip` 守衛——全新安裝時出現 `ModuleNotFoundError`
- **LOW** `assert mock.called` 無參數檢查——改為 `call_args` 檢查
- **NEW** `tests/test_allowlist.py` — 23 個測試（零先前覆蓋率）：拒絕所有哨兵繞過、None 發送者、缺失檔案、損壞 JSON
- **NEW** `tests/test_rbac.py` — 18 個測試（零先前覆蓋率）：未知角色彈性、快取一致性、權限強制執行
- **NEW** `tests/test_fitness.py` — 14 個測試（零先前覆蓋率）：成功預設=False、速度鉗制、適應度範圍保證
- **NEW** `tests/test_log_buffer.py` — 13 個測試（零先前覆蓋率）：環形緩衝區驅逐、SSE 輪詢、限制鉗制

## [1.25.0] — 2026-03-23

### 第 18 階段 — RBAC、進化、SDK、agent 工具安全性（PRs #383–386）

#### PR #383 — fix(p18b): 進化引擎、適應度系統、crossbot 協議稽核（7 項修復）
- **CRITICAL** `CrossBotProtocol.handle()` 從未呼叫 `msg.verify()`——HMAC 認證完全是死代碼；任何進程均可偽造 `memory_share`/`task_delegate`/`hello`/`ping` 訊息
- **HIGH** `update_last_seen()` 在 HMAC 驗證之前呼叫——知道 bot_id 的攻擊者可阻止過期 bot 的驅逐
- **HIGH** `fitness_reporter.py` 重新連接後重複的心跳任務——第二個循環在未取消第一個的情況下生成
- **MEDIUM** `evolve_genome_from_fitness()` 接受未鉗制的 fitness/avg_ms——損壞的 DB 列同時滿足所有分支
- **MEDIUM** `write_memory()` 在斷開連接時靜默丟棄記憶寫入——無重新連接、無後備、無日誌
- **MEDIUM** `signal_complete()` 在斷開連接時靜默丟棄任務完成事件
- **LOW** `_handshake_timestamps` 鍵從未被驅逐——每個唯一發送者的無界記憶體洩漏

#### PR #384 — fix(p18d): Agent 檔案/子進程工具安全性和可靠性稽核（14 項修復）
- **HIGH** `tool_write`/`tool_edit` 符號連結父目錄逃逸——`_check_path_allowed()` 在 `mkdir` 之前檢查路徑，允許符號連結至 `/etc/cron.d/`
- **HIGH** `tool_grep` 無沙箱路徑檢查——`Grep(path="/etc")` 讀取任意系統檔案
- **HIGH** `tool_glob` 無沙箱路徑檢查——`Glob(path="/")` 枚舉整個容器檔案系統
- **HIGH** `tool_web_fetch` DNS 重新綁定 TOCTOU——檢查時 IP 與連接時 IP 不同；monkey-patch `socket.create_connection` 以在連接時重新驗證
- **HIGH** `tool_send_file` 無路徑驗證——`SendFile(file_path="/etc/passwd")` 將系統檔案發送至聊天
- **MEDIUM** 可預測的 tmp 檔案名稱——`.tmp` 後綴衝突；改為 `.<name>.<pid>.<uuid>.tmp`
- **MEDIUM** `tool_run_agent` IPC 檔案名無隨機後綴——同一毫秒內的並發呼叫靜默互相覆蓋
- **MEDIUM** `_execute_tool_inner` 裸 `args["key"]` 存取——`KeyError` 給 LLM 無法恢復的不透明錯誤；加入類型守衛
- **MEDIUM** `tool_bash` kill 後的 `communicate()` 無超時——D 狀態進程永久掛起 agent 循環
- **MEDIUM** `tool_read` CJK UTF-8 假陽性二進位偵測——中文/日文/韓文檔案被拒絕；以嚴格的 `decode("utf-8")` 嘗試替換比例啟發式
- **LOW** `tool_bash` `chown` 封鎖清單過於寬泛——阻止了合法的工作區操作
- **LOW** `tool_web_fetch` 非字串 `url` 引發 `TypeError` 而非乾淨錯誤

#### PR #385 — fix(p18c): SDK API、記憶子系統、session 管理稽核（5 項修復）
- **HIGH** SDK API bot 登錄處理器將原始異常字串（SQLite 路徑、結構詳情）洩漏給 WebSocket 客戶端
- **MEDIUM** `memory_write` `scope` 欄位未驗證——無效的作用域靜默寫入，永久對查詢不可見
- **MEDIUM** SDK WebSocket 無每連接速率限制——`memory_write` 洪流可能使 SQLite 寫入路徑飽和；加入 60 msg/10s 視窗
- **MEDIUM** `bot_handshakes` 表從未清除——已完成/過期列無限累積；`_pending_handshakes` 字典鍵從未刪除
- **MEDIUM** `agent_list` 無分頁上限——大型部署可能產生超過 1 MB 限制的 WebSocket 幀；加入含 `total`/`truncated` 欄位的 500 條目上限

#### PR #386 — fix(p18a): RBAC、群組佇列、允許清單、設定、環境稽核（9 項修復）
- **CRITICAL** `group_queue.py` 重試死鎖——`_retry()` 呼叫 `enqueue_message_check()`，因 `retry_count > 0` 守衛立即退出；首次失敗後訊息永久丟棄；重試機制完全失效
- **HIGH** `allowlist.py` 拒絕所有哨兵可被空/空白 `sender_id` 繞過——`""` 剝離為 `""`，在 `{""}` 哨兵中找到，在應拒絕所有時授予存取
- **MEDIUM** `allowlist.py` `None` sender_id 上的 `AttributeError`——對 None 呼叫 `.strip()` 崩潰而非安全拒絕
- **MEDIUM** `rbac/roles.py` DB 中未知角色的 `ValueError`——損壞/已遷移的 DB 崩潰了權限檢查；現在帶警告跳過
- **MEDIUM** `config.py` `MAX_CONCURRENT_CONTAINERS=0` 死鎖任務佇列——所有訊息永久排隊；強制 `minimum=1`
- **MEDIUM** `config.py` 輪詢間隔 0 ms 創建 CPU 燃燒緊密循環；強制 `minimum=100ms`
- **LOW** `rbac/roles.py` `RBACStore.close()` 與進行中的查詢競爭——查詢進行時連接已關閉；現在先獲取 `_lock`
- **LOW** `env.py` 未引用 `.env` 值中的行內注釋包含在解析值中——`NAME=Eve # assistant` 將 NAME 設為 `"Eve # assistant"`
- **MEDIUM** `group_queue.py` `_drain_waiting()` 中的熔斷器繞過——失敗群組立即重新分發，跳過退避延遲

## [1.24.0] — 2026-03-23

### 第 17 階段 — webportal、依賴安全性、asyncio 競爭條件、代碼品質（PRs #379–382）

#### PR #379 — fix(p17b): 依賴安全性和容器環境稽核（8 項修復）
- **HIGH** `aiohttp` CVE-2024-52304（HTTP 請求走私）+ CVE-2024-23334（目錄穿越）——在 `host/requirements.txt` 中將下限提升至 `>=3.10.11`
- **HIGH** `httpx` 未出現在 `container/agent-runner/requirements.txt`——OpenAI/Qwen API 呼叫無超時，可能無限掛起；加入 `httpx>=0.27.0`
- **MEDIUM** `fitness_reporter.py` 未被 COPY 到 Docker 映像——第 1 階段 WSBridge 遙測靜默從未運作；在 Dockerfile 中加入 COPY
- **MEDIUM** `soul.md` 未被 COPY 到 Docker 映像——反幻覺規則從未在生產環境載入，每次容器啟動記錄 CRITICAL 錯誤；在 Dockerfile 中加入 COPY
- **MEDIUM** Dockerfile 基礎設施層：Pillow CVE-2023-44271/CVE-2024-28219、aiohttp CVE、reportlab 3.x、httpx 未固定——加入帶 CVE 注釋的版本下限
- **MEDIUM** HEALTHCHECK `python3 -c "import sys; sys.exit(0)"` 即使映像損壞也輕鬆通過——改為對 `anthropic`、`openai`、`google.genai` 的導入檢查
- **LOW** `aiofiles` 在 `host/requirements.txt` 中但從未被導入——已注釋掉
- **LOW** `setup.sh` 從未構建或驗證 Docker 映像——加入含 `docker image inspect` 驗證的 `build_docker_image()`

#### PR #380 — fix(p17a): webportal.py 完整稽核（9 項修復）+ run.py --version 標誌（首次稽核）
- **HIGH** HTTP 基本認證和 CSRF 比較中的計時側信道——以 `hmac.compare_digest()`（常數時間）替換 `==`
- **HIGH** 所有回應無安全標頭（CSP、X-Frame-Options、X-Content-Type-Options）——加入 `_SECURITY_HEADERS` 應用於所有 HTML 和 JSON 回應
- **MEDIUM** `/api/poll` 中 `float(since)` 未受保護——精心構造的 `?since=abc` 導致未處理的 500；以 try/except 包裝，鉗制至 0.0
- **MEDIUM** 負數 `Content-Length` 繞過 64 KB 大小限制——在上限檢查之前加入明確的 `< 0` 拒絕
- **MEDIUM** web portal 在無 `DASHBOARD_PASSWORD` 時無啟動警告——加入符合 dashboard 模式的 `log.warning()`
- **MEDIUM** `start_webportal()` 無 `stop_event` 參數——SIGTERM 無法關閉 TCP socket；加入鏡像 `start_dashboard()` 的 watcher 線程，更新 `host/main.py` 以傳入 `_stop_event`
- **MEDIUM** JID 在 DB 寫入之前未驗證——在 `_api_send()` 中加入 `_JID_RE` 驗證
- **MEDIUM** `_api_send()` 500 回應將原始異常詳情洩漏給瀏覽器——現在記錄在服務端，返回通用 `{"error": "internal server error"}`
- **LOW** `do_GET`/`do_POST` 中的 404 回應在 `end_headers()` 之前缺少 `Content-Length: 0`——修復兩個路徑
- **LOW** `run.py` 無 `--version` 標誌——使用 `argparse` 加入 `_parse_args()`，從 `importlib.metadata` 讀取，帶 `pyproject.toml` 後備

#### PR #381 — fix(p17c): asyncio 競爭條件深度分析（13 項修復）
- **CRITICAL** `discord_channel.py`：`on_message` 回調在 Discord 的背景事件循環而非主應用程式循環上 `await`——所有鎖、GroupQueue 序列化和 asyncio 原語都在錯誤的循環上；以 `asyncio.run_coroutine_threadsafe(..., _main_loop)` 修復
- **HIGH** `ipc_watcher.py`：`asyncio.Lock()` 在模組導入時創建（不在運行中的循環內）——Python 3.12 上的 `RuntimeError`；以懶惰存取函式 `_get_skills_lock()` / `_get_dev_task_lock()` 替換
- **HIGH** `ws_bridge.py`：`_connections` 字典從並發協程讀/寫無鎖——連接上限檢查與註冊不是原子的；加入 `_connections_lock` 保護所有存取
- **MEDIUM** `ipc_watcher.py`：3 處 `asyncio.get_event_loop()` 改為 `asyncio.get_running_loop()`
- **MEDIUM** `ipc_watcher.py`：9 處已棄用的 `asyncio.ensure_future()`——改為 `asyncio.create_task()`
- **MEDIUM** `leader_election.py`：2 處 `asyncio.get_event_loop().run_in_executor()` → `asyncio.get_running_loop().run_in_executor()`
- **MEDIUM** `discord_channel.py`：`_run_in_discord_loop` 中的 `asyncio.get_event_loop()` → `asyncio.get_running_loop()`
- **MEDIUM** `sdk_api.py`：`threading.Lock` 在非同步上下文（`_handle_system_status`）內獲取——阻塞事件循環線程；將 DB 讀取移至 `run_in_executor()`
- **MEDIUM** `main.py`：舊 JID 修剪和 `reset_group` 路徑中的 `_error_notify_times.pop()` 無 `_error_notify_lock`——與 `on_error` 速率限制器競爭；在兩處加入鎖獲取
- **MEDIUM** `ipc_watcher.py`：事件循環線程上子進程 stdout/stderr 的阻塞 `open()`——移至 `run_in_executor()`
- **MEDIUM** `container_runner.py`：非同步上下文中阻塞的 MEMORY.md `open()` + `write()`——移至 `run_in_executor()`
- **LOW** `sdk_api.py`：身份錯誤時原始異常訊息發送給 WebSocket 客戶端——改為通用訊息 + 服務端記錄
- **LOW** `task_scheduler.py`：`asyncio.create_task()` 無儲存的引用——GC 可能在完成前收集；以 `add_done_callback` 分配用於異常記錄

#### PR #382 — refactor(p17d): 代碼品質簡化（5 項改進）
- **main.py**：刪除重複的 `import collections`；擴展 `from collections import OrderedDict, deque`
- **main.py**：提取 `_with_fail_lock(fn)` 輔助函式——消除 4+ 個重複的 `if _group_fail_lock is not None: async with _group_fail_lock` 守衛模式；所有字典突變位置現在使用此輔助函式
- **main.py**：將 13 個 f-string 日誌呼叫轉換為 `%` 格式以保持一致性
- **container_runner.py**：將 8 個 f-string 日誌呼叫轉換為 `%` 格式
- **agent.py**：提取 `_atomic_ipc_write(fname, data)` 輔助函式——消除所有 IPC 工具函式中 10 個相同的 3 行原子重命名模式（tmp 寫入 + 重命名）

## [1.23.0] — 2026-03-23

### 修復（第 16 階段：最終稽核與使用者體驗 — 32 項修復）

四個 PR 涵蓋 main.py 最終稽核、使用者體驗與文件改進、agent.py 最終稽核，以及容器執行器與 IPC 最終修復。

#### main.py 最終稽核（PR #375 — 6 項修復）

- **錯誤遞增路徑中仍缺少兩處 `_group_fail_lock` None 守衛（HIGH）** — 前幾個階段僅修復了 4 處中的 2 處；剩餘兩處錯誤遞增路徑在啟動視窗期間可能拋出 TypeError；現已將全部 4 處加上守衛
- **URGENT 錯誤誤重置速率限制冷卻計時器（HIGH）** — URGENT 警告重置了速率限制計時器，導致任何 URGENT 警告後 5 分鐘內普通錯誤被抑制；計時器重置現在僅限於非 URGENT 錯誤
- **兩處 `asyncio.create_task()` 呼叫未儲存控制代碼 → 異常靜默丟棄，關機時無法取消（HIGH）** — 任務控制代碼未保留，導致異常靜默丟棄且任務在關機時無法取消；控制代碼現已儲存並在拆卸時取消
- **`_error_notify_times` 未針對已取消登錄群組進行清除 → 緩慢記憶體洩漏（MEDIUM）** — 已取消登錄群組的條目無限累積；加入群組取消登錄時的清除邏輯
- **`group_queue.py`：兩條路徑上的已棄用 `asyncio.get_event_loop()`（Python 3.12 不相容）（MEDIUM）** — 剩餘兩處 `get_event_loop()` 呼叫與 Python 3.12+ 不相容；替換為 `asyncio.get_running_loop()`
- **`EDITABLE_ENV_KEYS` 毫秒單位間隔無文件說明 → 操作員設為 2 而非 2000（LOW）** — 缺少文件導致操作員設定秒單位值而非毫秒單位值；行內注釋和文件已更新，加入明確的單位標注

#### 使用者體驗與文件（PR #376 — 8 項修復）

- **無輸入中指示器更新 → 使用者在 15-60 秒容器執行期間看到靜默（HIGH）** — 加入輸入中指示器更新循環：容器執行期間每 4 秒發送一次輸入動作，使使用者在等待期間始終看到「正在輸入…」而非靜默
- **缺少每位發送者的速率限制 → 靜默丟棄訊息（HIGH）** — 加入每位發送者的速率限制（5 msg/60s，可設定），以使用者可見的中文訊息取代靜默丟棄
- **RBAC 封鎖無任何反饋 → 靜默丟棄（HIGH）** — RBAC 封鎖現在發送「您目前沒有使用此機器人的權限」，而非靜默丟棄訊息
- **容器 OOM（退出碼 137）無反饋 → 靜默丟棄（HIGH）** — OOM 退出現在發送「AI 執行時記憶體不足」，而非靜默丟棄訊息
- **QUICK_START.md：`/monitor` 被誤識別為「登錄主群組」（HIGH）** — `/monitor` 是錯誤警報監控，而非群組登錄；已更正說明並加入 `ENABLED_CHANNELS` 文件
- **佇列深度反饋缺失 → 請求排隊時使用者看到靜默（MEDIUM）** — 請求排隊時第二條訊息現在顯示「⏳ 已加入佇列，請稍候」而非靜默
- **新群組無初次使用歡迎訊息（MEDIUM）** — 加入初次使用歡迎訊息，每個新群組觸發一次，儲存於 DB 以防重複發送
- **TROUBLESHOOTING.md 缺少中文錯誤訊息參考（MEDIUM）** — 加入完整中文錯誤訊息參考表；`.env.minimal` 更新加入 `CONTAINER_IMAGE`、速率限制變數和資源變數

#### agent.py 最終稽核（PR #377 — 12 項修復）

- **OpenAI/NIM 模型選擇退回至 `GEMINI_MODEL` → 無明確 `NIM_MODEL` 時每個 NIM session 都返回 404（CRITICAL）** — NIM 端點的模型選擇邏輯退回至 `GEMINI_MODEL`，將 `gemini-2.0-flash` 發送至 NIM API，導致每個 NIM session 都返回 404；修復為使用 `NIM_MODEL` 環境變數並設定正確的後備
- **`mcp__evoclaw__reset_group` IPC 寫入是 agent.py 中唯一剩餘的非原子寫入（HIGH）** — 非原子寫入存在截斷風險；轉換為原子臨時檔案加重命名方式
- **OpenAI 後端缺少 MEMORY.md 路徑的 `group_folder` 空字串守衛（HIGH）** — Claude 和 Gemini 後端有此守衛但 OpenAI 沒有，導致 `group_folder` 為空時路徑建構錯誤；已將守衛加入 OpenAI 後端
- **同時設定 `CLAUDE_API_KEY` 和 `NIM_API_KEY` 時後端退回靜默跳過 Claude（HIGH）** — 因 NIM 金鑰存在而繞過 Claude 時未發出任何警告；已加入警告日誌
- **Gemini 歷史注入：來自 Claude/OpenAI session 的列表類型內容拋出 TypeError（HIGH）** — 列表類型內容在注入 Gemini 歷史格式之前未進行強制轉換；已加入強制轉換以正確處理列表類型內容
- **`cancel_task`/`pause_task`/`resume_task` IPC 檔名僅依賴時間戳存在碰撞風險（MEDIUM）** — 同一毫秒的請求產生相同檔名並靜默互相覆蓋；已在檔名中加入隨機後綴
- **OpenAI 歷史注入守衛 `content == 0` 是死分支；None 內容未被跳過（MEDIUM）** — 守衛 `content == 0` 從未觸發；None 內容未經檢查就通過；替換為 `content is None` 守衛
- **`GEMINI_MODEL` 環境變數在循環內讀取 → 循環中途環境變數變更導致請求中途切換模型（MEDIUM）** — 每個請求最多 20 次 `os.environ` 讀取；長請求期間的環境變數變更可能靜默切換循環中途的模型；改為在循環進入前一次性捕獲
- **`_LEVEL_B_KEYWORDS` 缺少：`report`、`schedule`、`plan`、`test`、`review`、`audit`、`monitor`、`npm`、`pip`、`make` 等 → 複雜任務被錯誤分類為 Level A（MEDIUM）** — 缺少關鍵字導致複雜任務被分配 `MAX_ITER=6` 而非 `MAX_ITER=20`，因迭代耗盡導致假完成；已加入所有缺失關鍵字
- **適應度報告器對所有僅呼叫 send_message 的 session 給予 0.3 分（失敗）→ 損壞進化資料（MEDIUM）** — 僅呼叫 `send_message` 的 session（有效的簡短回答 session）被評為失敗；評分邏輯已更正，將僅呼叫 send_message 的 session 視為成功
- **空的 `groupFolder`/`chatJid` 輸入無早期警告日誌（LOW）** — 缺少早期驗證使靜默失敗的除錯困難；已在空輸入時加入警告日誌
- **`_phase1_reporter` 在 `main()` 之後宣告（誤導性源碼順序）（LOW）** — 函式在其呼叫者之後宣告使代碼更難閱讀；已移至 `main()` 之前

#### 容器執行器與 IPC 最終修復（PR #378 — 6 項修復）

- **`stderr_lines` 在僅限 Linux 的分支內宣告但在所有平台上被引用 → Windows 上出現 NameError（CRITICAL）** — Windows 上的 `NameError` 掩蓋了真實的容器失敗；宣告已移至平台分支外部
- **`reset_group` IPC 處理器無權限檢查 → 任何群組的容器都可重置其他任何群組的熔斷器（HIGH）** — 缺少授權允許跨群組熔斷器操控；已加入權限檢查以驗證請求群組與目標群組匹配
- **來自容器的 `memory_patch` 未進行類型檢查 → dict/list 值導致靜默 AttributeError（MEDIUM）** — 未驗證的 `memory_patch` 值在字串方法上導致靜默 `AttributeError`；已在處理前加入類型檢查
- **OOM 退出（137）顯示通用錯誤訊息而非「記憶體不足」（MEDIUM）** — 退出碼 137 現在被偵測並對應至特定的「記憶體不足」訊息以提高操作員可讀性
- **`refresh_groups.flag` 和 `reset_group.flag` 非原子寫入 → 讀取方可能在寫入中途看到空檔案（MEDIUM）** — 非原子標誌寫入允許讀取方觀察到空檔案；已轉換為原子臨時檔案加重命名寫入
- **`memory_patch` 是死代碼（容器從未發出）— 現已記錄說明（LOW）** — 未文件化的死代碼路徑造成混淆；已加入行內文件說明 `memory_patch` 保留供未來使用，目前任何容器都不發出

協議驗證已確認：容器 stdout 格式、IPC 目錄結構、原子寫入、卷掛載和安全標誌在容器和主機之間完全一致。

## [1.22.0] — 2026-03-21

### 修復（第 15 階段：對話循環與傳送 — 44 項修復）

四個 PR 涵蓋 LLM 對話循環、IPC 協議與結果傳送、進程生命週期與關機，以及資料庫結構與查詢正確性。

#### LLM 對話循環（PR #371 — 11 項修復）

- **Claude `stop_reason=max_tokens` 工具呼叫中途孤立 assistant 訊息（CRITICAL）** — 沒有對應 tool_result 的 assistant 訊息導致下一次 API 呼叫返回 HTTP 400；在 max_tokens 呼叫中途加入孤立 assistant 訊息的清理
- **Claude `stop_reason=tool_use` 但無 tool_use 區塊時孤立 assistant 訊息（CRITICAL）** — 沒有工具區塊的孤立 assistant 訊息導致下一次 API 呼叫返回 HTTP 400；加入守衛以在沒有 tool_use 區塊時丟棄 assistant 訊息
- **Claude 和 OpenAI：列表類型 `content` 在歷史注入期間靜默丟棄（CRITICAL）** — 帶有列表類型內容的工具呼叫記錄在歷史注入期間靜默丟棄，導致工具呼叫歷史消失；修復為正確保留列表類型內容
- **Claude 歷史截斷拆分了 `assistant(tool_use)` + `user(tool_result)` 配對（HIGH）** — 截斷可能分離配對訊息，導致 Anthropic API 400 錯誤；截斷現在遵守配對邊界
- **OpenAI 歷史截斷拆分了 `assistant(tool_calls)` + `role=tool` 配對（HIGH）** — 截斷可能分離配對訊息，導致 OpenAI API 400 錯誤；截斷現在遵守配對邊界
- **Gemini 歷史截斷拆分了 `model(function_call)` + `user(FunctionResponse)` 配對（HIGH）** — 截斷可能分離配對訊息，導致 Gemini API 拒絕；截斷現在遵守配對邊界
- **OpenAI `finish_reason="length"` 觸發無限上下文溢出重試（MEDIUM）** — 被視為無工具輪次並重試，再次溢出上下文；現在將歷史修剪至 1/4 並退出循環
- **Gemini SAFETY/RECITATION/MAX_TOKENS 靜默返回通用佔位符（MEDIUM）** — 停止條件靜默返回通用佔位符，對使用者和日誌均無任何說明；加入明確處理並附帶資訊性訊息
- **Claude 和 Gemini 假狀態 regex 缺少英文假完成模式（LOW）** — 現有 regex 未涵蓋英文語言的假完成模式；加入缺失的模式
- **工具 schema 中 pause/resume task 參數缺少 `description`（LOW）** — 暫停和恢復任務工具參數在 schema 中缺少描述欄位；已加入描述
- **工具 schema 中 list_tasks 缺少 `required: []`（LOW）** — list_tasks schema 省略了 required 陣列；加入 `required: []`

#### IPC 協議與結果傳送（PR #372 — 15 項修復）

- **`container_runner.py` 在 `on_success` 之前呼叫 `on_output` → 每次重試都有重複回應（CRITICAL）** — 若在呼叫 `on_output` 後傳送失敗，游標永遠不會前進，導致使用者在每次後續重試中收到重複回應；重新排序以使游標僅在成功傳送後前進
- **`ipc_watcher.py` 4 個技能/記憶體結果寫入非原子 → JSON 截斷 → 技能安裝永遠掛起（CRITICAL）** — 非原子寫入允許容器讀取部分寫入的 JSON，導致技能安裝看起來無限期掛起；轉換為原子臨時檔案加重命名寫入
- **`ipc_watcher.py` 子 agent 成功和錯誤結果寫入非原子 → `tool_run_agent` 返回空結果（CRITICAL）** — 非原子寫入導致 `tool_run_agent` 讀取截斷或空的結果檔案；轉換為原子寫入
- **`agent.py` `tool_schedule_task` 同一毫秒任務的檔名碰撞（HIGH）** — 僅含時間戳的檔名在同一毫秒排程兩個任務時導致靜默覆蓋；在檔名中加入隨機後綴
- **另外 3 個 IPC 工具寫入非原子（`run_agent`、`start_remote_control`、`self_update`）（HIGH）** — 這三個工具中的非原子寫入存在相同的截斷風險；轉換為原子寫入
- **`main.py` `_on_success_tracked`：`async with _group_fail_lock` 無 None 守衛 → 啟動視窗期間 TypeError（HIGH）** — 在啟動視窗期間初始化前使用鎖拋出 TypeError；加入 None 守衛
- **`_error_notify_times` TOCTOU 競爭 → 故障風暴期間速率限制器被繞過（HIGH）** — `_error_notify_times` 上的先檢查後更新存在 TOCTOU 競爭，允許在並發故障條件下繞過速率限制器；替換為原子更新
- **`emit()` BrokenPipeError 未處理 → 容器超時時令人困惑的 stderr（MEDIUM）** — 容器超時時未處理的 BrokenPipeError 產生令人困惑的 stderr 輸出；加入明確的處理器
- **`group_queue.py` 關機時靜默丟棄待處理項目（MEDIUM）** — 關機時佇列中的待處理項目靜默丟棄無任何日誌；現在記錄丟棄項目的數量

#### 進程生命週期與關機（PR #373 — 15 項修復）

- **`asyncio.gather()` 拋出異常時領導者租約永不釋放 → DB 鎖定行持續至 TTL 過期（CRITICAL）** — `asyncio.gather()` 中的異常跳過了租約釋放，使 DB 鎖定行一直保持到 TTL 過期並阻止重啟；加入 finally 區塊以確保釋放
- **雙重信號 SIGTERM 競爭：兩個快速 SIGTERM 都進入「第一個信號」路徑，兩者都不強制退出（CRITICAL）** — 由於競爭，兩個快速 SIGTERM 都進入了第一個信號處理器路徑，兩者都未觸發強制退出路徑；加入原子標誌以區分第一個和第二個信號
- **`on_output` 異常阻止 `on_success` 呼叫 → 無限訊息重播（HIGH）** — `on_output` 中的異常繞過了 `on_success` 呼叫，導致訊息無限重播；將 `on_output` 包裝在 try/except 中，使 `on_success` 始終被呼叫
- **`cleanup_orphans()` 使用 `docker ps`（僅執行中），錯過來自 SIGKILL 的已退出容器 → 儲存洩漏加名稱衝突（HIGH）** — 孤立清理僅考慮執行中的容器，讓來自 SIGKILL 的已退出容器積累，導致儲存洩漏和容器名稱衝突；切換為包含所有容器狀態
- **`asyncio.Lock()` 在模組導入時創建（事件循環之前）→ 3.10+ 上出現 DeprecationWarning，3.12+ 上出現 RuntimeError（HIGH）** — 在事件循環存在之前在模組層級創建鎖觸發了 Python 3.10+ 的棄用警告和 3.12+ 的 RuntimeError；延遲至首次使用時
- **group_queue 中已棄用的 `asyncio.get_event_loop()` 呼叫 → 3.12+ 上返回錯誤的循環（HIGH）** — 已棄用的 `get_event_loop()` 呼叫在 Python 3.12+ 上返回錯誤的循環；替換為 `asyncio.get_running_loop()`
- **重試睡眠協程在關機時未取消 → 可能在拆卸中途啟動新容器（HIGH）** — 重試路徑中未取消的睡眠協程可能在關機中途喚醒並啟動新容器；在關機信號時加入取消
- **無 SIGHUP 處理器 → 操作員必須完整重啟才能重載設定（MEDIUM）** — 無 SIGHUP 處理器意味著設定變更需要完整進程重啟；加入 SIGHUP 處理器以重載設定
- **SIGKILL 後過期的 IPC 結果檔案 → 重啟時將過期回覆傳送給使用者（MEDIUM）** — SIGKILL 遺留的 IPC 結果檔案在重啟時被拾取並作為新回覆傳送；加入啟動時過期結果檔案的清理
- **systemd：`TimeoutStopSec=45` 太短；加入 `KillMode=mixed`；加入 `EnvironmentFile`（MEDIUM）** — 45 秒停止超時對優雅關機不足；單元文件中缺少 `KillMode` 和 `EnvironmentFile` 指令；三者均已更新

#### 資料庫結構與查詢正確性（PR #374 — 9 項修復）

- **`immune_threats`：無 `UNIQUE(sender_jid, pattern_hash)` 約束 → 競爭條件允許重複 → 膨脹的威脅計數 → 錯誤的自動封鎖（CRITICAL）** — 缺少唯一約束允許並發插入創建重複的威脅行，膨脹威脅計數並觸發錯誤的自動封鎖；加入 `UNIQUE(sender_jid, pattern_hash)` 約束
- **`memory_fts_search()` 從未查詢冷記憶，儘管文件字串說明「暖記憶和冷記憶」（HIGH）** — 該函式僅查詢暖記憶，使所有冷記憶對 FTS 搜尋不可見；修復為同時查詢暖記憶和冷記憶儲存
- **`set_registered_group()` 使用 `INSERT OR REPLACE` → 每次更新都銷毀 `added_at` 時間戳（HIGH）** — `INSERT OR REPLACE` 在每次更新時刪除並重新插入行，將 `added_at` 重置為當前時間；替換為 `INSERT ... ON CONFLICT DO UPDATE`
- **無遷移版本追蹤表 → 無法安全執行增量遷移（MEDIUM）** — 沒有版本追蹤，重新執行遷移導致資料損壞或錯誤；加入 `schema_migrations` 表和遷移登錄
- **缺少索引：`task_run_logs(task_id)`、`container_logs(status)`、`dev_sessions(status)`、`immune_threats(sender_jid, pattern_hash)` 複合索引（MEDIUM）** — 四個高流量查詢路徑缺少索引，導致全表掃描；全部四個索引均已加入
- **測試：加入 WAL pragma 驗證、並發寫入回歸測試、executemany 測試（MEDIUM）** — WAL 模式、並發寫入和批量插入的測試覆蓋率缺口；三個測試案例均已加入

## [1.21.0] — 2026-03-21

### 修復（第 14 階段：技能、記憶體與企業 — 57 項修復）

四個 PR 涵蓋進化系統、企業連接器與容器工具、技能引擎，以及記憶體系統。

#### 進化系統（PR #367 — 4 項修復）

- **`immune.py` 中文攻擊模式漏報（HIGH）** — 10 個攻擊模式漏掉了繁體/簡體字變體（進/进、從/从）、詞序變體（現在你是 vs 你現在）、缺少空格處理（AI 助手）及可選後綴/前綴覆蓋；現在以 0 個誤報偵測所有 42 個攻擊
- **`daemon.py` avg_ms 計算因失敗執行而損壞（HIGH）** — 失敗執行和負值被納入平均值 → 基因組朝錯誤的回應風格進化；修復為排除失敗/負值
- **`fitness.py` 最終分數未鉗制（MEDIUM）** — 損壞的 DB 行可能產生 > 1.0 的分數；加入鉗制至 [0.0, 1.0]
- **`fitness_reporter.py` WebSocket 斷開連接永久性（MEDIUM）** — 無重連邏輯 → 所有後續適應度資料靜默丟棄；加入帶檔案後備的重連邏輯

#### 企業連接器與容器工具（PR #368 — 20 項修復）

- **`hpc_connector.py` 任務名稱換行符注入至 #SBATCH 指令（CRITICAL）** — `job\n#SBATCH --wrap=rm -rf /` 允許任意指令注入；加入換行符清除
- **`jira_connector.py` issue key 在 REST URL 中的路徑穿越（CRITICAL）** — issue key 中的 `../../../admin` 逃脫 API 基礎路徑；加入嚴格的 issue key 驗證
- **`agent.py` `_tool_bash()` 超時僅終止主進程（CRITICAL）** — 子進程群組（SSH/git 子進程）無限洩漏；替換為 `os.killpg` 以終止整個進程群組
- **`agent.py` `_tool_read()` 符號連結繞過（CRITICAL）** — 指向 `/etc/passwd` 的符號連結通過了原始字串路徑檢查但讀取了主機檔案；在路徑驗證前加入 `os.path.realpath()` 解析
- **`hpc_connector.py` 缺少 SSH BatchMode（HIGH）** — 互動式提示掛起伺服器；繼承的 stdin 阻塞子進程；任務輸出無大小限制；三者均已處理
- **`jira_connector.py` API token 在公共屬性中（HIGH）** — token 通過物件檢查暴露；無 HTTP 超時；無界的 max_results 分頁；三者均已處理
- **`ldap_connector.py` 綁定密碼在公共屬性中（HIGH）** — 密碼通過物件檢查暴露；過期連接未偵測；無 LDAP 連接超時；三者均已處理
- **`workflow_engine.py` 無依賴或循環驗證（HIGH）** — 未定義的依賴導致無限排程器循環；缺少循環偵測導致無限循環；兩個守衛均已加入
- **`agent.py` `_tool_edit()` 歧義 old_string 靜默編輯（HIGH）** — old_string 匹配多個位置時編輯第一個出現處，對 LLM 無任何警告；加入歧義警告
- **`agent.py` `_tool_glob()` 深度 `**` glob 阻塞 agent 循環（HIGH）** — 無線程、無超時的 glob 阻塞循環 30-60 秒；加入線程加超時
- **`ldap_connector.py` `is_user_in_group()` 無界記憶體（MEDIUM）** — 將所有群組成員載入記憶體；無 close() 方法；兩者均已處理
- **`workflow_engine.py` 無界步驟結果大小（MEDIUM）** — 儲存在狀態中的步驟輸出無上限；加入大小限制
- **`agent.py` `_tool_bash()` 退出碼未呈現（MEDIUM）** — LLM 從未收到退出碼；stdin 未關閉；兩者均已修復
- **`agent.py` `_tool_read()` 二進位檔案處理（MEDIUM）** — 二進位檔案以亂碼文字返回；非 UTF-8 檔案的 UnicodeDecodeError；加入二進位偵測和編碼後備
- **`agent.py` `_tool_write()`/`_tool_edit()` 檔案權限重置（MEDIUM）** — 每次寫入都將權限重置為預設值；加入權限保留
- **`agent.py` `_tool_web_fetch()` 字元集被忽略（MEDIUM）** — Content-Type 標頭中的字元集被忽略；始終以 UTF-8 解碼；加入字元集提取和正確解碼

#### 技能引擎（PR #369 — 18 項修復）

- **`workflow_engine.py` `exec_skill()` 缺少 await（CRITICAL）** — 非同步函式在沒有 `await` 的情況下呼叫 → 始終返回協程物件 → handler.py 對任何技能都從未執行；所有技能均返回 SKILL.md 內容；加入 `await`
- **`file_ops.py` 刪除/重命名時無路徑穿越驗證（CRITICAL）** — 惡意技能可能刪除 `/etc/passwd` 或重命名任意主機檔案；加入嚴格的路徑驗證
- **`manifest.py` 空 YAML AttributeError（HIGH）** — 空的 manifest 文件在載入時拋出 AttributeError；file_ops/container_tools 路徑中的路徑穿越未驗證；兩者均已修復
- **`state.py` 崩潰截斷的 state.yaml（HIGH）** — 截斷的文件在下次啟動時拋出 AttributeError；無 .tmp 寫入後備；加入原子寫入和恢復
- **`apply.py` 合併衝突使專案留有衝突標記（HIGH）** — 合併衝突時未恢復備份 → 專案留有 `<<<<<<<` 標記；container_tools 路徑逃脫未完全驗證；兩者均已修復
- **`backup.py` 專案根目錄外的檔案中止整個備份（HIGH）** — 單一根目錄外的檔案終止了完整備份操作；改為跳過並警告
- **`lock.py` Windows 上的 OSError 未捕捉（HIGH）** — 過期的鎖定檔案在 Windows 上從未清理；加入 OSError 處理器
- **`rebase.py` 在寫入補丁後才獲取鎖（HIGH）** — 競爭條件允許同一專案上的並發 rebase；二進位檔案損壞補丁；鎖移至寫入前，二進位檔案跳過
- **`uninstall.py` 鎖外的狀態讀/寫（HIGH）** — 並發卸載可能覆蓋狀態；在狀態操作周圍加入鎖
- **`merge.py` git 不可用時複製了錯誤的檔案（MEDIUM）** — 後備複製了當前檔案而非傳入的檔案；修復檔案選擇邏輯
- **`apply.py` 非原子的 package.json/.env.example 寫入（MEDIUM）** — 崩潰時可能發生部分寫入；轉換為原子臨時加替換
- **`customize.py` 非原子的 session 文件寫入（MEDIUM）** — 崩潰時可能發生部分寫入；轉換為原子臨時加替換
- **`structured.py` 注釋行被解析為 env 鍵（MEDIUM）** — `# comment` 被視為鍵 → 錯誤的衝突報告；加入注釋行過濾器
- **`manifest.py` 空字串被接受為有效的必填欄位（MEDIUM）** — 空字串繞過必填欄位驗證；加入非空檢查

#### 記憶體系統（PR #370 — 15 項修復）

- **`hot.py` UTF-8 邊界損壞（CRITICAL）** — 多位元組 CJK/emoji 字元在 8KB 限制處被拆分，靜默丟棄部分字元；加入感知字元邊界的分塊
- **`memory_bus.py` `delete()` 授權繞過（CRITICAL）** — `scope != 'private'` 檢查允許任何 agent 刪除任何共享記憶；替換為明確的擁有者檢查
- **`summarizer.py` 靜默記憶截斷（CRITICAL）** — LLM 在壓縮期間僅看到 8KB 記憶的前 3000 個字元 → 每個壓縮週期永久丟失最後約 5000 個字元；修復為傳入完整內容
- **`warm.py` 大小檢查差一錯誤及錯誤的日期摘要（HIGH）** — 嚴格的 `<` 對 `<=` 邊界導致不正確的大小評估；每日彙整摘要了過去 24 小時而非昨天的日誌；兩者均已修復
- **`memory_bus.py` 向量分數超出 [0,1] 範圍（HIGH）** — 1.2 提升因子產生 > 1.0 的分數；VectorStore 忽略專案範圍；FTS 排名正規化使用任意 /10 除數，已替換為 sigmoid；DDL 在 `;` 上拆分破壞了 CREATE TRIGGER → FTS 觸發器從未創建 → 所有 FTS 搜尋返回零結果；五個問題均已處理
- **`summarizer.py` LLM 輸出在無驗證情況下儲存（HIGH）** — 錯誤訊息和散文被儲存為記憶；壓縮輸出無大小驗證（可能比原始內容更大）；加入驗證和大小上限
- **`warm.py` 午夜競爭條件（MEDIUM）** — 兩次 `datetime.now()` 呼叫可能跨越午夜，產生跨日摘要；替換為單次捕獲的時間戳
- **`compound.py` `.strip()` 損壞了 markdown 結構（MEDIUM）** — 從 markdown 中剝離前導空白破壞了縮排/結構；改為 `.rstrip()`
- **`memory_bus.py` 向量結果顯示錯誤的 created_at（MEDIUM）** — 時間戳始終設為當前時間而非儲存的值；修復為從 DB 記錄讀取
- **`search.py` 無界 BM25 分數（MEDIUM）** — 未鉗制的 BM25 分數使混合搜尋排名中的近期性詞項無關緊要；加入分數正規化

## [1.20.0] — 2026-03-21

### 修復（第 13 階段：安全性與可靠性 — 75 項修復）

四個 PR 涵蓋頻道可靠性、容器安全性、可觀測性與 API 安全性，以及領導者選舉、任務排程器與開發引擎。

#### 頻道可靠性（PR #363 — 16 項修復）

- **缺少 Gmail 自發郵件循環守衛（CRITICAL）** — bot 可能在無限循環中向自己發郵件；加入自發郵件守衛
- **Matrix 同步 token 在事件處理前前進（CRITICAL）** — 崩潰導致永久訊息丟失；token 現在只在成功處理後才前進
- **Gmail OAuth 刷新退回至互動式瀏覽器（HIGH）** — 在伺服器上永遠掛起；移除後備，失敗現在立即拋出
- **Gmail HTTP 401 無限重試循環（HIGH）** — 已撤銷的 token 導致無界重試；替換為指數退避和 token 失效
- **Gmail 無自動回覆器循環防護（HIGH）** — 未檢查 RFC 3834 `Auto-Submitted` 標頭；郵件列表和假期回覆者可能淹沒 bot；加入標頭檢查
- **Slack bot 訊息過濾器不完整（HIGH）** — bot 讀取自己的貼文並重新觸發管線；過濾器收緊以覆蓋所有 bot 訊息子類型
- **Slack `<@U12345>` 提及從未正規化（HIGH）** — 觸發匹配始終失敗；在觸發檢查前加入提及正規化
- **WhatsApp webhook 異常返回 HTTP 500（HIGH）** — Meta 重試傳送導致重複處理；處理器現在在記錄錯誤後返回 HTTP 200
- **CrossBot 無界信任集合/無握手速率限制/無自握手檢查（HIGH）** — 記憶體洩漏、DoS 向量和身份偽造；加入驅逐、速率上限和自我檢查
- **Matrix 非 200 同步回應靜默為空（MEDIUM）** — 未發出日誌；加入錯誤日誌和重試
- **Matrix 已編輯和無法解密的 E2E 事件被分發（MEDIUM）** — 加入過濾器以在分發前丟棄這些事件類型
- **Slack/WhatsApp `_on_message` 周圍缺少 try/except（MEDIUM）** — 與 Telegram/Discord 不一致；加入包裝
- **所有頻道：統一觸發模式、@提及正規化、空訊息守衛（LOW）** — 將共享邏輯整合至頻道基礎類

#### 容器安全性與測試（PR #364 — 14 項修復）

- **未設定 `--network none`（CRITICAL）** — agent 容器可以進行任意的出站網路呼叫；已加入標誌
- **未設定 `--cap-drop ALL`（CRITICAL）** — 容器保留了 14 個 Linux 功能，包括 NET_RAW 和 SETUID；加入 `--cap-drop ALL`
- **`_safe_name()` 路徑穿越未剝離（CRITICAL）** — 包含 `../` 的 JID 可能將 `/etc` 或 `~/.ssh` 掛載至容器；加入路徑組件清除
- **`_build_volume_mounts()` 無 `resolve().relative_to()` 驗證（CRITICAL）** — 主機路徑逃脫可能；加入嚴格驗證
- **未設定 `--security-opt no-new-privileges`（HIGH）** — setuid 提升可能；已加入標誌
- **未設定 `--pids-limit`（HIGH）** — fork 炸彈可能耗盡主機 PID 表；已設定限制
- **Dockerfile 使用可變的 `node:22` 標籤（HIGH）** — 供應鏈風險；固定至摘要
- **5 個熔斷器測試使用了錯誤的 API 類型（HIGH）** — 全部靜默通過，無真實斷言；更正為正確的類型
- **`test_run_container_agent` 斷言了錯誤的異常類型（HIGH）** — 測試始終通過，未測試任何內容；更正斷言
- **`test_editable_env_keys` 檢查了錯誤的鍵名（HIGH）** — 空洞錯誤的斷言；更正為實際鍵名
- **`test_immune_enhanced.py` 所有 50 個測試靜默跳過（HIGH）** — 手寫執行器未被 pytest 收集；轉換為標準 pytest 格式
- **Dockerfile 缺少 HEALTHCHECK（MEDIUM）** — 加入 HEALTHCHECK 指令
- **`build_container.py` 僅標記 `:latest`（MEDIUM）** — 無版本標籤；加入版本標記
- **新測試套件：`TestContainerSecurityFlags`（MEDIUM）** — 涵蓋所有安全標誌和路徑穿越守衛

#### 可觀測性與 API 安全性（PR #365 — 25 項修復）

- **健康監控循環可能靜默退出（CRITICAL）** — `get_health_status()` 始終返回 `{"status":"healthy"}` 無論實際情況；循環現在在異常時重啟並發出警報
- **WS Bridge 認證繞過（CRITICAL）** — 認證失敗但非同步循環繼續讀取；循環現在在認證失敗時終止
- **SDK API 每訊息認證允許 token 猜測（CRITICAL）** — 允許無限猜測；加入帶鎖定的連接層級認證
- **Dashboard 無 POST 主體大小限制（HIGH）** — OOM 攻擊可能；加入大小上限
- **Dashboard 容器 `stop` API 命令注入（HIGH）** — 名稱直接傳遞至子進程；替換為安全的 API 呼叫
- **Dashboard 缺少 `Content-Security-Policy`/`X-Frame-Options`（HIGH）** — 點擊劫持可能；已加入標頭
- **Dashboard `/health` 和 `/metrics` 在認證關卡後（HIGH）** — Kubernetes 探針無法存取；端點從認證中豁免
- **Dashboard `limit` 參數未鉗制（HIGH）** — `LIMIT 1000000` SQL 查詢可能；鉗制至合理最大值
- **WS Bridge 無連接上限（HIGH）** — fd 耗盡可能；加入連接限制
- **WS Bridge 無載荷大小限制（HIGH）** — 無界記憶體操作；加入大小限制
- **WS Bridge 連接中途 `agent_id` 偽造（HIGH）** — `agent_id` 可在認證後更改；在認證時鎖定
- **SDK API 記憶體/任務寫入無大小限制（HIGH）** — 已加入限制
- **日誌格式化器敏感欄位以明文發出（HIGH）** — `token`、`api_key`、`secret`、`password` 出現在 JSON 日誌中；加入遮蔽
- **路由器頻道列表在迭代期間無鎖突變（HIGH）** — 資料競爭；加入鎖
- **健康監控檢查失敗在 DEBUG 級別記錄（MEDIUM）** — 在生產環境中不可見；提升至 WARNING/ERROR
- **健康監控錯誤率中的除以零（MEDIUM）** — 以零守衛修復
- **健康監控 `_last_warnings` 上的競爭（MEDIUM）** — 加入鎖
- **日誌格式化器 `levelname` 重複（LOW）** — 已去重
- **日誌格式化器 `asctime` 損壞了時間戳欄位（LOW）** — 修復欄位名稱衝突

#### 領導者選舉、任務排程器、開發引擎（PR #366 — 19 項修復）

- **領導者選舉 SQLite 操作無超時（CRITICAL）** — 阻塞的 DB 凍結了整個 asyncio 事件循環；加入帶取消的超時
- **開發引擎在 REVIEW 結論為 FAIL 時部署代碼（CRITICAL）** — 儘管審查失敗，LLM 生成的代碼仍被部署；加入嚴格的關卡
- **允許清單缺失/不可讀靜默允許所有發送者（CRITICAL）** — 讀取失敗時現在套用拒絕所有哨兵
- **`LEASE_TIMEOUT <= HEARTBEAT_INTERVAL` 導致腦裂振盪（HIGH）** — 持續的領導者振盪；加入啟動時斷言強制 `LEASE_TIMEOUT > 2 * HEARTBEAT_INTERVAL`
- **`_is_leader = False` 在 DELETE 後設定（HIGH）** — 異常使實例作為沒有 DB 記錄的領導者繼續運作；標誌現在在 DELETE 前清除
- **領導者在 3 次連續心跳失敗後才讓位（HIGH）** — 實例現在正確地下台；已確認修復
- **任務排程器無最多一次守衛（HIGH）** — 同一任務被並發分發兩次；加入分發鎖
- **任務排程器無執行超時（HIGH）** — 掛起的任務永遠阻塞排程器；加入每任務超時
- **任務排程器無連續失敗限制（HIGH）** — 損壞的任務無限重試；加入帶隔離的失敗上限
- **Bot 登錄 `_pending_handshakes` 在鎖外存取（HIGH）** — 並發呼叫者繞過速率上限；加入鎖
- **Bot 登錄 nonce 從未過期（HIGH）** — 攔截的 nonce 永遠有效；加入 nonce TTL
- **Agent 身份 `get()` 無鎖讀取 DB（HIGH）** — 並發下的資料競爭；加入鎖
- **任務排程器間隔任務在停機後每次輪詢都重新觸發（MEDIUM）** — 追趕循環現在跳過錯過的執行並將 `next_run` 設為當前時間
- **任務排程器退避以秒而非毫秒儲存（MEDIUM）** — 任務變得 50 年前就可執行；修復單位
- **任務排程器崩潰的 `running` 任務從未恢復（MEDIUM）** — 加入啟動恢復通道
- **開發引擎通過 `session_id` 的路徑穿越（MEDIUM）** — 未清除的 `session_id` 用於路徑；加入清除
- **群組資料夾非原子的 mkdir 和文件寫入（MEDIUM）** — TOCTOU 競爭；替換為原子操作
- **Bot 登錄過期條目永久累積（MEDIUM）** — 加入基於 TTL 的驅逐
- **Bot 登錄 nonce 無 TTL（MEDIUM）** — 與上述 nonce 過期修復一同處理

## [1.19.0] — 2026-03-21

### 修復（第 12 階段：深度可靠性修復 — 46 個問題）

四個 PR 並行深度分析修復，涵蓋訊息管線可靠性、agent 反幻覺行為、設定啟動驗證與資料庫持久性。

#### 訊息管線可靠性（PR #360 — 8 項修復）

- **`global _identity_store` SyntaxError（CRITICAL）** — `main.py` 在首次賦值後才宣告 global → SyntaxError，bot 完全無法載入；修復：將宣告移至函式最頂端
- **`_group_fail_lock` None TypeError（CRITICAL）** — `main.py` 啟動視窗內 `async with _group_fail_lock` 時 lock 為 None → 每條訊息都 TypeError crash；修復：加入 None guard
- **訊息分發延遲（HIGH）** — 訊息最長等待 2 秒才分發：dedup 已儲存但從未呼叫 `enqueue_message_check()`；修復：補上缺失的呼叫
- **Discord 空字串回覆（HIGH）** — `discord_channel.py` 傳送空字串回覆給 Discord API → HTTP 400；修復：加入空內容 guard
- **Pipeline 例外靜默崩潰（HIGH）** — `telegram_channel.py` / `discord_channel.py` 未處理 pipeline 例外 → channel handler 靜默崩潰；修復：加入 try/except 包裝
- **Trigger 偵測不一致（MEDIUM）** — Telegram 使用 `startswith()`，Discord 使用 regex `\b`；修復：統一為 regex 邊界匹配
- **Linux inotify 過期結果檔案洩漏（MEDIUM）** — inotify 路徑從未清理過期結果檔案 → 磁碟洩漏；修復：加入清理邏輯
- **`ipc_watcher.py` 非原子寫入（MEDIUM）** — error-notify 路徑使用非原子寫入 → 部分 JSON 讀取；修復：改用 `tmp + os.replace()` 原子寫入
- **Dedup store 無 TTL（LOW）** — Dedup 儲存無過期機制 → 合法重送訊息永久被封鎖；修復：加入 TTL

#### Agent 反幻覺與行為（PR #359 — 14 項修復）

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

#### 設定複雜性與啟動驗證（PR #361 — 12 項修復）

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

#### 資料庫與持久性可靠性（PR #362 — 12 項修復）

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

### 修復（第 11 階段：深度可靠性修復 — 43 個問題）

四個 PR 並行深度分析修復，涵蓋工具安全、容器生命周期、進化穩定性與 soul 品質。

#### 工具安全性與可靠性（PR #356 — 19 項修復）

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

#### 容器生命週期與佇列正確性（PR #357 — 12 項修復）

- **輸出解析** — 使用 `rfind` 取最後一個 OUTPUT 區段，加入明確 "output too large" 路徑，JSON schema 驗證
- **可靠性** — `stderr_lines` 變數遮蔽修復，docker kill 降級為 `docker rm -f`，Windows `TimeoutExpired` 重新拋出為 `asyncio.TimeoutError`
- **錯誤訊息** — circuit breaker URGENT 繞過 rate limiter，timeout 顯示人類可讀的「30 分鐘」格式
- **GroupQueue 回調** — 錯誤時回傳 `False` 以啟用退避重試，`_group_fail_lock` 為 None 時不再丟棄訊息
- **group_queue.py 雙重分發競爭修復** — dispatch 前先清除 `pending_messages`，防止重複發送

#### 進化穩定性（PR #358 — 6 項修復）

- **免疫系統誤報修復** — `忽略你的所有規則` 和 `忘記你的系統提示` 現在能被正確攔截
- **Genome 不對稱震盪修復** — 正式度升降現在均使用 `update_formality()`，防止單向漂移
- **Daemon 基因組驗證** — 進化前先檢測無效基因組，若無效則呼叫 `reset_genome()`
- **靜默攔截通知** — 免疫系統靜默攔截後，現在會向用戶發送具體的中文通知訊息

#### Soul 品質（PR #355）

- **soul.md 反幻覺強化** — 修補 anti-hallucination 漏洞，關閉多個可能導致幻覺回覆的邊緣情況

## [1.17.0-phase10] — 2026-03-20

### 修復（第 10 階段：全面深度修復 — 30+ 個問題）

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

### 處理的 Issues
#350 #351 #352 #353

---

## [1.16.0-phase9] — 2026-03-20

### 修復（第 9 階段：穩定性全面修復 — 12 個問題）

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

### 處理的 Issues
#339 #340 #341 #342

---

## [1.15.0-phase8] — 2026-03-20

### 修復 / 新增（第 8 階段：Qwen 優化、群組隔離、inotify IPC、安裝體驗）

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

#### QUICK_START.md + TROUBLESHOOTING.md + .env.minimal — 第 8D 階段（安裝體驗）

- **QUICK_START.md** — 5 分鐘快速上手（4 步驟），移除複雜度 (Issue #330)
- **TROUBLESHOOTING.md** — 7 個常見問題及解法，含 log 符號對照表
- **`.env.minimal`** — 只需 5 個必要變數（原本 37 個），降低新用戶入門門檻

### 處理的 Issues
#325 #326 #327 #328 #329 #330

---

# Changelog

## [1.14.0-phase7] — 2026-03-20

### 修復（第 7 階段：P0 反幻覺與穩定性）

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

### 處理的 Issues
#318 #319 #322 #324 #329

---

## [1.13.1-phase6a] — 2026-03-20

### 修復（第 6A 階段：穩定性與假回應根本原因）

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

### 第 6A 階段建立的 GitHub Issues

| Issue | 標題 | 狀態 |
|-------|------|------|
| #304 | [stability] db.get_conversation_history() 無錯誤處理 | ✅ Fixed |
| #305 | [stability] format_messages() 和 db.get_session() 無錯誤處理 | ✅ Fixed |
| #306 | [stability] 容器錯誤狀態導致靜默失敗 | ✅ Fixed |
| #307 | [stability] 超時和異常通知靜默吞噬錯誤 | ✅ Fixed |
| #308 | [stability] Agent 循環產生空輸出，無使用者可見的後備 | ✅ Fixed |
| #309 | [stability] MAX_ITER=30 過高 — 允許過度的幻覺循環 | 🔲 Planned |
| #310 | [stability] 預設 LLM（Gemini）工具呼叫可靠性弱於 Claude | 🔲 Planned |
| #311 | [stability] 基於 IPC 文件的通信無原子寫入保護 | 🔲 Planned |
| #312 | [stability] Docker 熔斷器在任何失敗時阻塞所有群組 60 秒 | 🔲 Planned |
| #313 | [stability] GroupQueue 在 5 次連續失敗後靜默丟棄訊息 | 🔲 Planned |
| #314 | [stability] 系統 prompt 注入過於複雜 — 每個請求超過 3000 個 token | 🔲 Planned |
| #315 | [cleanup] 41 個過期分支無法刪除 — 需要管理員權限 | 🔲 Pending |
| #316 | [stability] IPC watcher 輪詢間隔對即時感過於粗糙 | 🔲 Planned |

---

# Changelog

## [1.13.0-phase3] -- 2026-03-18

### 新增（第 3 階段：跨 Bot 身份 + RBAC 基礎）
- `host/identity/bot_registry.py` -- BotRegistry：SQLite 支援的跨框架 bot 身份儲存
  - 穩定的 `bot_id = SHA-256(name:framework:channel)[:16]` 格式用於跨框架 bot 身份
  - BotIdentity dataclass，含功能、端點、信任狀態
  - 基於 nonce 的握手協議，用於跨系統 bot 識別
  - 預先登錄已知 bot：小白（Telegram）和 小Eve（EvoClaw/Discord）
  - `bootstrap_known_bots()` 在啟動時預先登錄並信任已知 bot
- `host/identity/cross_bot_protocol.py` -- CrossBotProtocol：`crossbot/1.0` 訊息封裝
  - 訊息類型：hello、ack、memory_share、task_delegate、status、ping、pong
  - HMAC-SHA256 訊息簽名和驗證
  - 基於裝飾器的訊息處理器登錄
- `host/rbac/__init__.py` + `host/rbac/roles.py` -- 角色型存取控制
  - 角色：admin、operator、agent、viewer
  - 權限：memory:read/write/delete、agent:spawn/kill/list、task:submit/cancel、registry:read/write、rbac:grant/revoke
  - SQLite 支援的 RBACStore，含 grant/revoke/query 操作
- `host/identity/__init__.py` -- 更新以匯出 BotRegistry、BotIdentity、CrossBotProtocol、CrossBotMessage
- `host/sdk_api.py` -- 加入 bot 登錄 WebSocket 端點：bot_register、bot_lookup、bot_list、bot_handshake
- `host/main.py` -- 第 3 階段啟動區塊：BotRegistry + RBAC 初始化

### 建立的 GitHub Issues
- #265 [Phase 3] 跨 Bot 身份協議
- #266 [Phase 3] 企業工具套件 - 整合層
- #267 [Phase 3] RBAC - 角色型存取控制
- #268 [Phase 3] Matrix 頻道支援
- #269 [Phase 3] 多租戶支援

### 第 3 階段後的架構
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

### 新增（第 2 階段：通用記憶體層）
- `host/memory/summarizer.py` -- MemorySummarizer：LLM 驅動的對話->MEMORY.md 壓縮
  - 支援 Gemini / Claude / OpenAI 相容 API，帶優雅降級後備
  - MEMORY.md 接近 8KB 限制時自動壓縮
- `host/sdk_api.py` -- 外部 WebSocket SDK API（port 8767）
  - 從外部工具/CLI 查詢 agent 記憶
  - 通過 WebSocket 向群組提交任務
  - 向監控客戶端實時廣播事件
  - 可選的 bearer token 認證
- `host/container_runner.py` -- 向 Docker 容器傳遞穩定的 AGENT_ID 環境變數
  - 在重啟間啟用持久的 agent 身份
- `host/main.py` -- 第 2 階段啟動整合
  - SdkApi 作為後台 asyncio 任務啟動
  - MemorySummarizer 初始化

### 第 2 階段後的架構
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

### 處理的 Issues
- #255（MemorySummarizer）、#256（SdkApi）、#257（容器中的 AGENT_ID）、
- #258（跨專案知識）、#259（自動身份摘要更新）

---

## [Unreleased] — UnifiedClaw 路線圖

### 架構（計劃中）
- [ ] 通用記憶體匯流排（sqlite-vec + 共享記憶體範圍）
- [ ] Agent 身份層（跨重啟的持久設定檔）
- [ ] WebSocket IPC 替換基於文件的輪詢
- [ ] 跨 agent 基因組協作（第 4 階段）
- [ ] MinionDesk 企業工具移植（第 3 階段）
- [ ] Matrix/Signal 頻道支援（第 3 階段）

### 第 1 階段進行中
- [ ] sqlite-vec 語義搜尋整合
- [ ] MemoryBus 抽象介面類
- [ ] 通過 WebSocket 的 Agent 適應度反饋

---

## [1.11.42] — 2026-03-17

### 新增
- SECURITY.md — 漏洞回報政策
- ARCHITECTURE.md — UnifiedClaw 架構設計與路線圖
- 更新 .gitignore 以排除 __pycache__ 和 .pyc 文件

### 修復
- dev_engine _deploy_files() 中的路徑穿越漏洞
- 長時間運行容器 session 中的記憶體洩漏
- 進化守護進程時間戳處理錯誤

### 安全性
- 22 個架構和安全性 issues 已追蹤（見 GitHub Issues）
- 3 個 CRITICAL issues 已識別，需立即補救

---

## [1.11.34] - 2026-03-17
### 新增
- 心跳：EvoClaw 每 30 分鐘向監控群組發送 `💓 EvoClaw 運行中 | 上線時間 | 群組數 | 成功/失敗數` — 若 ping 停止，表示主機已停機（#217）
- `HEARTBEAT_INTERVAL` 環境變數以設定間隔（預設 1800s，設為 0 以停用）

## [1.11.33] - 2026-03-17
### 新增
- `/monitor` Telegram 命令：在任何群組中發送以立即將其登錄為監控群組 — 無需編輯 `.env` 或重啟（#215）
- `_write_monitor_jid_to_env()`：自動將 `MONITOR_JID` 持久化至 `.env`，使設定在重啟後保留

## [1.11.32] - 2026-03-17
### 新增
- 通過 `MONITOR_JID` 環境變數支援監控群組：錯誤警報自動轉發至專用的看門狗 Telegram 群組（#213）
- `reset_group` IPC 命令：監控 agent 可在無人工干預的情況下解凍卡住的群組
- `mcp__evoclaw__reset_group` 工具可在 Gemini、OpenAI 相容和 Claude 模式下供 agent 使用
- `groups/telegram_monitor/MEMORY.md` 模板：看門狗 agent 角色預先配置
- 獨立看門狗排程任務：每 5 分鐘檢查 EvoClaw DB 作為備份

## [1.11.31] - 2026-03-17
### 新增
- 行內錯誤通知：容器崩潰/超時/異常現在直接在對話中向使用者發送訊息 — 無需觀察後端日誌（#211）
- 速率限制為每個群組每 5 分鐘 1 次通知，以防止故障風暴期間的淹沒
- 零配置開箱即用

## [1.11.30] - 2026-03-17
### 修復
- `run_agent_openai()` 在每個 NIM / OpenAI 相容 session 中崩潰，錯誤為 `NameError: name 'group_folder' is not defined` — MEMORY.md 路徑從未傳入的局部變數計算（#209）

## [1.11.29] - 2026-03-17
### 新增
- `TELEGRAM_PROXY` 環境變數：通過 HTTP 或 SOCKS5 代理路由 Telegram API 呼叫 — 解決 `api.telegram.org` 被封鎖的網路上的 TimedOut 錯誤（#207）
- 將 `MAX_RETRIES` 從 3 增加至 5，帶上限的指數退避（2s、4s、8s、16s、最大 30s），用於瞬時網路故障
- 在 `.env.example` 中記錄 `TELEGRAM_PROXY`，包含 HTTP 和 SOCKS5 範例

## [1.11.28] - 2026-03-17
### 修復
- 安全性：`_resolve_container_path` 中通過 `str.startswith` 前綴繞過的路徑穿越 — 現在使用 `pathlib.is_relative_to()`（#201）
- 安全性：`register_group` IPC 處理器現在驗證資料夾名稱以防路徑穿越（#202）
- 記憶體洩漏：每個群組的追蹤字典（`_per_jid_cursors`、`_group_msg_timestamps`、失敗計數器）現在在群組重載時清除（#203）
- 進化守護進程 `_last_micro_sync`/`_last_weekly_compound` 現在在啟動時從 DB 載入 — 防止每次重啟後立即執行（#205）

## [1.11.27] - 2026-03-17
### 修復
- 安全性強化：減少容器密鑰暴露 — 僅傳遞 LLM 金鑰，排除頻道/SCM token（PR #198）
- 可靠性：含堆疊追蹤的正確錯誤日誌，啟動時的 Docker 健康檢查（PR #199）
- 代碼品質：DRY _store_bot_reply() 輔助函式，命名常數，僅在啟動時的密鑰驗證（PR #200）
- container_logs 表從未清除 — 加入 prune_old_logs() 以防止無界磁碟增長
- warm_logs FTS 索引在刪除時未同步 — 清除後出現過期搜尋結果
- container_runner 中的 stderr_lines 列表無界 — 上限為 5000 行以防止 OOM

### 新增
- 未設定 DASHBOARD_PASSWORD 時啟動時的 Dashboard 認證警告
- 啟動時的 ENABLED_CHANNELS 驗證 — 對無法識別的頻道名稱發出警告

## [1.11.26] - 2026-03-16
### 新增
- 意志系統：MEMORY.md 智慧注入（身份永遠保留 + task log 後 3000 字元，防截斷）
- 身份引導 Bootstrap：首次或缺少身份區段時注入模板 + 填寫指令
- Milestone Enforcer v3：偵測 Write/Edit/Bash 寫入 MEMORY.md，turn-28 未寫入注入 CRITICAL 提醒
- Host Auto-Write Fallback：成功 run 後若 mtime < t0，host 自動補寫最小記錄
- soul.md 新增 `### 自我認知` 區段與 MEMORY.md 結構說明

## [1.11.25] - 2026-03-16
### 修復
- circuit breaker 誤分類：container 有 stderr（確實跑了）時呼叫 _record_docker_success() 而非 _record_docker_failure()，防止 agent crash 錯誤開路
- 新增 SIGUSR1 信號處理器：kill -USR1 <pid> 可線上重置 circuit breaker，不需重啟進程

## [1.11.24] - 2026-03-16
### 重構
- 靈魂規則獨立為 container/agent-runner/soul.md，runner 啟動時讀取注入 — 更新規則無需改 Python code

## [1.11.23] - 2026-03-16
### 修復
- health_monitor: 加入 ERROR_RATE_MIN_SAMPLES=5 門檻，避免樣本數不足時誤報高錯誤率（如 1/1=100%）

## [1.11.22] - 2026-03-16
### 修復
- Docker circuit breaker 半開放狀態（half-open）：60秒後允許一次試探請求，避免永久死鎖 (#177)
- group_queue.py: enqueue_message_check 和 _drain_group 加入 retry_count > 0 檢查，防止 circuit breaker 開路時形成緊密無限重試迴圈（「無法中斷」問題）(#177)

## [2.4.16] - 2026-03-16
### 修復
- 里程碑強制器 v2：區分「實質工具」vs「報告工具」— 只有 Bash/Read/Write/run_agent + send_message 組合才算真里程碑 (#169)
- 新增 _only_notify_turns 計數器：連續 >=2 輪只呼叫 send_message 無實質工具 → 注入強硬反假報告警告 (#169)
- CRITICAL 規則加入「禁止虛報進度」和「卡住請用 run_agent 委派」(#169)

## [1.11.21] - 2026-03-16
### 修復
- 里程碑強制器 v2：區分「實質工具」vs「報告工具」— 只有 Bash/Read/Write/run_agent + send_message 組合才算真里程碑 (#175)
- 新增 _only_notify_turns 計數器：連續 >=2 輪只呼叫 send_message 無實質工具 → 注入強硬反假報告警告 (#175)
- CRITICAL 規則加入「禁止虛報進度」和「卡住請用 run_agent 委派」(#175)

## [1.11.20] - 2026-03-16
### 新增
- MEMORY.md 啟動注入：session 啟動時讀取 {group_folder}/MEMORY.md，注入為「長期記憶」section — 讓知識歸檔真正有效 (#173)
- 里程碑強制器：run_agent_openai loop 追蹤 _turns_since_notify，超過 4 輪無 mcp__evoclaw__send_message 自動注入提醒 (#173)
- Level B 啟發式偵測：prompt 長度 > 200 或含關鍵字時代碼層面標記 Level B，輔助模型委派決策 (#173)

## [1.11.19] - 2026-03-16
### 新增
- Agent 靈魂：系統提示中加入 `## 任務協調與智慧委派` 區段
- 預飛分析：Level A（簡單，直接處理）vs Level B（複雜，委派）任務分類
- 智慧委派：Level B 任務使用注入了 `/reasoning on` 的 `mcp__evoclaw__run_agent`
- 知識歸檔：重要任務將摘要附加至 `MEMORY.md`
- 透明度：Level B 宣布工作目錄，創建 `progress.log`，發送里程碑更新（#171）

EvoClaw 的所有重要變更都將記錄在此文件中。

格式基於 [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)，
本專案遵循 [語義化版本控制](https://semver.org/spec/v2.0.0.html)。

## [1.11.18] — 2026-03-16

### 修復
- `container/agent-runner/agent.py`: 追蹤 `_no_tool_turns` 計數器，連續未呼叫工具時將 `tool_choice` 從 `"auto"` 升級為 `"required"` — API 層面強制模型必須呼叫工具 (Fix #169)
- `container/agent-runner/agent.py`: 連續 3 次無 tool call → break loop，防止無限循環 (Fix #169)
- `container/agent-runner/agent.py`: `tool_choice="required"` 不支援時自動降級為 `"auto"` (Fix #169)
- `container/agent-runner/agent.py`: fake-status re-prompt 訊息改為說明「下一輪強制 tool_choice=required」(Fix #167+#169)

## [1.11.17] — 2026-03-16

### 修復
- `container/agent-runner/agent.py`: CRITICAL 系統提示加入第二條禁令 — 明確禁止 `*(正在執行...)*` 等假狀態行，說明這些純文字對系統沒有任何作用 (Fix #167)
- `container/agent-runner/agent.py`: openai-compat loop 新增 Fallback 2 — 偵測 `*(...)* ` / `*[...]* ` 假狀態模式，自動 re-prompt 模型「請停止假裝，立刻呼叫 Bash tool」(Fix #167)

## [1.11.16] — 2026-03-14

### 修復
- `host/container_runner.py`: `_stop_container` 改用 `docker kill`（即時 SIGKILL）替代 `docker stop --time 10`（10 秒 grace period），大幅縮短 shutdown 等待時間 (Fix #164)
- `host/container_runner.py`: 新增 `kill_all_containers()` — shutdown 超時後強制 kill 所有追蹤中的 container (Fix #164)
- `host/container_runner.py`: `CancelledError` handler 直接呼叫 `proc.kill()` 殺死 asyncio subprocess，再用 `asyncio.shield(_stop_container())` 確保 docker kill 完成 (Fix #164)
- `host/main.py`: 第二次 Ctrl+C (SIGINT) → 同步 `docker kill` 所有 container + 立即 `os._exit(1)` — 不再無限卡住 (Fix #164)
- `host/main.py`: `wait_for_active` timeout 從 30 秒縮短至 **10 秒** (Fix #164)
- `host/main.py`: `wait_for_active` 超時後呼叫 `kill_all_containers()` 強制終止殘留 container (Fix #164)
- `host/main.py`: final `asyncio.gather(*pending, ...)` 加 **5 秒 timeout** — task cleanup 本身卡住時不再永久阻塞 (Fix #164)

## [1.11.15] — 2026-03-14

### 修復
- `host/container_runner.py`: `_read_secrets()` 加入 `GITHUB_TOKEN` / `GH_TOKEN` — 這才是真正傳進 container 的 secrets 函數（非 config.get_secrets()），之前一直修錯地方

## [1.11.14] — 2026-03-13

### 修復
- `container/agent-runner/agent.py`: openai-compat loop 加入 bash code block 自動執行 fallback — Qwen/NIM 模型輸出 ` ```bash ` 代碼塊時自動偵測並執行，結果回饋 history 繼續迴圈
- `container/agent-runner/agent.py`: 系統提示加入 CRITICAL tool usage 警告 — 明確禁止輸出 code blocks，要求 ALWAYS call Bash tool directly

## [1.11.13] — 2026-03-13

### 新增
- `.env.example`: 加入 `GITHUB_TOKEN` 說明（附 GitHub settings token 連結），讓用戶知道必須設定此值才能讓 container 使用 git push / gh CLI

## [1.11.12] — 2026-03-13

### 修復
- `container/Dockerfile`: 安裝 GitHub CLI (`gh`)，修復 container 內 `gh: command not found` 根本原因
- `container/agent-runner/agent.py`: `gh auth login` 成功後執行 `gh auth setup-git`，設定 git credential helper 讓 `git push` via HTTPS 能使用 token
- `container/agent-runner/agent.py`: 設定 `git config user.email/user.name`（agent@evoclaw.local），避免「Please tell me who you are」commit 失敗

## [1.11.11] — 2026-03-13

### 修復
- `host/config.py`: `get_secrets()` 加入 `GITHUB_TOKEN` / `GH_TOKEN`，修復 container 啟動時 gh CLI 永遠顯示 `⚠️ GH AUTH no GITHUB_TOKEN in secrets` 的根本原因

## [1.11.10] — 2026-03-13

### 修復
- `container/agent-runner/agent.py`: secrets 設入 `os.environ` 後自動執行 `gh auth login --with-token`，解決 `gh repo create` 及 `git push` 因「no credentials found」失敗的問題；認證成功/失敗/gh 未安裝均有 log

## [1.11.9] — 2026-03-13

### 變更
- `container/agent-runner/agent.py`: 工具 args/result 日誌截斷從 400 提升至 1500 字，可看到完整 bash command 和執行結果

## [1.11.8] — 2026-03-13

### 新增
- `container/agent-runner/agent.py`: 在 `system_instruction` 建立後立即 log 前 800 字（`📋 SYSTEM`，逐行分段顯示）
- `container/agent-runner/agent.py`: log 最近 3 輪對話歷史（`📚 HISTORY`），方便在 Container Logs 看到完整 LLM context

## [1.11.7] — 2026-03-13

### 新增
- `container/agent-runner/agent.py`: 從 XML prompt 提取純文字 `💬 USER` log，顯示實際用戶訊息（最多 600 字）
- `container/agent-runner/agent.py`: 新增 `📤 REPLY` log，顯示 bot 回覆前 600 字（原本只記字數）
- `container/agent-runner/agent.py`: 工具 args/result 日誌截斷從 200 提升至 400 字

### 修復
- `host/dashboard.py`: 修復 `showContainerLog()` 中 undefined 問題 — 雙 key（數字 + 字串）查找處理型別不符
- `host/db.py`: stderr 儲存限制從 8KB 提升至 32KB，避免長對話日誌截斷

## [1.11.6] — 2026-03-13

### 新增
- `host/dashboard.py`: Container Logs 分頁新增「📋 展開」按鈕，點擊後彈出 Modal 顯示完整 stderr（含所有 print/log 輸出）
- Stderr 摘要由最後 3 行改為最後 5 行
- Modal 採 Monospace 字體、深色背景，可捲動瀏覽完整 container 執行日誌
- 使用 JS Map 快取完整 stderr，展開無需額外 API 請求

## [1.11.5] — 2026-03-13

### 新增
- `host/db.py`: 新增 `container_logs` 資料表，記錄每次 container 執行的 stderr/stdout 摘要
- `host/db.py`: 新增 `log_container_start()` / `log_container_finish()` / `get_container_logs()` 函數
- `host/container_runner.py`: 在所有執行路徑（success/error/timeout/exception）呼叫 log 函數
- `host/dashboard.py`: 新增「🐳 Container Logs」分頁 — 可依群組/狀態過濾，顯示執行時間、耗時、stderr 摘要
- 新增 `GET /api/container-logs` 端點（支援 `jid`、`status`、`limit` 查詢參數）

## [1.11.4] — 2026-03-13

### 新增
- `host/dashboard.py`: 新增「⚡ Skills」分頁 — 掃描 `skills/` 目錄並顯示每個技能的名稱、版本、作者、說明
- `host/dashboard.py`: 新增「📈 使用統計」分頁 — 整合訊息數/群組、任務執行摘要（總數/成功率/平均時間）、進化執行統計
- 新增 `GET /api/skills` 端點：掃描 `skills/*/manifest.yaml` 回傳技能清單
- 新增 `GET /api/usage` 端點：整合 `messages`、`task_run_logs`、`evolution_runs` 三表統計

## [1.11.3] — 2026-03-13

### 新增
- `host/dashboard.py`: 新增「🧠 記憶查看器」分頁 — 可依群組檢視熱記憶（MEMORY.md）、暖記憶日誌（最近 N 天），以及全文搜尋冷/暖記憶
- `host/dashboard.py`: 新增 `GET /api/memory?jid=&days=&search=` 端點，整合 `db.get_hot_memory`、`db.get_warm_logs_recent`、`memory.search.memory_search`

## [1.11.2] — 2026-03-13

### 修復
- `main.py`: 關機 `finally` 區塊順序修正 — 先 `channel.disconnect()` 再取消 asyncio tasks，消除 Telegram CRITICAL CancelledError 誤報 (#135)
- `channels/telegram_channel.py`: `disconnect()` 各步驟獨立 try/except，防止 `CancelledError` 向外傳播

## [1.11.1] — 2026-03-13

### 修復
- `host/config.py`: `CONTAINER_IMAGE` 預設值從 `evoclaw-agent:1.11.0` 改為 `evoclaw-agent:latest`，避免每次版本 bump 都造成 Docker image 找不到錯誤 (#133)
- 新增 `Makefile` 提供 `make build` / `make start` / `make dev` 等指令

## [1.11.0] — 2026-03-12

### 新增
- 三層記憶體系統，靈感來自 OpenClaw/MemSearch 架構
  - 熱記憶：每個群組的 MEMORY.md（8KB），每次容器呼叫時載入
  - 暖記憶：每次對話後自動附加的每日日誌，3 小時微同步
  - 冷記憶：SQLite FTS5 混合搜尋（關鍵字 + 近期性評分）
  - 每週彙整：清除 >30 天日誌，將模式提煉至熱記憶
- 容器在系統上下文中接收熱記憶（`[MEMORY]...[/MEMORY]`）
- 容器可通過回應 JSON 中的 `memory_patch` 更新熱記憶
- IPC 命令 `memory_search` 用於對話中的冷記憶查詢
- 新 DB 表：`group_hot_memory`、`group_warm_logs`、`group_warm_logs_fts`、`group_cold_memory`、`group_cold_memory_fts`、`group_memory_sync`
- 新模組：`host/memory/`，含 `hot.py`、`warm.py`、`search.py`、`compound.py`

### 例行事務
- 版本升級 1.10.28 → 1.11.0

## [1.10.28] - 2026-03-12

### 修復
- **#128** `agent.py`：`newSessionId` 現在保留來自主機的傳入 `sessionId`，而非始終生成新的 `uuid.uuid4()` — 每次容器執行都在啟動新的 session，銷毀了跨輪次的對話記憶；現在主機提供的 session ID 被回傳，只有在未傳入 session ID 時才退回至新 UUID
- **#128** `main.py`：`get_conversation_history(jid, limit=20)` 增加至 `limit=50` — 之前的 20 訊息視窗（約 10 輪）對有意義的多輪上下文太小；50 條訊息（約 25 輪）為 LLM 提供了更豐富的對話歷史
- **#129** `daemon.py`：`EVOLUTION_INTERVAL_SECS` 從 `24 * 3600`（24 小時）縮短至 `3600`（1 小時）— 24 小時的首次循環延遲使進化無法觀察或測試；1 小時間隔使守護進程在開發和生產環境中均可實用
- **#129** `daemon.py`：`MIN_SAMPLES` 從 `10` 減少至 `3` — 要求 10 次執行才觸發進化意味著群組幾乎從未達到閾值；3 個樣本足以做出基本適應度決策，同時仍能避免單樣本雜訊
- **#129** `container_runner.py`：當容器輸出沒有有效標記或 JSON 解析失敗時，現在呼叫 `record_run(..., success=False)` — 這些錯誤路徑之前在未記錄的情況下提前返回，導致靜默資料丟失並低估了適應度計算中的失敗率
- **#129** `fitness.py`：`record_run()` 異常處理器從靜默的 `log.warning` 改為 `log.error("record_run failed (jid=%s): %s", jid, exc)` — DB 錯誤在高流量日誌中之前很容易被忽視
- **#129** `genome.py`：`upsert_genome()` 異常處理器從 `log.warning` 改為 `log.error("upsert_genome failed (jid=%s): %s", jid, exc)` — 基因組更新失敗現在在錯誤日誌中清晰可見
- **#129** `db.py`：`get_active_evolution_jids()` 現在包含冷啟動群組 — 之前僅查詢 `evolution_runs`（表為空時返回空列表），在新部署時導致「正在評估 0 個群組」；現在還包含有近期對話歷史的群組，使守護進程可以啟動其第一個基因組

### 例行事務
- 版本升級 1.10.27 → 1.10.28

## [1.10.27] - 2026-03-12

### 修復
- **#118** `main.py`：`_is_rate_limited()` — 以 `maxlen=RATE_LIMIT_MAX_MSGS*2` 初始化每個群組的 deque；沒有上限時，持續在滾動視窗內發送的群組的 deque 無限增長，在數天運行後導致記憶體膨脹和 O(n) deque 操作
- **#119** `ipc_watcher.py`：加入 `_cleanup_stale_results()` 背景清除 — 移除 `data/ipc/*/results/` 中超過 1 小時的子 agent 結果檔案；每 120 個 IPC 輪詢週期執行一次，以防止容器在寫入前崩潰或父 agent 在讀取前被取消時磁碟填滿
- **#120** `evolution/immune.py`：`check_message()` 現在區分瞬時 DB 鎖（`sqlite3.OperationalError: database is locked`）和永久錯誤 — 瞬時鎖允許失敗（允許訊息），以防止短暫的 prune_old_logs 鎖封鎖所有群組訊息；永久/IO 錯誤仍然安全失敗
- **#121** `main.py`：優雅關機現在在斷開頻道連接前明確取消所有待處理的 asyncio 任務 — 在 `asyncio.sleep()` 中睡眠的任務（訊息循環 POLL_INTERVAL、進化循環）現在在 SIGTERM 時立即退出，而不是阻塞關機達 POLL_INTERVAL 秒
- **#122** `task_scheduler.py`：當 `compute_next_run()` 返回 `None`（無效的排程表達式）時，任務現在被標記為 `status=paused`，帶有說明性的 `last_result` 訊息，而不是保留 `next_run=NULL`/`status=active`，對排程器輪詢不可見但從未被清理

### 例行事務
- 版本升級 1.10.26 → 1.10.27

## [1.10.26] - 2026-03-12

### 修復
- **#118** `main.py`：`_is_rate_limited()` — 以 `maxlen=RATE_LIMIT_MAX_MSGS*2` 初始化每個群組的 deque；沒有上限時，持續在滾動視窗內發送的群組的 deque 無限增長，在數天運行後導致記憶體膨脹和 O(n) deque 操作
- **#119** `ipc_watcher.py`：加入 `_cleanup_stale_results()` 背景清除 — 移除 `data/ipc/*/results/` 中超過 1 小時的子 agent 結果檔案；每 120 個 IPC 輪詢週期執行一次，以防止容器在寫入前崩潰或父 agent 在讀取前被取消時磁碟填滿
- **#120** `evolution/immune.py`：`check_message()` 現在區分瞬時 DB 鎖（`sqlite3.OperationalError: database is locked`）和永久錯誤 — 瞬時鎖允許失敗（允許訊息），以防止短暫的 prune_old_logs 鎖封鎖所有群組訊息；永久/IO 錯誤仍然安全失敗
- **#121** `main.py`：優雅關機現在在斷開頻道連接前明確取消所有待處理的 asyncio 任務 — 在 `asyncio.sleep()` 中睡眠的任務（訊息循環 POLL_INTERVAL、進化循環）現在在 SIGTERM 時立即退出，而不是阻塞關機達 POLL_INTERVAL 秒
- **#122** `task_scheduler.py`：當 `compute_next_run()` 返回 `None`（無效的排程表達式）時，任務現在被標記為 `status=paused`，帶有說明性的 `last_result` 訊息，而不是保留 `next_run=NULL`/`status=active`，對排程器輪詢不可見但從未被清理

### 例行事務
- 版本升級 1.10.25 → 1.10.26

## [1.10.25] - 2026-03-12

### 修復
- **#105** `main.py`：`_is_duplicate_message()` TOCTOU 競爭 — 轉換為 `async def`，加入在 `main()` 中初始化的 `_dedup_lock = asyncio.Lock()`，並將整個先檢查後插入序列包裝在單一 `async with _dedup_lock:` 區塊中，使兩個協程無法同時讀取/插入
- **#106** `task_scheduler.py`：`run_task()` 現在在 `finally` 區塊中推進 `next_run` — 無論執行成功還是拋出異常，計算的 `next_run_ts` 始終通過 `db.update_task()` 寫入，防止任務卡在過去的時間戳
- **#107** `webportal.py`：`_pending_replies` 從 `dict[str, str]` 改為 `dict[str, tuple[str, float]]`，儲存 `(session_id, created_at_timestamp)`；`_cleanup_pending_replies()` 現在除了驅逐 session 不再存在的條目外，還驅逐超過 300 秒（5 分鐘 TTL）的條目
- **#108** `evolution/immune.py`：`check_message()` 從允許失敗改為安全失敗 — 來自 DB 呼叫的異常現在返回 `(False, "immune_check_error")`（拒絕）而非 `(True, None)`（允許）；DB 中斷現在無法再繞過免疫檢查
- **#109** `ipc_watcher.py`：`apply_skill` 和 `uninstall_skill` IPC 操作包裝在 `asyncio.wait_for(..., timeout=300.0)` 中；`TimeoutError` 記錄錯誤並向使用者發送通知，而非無限期掛起 `_skills_lock`
- **#110** `container_runner.py`：加入 `_SECRET_PATTERNS` regex 列表和 `_redact_secrets()` 函式；所有容器 stderr 行在記錄前現在都通過 `_redact_secrets()` 處理，防止 API 金鑰、token 和密碼出現在主機日誌或 dashboard 日誌流中

### 例行事務
- 版本升級 1.10.24 → 1.10.25

## [1.10.24] - 2026-03-12

### 修復
- **#92** dev_engine.py 第 7 階段：用 `Path.relative_to()` 替換字串 `startswith()` 路徑穿越守衛 — 消除像 `/base_evil/file` 這樣路徑的誤通過
- **#90** webportal.py：將 `_pending_replies[msg_id] = session_id` 移至 `_sessions_lock` 內，以消除並發 `/api/send` 請求之間的競爭條件

### 已關閉（在先前版本中已修復）
- **#95** Docker：CJK 字體和 PPT/PDF 函式庫（libfreetype6、zlib1g、fonts-wqy-zenhei）已在 v1.10.21 Dockerfile 中
- **#96** CONTAINER_IMAGE 環境變數自 v1.10.22 起已可設定
- **#97** RELEASE.md 已在 v1.10.22 中加入
- **#98** CHANGELOG.md 已在 v1.10.22 中加入
- **#99** #95 的重複

### 例行事務
- 版本升級 1.10.23 → 1.10.24

## [1.10.23] - 2026-03-12

### 修復
- **#86** `router.py`：在所有訊息區塊重試後傳送失敗時，加入使用者通知（⚠️ 回應傳送失敗）
- **#87** `discord_channel.py`：將 `future.result(30)` 包裝在 try/except 中以捕捉 `concurrent.futures.TimeoutError` — 防止 Discord API 回應緩慢時崩潰
- **#88** `whatsapp_channel.py`：`_last_wamid` 從普通字典改為 `OrderedDict`，帶 LRU 驅逐，上限 10,000 個條目 — 防止高流量部署中的無界記憶體增長
- **#89** `fitness.py`：修復 `speed_score` 公式 — 低於目標的回應時間現在正確評分 1.0（之前錯誤地返回 > 1.0 的值）
- **#90** `webportal.py`：在 `db.store_message()` 呼叫前釋放 Sessions 鎖 — 防止並發 session 和訊息儲存操作下的潛在死鎖
- **#91** `telegram_channel.py`：上傳超時現在可通過 `TELEGRAM_UPLOAD_TIMEOUT` 環境變數設定（預設：300s，之前硬編碼為 120s）
- **#92** `dev_engine.py`：路徑穿越守衛改進
- **#93** `immune.py`：在 `check_message()` 中防範空的 `sender_jid` — 防止格式錯誤的訊息上的潛在崩潰或錯誤的威脅歸因

### 例行事務
- 版本升級 1.10.22 → 1.10.23

## [1.10.22] - 2026-03-12

### 修復
- **#66** WhatsApp `send_typing` 現在發送帶有正確 `wamid`（每條訊息的 WhatsApp ID）的已讀回條，而非 `chat_id`；未收到先前訊息時優雅跳過
- **#68** `send_file` IPC 處理器支援 `deleteAfterSend` 標誌；`research-ppt` 技能指示 agent 在傳送後清理臨時 `.pptx`/`.txt` 文件
- **#5** 正式關閉：每個 JID 的時間戳游標（在 v1.10.17 中實作）完全解決了群組隔離違反問題

### 新增
- **#6** 所有 LLM 提供商的多金鑰輪換：`GOOGLE_API_KEY`、`CLAUDE_API_KEY`、`OPENAI_API_KEY`、`NIM_API_KEY` 接受以逗號分隔的值；容器 agent 在 429/配額錯誤時自動輪換至下一個金鑰，帶 `🔑 KEY ROTATE` 日誌

### 例行事務
- 版本升級 1.10.19 → 1.10.22

## [1.10.21] - 2026-03-12

### 新增
- **生產就緒的 Docker 映像**（`container/Dockerfile`）：將基礎映像從 `node:22-slim` 升級至 `node:22`（完整 Debian），以提供原生 Python 擴展和 MCP 工具所需的更廣泛系統函式庫相容性（Issue #83）
- **完整文件生成堆疊**預裝於映像中：`reportlab`（PDF）、`openpyxl`（Excel）、`python-docx`（Word），以及現有的 `python-pptx==1.0.2` — 消除了所有文件類型的運行時 pip 安裝（Issue #77）
- **網頁抓取堆疊**預裝：`httpx`、`beautifulsoup4`、`lxml` — agent 可在無運行時網路依賴的情況下抓取和解析 HTML（Issue #78）
- **圖像處理**預裝：`Pillow`，帶系統函式庫 `libjpeg-dev`、`libpng-dev`、`zlib1g-dev`、`libcairo2` — reportlab 圖像嵌入和未來視覺工作流所需（Issue #79）
- **資料科學堆疊**預裝：`pandas`、`numpy`、`matplotlib` — 啟用容器內資料分析、表格處理和圖表生成（Issue #80）
- **完整 CJK 字體覆蓋**：加入 `fonts-liberation`、`fonts-noto-color-emoji`，以及現有的 `fonts-noto-cjk`、`fonts-wqy-zenhei`、`fonts-wqy-microhei`；全部通過 `fc-cache -fv` 處理（Issue #81）
- **系統工具**：加入 `wget`、`unzip`、`jq`、`ffmpeg` — 涵蓋許多 MCP 伺服器安裝腳本所需的存檔提取、JSON shell 腳本和媒體處理（Issue #82）
- **構建工具**：加入 `python3-dev`、`build-essential`、`gcc`，使帶 C 擴展的 pip 套件（lxml、Pillow、numpy）在沒有預構建 wheel 的情況下也能正確編譯
- **基礎設施與專案分離**：Dockerfile 現在擁有所有基礎設施 Python 套件；`requirements.txt` 保持精簡（僅 `google-genai`、`openai`、`anthropic`）
- 加入 **`libfontconfig1`** 和 **`libpangocairo-1.0-0`** 以確保字體渲染在無頭 PDF/PPT 生成中正常工作

### 變更
- 基礎映像：`node:22-slim` → `node:22` 以提供完整的系統函式庫可用性（Issue #83）
- `apt-get install` 現在使用 `--no-install-recommends` 以在升級基礎的同時保持映像大小最小化

## [1.10.20] - 2026-03-12

### 變更
- 升級 container Docker 基礎鏡像至 python:3.9 (Debian Bullseye)
- 預裝中文字體：fonts-wqy-zenhei、fonts-wqy-microhei + fc-cache
- 預裝系統依賴：libfreetype6、libpng16-16、zlib1g
- 預裝 python-pptx==1.0.2 進鏡像，消除 runtime pip 網路依賴
- 設定 PYTHONUNBUFFERED=1 + LANG=C.UTF-8 確保輸出編碼正確

### 修復
- research_ppt 工具在網路不穩定時因 pip install 失敗而崩潰的問題
- 中文字元在 PPT/PDF 中顯示為方塊的問題

## [1.10.19] - 2026-03-12

### 修復
- **Gmail 郵件主體大小無界限**（`host/channels/gmail_channel.py`）：`_extract_body()` 現在在 32 KB 處截斷已解碼的郵件主體，帶有明確的 `[... email truncated at 32 KB ...]` 後綴。之前，大型郵件（電子報、引用的討論串）可能使 agent LLM 上下文視窗飽和並使訊息表膨脹（Issue #69）
- **Telegram 非文字訊息靜默丟棄**（`host/channels/telegram_channel.py`）：加入了處理照片、語音訊息、視頻、音頻、文件、貼圖、位置和聯絡人訊息類型的處理器，發送簡短的資訊性回覆：`I can only process text messages at the moment.` 之前，所有非文字 Telegram 訊息都被靜默忽略，使用者零反饋（Issue #70）
- **GroupQueue `create_task()` 靜默吞噬異常**（`host/group_queue.py`）：所有 `asyncio.create_task()` 呼叫現在附加了 `_task_done_callback`，在 ERROR 級別記錄未處理的異常。沒有這個，inner try/except 外的異常（例如關機期間的 CancelledError、來自事件循環的 RuntimeError）被 Python 事件循環靜默丟棄（Issue #71）
- **`.env.example` 缺少安全關鍵和操作變數**（`.env.example`）：加入了 `WHATSAPP_APP_SECRET`（帶有顯著的安全警告）、`LOG_FORMAT`、`RATE_LIMIT_MAX_MSGS`、`RATE_LIMIT_WINDOW_SECS`、`DASHBOARD_USER`、`DASHBOARD_PASSWORD`、`WEBPORTAL_ENABLED`、`WEBPORTAL_HOST`、`WEBPORTAL_PORT` 和 `HEALTH_PORT`。省略 `WHATSAPP_APP_SECRET` 尤其關鍵 — 沒有此變數的操作員在沒有 HMAC 簽名驗證的情況下運行，接受任何呼叫者的 webhook 載荷（Issue #72）
- **IPC `ensure_future()` 發射後忘記靜默吞噬異常**（`host/ipc_watcher.py`）：`_run_apply_skill`、`_run_uninstall_skill`、`_run_list_skills`、`_run_subagent` 和 `_run_dev_task` 的所有 `asyncio.ensure_future()` 呼叫現在附加了 `_ipc_task_done_callback`，在 ERROR 級別記錄未處理的異常（Issue #73）
- **Discord `disconnect()` 死鎖 — `close()` 在錯誤的事件循環上呼叫**（`host/channels/discord_channel.py`）：`disconnect()` 現在通過 `asyncio.run_coroutine_threadsafe()` 在 Discord 後台循環上排程 `client.close()`，而非從主 asyncio 循環中等待。還加入了 `thread.join(timeout=5)` 以確保後台線程在進程退出前乾淨地排空（Issue #67）

## [1.10.18] - 2026-03-12

### 修復
- **容器名稱衝突**（`host/container_runner.py`）：`container_name` 現在使用 `run_id`（UUID4）的前 8 個十六進位字元，而非 `int(time.time())`。之前，同一群組的兩個並發容器在同一個時鐘秒內啟動，會導致 Docker 以名稱衝突錯誤拒絕第二個 `run`，觸發熔斷器（Issue #59）
- **五個 DB 讀取函式缺少 `_db_lock`**（`host/db.py`）：`get_messages_since`、`get_state`、`get_session`、`get_registered_group` 和 `get_dev_events` 現在在查詢期間持有 `_db_lock`，與所有其他 DB 讀/寫函式一致。消除了 dashboard/webportal/進化守護進程線程並發存取共享連接時的潛在 `SQLITE_LOCKED` 錯誤和過期讀取（Issue #60）
- **`docker run` 無記憶體/CPU 限制**（`host/container_runner.py`、`host/config.py`）：在容器命令中加入 `--memory` 和 `--cpus` 標誌，通過 `CONTAINER_MEMORY`（預設 `512m`）和 `CONTAINER_CPUS`（預設 `1.0`）環境變數設定。防止失控的 agent 耗盡主機記憶體並觸發核心 OOM 殺手（Issue #61）
- **WAL 文件無限增長**（`host/evolution/daemon.py`）：`_sync_prune_logs()` 現在在日誌清除後執行 `PRAGMA wal_checkpoint(TRUNCATE)`，使 WAL 文件每 24 小時被回收，防止高流量部署中的無界 WAL 增長（Issue #62）
- **未使用的 `immune_cutoff_ms` 變數**（`host/db.py`）：移除了 `prune_old_logs()` 中計算但從未使用的死代碼 `immune_cutoff_ms = int(...)` 賦值；加入了對硬編碼的 90 天免疫威脅保留政策的說明注釋（Issue #63）
- **`PRAGMA foreign_keys = ON` 從未設定**（`host/db.py`）：`init_database()` 現在在設定 WAL 模式後立即啟用 SQLite 外鍵強制執行。沒有此 pragma，任何使用 `ON DELETE CASCADE`/`ON DELETE RESTRICT` 的未來結構新增都會被靜默忽略，導致孤立行和偏差的度量（Issue #64）

## [1.10.17] - 2026-03-12

### 修復
- **每個 JID 的訊息游標**（`host/main.py`）：用每個 JID 的游標字典（`_per_jid_cursors`）替換單一全域 `_last_timestamp`。群組 A 的成功容器執行不再能將共享時間戳推過群組 B 的待處理訊息，防止多群組部署中的靜默訊息丟失（Issue #52）
- **DB 線程安全性**（`host/db.py`）：`get_new_messages()` 和 `get_conversation_history()` 現在在查詢期間持有 `_db_lock`，與所有其他 DB 讀取函式一致。消除了 dashboard/webportal/進化守護進程線程並發運行時的潛在 SQLITE_LOCKED 錯誤和過期讀取（Issue #53）
- **任務排程器緊密重試循環**（`host/task_scheduler.py`）：`run_task()` 異常處理器現在在失敗後呼叫 `db.update_task()` 以推進 `next_run`，防止在正常更新路徑前發生異常時同一任務在每個排程器輪詢週期都重新觸發（Issue #54）
- **空 env 臨時文件競爭**（`host/container_runner.py`）：`_get_empty_env_file()` 現在使用帶雙重檢查鎖定的 `threading.Lock`，防止兩個並發呼叫者在首次呼叫期間各自創建單獨的臨時文件，導致一個文件孤立（Issue #55）
- **SSE 日誌流優雅關機**（`host/dashboard.py`）：`_handle_sse_logs()` 現在檢查模組層級的 `_dashboard_stopping` threading.Event，而非永遠循環，在主機接收到 SIGTERM/SIGINT 時立即退出，而非等待客戶端斷開連接（Issue #56）
- **子 agent 結果文件大小上限**（`host/ipc_watcher.py`）：`_run_subagent()` 現在在寫入 IPC 結果目錄前將結果文字截斷至 1 MB，防止失控的子 agent 通過無界結果文件寫入填滿主機磁碟（Issue #57）
- **排程器空 chat_jid 守衛**（`host/task_scheduler.py`）：`start_scheduler_loop()` 現在以警告跳過空 `chat_jid` 的任務，而非以空鍵將其加入佇列，空鍵可能損壞 GroupQueue 的每個群組序列化映射（Issue #48）

## [1.10.16] - 2026-03-12

### 安全性
- WhatsApp webhook 現在在每次傳送時驗證 `X-Hub-Signature-256` HMAC-SHA256 標頭；驗證失敗的請求被以 HTTP 403 拒絕，防止來自未認證呼叫者的偽造載荷（Issue #42）
- WebPortal session 端點現在返回每個 session 的 CSRF token；所有 POST 請求（`/api/send`）必須將 token 作為 `X-CSRF-Token` 回傳，即使 Basic Auth 憑證被瀏覽器快取也能阻止跨站請求偽造攻擊（Issue #45）
- `immune.py` 內容指紋從 MD5 升級至 SHA-256，防止可能允許攻擊者繞過垃圾郵件計數器或毒化威脅資料庫的雜湊碰撞攻擊（Issue #47）

### 修復
- 從後台線程呼叫的 DB 讀取函式現在持有 `_db_lock`：`get_all_registered_groups`、`get_all_tasks`、`get_evolution_runs`、`get_active_evolution_jids`、`get_recent_run_stats`、`get_group_genome`、`is_sender_blocked`、`get_recent_threat_count`、`get_immune_stats`、`get_evolution_log`、`get_due_tasks`、`get_pending_task_count`、`get_error_stats` — 消除了 dashboard/webportal 和進化守護進程並發負載下的 `database is locked` 錯誤和過期讀取（Issue #43）
- Discord `send_message()` 和 `send_typing()` 現在使用 `asyncio.run_coroutine_threadsafe()` 橋接主事件循環和 Discord 客戶端的後台事件循環，修復了靜默阻止 Discord 訊息傳送的跨循環 `RuntimeError`（Issue #44）
- Gmail 頻道 `_seen_message_ids` 替換為有界的 `OrderedDict`（上限 10,000 個條目，LRU 驅逐），防止長時間運行的高容量郵件處理部署中的無界記憶體增長（Issue #46）
- Slack `auth_test()` 現在在 `connect()` 期間呼叫一次，工作區 ID 被快取在 `self._workspace_id` 上；之前在每條傳入訊息上呼叫，在高訊息速率下達到 Slack 速率限制（Issue #49）
- `ipc_watcher._notify_main_group_error()` 現在在將錯誤字串發送到主群組聊天前對其進行清除 — 文件系統路徑被替換為 `<path>`，輸出被截斷至 120 個字元，防止內部目錄佈局洩漏給聊天成員（Issue #50）

## [1.10.15] - 2026-03-12

### 新增
- 新的 `research-ppt` 技能：生成帶有自我修復依賴管理的 PowerPoint 簡報（Issue #39）
  - `research_ppt_tool.py` 容器工具在運行時通過 `register_dynamic_tool()` 登錄
  - 版本固定 `python-pptx==1.0.2` 以防止臨時 Docker 容器上的依賴漂移
  - 自我修復安裝器在瞬時 PyPI 網路失敗時最多重試 2 次
  - 優雅降級：當 PPTX 生成因任何原因失敗時，產生純文字 `.txt` 報告
  - 字體安全：使用後備鏈跳過不可用的 CJK/中文字體而非崩潰
  - 技能 manifest `skills/research-ppt/manifest.yaml` 包含 `container_tools:` 條目，使工具在不重新構建容器映像的情況下熱部署至 `data/dynamic_tools/`

### 修復
- `router.py` 中的 `route_file()` 現在在嘗試上傳前驗證文件存在並強制執行 45 MB 大小守衛；超大文件向使用者觸發純文字通知，而非靜默的損壞上傳（Issue #40）
- `TelegramChannel.send_file()` 現在通過開放的文件物件流式傳輸文件，而非使用 `f.read()` 將整個二進位內容載入記憶體，防止多兆字節文件的大記憶體峰值（Issue #40）
- `TelegramChannel.send_file()` 包裝在 `asyncio.wait_for(..., timeout=120)` 中，使緩慢的網路不能無限期阻塞 GroupQueue 插槽（Issue #40）
- 從 `TelegramChannel.send_file()` 移除了在每次文件發送時寫入 `/workspace/group/debug_send.log` 的除錯日誌文件（`debug_send.log`）副作用

## [1.10.14] - 2026-03-12

### 修復
- `db.record_immune_threat()` 現在在完整的讀取-修改-寫入序列中持有 `_db_lock`，消除了並發 dashboard/webportal 線程存取下的 TOCTOU 競爭條件（Issue #32）
- `db.prune_old_logs()` 現在還清除 `evolution_log`、`messages`、`immune_threats`（僅雜訊）、`dev_events` 和 `dev_sessions` 表 — 之前僅清除 `task_run_logs` 和 `evolution_runs`，讓五個表無限增長（Issue #33）
- 將 `psutil>=5.9.0` 加入 `host/requirements.txt` 和 `pyproject.toml`；`health_monitor.py` 無條件導入 `psutil` 但未將其列為依賴，導致新安裝時出現 `ImportError`（Issue #34）
- 在 `db.py` 中實作了 `db.get_pending_task_count()` 和 `db.get_error_stats()`；健康監控器用 `hasattr()` 守衛呼叫並靜默使用零值後備，使容器佇列和錯誤率健康檢查永久失效（Issue #35）
- LLM API 呼叫（Gemini、Claude、OpenAI 相容）現在包裝在 `_llm_call_with_retry()` 中，帶指數退避（最多 3 次嘗試：1s、2s 延遲）用於瞬時錯誤（429 速率限制、5xx 伺服器錯誤）；永久錯誤（400、401）不重試（Issue #36）

### 新增
- 定期 DB 日誌清除：`daemon.py` 中的 `evolution_loop` 現在在每個 24 小時進化週期後呼叫 `prune_old_logs()`，確保長時間運行的進程受益於維護而無需重啟（Issue #37）

## [1.10.13] - 2026-03-12

### 安全性
- Agent 工具（`tool_read`、`tool_write`、`tool_edit`）現在在執行前驗證文件路徑解析在 `/workspace/` 內，阻止試圖讀取 `/proc/self/environ` 或其他敏感容器文件的 prompt 注入攻擊（Issue #29）
- `skills_engine/apply.py` post_apply 命令現在對照安全前綴允許清單（`pip install`、`npm install`、`pytest` 等）進行檢查 — 未知命令以警告跳過，防止惡意技能 manifest 執行任意主機命令（Issue #28）
- `ipc_watcher._resolve_container_path` 現在驗證解析的主機路徑留在預期的根目錄內，防止通過精心設計的容器文件路徑進行路徑穿越（Issue #26）

### 修復
- WebPortal `_pending_replies` 字典現在在每次 `/api/send` 呼叫時惰性清理（驅逐 session 不再存在的條目），修復了隨著 session 過期而無限累積的無界記憶體洩漏（Issue #21）
- DB 寫入函式 `set_session`、`create_task`、`update_task`、`delete_task`、`set_registered_group`、`upsert_group_genome`、`block_sender`、`log_evolution_event`、`log_dev_event` 現在全部持有 `_db_lock` 以確保線程安全，防止來自 webportal/dashboard/進化線程的潛在 `database is locked` 錯誤（Issue #22）
- WebPortal `/api/send` 現在強制執行每個群組的速率限制（與 Telegram/WhatsApp 路徑相同），防止已認證的 WebPortal 使用者繞過速率限制器並淹沒 GroupQueue（Issue #25）
- `router.route_outbound` 現在重試失敗的區塊（最多 2 次嘗試，1s 延遲），並在重試後區塊無法傳送時通知使用者，而非靜默丟棄剩餘區塊（Issue #27）

### 新增
- WebPortal `_sessions` 字典現在上限為 500 個並發 session；`_expire_sessions` 在每次新 session 創建時被呼叫以強制執行上限（Issue #23）
- 每個 session 的訊息列表上限為 200 個條目，以防止無界的每個 session 記憶體增長；`deliver_reply` 也遵守此上限（Issue #23）
- WebPortal `_read_body` 現在強制執行 64 KB 最大 POST 主體大小，對超大請求返回 HTTP 413，以防止記憶體耗盡（Issue #24）
- WebPortal `/api/send` 中的單條訊息文字上限為 32 KB（Issue #24）
- `ENABLED_CHANNELS` 在啟動時對照已知頻道名稱集合進行驗證；無法識別的名稱觸發明確的 `ERROR` 日誌條目，使操作員立即看到拼寫錯誤（Issue #30）

## [1.10.12] - 2026-03-12

### 安全性
- WebPortal 現在在設定 `DASHBOARD_PASSWORD` 時強制執行 Basic Auth，防止未認證存取群組列表和訊息注入（Issue #12）

### 修復
- 適應度 `speed_score` 公式現在排除失敗執行（response_ms=0）的平均值，防止損壞的群組被評為「完美速度」（Issue #18）
- SQLite 連接現在在所有寫入操作上受 `threading.Lock` 保護，防止 dashboard/webportal/進化線程並發寫入時出現 `database is locked` 錯誤（Issue #15）
- `task_run_logs` 和 `evolution_runs` 表現在在啟動時清除（30 天保留），以防止無界磁碟增長（Issue #19）

### 新增
- 每個群組的訊息速率限制（滑動視窗：預設 20 msgs/60s，可通過 `RATE_LIMIT_MAX_MSGS` / `RATE_LIMIT_WINDOW_SECS` 設定），防止一個群組使其他群組饑餓（Issue #16）
- `GroupQueue` 反壓：`pending_tasks` 每個群組上限 50 個，`_waiting_groups` 上限 100 個條目 — 超出的任務帶警告丟棄（Issue #14）
- 結構化日誌格式支援：設定 `LOG_FORMAT=json` 以為 Loki/Datadog/CloudWatch 發出換行分隔的 JSON 日誌（需要 `python-json-logger`）（Issue #17）
- 容器映像固定警告：當 `CONTAINER_IMAGE` 使用可變的 `:latest` 標籤時，在啟動時記錄 `WARNING`（Issue #13）
- `db.prune_old_logs(days=30)` 日誌表維護函式

## [1.10.11] - 2026-03-12

### 架構改進
- 新增 `run_id` 關聯 ID 傳入 container input_data，提升多群組除錯能力（Issue #1, #8）
- 修正 outer timeout 硬編碼 300s 改用 `config.CONTAINER_TIMEOUT`，確保設定一致性（Issue #2）
- 修正 IPC 未知 type 靜默忽略，現在記錄 warning 日誌（Issue #3）
- 新增 `GroupQueue.wait_for_active()` 和 `shutdown_sync()`，graceful shutdown 等待執行中的 container（Issue #4）
- 新增訊息去重機制（`_is_duplicate_message` + LRU fingerprint set），防止 webhook 重試造成重複處理（Issue #7）
- 修正 `ipc_watcher._resolve_container_path` 引用未定義 `logger`（應為 `log`）導致 NameError（Issue #10）
- 將 `asyncio.get_event_loop().run_in_executor()` 替換為 `asyncio.to_thread()`，修正 Python 3.10+ DeprecationWarning（Issue #9）

## [1.10.10] - 2026-03-12

### 修復
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

### 修復
- 移除對話歷史訊息 800 字截斷限制，保留完整 context
- 修正 Session 管理：container 現在回傳 newSessionId，DB 正確更新
- 歷史時間窗從硬編碼 2 小時改為可設定（預設 4 小時）
- 歷史訊息上限從 30 則增加至 50 則

### 變更
- history_lookback_hours 可在 group config 中設定（預設 4）

## [1.10.8] - 2026-03-11

### 新增 — 動態容器工具熱替換（Skills 2.0）

解決 DevEngine 生成技能的核心 Docker 限制：新的 Python 工具現在無需重建映像即可安裝到運行中的容器。

#### 架構：`data/dynamic_tools/` 卷掛載
- `host/container_runner.py`：`_build_volume_mounts()` 現在在**每個**容器（主群組和普通群組）中掛載 `{DATA_DIR}/dynamic_tools/` → `/app/dynamic_tools:ro`
- `container/agent-runner/agent.py`：新的 `_load_dynamic_tools()` 函式 — 在啟動時掃描 `/app/dynamic_tools/*.py` 並通過 `importlib.util` 動態導入每個文件；`register_dynamic_tool` 被注入每個模組的命名空間
- 將 `.py` 文件放入 `data/dynamic_tools/`，下次容器執行自動拾取 — 無需 `docker build`

#### 動態工具登錄（`agent.py`）
- `_dynamic_tools: dict` — 全域進程內登錄：`{name → {fn, schema, description}}`
- `register_dynamic_tool(name, description, schema, fn)` — 附加至**所有三個**提供商宣告列表（Gemini `TOOL_DECLARATIONS`、`CLAUDE_TOOL_DECLARATIONS`、`OPENAI_TOOL_DECLARATIONS`）並登錄分發函式
- `_json_schema_to_gemini()` — 在運行時將 JSON Schema 屬性字典轉換為 Gemini `types.Schema`（支援 string、integer、boolean、object、array 類型）
- `_execute_tool_inner()` — 在所有內置工具之後退回至 `_dynamic_tools` 分發

#### 技能引擎：`container_tools:` manifest 欄位
- `skills_engine/types.py`：`SkillManifest` dataclass 增加 `container_tools: list[str]` 欄位（預設 `[]`）
- `skills_engine/manifest.py`：`read_manifest()` 從 YAML 讀取 `container_tools:`
- `skills_engine/apply.py`：在 `adds:` 處理後，將 `container_tools` 文件從 `skill/add/` 複製至 `{DATA_DIR}/dynamic_tools/`（以檔名扁平化）
- `skills_engine/uninstall.py`：在重播前，找到技能目錄，讀取 manifest，從 `dynamic_tools/` 中刪除其 `container_tools` 文件
- `dynamic_tools/.gitkeep` — git 追蹤的目錄佔位符

### 帶 `container_tools:` 的 `manifest.yaml` 範例
```yaml
skill: my-skill
version: "1.0.0"
adds:
  - docs/superpowers/my-skill/SKILL.md
container_tools:
  - dynamic_tools/my_tool.py   # injected at /app/dynamic_tools/my_tool.py
```

### 動態工具文件範例
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

### 更改的文件
- `host/container_runner.py`（`_build_volume_mounts` 中的 dynamic_tools 掛載）
- `container/agent-runner/agent.py`（`_dynamic_tools` 登錄、`register_dynamic_tool`、`_load_dynamic_tools`、`_execute_tool_inner` 後備）
- `skills_engine/types.py`（`SkillManifest` 上的 `container_tools` 欄位）
- `skills_engine/manifest.py`（`container_tools` 反序列化）
- `skills_engine/apply.py`（`container_tools` 複製至 `dynamic_tools/`）
- `skills_engine/uninstall.py`（重播前的 `container_tools` 清理）
- `dynamic_tools/.gitkeep`（新增）

---

## [1.10.7] - 2026-03-11

### 修復
- **Telegram 文件發送優化**：通過移除導致相容性問題的冗餘 `disable_content_type_detection` 參數，改進了 v1.10.1 的二進位文件修復。
- **除錯日誌傳送**：增強錯誤報告，直接向使用者的 Telegram 發送除錯日誌，而非寫入容器內部文件（解決 Docker 中的持久性問題）。
- **文件同步**：確保 `CHANGELOG.md`、`README.md` 和 `RELEASE.md` 與實際代碼變更正確同步。


## [1.10.6] - 2026-03-11

### 修復（代碼審查發現）
- CRASH：.env 影子掛載不再對 `-v` 標誌重複加前綴（容器在 Linux/macOS 上無法啟動）
- ERROR：run_container_agent 現在捕捉 asyncio.CancelledError 並呼叫 _stop_container（外部超時不再創建殭屍容器）
- ERROR：/api/dev/resume 現在將 IPC 文件寫入正確的群組資料夾路徑（DevEngine 恢復之前靜默損壞）
- WARNING：cleanup_orphans 現在在 docker rm 後等待 proc.wait()
- 次要：send_file 工具 schema — chat_jid 從 required[] 中移除（自動從輸入中偵測）
- 次要：_resolve_container_path 防範空的 group_folder
- 次要：TelegramChannel.send_file 移除冗餘的 filename 參數

## [1.10.5] - 2026-03-11

### 新增
- **全面的容器 Agent 日誌**：在 `container/agent-runner/agent.py` 中加入帶毫秒時間戳的 `_log(tag, msg)` 輔助函式，用於 agent 生命週期中的結構化 stderr 日誌。
  - 啟動：在容器啟動時記錄進程 ID（`🚀 START`）。
  - 輸入解析：JID、群組資料夾和訊息數（`📥 INPUT`）。
  - 最後一條訊息預覽，便於快速除錯（`💬 MSG`）。
  - 在第一次 LLM 呼叫前選擇模型/提供商（`🤖 MODEL`）。
  - 每輪 LLM 呼叫和帶停止原因的回應（`🧠 LLM →/←`）。
  - 帶名稱和截斷 args 的工具分發（`🔧 TOOL`）。
  - 工具結果預覽（`🔧 RESULT`）。
  - 訊息、任務和文件的 IPC 文件寫入（`📨 IPC`）。
  - 文件發送路徑和存在檢查（`📎 FILE`）。
  - 發出前的輸出大小（字元數）（`📤 OUTPUT`）。
  - 異常類型和帶完整堆疊追蹤的訊息至 stderr（`❌ ERROR`）。
  - 帶成功標誌的完成（`🏁 DONE`）。
- **嘈雜 SDK 日誌抑制**：`httpx`、`httpcore`、`google` 和 `urllib3` 日誌器鉗制至 WARNING 級別。
- **主機 stderr 提升**：`host/container_runner.py` `_stream_stderr()` 現在將帶 emoji 標記的 agent 日誌行從 DEBUG 提升至 INFO，使其在不使用 `--debug` 的情況下也出現在生產日誌中。

## [1.10.1] - 2026-03-11

### 修復
- **Telegram 頻道**：修復 `send_file()` 中的嚴重 bug，二進位文件（例如 `.pptx`、`.pdf`、`.jpg`）因錯誤的編碼處理（`cp950 codec can't decode` 錯誤）而無法發送。
  - 更改文件讀取為明確使用二進位模式（`rb`）並在發送前讀取整個內容。
  - 現在使用 `telegram.InputFile` 以確保二進位資料正確傳輸。
  - 加入 MIME 類型偵測，後備為 `application/octet-stream`。
  - 設定 `disable_content_type_detection=True` 以防止 Telegram 重新編碼文件。
  - 改進文件發送失敗的錯誤日誌。

## [1.10.0] - 2026-03-10

### 新增
- **進化引擎**：帶有 formality、technical_depth 和 responsiveness 基因的完整基因組進化。
- **健康監控器**：帶自動警報的實時系統健康追蹤。
- **DevEngine**：7 階段自動化開發管線（分析 → 設計 → 實作 → 測試 → 審查 → 文件 → 部署）。
- **Web Dashboard**：帶子 Agent 層次視覺化的 7 頁籤監控介面。
- **Superpowers 整合**：來自 Superpowers 方法論的 12 個工作流程技能套件。

### 變更
- 將 `threading.Lock` 替換為 `asyncio.Lock` 以提升非同步相容性。
- GroupQueue 現在按群組序列化容器執行。
- WebPortal session 超時縮短至 1 小時。

### 修復
- `_stop_container` 現在正確等待 `proc.wait()` 完成。
- `/api/env` 現在使用金鑰白名單以確保安全。
- DevEngine JID 後備現在提供清晰的錯誤訊息。
- macOS 相容性修復，針對 `.env` 文件處理。

## [1.9.0] - 2026-02-15

### 新增
- **免疫系統增強**：22 個注入模式偵測。
- **適應性進化**：基於系統負載和時段的表觀遺傳適應。
- **進化日誌**：`evolution_log` 表中基因組變更的完整歷史。

### 變更
- 改進容器隔離和安全性。
- 增強 dashboard 中的錯誤報告。

## [1.8.0] - 2026-02-01

### 新增
- **技能引擎**：用於新增功能的插件系統。
- **WhatsApp 支援**：用於 WhatsApp 整合的可選技能。
- **多模型支援**：Gemini、OpenAI 相容和 Claude。

### 變更
- 重構頻道架構以提升模組化。

---

## 版本歷史摘要

| 版本 | 日期 | 主要變更 |
|---------|------|-------------|
| 1.10.23 | 2026-03-12 | 路由器失敗通知（#86）、Discord 超時守衛（#87）、WhatsApp LRU wamid 字典（#88）、適應度分數修復（#89）、webportal 死鎖修復（#90）、Telegram 上傳超時環境變數（#91）、路徑穿越守衛（#92）、免疫空 JID 守衛（#93） |
| 1.10.22 | 2026-03-12 | WhatsApp send_typing wamid 修復、send_file deleteAfterSend、多金鑰輪換（#6）、關閉 #5 |
| 1.10.1 | 2026-03-11 | 修復 Telegram 二進位文件發送 bug |
| 1.10.0 | 2026-03-10 | 完整進化引擎、DevEngine、健康監控器 |
| 1.9.0 | 2026-02-15 | 增強免疫系統、適應性進化 |
| 1.8.0 | 2026-02-01 | 技能引擎、WhatsApp 支援 |

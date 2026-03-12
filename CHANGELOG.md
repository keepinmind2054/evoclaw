# Changelog

All notable changes to EvoClaw will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
| 1.10.1 | 2026-03-11 | Fixed Telegram binary file sending bug |
| 1.10.0 | 2026-03-10 | Full evolution engine, DevEngine, Health Monitor |
| 1.9.0 | 2026-02-15 | Enhanced immune system, adaptive evolution |
| 1.8.0 | 2026-02-01 | Skills engine, WhatsApp support |

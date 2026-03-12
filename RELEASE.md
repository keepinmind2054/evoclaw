# Release Process

This document describes the release process for EvoClaw.

## Pre-release Checklist

Before creating a new release, ensure:

- [ ] All new features are documented in `CHANGELOG.md`
- [ ] `README.md` version banner is updated
- [ ] All tests pass (`python -m pytest tests/`)
- [ ] No critical bugs in the issue tracker
- [ ] Version number is updated in relevant files

## Release Steps

### 1. Update Documentation

**Update `CHANGELOG.md`:**
- Add new version section with date
- List all changes under `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`
- Move `[Unreleased]` changes to the new version

**Update `README.md`:**
- Update version banner (e.g., `**v1.10.1**`)
- Ensure feature list is current

### 2. Commit Changes

```bash
git add CHANGELOG.md README.md
git commit -m "chore: prepare release v1.10.1"
```

### 3. Create Git Tag

```bash
# Create annotated tag
git tag -a v1.10.1 -m "Release version 1.10.1"

# Verify tag
git tag -l
git show v1.10.1
```

### 4. Push to Remote

```bash
# Push commits
git push origin main

# Push tag
git push origin v1.10.1
```

### 5. Create GitHub Release

1. Go to [GitHub Releases](https://github.com/KeithKeepGoing/evoclaw/releases)
2. Click "Draft a new release"
3. Select the tag `v1.10.1`
4. Use the following template:

```markdown
## 🎉 What's New

### 🐛 Bug Fixes
- Fixed critical bug in Telegram channel where binary files would fail to send

### 📝 Documentation
- Updated CHANGELOG.md with new format
- Added RELEASE.md for release process

## 📦 Installation

```bash
git clone https://github.com/KeithKeepGoing/evoclaw.git
cd evoclaw
git checkout v1.10.1
python setup/setup.py
```

## 🔗 Links
- [Full Changelog](https://github.com/KeithKeepGoing/evoclaw/blob/main/CHANGELOG.md)
- [Documentation](https://github.com/KeithKeepGoing/evoclaw#readme)
```

5. Click "Publish release"

### 6. Notify Users

- Post in project discussions/announcements
- Update any relevant community channels

## Version Numbering

EvoClaw follows [Semantic Versioning](https://semver.org/):

- **MAJOR.MINOR.PATCH** (e.g., 1.10.1)
- **MAJOR**: Incompatible API changes
- **MINOR**: Backwards-compatible functionality additions
- **PATCH**: Backwards-compatible bug fixes

### Examples

- `1.10.0` → `1.10.1`: Bug fix patch
- `1.10.1` → `1.11.0`: New feature (minor)
- `1.11.0` → `2.0.0`: Breaking change (major)

## Hotfix Process

For critical bugs requiring immediate fix:

1. Create hotfix branch from tag: `git checkout -b hotfix/v1.10.1-fix v1.10.1`
2. Apply fix and commit
3. Update version to `1.10.2`
4. Follow release steps above
5. Merge hotfix back to main

## Release Notes Template

```markdown
## [VERSION] - YYYY-MM-DD

### Added
- New features here

### Changed
- Changes to existing functionality

### Deprecated
- Soon-to-be removed features

### Removed
- Removed features

### Fixed
- Bug fixes

### Security
- Security improvements
```

## Verification

After release, verify:

- [ ] Tag is visible on GitHub
- [ ] Release notes are correct
- [ ] Installation from tag works
- [ ] All features function as expected
- [ ] Documentation is accessible

---

**Last Updated:** 2026-03-12 (v1.10.23)

---

## v1.10.23 Release Notes

### Router Failure Notification, Discord Timeout Guard, WhatsApp Memory Cap, Fitness Score Fix, and More

**Problems Fixed**:

1. *No user feedback when all message chunks fail to deliver* (#86): `router.py` now sends a ⚠️ 回應傳送失敗 notification to the user when every chunk of a multi-part outbound message fails after all retries. Previously the failure was silent from the user's perspective.

2. *Discord channel crashes on slow API responses* (#87): `discord_channel.py` wrapped `future.result(30)` in a try/except block to catch `concurrent.futures.TimeoutError`. Previously a slow or stalled Discord API call would raise an unhandled exception and crash the channel handler.

3. *`_last_wamid` dict grows without bound in WhatsApp channel* (#88): `whatsapp_channel.py` replaced the plain `dict` used for `_last_wamid` with a `collections.OrderedDict` capped at 10,000 entries with LRU eviction. On high-volume deployments receiving thousands of distinct JIDs, the previous dict accumulated entries indefinitely.

4. *`speed_score` incorrectly exceeds 1.0 for fast responses* (#89): `fitness.py` fixed the `speed_score` formula so that response times shorter than the target threshold correctly return 1.0. The previous formula produced values greater than 1.0 for sub-target times, inflating fitness scores for fast groups.

5. *Potential deadlock in WebPortal `store_message` call* (#90): `webportal.py` now releases the sessions lock before calling `db.store_message()`. Previously the lock was held across the DB write, creating a potential deadlock if the DB layer attempted to acquire any lock that interacted with the session lock.

6. *Telegram upload timeout hardcoded at 120s* (#91): `telegram_channel.py` now reads upload timeout from the `TELEGRAM_UPLOAD_TIMEOUT` environment variable (default: 300s). The previous hardcoded 120s was too short for large file uploads on slow connections.

7. *Path traversal guard improvements in DevEngine* (#92): `dev_engine.py` received additional path traversal guard improvements to more robustly reject crafted paths that attempt to escape the intended workspace directory.

8. *Empty `sender_jid` causes incorrect behaviour in immune check* (#93): `immune.py` `check_message()` now guards against empty or None `sender_jid` values. Previously a message with a missing sender JID could trigger incorrect threat attribution or an unhandled exception.

**Upgrade**:

No `docker build` needed — all changes are in the host process. Restart EvoClaw to apply.

```bash
git pull
python run.py start
```

---

## v1.10.22 Release Notes

### WhatsApp Fix, send_file Cleanup Flag, and Multi-Key LLM Rotation

**Problems Fixed / Capabilities Added**:

1. *WhatsApp `send_typing` used wrong message ID* (#66): `send_typing` was passing `chat_id` as the `wamid` parameter in the read-receipt payload. WhatsApp Cloud API requires a per-message `wamid` (e.g. `wamid.xxx...`). Added `_last_wamid: dict[str, str]` to store the latest received message ID per JID; `send_typing` now reads from this dict and skips gracefully when no prior message has been received for that JID.

2. *Temp files left on disk after `send_file`* (#68): The `send_file` IPC handler had no mechanism to clean up temporary files after delivery. Added `deleteAfterSend: true` flag to the IPC payload — when set, the host deletes the file after successful channel delivery. The `research-ppt` skill system prompt updated to instruct the agent to include this flag when sending `.pptx` / `.txt` output files.

3. *Single LLM key causes 429 failures under load* (#6): All four LLM provider key variables (`GOOGLE_API_KEY`, `CLAUDE_API_KEY`, `OPENAI_API_KEY`, `NIM_API_KEY`) now accept comma-separated key lists (e.g. `GOOGLE_API_KEY=key1,key2,key3`). The container agent round-robins to the next key on 429 or quota-exceeded errors and logs `🔑 KEY ROTATE` so operators can observe rotation in the dashboard log stream.

4. *Per-JID timestamp cursors formally closed* (#5): Issue #5 (group-isolation violation via shared timestamp cursor) was resolved in v1.10.17 with the introduction of `_per_jid_cursors`. This release formally closes the issue with no additional code changes.

**Upgrade**:

No `docker build` needed for the WhatsApp and `send_file` fixes — all changes are in the host process. The multi-key rotation change is in `container/agent-runner/agent.py` and requires a container rebuild.

```bash
git pull
docker build -t evoclaw-agent:1.10.22 container/
docker tag evoclaw-agent:1.10.22 evoclaw-agent:latest
python run.py start
```

---

## v1.10.21 Release Notes

### Docker: Production-Ready Image with Full MCP/Tool-Use Support

**Problems Fixed / Capabilities Added**:

1. *Document generation limited to PPT only* (Issue #77): Pre-install `reportlab` (PDF), `openpyxl` (Excel), `python-docx` (Word) in the image. All four document types (PPT/PDF/Excel/Word) are now available without any runtime pip install or network dependency.

2. *No Python web scraping libraries* (Issue #78): Pre-install `httpx`, `beautifulsoup4`, `lxml`. Agents can now scrape and parse HTML/XML pages using lightweight Python libraries, complementing the existing Chromium browser automation.

3. *No image processing support* (Issue #79): Pre-install `Pillow` with required system libs (`libjpeg-dev`, `libpng-dev`, `zlib1g-dev`, `libcairo2`). Agents can now resize, convert, annotate, and embed images in generated documents.

4. *No data science libraries* (Issue #80): Pre-install `pandas`, `numpy`, `matplotlib`. Agents can now analyze tabular data, compute statistics, and generate charts in-container.

5. *Incomplete CJK font stack* (Issue #81): Add `fonts-liberation` and `fonts-noto-color-emoji` alongside existing Noto CJK and WQY fonts. Full Japanese, Korean, and Chinese (simplified + traditional) rendering across all generated document types.

6. *Missing common system utilities* (Issue #82): Add `wget`, `unzip`, `jq`, `ffmpeg`. Standard tools needed by MCP server setup scripts, archive handling, JSON shell processing, and media conversion.

7. *Slim base image blocks native Python extension builds* (Issue #83): Upgrade from `node:22-slim` to `node:22` (full Debian). Add `python3-dev`, `build-essential`, `gcc`. Native extensions (lxml, numpy, Pillow) now compile from source when pre-built wheels are unavailable.

8. *`apt-get install` without `--no-install-recommends`*: Added the flag to the install block to keep the image lean despite the base upgrade.

**Architecture**:

- Base: `node:22` (Node.js 22 LTS, full Debian) — Node.js included for MCP servers
- User: `node` (uid 1000, non-root) — production security
- Python: system Python 3 via apt + pip with `--break-system-packages`
- Infrastructure packages: owned by Dockerfile (document gen, scraping, images, data science)
- Project packages: owned by `requirements.txt` (google-genai, openai, anthropic)

**Upgrade**:

Rebuild the agent container image to pick up all new capabilities:

```bash
git pull
docker build -t evoclaw-agent:1.10.21 container/
docker tag evoclaw-agent:1.10.21 evoclaw-agent:latest
```

---

## v1.10.20 Release Notes

### Docker: Upgrade Container Image with CJK Fonts and Pre-installed python-pptx

**Problems Fixed**:

1. *research_ppt tool fails on network instability* (Issue #75): `python-pptx` was installed at runtime via `pip install` inside the container skill script. A transient PyPI network failure would cause the tool to crash with no output. Moving the install to the Dockerfile eliminates this network dependency entirely — the package is baked into the image.

2. *Chinese characters display as squares in PPT/PDF* (Issue #75): The base image had no CJK (Chinese/Japanese/Korean) font packages. `python-pptx` fell back to a placeholder glyph (square box) for all Chinese characters. Pre-installing `fonts-wqy-zenhei` and `fonts-wqy-microhei` with `fc-cache -fv` ensures correct CJK rendering in generated presentations.

**Changes**:

- Added `fonts-wqy-zenhei`, `fonts-wqy-microhei` to apt install block with `fc-cache -fv` post-install
- Added `libfreetype6`, `libpng16-16`, `zlib1g` system libraries for PPT/PDF rendering
- Added pre-install step: `pip3 install --break-system-packages --no-cache-dir python-pptx==1.0.2`
- Added `ENV PYTHONUNBUFFERED=1` and `ENV LANG=C.UTF-8` for correct output encoding

**Upgrade**:

Rebuild the agent container image to pick up the new fonts and pre-installed package:

```bash
git pull
docker build -t evoclaw-agent:1.10.20 container/
```

---

## v1.10.19 Release Notes

### Eighth Round PUA Analysis — Channel Safety, Task Observability, and Config Completeness

**Problems Fixed**:

1. *Gmail body size unbounded — LLM context window exhaustion* (Issue #69): `_extract_body()` in `host/channels/gmail_channel.py` returned the full decoded email text with no size limit. A large email (newsletter, quoted thread, 500 KB plain text) was stored verbatim in the messages table and injected into the agent prompt, potentially saturating the context window. Added a 32 KB cap with a `[... email truncated at 32 KB ...]` suffix so the agent knows content is partial.

2. *Telegram non-text messages silently dropped with no user feedback* (Issue #70): The `MessageHandler` in `host/channels/telegram_channel.py` filtered on `filters.TEXT` only. Photos, voice messages, videos, documents, stickers, location, and contact messages produced zero bot response. Users had no indication their message was received but unsupported. Added a separate handler for all non-text types that sends a short informational reply.

3. *GroupQueue `asyncio.create_task()` calls have no done-callback* (Issue #71): All six `create_task()` sites in `host/group_queue.py` (`enqueue_message_check`, `enqueue_task`, `_drain_group` x2, `_drain_waiting` x2, and `_schedule_retry`) are now wired to `_task_done_callback`. This logs unhandled exceptions at ERROR level instead of silently discarding them per Python asyncio semantics.

4. *`.env.example` missing security-critical and operational variables* (Issue #72): Added `WHATSAPP_APP_SECRET` with a prominent security warning (operators without this key accept spoofed webhook payloads from any caller), `LOG_FORMAT`, `RATE_LIMIT_MAX_MSGS`, `RATE_LIMIT_WINDOW_SECS`, `DASHBOARD_USER`, `DASHBOARD_PASSWORD`, `WEBPORTAL_ENABLED`, `WEBPORTAL_HOST`, `WEBPORTAL_PORT`, and `HEALTH_PORT`.

5. *IPC `ensure_future()` fire-and-forget swallows exceptions silently* (Issue #73): All five `ensure_future()` dispatch sites in `host/ipc_watcher._handle_ipc()` (apply_skill, uninstall_skill, list_skills, spawn_agent, dev_task) now attach `_ipc_task_done_callback` to log unhandled exceptions at ERROR level.

6. *Discord `disconnect()` deadlocks when `client.close()` awaited on wrong event loop* (Issue #67): The Discord client runs in a background `threading.Thread` with its own event loop. Calling `await self._client.close()` from the main asyncio event loop in `disconnect()` deadlocks. Fixed by scheduling `close()` via `asyncio.run_coroutine_threadsafe()` on the Discord event loop, then joining the thread with a 5-second timeout.

**Upgrade**:

No `docker build` needed — all changes are in the host process. Restart EvoClaw to apply.

```bash
git pull
python run.py start
```

---

## v1.10.17 Release Notes

### Sixth Round PUA-Spirit Exhaustive Analysis Fixes

**Problems Fixed**:

1. *Global timestamp cursor causes silent message loss across groups* (Issue #52): `_last_timestamp` was a single global cursor shared across all registered groups. When group A's messages were successfully processed, the cursor advanced to cover their timestamps. If group B had messages at the same or earlier timestamp range, they would be skipped on the next poll because the global cursor had moved past them. Fixed by introducing `_per_jid_cursors: dict[str, int]` — each group maintains its own cursor and advances it independently. The legacy `lastTimestamp` key in `router_state` is preserved for backward compatibility and read-only reference; per-JID cursors are persisted as `cursorJID:<jid>` keys.

2. *`get_new_messages` and `get_conversation_history` missing `_db_lock`* (Issue #53): These two read functions were the only DB accessors not holding `_db_lock`. Dashboard, webportal, and evolution daemon threads can run concurrently with the asyncio event loop. Without the lock, concurrent access to the shared SQLite connection could cause `SQLITE_LOCKED` or stale reads. Both functions now acquire `_db_lock` for the full query, consistent with all other DB functions.

3. *Task scheduler tight-retry loop on exception* (Issue #54): When `run_task()` raised an exception before reaching the normal `db.update_task()` call (which advances `next_run`), `next_run` remained at its original value in the past. On the next scheduler poll (60 seconds later), the same task appeared due again and was immediately re-enqueued, creating a continuous tight retry loop. The `except` block now calls `db.update_task(task_id, next_run=backoff_next)` with a computed backoff `next_run`, ensuring the task only retries at its normal schedule interval.

4. *`_get_empty_env_file()` race condition on first call* (Issue #55): The function used a global `_EMPTY_ENV_FILE` with a simple `if not None` check and no lock. Two concurrent callers (from different container launches at startup) could both find `_EMPTY_ENV_FILE is None`, each call `tempfile.mkstemp()`, and only one path would be stored — leaving the other temp file orphaned until process exit. Fixed with `_EMPTY_ENV_FILE_LOCK = threading.Lock()` and double-checked locking inside the critical section.

5. *SSE log stream does not exit on graceful shutdown* (Issue #56): `_handle_sse_logs()` ran `while True` with `time.sleep(0.5)`, only exiting when the HTTP client disconnected. When the host received SIGTERM/SIGINT, active SSE connections kept the dashboard thread pool threads alive indefinitely. Added `_dashboard_stopping = threading.Event()` set by `start_dashboard()` when `stop_event` fires (via a background watcher thread). The SSE loop now checks `_dashboard_stopping.is_set()` on each iteration and uses `_dashboard_stopping.wait(timeout=0.5)` instead of `time.sleep(0.5)`.

6. *Subagent result file has no size cap* (Issue #57): `_run_subagent()` wrote the full container agent output (up to 2 MB after the container_runner cap) into a JSON result file with no additional limit. The results directory is never automatically pruned, so many large result files could accumulate and fill the host disk. Result text is now truncated to 1 MB (`_SUBAGENT_RESULT_MAX_BYTES`) with a warning log before writing.

7. *Scheduler skips tasks with empty `chat_jid`* (Issue #48 partial): `start_scheduler_loop()` now checks that `chat_jid` is non-empty before calling `group_queue.enqueue_task()`. Previously, an empty JID would be used as the GroupQueue key, inserting a `""` entry into the `_groups` dict and corrupting the per-group serialization map. Tasks with empty `chat_jid` are now skipped with a warning log entry.

**Upgrade**:

No `docker build` needed — all changes are in the host process. Restart EvoClaw to apply.

```bash
git pull
python run.py start
```

---

## v1.10.16 Release Notes

### Security Hardening + Thread-Safety + Channel Fixes

**Security**:

1. *WhatsApp HMAC-SHA256 webhook validation* (Issue #42): The WhatsApp webhook handler now reads the raw request body before parsing JSON, computes `HMAC-SHA256(WHATSAPP_APP_SECRET, body)`, and compares with the `X-Hub-Signature-256` header using `hmac.compare_digest`. Requests without a valid signature are rejected with HTTP 403. `WHATSAPP_APP_SECRET` is read from `.env` via `read_env_file`. When the secret is not configured the check is skipped for backward compatibility.

2. *WebPortal CSRF token* (Issue #45): `/api/session` now returns a `csrf_token` UUID alongside the `session_id`. All POST requests to `/api/send` must include this token as the `X-CSRF-Token` header. The embedded JavaScript in the portal SPA automatically stores and sends the token. Since custom headers require a CORS preflight (which this server never approves), cross-origin forged requests are blocked.

3. *immune.py MD5 -> SHA-256* (Issue #47): `_hash()` now uses `hashlib.sha256` instead of `hashlib.md5`. SHA-256 is collision-resistant, preventing adversaries from crafting two messages with the same hash to bypass spam counters or poison the threat database.

**Fixed**:

4. *DB read functions missing _db_lock* (Issue #43): 12 read-only DB functions called from background threads (dashboard, webportal, evolution daemon via `asyncio.to_thread`) now hold `_db_lock` for the full query: `get_all_registered_groups`, `get_all_tasks`, `get_evolution_runs`, `get_active_evolution_jids`, `get_recent_run_stats`, `get_group_genome`, `is_sender_blocked`, `get_recent_threat_count`, `get_immune_stats`, `get_evolution_log`, `get_due_tasks`, `get_pending_task_count`, `get_error_stats`.

5. *Discord cross-event-loop calls* (Issue #44): `DiscordChannel.send_message()` and `send_typing()` now use `asyncio.run_coroutine_threadsafe()` to schedule discord.py coroutines onto the Discord client's background event loop, then await via `run_in_executor`. This fixes silent failures / `RuntimeError: no running event loop` when dispatching messages from the main asyncio event loop into the Discord client's separate loop.

6. *Gmail _seen_message_ids unbounded* (Issue #46): Replaced `set[str]` with a `collections.OrderedDict` capped at 10,000 entries. When the cap is reached the oldest (least-recently-seen) entry is evicted. This prevents indefinite memory growth on long-running deployments with high Gmail volume.

7. *Slack auth_test() per-message API call* (Issue #49): `auth_test()` is now called once during `connect()` and the resolved `team_id` is stored as `self._workspace_id`. The `handle_message` event handler reads the cached value instead of firing an API call on every message.

8. *IPC error notification leaks internal paths* (Issue #50): `_notify_main_group_error()` now passes the error string through `_sanitize_error_for_notification()` which replaces absolute filesystem paths with `<path>` and truncates to 120 characters, preventing the internal directory layout from being exposed to chat group members.

---

## v1.10.15 Release Notes

### research-ppt Skill: Self-Healing Architecture + Telegram File Sending Fixes

**Added**:

1. *research-ppt skill* (Issue #39): New installable skill providing a `research_ppt` container tool for generating PowerPoint presentations. Key design principles:
   - Version-pins `python-pptx==1.0.2` — prevents dependency drift on ephemeral Docker containers where each run starts fresh
   - Self-healing installer: retries `pip install` up to 2 times on transient PyPI network failures before giving up
   - Graceful degradation: if PPTX generation fails for any reason (missing package, font error, API change), automatically writes a plain-text `.txt` report instead of crashing
   - Font safety: tries a chain of preferred fonts (including CJK fonts like Microsoft YaHei, Noto Sans CJK) and falls back to Arial silently — no hard crash on minimal Docker images without Chinese font packages
   - Hot-deployed via `container_tools:` in the skill manifest — no image rebuild required

**Fixed**:

2. *`route_file()` no file size guard* (Issue #40): `router.route_file()` now performs two pre-flight checks before passing to the channel:
   - File existence: if the file does not exist on disk, sends a plain-text notification and returns
   - File size: files over 45 MB (safely under Telegram's 50 MB bot limit) trigger a plain-text notification instead of a failed upload attempt

3. *`TelegramChannel.send_file()` memory spike* (Issue #40): The previous implementation called `f.read()` to load the entire file into memory before sending. Replaced with a streaming approach — the open file object `fh` is passed directly to `send_document`, so python-telegram-bot streams the upload without buffering the full binary content in the host process.

4. *`TelegramChannel.send_file()` no upload timeout* (Issue #40): The `send_document` call now runs inside `asyncio.wait_for(..., timeout=120)`. A slow or stalled network can no longer hold a GroupQueue slot indefinitely.

5. *Debug log side-effect removed*: The previous `send_file` implementation wrote every upload attempt to `/workspace/group/debug_send.log`. This file accumulated indefinitely and was not gated on any debug flag. The entire debug-logging block has been removed.

**Upgrade**:

No `docker build` needed for the router and Telegram channel fixes — all changes are in the host process. To enable the `research-ppt` skill, install it via the skills engine:

```bash
git pull
python -m skills_engine apply skills/research-ppt
python run.py start
```

---

## v1.10.14 Release Notes

### Fourth Round Reliability and Edge Case Improvements

**Bug Fixes**:

1. *DB race condition in immune system* (Issue #32): `record_immune_threat()` was performing a read-modify-write without holding `_db_lock`, creating a TOCTOU race with dashboard/webportal threads. Fixed by wrapping the full sequence in `with _db_lock:`.

2. *Five tables growing without bound* (Issue #33): `prune_old_logs()` only cleaned `task_run_logs` and `evolution_runs`. Added pruning for `evolution_log`, `messages`, `immune_threats` (noise entries), `dev_events`, and `dev_sessions` using configurable retention windows.

3. *psutil missing from requirements* (Issue #34): `health_monitor.py` imported `psutil` unconditionally but it was absent from `host/requirements.txt` and `pyproject.toml`, causing `ImportError` on fresh installs. Added `psutil>=5.9.0` to both files.

4. *Health monitor permanently disabled* (Issue #35): `_check_container_queue()` and `_check_error_rate()` called `db.get_pending_task_count()` and `db.get_error_stats()` which did not exist in `db.py`. The `hasattr()` guards silently fell back to zero values, making both checks no-ops. Implemented both functions in `db.py`.

5. *No LLM API retry on transient errors* (Issue #36): All three LLM provider loops (Gemini, Claude, OpenAI-compatible) called the API with no retry logic. A single 429 or 5xx response failed the entire container run. Added `_llm_call_with_retry()` with exponential backoff (3 attempts, 1s/2s delays).

**Added**:

6. *Periodic log pruning* (Issue #37): `prune_old_logs()` was called only at startup. Long-running processes accumulated rows between restarts. The evolution daemon now calls `prune_old_logs()` after each 24-hour cycle.

---

## v1.10.13 Release Notes

### Third Round Security and Reliability Improvements

**Security Fixes**:

1. *Agent file tool path sandbox* (Issue #29): `tool_read`, `tool_write`, and `tool_edit` in the container agent now validate that the resolved path is within `/workspace/` before executing. This blocks prompt-injection or jailbreak attacks that attempt to read sensitive container files like `/proc/self/environ` (which contains env vars) or `/etc/passwd`.

2. *Skills `post_apply` command allowlist* (Issue #28): Skill manifest `post_apply` commands are now checked against an allowlist of safe command prefixes (`pip install`, `npm install`, `pytest`, etc.). Commands not matching the allowlist are skipped with a warning, preventing malicious skill manifests from running arbitrary host OS commands.

3. *IPC path traversal prevention* (Issue #26): `_resolve_container_path()` now resolves and validates the computed host path stays within the expected root directory, blocking container-crafted paths like `/workspace/group/../../etc/passwd` from escaping the intended directory.

**Reliability Fixes**:

4. *WebPortal `_pending_replies` memory leak* (Issue #21): The `_pending_replies` dict was never cleaned up, accumulating entries indefinitely as sessions expired. It is now lazily evicted on every `/api/send` call by removing entries whose session no longer exists.

5. *DB write functions missing `_db_lock`* (Issue #22): Nine DB write functions (`set_session`, `create_task`, `update_task`, `delete_task`, `set_registered_group`, `upsert_group_genome`, `block_sender`, `log_evolution_event`, `log_dev_event`) were called from background threads without holding `_db_lock`. All now acquire the lock, preventing potential `database is locked` errors.

6. *WebPortal bypasses rate limiter* (Issue #25): The WebPortal `/api/send` endpoint wrote messages directly to the database without checking the per-group rate limiter, allowing authenticated WebPortal users to flood the GroupQueue. Rate limiting is now applied consistently.

7. *Partial message delivery on chunked send* (Issue #27): `route_outbound()` silently dropped remaining chunks when one chunk failed to send. It now retries each chunk up to 2 times, and if all retries fail, notifies the user that the response was truncated.

**Added**:

8. *WebPortal session count cap* (Issue #23): `_sessions` is now capped at 500 concurrent sessions. New session creation triggers `_expire_sessions()` immediately. Per-session message lists are capped at 200 entries to bound per-session memory.

9. *WebPortal body size limit* (Issue #24): `_read_body()` now enforces a 64 KB maximum POST body size (HTTP 413 for oversized requests). Individual message text is capped at 32 KB.

10. *ENABLED_CHANNELS startup validation* (Issue #30): Unrecognised channel names in `ENABLED_CHANNELS` now trigger a clear `ERROR` log entry at startup, so operators immediately see typos rather than silently running with no active channels.

**Upgrade**:

Rebuild the agent container image to pick up the file tool path sandbox fix. All other changes are in the host process only.

```bash
git pull
docker build -t evoclaw-agent:1.10.13 container/
python run.py start
```

---

## v1.10.12 Release Notes

### Security, Stability, and Observability Improvements

**Problems Fixed**:

1. *WebPortal auth bypass* (Issue #12): The Web Portal had no authentication. Any host-network user could list all groups and inject messages, bypassing the allowlist and immune system. Basic Auth is now enforced when `DASHBOARD_PASSWORD` is set.

2. *Fitness speed_score wrong for failed runs* (Issue #18): `compute_fitness()` included zero-ms values (from timed-out containers) in the speed average, making fully broken groups appear fast. The formula now excludes zero-ms unsuccessful runs.

3. *SQLite thread-safety* (Issue #15): The shared `_db` connection was written concurrently from the dashboard, webportal, and evolution daemon threads without any locking. All write operations now acquire `_db_lock` (a `threading.Lock`), preventing `database is locked` errors.

4. *Unbounded log table growth* (Issue #19): `task_run_logs` and `evolution_runs` grew forever. A new `prune_old_logs(days=30)` function is called at startup to cap retention.

**Added**:

5. *Per-group rate limiting* (Issue #16): A sliding-window rate limiter (default 20 msgs/60s) in `_on_message()` prevents one talkative group from starving others. Configurable via `RATE_LIMIT_MAX_MSGS` and `RATE_LIMIT_WINDOW_SECS`.

6. *GroupQueue backpressure* (Issue #14): `pending_tasks` is capped at 50 per group and `_waiting_groups` at 100 entries, preventing unbounded memory growth under sustained load.

7. *JSON structured logging* (Issue #17): `LOG_FORMAT=json` enables newline-delimited JSON output compatible with Loki, Datadog, and CloudWatch Logs Insights (requires `python-json-logger`).

8. *Container image pin warning* (Issue #13): A startup warning is emitted when `CONTAINER_IMAGE` uses the mutable `:latest` tag, prompting operators to pin to a versioned tag.

**Upgrade**:

No `docker build` needed — all changes are in the host process. Restart EvoClaw to apply.

```bash
git pull
python run.py start
```

---

**Last Updated:** 2026-03-12 (v1.10.10)

---

## v1.10.10 Release Notes

### Stability Improvements

**Problems Fixed**:
1. Container JSON output had no size limit — a misbehaving agent could send megabytes of output, causing memory pressure (DoS vector). Now capped at 2MB.
2. `_group_fail_counts` and `_group_fail_timestamps` were accessed from async coroutines without any lock, creating a race condition when multiple groups processed messages concurrently.
3. The global SQLite connection `_db` was never explicitly closed on process exit, leaving file locks that could block subsequent starts.
4. `_stream_stderr()` called `proc.stderr.readline()` with no timeout — a container that stopped writing to stderr but kept running would hang the stream reader indefinitely.
5. Secret key validation was missing — if `GOOGLE_API_KEY` and all other LLM keys were absent, the container would start and fail only inside Docker with an unhelpful error.
6. The `folder` parameter in `set_registered_group()` was not validated, allowing path traversal characters (`..`, `/`, `\`) that could escape the groups directory.
7. `_cleanup_orphan_tasks()` only removed tasks with empty `chat_jid`, but tasks belonging to groups that were later deregistered were left behind indefinitely.

**Changes**:
- Added 2MB output size guard before `json.loads()` in `container_runner.py`.
- Added `_group_fail_lock = asyncio.Lock()` initialized in `main()` and wrapped all reads/writes to `_group_fail_counts` / `_group_fail_timestamps`.
- Added `atexit.register(_close_connections)` to `host/db.py` to close `_db` on shutdown.
- Added `asyncio.wait_for(..., timeout=30.0)` to `proc.stderr.readline()` in `_stream_stderr()`.
- Added `_validate_secrets()` helper called after `_read_secrets()` to warn on missing LLM keys.
- Added `_validate_folder()` with regex guard called at the top of `set_registered_group()`.
- Extended `_cleanup_orphan_tasks()` to also delete tasks whose `chat_jid` is not in the registered groups set.

#### Upgrade

No `docker build` needed — all changes are in the host process. Restart EvoClaw to apply.

```bash
git pull
python run.py start
```

---

## v1.10.9 Release Notes

### Memory & Session Improvements

**Problems Fixed**:
1. Conversation history messages were truncated to 800 characters, losing context mid-sentence.
2. The history lookback window was hardcoded at 2 hours, which was too short for many use cases.
3. The session table in the database was never updated because the container returned a timestamp-based `newSessionId` (not a proper UUID), and the session was not reliably tracked.

**Changes**:
- Removed the 800-character truncation from conversation history messages — full content is now preserved.
- `history_lookback_hours` is now configurable per group config (default: 4 hours, was hardcoded 2 hours).
- History message limit increased from 30 to 50.
- `newSessionId` in the container output now uses `uuid.uuid4()` for a proper unique session identifier, ensuring `db.set_session()` is called correctly on every run.

#### Upgrade

No `docker build` needed for the `newSessionId` fix — restart EvoClaw and the session table will begin updating correctly.

```bash
git pull
python run.py start
```

---

## v1.10.8 Release Notes

### 🔌 Dynamic Container Tool Hot-swap (Skills 2.0)

**Problem**: DevEngine-generated skills could add new Python tools, but Docker containers are pre-built images — new tool files couldn't be loaded at runtime without `docker build`.

**Solution**: A `data/dynamic_tools/` directory is now mounted read-only into every container at `/app/dynamic_tools`. The agent auto-imports all `*.py` files from this directory at startup, giving installed skills a way to add new callable tools without touching the image.

#### How it works

```
Host: {DATA_DIR}/dynamic_tools/my_tool.py
       │  (mounted via docker run -v)
       ▼
Container: /app/dynamic_tools/my_tool.py
       │  (_load_dynamic_tools() → importlib.util.exec_module)
       ▼
Tool registry: register_dynamic_tool("my_tool", ...) → available to LLM
```

#### Skills manifest addition

```yaml
skill: my-data-skill
version: "1.0.0"
core_version: "1.10.8"
adds:
  - docs/superpowers/my-data-skill/SKILL.md
container_tools:
  - dynamic_tools/my_tool.py     # hot-loaded, no image rebuild
modifies: []
```

#### Upgrade

No `docker build` needed — the change is in the host runner and agent startup logic only. Restart EvoClaw and the feature is live.

```bash
git pull
python run.py start
```

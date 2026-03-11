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

**Last Updated:** 2026-03-12 (v1.10.12)

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

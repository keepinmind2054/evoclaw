# EvoClaw Self-Update and Restart

Last updated: 2026-05-19

This document captures the current self-update and restart design for EvoClaw. It consolidates the intended behavior across `host/auto_update.py`, `host/ipc_watcher.py`, `host/main.py`, related slash commands, and the supporting changelog / issue history.

## 1. Restart policy: `os.execv`, not `pm2 restart`

The canonical in-process restart path is `os.execv(...)` from `host/main.py`.

Why:

- `os.execv` keeps the same supervised process slot under pm2 instead of creating a separate operator-driven restart flow.
- It avoids depending on pm2 CLI availability or PATH correctness from inside EvoClaw.
- It preserves pm2 as the crash safety net via `autorestart: true`, while keeping the normal restart path inside the host process lifecycle.

`pm2 restart evoclaw` is still valid for manual operator use, but it is not the primary programmatic restart mechanism.

Relevant history:

- Issue `#530`: self-update architecture and restart policy
- Issue `#573`: `restart_host` tool and restart flag flow

## 2. Update entry points

EvoClaw can request update / restart from multiple sources:

| Entry point | Purpose | Gate |
|---|---|---|
| `/update` slash command | Owner-triggered interactive update | `OWNER_IDS` |
| `auto_update_loop` | Scheduled `git fetch` + update check | `AUTO_UPDATE_ENABLED=true` |
| `mcp__evoclaw__self_update` IPC | Agent-triggered self-update | `SELF_UPDATE_TOKEN` |
| `mcp__evoclaw__restart_host` IPC | Agent-triggered restart or `/restart` backend | owner / trusted gate |

Operationally:

- `/update` is the preferred human-operated path.
- Legacy token-gated IPC self-update still exists for compatibility.
- `/restart` and `restart_host` are restart-only; they do not implicitly update code.

Relevant history:

- Issue `#577`: `/update` and `/restart` slash commands
- Issue `#584`: legacy self-update flow bug / loop risk

## 3. Worktree-based update flow

Self-update should run through a detached git worktree instead of mutating the main checkout first.

Primary implementation area:

- `host/ipc_watcher.py:_run_self_update`
- `host/ipc_watcher.py:_run_self_update_worktree`

Expected flow:

1. `git worktree prune`
2. Clean up stale temporary worktree state if needed
3. `git fetch origin <AUTO_UPDATE_BRANCH>`
4. Check whether `FETCH_HEAD` is ahead of local `HEAD`
5. `git worktree add --detach <WORKTREE_DIR> FETCH_HEAD`
6. Run `AUTO_UPDATE_TEST_CMD` inside the worktree
7. If tests pass:
   - `git merge --ff-only FETCH_HEAD`
   - write `<DATA_DIR>/self_update.flag`
   - optionally write `<DATA_DIR>/restart_notify.json`
   - let the main loop perform `os.execv`
8. If tests fail:
   - remove the temporary worktree
   - do not mutate the main checkout
   - optionally enter AI patch flow if enabled

Why worktree mode exists:

- It prevents failed update attempts from polluting the live checkout.
- It avoids risky in-place rollback patterns like `git pull` followed by `reset --hard`.
- It gives update testing an isolated, disposable workspace.

Relevant history:

- Issue `#569`: worktree-based safe update flow

## 4. AI auto-fix flow

If the worktree test gate fails and `AUTO_UPDATE_AI_FIX_ENABLED=true`, EvoClaw may attempt an AI-generated patch flow.

Expected behavior:

1. Detect failed test output in the temporary worktree
2. Ask the configured LLM for a unified diff patch
3. Apply the patch inside the worktree
4. Re-run the test command
5. If the patch succeeds:
   - either require human approval, or
   - fast-forward merge directly if policy allows
6. If retries are exhausted:
   - discard the worktree
   - leave the live checkout untouched

Guardrails:

- Limit writable scope to intended paths such as `host/`, `container/agent-runner/`, `scripts/`, and `docs/`
- Keep retry count bounded with `AUTO_UPDATE_AI_FIX_MAX_RETRIES`
- Optionally require human approval with `AUTO_UPDATE_AI_FIX_REQUIRE_HUMAN_APPROVE=true`
- **Static Code Security Validation**: Automatically inspects AI-generated unified diffs. Rejects any changes introducing networking (`socket`, `requests`, `httpx`), subprocess calls (`subprocess`, `os.system` with `shell=True`), modifications to sensitive core tools (`_tools.py`), or bypass attempts of SSRF/authorization logic.
- **Force Termination & Lockout**: If unsafe code injection is detected, the self-update flow is immediately aborted, changes are discarded, and a critical security alert (`🚨 CRITICAL SECURITY ALERT`) is sent to the originating channel, bypassing any automatic merge policy (`AUTO_UPDATE_AI_FIX_REQUIRE_HUMAN_APPROVE=false` is ignored) for safety.

Primary implementation area:

- `host/self_update_ai_fix.py`

Relevant history:

- Issue `#570`: AI-assisted fix flow after failed update tests

## 5. SQLite state keys and runtime fallback flags

EvoClaw primarily utilizes SQLite database states (via the `router_state` table) to communicate control signals across loops. This avoids `PermissionError` issues on Windows caused by file locking when updating `.flag` files. File-system flags are still written and cleared as a fallback and for manual operations.

| SQLite State Key | File-System Fallback | Meaning | Writer |
|---|---|---|---|
| `control:self_update` | `self_update.flag` | New code has been merged; host should restart into it | self-update flow |
| `control:restart` | `restart.flag` | Restart requested without code update | restart IPC / TG channel flow |
| `control:refresh_groups` | `refresh_groups.flag` | Request host to reload registered groups list dynamically | IPC / Web Portal |
| `control:reset_group` | `reset_group.flag` | Clear failure tracking metrics for a specific group JID | IPC watcher |
| N/A | `restart_notify.json` | Best-effort post-restart notification metadata (JSON) | self-update / restart flow |

Design notes:

- `host/main.py` polls both the SQLite `router_state` table and the file-system `.flag` files in its main loop.
- Once a restart signal is detected, the state is reset, the file is unlinked, and the loop transitions to the `os.execv` graceful restart flow.
- `restart_notify.json` includes JID, source label, and start timestamp for sending a post-restart confirmation message.

Relevant history:

- Issue `#579`: post-restart notify back to originating chat

## 6. `/update` vs legacy self-update IPC

Preferred path:

- `/update` slash command

Why:

- It is operator-driven
- It uses explicit owner gating
- It avoids prompting an agent to decide whether it should update itself
- It is less likely to feed update logic back into the agent loop

Legacy path:

- `mcp__evoclaw__self_update`

Why it still exists:

- Backward compatibility
- Some older flows still reference token-gated self-update behavior

Risk called out in prior work:

- Legacy self-update can interact badly with agent-loop behavior if the agent keeps re-enqueueing update-oriented actions
- This is part of the motivation for keeping `/update` as the primary operator path

Relevant history:

- Issue `#584`: legacy self-update behavior and loop hazard

## 7. Manual operator flows

For plain operator maintenance, use simple explicit commands.

Host-only changes:

Files such as:

- `host/*.py`
- `scripts/*`
- `docs/*`
- `.env.example`
- `.github/*`

Commands:

```bash
git pull
pm2 restart evoclaw
```

If agent image code changed:

Files such as:

- `container/agent-runner/*`
- `container/Dockerfile`

Commands:

```bash
git pull
docker build -t evoclaw-agent:latest container/
pm2 restart evoclaw
```

This split matters because host self-update only changes the host process. Container runtime code is baked into `evoclaw-agent:latest` and does not change until the image is rebuilt.

## 8. Self-update does not automatically rebuild the agent image

Important rule:

- Updating the repo and restarting the host does not by itself refresh code under `container/`

Implication:

- If the diff touches `container/agent-runner/` or `container/Dockerfile`, operators must rebuild `evoclaw-agent:latest`

Recommended changelog convention:

- Mark releases with `Image rebuild required: Yes` whenever `container/` changes are part of the rollout

Future improvement ideas:

- Detect whether the update diff includes `container/`
- Surface a stronger operator warning
- Optionally include image-build verification in the worktree gate

## 9. Config reference

Common `.env` flags related to self-update:

| Variable | Purpose | Typical default |
|---|---|---|
| `AUTO_UPDATE_ENABLED` | Enable scheduled update checks | `false` |
| `AUTO_UPDATE_INTERVAL_SECS` | Polling interval for update checks | `60` |
| `AUTO_UPDATE_BRANCH` | Branch to track for updates | `main` |
| `AUTO_UPDATE_TEST_CMD` | Test gate command run in worktree | `pytest -x --timeout=60 -q tests/` |
| `AUTO_UPDATE_USE_WORKTREE` | Use isolated worktree flow | `true` |
| `AUTO_UPDATE_WORKTREE_DIR` | Temporary worktree location | `$DATA_DIR/auto_update_worktree` |
| `AUTO_UPDATE_AI_FIX_ENABLED` | Enable AI patch flow after failed tests | `false` |
| `AUTO_UPDATE_AI_FIX_REQUIRE_HUMAN_APPROVE` | Require manual approval before merge | `true` |
| `AUTO_UPDATE_AI_FIX_MAX_RETRIES` | Max AI repair attempts | `3` |
| `SELF_UPDATE_TOKEN` | Legacy token for self-update IPC | unset unless needed |

## 10. Related issues and implementation map

Key issues:

| Issue | Summary |
|---|---|
| `#530` | self-update architecture, auto-update loop, restart policy |
| `#569` | safe worktree-based update flow |
| `#570` | AI-assisted fix flow |
| `#573` | restart-host tool and restart flag handling |
| `#575` | auto-update fetch stderr / reliability |
| `#577` | `/update` and `/restart` slash commands |
| `#579` | post-restart notification |
| `#584` | legacy self-update loop / failure mode |

Main code locations:

| File | Responsibility |
|---|---|
| `host/auto_update.py` | scheduled update check loop |
| `host/ipc_watcher.py` | self-update execution, worktree flow, restart notify write |
| `host/main.py` | main loop flag handling and `os.execv` restart path |
| `host/self_update_ai_fix.py` | AI patch generation / retry flow |
| `host/channels/telegram_channel.py` | `/update` and `/restart` handlers |

## 11. Current operational stance

Current preferred model:

- Human-triggered updates should usually go through `/update`
- Automatic updates should use worktree gating
- Restart should be performed by `os.execv` from inside the host process
- Container changes require explicit `docker build`
- pm2 remains the supervisor and crash recovery layer, not the primary programmatic restart primitive

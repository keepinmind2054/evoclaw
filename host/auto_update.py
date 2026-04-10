"""Scheduled auto-update loop (Issue #530).

Periodically runs ``git fetch`` against the project repo and, when ``HEAD`` is
behind ``origin/<branch>``, invokes :func:`host.ipc_watcher._run_self_update`
to pull, test-gate, and (on success) write ``self_update.flag`` so the host
main loop performs an in-place restart via ``os.execv``.

Design notes
------------
* This path **bypasses the SELF_UPDATE_TOKEN IPC gate** intentionally.  The
  token exists to block prompt-injection attacks that flow from an LLM agent
  into the IPC handler; the scheduled loop runs in trusted host code and has
  no attacker-controlled input, so requiring a token here would add no
  security but would turn auto-update into a manual ritual.
* Enabled only when ``AUTO_UPDATE_ENABLED=true``.  Default is disabled so
  existing deployments see zero behaviour change on upgrade.
* Uses the **existing** ``_run_self_update`` implementation for the actual
  work — test gate, pip install, flag write, restart — so there is exactly
  one code path that mutates the working tree.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

from . import config

log = logging.getLogger(__name__)


async def _noop_route(_jid: str, _text: str) -> None:  # pragma: no cover - trivial
    """Route fn used when the auto-update loop has no chat to notify."""
    return None


async def _git_is_behind(cwd: str, branch: str) -> bool:
    """Return True iff local HEAD is strictly behind ``origin/<branch>``.

    Runs ``git fetch`` first; on any fetch/rev-list failure returns ``False``
    (fail-safe: a transient network glitch should not trigger rollback churn).
    """
    # git fetch origin <branch>
    fetch_proc = await asyncio.create_subprocess_exec(
        "git", "fetch", "origin", branch,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    try:
        await asyncio.wait_for(fetch_proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:
        try:
            fetch_proc.kill()
        except Exception:
            pass
        log.warning("auto_update: git fetch timed out")
        return False
    if fetch_proc.returncode != 0:
        log.warning("auto_update: git fetch exit=%s", fetch_proc.returncode)
        return False

    # git rev-list --count HEAD..origin/<branch>
    rl_proc = await asyncio.create_subprocess_exec(
        "git", "rev-list", "--count", f"HEAD..origin/{branch}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        rl_out, _ = await asyncio.wait_for(rl_proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        try:
            rl_proc.kill()
        except Exception:
            pass
        return False
    if rl_proc.returncode != 0:
        return False
    try:
        count = int(rl_out.decode("utf-8", errors="replace").strip() or "0")
    except ValueError:
        return False
    return count > 0


async def auto_update_loop(stop_event: asyncio.Event) -> None:
    """Main entry point, called from ``host.main``.

    Runs forever until ``stop_event`` is set.  Yields control to ``asyncio``
    between iterations so cancellation on shutdown is near-instant.
    """
    if not config.AUTO_UPDATE_ENABLED:
        log.info("auto_update: disabled (AUTO_UPDATE_ENABLED != true)")
        return

    # Import lazily to avoid circular import with ipc_watcher at module load.
    from .ipc_watcher import _run_self_update

    interval = max(60, int(config.AUTO_UPDATE_INTERVAL_SECS))
    branch = config.AUTO_UPDATE_BRANCH or "main"
    cwd = str(config.BASE_DIR)
    log.info(
        "auto_update: enabled — interval=%ss branch=%s cwd=%s",
        interval, branch, cwd,
    )

    # First check happens after one full interval, not immediately.  Restarts
    # triggered by config reloads shouldn't cause a stampede of `git fetch`.
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return  # stop_event fired — graceful exit
            except asyncio.TimeoutError:
                pass  # interval elapsed — run one check

            try:
                behind = await _git_is_behind(cwd, branch)
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("auto_update: _git_is_behind raised: %s", exc)
                continue
            if not behind:
                log.debug("auto_update: up to date")
                continue

            log.info("auto_update: behind origin/%s — triggering self_update", branch)
            try:
                # jid="" → no user-facing chat output; this is a background
                # trigger.  _run_self_update handles the test gate + flag.
                await _run_self_update("", _noop_route)
            except Exception as exc:
                log.error("auto_update: _run_self_update raised: %s", exc)
    except asyncio.CancelledError:
        log.info("auto_update: cancelled")
        raise

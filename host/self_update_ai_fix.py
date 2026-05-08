"""AI auto-patch for self-update test failures (Issue #570).

Wired into `_run_self_update_worktree` (#569).  When pytest fails inside
the worktree sandbox, spawn an LLM with the failure output, ask it to
emit a unified diff that fixes the problem, apply it in the worktree,
re-run tests, repeat up to AUTO_UPDATE_AI_FIX_MAX_RETRIES.

If tests eventually pass, default behavior is **open a PR for human
review** (NEVER auto-merge AI-touched code unless
AUTO_UPDATE_AI_FIX_REQUIRE_HUMAN_APPROVE=false).

Safety constraints:
  * Test files (tests/) are hash-protected — if the AI modifies them,
    the patch is reverted and the attempt counted as failure.
  * Hard cap on retries.
  * Per-attempt audit trail (diff + pytest output) included in the PR
    body so reviewers can see what the AI did.
  * Patches that touch .github/workflows/, .env, or any file outside
    a small allowlist are rejected.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess as _subprocess
import sys as _sys
from pathlib import Path
from typing import Optional

from . import config


log = logging.getLogger(__name__)
_NO_WINDOW = _subprocess.CREATE_NO_WINDOW if _sys.platform == "win32" else 0


# ── Path allowlist ────────────────────────────────────────────────────────────
# AI patches may touch only files under these prefixes.  Everything else
# (tests, CI config, secrets, generated artefacts) is rejected outright.
_PATCH_ALLOW_PREFIXES = (
    "host/",
    "container/agent-runner/",
    "scripts/",
    "docs/",
)
# Tests are protected: the AI must NEVER modify them.  Counted separately
# from the allowlist so we can emit a clear "modified tests — rejected" log.
_PATCH_DENY_PREFIXES = (
    "tests/",
    ".github/",
    ".env",
    ".env.example",
)


# ── Config helpers ────────────────────────────────────────────────────────────
def _cfg(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def is_enabled() -> bool:
    return _cfg("AUTO_UPDATE_AI_FIX_ENABLED", "false").lower() == "true"


def _max_retries() -> int:
    try:
        return max(1, int(_cfg("AUTO_UPDATE_AI_FIX_MAX_RETRIES", "3")))
    except ValueError:
        return 3


def _require_human_approve() -> bool:
    return _cfg("AUTO_UPDATE_AI_FIX_REQUIRE_HUMAN_APPROVE", "true").lower() == "true"


def _provider() -> str:
    return _cfg("AUTO_UPDATE_AI_FIX_PROVIDER", "openai-compat").lower()


def _model() -> str:
    # Prefer a strong tool-using model; default = same NIM nemotron used by
    # main agent.  Operator can override to claude-3-5-sonnet etc.
    return _cfg("AUTO_UPDATE_AI_FIX_MODEL", "") or _cfg("NIM_MODEL", "nvidia/nemotron-3-super-120b-a12b")


def _api_key() -> str:
    # Reuse the main-backend key by default.
    p = _provider()
    if p in ("openai-compat", "nim", "openai"):
        return _cfg("AUTO_UPDATE_AI_FIX_API_KEY") or _cfg("NIM_API_KEY") or _cfg("OPENAI_API_KEY")
    if p == "claude":
        return _cfg("AUTO_UPDATE_AI_FIX_API_KEY") or _cfg("CLAUDE_API_KEY") or _cfg("ANTHROPIC_API_KEY")
    if p == "gemini":
        return _cfg("AUTO_UPDATE_AI_FIX_API_KEY") or _cfg("GOOGLE_API_KEY")
    return ""


def _base_url() -> str:
    return _cfg("AUTO_UPDATE_AI_FIX_BASE_URL") or _cfg("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")


# ── Hash protection for tests/ ────────────────────────────────────────────────
def _hash_dir(root: Path, subdir: str) -> dict[str, str]:
    """Return {relative_path: sha256} for every file under root/subdir."""
    out: dict[str, str] = {}
    base = root / subdir
    if not base.exists():
        return out
    for p in base.rglob("*"):
        if p.is_file():
            try:
                rel = p.relative_to(root).as_posix()
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            except Exception:
                continue
    return out


def _tests_unchanged(worktree: Path, before: dict[str, str]) -> bool:
    after = _hash_dir(worktree, "tests")
    return before == after


# ── LLM call (provider-agnostic, lazy SDK imports) ────────────────────────────
async def _llm_call(system: str, user: str, *, max_tokens: int = 4096, timeout_s: float = 120.0) -> Optional[str]:
    provider = _provider()
    model = _model()
    api_key = _api_key()
    if not api_key:
        log.error("ai_fix: no API key for provider=%s", provider)
        return None

    def _sync_call() -> Optional[str]:
        try:
            if provider in ("openai-compat", "openai", "nim"):
                from openai import OpenAI
                import httpx as _httpx
                client = OpenAI(
                    base_url=_base_url(),
                    api_key=api_key,
                    timeout=_httpx.Timeout(connect=15.0, read=timeout_s, write=15.0, pool=10.0),
                )
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.1,
                    max_tokens=max_tokens,
                )
                return (resp.choices[0].message.content or "").strip()
            if provider == "claude":
                import anthropic
                client = anthropic.Anthropic(api_key=api_key, timeout=timeout_s)
                resp = client.messages.create(
                    model=model, system=system,
                    messages=[{"role": "user", "content": user}],
                    max_tokens=max_tokens, temperature=0.1,
                )
                parts = []
                for block in resp.content:
                    t = getattr(block, "text", "")
                    if t:
                        parts.append(t)
                return "".join(parts).strip()
            if provider == "gemini":
                from google import genai
                from google.genai import types as _gt
                client = genai.Client(api_key=api_key)
                cfg = _gt.GenerateContentConfig(
                    system_instruction=system, temperature=0.1, max_output_tokens=max_tokens,
                )
                resp = client.models.generate_content(model=model, contents=user, config=cfg)
                return (resp.text or "").strip()
        except Exception as exc:
            log.error("ai_fix: LLM call failed (%s/%s): %s", provider, model, exc)
            return None
        log.error("ai_fix: unknown provider %r", provider)
        return None

    return await asyncio.to_thread(_sync_call)


# ── Diff extraction & validation ──────────────────────────────────────────────
_DIFF_FENCE_RE = re.compile(r"```(?:diff|patch)?\n(.+?)```", re.DOTALL)


def _extract_diff(text: str) -> Optional[str]:
    """Pull a unified diff out of the LLM response.

    Accepts either a fenced code block (```diff ... ```) or raw `diff --git`
    starting at column 0.
    """
    if not text:
        return None
    m = _DIFF_FENCE_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        if "diff --git" in candidate or candidate.startswith("--- "):
            return candidate
    # Raw diff at top level
    if "diff --git" in text:
        idx = text.index("diff --git")
        return text[idx:].strip()
    return None


def _diff_touches_only_allowlist(diff_text: str) -> tuple[bool, list[str]]:
    """Returns (allowed, offending_paths)."""
    paths = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            # `diff --git a/path b/path`
            parts = line.split()
            if len(parts) >= 4:
                a = parts[2][2:] if parts[2].startswith("a/") else parts[2]
                b = parts[3][2:] if parts[3].startswith("b/") else parts[3]
                paths.add(a)
                paths.add(b)
        elif line.startswith("+++ b/") or line.startswith("--- a/"):
            paths.add(line[6:].strip())
    bad = []
    for p in paths:
        if any(p.startswith(d) for d in _PATCH_DENY_PREFIXES):
            bad.append(p)
            continue
        if not any(p.startswith(a) for a in _PATCH_ALLOW_PREFIXES):
            bad.append(p)
    return (len(bad) == 0, bad)


# ── git apply / pytest in worktree ────────────────────────────────────────────
async def _git_apply(worktree: Path, diff_text: str) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "apply", "--whitespace=nowarn", "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(worktree),
        creationflags=_NO_WINDOW,
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(diff_text.encode("utf-8")), timeout=30.0
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return False, "git apply timed out"
    if proc.returncode != 0:
        return False, out.decode("utf-8", errors="replace")[:1000]
    return True, ""


async def _git_revert_changes(worktree: Path) -> None:
    """Revert ALL uncommitted changes in the worktree (use after a bad patch)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", "--", ".",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(worktree),
            creationflags=_NO_WINDOW,
        )
        await asyncio.wait_for(proc.wait(), timeout=15.0)
    except Exception:
        pass


async def _run_pytest(worktree: Path, cmd_raw: str, timeout_s: float = 600.0) -> tuple[int, str]:
    import shlex as _shlex
    argv = _shlex.split(cmd_raw)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(worktree),
        creationflags=_NO_WINDOW,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        return proc.returncode or 0, out.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "(pytest timed out)"


# ── PR / issue creation via gh CLI ────────────────────────────────────────────
async def _gh(*args: str, cwd: Optional[str] = None, input_text: Optional[str] = None, timeout_s: float = 30.0) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        creationflags=_NO_WINDOW,
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(input_text.encode("utf-8") if input_text else None),
            timeout=timeout_s,
        )
        return proc.returncode or 0, out.decode("utf-8", errors="replace")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, "(gh timed out)"


async def _push_branch_and_open_pr(worktree: Path, attempts: list[dict]) -> Optional[str]:
    """Create a branch from worktree HEAD, push, and open a PR.

    Returns the PR URL or None on failure.
    """
    import time as _time
    branch = f"ai-fix/auto-{int(_time.time())}"
    # Inside worktree: create branch, commit, push.
    for argv in (
        ["git", "checkout", "-b", branch],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=ai-fix@evoclaw", "-c", "user.name=evoclaw-ai-fix",
         "commit", "-m", f"ai-fix: auto-patch attempt {len(attempts)} (#570)"],
        ["git", "push", "-u", "origin", branch],
    ):
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(worktree),
            creationflags=_NO_WINDOW,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode != 0:
            log.error("ai_fix: %s failed rc=%s: %s", argv[1], proc.returncode, out[:500])
            return None

    body_lines = [
        "**This PR was generated by `evoclaw self_update_ai_fix` (#570).**",
        "",
        f"After self-update tests failed in the worktree sandbox, the AI made "
        f"{len(attempts)} attempt(s) to patch the code. The final attempt passed all tests.",
        "",
        "**Audit trail (each attempt):**",
        "",
    ]
    for i, a in enumerate(attempts, 1):
        body_lines += [
            f"### Attempt {i} — {'PASS' if a.get('passed') else 'FAIL'}",
            "",
            "<details><summary>diff</summary>",
            "",
            "```diff",
            a.get("diff", "(no diff)")[:4000],
            "```",
            "",
            "</details>",
            "",
            "<details><summary>pytest output (tail 1500 chars)</summary>",
            "",
            "```",
            (a.get("pytest_out", "") or "")[-1500:],
            "```",
            "",
            "</details>",
            "",
        ]

    rc, out = await _gh(
        "pr", "create", "--base", "main", "--head", branch,
        "--title", f"ai-fix: self-update auto-patch (attempt {len(attempts)} of {_max_retries()})",
        "--body-file", "-",
        cwd=str(worktree), input_text="\n".join(body_lines),
    )
    if rc != 0:
        log.error("ai_fix: gh pr create failed rc=%s: %s", rc, out[:500])
        return None
    # gh prints PR URL on stdout
    for line in out.splitlines():
        if line.startswith("https://"):
            return line.strip()
    return None


async def _open_failure_issue(attempts: list[dict], pytest_output: str) -> Optional[str]:
    body_lines = [
        f"AI auto-patch failed after {len(attempts)} attempt(s).",
        "",
        "**Initial pytest output:**",
        "",
        "```",
        pytest_output[-2000:],
        "```",
        "",
        "**Attempts:**",
        "",
    ]
    for i, a in enumerate(attempts, 1):
        body_lines += [
            f"### Attempt {i} — {'PASS' if a.get('passed') else 'FAIL'} {a.get('reject_reason', '')}",
            "",
            "<details><summary>diff</summary>",
            "",
            "```diff",
            (a.get("diff", "(no diff)") or "")[:3000],
            "```",
            "",
            "</details>",
            "",
        ]
    rc, out = await _gh(
        "issue", "create",
        "--title", f"self_update AI auto-patch failed after {len(attempts)} attempts",
        "--label", "self-update,ai-fix",
        "--body-file", "-",
        input_text="\n".join(body_lines),
    )
    if rc != 0:
        log.error("ai_fix: gh issue create failed rc=%s: %s", rc, out[:500])
        return None
    for line in out.splitlines():
        if line.startswith("https://"):
            return line.strip()
    return None


# ── Public entry: try N attempts, return outcome ──────────────────────────────
class FixResult:
    __slots__ = ("status", "pr_url", "issue_url", "attempts")
    def __init__(self):
        self.status: str = "untried"  # "passed_pr_opened" | "passed_merged_inline" | "failed_issue_opened" | "disabled" | "no_api_key"
        self.pr_url: Optional[str] = None
        self.issue_url: Optional[str] = None
        self.attempts: list[dict] = []


_AI_SYSTEM_PROMPT = """You are a code-fix assistant for the EvoClaw repository.
Tests failed after the most recent commit pulled from origin/main.

Your job:
1. Read the pytest output below.
2. Identify the smallest possible code change that will make the tests pass.
3. Output ONLY a unified diff in `git apply` format.

Rules:
- Output a single ```diff fenced block — nothing else, no commentary.
- NEVER modify any file under `tests/`. The tests are the spec; make the
  code conform to them, not the other way around.
- NEVER touch `.github/`, `.env`, `.env.example`, or workflow files.
- Patches may touch ONLY files under: host/, container/agent-runner/,
  scripts/, docs/.
- Keep the patch minimal. No unrelated cleanup, no formatting changes.
- If you cannot determine a fix from the output, output `# NO FIX` and nothing else.
"""


async def attempt_fixes(worktree: Path, initial_pytest_output: str, test_cmd: str) -> FixResult:
    """Run up to N AI fix attempts in the worktree.  Returns FixResult."""
    result = FixResult()
    if not is_enabled():
        result.status = "disabled"
        return result
    if not _api_key():
        result.status = "no_api_key"
        log.error("ai_fix: AUTO_UPDATE_AI_FIX_ENABLED=true but no API key for provider=%s", _provider())
        return result

    test_hashes = _hash_dir(worktree, "tests")
    last_pytest_out = initial_pytest_output

    for attempt in range(1, _max_retries() + 1):
        log.info("ai_fix: attempt %d/%d", attempt, _max_retries())
        record: dict = {"attempt": attempt, "passed": False, "diff": "", "pytest_out": "", "reject_reason": ""}

        user_msg = (
            f"### pytest output\n```\n{last_pytest_out[-3000:]}\n```\n\n"
            f"Output a diff that fixes this."
        )
        llm_text = await _llm_call(_AI_SYSTEM_PROMPT, user_msg)
        if not llm_text:
            record["reject_reason"] = "(LLM call failed)"
            result.attempts.append(record)
            continue
        if "# NO FIX" in llm_text:
            record["reject_reason"] = "(LLM said NO FIX)"
            result.attempts.append(record)
            break

        diff = _extract_diff(llm_text)
        if not diff:
            record["reject_reason"] = "(no valid diff in LLM output)"
            record["diff"] = llm_text[:1000]
            result.attempts.append(record)
            continue
        record["diff"] = diff

        ok_path, bad_paths = _diff_touches_only_allowlist(diff)
        if not ok_path:
            record["reject_reason"] = f"(diff touches disallowed paths: {bad_paths[:5]})"
            log.warning("ai_fix: rejecting patch — disallowed paths %s", bad_paths)
            result.attempts.append(record)
            continue

        applied, apply_err = await _git_apply(worktree, diff)
        if not applied:
            record["reject_reason"] = f"(git apply failed: {apply_err[:200]})"
            await _git_revert_changes(worktree)
            result.attempts.append(record)
            continue

        if not _tests_unchanged(worktree, test_hashes):
            record["reject_reason"] = "(AI modified test files)"
            log.warning("ai_fix: AI patch modified tests/ — reverting")
            await _git_revert_changes(worktree)
            result.attempts.append(record)
            continue

        rc, pytest_out = await _run_pytest(worktree, test_cmd)
        record["pytest_out"] = pytest_out
        last_pytest_out = pytest_out
        if rc == 0:
            record["passed"] = True
            result.attempts.append(record)
            log.info("ai_fix: attempt %d PASSED", attempt)
            break
        else:
            record["reject_reason"] = f"(tests still failing rc={rc})"
            await _git_revert_changes(worktree)
            result.attempts.append(record)

    if result.attempts and result.attempts[-1].get("passed"):
        # Open PR for human review (default), or signal caller to merge inline.
        if _require_human_approve():
            pr_url = await _push_branch_and_open_pr(worktree, result.attempts)
            result.status = "passed_pr_opened"
            result.pr_url = pr_url
        else:
            # Caller is responsible for the merge + restart.  Return the
            # signal and let _run_self_update_worktree handle it.
            result.status = "passed_merge_inline"
    else:
        issue_url = await _open_failure_issue(result.attempts, initial_pytest_output)
        result.status = "failed_issue_opened"
        result.issue_url = issue_url

    return result

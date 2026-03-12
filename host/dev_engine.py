"""
EvoClaw DevEngine — 7-Stage Self-Development Pipeline.

Pipeline: Analyze → Design → Implement → Test → Review → Document → Deploy

Modes:
  auto        — fully automated, runs all stages sequentially
  interactive — pauses after each stage and waits for user to "continue"

Each stage (1-6) runs via Docker container so the LLM powers the generation.
Stage 7 (Deploy) runs in the host process and writes files to disk.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from . import config, db

log = logging.getLogger(__name__)


# ── Dev log helpers (per-session file-based log for Dashboard terminal) ────────

def _dev_log_path(session_id: str) -> Path:
    """Return path to the per-session log file."""
    log_dir = config.DATA_DIR / "dev_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{session_id}.log"


def _write_dev_log(session_id: str, text: str) -> None:
    """Append a log line to the session's log file (timestamp prefix)."""
    try:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {text}\n"
        with _dev_log_path(session_id).open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def get_dev_logs(session_id: str, offset: int = 0) -> list[str]:
    """Return log lines starting from *offset* (line index)."""
    try:
        p = _dev_log_path(session_id)
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()
        return lines[offset:]
    except Exception:
        return []


# ── Stage definitions ─────────────────────────────────────────────────────────

class DevStage(Enum):
    ANALYZE   = "analyze"
    DESIGN    = "design"
    IMPLEMENT = "implement"
    TEST      = "test"
    REVIEW    = "review"
    DOCUMENT  = "document"
    DEPLOY    = "deploy"

STAGE_ORDER: list[DevStage] = list(DevStage)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class DevSession:
    session_id: str
    prompt: str
    jid: str
    mode: str                                        # "auto" | "interactive"
    artifacts: Dict[str, str] = field(default_factory=dict)  # stage_name → content
    current_stage: Optional[str] = None
    status: str = "pending"                          # pending/running/paused/completed/failed
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _ensure_table() -> None:
    """Create dev_sessions table if it doesn't exist yet."""
    conn = db.get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dev_sessions (
            session_id   TEXT PRIMARY KEY,
            jid          TEXT NOT NULL,
            prompt       TEXT NOT NULL,
            mode         TEXT NOT NULL DEFAULT 'auto',
            status       TEXT NOT NULL DEFAULT 'pending',
            current_stage TEXT,
            artifacts    TEXT DEFAULT '{}',
            error        TEXT,
            created_at   REAL NOT NULL,
            updated_at   REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dev_sessions_jid ON dev_sessions(jid, created_at)")
    conn.commit()


def save_session(session: DevSession) -> None:
    session.updated_at = time.time()
    try:
        _ensure_table()
        conn = db.get_db()
        conn.execute("""
            INSERT OR REPLACE INTO dev_sessions
            (session_id, jid, prompt, mode, status, current_stage, artifacts, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session.session_id, session.jid, session.prompt, session.mode,
            session.status, session.current_stage,
            json.dumps(session.artifacts, ensure_ascii=False),
            session.error, session.created_at, session.updated_at,
        ))
        conn.commit()
    except Exception as e:
        log.error(f"DevEngine: save_session failed: {e}")


def load_session(session_id: str) -> Optional[DevSession]:
    try:
        _ensure_table()
        conn = db.get_db()
        row = conn.execute(
            "SELECT * FROM dev_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        return DevSession(
            session_id=row["session_id"],
            jid=row["jid"],
            prompt=row["prompt"],
            mode=row["mode"],
            status=row["status"],
            current_stage=row["current_stage"],
            artifacts=json.loads(row["artifacts"] or "{}"),
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    except Exception as e:
        log.error(f"DevEngine: load_session failed: {e}")
        return None


def list_sessions(jid: Optional[str] = None, limit: int = 30) -> list[dict]:
    """Return recent sessions as plain dicts (for dashboard)."""
    try:
        _ensure_table()
        conn = db.get_db()
        if jid:
            rows = conn.execute(
                "SELECT * FROM dev_sessions WHERE jid=? ORDER BY created_at DESC LIMIT ?",
                (jid, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dev_sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            artifacts = json.loads(r["artifacts"] or "{}")
            result.append({
                "session_id":    r["session_id"],
                "jid":           r["jid"],
                "prompt":        r["prompt"][:120],
                "mode":          r["mode"],
                "status":        r["status"],
                "current_stage": r["current_stage"],
                "stages_done":   len(artifacts),
                "error":         r["error"],
                "created_at":    r["created_at"],
                "updated_at":    r["updated_at"],
            })
        return result
    except Exception as e:
        log.error(f"DevEngine: list_sessions failed: {e}")
        return []


def get_session_detail(session_id: str) -> Optional[dict]:
    """Return full session including artifact previews."""
    s = load_session(session_id)
    if not s:
        return None
    return {
        "session_id":    s.session_id,
        "jid":           s.jid,
        "prompt":        s.prompt,
        "mode":          s.mode,
        "status":        s.status,
        "current_stage": s.current_stage,
        "error":         s.error,
        "created_at":    s.created_at,
        "updated_at":    s.updated_at,
        "artifacts": {
            stage: content[:500] + ("..." if len(content) > 500 else "")
            for stage, content in s.artifacts.items()
        },
    }


# ── Stage prompts ─────────────────────────────────────────────────────────────

def _build_prompt(stage: DevStage, session: DevSession) -> str:
    arts = session.artifacts
    req  = arts.get("analyze",   "(no requirements yet)")
    des  = arts.get("design",    "(no design yet)")
    impl = arts.get("implement", "(no implementation yet)")
    test = arts.get("test",      "(no tests yet)")
    rev  = arts.get("review",    "(no review yet)")
    doc  = arts.get("document",  "(no docs yet)")

    if stage == DevStage.ANALYZE:
        return f"""You are a software requirements analyst working on the EvoClaw AI assistant framework (Python, Docker, SQLite, asyncio).

The user wants to build:
> {session.prompt}

Produce a clear, actionable requirements document in markdown:

## Summary
2-3 sentences describing what will be built.

## Key Features
Bulleted list of specific features to implement.

## Technical Constraints
Language, compatibility, performance requirements.

## Files to Create or Modify
Exact file paths relative to the project root (e.g., `host/dev_engine.py`, `host/dashboard.py`).

## Success Criteria
How to verify the feature works correctly.

Be specific and concrete. No vague language."""

    elif stage == DevStage.DESIGN:
        return f"""You are a software architect working on the EvoClaw AI assistant framework.

Requirements:
{req}

Produce a detailed technical design document in markdown:

## Architecture Overview
How this fits into the existing EvoClaw host/ structure.

## Module Structure
For each file to create or modify, describe its responsibilities.

## Key Classes and Functions
Signatures, parameters, return types, and purpose.

## Data Flow
Step-by-step how data flows through the feature.

## Integration Points
How this connects to: main.py, db.py, dashboard.py, ipc_watcher.py, container_runner.py.

## Database Schema
Any new tables or columns needed (include CREATE TABLE SQL).

Be precise. Include real function signatures."""

    elif stage == DevStage.IMPLEMENT:
        return f"""You are a senior Python developer implementing a feature for EvoClaw.

Requirements:
{req}

Design:
{des}

Write the complete, production-ready Python implementation.

Rules:
- Full file contents with all imports
- Docstrings on all public functions and classes
- Proper error handling with try/except and logging
- Type hints throughout
- No TODOs, no placeholders — real working code only

Format each file as:
--- FILE: host/example.py ---
(complete file contents)
--- END FILE ---

If modifying an existing file, output the complete modified file."""

    elif stage == DevStage.TEST:
        return f"""You are a QA engineer writing tests for EvoClaw.

Implementation:
{impl}

Write comprehensive pytest test files:
- Test all public functions and classes
- Include happy path, edge cases, and error conditions
- Mock external dependencies (Docker subprocess, SQLite, network calls)
- Use pytest fixtures for common setup

Format each file as:
--- FILE: tests/test_example.py ---
(complete test file)
--- END FILE ---

After the files, add:
## Test Plan Summary
Brief description of what is covered and any manual verification steps."""

    elif stage == DevStage.REVIEW:
        return f"""You are a senior code reviewer. Review this implementation for quality, security, and correctness.

Implementation:
{impl}

Tests:
{test}

Produce a review report:

## Overall Assessment
PASS or FAIL — one sentence justification.

## Security Issues
Any vulnerabilities: path traversal, injection, credential exposure, unsafe eval/exec, etc.
Be specific with file and line references.

## Code Quality
Error handling gaps, missing edge cases, style issues.

## Performance
Any obvious bottlenecks or resource leaks.

## Required Changes
Specific fixes needed if FAIL. Include corrected code snippets.

## Approved Items
What looks good and is confirmed LGTM.

Be strict. If there are real issues, mark FAIL."""

    elif stage == DevStage.DOCUMENT:
        return f"""You are a technical writer documenting a new EvoClaw feature.

Requirements:
{req}

Design:
{des}

Review result:
{rev}

Produce:

## README Section
A markdown section suitable for insertion into README.md. Explain what the feature does and how to use it.

## CHANGELOG Entry
A [X.Y.Z] — YYYY-MM-DD changelog entry following Keep-a-Changelog format.

## Usage Examples
Concrete examples of how to trigger/use the feature."""

    elif stage == DevStage.DEPLOY:
        return f"""You are a DevOps engineer preparing deployment for a new EvoClaw feature.

Implementation:
{impl}

Documentation:
{doc}

Produce:

## Files to Write
List each file path and whether it's a new file or modification.

## Deployment Steps
Ordered steps to deploy (git add, commit message, any migrations needed).

## Verification
Commands to verify the feature works after deployment.

## Rollback Plan
How to revert if something breaks.

Note: The host process will write the actual files. Your output is the deployment manifest."""

    return f"Stage {stage.value}: {session.prompt}"


# ── Stage execution ───────────────────────────────────────────────────────────

async def _run_llm_stage(stage: DevStage, session: DevSession, group: dict) -> Optional[str]:
    """Run a single stage via Docker container. Returns artifact text or None."""
    from .container_runner import run_container_agent

    prompt = _build_prompt(stage, session)
    log.info(f"DevEngine [{session.session_id}] stage={stage.value} group={group['folder']}")

    try:
        result = await run_container_agent(
            group=group,
            prompt=prompt,
            is_scheduled_task=False,
            conversation_history=[],          # isolated per-stage context
        )
        text = (result.get("result") or "").strip()
        if not text:
            log.warning(f"DevEngine stage {stage.value}: empty LLM output (status={result.get('status')})")
            return None
        return text
    except Exception as e:
        log.error(f"DevEngine stage {stage.value} exception: {e}")
        return None


def _deploy_files(session: DevSession) -> tuple[bool, str]:
    """
    Stage 7 (host-process): parse '--- FILE: path ---' blocks from the
    implement artifact and write them to disk within the project root.
    Returns (success, summary_message).
    """
    written: list[str] = []
    errors:  list[str] = []
    base = config.BASE_DIR.resolve()

    for artifact_key in ("implement", "document"):
        content = session.artifacts.get(artifact_key, "")
        if not content:
            continue
        lines = content.splitlines()
        current_file: Optional[str] = None
        current_lines: list[str] = []

        for line in lines:
            if line.startswith("--- FILE:") and line.rstrip().endswith("---"):
                # flush previous block
                if current_file and current_lines:
                    _write_one_file(base, current_file, "\n".join(current_lines), written, errors)
                current_file = line[9:].rstrip()[:-3].strip()
                current_lines = []
            elif line.strip() == "--- END FILE ---":
                if current_file and current_lines:
                    _write_one_file(base, current_file, "\n".join(current_lines), written, errors)
                current_file = None
                current_lines = []
            elif current_file is not None:
                current_lines.append(line)
        # flush trailing block if no END FILE marker
        if current_file and current_lines:
            _write_one_file(base, current_file, "\n".join(current_lines), written, errors)

    if not written and not errors:
        return True, "No --- FILE: --- blocks found. Manual deployment may be required."

    summary = f"Wrote {len(written)} file(s): {', '.join(written)}"
    if errors:
        summary += f" | {len(errors)} error(s): {', '.join(errors)}"
    return len(errors) == 0, summary


def _write_one_file(
    base: Path, rel_path: str, content: str,
    written: list[str], errors: list[str],
) -> None:
    """Write a single file, enforcing that it stays within base directory."""
    try:
        target = (base / rel_path).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            errors.append(f"BLOCKED path traversal: {rel_path!r}")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel_path)
        log.info(f"DevEngine deployed: {rel_path}")
    except Exception as e:
        errors.append(f"{rel_path}: {e}")
        log.error(f"DevEngine deploy error for {rel_path}: {e}")


# ── Main engine class ─────────────────────────────────────────────────────────

class DevEngine:
    """
    EvoClaw Development Engine.

    Usage:
        engine = DevEngine(jid="<group-jid>")
        session = await engine.start("Add a metrics endpoint to the dashboard")
        await engine.run(session, group=main_group, notify_fn=send_message)
    """

    def __init__(self, jid: str):
        self.jid = jid

    async def start(self, prompt: str, mode: str = "auto") -> DevSession:
        """Create a new session and persist it."""
        session = DevSession(
            session_id=f"dev_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            prompt=prompt,
            jid=self.jid,
            mode=mode,
        )
        save_session(session)
        _write_dev_log(session.session_id, f"🚀 DevEngine session 建立（mode={mode}）")
        _write_dev_log(session.session_id, f"📝 Prompt: {prompt[:200]}")
        log.info(f"DevEngine: new session {session.session_id} mode={mode}")
        return session

    async def run(
        self,
        session: DevSession,
        group: dict,
        notify_fn: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> bool:
        """
        Execute (or resume) the pipeline.

        In interactive mode, returns True after each stage completes and sets
        status='paused'. The caller must call resume() to continue.
        """
        session.status = "running"
        save_session(session)

        async def _notify(text: str) -> None:
            if notify_fn:
                try:
                    await notify_fn(text)
                except Exception:
                    pass

        for stage in STAGE_ORDER:
            # Skip stages already completed (enables resume)
            if stage.value in session.artifacts:
                log.debug(f"DevEngine: skip {stage.value} (already done)")
                _write_dev_log(session.session_id, f"⏭ 跳過（已完成）：{stage.value}")
                continue

            session.current_stage = stage.value
            save_session(session)

            stage_label = f"[{stage.value.upper()}]"
            _write_dev_log(session.session_id, f"🔧 {stage_label} 開始執行...")
            await _notify(f"🔧 *{stage_label}* 開始執行...")

            if stage == DevStage.DEPLOY:
                ok, msg = _deploy_files(session)
                artifact = f"{'✅' if ok else '⚠️'} {msg}"
                if not ok:
                    session.status = "failed"
                    session.error = msg
                    save_session(session)
                    _write_dev_log(session.session_id, f"❌ Deploy 失敗：{msg}")
                    await _notify(f"❌ Deploy 失敗：{msg}")
                    return False
                _write_dev_log(session.session_id, f"✅ {stage_label} 完成 — {msg}")
            else:
                artifact = await _run_llm_stage(stage, session, group)
                if not artifact:
                    session.status = "failed"
                    session.error = f"Stage {stage.value} returned no output"
                    save_session(session)
                    _write_dev_log(session.session_id, f"❌ {stage_label} 失敗（LLM 無輸出）")
                    await _notify(f"❌ *{stage_label}* 失敗（LLM 無輸出）")
                    return False
                _write_dev_log(session.session_id,
                               f"✅ {stage_label} 完成（{len(artifact)} 字元）")

            session.artifacts[stage.value] = artifact
            save_session(session)
            await _notify(f"✅ *{stage_label}* 完成")

            # Interactive mode: pause and let caller resume
            if session.mode == "interactive":
                session.status = "paused"
                save_session(session)
                _write_dev_log(session.session_id,
                               f"⏸ 已暫停（interactive mode），等待確認繼續...")
                await _notify(
                    f"⏸ 已暫停。查看 artifact 後回覆 `continue {session.session_id}` 繼續，"
                    f"或 `cancel {session.session_id}` 取消。"
                )
                return True  # caller must invoke resume()

        session.status = "completed"
        session.current_stage = None
        save_session(session)
        stages_done = len(session.artifacts)
        _write_dev_log(session.session_id,
                       f"🎉 DevEngine 完成！{stages_done}/7 個階段全部通過。")
        await _notify(
            f"🎉 *DevEngine 完成！* {stages_done}/7 個階段全部通過。\n"
            f"Session ID: `{session.session_id}`"
        )
        return True

    async def resume(
        self,
        session_id: str,
        group: dict,
        notify_fn: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> bool:
        """Resume a paused session."""
        session = load_session(session_id)
        if not session:
            log.error(f"DevEngine: session not found: {session_id}")
            return False
        if session.status not in ("paused", "failed"):
            log.warning(f"DevEngine: cannot resume session in status={session.status}")
            return False
        return await self.run(session, group, notify_fn)

    async def cancel(self, session_id: str) -> bool:
        """Mark a session as cancelled."""
        session = load_session(session_id)
        if not session:
            return False
        session.status = "cancelled"
        session.current_stage = None
        save_session(session)
        log.info(f"DevEngine: session {session_id} cancelled")
        return True

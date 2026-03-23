"""
Tests for DevEngine: 7-Stage Development Pipeline

API tested:
  - DevSession dataclass
  - DevEngine.start() / run() / resume() / cancel()
  - load_session() / list_sessions() / get_session_detail()
  - _deploy_files() path-traversal protection

LLM stages (_run_llm_stage) and Docker calls are mocked so tests run
without a running Docker daemon or API keys.
"""
import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from host.dev_engine import (
    DevEngine,
    DevSession,
    DevStage,
    STAGE_ORDER,
    _deploy_files,
    get_session_detail,
    list_sessions,
    load_session,
    save_session,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def in_memory_db(monkeypatch):
    """
    Replace db.get_db() with an in-memory SQLite connection for test isolation.
    Each test gets a fresh, empty database so tests never share state.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    import host.db as db_module
    monkeypatch.setattr(db_module, "get_db", lambda: conn)
    yield conn
    conn.close()


def make_session(**kwargs) -> DevSession:
    """Helper: create a DevSession with sensible defaults."""
    defaults = dict(
        session_id=f"dev_{int(time.time())}_test01",
        prompt="Create a test feature",
        jid="tg:12345",
        mode="auto",
    )
    defaults.update(kwargs)
    return DevSession(**defaults)


MOCK_GROUP = {"folder": "telegram_test", "jid": "tg:12345", "name": "Test"}

# Stage artifact values returned by the mocked LLM
FAKE_ARTIFACTS = {
    "analyze":   "## Requirements\n- Feature A\n- Feature B",
    "design":    "## Design\nUse module X with class Y",
    "implement": "--- FILE: host/new_feature.py ---\ndef hello(): pass\n--- END FILE ---",
    "test":      "## Tests\ndef test_hello(): assert hello() is None",
    "review":    "PASS – no security issues found",
    "document":  "## Docs\n`hello()` — greeting function",
}


# ── DevSession Dataclass ───────────────────────────────────────────────────────

class TestDevSession:
    def test_creation_with_required_fields(self):
        s = DevSession(
            session_id="dev_123_abc",
            prompt="Build X",
            jid="tg:9999",
            mode="auto",
        )
        assert s.session_id == "dev_123_abc"
        assert s.prompt == "Build X"
        assert s.jid == "tg:9999"
        assert s.mode == "auto"

    def test_default_values(self):
        s = make_session()
        assert s.artifacts == {}
        assert s.current_stage is None
        assert s.status == "pending"
        assert s.error is None
        assert isinstance(s.created_at, float)
        assert isinstance(s.updated_at, float)

    def test_artifacts_are_independent_instances(self):
        s1 = make_session(session_id="s1")
        s2 = make_session(session_id="s2")
        s1.artifacts["analyze"] = "foo"
        assert "analyze" not in s2.artifacts  # must not share the same dict


# ── DevStage Enum ──────────────────────────────────────────────────────────────

class TestDevStage:
    def test_stage_values(self):
        assert DevStage.ANALYZE.value   == "analyze"
        assert DevStage.DESIGN.value    == "design"
        assert DevStage.IMPLEMENT.value == "implement"
        assert DevStage.TEST.value      == "test"
        assert DevStage.REVIEW.value    == "review"
        assert DevStage.DOCUMENT.value  == "document"
        assert DevStage.DEPLOY.value    == "deploy"

    def test_stage_order_length(self):
        assert len(STAGE_ORDER) == 7

    def test_stage_order_sequence(self):
        assert STAGE_ORDER[0] == DevStage.ANALYZE
        assert STAGE_ORDER[-1] == DevStage.DEPLOY


# ── DB helpers ─────────────────────────────────────────────────────────────────

class TestSessionPersistence:
    def test_save_and_load(self, in_memory_db):
        s = make_session()
        s.artifacts["analyze"] = "some requirements"
        save_session(s)

        loaded = load_session(s.session_id)
        assert loaded is not None
        assert loaded.session_id == s.session_id
        assert loaded.prompt == s.prompt
        assert loaded.artifacts["analyze"] == "some requirements"
        assert loaded.status == "pending"

    def test_load_nonexistent_returns_none(self, in_memory_db):
        result = load_session("dev_nonexistent_000")
        assert result is None

    def test_save_overwrites_existing(self, in_memory_db):
        s = make_session()
        save_session(s)
        s.status = "completed"
        s.artifacts["analyze"] = "updated"
        save_session(s)

        loaded = load_session(s.session_id)
        assert loaded.status == "completed"
        assert loaded.artifacts["analyze"] == "updated"

    def test_list_sessions_empty(self, in_memory_db):
        result = list_sessions()
        assert result == []

    def test_list_sessions_returns_recent_first(self, in_memory_db):
        s1 = make_session(session_id="s1", created_at=1000.0)
        s2 = make_session(session_id="s2", created_at=2000.0)
        save_session(s1)
        save_session(s2)

        result = list_sessions()
        assert len(result) == 2
        assert result[0]["session_id"] == "s2"  # most recent first
        assert result[1]["session_id"] == "s1"

    def test_list_sessions_filter_by_jid(self, in_memory_db):
        s1 = make_session(session_id="s1", jid="tg:aaa")
        s2 = make_session(session_id="s2", jid="tg:bbb")
        save_session(s1)
        save_session(s2)

        result = list_sessions(jid="tg:aaa")
        assert len(result) == 1
        assert result[0]["session_id"] == "s1"

    def test_get_session_detail(self, in_memory_db):
        s = make_session()
        s.artifacts["analyze"] = "A" * 600  # longer than 500 chars
        save_session(s)

        detail = get_session_detail(s.session_id)
        assert detail is not None
        assert detail["session_id"] == s.session_id
        # Artifact preview should be truncated to 500 + "..."
        assert detail["artifacts"]["analyze"].endswith("...")
        assert len(detail["artifacts"]["analyze"]) == 503

    def test_get_session_detail_nonexistent(self, in_memory_db):
        assert get_session_detail("dev_missing") is None


# ── DevEngine.start() ──────────────────────────────────────────────────────────

class TestDevEngineStart:
    @pytest.mark.asyncio
    async def test_start_creates_session(self, in_memory_db):
        engine = DevEngine(jid="tg:12345")
        session = await engine.start("Build a metrics endpoint")

        assert session.jid == "tg:12345"
        assert session.prompt == "Build a metrics endpoint"
        assert session.mode == "auto"
        assert session.status == "pending"
        assert session.session_id.startswith("dev_")

    @pytest.mark.asyncio
    async def test_start_persists_to_db(self, in_memory_db):
        engine = DevEngine(jid="tg:12345")
        session = await engine.start("Test prompt")

        loaded = load_session(session.session_id)
        assert loaded is not None
        assert loaded.session_id == session.session_id

    @pytest.mark.asyncio
    async def test_start_interactive_mode(self, in_memory_db):
        engine = DevEngine(jid="tg:12345")
        session = await engine.start("Interactive task", mode="interactive")
        assert session.mode == "interactive"


# ── DevEngine.run() ────────────────────────────────────────────────────────────

class TestDevEngineRun:
    @pytest.mark.asyncio
    async def test_auto_mode_completes_all_stages(self, in_memory_db):
        """Auto mode should run all 7 stages and mark session completed."""
        engine = DevEngine(jid="tg:12345")
        session = await engine.start("Auto test feature")

        # Mock LLM stages to return fake artifacts (no Docker needed)
        async def mock_llm_stage(stage, sess, group):
            return FAKE_ARTIFACTS.get(stage.value, f"artifact for {stage.value}")

        # Mock deploy stage to succeed without writing files
        def mock_deploy(sess):
            return True, "Wrote 1 file(s): host/new_feature.py"

        with patch("host.dev_engine._run_llm_stage", side_effect=mock_llm_stage), \
             patch("host.dev_engine._deploy_files", side_effect=mock_deploy):
            success = await engine.run(session, group=MOCK_GROUP)

        assert success is True
        assert session.status == "completed"
        assert len(session.artifacts) == 7
        for stage in DevStage:
            assert stage.value in session.artifacts

    @pytest.mark.asyncio
    async def test_interactive_mode_pauses_after_first_stage(self, in_memory_db):
        """Interactive mode should pause after each stage and return True."""
        engine = DevEngine(jid="tg:12345")
        session = await engine.start("Interactive feature", mode="interactive")

        async def mock_llm_stage(stage, sess, group):
            return FAKE_ARTIFACTS.get(stage.value, "ok")

        with patch("host.dev_engine._run_llm_stage", side_effect=mock_llm_stage):
            result = await engine.run(session, group=MOCK_GROUP)

        assert result is True
        assert session.status == "paused"
        # Only the first stage should have been completed
        assert len(session.artifacts) == 1
        assert DevStage.ANALYZE.value in session.artifacts

    @pytest.mark.asyncio
    async def test_notify_fn_is_called(self, in_memory_db):
        """notify_fn should be called for each stage start and completion."""
        engine = DevEngine(jid="tg:12345")
        session = await engine.start("Notify test")
        notifications = []

        async def mock_llm_stage(stage, sess, group):
            return "ok"

        def mock_deploy(sess):
            return True, "Wrote 0 file(s)"

        async def capture_notify(text):
            notifications.append(text)

        with patch("host.dev_engine._run_llm_stage", side_effect=mock_llm_stage), \
             patch("host.dev_engine._deploy_files", side_effect=mock_deploy):
            await engine.run(session, group=MOCK_GROUP, notify_fn=capture_notify)

        # Should have received at least one notification per stage (start + end = 14+)
        assert len(notifications) >= 14

    @pytest.mark.asyncio
    async def test_stage_failure_marks_session_failed(self, in_memory_db):
        """If a stage returns empty output, session should be marked failed."""
        engine = DevEngine(jid="tg:12345")
        session = await engine.start("Failing test")

        async def mock_llm_fail(stage, sess, group):
            return ""  # empty output → failure

        with patch("host.dev_engine._run_llm_stage", side_effect=mock_llm_fail):
            success = await engine.run(session, group=MOCK_GROUP)

        assert success is False
        assert session.status == "failed"
        assert session.error is not None

    @pytest.mark.asyncio
    async def test_skip_already_completed_stages_on_resume(self, in_memory_db):
        """run() should skip stages that already have artifacts (resume support)."""
        engine = DevEngine(jid="tg:12345")
        session = await engine.start("Resume test")

        # Pre-populate first 3 stages
        session.artifacts["analyze"]   = "pre-existing requirements"
        session.artifacts["design"]    = "pre-existing design"
        session.artifacts["implement"] = "pre-existing impl"
        session.status = "paused"
        save_session(session)

        call_log = []

        async def mock_llm_stage(stage, sess, group):
            call_log.append(stage.value)
            return f"output for {stage.value}"

        def mock_deploy(sess):
            return True, "Wrote 0 file(s)"

        with patch("host.dev_engine._run_llm_stage", side_effect=mock_llm_stage), \
             patch("host.dev_engine._deploy_files", side_effect=mock_deploy):
            await engine.run(session, group=MOCK_GROUP)

        # Only stages 4-7 (test, review, document + deploy) should be called
        assert "analyze" not in call_log
        assert "design" not in call_log
        assert "implement" not in call_log
        assert "test" in call_log
        assert "review" in call_log
        assert "document" in call_log


# ── DevEngine.resume() ─────────────────────────────────────────────────────────

class TestDevEngineResume:
    @pytest.mark.asyncio
    async def test_resume_continues_paused_session(self, in_memory_db):
        """resume() should load the paused session and continue execution."""
        engine = DevEngine(jid="tg:12345")

        # Set up a paused session with analyze done
        session = DevSession(
            session_id="dev_resume_test",
            prompt="Resume feature",
            jid="tg:12345",
            mode="auto",
            artifacts={"analyze": "requirements"},
            status="paused",
        )
        save_session(session)

        async def mock_llm_stage(stage, sess, group):
            return f"artifact for {stage.value}"

        def mock_deploy(sess):
            return True, "Wrote 0 file(s)"

        with patch("host.dev_engine._run_llm_stage", side_effect=mock_llm_stage), \
             patch("host.dev_engine._deploy_files", side_effect=mock_deploy):
            success = await engine.resume("dev_resume_test", group=MOCK_GROUP)

        assert success is True
        loaded = load_session("dev_resume_test")
        assert loaded.status == "completed"
        assert len(loaded.artifacts) == 7

    @pytest.mark.asyncio
    async def test_resume_nonexistent_session_returns_false(self, in_memory_db):
        engine = DevEngine(jid="tg:12345")
        result = await engine.resume("dev_does_not_exist", group=MOCK_GROUP)
        assert result is False

    @pytest.mark.asyncio
    async def test_resume_completed_session_returns_false(self, in_memory_db):
        """Cannot resume a session that is already completed."""
        engine = DevEngine(jid="tg:12345")
        session = make_session(session_id="dev_done", status="completed")
        save_session(session)

        result = await engine.resume("dev_done", group=MOCK_GROUP)
        assert result is False


# ── DevEngine.cancel() ─────────────────────────────────────────────────────────

class TestDevEngineCancel:
    @pytest.mark.asyncio
    async def test_cancel_marks_session_cancelled(self, in_memory_db):
        engine = DevEngine(jid="tg:12345")
        session = await engine.start("Cancel me")

        result = await engine.cancel(session.session_id)

        assert result is True
        loaded = load_session(session.session_id)
        assert loaded.status == "cancelled"
        assert loaded.current_stage is None

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_false(self, in_memory_db):
        engine = DevEngine(jid="tg:12345")
        result = await engine.cancel("dev_nonexistent")
        assert result is False


# ── _deploy_files() ────────────────────────────────────────────────────────────

_PASSING_REVIEW = "## Overall Assessment\nPASS — no security issues found.\n"


class TestDeployFiles:
    def test_no_file_blocks_returns_message(self, tmp_path, in_memory_db):
        """If implement artifact has no --- FILE: --- blocks, returns a message.

        TEST-10 FIX: _deploy_files() now gates on the REVIEW artifact containing
        a PASS verdict (BUG-DE-2 fix).  Tests must set the review artifact or the
        function returns (False, 'Deploy blocked: REVIEW stage ...') before even
        checking for file blocks.
        """
        session = make_session()
        session.artifacts["review"] = _PASSING_REVIEW
        session.artifacts["implement"] = "def hello(): pass  # no file block"
        ok, msg = _deploy_files(session)
        # Returns True with a 'no blocks' message (not a failure)
        assert "No --- FILE: ---" in msg or "Manual deployment" in msg

    def test_writes_file_from_implement_artifact(self, tmp_path, in_memory_db):
        """TEST-10 FIX: also set review artifact with PASS verdict."""
        session = make_session()
        session.artifacts["review"] = _PASSING_REVIEW
        session.artifacts["implement"] = (
            "--- FILE: hello.py ---\n"
            "def hello():\n"
            "    return 'world'\n"
            "--- END FILE ---\n"
        )

        with patch("host.dev_engine.Path") as mock_path_cls:
            # Use a real Path pointing to tmp_path
            import host.dev_engine as de_module
            original_path = de_module.Path
            de_module.Path = original_path  # keep real Path

            # Patch config.DATA_DIR to use tmp_path
            with patch("host.dev_engine.config") as mock_config:
                mock_config.DATA_DIR = tmp_path
                ok, msg = _deploy_files(session)

        assert ok is True
        assert "hello.py" in msg

    def test_deploy_blocked_without_passing_review(self, tmp_path, in_memory_db):
        """_deploy_files must refuse to deploy if REVIEW did not PASS.

        This tests the BUG-DE-2 fix: a deploy without a PASS review must be
        blocked regardless of the implement artifact content.
        """
        session = make_session()
        # No review artifact or FAIL review → must block
        session.artifacts["review"] = "## Overall Assessment\nFAIL — security issues."
        session.artifacts["implement"] = (
            "--- FILE: hello.py ---\n"
            "def hello(): pass\n"
            "--- END FILE ---\n"
        )
        ok, msg = _deploy_files(session)
        assert ok is False
        assert "blocked" in msg.lower() or "review" in msg.lower()

    def test_deploy_blocked_when_review_absent(self, tmp_path, in_memory_db):
        """_deploy_files must block when no review artifact exists at all."""
        session = make_session()
        # review artifact deliberately absent
        session.artifacts["implement"] = (
            "--- FILE: hello.py ---\ndef hello(): pass\n--- END FILE ---\n"
        )
        ok, msg = _deploy_files(session)
        assert ok is False

    def test_blocks_path_traversal(self, tmp_path, in_memory_db):
        """Paths like ../../etc/passwd must be blocked.

        TEST-12 FIX: _write_one_file uses config.BASE_DIR (not DATA_DIR) as the
        containment boundary.  The test must patch BASE_DIR so that the
        path-traversal check has a meaningful base to test against.
        Without patching BASE_DIR, the 'base' variable resolves to the real
        project root and the traversal assertion is non-deterministic.
        """
        session = make_session()
        session.artifacts["review"] = _PASSING_REVIEW
        session.artifacts["implement"] = (
            "--- FILE: ../../etc/passwd ---\n"
            "root:x:0:0\n"
            "--- END FILE ---\n"
        )

        # Create a shallow jail directory so ../../etc/passwd escapes it
        jail = tmp_path / "jail"
        jail.mkdir()

        with patch("host.dev_engine.config") as mock_config:
            mock_config.DATA_DIR = tmp_path
            mock_config.BASE_DIR = jail
            ok, msg = _deploy_files(session)

        # Path traversal should be blocked (BLOCKED in errors → summarised in msg)
        assert "BLOCKED" in msg or (not ok), (
            f"Expected path traversal to be blocked, got ok={ok}, msg={msg!r}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

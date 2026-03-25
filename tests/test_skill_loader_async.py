"""
tests/test_skill_loader_async.py — Phase 27B coverage for BUG-P26A-1 and BUG-P26A-2.

BUG-P26A-1 (skill_loader.call_skill): If a skill handler's entry-point
    function is `async def run(...)`, calling it without `await` returns a
    coroutine object — always truthy, never executed, eventually
    garbage-collected with a "coroutine was never awaited" warning.
    Fix: detect iscoroutine(result) and await it.

BUG-P26A-2 (workflow_engine.add_skill_step): skill_step_fn previously called
    exec_skill() without await.  This returned a coroutine object (always
    truthy), so getattr(coroutine, "run", None) returned None and every skill
    silently fell back to returning its SKILL.md content.  Additionally, even
    after the exec_skill fix, if run_fn is an async function, calling it
    without await also returns a coroutine object instead of the result.
    Fix: await exec_skill() AND detect iscoroutine(run_fn(...)) then await.

Covers:
  - call_skill() with an async handler awaits and returns the correct result
  - call_skill() with a sync handler still works correctly
  - call_skill() with a missing handler raises FileNotFoundError
  - call_skill() with a handler that has no run() raises AttributeError
  - workflow step with async run_fn returns actual result, not coroutine object
  - workflow step with sync run_fn returns actual result
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.skill_loader import SkillLoader
from host.enterprise.workflow_engine import WorkflowEngine, StepStatus, RunStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def skills_dir(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    return d


@pytest.fixture
def loader(skills_dir):
    return SkillLoader(skills_dir=skills_dir)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_skill(skills_dir: Path, name: str, handler_src: str, md: str = "") -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(md or f"# {name}\nTest skill.", encoding="utf-8")
    (skill_dir / "handler.py").write_text(handler_src, encoding="utf-8")


def _write_md_only_skill(skills_dir: Path, name: str, content: str) -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# call_skill — BUG-P26A-1
# ---------------------------------------------------------------------------

class TestCallSkillAsync:
    """BUG-P26A-1: call_skill() must await async handlers."""

    @pytest.mark.asyncio
    async def test_async_handler_returns_correct_result(self, loader, skills_dir):
        """An async run() function must be awaited; its return value must be returned."""
        _write_skill(skills_dir, "async-skill", """\
import asyncio

async def run(agent_id="default", **kwargs):
    await asyncio.sleep(0)  # minimal yield to confirm it is truly async
    return {"agent": agent_id, "async": True}
""")
        result = await loader.call_skill("async-skill", fn="run", agent_id="test-agent")
        assert result == {"agent": "test-agent", "async": True}, (
            f"Expected async handler result dict, got: {result!r}"
        )

    @pytest.mark.asyncio
    async def test_async_handler_not_coroutine_object(self, loader, skills_dir):
        """call_skill() must NOT return a coroutine object for async handlers."""
        import inspect
        _write_skill(skills_dir, "async-no-co", """\
async def run(**kwargs):
    return "awaited_value"
""")
        result = await loader.call_skill("async-no-co")
        # Must NOT be a coroutine — it must have been awaited
        assert not inspect.iscoroutine(result), (
            "call_skill() returned a coroutine object instead of awaiting it"
        )
        assert result == "awaited_value"

    @pytest.mark.asyncio
    async def test_sync_handler_still_works(self, loader, skills_dir):
        """A sync run() function must still work correctly after the fix."""
        _write_skill(skills_dir, "sync-skill", """\
def run(agent_id="default", **kwargs):
    return {"sync": True, "agent": agent_id}
""")
        result = await loader.call_skill("sync-skill", fn="run", agent_id="sync-agent")
        assert result == {"sync": True, "agent": "sync-agent"}

    @pytest.mark.asyncio
    async def test_missing_handler_raises_file_not_found(self, loader, skills_dir):
        """call_skill() on a skill with no handler.py must raise FileNotFoundError."""
        _write_md_only_skill(skills_dir, "md-only", "# md-only\nNo handler here.")
        with pytest.raises(FileNotFoundError, match="No handler.py found"):
            await loader.call_skill("md-only")

    @pytest.mark.asyncio
    async def test_missing_function_raises_attribute_error(self, loader, skills_dir):
        """call_skill() with fn='nonexistent' must raise AttributeError."""
        _write_skill(skills_dir, "no-func-skill", "def run(**kwargs): return 'ok'\n")
        with pytest.raises(AttributeError, match="no function 'nonexistent'"):
            await loader.call_skill("no-func-skill", fn="nonexistent")

    @pytest.mark.asyncio
    async def test_async_handler_with_kwargs_passes_args(self, loader, skills_dir):
        """kwargs are correctly forwarded to async handler functions."""
        _write_skill(skills_dir, "kwargs-skill", """\
async def run(agent_id="default", extra=None, **kwargs):
    return {"agent": agent_id, "extra": extra}
""")
        result = await loader.call_skill(
            "kwargs-skill", fn="run", agent_id="eve", extra="hello"
        )
        assert result == {"agent": "eve", "extra": "hello"}

    @pytest.mark.asyncio
    async def test_async_handler_exception_propagates(self, loader, skills_dir):
        """Exceptions from async handlers propagate through call_skill()."""
        _write_skill(skills_dir, "async-err-skill", """\
async def run(**kwargs):
    raise ValueError("async error from handler")
""")
        with pytest.raises(ValueError, match="async error from handler"):
            await loader.call_skill("async-err-skill")

    @pytest.mark.asyncio
    async def test_sync_handler_exception_propagates(self, loader, skills_dir):
        """Exceptions from sync handlers also propagate through call_skill()."""
        _write_skill(skills_dir, "sync-err-skill", """\
def run(**kwargs):
    raise RuntimeError("sync error from handler")
""")
        with pytest.raises(RuntimeError, match="sync error from handler"):
            await loader.call_skill("sync-err-skill")


# ---------------------------------------------------------------------------
# WorkflowEngine async run_fn — BUG-P26A-2
# ---------------------------------------------------------------------------

class TestWorkflowEngineAsyncRunFn:
    """BUG-P26A-2: workflow step with async run_fn returns actual result, not coroutine."""

    @pytest.mark.asyncio
    async def test_async_handler_step_returns_value_not_coroutine(self, skills_dir):
        """
        Regression test: if run_fn is async def, the workflow step must await it
        and return the actual value — not a coroutine object.
        """
        import inspect

        _write_skill(skills_dir, "wf-async-skill", """\
import asyncio

async def run(agent_id="default", **kwargs):
    await asyncio.sleep(0)
    return "ASYNC_RESULT"
""")
        loader = SkillLoader(skills_dir=skills_dir)
        engine = WorkflowEngine()
        engine.add_skill_step("wf-async", skill_name="wf-async-skill", skill_loader=loader)

        run_obj = await engine.run_skill_steps()
        assert run_obj is not None
        step = run_obj.steps["wf-async"]
        assert step.status == StepStatus.SUCCESS

        # The result must be the actual string, not a coroutine
        assert not inspect.iscoroutine(step.result), (
            "Workflow step result is a coroutine object — async run_fn was not awaited"
        )
        assert step.result == "ASYNC_RESULT", (
            f"Expected 'ASYNC_RESULT' from async handler, got: {step.result!r}"
        )

    @pytest.mark.asyncio
    async def test_sync_handler_step_returns_value(self, skills_dir):
        """Workflow step with a sync run_fn must return its actual value."""
        _write_skill(skills_dir, "wf-sync-skill", """\
def run(agent_id="default", **kwargs):
    return "SYNC_RESULT"
""")
        loader = SkillLoader(skills_dir=skills_dir)
        engine = WorkflowEngine()
        engine.add_skill_step("wf-sync", skill_name="wf-sync-skill", skill_loader=loader)

        run_obj = await engine.run_skill_steps()
        assert run_obj is not None
        step = run_obj.steps["wf-sync"]
        assert step.status == StepStatus.SUCCESS
        assert step.result == "SYNC_RESULT"

    @pytest.mark.asyncio
    async def test_async_handler_result_differs_from_skill_md(self, skills_dir):
        """
        The most important regression: handler.py IS executed (not silently
        skipped to return SKILL.md content).  The result must differ from
        the SKILL.md fallback text.
        """
        _write_skill(
            skills_dir, "wf-exec-check",
            "async def run(**kwargs): return 'HANDLER_EXECUTED'\n",
            md="# wf-exec-check\nFallback SKILL.md content that must NOT appear.",
        )
        loader = SkillLoader(skills_dir=skills_dir)
        engine = WorkflowEngine()
        engine.add_skill_step("exec-check", skill_name="wf-exec-check", skill_loader=loader)

        run_obj = await engine.run_skill_steps()
        step = run_obj.steps["exec-check"]
        assert step.result == "HANDLER_EXECUTED", (
            f"Expected handler.py to run, but got: {step.result!r}. "
            "This indicates exec_skill() or run_fn was not awaited (BUG-P26A-2)."
        )

    @pytest.mark.asyncio
    async def test_async_handler_with_dict_result(self, skills_dir):
        """Async handler returning a dict — full round-trip through workflow engine."""
        _write_skill(skills_dir, "wf-dict-skill", """\
import asyncio

async def run(agent_id="default", **kwargs):
    await asyncio.sleep(0)
    return {"status": "success", "agent": agent_id}
""")
        loader = SkillLoader(skills_dir=skills_dir)
        engine = WorkflowEngine()
        engine.add_skill_step(
            "dict-step", skill_name="wf-dict-skill", skill_loader=loader, agent_id="andy"
        )

        run_obj = await engine.run_skill_steps()
        assert run_obj.status == RunStatus.SUCCESS
        step = run_obj.steps["dict-step"]
        assert step.result == {"status": "success", "agent": "andy"}

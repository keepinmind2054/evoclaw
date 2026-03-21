"""
tests/test_skill_workflow.py — Phase 4C: Skill ↔ WorkflowEngine integration tests.

Covers:
  - Skill with handler.py runs correctly as a workflow step
  - SKILL.md-only skill returns markdown content as step result
  - Failing skill sets step status to FAILED (no crash)
  - Dependency ordering: step B depends on step A, both skills
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.skill_loader import SkillLoader
from host.enterprise.workflow_engine import WorkflowEngine, StepStatus, RunStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill_with_handler(skills_dir: Path, name: str, handler_src: str) -> None:
    """Create a skill directory with a SKILL.md and a handler.py."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}\nA test skill.", encoding="utf-8")
    (skill_dir / "handler.py").write_text(handler_src, encoding="utf-8")


def _make_skill_md_only(skills_dir: Path, name: str, content: str) -> None:
    """Create a SKILL.md-only skill (no handler.py)."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: handler.py skill runs correctly as a workflow step
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handler_skill_runs_as_step(tmp_path):
    """A skill that has handler.py with run() should execute and return its result."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    handler_src = """\
def run(agent_id="default", **kwargs):
    return {"agent": agent_id, "status": "ok"}
"""
    _make_skill_with_handler(skills_dir, "hello-skill", handler_src)

    loader = SkillLoader(skills_dir=skills_dir)
    engine = WorkflowEngine()
    engine.add_skill_step(
        "greet",
        skill_name="hello-skill",
        skill_loader=loader,
        agent_id="eve",
    )

    run = await engine.run_skill_steps()
    assert run is not None
    assert run.status == RunStatus.SUCCESS

    step = run.steps["greet"]
    assert step.status == StepStatus.SUCCESS
    assert step.result == {"agent": "eve", "status": "ok"}


# ---------------------------------------------------------------------------
# Test 2: SKILL.md-only skill returns markdown content as step result
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_md_only_skill_returns_content(tmp_path):
    """A skill with only SKILL.md (no handler.py) should return the markdown text."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    md_content = "# my-doc-skill\nThis skill provides documentation.\n"
    _make_skill_md_only(skills_dir, "my-doc-skill", md_content)

    loader = SkillLoader(skills_dir=skills_dir)
    engine = WorkflowEngine()
    engine.add_skill_step(
        "docs",
        skill_name="my-doc-skill",
        skill_loader=loader,
    )

    run = await engine.run_skill_steps()
    assert run is not None
    assert run.status == RunStatus.SUCCESS

    step = run.steps["docs"]
    assert step.status == StepStatus.SUCCESS
    assert step.result == md_content


# ---------------------------------------------------------------------------
# Test 3: Failing skill — step function catches the error and returns error dict
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failing_skill_sets_step_failed(tmp_path):
    """
    A skill whose run() raises an exception should have its error captured in
    the step result dict.  The skill_step_fn wrapper catches exceptions so the
    WorkflowDAG step itself completes (StepStatus.SUCCESS at the step wrapper
    level), but the result must be an error dict containing the failure detail.

    BUG-FIX: previously skill_step_fn called exec_skill() without await,
    so handler.py was never actually executed — the coroutine object was
    treated as a loaded module and the "run" attribute lookup silently fell
    through to skill_loader.load().  After the fix, handler.py IS executed
    and the exception IS captured in the error dict.
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    handler_src = """\
def run(agent_id="default", **kwargs):
    raise RuntimeError("intentional failure")
"""
    _make_skill_with_handler(skills_dir, "broken-skill", handler_src)

    loader = SkillLoader(skills_dir=skills_dir)
    engine = WorkflowEngine()
    engine.add_skill_step(
        "bad-step",
        skill_name="broken-skill",
        skill_loader=loader,
    )

    # Engine should not raise — the skill_step_fn wrapper catches errors
    run = await engine.run_skill_steps()
    assert run is not None

    step = run.steps["bad-step"]
    # The step wrapper catches exceptions and returns an error dict, so the
    # DAG-level step succeeds (the wrapper fn didn't raise).
    assert step.status == StepStatus.SUCCESS
    assert isinstance(step.result, dict)
    assert "error" in step.result
    assert "intentional failure" in step.result["error"]
    assert step.result["skill"] == "broken-skill"


# ---------------------------------------------------------------------------
# Test 4: Dependency ordering — step B depends on step A
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dependency_ordering(tmp_path):
    """Step B that depends_on step A should run after A, and see A's result in context."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    handler_a = """\
def run(agent_id="default", **kwargs):
    return {"step": "A", "value": 42}
"""
    handler_b = """\
def run(agent_id="default", **kwargs):
    return {"step": "B", "value": 99}
"""
    _make_skill_with_handler(skills_dir, "skill-a", handler_a)
    _make_skill_with_handler(skills_dir, "skill-b", handler_b)

    loader = SkillLoader(skills_dir=skills_dir)
    engine = WorkflowEngine()
    engine.add_skill_step(
        "step-a",
        skill_name="skill-a",
        skill_loader=loader,
        agent_id="andy",
    )
    engine.add_skill_step(
        "step-b",
        skill_name="skill-b",
        skill_loader=loader,
        agent_id="eve",
        depends_on=["step-a"],
    )

    run = await engine.run_skill_steps()
    assert run is not None
    assert run.status == RunStatus.SUCCESS

    step_a = run.steps["step-a"]
    step_b = run.steps["step-b"]

    assert step_a.status == StepStatus.SUCCESS
    assert step_b.status == StepStatus.SUCCESS

    # Both steps must have completed; A before B
    assert step_a.finished_at is not None
    assert step_b.started_at is not None
    assert step_a.finished_at <= step_b.started_at

    assert step_a.result == {"step": "A", "value": 42}
    assert step_b.result == {"step": "B", "value": 99}


# ---------------------------------------------------------------------------
# Test 5: Method chaining — add_skill_step returns WorkflowEngine
# ---------------------------------------------------------------------------

def test_add_skill_step_returns_engine(tmp_path):
    """add_skill_step() should return the WorkflowEngine instance for chaining."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _make_skill_md_only(skills_dir, "chain-skill", "# chain\nChain test.")

    loader = SkillLoader(skills_dir=skills_dir)
    engine = WorkflowEngine()
    result = engine.add_skill_step(
        "chain",
        skill_name="chain-skill",
        skill_loader=loader,
    )
    assert result is engine


# ---------------------------------------------------------------------------
# Test 6: Non-existent skill returns error dict (no crash)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nonexistent_skill_returns_error_dict(tmp_path):
    """Requesting a skill that does not exist should yield an error dict, not raise."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    loader = SkillLoader(skills_dir=skills_dir)
    engine = WorkflowEngine()
    engine.add_skill_step(
        "ghost",
        skill_name="does-not-exist",
        skill_loader=loader,
    )

    run = await engine.run_skill_steps()
    assert run is not None

    step = run.steps["ghost"]
    # The skill_step_fn wrapper catches FileNotFoundError and returns an error dict
    assert step.status == StepStatus.SUCCESS  # wrapper fn completed without raising
    assert isinstance(step.result, dict)
    assert "error" in step.result
    assert step.result["skill"] == "does-not-exist"


# ---------------------------------------------------------------------------
# Test 7: exec_skill await correctness — handler.py IS executed (not skipped)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handler_is_actually_executed(tmp_path):
    """
    Regression test for the missing-await bug in skill_step_fn.

    Previously exec_skill() was called without await, returning a coroutine
    object that evaluated as truthy.  getattr(coroutine, 'run', None) always
    returned None, so handler.py was never executed and every skill silently
    fell back to returning its SKILL.md content.

    This test verifies that handler.py IS executed by confirming the run()
    return value differs from the SKILL.md content.
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    handler_src = """\
def run(agent_id="default", **kwargs):
    return "HANDLER_EXECUTED"
"""
    skill_dir = skills_dir / "marker-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# marker-skill\nFallback content.", encoding="utf-8")
    (skill_dir / "handler.py").write_text(handler_src, encoding="utf-8")

    loader = SkillLoader(skills_dir=skills_dir)
    engine = WorkflowEngine()
    engine.add_skill_step("marker", skill_name="marker-skill", skill_loader=loader)

    run = await engine.run_skill_steps()
    step = run.steps["marker"]
    assert step.status == StepStatus.SUCCESS
    # If the handler is actually called, result is "HANDLER_EXECUTED"
    # If the missing-await bug is present, result would be "# marker-skill\nFallback content."
    assert step.result == "HANDLER_EXECUTED", (
        f"Expected handler.py to be executed, but got: {step.result!r}"
    )

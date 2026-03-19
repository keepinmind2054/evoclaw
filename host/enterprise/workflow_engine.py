"""
Workflow Engine — Phase 3 Enterprise Suite

DAG-based task orchestration for multi-step agent workflows.
"""
from __future__ import annotations

import collections
import copy
import inspect
import time
import logging
import asyncio
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import TYPE_CHECKING, Optional, Dict, List, Callable, Any

if TYPE_CHECKING:
    from host.skill_loader import SkillLoader

logger = logging.getLogger(__name__)

_SKILL_STEPS_DAG = "_skill_steps"


class StepStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCESS   = "success"
    FAILED    = "failed"
    SKIPPED   = "skipped"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED  = "failed"


@dataclass
class WorkflowStep:
    name: str
    fn: Callable
    depends_on: List[str] = field(default_factory=list)
    timeout: float = 300.0
    retries: int = 0
    status: StepStatus = StepStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


@dataclass
class WorkflowRun:
    workflow_id: str
    name: str
    steps: Dict[str, WorkflowStep]
    status: RunStatus = RunStatus.PENDING
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    context: Dict[str, Any] = field(default_factory=dict)


class WorkflowDAG:
    """
    Simple DAG-based workflow for agent task orchestration.

    Usage:
        dag = WorkflowDAG("deploy-pipeline")

        @dag.step("build")
        async def build(ctx):
            return {"artifact": "app.tar.gz"}

        @dag.step("test", depends_on=["build"])
        async def test(ctx):
            artifact = ctx["build"]["artifact"]
            return {"passed": True}

        @dag.step("deploy", depends_on=["test"])
        async def deploy(ctx):
            return {"url": "https://app.example.com"}

        result = await dag.run()
    """

    def __init__(self, name: str):
        self.name = name
        self._steps: Dict[str, WorkflowStep] = {}

    def step(self, name: str, depends_on: Optional[List[str]] = None, timeout: float = 300.0, retries: int = 0):
        """Decorator to register a workflow step."""
        def decorator(fn):
            self._steps[name] = WorkflowStep(
                name=name, fn=fn,
                depends_on=depends_on or [],
                timeout=timeout, retries=retries
            )
            return fn
        return decorator

    def _topo_sort(self) -> List[str]:
        """Iterative topological sort of steps. Raises ValueError if a cycle is detected."""
        visited = set()
        order = []

        def visit(name, visiting=None):
            if visiting is None:
                visiting = set()
            if name in visited:
                return
            if name in visiting:
                raise ValueError(f"Cycle detected in workflow graph")
            visiting.add(name)
            step = self._steps.get(name)
            if step:
                for dep in step.depends_on:
                    visit(dep, visiting)
            visiting.discard(name)
            visited.add(name)
            order.append(name)

        for name in self._steps:
            visit(name)
        return order

    async def run(self, initial_context: Optional[Dict] = None) -> WorkflowRun:
        """Execute all steps in dependency order, running independent steps in parallel."""
        import uuid
        run = WorkflowRun(
            workflow_id=str(uuid.uuid4())[:8],
            name=self.name,
            steps={k: WorkflowStep(**{f.name: getattr(v, f.name) for f in fields(v)})
                   for k, v in self._steps.items()},
            context=initial_context or {},
            started_at=time.time(),
            status=RunStatus.RUNNING,
        )

        # Build dependency map
        remaining = set(run.steps.keys())

        while remaining:
            # Find all steps whose dependencies are satisfied
            ready = {
                name for name in remaining
                if all(
                    (s := run.steps.get(dep)) is not None and
                    s.status == StepStatus.SUCCESS
                    for dep in run.steps[name].depends_on
                )
            }

            # Check for steps that can never run (deps failed/skipped)
            blocked = set()
            for name in remaining - ready:
                step = run.steps[name]
                for dep in step.depends_on:
                    dep_step = run.steps.get(dep)
                    if dep_step and dep_step.status in (StepStatus.FAILED, StepStatus.SKIPPED):
                        step.status = StepStatus.SKIPPED
                        blocked.add(name)
                        break

            remaining -= blocked

            if not ready:
                if remaining:
                    # Deadlock - mark remaining as failed
                    for name in remaining:
                        run.steps[name].status = StepStatus.FAILED
                        run.steps[name].error = "Deadlock: unsatisfied dependencies"
                break

            remaining -= ready

            # Run all ready steps IN PARALLEL
            # Collect results separately and merge after gather to prevent
            # parallel steps from seeing each other's partial writes to run.context.
            _parallel_results: dict = {}

            async def run_step(step_name):
                step = run.steps[step_name]
                step.status = StepStatus.RUNNING
                step.started_at = time.time()
                # Pass a deep copy snapshot so no step sees a sibling's partial write
                ctx_snapshot = copy.deepcopy(run.context)
                for attempt in range(step.retries + 1):
                    try:
                        if inspect.iscoroutinefunction(step.fn):
                            coro = step.fn(ctx_snapshot)
                        else:
                            loop = asyncio.get_running_loop()
                            coro = loop.run_in_executor(None, step.fn, ctx_snapshot)
                        result = await asyncio.wait_for(coro, timeout=step.timeout)
                        step.result = result
                        step.status = StepStatus.SUCCESS
                        _parallel_results[step_name] = result  # collect result separately
                        logger.info(f"Step '{step_name}' succeeded")
                        break
                    except asyncio.TimeoutError:
                        step.error = f"Timeout after {step.timeout}s"
                        logger.error(f"Step '{step_name}' timed out (attempt {attempt+1})")
                    except Exception as e:
                        step.error = str(e)
                        logger.error(f"Step '{step_name}' failed: {e} (attempt {attempt+1})")
                    if attempt == step.retries:
                        step.status = StepStatus.FAILED
                step.finished_at = time.time()

            await asyncio.gather(*[run_step(name) for name in ready])
            # Merge results back into context AFTER all parallel steps complete
            run.context.update(_parallel_results)
            _parallel_results.clear()

        failed = [n for n, s in run.steps.items() if s.status == StepStatus.FAILED]
        run.status = RunStatus.FAILED if failed else RunStatus.SUCCESS
        run.finished_at = time.time()
        return run


class WorkflowEngine:
    """Registry + runner for multiple named DAGs."""

    def __init__(self):
        self._dags: Dict[str, WorkflowDAG] = {}
        self._history: collections.deque = collections.deque(maxlen=1000)

    def register(self, dag: WorkflowDAG):
        self._dags[dag.name] = dag
        logger.info(f"Workflow registered: {dag.name}")

    def get(self, name: str) -> Optional[WorkflowDAG]:
        return self._dags.get(name)

    async def run(self, name: str, context: Optional[Dict] = None) -> Optional[WorkflowRun]:
        dag = self._dags.get(name)
        if not dag:
            logger.error(f"Workflow not found: {name}")
            return None
        run = await dag.run(context)
        self._history.append(run)
        return run

    def list_workflows(self) -> List[str]:
        return list(self._dags.keys())

    def history(self, limit: int = 20) -> List[WorkflowRun]:
        history_list = list(self._history)
        return history_list[-limit:]

    def add_skill_step(
        self,
        name: str,
        skill_name: str,
        *,
        skill_loader: SkillLoader,
        agent_id: str = "default",
        kwargs: dict | None = None,
        depends_on: list[str] | None = None,
        timeout: float = 60.0,
        retries: int = 0,
    ) -> WorkflowEngine:
        """Register a SkillLoader skill as a workflow step.

        The skill's handler.py run() function is called as the step function.
        If the skill has no handler.py (SKILL.md only), the SKILL.md content
        is returned as the step result.

        Args:
            name: Unique step name in the workflow.
            skill_name: Name of the skill to load (directory under skills/).
            skill_loader: SkillLoader instance used to load the skill.
            agent_id: Agent identifier passed to the skill's run() function.
            kwargs: Additional keyword arguments forwarded to the skill's run().
            depends_on: List of step names that must complete before this step.
            timeout: Per-attempt timeout in seconds.
            retries: Number of retry attempts on failure.

        Returns:
            self, to enable method chaining.
        """
        _kwargs = kwargs or {}

        def skill_step_fn(_ctx: dict) -> Any:
            try:
                module = skill_loader.exec_skill(skill_name)
                if module is not None:
                    run_fn = getattr(module, "run", None)
                    if run_fn is not None:
                        return run_fn(agent_id=agent_id, **_kwargs)
                    # handler.py exists but has no run() — treat as md-only
                    logger.warning(
                        "add_skill_step: handler.py for '%s' has no run() function; "
                        "falling back to SKILL.md content",
                        skill_name,
                    )
                # No handler.py (or no run()); return SKILL.md content
                content = skill_loader.load(skill_name)
                if content is None:
                    raise FileNotFoundError(f"Skill not found: {skill_name}")
                return content
            except Exception as exc:
                logger.error(
                    "add_skill_step: skill '%s' (step '%s') raised: %s",
                    skill_name,
                    name,
                    exc,
                )
                return {"error": str(exc), "skill": skill_name, "step": name}

        # Ensure a DAG exists for ad-hoc skill steps
        if _SKILL_STEPS_DAG not in self._dags:
            self._dags[_SKILL_STEPS_DAG] = WorkflowDAG(_SKILL_STEPS_DAG)

        dag = self._dags[_SKILL_STEPS_DAG]
        dag._steps[name] = WorkflowStep(
            name=name,
            fn=skill_step_fn,
            depends_on=depends_on or [],
            timeout=timeout,
            retries=retries,
        )
        logger.info("add_skill_step: registered step '%s' using skill '%s'", name, skill_name)
        return self

    async def run_skill_steps(self, context: Optional[Dict] = None) -> Optional[WorkflowRun]:
        """Execute all skill steps registered via add_skill_step()."""
        return await self.run(_SKILL_STEPS_DAG, context)

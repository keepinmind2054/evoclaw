"""
Workflow Engine — Phase 3 Enterprise Suite

DAG-based task orchestration for multi-step agent workflows.
"""
import collections
import inspect
import time
import logging
import asyncio
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Optional, Dict, List, Callable, Any

logger = logging.getLogger(__name__)


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
        """Iterative topological sort of steps."""
        visited = set()
        order = []

        def visit(name, visiting=None):
            if visiting is None:
                visiting = set()
            if name in visited:
                return
            if name in visiting:
                return  # cycle, skip
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
                # Pass a read-only snapshot so no step sees a sibling's partial write
                ctx_snapshot = dict(run.context)
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

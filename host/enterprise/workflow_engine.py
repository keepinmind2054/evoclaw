"""
Workflow Engine — Phase 3 Enterprise Suite

DAG-based task orchestration for multi-step agent workflows.
"""
import inspect
import time
import logging
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, List, Callable, Any

logger = logging.getLogger(__name__)


class StepStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SUCCESS   = "success"
    FAILED    = "failed"
    SKIPPED   = "skipped"


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
    status: str = "pending"
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
        """Topological sort of steps."""
        visited = set()
        order = []

        def visit(name):
            if name in visited:
                return
            visited.add(name)
            step = self._steps.get(name)
            if step:
                for dep in step.depends_on:
                    visit(dep)
            order.append(name)

        for name in self._steps:
            visit(name)
        return order

    async def run(self, initial_context: Optional[Dict] = None) -> WorkflowRun:
        """Execute all steps in dependency order."""
        import uuid
        run = WorkflowRun(
            workflow_id=str(uuid.uuid4())[:8],
            name=self.name,
            steps={k: v for k, v in self._steps.items()},
            context=initial_context or {},
            started_at=time.time(),
            status="running",
        )
        order = self._topo_sort()
        for step_name in order:
            step = run.steps.get(step_name)
            if not step:
                continue
            # Check dependencies
            all_ok = all(
                run.steps.get(dep, WorkflowStep("", lambda ctx: None)).status == StepStatus.SUCCESS
                for dep in step.depends_on
            )
            if not all_ok:
                step.status = StepStatus.SKIPPED
                logger.warning(f"Step '{step_name}' skipped (deps failed)")
                continue
            step.status = StepStatus.RUNNING
            step.started_at = time.time()
            for attempt in range(step.retries + 1):
                try:
                    if inspect.iscoroutinefunction(step.fn):
                        coro_or_future = step.fn(run.context)
                    else:
                        # Wrap sync callables so asyncio.wait_for can handle them
                        loop = asyncio.get_running_loop()
                        coro_or_future = loop.run_in_executor(None, step.fn, run.context)
                    result = await asyncio.wait_for(coro_or_future, timeout=step.timeout)
                    step.result = result
                    step.status = StepStatus.SUCCESS
                    run.context[step_name] = result
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

        failed = [n for n, s in run.steps.items() if s.status == StepStatus.FAILED]
        run.status = "failed" if failed else "success"
        run.finished_at = time.time()
        return run


class WorkflowEngine:
    """Registry + runner for multiple named DAGs."""

    def __init__(self):
        self._dags: Dict[str, WorkflowDAG] = {}
        self._history: List[WorkflowRun] = []

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
        return self._history[-limit:]

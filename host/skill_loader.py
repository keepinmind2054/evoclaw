"""
skill_loader.py — Natural language skill system for EvoClaw.

Skills are SKILL.md files in host/skills/<name>/SKILL.md describing
in natural language what the skill does and how to invoke it.

The agent (Claude) reads skill descriptions and executes them.
New skills can be created dynamically at runtime.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Skills directory — writable so agent can create new skills at runtime
_SKILLS_DIR = Path(__file__).parent / "skills"


class SkillLoader:
    """Load and manage natural language skills from SKILL.md files."""

    def __init__(self, skills_dir: Path | None = None):
        self._dir = skills_dir or _SKILLS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._skill_locks: dict[str, asyncio.Lock] = {}

    def list_skills(self) -> list[str]:
        """Return names of all available skills."""
        return sorted(
            d.name
            for d in self._dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        )

    def load(self, name: str) -> Optional[str]:
        """Return the SKILL.md content for a skill, or None if not found."""
        resolved = (self._dir / name).resolve()
        if not str(resolved).startswith(str(self._dir.resolve())):
            raise ValueError(f"Path traversal attempt: {name}")
        skill_file = self._dir / name / "SKILL.md"
        if not skill_file.exists():
            logger.warning("skill_loader: skill not found: %s", name)
            return None
        return skill_file.read_text(encoding="utf-8")

    def load_all(self) -> dict[str, str]:
        """Return all skill descriptions as {name: content}."""
        result = {}
        for name in self.list_skills():
            content = self.load(name)
            if content:
                result[name] = content
        return result

    def create(self, name: str, description: str, overwrite: bool = False) -> bool:
        """
        Dynamically create a new skill.
        The agent calls this to teach itself new capabilities.
        Returns True if created, False if already exists and overwrite=False.
        """
        resolved = (self._dir / name).resolve()
        if not str(resolved).startswith(str(self._dir.resolve())):
            raise ValueError(f"Path traversal attempt: {name}")
        skill_dir = self._dir / name
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists() and not overwrite:
            logger.info("skill_loader: skill already exists: %s", name)
            return False
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(description, encoding="utf-8")
        logger.info("skill_loader: created skill: %s", name)
        return True

    def delete(self, name: str) -> bool:
        """Remove a skill."""
        import shutil
        resolved = (self._dir / name).resolve()
        if not str(resolved).startswith(str(self._dir.resolve())):
            raise ValueError(f"Path traversal attempt: {name}")
        skill_dir = self._dir / name
        if not skill_dir.exists():
            return False
        shutil.rmtree(skill_dir)
        logger.info("skill_loader: deleted skill: %s", name)
        return True

    async def exec_skill(self, name: str) -> types.ModuleType | None:
        """
        Dynamically load and execute host/skills/{name}/handler.py if it exists.
        Hot-swap: each call re-imports the module fresh (no caching in sys.modules).
        Returns the loaded module, or None if no handler.py exists.

        A per-skill asyncio.Lock serialises concurrent calls so that the
        sys.modules pop/insert/exec sequence is atomic — preventing races
        where two coroutines could simultaneously see a partially-initialised
        module under the same module_name key.
        """
        handler_path = self._dir / name / "handler.py"
        if not handler_path.exists():
            logger.debug("skill_loader: no handler.py for skill: %s", name)
            return None
        lock = self._skill_locks.setdefault(name, asyncio.Lock())
        async with lock:
            module_name = f"_evoclaw_skill_{name}"
            # Remove cached version to force fresh load (hot-swap)
            sys.modules.pop(module_name, None)
            spec = importlib.util.spec_from_file_location(module_name, handler_path)
            if spec is None or spec.loader is None:
                logger.error("skill_loader: cannot load handler for skill: %s", name)
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)  # type: ignore[union-attr]
                logger.info("skill_loader: loaded handler for skill: %s", name)
                return module
            except Exception as exc:
                sys.modules.pop(module_name, None)
                logger.error("skill_loader: failed to load handler for skill %s: %s", name, exc)
                return None

    async def reload_skill(self, name: str) -> types.ModuleType | None:
        """
        Hot-swap: reload an already-loaded skill handler without restarting EvoClaw.
        Equivalent to exec_skill() but makes the intent explicit.
        """
        logger.info("skill_loader: hot-reloading skill: %s", name)
        return await self.exec_skill(name)

    async def call_skill(self, name: str, fn: str = "run", **kwargs: Any) -> Any:
        """
        Load skill handler and call a specific function within it.
        Useful for skills that expose a `run(**kwargs)` entry point.

        Example:
            result = await loader.call_skill("weekly-report", fn="run", agent_id="andy")
        """
        module = await self.exec_skill(name)
        if module is None:
            raise FileNotFoundError(f"No handler.py found for skill: {name}")
        func = getattr(module, fn, None)
        if func is None:
            raise AttributeError(f"Skill '{name}' handler has no function '{fn}'")
        return func(**kwargs)

    def skill_summary(self) -> str:
        """Return a human-readable summary of all available skills for agent context."""
        skills = self.list_skills()
        if not skills:
            return "No skills available."
        lines = ["Available skills:"]
        for name in skills:
            content = self.load(name)
            has_handler = (self._dir / name / "handler.py").exists()
            handler_tag = " [handler]" if has_handler else ""
            first_line = next(
                (l.strip() for l in (content or "").splitlines() if l.strip() and not l.startswith("#")),
                "(no description)"
            )
            lines.append(f"  - {name}{handler_tag}: {first_line}")
        return "\n".join(lines)

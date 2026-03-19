"""
skill_loader.py — Natural language skill system for EvoClaw.

Skills are SKILL.md files in host/skills/<name>/SKILL.md describing
in natural language what the skill does and how to invoke it.

The agent (Claude) reads skill descriptions and executes them.
New skills can be created dynamically at runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Skills directory — writable so agent can create new skills at runtime
_SKILLS_DIR = Path(__file__).parent / "skills"


class SkillLoader:
    """Load and manage natural language skills from SKILL.md files."""

    def __init__(self, skills_dir: Path | None = None):
        self._dir = skills_dir or _SKILLS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> list[str]:
        """Return names of all available skills."""
        return sorted(
            d.name
            for d in self._dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        )

    def load(self, name: str) -> Optional[str]:
        """Return the SKILL.md content for a skill, or None if not found."""
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
        skill_dir = self._dir / name
        if not skill_dir.exists():
            return False
        shutil.rmtree(skill_dir)
        logger.info("skill_loader: deleted skill: %s", name)
        return True

    def skill_summary(self) -> str:
        """Return a human-readable summary of all available skills for agent context."""
        skills = self.list_skills()
        if not skills:
            return "No skills available."
        lines = ["Available skills:"]
        for name in skills:
            content = self.load(name)
            # Extract first non-empty line as description
            first_line = next(
                (l.strip() for l in (content or "").splitlines() if l.strip() and not l.startswith("#")),
                "(no description)"
            )
            lines.append(f"  - {name}: {first_line}")
        return "
".join(lines)

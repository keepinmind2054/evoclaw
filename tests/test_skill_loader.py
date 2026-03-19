"""
Smoke tests for host.skill_loader.SkillLoader.

Tests run entirely in a temporary directory — no real host/skills directory
is touched.  All filesystem operations use the tmp_path fixture provided by
pytest.
"""
import sys
from pathlib import Path

import pytest

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from host.skill_loader import SkillLoader


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def skills_dir(tmp_path):
    """Return a fresh temporary skills directory."""
    d = tmp_path / "skills"
    d.mkdir()
    return d


@pytest.fixture
def loader(skills_dir):
    """Return a SkillLoader pointed at the temp skills directory."""
    return SkillLoader(skills_dir=skills_dir)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSkillLoaderList:
    def test_empty_skills_dir_returns_empty_list(self, loader):
        """list_skills() should return [] when no skills exist."""
        assert loader.list_skills() == []

    def test_list_shows_created_skill(self, loader):
        """After creating a skill, list_skills() must include its name."""
        loader.create("hello-world", "Say hello to the world.")
        skills = loader.list_skills()
        assert "hello-world" in skills

    def test_list_ignores_dirs_without_skill_md(self, skills_dir, loader):
        """Directories that lack SKILL.md should not appear in list_skills()."""
        (skills_dir / "orphan-dir").mkdir()
        assert loader.list_skills() == []

    def test_list_is_sorted(self, loader):
        """list_skills() must return names in sorted order."""
        for name in ["zebra", "alpha", "mango"]:
            loader.create(name, f"Skill: {name}")
        skills = loader.list_skills()
        assert skills == sorted(skills)


class TestSkillLoaderCreate:
    def test_create_returns_true_on_new_skill(self, loader):
        """create() should return True when a skill is created for the first time."""
        result = loader.create("new-skill", "Does something new.")
        assert result is True

    def test_create_returns_false_if_exists_and_no_overwrite(self, loader):
        """create() should return False when skill already exists and overwrite=False."""
        loader.create("existing-skill", "First version.")
        result = loader.create("existing-skill", "Second version.", overwrite=False)
        assert result is False

    def test_create_overwrites_when_flag_set(self, loader):
        """create() with overwrite=True should replace existing SKILL.md content."""
        loader.create("overwrite-me", "Original content.")
        loader.create("overwrite-me", "Updated content.", overwrite=True)
        content = loader.load("overwrite-me")
        assert content == "Updated content."

    def test_create_writes_skill_md(self, loader, skills_dir):
        """create() should produce a SKILL.md file with the given description."""
        loader.create("my-skill", "My description.")
        skill_file = skills_dir / "my-skill" / "SKILL.md"
        assert skill_file.exists()
        assert skill_file.read_text(encoding="utf-8") == "My description."


class TestSkillLoaderLoad:
    def test_load_returns_content(self, loader):
        """load() should return the SKILL.md content for an existing skill."""
        loader.create("readable-skill", "Read me.")
        content = loader.load("readable-skill")
        assert content == "Read me."

    def test_load_returns_none_for_missing_skill(self, loader):
        """load() should return None when the skill does not exist."""
        result = loader.load("nonexistent-skill")
        assert result is None

    def test_load_all_returns_all_skills(self, loader):
        """load_all() should return a dict mapping each skill name to its content."""
        loader.create("skill-a", "Content A.")
        loader.create("skill-b", "Content B.")
        all_skills = loader.load_all()
        assert set(all_skills.keys()) == {"skill-a", "skill-b"}
        assert all_skills["skill-a"] == "Content A."
        assert all_skills["skill-b"] == "Content B."


class TestSkillLoaderPathTraversal:
    def test_load_path_traversal_raises(self, loader):
        """load() must raise ValueError for path traversal attempts."""
        with pytest.raises(ValueError, match="Path traversal"):
            loader.load("../etc/passwd")

    def test_create_path_traversal_raises(self, loader):
        """create() must raise ValueError for path traversal attempts."""
        with pytest.raises(ValueError, match="Path traversal"):
            loader.create("../../evil", "Malicious content.")

    def test_delete_path_traversal_raises(self, loader):
        """delete() must raise ValueError for path traversal attempts."""
        with pytest.raises(ValueError, match="Path traversal"):
            loader.delete("../../../tmp")


class TestSkillLoaderDelete:
    def test_delete_existing_skill(self, loader):
        """delete() should remove the skill directory and return True."""
        loader.create("to-delete", "Temporary skill.")
        result = loader.delete("to-delete")
        assert result is True
        assert loader.load("to-delete") is None

    def test_delete_nonexistent_skill_returns_false(self, loader):
        """delete() should return False for a skill that does not exist."""
        result = loader.delete("does-not-exist")
        assert result is False


class TestSkillSummary:
    def test_empty_summary(self, loader):
        """skill_summary() should report no skills when none exist."""
        summary = loader.skill_summary()
        assert "No skills available" in summary

    def test_summary_lists_skill_names(self, loader):
        """skill_summary() should include skill names in its output."""
        loader.create("summary-skill", "# Skill heading\nThis skill does X.")
        summary = loader.skill_summary()
        assert "summary-skill" in summary

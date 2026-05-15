"""Tests for scripts/scrub_memory_xml.py (#583 cleanup)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Import the script as a module without running its __main__.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRUB_PATH = _REPO_ROOT / "scripts" / "scrub_memory_xml.py"
_spec = importlib.util.spec_from_file_location("scrub_memory_xml", _SCRUB_PATH)
assert _spec and _spec.loader
scrub_memory_xml = importlib.util.module_from_spec(_spec)
sys.modules["scrub_memory_xml"] = scrub_memory_xml
_spec.loader.exec_module(scrub_memory_xml)


def test_garbage_line_removed():
    src = (
        "## 任務記錄\n"
        "[2026-05-12] [auto] Task: <context timezone=\"UTC\" /> <messages> <message sender=\"Ke\" time=\"May 12, 2026. Result: success.\n"
        "[2026-05-12] User asked about X — answered.\n"
    )
    cleaned, n = scrub_memory_xml.scrub_text(src)
    assert n == 1
    assert "<context" not in cleaned
    assert "User asked about X" in cleaned


def test_multiple_garbage_lines_removed():
    src = (
        "[2026-05-07] [auto] Task: <context timezone=\"UTC\" /> stuff\n"
        "[2026-05-08] [auto] Task: <context timezone=\"UTC\" /> more\n"
        "[2026-05-09] [auto] Task: <context timezone=\"UTC\" /> even more\n"
        "[2026-05-10] Real entry.\n"
    )
    cleaned, n = scrub_memory_xml.scrub_text(src)
    assert n == 3
    assert cleaned.count("<context") == 0
    assert "Real entry." in cleaned


def test_clean_file_unchanged():
    src = (
        "## 任務記錄\n"
        "[2026-05-12] User asked about X — answered.\n"
        "[2026-05-13] Updated MEMORY with new task log entry.\n"
    )
    cleaned, n = scrub_memory_xml.scrub_text(src)
    assert n == 0
    assert cleaned == src


def test_non_auto_lines_preserved():
    """Lines that look superficially similar but are not the garbage shape
    must not be touched."""
    src = (
        "[2026-05-12] [manual] Task: <context note> — kept on purpose.\n"
        "[2026-05-12] [auto] Task: real summary text, no XML.\n"
        "[2026-05-12] [auto] Task: <context timezone=\"UTC\" /> <messages>...\n"
    )
    cleaned, n = scrub_memory_xml.scrub_text(src)
    assert n == 1
    assert "[manual]" in cleaned
    assert "real summary text" in cleaned
    assert "<context timezone" not in cleaned


def test_main_dry_run_does_not_mutate(tmp_path: Path):
    f = tmp_path / "MEMORY.md"
    src = "[2026-05-12] [auto] Task: <context timezone=\"UTC\" /> blah\nkept\n"
    f.write_text(src, encoding="utf-8")
    rc = scrub_memory_xml.main([str(f), "--dry-run"])
    assert rc == 0
    assert f.read_text(encoding="utf-8") == src  # unchanged


def test_main_writes_backup_and_cleans(tmp_path: Path):
    f = tmp_path / "MEMORY.md"
    src = "[2026-05-12] [auto] Task: <context timezone=\"UTC\" /> blah\nkept\n"
    f.write_text(src, encoding="utf-8")
    rc = scrub_memory_xml.main([str(f)])
    assert rc == 0
    after = f.read_text(encoding="utf-8")
    assert "<context" not in after
    assert "kept" in after
    backups = list(tmp_path.glob("MEMORY.md.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == src


def test_main_no_backup_flag(tmp_path: Path):
    f = tmp_path / "MEMORY.md"
    src = "[2026-05-12] [auto] Task: <context /> blah\nkept\n"
    f.write_text(src, encoding="utf-8")
    rc = scrub_memory_xml.main([str(f), "--no-backup"])
    assert rc == 0
    assert "<context" not in f.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("MEMORY.md.bak.*"))


def test_main_missing_path_returns_2(tmp_path: Path):
    missing = tmp_path / "does_not_exist.md"
    rc = scrub_memory_xml.main([str(missing)])
    assert rc == 2

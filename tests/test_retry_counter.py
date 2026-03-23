"""
Tests for Phase 21A tool-failure retry counter logic.

The counter tracks (tool_name, args_hash) → consecutive_fail_count.
Rules (from agent.py):
  - ✗-prefixed result  → counter[key] += 1
  - [ERROR]-prefixed   → counter[key] += 1
  - Error:-prefixed    → counter[key] += 1
  - ✓-prefixed result  → counter[key] removed (reset)
  - [OK]-prefixed      → counter[key] removed (reset)
  - counter >= _MAX_CONSECUTIVE_TOOL_FAILS (3) → warning injected

These tests replicate the counter logic verbatim from agent.py so they
remain valid without importing the full agent module.
"""
import sys
from pathlib import Path
from pathlib import Path as _Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Constants mirrored from agent.py ──────────────────────────────────────────
_MAX_CONSECUTIVE_TOOL_FAILS = 3
_FAIL_PREFIXES = ("\u2717", "[ERROR]", "Error:")  # ✗, [ERROR], Error:
_SUCCESS_PREFIXES = ("\u2713", "[OK]")             # ✓, [OK]


# ── Helper: apply one iteration of the counter logic ─────────────────────────

def _process_result(result_str: str, tool_name: str, args: dict,
                    counter: dict, warning_holder: list) -> None:
    """
    Replicate the retry-counter block from agent.py (Anthropic/OAI/Gemini loops).

    counter: mutable dict, modified in place.
    warning_holder: single-element list; element 0 is set to the warning string
                    when the threshold is reached, "" otherwise.
    """
    fail_key = (tool_name, hash(str(args)[:200]))
    is_failure = any(result_str.startswith(p) for p in _FAIL_PREFIXES)
    is_success = any(result_str.startswith(p) for p in _SUCCESS_PREFIXES)

    if is_failure:
        counter[fail_key] = counter.get(fail_key, 0) + 1
        if counter[fail_key] >= _MAX_CONSECUTIVE_TOOL_FAILS:
            warning_holder[0] = (
                f"【系統警告】工具 `{tool_name}` 以相同參數已連續失敗 "
                f"{counter[fail_key]} 次。"
                f"請立即更換策略：嘗試不同的方法、參數或工具。"
                f"不要繼續重試相同的失敗操作。"
            )
        else:
            warning_holder[0] = ""
    elif is_success:
        counter.pop(fail_key, None)
        warning_holder[0] = ""


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRetryCounterIncrements:
    """Counter must increment for every failure prefix."""

    def test_cross_prefix_increments_counter(self):
        """✗-prefixed result increments the counter for the (tool, args) key."""
        counter: dict = {}
        warning: list = [""]
        _process_result("\u2717 [exit 1] some error", "Bash", {"command": "ls"}, counter, warning)
        key = ("Bash", hash("{'command': 'ls'}"[:200]))
        assert counter.get(key, 0) == 1

    def test_error_bracket_prefix_increments_counter(self):
        """[ERROR]-prefixed result increments the counter."""
        counter: dict = {}
        warning: list = [""]
        _process_result("[ERROR] path not allowed", "Write", {"file_path": "/etc/x"}, counter, warning)
        key = ("Write", hash("{'file_path': '/etc/x'}"[:200]))
        assert counter.get(key, 0) == 1

    def test_error_colon_prefix_increments_counter(self):
        """'Error:'-prefixed result (tool_read path check) increments the counter."""
        counter: dict = {}
        warning: list = [""]
        _process_result("Error: cannot resolve path", "Read", {"file_path": "/bad"}, counter, warning)
        key = ("Read", hash("{'file_path': '/bad'}"[:200]))
        assert counter.get(key, 0) == 1

    def test_counter_accumulates_across_calls(self):
        """Multiple consecutive failures on the same (tool, args) accumulate."""
        counter: dict = {}
        warning: list = [""]
        args = {"command": "exit 1"}
        for _ in range(2):
            _process_result("\u2717 [exit 1] err", "Bash", args, counter, warning)
        key = ("Bash", hash(str(args)[:200]))
        assert counter[key] == 2

    def test_different_args_use_separate_keys(self):
        """Failures with different args must not affect each other's counter."""
        counter: dict = {}
        warning: list = [""]
        _process_result("\u2717 [exit 1]", "Bash", {"command": "cmd_a"}, counter, warning)
        _process_result("\u2717 [exit 1]", "Bash", {"command": "cmd_b"}, counter, warning)
        key_a = ("Bash", hash("{'command': 'cmd_a'}"[:200]))
        key_b = ("Bash", hash("{'command': 'cmd_b'}"[:200]))
        assert counter.get(key_a, 0) == 1
        assert counter.get(key_b, 0) == 1

    def test_different_tools_use_separate_keys(self):
        """Failures on different tools must be tracked independently."""
        counter: dict = {}
        warning: list = [""]
        args = {"path": "/x"}
        _process_result("[ERROR] denied", "Write", args, counter, warning)
        _process_result("[ERROR] denied", "Edit", args, counter, warning)
        key_write = ("Write", hash(str(args)[:200]))
        key_edit = ("Edit", hash(str(args)[:200]))
        assert counter.get(key_write, 0) == 1
        assert counter.get(key_edit, 0) == 1


class TestRetryCounterResetsOnSuccess:
    """Counter must be removed (reset) when a success prefix is seen."""

    def test_checkmark_prefix_resets_counter(self):
        """✓-prefixed result removes the key from the counter."""
        counter: dict = {}
        warning: list = [""]
        args = {"command": "ls"}
        # Fail twice
        for _ in range(2):
            _process_result("\u2717 [exit 1]", "Bash", args, counter, warning)
        # Then succeed
        _process_result("\u2713 [exit 0] file.txt", "Bash", args, counter, warning)
        key = ("Bash", hash(str(args)[:200]))
        assert key not in counter, "Counter must be removed on success"

    def test_ok_bracket_prefix_resets_counter(self):
        """[OK]-prefixed result removes the key from the counter."""
        counter: dict = {}
        warning: list = [""]
        args = {"file_path": "/workspace/group/x.txt"}
        _process_result("[ERROR] not found", "Write", args, counter, warning)
        _process_result("[OK] Written: /workspace/group/x.txt", "Write", args, counter, warning)
        key = ("Write", hash(str(args)[:200]))
        assert key not in counter

    def test_success_does_not_affect_other_keys(self):
        """Resetting one (tool, args) key must not clear a different key's counter."""
        counter: dict = {}
        warning: list = [""]
        args_a = {"command": "cmd_a"}
        args_b = {"command": "cmd_b"}
        _process_result("\u2717 [exit 1]", "Bash", args_a, counter, warning)
        _process_result("\u2717 [exit 1]", "Bash", args_b, counter, warning)
        # Only succeed on args_a
        _process_result("\u2713 [exit 0]", "Bash", args_a, counter, warning)
        key_a = ("Bash", hash(str(args_a)[:200]))
        key_b = ("Bash", hash(str(args_b)[:200]))
        assert key_a not in counter, "Succeeded key must be removed"
        assert counter.get(key_b, 0) == 1, "Other key must be unaffected"

    def test_reset_on_success_after_max_reached(self):
        """Even after the warning threshold is reached, a success clears the counter."""
        counter: dict = {}
        warning: list = [""]
        args = {"command": "bad"}
        for _ in range(_MAX_CONSECUTIVE_TOOL_FAILS):
            _process_result("\u2717 [exit 1]", "Bash", args, counter, warning)
        # Warning should have been set
        assert warning[0] != ""
        # Now succeed
        _process_result("\u2713 [exit 0]", "Bash", args, counter, warning)
        key = ("Bash", hash(str(args)[:200]))
        assert key not in counter


class TestRetryCounterWarningThreshold:
    """Warning must be injected when consecutive fails >= _MAX_CONSECUTIVE_TOOL_FAILS."""

    def test_no_warning_before_threshold(self):
        """Warning must NOT be set if fail count is below the threshold."""
        counter: dict = {}
        warning: list = [""]
        args = {"command": "bad"}
        for _ in range(_MAX_CONSECUTIVE_TOOL_FAILS - 1):
            _process_result("\u2717 [exit 1]", "Bash", args, counter, warning)
        assert warning[0] == "", (
            f"Warning set too early at count={_MAX_CONSECUTIVE_TOOL_FAILS - 1}"
        )

    def test_warning_set_at_threshold(self):
        """Warning must be set when fail count reaches _MAX_CONSECUTIVE_TOOL_FAILS."""
        counter: dict = {}
        warning: list = [""]
        args = {"command": "bad_cmd"}
        for _ in range(_MAX_CONSECUTIVE_TOOL_FAILS):
            _process_result("\u2717 [exit 1]", "Bash", args, counter, warning)
        assert warning[0] != "", "Warning must be set at threshold"

    def test_warning_contains_tool_name(self):
        """The warning message must name the tool that is stuck in a retry loop."""
        counter: dict = {}
        warning: list = [""]
        args = {"file_path": "/workspace/group/test.txt"}
        for _ in range(_MAX_CONSECUTIVE_TOOL_FAILS):
            _process_result("[ERROR] denied", "Write", args, counter, warning)
        assert "Write" in warning[0], (
            f"Tool name 'Write' not found in warning: {warning[0]!r}"
        )

    def test_warning_contains_fail_count(self):
        """The warning message must include the consecutive fail count."""
        counter: dict = {}
        warning: list = [""]
        args = {"command": "x"}
        for _ in range(_MAX_CONSECUTIVE_TOOL_FAILS):
            _process_result("\u2717 [exit 1]", "Bash", args, counter, warning)
        assert str(_MAX_CONSECUTIVE_TOOL_FAILS) in warning[0], (
            f"Fail count {_MAX_CONSECUTIVE_TOOL_FAILS!r} not found in warning: {warning[0]!r}"
        )

    def test_warning_persists_on_repeated_failures_past_threshold(self):
        """Consecutive failures beyond the threshold must keep re-setting the warning."""
        counter: dict = {}
        warning: list = [""]
        args = {"command": "x"}
        for _ in range(_MAX_CONSECUTIVE_TOOL_FAILS + 2):
            _process_result("\u2717 [exit 1]", "Bash", args, counter, warning)
        assert warning[0] != "", "Warning must remain set after threshold is exceeded"

    def test_warning_cleared_after_success(self):
        """After a success the warning holder is cleared (reset to empty string)."""
        counter: dict = {}
        warning: list = [""]
        args = {"command": "x"}
        for _ in range(_MAX_CONSECUTIVE_TOOL_FAILS):
            _process_result("\u2717 [exit 1]", "Bash", args, counter, warning)
        assert warning[0] != ""
        _process_result("\u2713 [exit 0] ok", "Bash", args, counter, warning)
        assert warning[0] == "", "Warning must be cleared after success"


# ── Config alias test (Phase 21C) ─────────────────────────────────────────────

class TestAnthropicApiKeyAlias:
    """Phase 21C: ANTHROPIC_API_KEY must be promoted to CLAUDE_API_KEY at import time."""

    def test_anthropic_key_fallback_sets_claude_key(self, monkeypatch):
        """
        When only ANTHROPIC_API_KEY is set, importing host.config must
        copy its value into CLAUDE_API_KEY so downstream consumers (which
        read CLAUDE_API_KEY) pick up the correct key.
        """
        import importlib
        import os

        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-abc123")

        # Force a fresh import of host.config so the module-level alias code runs
        import host.config as cfg
        if "host.config" in sys.modules:
            del sys.modules["host.config"]

        # Re-import with the patched env
        import host.config as cfg_fresh
        assert os.environ.get("CLAUDE_API_KEY") == "test-key-abc123", (
            "CLAUDE_API_KEY must be populated from ANTHROPIC_API_KEY when "
            "the latter is set and the former is absent."
        )

    def test_anthropic_key_does_not_override_existing_claude_key(self, monkeypatch):
        """
        When CLAUDE_API_KEY is already set, ANTHROPIC_API_KEY must NOT
        overwrite it (the alias is a fallback only).
        """
        import os

        monkeypatch.setenv("CLAUDE_API_KEY", "original-key-xyz")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-override")

        if "host.config" in sys.modules:
            del sys.modules["host.config"]

        import host.config as cfg_fresh
        assert os.environ.get("CLAUDE_API_KEY") == "original-key-xyz", (
            "ANTHROPIC_API_KEY must not overwrite an existing CLAUDE_API_KEY."
        )

    def test_no_anthropic_key_leaves_claude_key_unset(self, monkeypatch):
        """
        When neither key is set, CLAUDE_API_KEY must remain absent after import.
        """
        import os

        monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        if "host.config" in sys.modules:
            del sys.modules["host.config"]

        import host.config as cfg_fresh
        assert os.environ.get("CLAUDE_API_KEY") is None, (
            "CLAUDE_API_KEY must not be set when ANTHROPIC_API_KEY is also absent."
        )

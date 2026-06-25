import pytest
import asyncio
import sys
import json
from pathlib import Path
from host.hooks_engine import (
    HookRegistry,
    HookDefinition,
    HookResult,
    _matches_tool,
    _run_hook_command,
    _execute_event_hooks,
    AggregatedHookResult,
)

def test_hook_registry_load_dict():
    registry = HookRegistry()
    config = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": "echo 'ok'", "timeout": 10, "async": False}
                ]
            }
        ],
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": "echo 'stopping'", "async": True, "once": True}
                ]
            }
        ]
    }
    registry.load_from_dict(config)
    assert registry.has_hooks("PreToolUse")
    assert registry.has_hooks("Stop")
    assert not registry.has_hooks("SessionStart")

    matchers = registry.get_matchers("PreToolUse")
    assert len(matchers) == 1
    assert matchers[0].matcher == "Bash"
    assert len(matchers[0].hooks) == 1
    assert matchers[0].hooks[0].command == "echo 'ok'"
    assert matchers[0].hooks[0].timeout == 10
    assert not matchers[0].hooks[0].async_run


def test_matches_tool():
    assert _matches_tool("", "Bash") is True
    assert _matches_tool("*", "Bash") is True
    assert _matches_tool("Bash", "Bash") is True
    assert _matches_tool("Write", "Bash") is False
    # Glob-like matching fallback
    assert _matches_tool("Bash(git *)", "Bash") is True


@pytest.mark.asyncio
async def test_run_hook_command_success():
    cmd = '"' + sys.executable + '" -c "import json; print(json.dumps({\'continue\': False, \'reason\': \'blocked\', \'hookSpecificOutput\': {\'updatedInput\': {\'key\': \'val\'}, \'additionalContext\': \'extra info\'}}))"'
    hook = HookDefinition(type="command", command=cmd, timeout=5)
    
    result = await _run_hook_command(hook, {"test": 123}, "test_hook")
    assert result.exit_code == 0
    assert result.continue_ is False
    assert result.reason == "blocked"
    assert result.updated_input == {"key": "val"}
    assert result.additional_context == "extra info"


@pytest.mark.asyncio
async def test_run_hook_command_timeout():
    # Command that sleeps longer than timeout
    cmd = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    hook = HookDefinition(type="command", command=cmd, timeout=1)
    
    result = await _run_hook_command(hook, {}, "test_timeout")
    assert result.exit_code != 0
    assert "timed out" in result.stderr


@pytest.mark.asyncio
async def test_execute_event_hooks_blocking():
    # A mock command that exits with code 2 (EXIT_BLOCK) to trigger blocking behavior
    cmd = f'"{sys.executable}" -c "import sys; sys.exit(2)"'
    
    registry = HookRegistry()
    config = {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": cmd, "timeout": 5}]
            }
        ]
    }
    registry.load_from_dict(config)
    
    agg = await _execute_event_hooks("PreToolUse", {}, "Bash", registry=registry)
    assert agg.is_blocked is True
    assert len(agg.blocking_errors) == 1
    assert "blocked (exit 2)" in agg.blocking_errors[0]

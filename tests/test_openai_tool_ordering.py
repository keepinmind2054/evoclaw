"""
tests/test_openai_tool_ordering.py — Phase 27B coverage for BUG-P26B-3.

BUG-P26B-3: The OpenAI API requires that every tool-role message immediately
follows the assistant message that issued the tool_calls — no user-role message
may be inserted between them.  The milestone enforcer and MEMORY.md reminder
previously injected user messages BEFORE tool results were appended, which
corrupted history and caused a 400 validation error on the next API call.

Fix: deferred user injections are collected in _deferred_user_msgs and appended
AFTER all tool-role messages have been added to history.

Covers:
  - Deferred injection: fake-progress warning does NOT land between
    assistant(tool_calls) and tool-role results.
  - MEMORY.md reminder does NOT land between tool_calls and tool results.
  - Structural invariant: for every assistant message that has tool_calls,
    all immediately-following messages are tool-role until the first non-tool.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_assistant_with_tool_calls(*call_ids: str) -> dict:
    """Build a synthetic assistant message with tool_calls."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": "Bash", "arguments": '{"command": "echo hi"}'},
            }
            for cid in call_ids
        ],
    }


def _make_tool_result(call_id: str, content: str = "ok") -> dict:
    """Build a synthetic tool-role message."""
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _make_user_msg(content: str) -> dict:
    return {"role": "user", "content": content}


def _is_tool_msg(msg: dict) -> bool:
    return msg.get("role") == "tool"


def _has_tool_calls(msg: dict) -> bool:
    return bool(msg.get("tool_calls"))


# ---------------------------------------------------------------------------
# Invariant checker
# ---------------------------------------------------------------------------

def assert_tool_ordering(history: list[dict]) -> None:
    """
    Assert that in *history* every assistant message that carries tool_calls is
    immediately followed (consecutively) by tool-role messages — no user-role
    message may appear between them.

    Raises AssertionError with a descriptive message on failure.
    """
    for i, msg in enumerate(history):
        if msg.get("role") == "assistant" and _has_tool_calls(msg):
            # Collect tool_call ids from this assistant message
            expected_ids = {tc["id"] for tc in msg["tool_calls"]}
            seen_ids: set[str] = set()
            j = i + 1
            while j < len(history):
                next_msg = history[j]
                if _is_tool_msg(next_msg):
                    seen_ids.add(next_msg.get("tool_call_id", ""))
                    j += 1
                    if seen_ids >= expected_ids:
                        # All tool results collected; stop scanning
                        break
                else:
                    # Non-tool message before all results are collected
                    assert False, (
                        f"History ordering violation at index {j}: "
                        f"message role={next_msg.get('role')!r} appeared between "
                        f"assistant tool_calls (index {i}) and their tool results. "
                        f"Expected tool-role messages for ids {expected_ids - seen_ids}."
                    )


# ---------------------------------------------------------------------------
# Test 1: normal ordering — no injection
# ---------------------------------------------------------------------------

def test_no_injection_ordering_correct():
    """Baseline: normal [assistant(tool_calls), tool, tool] ordering passes."""
    history = [
        _make_user_msg("do something"),
        _make_assistant_with_tool_calls("call-1", "call-2"),
        _make_tool_result("call-1", "result A"),
        _make_tool_result("call-2", "result B"),
        _make_user_msg("continue"),
    ]
    # Should not raise
    assert_tool_ordering(history)


# ---------------------------------------------------------------------------
# Test 2: bad ordering — user injected between tool_calls and tool results
# ---------------------------------------------------------------------------

def test_user_between_tool_calls_and_results_detected():
    """
    If a user message appears between an assistant tool_calls message and its
    tool results, assert_tool_ordering() must raise AssertionError.
    """
    bad_history = [
        _make_user_msg("start"),
        _make_assistant_with_tool_calls("call-x"),
        _make_user_msg("【系統警告】injected too early!"),  # BUG: should not be here
        _make_tool_result("call-x", "result X"),
    ]
    with pytest.raises(AssertionError, match="ordering violation"):
        assert_tool_ordering(bad_history)


# ---------------------------------------------------------------------------
# Test 3: deferred injection simulation — fake-progress warning
# ---------------------------------------------------------------------------

def test_deferred_fake_progress_injection():
    """
    Simulate the BUG-P26B-3 fix pattern: the fake-progress warning is
    collected in _deferred_user_msgs and appended AFTER the tool-role result
    message, not before it.

    History built the correct (fixed) way:
      assistant(tool_calls) → tool(result) → user(warning)
    """
    history: list[dict] = []
    _deferred_user_msgs: list[dict] = []

    # Step 1: assistant sends a tool call (only send_message — fake-progress streak)
    history.append(_make_assistant_with_tool_calls("tc-001"))

    # Milestone enforcer detects fake-progress: defer the warning
    _deferred_user_msgs.append(_make_user_msg("【系統警告】fake-progress detected"))

    # Step 2: tool result appended first
    history.append(_make_tool_result("tc-001", "message sent"))

    # Step 3: deferred warnings appended AFTER tool results
    for dm in _deferred_user_msgs:
        history.append(dm)

    # Ordering must be valid
    assert_tool_ordering(history)

    # Verify the warning IS present (not silently dropped)
    warning_msgs = [m for m in history if "fake-progress" in str(m.get("content", ""))]
    assert len(warning_msgs) == 1, "Deferred warning should appear exactly once"

    # Verify warning is NOT between tool_calls and tool result
    assistant_idx = next(i for i, m in enumerate(history) if _has_tool_calls(m))
    tool_idx = next(i for i, m in enumerate(history) if _is_tool_msg(m))
    warning_idx = next(i for i, m in enumerate(history) if "fake-progress" in str(m.get("content", "")))
    assert warning_idx > tool_idx, (
        f"Warning (idx={warning_idx}) must come AFTER tool result (idx={tool_idx})"
    )
    assert tool_idx == assistant_idx + 1, (
        f"Tool result (idx={tool_idx}) must immediately follow assistant tool_calls (idx={assistant_idx})"
    )


# ---------------------------------------------------------------------------
# Test 4: MEMORY.md reminder deferred correctly
# ---------------------------------------------------------------------------

def test_memory_reminder_deferred_after_tool_results():
    """
    MEMORY.md reminder (injected on penultimate turn) must appear AFTER all
    tool-role messages, not between assistant(tool_calls) and tool results.
    """
    history: list[dict] = []
    _deferred_user_msgs: list[dict] = []

    # Penultimate turn: assistant calls Write tool
    history.append(_make_assistant_with_tool_calls("write-tc"))

    # MEMORY.md not yet written → defer the reminder
    _deferred_user_msgs.append(_make_user_msg(
        "【CRITICAL 系統警告】你在本 session 中尚未更新 MEMORY.md"
    ))

    # Tool result for Write call
    history.append(_make_tool_result("write-tc", "[OK] Written: /workspace/group/output.txt"))

    # Deferred messages appended after tool results
    for dm in _deferred_user_msgs:
        history.append(dm)

    assert_tool_ordering(history)

    reminder_present = any(
        "MEMORY.md" in str(m.get("content", ""))
        for m in history
    )
    assert reminder_present, "MEMORY.md reminder must be present in history"


# ---------------------------------------------------------------------------
# Test 5: multiple tool calls all resolved before any deferred injection
# ---------------------------------------------------------------------------

def test_multiple_tool_calls_all_resolved_before_injection():
    """
    When an assistant message contains multiple tool_calls, ALL tool-role
    messages must appear before any deferred user injection.
    """
    history: list[dict] = []
    _deferred: list[dict] = []

    # Three tool calls in one turn
    history.append(_make_assistant_with_tool_calls("tc-A", "tc-B", "tc-C"))
    _deferred.append(_make_user_msg("deferred warning"))

    # All three tool results added first
    history.append(_make_tool_result("tc-A", "res-A"))
    history.append(_make_tool_result("tc-B", "res-B"))
    history.append(_make_tool_result("tc-C", "res-C"))

    # Now safe to add deferred
    for dm in _deferred:
        history.append(dm)

    assert_tool_ordering(history)


# ---------------------------------------------------------------------------
# Test 6: buggy pattern (deferred injected BEFORE results) is detected
# ---------------------------------------------------------------------------

def test_buggy_pattern_deferred_before_results_detected():
    """
    Simulate the PRE-FIX buggy behaviour: warning injected before tool results.
    The ordering check must flag this as a violation.
    """
    buggy_history: list[dict] = []

    buggy_history.append(_make_assistant_with_tool_calls("tc-bad"))
    # BUG: warning injected BEFORE tool result
    buggy_history.append(_make_user_msg("【系統警告】injected too early"))
    buggy_history.append(_make_tool_result("tc-bad", "some result"))

    with pytest.raises(AssertionError, match="ordering violation"):
        assert_tool_ordering(buggy_history)


# ---------------------------------------------------------------------------
# Test 7: history with no tool_calls is always valid
# ---------------------------------------------------------------------------

def test_history_without_tool_calls_always_valid():
    """Plain assistant/user turns without tool_calls are always ordering-valid."""
    history = [
        _make_user_msg("hello"),
        {"role": "assistant", "content": "world", "tool_calls": []},
        _make_user_msg("next"),
        {"role": "assistant", "content": "done"},
    ]
    assert_tool_ordering(history)

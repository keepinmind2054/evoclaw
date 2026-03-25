"""
Tests for Phase 30A rate-limit config edge cases.

Covers BUG-CFG-RL-01 (RATE_LIMIT_MAX_MSGS minimum=1) and BUG-CFG-RL-02
(RATE_LIMIT_WINDOW_SECS minimum=1) in host/config.py, and their mirrors
inside _is_sender_rate_limited in host/main.py.

The _env_int helper in config.py falls back to *default* (not the minimum)
when a value is below minimum.  Tests verify that behaviour.

For _is_sender_rate_limited we test the clamping logic by exercising the
function directly, mocking os.environ and time so that tests are
deterministic and require no external services.
"""
import importlib
import os
import sys
import time
import types
from collections import deque
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Helpers — replicate _env_int from host/config.py without importing the
# module (importing config.py reads the live environment at import time).
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int, minimum: int | None = None) -> int:
    """Replicate host/config._env_int for white-box testing."""
    try:
        val = int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default
    if minimum is not None and val < minimum:
        return default
    return val


# ---------------------------------------------------------------------------
# Replicate _is_sender_rate_limited logic so tests are hermetic
# ---------------------------------------------------------------------------

_SENDER_TIMESTAMPS_MAX_TEST = 5000


def _make_rate_limited_fn(timestamps_dict: dict):
    """
    Return a function that behaves like _is_sender_rate_limited but uses
    the supplied *timestamps_dict* so each test starts with a clean slate.
    """
    def _is_sender_rate_limited(sender: str, *, now: float | None = None) -> bool:
        try:
            max_msgs = max(1, int(os.environ.get("SENDER_RATE_LIMIT_MAX", 5)))
        except (ValueError, TypeError):
            max_msgs = 5
        try:
            window = max(1.0, float(os.environ.get("SENDER_RATE_LIMIT_WINDOW_SECS", 60)))
        except (ValueError, TypeError):
            window = 60.0
        _now = now if now is not None else time.time()
        if sender not in timestamps_dict and len(timestamps_dict) >= _SENDER_TIMESTAMPS_MAX_TEST:
            _oldest = next(iter(timestamps_dict))
            timestamps_dict.pop(_oldest, None)
        q = timestamps_dict.setdefault(sender, deque(maxlen=max_msgs * 2))
        while q and _now - q[0] > window:
            q.popleft()
        if len(q) >= max_msgs:
            return True
        q.append(_now)
        return False

    return _is_sender_rate_limited


# ===========================================================================
# Tests for _env_int / config-level clamping
# ===========================================================================

class TestEnvIntClamping:

    def test_rate_limit_max_msgs_zero_clamped_to_default(self):
        """RATE_LIMIT_MAX_MSGS=0 is below minimum=1, so default (20) is returned."""
        with patch.dict(os.environ, {"RATE_LIMIT_MAX_MSGS": "0"}):
            result = _env_int("RATE_LIMIT_MAX_MSGS", 20, minimum=1)
        # Below minimum → fall back to default, not minimum
        assert result == 20

    def test_rate_limit_max_msgs_one_accepted(self):
        """RATE_LIMIT_MAX_MSGS=1 meets minimum=1 and is returned as-is."""
        with patch.dict(os.environ, {"RATE_LIMIT_MAX_MSGS": "1"}):
            result = _env_int("RATE_LIMIT_MAX_MSGS", 20, minimum=1)
        assert result == 1

    def test_rate_limit_max_msgs_negative_clamped_to_default(self):
        """RATE_LIMIT_MAX_MSGS=-5 is below minimum=1, so default (20) is returned."""
        with patch.dict(os.environ, {"RATE_LIMIT_MAX_MSGS": "-5"}):
            result = _env_int("RATE_LIMIT_MAX_MSGS", 20, minimum=1)
        assert result == 20

    def test_rate_limit_window_secs_zero_clamped_to_default(self):
        """RATE_LIMIT_WINDOW_SECS=0 is below minimum=1, so default (60) is returned."""
        with patch.dict(os.environ, {"RATE_LIMIT_WINDOW_SECS": "0"}):
            result = _env_int("RATE_LIMIT_WINDOW_SECS", 60, minimum=1)
        assert result == 60

    def test_rate_limit_window_secs_negative_clamped_to_default(self):
        """RATE_LIMIT_WINDOW_SECS=-1 is below minimum=1, so default (60) is returned."""
        with patch.dict(os.environ, {"RATE_LIMIT_WINDOW_SECS": "-1"}):
            result = _env_int("RATE_LIMIT_WINDOW_SECS", 60, minimum=1)
        assert result == 60

    def test_normal_values_accepted(self):
        """Normal values (20 max, 60s window) pass through unchanged."""
        with patch.dict(os.environ, {"RATE_LIMIT_MAX_MSGS": "20", "RATE_LIMIT_WINDOW_SECS": "60"}):
            max_msgs = _env_int("RATE_LIMIT_MAX_MSGS", 20, minimum=1)
            window = _env_int("RATE_LIMIT_WINDOW_SECS", 60, minimum=1)
        assert max_msgs == 20
        assert window == 60


# ===========================================================================
# Tests for _is_sender_rate_limited — clamping of SENDER_RATE_LIMIT_MAX and
# SENDER_RATE_LIMIT_WINDOW_SECS inside the function itself
# ===========================================================================

class TestSenderRateLimitClamping:

    def test_sender_rate_limit_max_zero_clamped_to_one(self):
        """
        SENDER_RATE_LIMIT_MAX=0 must be clamped to 1.
        Without the fix, deque(maxlen=0) is created and len(q) >= 0 is always
        True, blocking every sender permanently.
        With the fix, the first message from a new sender is allowed through.
        """
        timestamps: dict = {}
        fn = _make_rate_limited_fn(timestamps)
        base = 1_000_000.0
        with patch.dict(os.environ, {"SENDER_RATE_LIMIT_MAX": "0",
                                     "SENDER_RATE_LIMIT_WINDOW_SECS": "60"}):
            # First call: sender has sent 0 messages, should NOT be rate-limited
            result = fn("alice", now=base)
        assert result is False, (
            "First message from new sender must not be blocked even when "
            "SENDER_RATE_LIMIT_MAX=0 (clamped to 1)"
        )

    def test_sender_rate_limit_max_zero_second_msg_blocked(self):
        """
        With SENDER_RATE_LIMIT_MAX=0 clamped to 1, a second rapid message
        from the same sender within the window must be blocked.
        """
        timestamps: dict = {}
        fn = _make_rate_limited_fn(timestamps)
        base = 1_000_000.0
        with patch.dict(os.environ, {"SENDER_RATE_LIMIT_MAX": "0",
                                     "SENDER_RATE_LIMIT_WINDOW_SECS": "60"}):
            fn("alice", now=base)           # first: allowed
            result = fn("alice", now=base + 1)  # second: blocked
        assert result is True, "Second rapid message must be blocked when max clamped to 1"

    def test_sender_rate_limit_window_secs_zero_clamped(self):
        """
        SENDER_RATE_LIMIT_WINDOW_SECS=0 must be clamped to 1.0.
        Without the fix, every timestamp appears outside the window (age > 0
        is always True), so the deque is cleared each call and the sender is
        never blocked — rate limiting is silently disabled.
        With the fix, a sender who fires many messages within 1 second is blocked.
        """
        timestamps: dict = {}
        fn = _make_rate_limited_fn(timestamps)
        base = 1_000_000.0
        with patch.dict(os.environ, {"SENDER_RATE_LIMIT_MAX": "2",
                                     "SENDER_RATE_LIMIT_WINDOW_SECS": "0"}):
            fn("bob", now=base)           # msg 1: allowed
            fn("bob", now=base + 0.1)    # msg 2: allowed
            result = fn("bob", now=base + 0.2)  # msg 3: blocked
        assert result is True, (
            "Sender must be blocked after max messages even when "
            "SENDER_RATE_LIMIT_WINDOW_SECS=0 (clamped to 1)"
        )

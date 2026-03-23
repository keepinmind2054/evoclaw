"""
Tests for Phase 21B exponential backoff cooldown formula in host/main.py.

The function _get_fail_cooldown(fail_count) is defined as:
    min(BASE * 2^max(0, fail_count-1), MAX)
where BASE=60.0 and MAX=600.0 (10 minutes).

Expected schedule:
  fail_count=1  →  60 * 2^0  =  60s
  fail_count=2  →  60 * 2^1  = 120s
  fail_count=3  →  60 * 2^2  = 240s  (NOT 300 — formula is 2^(n-1))
  fail_count=4  →  60 * 2^3  = 480s
  fail_count=5  →  60 * 2^4  = 960s  → capped at 600s
  fail_count=99 →  very large → capped at 600s

Note: the docstring in main.py says "60 → 120 → 300 → 600s" but the actual
formula produces 60 → 120 → 240 → 480 → 600 (capped). The tests below verify
the formula as implemented, not the summary comment.
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Import the function under test.
#
# host.main pulls in heavy optional dependencies (psutil via health_monitor,
# asyncio-based channels, etc.) that are absent in CI.  We avoid the full
# module import by extracting only the lines we need from main.py source via
# exec() into a minimal namespace.  This mirrors what the existing test suite
# does for host.ipc_watcher._compute_next_run (test_core.py) — that function
# is also at the top of a file with heavy transitive deps.
# ---------------------------------------------------------------------------
_MAIN_PY = Path(__file__).parent.parent / "host" / "main.py"

def _load_backoff_symbols():
    """
    Parse and exec just the constant definitions and _get_fail_cooldown
    function from host/main.py without triggering its full import chain.
    """
    src = _MAIN_PY.read_text(encoding="utf-8")
    lines = src.splitlines()

    # Collect lines from the top of the file up to (and including) the
    # _get_fail_cooldown function body.  We stop at the first blank line
    # after the function so we don't accidentally pull in code that imports
    # heavy deps.
    collected: list[str] = []
    in_fn = False
    fn_done = False
    for line in lines:
        stripped = line.strip()
        # Grab the constant definitions
        if stripped.startswith("_GROUP_FAIL_COOLDOWN_BASE") or \
           stripped.startswith("_GROUP_FAIL_COOLDOWN_MAX") or \
           stripped.startswith("_GROUP_MAX_FAILS"):
            collected.append(line)
            continue
        # Detect function start
        if stripped.startswith("def _get_fail_cooldown("):
            in_fn = True
        if in_fn:
            collected.append(line)
            # Function body is a single return statement on the very next line
            if fn_done:
                break
            if stripped.startswith("return ") and not stripped.startswith("def "):
                fn_done = True
            continue

    ns: dict = {}
    exec("\n".join(collected), ns)
    return ns

_backoff_ns = _load_backoff_symbols()
_get_fail_cooldown = _backoff_ns["_get_fail_cooldown"]
_GROUP_FAIL_COOLDOWN_BASE = _backoff_ns["_GROUP_FAIL_COOLDOWN_BASE"]
_GROUP_FAIL_COOLDOWN_MAX = _backoff_ns["_GROUP_FAIL_COOLDOWN_MAX"]


# ── Formula correctness ───────────────────────────────────────────────────────

class TestExponentialBackoffFormula:
    """Verify _get_fail_cooldown produces the correct exponential sequence."""

    def test_fail_count_1_returns_base(self):
        """First failure: cooldown equals the base interval (60 s)."""
        result = _get_fail_cooldown(1)
        expected = _GROUP_FAIL_COOLDOWN_BASE  # 60.0
        assert result == expected, f"fail_count=1 → expected {expected}, got {result}"

    def test_fail_count_2_doubles(self):
        """Second failure: cooldown doubles to 120 s."""
        result = _get_fail_cooldown(2)
        expected = _GROUP_FAIL_COOLDOWN_BASE * 2  # 120.0
        assert result == expected, f"fail_count=2 → expected {expected}, got {result}"

    def test_fail_count_3_quadruples(self):
        """Third failure: cooldown is 4x base = 240 s (formula: 60 * 2^2)."""
        result = _get_fail_cooldown(3)
        expected = _GROUP_FAIL_COOLDOWN_BASE * 4  # 240.0
        assert result == expected, f"fail_count=3 → expected {expected}, got {result}"

    def test_fail_count_4(self):
        """Fourth failure: cooldown is 8x base = 480 s (formula: 60 * 2^3)."""
        result = _get_fail_cooldown(4)
        expected = _GROUP_FAIL_COOLDOWN_BASE * 8  # 480.0
        assert result == expected, f"fail_count=4 → expected {expected}, got {result}"

    def test_fail_count_5_hits_cap(self):
        """Fifth failure: formula gives 960 s which is capped to MAX (600 s)."""
        result = _get_fail_cooldown(5)
        assert result == _GROUP_FAIL_COOLDOWN_MAX, (
            f"fail_count=5 → expected cap={_GROUP_FAIL_COOLDOWN_MAX}, got {result}"
        )

    def test_large_fail_count_capped(self):
        """Very large fail_count must never exceed the cap."""
        for n in (10, 50, 100, 999):
            result = _get_fail_cooldown(n)
            assert result == _GROUP_FAIL_COOLDOWN_MAX, (
                f"fail_count={n} → expected cap={_GROUP_FAIL_COOLDOWN_MAX}, got {result}"
            )

    def test_return_type_is_float(self):
        """Return value must be a float (used in time.sleep())."""
        for n in (1, 2, 5, 99):
            result = _get_fail_cooldown(n)
            assert isinstance(result, float), (
                f"fail_count={n} → expected float, got {type(result).__name__}"
            )

    def test_monotonically_increasing_until_cap(self):
        """Cooldown must be non-decreasing as fail_count grows."""
        values = [_get_fail_cooldown(n) for n in range(1, 10)]
        for i in range(len(values) - 1):
            assert values[i] <= values[i + 1], (
                f"Cooldown decreased from fail_count={i+1} ({values[i]}) "
                f"to fail_count={i+2} ({values[i+1]})"
            )

    def test_never_exceeds_max(self):
        """Cooldown must never exceed _GROUP_FAIL_COOLDOWN_MAX for any input."""
        for n in range(1, 20):
            assert _get_fail_cooldown(n) <= _GROUP_FAIL_COOLDOWN_MAX


# ── Constants sanity ──────────────────────────────────────────────────────────

class TestBackoffConstants:
    """Verify the module-level constants match the documented values."""

    def test_base_cooldown_is_60(self):
        assert _GROUP_FAIL_COOLDOWN_BASE == 60.0

    def test_max_cooldown_is_600(self):
        """Cap is documented as 10 minutes = 600 seconds."""
        assert _GROUP_FAIL_COOLDOWN_MAX == 600.0

    def test_cap_is_greater_than_base(self):
        assert _GROUP_FAIL_COOLDOWN_MAX > _GROUP_FAIL_COOLDOWN_BASE

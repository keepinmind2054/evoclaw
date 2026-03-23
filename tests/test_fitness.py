"""
Tests for host.evolution.fitness — fitness score computation.

Critical paths covered:
  - compute_fitness: returns 0.5 when < 3 samples (neutral value)
  - compute_fitness: BUG-FIX missing "success" key defaults to False (not 1)
  - compute_fitness: speed_score correct normalisation
  - compute_fitness: failed runs (response_ms=0) excluded from speed calc
  - compute_fitness: final score clamped to [0.0, 1.0]
  - compute_fitness: reliability = 1/(1 + avg_retries)
  - get_system_load: returns 0.0 when no recent runs
"""
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.evolution.fitness import compute_fitness, get_system_load, SPEED_TARGET_MS, SPEED_FLOOR_MS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_run(success=True, response_ms=5000, retry_count=0):
    return {"success": success, "response_ms": response_ms, "retry_count": retry_count}


# ── compute_fitness ────────────────────────────────────────────────────────────

class TestComputeFitness:
    def test_too_few_samples_returns_neutral(self):
        """Fewer than 3 samples returns 0.5 (neutral — no evolution decision)."""
        runs = [_make_run() for _ in range(2)]
        with patch("host.db.get_evolution_runs", return_value=runs):
            result = compute_fitness("tg:test")
        assert result == 0.5

    def test_zero_samples_returns_neutral(self):
        """No samples returns 0.5."""
        with patch("host.db.get_evolution_runs", return_value=[]):
            result = compute_fitness("tg:test")
        assert result == 0.5

    def test_all_successful_fast_runs_high_fitness(self):
        """All successes + fast responses → fitness close to 1.0."""
        runs = [_make_run(success=True, response_ms=SPEED_TARGET_MS - 1000, retry_count=0) for _ in range(5)]
        with patch("host.db.get_evolution_runs", return_value=runs):
            result = compute_fitness("tg:test")
        assert result > 0.8, f"Expected high fitness, got {result}"
        assert 0.0 <= result <= 1.0

    def test_all_failed_runs_low_fitness(self):
        """All failures → fitness close to 0.0 (only reliability component > 0)."""
        runs = [_make_run(success=False, response_ms=0, retry_count=0) for _ in range(5)]
        with patch("host.db.get_evolution_runs", return_value=runs):
            result = compute_fitness("tg:test")
        # success_rate=0, speed_score=0.5 (no valid times), reliability=1.0
        # fitness = 0*0.5 + 0.5*0.3 + 1.0*0.2 = 0.35
        assert result < 0.5, f"Expected low fitness for all-failed runs, got {result}"

    def test_missing_success_key_defaults_to_false(self):
        """BUG-FIX: a row missing 'success' key must count as failure, not success.

        Before the fix the default was 1 (truthy), so missing-key rows inflated
        the success_rate.  The fix uses default=False.
        """
        # 3 runs with no 'success' key and 2 explicit successes
        runs = [
            {"response_ms": 5000, "retry_count": 0},  # no 'success' key → False
            {"response_ms": 5000, "retry_count": 0},  # no 'success' key → False
            {"response_ms": 5000, "retry_count": 0},  # no 'success' key → False
            {"success": True, "response_ms": 5000, "retry_count": 0},
            {"success": True, "response_ms": 5000, "retry_count": 0},
        ]
        with patch("host.db.get_evolution_runs", return_value=runs):
            result = compute_fitness("tg:test")
        # success_rate should be 2/5 = 0.4 (not 5/5 = 1.0 as with old bug)
        # With old bug: fitness = 1.0*0.5 + speed*0.3 + 1.0*0.2 ≈ 0.85
        # With fix: fitness = 0.4*0.5 + speed*0.3 + 1.0*0.2 ≈ 0.56 (speed ≈ 0.5 if no valid times)
        assert result < 0.75, (
            f"Expected reduced fitness when 'success' key is missing (counted as False), got {result}"
        )

    def test_failed_runs_excluded_from_speed_calc(self):
        """Runs with success=False or response_ms=0 must not affect speed_score.

        Before the fix a failed run with response_ms=0 would score as perfect
        speed (0ms is below target), inflating the score.
        """
        # Mix: 3 failed runs (response_ms=0) + 3 slow successes
        runs = (
            [_make_run(success=False, response_ms=0) for _ in range(3)] +
            [_make_run(success=True, response_ms=SPEED_FLOOR_MS) for _ in range(3)]
        )
        with patch("host.db.get_evolution_runs", return_value=runs):
            result = compute_fitness("tg:test")

        # Speed should be 0.0 (avg_ms = SPEED_FLOOR_MS for valid times), not inflated by zeros
        # success_rate = 3/6 = 0.5
        # speed_score = 0.0 (30s responses)
        # reliability = 1.0
        # fitness = 0.5*0.5 + 0.0*0.3 + 1.0*0.2 = 0.45
        assert result < 0.5, (
            f"Expected failed runs' response_ms=0 to be excluded from speed calc, got {result}"
        )

    def test_high_retry_count_lowers_fitness(self):
        """Many retries should reduce the reliability component."""
        runs_no_retry = [_make_run(success=True, response_ms=5000, retry_count=0) for _ in range(5)]
        runs_high_retry = [_make_run(success=True, response_ms=5000, retry_count=10) for _ in range(5)]

        with patch("host.db.get_evolution_runs", return_value=runs_no_retry):
            fitness_reliable = compute_fitness("tg:test")

        with patch("host.db.get_evolution_runs", return_value=runs_high_retry):
            fitness_unreliable = compute_fitness("tg:test")

        assert fitness_reliable > fitness_unreliable, (
            "High retry count should produce lower fitness than zero retries"
        )

    def test_fitness_always_in_0_1_range(self):
        """compute_fitness must always return a value in [0.0, 1.0]."""
        # Edge cases: negative retry_count (corrupt DB row)
        corrupt_runs = [
            {"success": True, "response_ms": 1000, "retry_count": -5},
        ] * 5
        with patch("host.db.get_evolution_runs", return_value=corrupt_runs):
            result = compute_fitness("tg:test")
        assert 0.0 <= result <= 1.0, f"Fitness out of range: {result}"

    def test_db_exception_returns_neutral(self):
        """If the DB raises an exception, compute_fitness returns 0.5."""
        with patch("host.db.get_evolution_runs", side_effect=Exception("DB error")):
            result = compute_fitness("tg:test")
        assert result == 0.5

    def test_speed_score_at_target_is_one(self):
        """A response exactly at SPEED_TARGET_MS should give speed_score ≈ 1.0."""
        runs = [_make_run(success=True, response_ms=SPEED_TARGET_MS) for _ in range(5)]
        with patch("host.db.get_evolution_runs", return_value=runs):
            result = compute_fitness("tg:test")
        # speed_score = 1.0, success_rate = 1.0, reliability = 1.0
        # fitness = 1.0*0.5 + 1.0*0.3 + 1.0*0.2 = 1.0
        assert result == pytest.approx(1.0, abs=0.01)

    def test_speed_score_at_floor_is_zero(self):
        """A response at SPEED_FLOOR_MS should give speed_score ≈ 0.0."""
        runs = [_make_run(success=True, response_ms=SPEED_FLOOR_MS) for _ in range(5)]
        with patch("host.db.get_evolution_runs", return_value=runs):
            result = compute_fitness("tg:test")
        # speed_score = 0.0, success_rate = 1.0, reliability = 1.0
        # fitness = 1.0*0.5 + 0.0*0.3 + 1.0*0.2 = 0.7
        assert result == pytest.approx(0.7, abs=0.01)


# ── get_system_load ────────────────────────────────────────────────────────────

class TestGetSystemLoad:
    def test_no_recent_runs_returns_zero(self):
        """No recent activity → system load is 0.0."""
        with patch("host.db.get_recent_run_stats", return_value={"count": 0, "avg_ms": 0}):
            result = get_system_load()
        assert result == 0.0

    def test_db_exception_returns_zero(self):
        """DB error → safe default 0.0."""
        with patch("host.db.get_recent_run_stats", side_effect=Exception("DB down")):
            result = get_system_load()
        assert result == 0.0

    def test_high_concurrency_high_load(self):
        """20+ concurrent runs should push load close to 1.0."""
        with patch("host.db.get_recent_run_stats", return_value={"count": 20, "avg_ms": SPEED_FLOOR_MS}):
            result = get_system_load()
        assert result >= 0.9, f"Expected near-max load, got {result}"
        assert 0.0 <= result <= 1.0

    def test_load_always_in_0_1_range(self):
        """get_system_load must always return a value in [0.0, 1.0]."""
        with patch("host.db.get_recent_run_stats", return_value={"count": 1000, "avg_ms": 999999}):
            result = get_system_load()
        assert 0.0 <= result <= 1.0

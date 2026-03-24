"""
Tests for evolution safety fixes (Phase 24):

fitness.py — p24c retry_count clamp:
  A DB row with a negative retry_count would produce avg_retries < 0,
  making reliability > 1.0 and pushing the composite fitness above 1.0
  before the final clamp.  The fix clamps each retry_count to >= 0.

genome.py — p24c response_style normalisation:
  When the stored response_style is not one of the valid values
  ("concise", "balanced", "detailed"), the previous code carried the
  corrupted value forward via the `else` branch.  The fix normalises
  unknown values to "balanced" before index-lookup so the genome
  evolution always produces a valid output.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.evolution.fitness import compute_fitness, SPEED_TARGET_MS, SPEED_FLOOR_MS


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_run(success=True, response_ms=5000, retry_count=0):
    return {"success": success, "response_ms": response_ms, "retry_count": retry_count}


def _run_evolve_and_capture(genome_in: dict, fitness: float, avg_ms: float) -> dict:
    """
    Call evolve_genome_from_fitness with mocked DB helpers and return the
    kwargs passed to upsert_genome.
    """
    captured: dict = {}

    def fake_upsert(jid, **kwargs):
        captured.update(kwargs)

    def fake_get(jid):
        return dict(genome_in)

    import host.evolution.genome as genome_mod
    with patch.object(genome_mod, "upsert_genome", side_effect=fake_upsert), \
         patch.object(genome_mod, "get_genome", side_effect=fake_get), \
         patch("host.db.log_evolution_event", MagicMock(), create=True):
        genome_mod.evolve_genome_from_fitness("test-jid", fitness, avg_ms)

    return captured


# ── fitness.py: negative retry_count ─────────────────────────────────────────

class TestFitnessNegativeRetryCount:
    """compute_fitness must never return > 1.0 even with negative retry_count rows."""

    def test_negative_retry_count_score_not_above_1(self):
        """
        BUG-FIX p24c: a negative retry_count makes avg_retries negative, which
        makes reliability = 1 / (1 + avg_retries) > 1.0, pushing fitness above 1.0.
        The fix clamps each row's retry_count to max(0, ...) before averaging.
        """
        corrupt_runs = [
            _make_run(success=True, response_ms=SPEED_TARGET_MS, retry_count=-1)
            for _ in range(5)
        ]
        with patch("host.db.get_evolution_runs", return_value=corrupt_runs):
            result = compute_fitness("tg:test")

        assert result <= 1.0, (
            f"compute_fitness returned {result} > 1.0 with negative retry_count — "
            "the p24c clamp fix is not working"
        )

    def test_very_negative_retry_count_score_stays_in_range(self):
        """Even extreme negative retry_count values must not push fitness outside [0, 1]."""
        extreme_runs = [
            _make_run(success=True, response_ms=SPEED_TARGET_MS, retry_count=-1000)
            for _ in range(5)
        ]
        with patch("host.db.get_evolution_runs", return_value=extreme_runs):
            result = compute_fitness("tg:test")

        assert 0.0 <= result <= 1.0, (
            f"compute_fitness={result} is outside [0.0, 1.0] for extreme negative retry_count"
        )

    def test_negative_retry_count_treated_as_zero_retries(self):
        """
        A run with retry_count=-5 must be treated identically to retry_count=0
        for the reliability calculation (both are clamped to 0).
        """
        negative_runs = [_make_run(success=True, response_ms=5000, retry_count=-5) for _ in range(5)]
        zero_runs = [_make_run(success=True, response_ms=5000, retry_count=0) for _ in range(5)]

        with patch("host.db.get_evolution_runs", return_value=negative_runs):
            score_negative = compute_fitness("tg:test")

        with patch("host.db.get_evolution_runs", return_value=zero_runs):
            score_zero = compute_fitness("tg:test")

        assert score_negative == pytest.approx(score_zero, abs=1e-4), (
            f"Negative retry_count ({score_negative}) should score same as zero ({score_zero})"
        )

    def test_mixed_negative_and_positive_retry_count(self):
        """Mix of negative and positive retry_count rows must still score <= 1.0."""
        mixed_runs = [
            _make_run(success=True, response_ms=5000, retry_count=-3),
            _make_run(success=True, response_ms=5000, retry_count=2),
            _make_run(success=True, response_ms=5000, retry_count=-1),
            _make_run(success=True, response_ms=5000, retry_count=0),
            _make_run(success=True, response_ms=5000, retry_count=1),
        ]
        with patch("host.db.get_evolution_runs", return_value=mixed_runs):
            result = compute_fitness("tg:test")

        assert 0.0 <= result <= 1.0, f"Mixed retry_count fitness out of range: {result}"

    def test_all_negative_retry_counts_reliability_at_most_one(self):
        """When all retry_counts are negative, reliability must not exceed 1.0."""
        runs = [
            {"success": True, "response_ms": 5000, "retry_count": -10},
            {"success": True, "response_ms": 5000, "retry_count": -20},
            {"success": True, "response_ms": 5000, "retry_count": -30},
            {"success": True, "response_ms": 5000, "retry_count": -40},
            {"success": True, "response_ms": 5000, "retry_count": -50},
        ]
        # Manually compute expected: all clamped to 0 → avg_retries=0 → reliability=1.0
        # fitness = 1.0*0.5 + 1.0*0.3 + 1.0*0.2 = 1.0
        with patch("host.db.get_evolution_runs", return_value=runs):
            result = compute_fitness("tg:test")

        assert result == pytest.approx(1.0, abs=0.01), (
            f"All-negative retry_count should give fitness≈1.0 (clamped to 0), got {result}"
        )


# ── genome.py: unknown response_style normalisation ───────────────────────────

class TestGenomeUnknownResponseStyleNormalisation:
    """
    evolve_genome_from_fitness must normalise unknown response_style to
    "balanced" instead of carrying the corrupted value forward.
    """

    def test_unknown_style_normalised_to_valid_output(self):
        """
        BUG-FIX p24c: if the stored response_style is not in the valid set,
        it must be treated as "balanced" for the evolution step.
        The resulting response_style in the upserted genome must be valid.
        """
        corrupted_genome = {
            "response_style": "INVALID_STYLE",
            "formality": 0.5,
            "technical_depth": 0.5,
            "generation": 3,
        }
        result = _run_evolve_and_capture(corrupted_genome, fitness=0.6, avg_ms=8000)
        valid_styles = {"concise", "balanced", "detailed"}
        assert result.get("response_style") in valid_styles, (
            f"Unknown response_style should normalise to a valid value; got {result.get('response_style')!r}"
        )

    def test_unknown_style_normalises_to_balanced_under_neutral_conditions(self):
        """
        Under neutral conditions (fitness=0.55, avg_ms=8000) the evolution
        leaves style unchanged — so an unknown style normalised to 'balanced'
        must result in 'balanced' being persisted.
        """
        corrupted_genome = {
            "response_style": "garbage_value",
            "formality": 0.5,
            "technical_depth": 0.5,
            "generation": 0,
        }
        result = _run_evolve_and_capture(corrupted_genome, fitness=0.55, avg_ms=8000)
        # Neutral conditions → no style change → balanced stays balanced
        assert result.get("response_style") == "balanced", (
            f"Neutral conditions + unknown style should produce 'balanced', got {result.get('response_style')!r}"
        )

    def test_unknown_style_can_shift_to_concise_on_bad_conditions(self):
        """
        An unknown style normalised to 'balanced' can still shift to 'concise'
        when conditions are bad (slow + low fitness), demonstrating that
        normalisation happens before the evolution logic.
        """
        corrupted_genome = {
            "response_style": "NOT_A_STYLE",
            "formality": 0.5,
            "technical_depth": 0.5,
            "generation": 0,
        }
        # avg_ms > 15_000 and fitness < 0.4 → shift balanced → concise
        result = _run_evolve_and_capture(corrupted_genome, fitness=0.3, avg_ms=20000)
        assert result.get("response_style") == "concise", (
            f"Bad conditions + normalised-to-balanced style should produce 'concise', "
            f"got {result.get('response_style')!r}"
        )

    def test_unknown_style_can_shift_to_detailed_on_good_conditions(self):
        """
        An unknown style normalised to 'balanced' can shift to 'detailed'
        when conditions are good (fast + high fitness).
        """
        corrupted_genome = {
            "response_style": "??",
            "formality": 0.5,
            "technical_depth": 0.5,
            "generation": 0,
        }
        # avg_ms < 5_000 and fitness > 0.7 → shift balanced → detailed
        result = _run_evolve_and_capture(corrupted_genome, fitness=0.85, avg_ms=3000)
        assert result.get("response_style") == "detailed", (
            f"Good conditions + normalised-to-balanced style should produce 'detailed', "
            f"got {result.get('response_style')!r}"
        )

    def test_valid_response_style_not_affected_by_normalisation(self):
        """Valid response_style values must not be changed by normalisation."""
        for style in ("concise", "balanced", "detailed"):
            genome = {
                "response_style": style,
                "formality": 0.5,
                "technical_depth": 0.5,
                "generation": 0,
            }
            # Use neutral conditions so the style itself doesn't change
            result = _run_evolve_and_capture(genome, fitness=0.55, avg_ms=8000)
            assert result.get("response_style") == style, (
                f"Valid style {style!r} must be preserved under neutral conditions; "
                f"got {result.get('response_style')!r}"
            )

    def test_none_response_style_normalised(self):
        """A None response_style (missing from DB row) must be treated safely."""
        corrupted_genome = {
            "response_style": None,
            "formality": 0.5,
            "technical_depth": 0.5,
            "generation": 0,
        }
        # Should not raise; should produce a valid style
        result = _run_evolve_and_capture(corrupted_genome, fitness=0.55, avg_ms=8000)
        valid_styles = {"concise", "balanced", "detailed"}
        assert result.get("response_style") in valid_styles, (
            f"None response_style should normalise to a valid value; got {result.get('response_style')!r}"
        )

    def test_generation_increments_even_with_corrupted_style(self):
        """Generation counter must increment even when the input style is corrupted."""
        corrupted_genome = {
            "response_style": "CORRUPTED",
            "formality": 0.5,
            "technical_depth": 0.5,
            "generation": 7,
        }
        result = _run_evolve_and_capture(corrupted_genome, fitness=0.55, avg_ms=8000)
        assert result.get("generation") == 8, (
            f"Generation should increment from 7 to 8; got {result.get('generation')}"
        )

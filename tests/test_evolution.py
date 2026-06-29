"""Tests for the evolution system — genome evolution (response_style, formality, technical_depth)."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_genome(response_style="balanced", formality=0.5, technical_depth=0.5, generation=0):
    return {
        "response_style": response_style,
        "formality": formality,
        "technical_depth": technical_depth,
        "generation": generation,
    }


def _run_evolve(genome_in, fitness, avg_ms):
    """Run evolve_genome_from_fitness with mocked DB and return captured upsert kwargs."""
    from host.evolution.genome import evolve_genome_from_fitness
    import host.evolution.genome as genome_mod

    captured = {}

    def fake_upsert_with_event(jid, genome_fields, event_kwargs):
        captured.update(genome_fields)

    def fake_get(jid):
        return dict(genome_in)

    with patch.object(genome_mod, "get_genome", side_effect=fake_get), \
         patch("host.db.upsert_group_genome_with_event", side_effect=fake_upsert_with_event):
        evolve_genome_from_fitness("test-jid", fitness, avg_ms)

    return captured


# ── Test: response_style ───────────────────────────────────────────────────────

class TestEvolveResponseStyle:
    def test_evolve_response_style_concise_on_slow(self):
        """Slow + low fitness → shift toward concise."""
        genome = _make_genome(response_style="balanced")
        result = _run_evolve(genome, fitness=0.3, avg_ms=20000)
        assert result["response_style"] == "concise", (
            "Slow + low fitness should shift balanced → concise"
        )

    def test_evolve_response_style_detailed_on_fast(self):
        """Fast + high fitness → shift toward detailed."""
        genome = _make_genome(response_style="balanced")
        result = _run_evolve(genome, fitness=0.8, avg_ms=3000)
        assert result["response_style"] == "detailed", (
            "Fast + high fitness should shift balanced → detailed"
        )

    def test_response_style_unchanged_in_middle_conditions(self):
        """Moderate conditions should not change the style."""
        genome = _make_genome(response_style="balanced")
        result = _run_evolve(genome, fitness=0.55, avg_ms=8000)
        assert result["response_style"] == "balanced"

    def test_response_style_cannot_go_below_concise(self):
        """Already at concise — slow + low fitness should not move further down."""
        genome = _make_genome(response_style="concise")
        result = _run_evolve(genome, fitness=0.1, avg_ms=30000)
        assert result["response_style"] == "concise"

    def test_response_style_cannot_go_above_detailed(self):
        """Already at detailed — fast + high fitness should not move further up."""
        genome = _make_genome(response_style="detailed")
        result = _run_evolve(genome, fitness=0.95, avg_ms=1000)
        assert result["response_style"] == "detailed"


# ── Test: formality ────────────────────────────────────────────────────────────

class TestEvolveFormality:
    def test_formality_increases_on_high_fitness(self):
        """High fitness + fast → formality nudged up."""
        genome = _make_genome(formality=0.5)
        result = _run_evolve(genome, fitness=0.8, avg_ms=5000)
        assert result["formality"] > 0.5, "High fitness + fast should increase formality"

    def test_formality_nudges_toward_neutral_on_low_fitness(self):
        """Low fitness → formality approaches 0.5 (neutral)."""
        # Start above 0.5 — low fitness should pull it toward 0.5
        genome = _make_genome(formality=0.8)
        result = _run_evolve(genome, fitness=0.2, avg_ms=10000)
        assert result["formality"] < 0.8, "Low fitness should nudge high formality toward 0.5"

        # Start below 0.5 — low fitness should push it toward 0.5
        genome = _make_genome(formality=0.2)
        result = _run_evolve(genome, fitness=0.2, avg_ms=10000)
        assert result["formality"] > 0.2, "Low fitness should nudge low formality toward 0.5"

    def test_formality_clamped_at_max_1(self):
        """Formality should never exceed 1.0."""
        genome = _make_genome(formality=0.98)
        result = _run_evolve(genome, fitness=0.9, avg_ms=4000)
        assert result["formality"] <= 1.0

    def test_formality_clamped_at_min_0(self):
        """Formality should never go below 0.0."""
        genome = _make_genome(formality=0.0)
        # Low fitness nudges toward 0.5, so cannot go below 0
        result = _run_evolve(genome, fitness=0.2, avg_ms=10000)
        assert result["formality"] >= 0.0


# ── Test: technical_depth ─────────────────────────────────────────────────────

class TestEvolveTechnicalDepth:
    def test_technical_depth_increases_on_fast_high_fitness(self):
        """Fast + high fitness → technical_depth increases."""
        genome = _make_genome(technical_depth=0.5)
        result = _run_evolve(genome, fitness=0.8, avg_ms=4000)
        assert result["technical_depth"] > 0.5, (
            "Fast + high fitness should increase technical_depth"
        )

    def test_technical_depth_decreases_on_slow(self):
        """Very slow responses → technical_depth decreases."""
        genome = _make_genome(technical_depth=0.6)
        result = _run_evolve(genome, fitness=0.5, avg_ms=25000)
        assert result["technical_depth"] < 0.6, (
            "Very slow response should decrease technical_depth"
        )

    def test_technical_depth_decreases_on_very_low_fitness(self):
        """Very low fitness → technical_depth decreases."""
        genome = _make_genome(technical_depth=0.6)
        result = _run_evolve(genome, fitness=0.1, avg_ms=8000)
        assert result["technical_depth"] < 0.6

    def test_technical_depth_clamped_at_max_1(self):
        """technical_depth should never exceed 1.0."""
        genome = _make_genome(technical_depth=0.98)
        result = _run_evolve(genome, fitness=0.9, avg_ms=4000)
        assert result["technical_depth"] <= 1.0

    def test_technical_depth_clamped_at_min_0(self):
        """technical_depth should never go below 0.0."""
        genome = _make_genome(technical_depth=0.02)
        result = _run_evolve(genome, fitness=0.1, avg_ms=25000)
        assert result["technical_depth"] >= 0.0


# ── Test: clamping + rounding ─────────────────────────────────────────────────

class TestGenomeValueBounds:
    def test_genome_values_clamped_to_0_1(self):
        """Formality and technical_depth never go below 0 or above 1."""
        # Force extreme starting values — the function should clamp
        genome = _make_genome(formality=1.0, technical_depth=0.0)
        result = _run_evolve(genome, fitness=0.9, avg_ms=4000)
        assert 0.0 <= result["formality"] <= 1.0
        assert 0.0 <= result["technical_depth"] <= 1.0

        genome = _make_genome(formality=0.0, technical_depth=1.0)
        result = _run_evolve(genome, fitness=0.1, avg_ms=30000)
        assert 0.0 <= result["formality"] <= 1.0
        assert 0.0 <= result["technical_depth"] <= 1.0

    def test_generation_always_increments(self):
        """Generation counter should increment by 1 each evolution cycle."""
        genome = _make_genome(generation=5)
        result = _run_evolve(genome, fitness=0.5, avg_ms=8000)
        assert result["generation"] == 6


# ── Test: get_genome_style_hints reflects evolved values ─────────────────────

class TestGenomeStyleHints:
    def test_get_genome_style_hints_uses_evolved_values(self):
        """Style hints reflect formality and technical_depth values."""
        from host.evolution.adaptive import get_genome_style_hints

        high_formal_genome = _make_genome(formality=0.8, technical_depth=0.8, response_style="detailed")
        with patch("host.evolution.genome.get_genome", return_value=high_formal_genome):
            hints = get_genome_style_hints("test-jid")
        assert "正式" in hints or "專業" in hints, "High formality should produce formal hint"
        assert "技術" in hints or "深入" in hints or "術語" in hints, "High technical_depth should produce tech hint"

    def test_get_genome_style_hints_concise_style(self):
        """Concise style should appear in hints."""
        from host.evolution.adaptive import get_genome_style_hints

        concise_genome = _make_genome(response_style="concise", formality=0.5, technical_depth=0.5)
        with patch("host.evolution.genome.get_genome", return_value=concise_genome):
            hints = get_genome_style_hints("test-jid")
        assert "簡短" in hints or "精準" in hints, "Concise style should produce short-answer hint"

    def test_get_genome_style_hints_neutral_genome_empty(self):
        """Neutral genome (all 0.5) should produce no style hints for formality/depth."""
        from host.evolution.adaptive import get_genome_style_hints

        neutral_genome = _make_genome(response_style="balanced", formality=0.5, technical_depth=0.5)
        with patch("host.evolution.genome.get_genome", return_value=neutral_genome):
            hints = get_genome_style_hints("test-jid")
        # No strong bias — hints should be empty or minimal (no formality/depth triggers)
        assert "正式" not in hints
        assert "輕鬆" not in hints
        assert "技術" not in hints
        assert "白話" not in hints

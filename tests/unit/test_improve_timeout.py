# Unit tests for compute_run_timeout_s (src/api/improve.py) — CB-40: the background A/B budget that
# replaced the vestigial fixed 180s cap. Pure/network-free: only exercises the timeout arithmetic
# (scaling, floor, ceiling, monotonicity), NOT a real run. Collaborators: src.api.improve constants
# (_RUN_MAX_TURNS / _MIN_RUN_N / _MAX_RUN_N / _RUN_TIMEOUT_FLOOR_S / _RUN_TIMEOUT_CEILING_S).
from __future__ import annotations

import pytest

from src.api.improve import (
    _MAX_RUN_N,
    _MIN_RUN_N,
    _RUN_MAX_TURNS,
    _RUN_TIMEOUT_CEILING_S,
    _RUN_TIMEOUT_FLOOR_S,
    compute_run_timeout_s,
)


def test_scales_monotonically_with_n():
    """More personas per arm -> never a smaller budget (it is the same A/B run, only longer)."""
    # Use a small max_turns so the realistic n range stays in the scaling band (below the ceiling),
    # where the n-dependence is observable rather than clamped.
    budgets = [compute_run_timeout_s(n, max_turns=4) for n in range(_MIN_RUN_N, _MAX_RUN_N + 1)]
    assert budgets == sorted(budgets)
    # And strictly increases somewhere in the band (not a flat line pinned at the floor/ceiling).
    assert budgets[-1] > budgets[0]


def test_scales_monotonically_with_max_turns():
    """A longer per-episode turn cap -> never a smaller budget, at a fixed n."""
    n = 8
    budgets = [compute_run_timeout_s(n, max_turns=t) for t in (4, 8, 12, 16, 24)]
    assert budgets == sorted(budgets)
    assert budgets[-1] > budgets[0]


@pytest.mark.parametrize("n", [4, 5, 8, 12, _MAX_RUN_N])
def test_comfortably_above_old_180s_cap_for_real_runs(n):
    """The whole point of CB-40: a real run (n>=4) gets WAY more than the old fixed 180s, so an honest
    longer (less-pushy) A/B is no longer falsely killed. Confirmed-failing sizes were n=8 and n=4."""
    assert compute_run_timeout_s(n, _RUN_MAX_TURNS) > 180.0
    # Specifically beats it with margin, not by a hair.
    assert compute_run_timeout_s(n, _RUN_MAX_TURNS) >= 2 * 180.0


def test_floor_protects_tiny_runs():
    """A degenerate/tiny run still gets at least the floor — the budget never starves a short A/B and
    can never go negative for a 0/negative input (the floor wins)."""
    assert compute_run_timeout_s(0) == _RUN_TIMEOUT_FLOOR_S
    assert compute_run_timeout_s(-5) == _RUN_TIMEOUT_FLOOR_S
    assert compute_run_timeout_s(1, max_turns=1) == _RUN_TIMEOUT_FLOOR_S
    assert compute_run_timeout_s(0) >= _RUN_TIMEOUT_FLOOR_S


def test_capped_at_absolute_ceiling():
    """A runaway size is clamped to the absolute ceiling so a genuinely hung run still settles failed
    (CB-20) rather than holding the single concurrency permit forever."""
    assert compute_run_timeout_s(10_000, max_turns=10_000) == _RUN_TIMEOUT_CEILING_S
    # Never exceeds the ceiling for any input.
    for n in (5, 24, 100, 1000, 10_000):
        for t in (1, 24, 100, 1000):
            assert compute_run_timeout_s(n, max_turns=t) <= _RUN_TIMEOUT_CEILING_S


def test_floor_below_ceiling_invariant():
    """Sanity: the band is non-empty (floor < ceiling) so clamping is well-defined."""
    assert _RUN_TIMEOUT_FLOOR_S < _RUN_TIMEOUT_CEILING_S
    # And the floor is itself comfortably past the dead 180s cap.
    assert _RUN_TIMEOUT_FLOOR_S > 180.0


def test_estimate_matches_two_arm_turn_model_in_band():
    """In the scaling band the budget equals the n * 2 arms * max_turns * per-turn estimate (the
    documented model), so the number is explainable, not magic."""
    # Choose a point that lands strictly inside (floor, ceiling): n=8, max_turns=8, 3s/turn = 384s.
    val = compute_run_timeout_s(8, max_turns=8, seconds_per_turn=3.0, floor_s=300.0, ceiling_s=1800.0)
    assert val == pytest.approx(8 * 2 * 8 * 3.0)

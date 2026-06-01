# Integration tests for U11 — the driver-validation gate (src/loop/validation.py). DB-FREE: builds
# in-memory Episode/Turn/BeliefSnapshot trajectories and a real champion_v0 AgentConfig, then drives
# the empirical driver analysis (variance / collinearity / outcome-separation), the validated-driver
# set, the graceful-degradation config wiring (R6), the fallback-experiment selector (R38), and the
# inter-annotator agreement helper. The dropped-driver tests exercise the REAL src.core.gates so the
# fallback trigger (discovery-slots/asked-price for price_gate; consecutive-pressure COUNT for the
# pushiness_cap) is shown to carry the gate when its driver path is neutralized — no change to gates.
from __future__ import annotations

import math
from typing import Optional

from src.config.settings import load_config
from src.core import gates
from src.core.belief_state import DRIVERS
from src.core.policy import Decision
from src.loop import validation
from src.memory.schema import BeliefSnapshot, Episode, Turn

# Per-driver phase offsets give each driver an INDEPENDENT (low mutual-correlation) wiggle on top of
# its outcome-separating base level, so a healthy baseline keeps all six (no spurious collinearity).
_PHASE = {d: i for i, d in enumerate(DRIVERS)}


# --- in-memory episode builders ----------------------------------------------------------------


def _belief(driver_values: dict[str, float]) -> BeliefSnapshot:
    """A snapshot whose six drivers are the given dict (missing ones default to the 0.5 neutral)."""
    drivers = {d: 0.5 for d in DRIVERS}
    drivers.update(driver_values)
    return BeliefSnapshot(drivers=drivers)


def _episode_from_trajectory(
    eid: str,
    trajectory: list[dict[str, float]],
    *,
    ladder_tier: int,
) -> Episode:
    """An episode whose agent turns carry the given per-turn driver dicts; ladder_tier sets the
    binary commit outcome (>=1 committed) the outcome-separation point-biserial reads."""
    turns: list[Turn] = []
    for i, driver_values in enumerate(trajectory):
        turns.append(
            Turn(
                turn_id=i,
                speaker="agent",
                text=f"turn {i}",
                decision="ask",
                belief=_belief(driver_values),
            )
        )
    return Episode(
        episode_id=eid,
        turns=turns,
        outcome="consult_booked" if ladder_tier >= 1 else "walked",
        ladder_tier=ladder_tier,
        qualified=True,
        version="champion_v0",
        kb_version="kb_v0",
        channel="sim",
    )


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _healthy_driver_value(driver: str, k: int, t: int, committed: bool) -> float:
    """A per-(driver, episode k, turn t) level that is healthy on ALL THREE axes: it MOVES (the
    per-driver sinusoidal wiggle gives variance), it is INDEPENDENT (a distinct base + phase +
    frequency per driver keeps mutual |Pearson| well under the collinearity threshold), and it
    SEPARATES outcomes (a small committed-vs-walked shift moves the per-episode mean apart). The
    large per-episode wiggle dominates the shared outcome shift, so the drivers stay un-collinear."""
    p = _PHASE[driver]
    base = 0.35 + 0.04 * p + (0.06 if committed else 0.0)  # distinct per-driver base + outcome shift
    wiggle = 0.22 * math.sin((p + 1) * 0.8 + k * (1.0 + 0.3 * p) + t * 1.1)
    return _clamp01(base + wiggle)


def _varied_episodes() -> list[Episode]:
    """A baseline batch where every driver MOVES, is independent, and separates outcomes — so a
    healthy driver is KEPT (the control against which the failure scenarios are injected)."""
    episodes: list[Episode] = []
    n_turns = 5
    # Five committed episodes (tier>=1) and five walked (tier 0); each driver gets its own pattern.
    for k in range(5):
        traj = [
            {d: _healthy_driver_value(d, k, t, committed=True) for d in DRIVERS}
            for t in range(n_turns)
        ]
        episodes.append(_episode_from_trajectory(f"commit_{k}", traj, ladder_tier=2))
    for k in range(5):
        traj = [
            {d: _healthy_driver_value(d, k + 100, t, committed=False) for d in DRIVERS}
            for t in range(n_turns)
        ]
        episodes.append(_episode_from_trajectory(f"walk_{k}", traj, ladder_tier=0))
    return episodes


# --- 1. collinearity ---------------------------------------------------------------------------


def test_collinear_driver_is_flagged_and_excluded():
    """When `urgency` moves in PERFECT lockstep with need_intensity, the analysis flags it COLLINEAR
    and DROPS the provisional one (urgency) — it is absent from the validated set, present in dropped,
    and carries a 'collinear' reason naming its partner."""
    episodes: list[Episode] = []
    # Build 10 episodes where urgency == need_intensity EXACTLY (perfect correlation), while the
    # other drivers carry independent, outcome-separating signal so ONLY urgency is redundant.
    for k in range(10):
        committed = k < 5
        traj = []
        for t in range(5):
            vals = {
                d: _healthy_driver_value(d, k if committed else k + 100, t, committed=committed)
                for d in DRIVERS
            }
            vals["urgency"] = vals["need_intensity"]  # PERFECT lockstep with need_intensity
            traj.append(vals)
        episodes.append(
            _episode_from_trajectory(f"ep_{k}", traj, ladder_tier=2 if committed else 0)
        )

    report = validation.analyze_drivers(episodes)

    assert "urgency" in report.dropped, f"urgency should be dropped, kept={report.kept}"
    assert "urgency" not in report.kept
    assert "urgency" not in validation.validated_drivers(report)
    assert "urgency" in validation.dropped_drivers(report)
    # need_intensity (the partner it's collinear WITH) is kept — only the provisional one is dropped.
    assert "need_intensity" in report.kept
    reasons = " ".join(report.stats["urgency"].reasons).lower()
    assert "collinear" in reasons
    assert report.stats["urgency"].most_correlated_with == "need_intensity"


# --- 2. zero variance --------------------------------------------------------------------------


def test_zero_variance_driver_is_flagged():
    """A driver pinned to a constant across every turn of every episode carries NO signal and is
    DROPPED with a low-signal/variance reason."""
    episodes = _varied_episodes()
    # Overwrite price_sensitivity to a fixed constant everywhere -> zero variance.
    for ep in episodes:
        for turn in ep.turns:
            turn.belief.drivers["price_sensitivity"] = 0.5

    report = validation.analyze_drivers(episodes)

    assert "price_sensitivity" in report.dropped
    assert "price_sensitivity" not in validation.validated_drivers(report)
    stats = report.stats["price_sensitivity"]
    assert stats.variance < 1e-4
    reasons = " ".join(stats.reasons).lower()
    assert "variance" in reasons or "signal" in reasons


# --- 3. outcome separation ---------------------------------------------------------------------


def test_non_separating_driver_is_flagged():
    """A driver whose per-episode mean is IDENTICAL for committed and walked episodes does not
    distinguish good from bad calls -> DROPPED with a non-separating reason (point-biserial ~0)."""
    episodes = _varied_episodes()
    # Make purchase_intent vary turn-to-turn (so it has variance) but with the SAME episode-mean
    # for every episode regardless of outcome -> zero outcome separation.
    for ep in episodes:
        for i, turn in enumerate(ep.turns):
            turn.belief.drivers["purchase_intent"] = 0.4 if i % 2 == 0 else 0.6  # mean 0.5 always

    report = validation.analyze_drivers(episodes)

    assert "purchase_intent" in report.dropped
    assert "purchase_intent" not in validation.validated_drivers(report)
    stats = report.stats["purchase_intent"]
    assert stats.variance >= 1e-4  # it DOES move (so not flagged for variance) ...
    assert abs(stats.outcome_separation) < 0.1  # ... but it does NOT separate outcomes
    reasons = " ".join(stats.reasons).lower()
    assert "separat" in reasons


# --- 4. graceful degradation: REAL gates still function with a dropped driver (R6) -------------


def _drop_report(dropped: list[str]) -> validation.DriverValidationReport:
    """A minimal hand-built report dropping the named drivers (kept = the rest), for config wiring
    tests that don't need the full analysis."""
    kept = [d for d in DRIVERS if d not in dropped]
    stats = {
        d: validation.DriverStats(
            name=d,
            variance=0.05,
            max_abs_corr=0.0,
            most_correlated_with=None,
            outcome_separation=0.5,
            kept=d not in dropped,
            reasons=[] if d not in dropped else ["test-dropped"],
        )
        for d in DRIVERS
    }
    return validation.DriverValidationReport(
        kept=kept,
        dropped=list(dropped),
        stats=stats,
        fallback_experiment=validation._fallback_for(dropped),
    )


def test_gates_still_function_with_dropped_driver():
    """Dropping `trust` neutralizes the trust PATH of the REAL price_gate (threshold pushed to an
    unreachable 1.01) yet the gate still OPENS via the discovery-slots / asked-price fallback trigger;
    dropping `bail_risk` neutralizes the pushiness SIGNAL yet the pushiness_cap still trips via the
    consecutive-pressure COUNT. Exercises src.core.gates directly — gates.py is unchanged."""
    config = load_config("champion_v0")

    # --- drop trust: price_gate must NOT open via the (now unreachable) trust path ---
    no_trust = validation.apply_validation_to_config(config, _drop_report(["trust"]))
    # input config is never mutated (a COPY is returned).
    assert config.thresholds.get("trust_gate_open_price", 0.5) <= 1.0
    assert no_trust.thresholds["trust_gate_open_price"] > 1.0  # unreachable -> trust path dead

    # A belief with even a HIGH trust no longer opens price via trust (no asked-price, no slots).
    high_trust = validation._belief_state_with(trust=1.0)  # below 1.01 -> trust path can't fire
    price_ask = Decision(act="ask", target_slot="budget")
    via_trust = gates.price_gate(price_ask.copy(), high_trust, no_trust)
    assert via_trust.act == "pitch", "trust path is dead -> price_gate stays closed, re-anchors value"

    # ... but the SAME gate OPENS once the required discovery slots are filled (fallback trigger).
    filled = validation._belief_state_with(trust=0.0)
    required = gates._required_discovery_slots(no_trust)
    need = int(gates._threshold(no_trust, "discovery_slots_required"))
    for slot in required[:need]:
        filled.set_slot(slot, "x", 0.95)
    via_slots = gates.price_gate(price_ask.copy(), filled, no_trust)
    assert via_slots.act == "ask" and via_slots.target_slot == "budget", (
        "discovery-slots fallback should carry the price_gate open with trust dropped"
    )

    # ... and ALSO opens when the prospect asked price unprompted (the other fallback trigger).
    asked = validation._belief_state_with(trust=0.0)
    asked.last_user_act = "price_inquiry"
    via_asked = gates.price_gate(price_ask.copy(), asked, no_trust)
    assert via_asked.act == "ask", "asked-price fallback should carry the price_gate open"

    # --- drop bail_risk: pushiness_cap must rely on the consecutive-pressure COUNT ---
    no_bail = validation.apply_validation_to_config(config, _drop_report(["bail_risk"]))
    assert no_bail.thresholds["pushiness_cap"] > 1.0  # signal path unreachable

    # High bail_risk no longer trips the cap via the (dead) signal path, with NO pressure history.
    pushy = validation._belief_state_with(bail_risk=1.0)
    close = Decision(act="attempt_close", tier="trial")
    no_history = gates.pushiness_cap(close.copy(), pushy, no_bail, history=[])
    assert no_history.act == "attempt_close", "bail_risk signal is dead -> cap not tripped by signal"

    # ... but the cap DOES trip once consecutive pressure exceeds the COUNT cap (fallback trigger).
    count_cap = int(gates._threshold(no_bail, "pushiness_pressure_count_cap"))
    history = [{"role": "assistant", "act": "attempt_close"} for _ in range(count_cap)]
    calm = validation._belief_state_with(bail_risk=0.0)
    via_count = gates.pushiness_cap(close.copy(), calm, no_bail, history=history)
    assert via_count.act != "attempt_close", (
        "consecutive-pressure COUNT fallback should carry the pushiness_cap with bail_risk dropped"
    )


# --- 5. fallback experiment (R38) --------------------------------------------------------------


def test_fallback_experiment_selectable():
    """If `trust` is dropped the discovery-sequencing experiment can't be trust-grounded, so the
    fallback is 'price_sensitivity_grounded'; otherwise the default 'discovery_sequencing'."""
    trust_dropped = _drop_report(["trust"])
    assert validation.fallback_experiment(trust_dropped) == "price_sensitivity_grounded"
    assert trust_dropped.fallback_experiment == "price_sensitivity_grounded"

    trust_kept = _drop_report(["urgency"])  # trust survives
    assert validation.fallback_experiment(trust_kept) == "discovery_sequencing"
    assert trust_kept.fallback_experiment == "discovery_sequencing"


# --- 6. the report enumerates kept vs dropped with per-driver stats (the U11 verification) -----


def test_report_lists_kept_and_dropped():
    """The validation report enumerates kept vs dropped drivers and carries per-driver DriverStats
    for ALL six — the U11 verification artifact ('validation report lists kept vs dropped')."""
    report = validation.analyze_drivers(_varied_episodes())

    # every driver appears exactly once across kept|dropped, and has a stats entry.
    assert set(report.kept) | set(report.dropped) == set(DRIVERS)
    assert not (set(report.kept) & set(report.dropped)), "a driver can't be both kept and dropped"
    assert set(report.stats.keys()) == set(DRIVERS)
    for name, stats in report.stats.items():
        assert stats.name == name
        assert stats.kept == (name in report.kept)
        if not stats.kept:
            assert stats.reasons, f"a dropped driver must carry at least one reason: {name}"
    # In the healthy baseline every driver moves, is independent, and separates -> all kept.
    assert report.kept and not report.dropped, "healthy baseline should keep all six drivers"


# --- 7. inter-annotator agreement (R38) --------------------------------------------------------


def test_inter_annotator_agreement():
    """Identical hand-label lists give kappa 1.0 and agreement_ok True; divergent labels give a
    lower kappa that may not clear the min — reusing src.loop._stats.cohens_kappa (no reimplementation)."""
    perfect_a = ["calm", "anxious", "anxious", "neutral", "calm", "neutral"]
    perfect_b = list(perfect_a)
    assert validation.inter_annotator_agreement(perfect_a, perfect_b) == 1.0
    assert validation.annotation_agreement_ok(perfect_a, perfect_b) is True

    # Divergent labels: 3 of 6 disagree -> kappa well below 1, likely below the 0.6 default.
    divergent_b = ["anxious", "calm", "neutral", "anxious", "neutral", "calm"]
    kappa = validation.inter_annotator_agreement(perfect_a, divergent_b)
    assert kappa < 1.0
    assert validation.annotation_agreement_ok(perfect_a, divergent_b, min_kappa=0.6) is False

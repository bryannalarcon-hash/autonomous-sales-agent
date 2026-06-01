# Integration tests for U10 — the improvement loop + PROMOTION GATE (integrity core of the
# self-improving agent: src/loop/{generator,experiment,promotion}.py). TEST-FIRST on the gate: most
# tests are DB-FREE — they inject synthetic ExperimentResults (hand-built ComparisonResult +
# GuardrailReports + qual accuracies) so the gate runs as a PURE function (no self-play, no network),
# proving the decision ORDER + `passed` predicate: AE5 clean non-extreme auto-promotes; AE6
# passing-but-extreme blocks for human approval; guardrail/qualification regression or absent lift is
# rejected; over-threshold sim-to-real divergence pauses (R36); minimal-diff containment (R21) accepts
# one-dim and rejects multi-dim; frozen-set rotation excludes R22-mined personas. DB-gated end-to-end
# tests run run_improvement_round with a deterministic dominating runner (mirrors test_store.py's
# _schema fixture) and assert the challenger promotes + becomes the stored champion (U10 verification).
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config.settings import AgentConfig, Persona, load_config
from src.loop import experiment as experiment_mod
from src.loop import generator as generator_mod
from src.loop import promotion as promotion_mod
from src.loop.experiment import (
    ExperimentResult,
    frozen_held_out,
    qualification_accuracy,
    rotate,
)
from src.loop.generator import (
    declared_diff,
    is_minimal_diff,
    mutate_threshold,
    reorder_discovery,
)
from src.loop.grading import ComparisonResult, GuardrailReport
from src.loop.promotion import evaluate_promotion
from src.memory.schema import BeliefSnapshot, Episode, Turn


# --- Synthetic ExperimentResult builders (PURE — no self-play, no DB) --------------------------
# The gate reads ONLY a ComparisonResult.challenger_better, two GuardrailReports, and two qual
# accuracies; we fabricate each directly so the gate's decision logic is tested in isolation.


def _comparison(*, challenger_better: bool, delta: float = 1.0) -> ComparisonResult:
    """A hand-built ComparisonResult carrying just what the gate reads (challenger_better) plus
    plausible KPI/CI fields. delta>0 with a CI excluding 0 mirrors a real significant lift."""
    lo = 0.1 if challenger_better else -0.5
    return ComparisonResult(
        champion_kpi=2.0,
        challenger_kpi=2.0 + delta,
        delta=delta,
        champion_ci=(1.8, 2.2),
        challenger_ci=(2.6, 3.4),
        delta_ci=(lo, lo + 1.0),
        challenger_better=challenger_better,
        n_champion=12,
        n_challenger=12,
    )


def _guardrails(*, pushiness_rate: float = 0.0, false_promise_flags: int = 0) -> GuardrailReport:
    return GuardrailReport(
        pushiness_rate=pushiness_rate,
        pushiness_violation_turns=[],
        false_promise_flags=false_promise_flags,
        escalation_appropriate=True,
        pushiness_over_cap=pushiness_rate > 0.0,
        agent_turns=10,
    )


def _experiment(
    *,
    challenger_better: bool,
    champ_guardrails: GuardrailReport,
    chal_guardrails: GuardrailReport,
    champ_qual_acc: float,
    chal_qual_acc: float,
    dimension: str = "playbooks.discovery_sequence",
) -> ExperimentResult:
    return ExperimentResult(
        dimension=dimension,
        comparison=_comparison(challenger_better=challenger_better),
        champion_guardrails=champ_guardrails,
        challenger_guardrails=chal_guardrails,
        champion_qual_acc=champ_qual_acc,
        challenger_qual_acc=chal_qual_acc,
        champion_eps=[],
        challenger_eps=[],
    )


def _clean_passing_experiment(**kw) -> ExperimentResult:
    """A challenger that clears EVERY criterion: significant lift, no guardrail regression, qual held.
    Override individual axes via kwargs to break exactly one criterion in a test."""
    defaults = dict(
        challenger_better=True,
        champ_guardrails=_guardrails(pushiness_rate=0.1, false_promise_flags=1),
        chal_guardrails=_guardrails(pushiness_rate=0.05, false_promise_flags=0),  # equal-or-better
        champ_qual_acc=0.80,
        chal_qual_acc=0.82,  # held / improved
    )
    defaults.update(kw)
    return _experiment(**defaults)


# === 1. THE PROMOTION GATE (the integrity core) — pure-function tests, no self-play ============


def test_clean_nonextreme_challenger_auto_promotes():
    """AE5: a non-extreme challenger that clears the bar (significant lift, guardrails clean, qual
    held) auto-promotes with post-hoc review — status 'promoted', promote=True, no human needed."""
    decision = evaluate_promotion(_clean_passing_experiment(), is_extreme=False)
    assert decision.status == "promoted"
    assert decision.promote is True
    assert decision.requires_human is False


def test_extreme_pricing_challenger_blocks_for_approval():
    """AE6: the SAME passing metrics but the diff is extreme (pricing concession / persona) -> the
    gate blocks for a human ('pending_approval', requires_human=True, promote=False)."""
    decision = evaluate_promotion(_clean_passing_experiment(), is_extreme=True)
    assert decision.status == "pending_approval"
    assert decision.requires_human is True
    assert decision.promote is False


def test_guardrail_regression_blocks_promotion():
    """A challenger with a real lift but that REGRESSES a guardrail (pushes harder) is rejected even
    though challenger_better is True — guardrails are a hard, non-tradeable gate (R24)."""
    exp = _clean_passing_experiment(
        chal_guardrails=_guardrails(pushiness_rate=0.5, false_promise_flags=0),  # pushed MORE
    )
    decision = evaluate_promotion(exp, is_extreme=False)
    assert decision.status == "rejected"
    assert decision.promote is False
    assert any("guardrail" in r.lower() for r in decision.reasons)


def test_qualification_regression_blocks_promotion():
    """A lift + clean guardrails but a qualification-accuracy DROP beyond tolerance is rejected — the
    agent must not learn to 'win' by misqualifying prospects (R29)."""
    exp = _clean_passing_experiment(champ_qual_acc=0.90, chal_qual_acc=0.70)  # 0.20 drop >> tol
    decision = evaluate_promotion(exp, is_extreme=False)
    assert decision.status == "rejected"
    assert decision.promote is False
    assert any("qualif" in r.lower() for r in decision.reasons)


def test_no_significant_lift_is_rejected():
    """challenger_better=False (the delta CI includes 0 -> not statistically separated from noise)
    is rejected regardless of everything else — the over-claim-resistant core of the bar (R20)."""
    exp = _clean_passing_experiment(challenger_better=False)
    decision = evaluate_promotion(exp, is_extreme=False)
    assert decision.status == "rejected"
    assert decision.promote is False
    assert any("lift" in r.lower() for r in decision.reasons)


def test_sim_to_real_divergence_pauses_loop():
    """R36: when sim and real disagree beyond the divergence threshold the loop PAUSES — nothing
    promotes even with otherwise-passing metrics. The divergence check runs FIRST, before the bar."""
    exp = _clean_passing_experiment()  # would otherwise promote
    decision = evaluate_promotion(exp, is_extreme=False, divergence=0.5)  # > 0.2 default
    assert decision.status == "paused"
    assert decision.promote is False
    assert any("diverg" in r.lower() for r in decision.reasons)


def test_divergence_within_threshold_does_not_pause():
    """Divergence at/under the threshold (or measured but small) does NOT pause; the normal bar runs
    and a clean non-extreme challenger still promotes."""
    exp = _clean_passing_experiment()
    decision = evaluate_promotion(exp, is_extreme=False, divergence=0.1)  # <= 0.2
    assert decision.status == "promoted"
    assert decision.promote is True


def test_divergence_none_skips_check():
    """divergence=None means 'not yet measured' (U14 feeds it later) -> the pause check is skipped
    and the normal bar decides."""
    exp = _clean_passing_experiment()
    decision = evaluate_promotion(exp, is_extreme=False, divergence=None)
    assert decision.status == "promoted"


# === 2. Minimal-diff containment (R21) =========================================================


def _config_with(*, thresholds=None, playbooks=None, prompts=None, persona=None) -> AgentConfig:
    """A champion-derived AgentConfig overriding only the named sub-maps (for diff tests)."""
    base = load_config("champion_v0")
    return AgentConfig(
        version=base.version,
        kb_version=base.kb_version,
        persona=persona if persona is not None else base.persona,
        prompts=prompts if prompts is not None else dict(base.prompts),
        playbooks=playbooks if playbooks is not None else dict(base.playbooks),
        thresholds=thresholds if thresholds is not None else dict(base.thresholds),
    )


def test_single_dimension_diff_is_minimal():
    """A challenger that changes EXACTLY ONE dimension (a reordered discovery sequence) is minimal;
    declared_diff names that one dimension."""
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[1] = seq[1], seq[0]  # swap first two slots -> one playbook dim changed
    chal = reorder_discovery(champ, seq)

    diff = declared_diff(champ, chal)
    assert diff == {"playbooks.discovery_sequence"}
    assert is_minimal_diff(champ, chal) is True


def test_multi_dimension_diff_is_not_minimal():
    """A config changing TWO dimensions (a threshold AND a playbook) violates containment ->
    is_minimal_diff False; declared_diff names BOTH changed dimensions."""
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq.reverse()
    two = AgentConfig(
        version=champ.version,
        kb_version=champ.kb_version,
        persona=champ.persona,
        prompts=dict(champ.prompts),
        playbooks={**champ.playbooks, "discovery_sequence": seq},
        thresholds={**champ.thresholds, "pushiness_cap": 0.9},  # second dimension changed
    )
    diff = declared_diff(champ, two)
    assert "playbooks.discovery_sequence" in diff
    assert "thresholds.pushiness_cap" in diff
    assert is_minimal_diff(champ, two) is False


def test_zero_diff_is_not_minimal():
    """An identical config (zero changed dimensions) is NOT a minimal diff — a challenger must
    declare exactly one change to be a challenger at all."""
    champ = load_config("champion_v0")
    same = _config_with()  # deep-equal copy
    assert declared_diff(champ, same) == set()
    assert is_minimal_diff(champ, same) is False


def test_mutate_threshold_does_not_mutate_input():
    """mutate_threshold returns a NEW config with one threshold changed; the input is untouched
    (deep-copy mutator) and the result is a minimal one-dimension diff."""
    champ = load_config("champion_v0")
    original = dict(champ.thresholds)
    chal = mutate_threshold(champ, "trust_gate_open_price", 0.7)
    assert chal.thresholds["trust_gate_open_price"] == 0.7
    assert champ.thresholds == original  # input not mutated
    assert declared_diff(champ, chal) == {"thresholds.trust_gate_open_price"}
    assert is_minimal_diff(champ, chal) is True


# === 3. is_extreme classification (R19) ========================================================


def test_pricing_threshold_change_is_extreme():
    """A diff touching a pricing-concession threshold is EXTREME (needs human approval)."""
    champ = load_config("champion_v0")
    chal = mutate_threshold(champ, "max_concession_band", 0.4)
    challenger = generator_mod._build_challenger(champ, chal, dimension_hint=None, seed=1)
    assert challenger.is_extreme is True
    assert challenger.dimension == "thresholds.max_concession_band"


def test_persona_change_is_extreme():
    """A diff touching the persona is EXTREME (a persona overhaul needs human approval)."""
    champ = load_config("champion_v0")
    chal = _config_with(persona=Persona(name="Alex", role="closer", style="high-pressure"))
    challenger = generator_mod._build_challenger(champ, chal, dimension_hint=None, seed=1)
    assert challenger.is_extreme is True


def test_discovery_reorder_is_not_extreme():
    """The demo dimension — reordering the discovery sequence — is NOT extreme (auto-promotable)."""
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    chal = reorder_discovery(champ, seq)
    challenger = generator_mod._build_challenger(champ, chal, dimension_hint=None, seed=1)
    assert challenger.is_extreme is False


# === 4. Generator: every challenger is minimal + correctly flagged + deterministic =============


def test_generate_challengers_are_minimal_and_versioned():
    """generate_challengers (no llm -> parametric/reorder path only, network-free) returns
    challengers that EACH satisfy is_minimal_diff, set is_extreme correctly, and carry a
    deterministic challenger_version derived from parent+dimension+seed."""
    champ = load_config("champion_v0")
    # A small failure-skewed episode set so the generator has a weakness to target.
    losers = [
        _ep(f"L{i}", ladder_tier=0, outcome="walked", qualified=True) for i in range(4)
    ]
    winners = [
        _ep(f"W{i}", ladder_tier=3, outcome="trial_booked", qualified=True) for i in range(4)
    ]
    challengers = generator_mod.generate_challengers(
        champ, losers + winners, llm=None, seed=11, n=2
    )
    assert len(challengers) >= 1
    for c in challengers:
        assert is_minimal_diff(champ, c.config) is True, f"{c.dimension} not minimal"
        assert c.parent_version == champ.version
        assert c.challenger_version and c.challenger_version != champ.version
        # is_extreme matches the dimension class.
        assert c.is_extreme == (c.dimension in {f"thresholds.{k}" for k in generator_mod._PRICING_THRESHOLD_KEYS} or c.dimension == "persona")

    # Determinism: same seed -> same challenger versions + dimensions.
    again = generator_mod.generate_challengers(champ, losers + winners, llm=None, seed=11, n=2)
    assert [c.challenger_version for c in challengers] == [c.challenger_version for c in again]


# === 5. Frozen held-out set + R22 rotation (mined failures excluded) ===========================


def test_frozen_held_out_is_reproducible():
    """frozen_held_out(seed,n,rotation_index) is FROZEN: the same args reproduce the EXACT persona
    set (same ids, same order)."""
    a = frozen_held_out(seed=5, n=8, rotation_index=0)
    b = frozen_held_out(seed=5, n=8, rotation_index=0)
    assert [p.persona_id for p in a.personas] == [p.persona_id for p in b.personas]
    assert a.rotation_index == 0
    # A different rotation_index yields a different sample (rotation moves the set).
    c = frozen_held_out(seed=5, n=8, rotation_index=1)
    assert [p.persona_id for p in a.personas] != [p.persona_id for p in c.personas]


def test_rotate_excludes_mined_personas():
    """R22: mined-failure personas feed only the TRAINING population, never the held-out eval set.
    rotate(held_out, exclude_persona_ids=[...]) advances rotation_index and re-samples until NONE of
    the excluded ids appear in the new held-out set."""
    base = frozen_held_out(seed=3, n=10, rotation_index=0)
    # Exclude some ids that the next naive rotation WOULD have produced, forcing a re-sample.
    naive_next = frozen_held_out(seed=3, n=10, rotation_index=1)
    excluded = {naive_next.personas[0].persona_id, naive_next.personas[1].persona_id}

    rotated = rotate(base, exclude_persona_ids=tuple(excluded))
    got_ids = {p.persona_id for p in rotated.personas}
    assert got_ids.isdisjoint(excluded), "rotated held-out set must exclude all mined ids"
    assert rotated.rotation_index > base.rotation_index
    assert len(rotated.personas) == len(base.personas)


# === 6. qualification_accuracy v0 proxy (R29) ==================================================


def test_qualification_accuracy_proxy():
    """qualification_accuracy (v0 proxy): agent_verdict = 'qualified' iff ladder_tier >= 2, else
    'unqualified'; accuracy = fraction matching Episode.qualified ground truth. Two correct
    (qualified+tier3, unqualified+tier0) + two wrong (qualified+tier0 missed, unqualified+tier3
    mis-pursued) -> 0.5."""
    eps = [
        _ep("c1", ladder_tier=3, outcome="trial_booked", qualified=True),    # correct: pursued, qualified
        _ep("c2", ladder_tier=0, outcome="released", qualified=False),       # correct: not pursued, unqualified
        _ep("w1", ladder_tier=0, outcome="walked", qualified=True),          # wrong: missed a qualified prospect
        _ep("w2", ladder_tier=3, outcome="trial_booked", qualified=False),   # wrong: pursued an unqualified one
    ]
    assert qualification_accuracy(eps) == 0.5
    # An empty set is a degenerate 1.0 (nothing to disagree on) — documented in the proxy.
    assert qualification_accuracy([]) == 1.0


# --- Episode builder shared by the generator + qual-accuracy tests -----------------------------


def _ep(eid: str, *, ladder_tier: int, outcome: str, qualified: bool, version: str = "champion_v0") -> Episode:
    """A minimal in-memory Episode with one agent turn carrying a belief snapshot (enough for the
    pure tests; ladder_tier + qualified are what the gate/qual-acc read)."""
    belief = BeliefSnapshot(drivers={"bail_risk": 0.1}, decision_confidence=0.8)
    return Episode(
        episode_id=eid,
        turns=[Turn(turn_id=0, speaker="agent", text="Hi!", decision="greeting", belief=belief)],
        outcome=outcome,
        ladder_tier=ladder_tier,
        qualified=qualified,
        version=version,
        kb_version="kb_v0",
        channel="sim",
        persona="anxious_parent",
        cohort="held_out",
    )


# === 7. DB-GATED end-to-end verification (U10) =================================================
# Skips when DATABASE_URL is unset; the orchestrator runs it with Postgres up (5434). A deterministic
# stacked runner makes the challenger CLEARLY better (its episodes dominate the ladder), so the round
# promotes and the challenger becomes the stored champion. No network: the runner returns fixed
# episode sets keyed by config version, bypassing self-play entirely.

pytest.importorskip("asyncpg", reason="asyncpg not installed — DB-dependent integration test")
from src.memory import store  # noqa: E402
from src.memory.schema import VersionLineage  # noqa: E402

_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"

_db_required = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — DB-dependent integration test (run with Postgres+pgvector up)",
)


@pytest.fixture()
async def _schema():
    """Rebind the store pool to THIS test's loop + apply the idempotent migration (test_store.py)."""
    await store.close_pool()
    pool = await store.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
    yield
    await store.close_pool()


async def _seed_champion(champ) -> None:
    """Record the loaded champion as the current champion so the lineage has a real parent node."""
    await store.record_version(
        VersionLineage(version=champ.version, parent_version=None, kb_version=champ.kb_version, is_champion=True)
    )


def _dominating_runner(champ_version: str):
    """A deterministic run_experiment runner stand-in (network-free, no self-play): champion config
    -> all tier-0 episodes; ANY other config -> all tier-4 episodes. Stacks the deck so the
    challenger is unambiguously, significantly better, keyed by the config.version stamped per run."""

    async def runner(personas, agent_llm_factory, prospect_llm_factory, config, **kw):
        if config.version == champ_version:
            tier, outcome = 0, "walked"
        else:
            tier, outcome = 4, "enrolled"
        return [
            _ep(f"{config.version}_{i}", ladder_tier=tier, outcome=outcome, qualified=True, version=config.version)
            for i in range(len(personas))
        ]

    return runner


@_db_required
async def test_obviously_better_sequencing_challenger_promotes_and_becomes_champion(_schema):
    """U10 VERIFICATION: a seeded 'obviously better' discovery-sequencing challenger runs through
    run_experiment, clears the gate (status 'promoted'), and after promote() the store's champion is
    the challenger version. Deterministic dominating runner (no self-play, no network)."""
    champ = load_config("champion_v0")
    await _seed_champion(champ)

    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]  # reorder discovery -> non-extreme, minimal
    challenger = generator_mod._build_challenger(champ, reorder_discovery(champ, seq), dimension_hint=None, seed=99)

    held = frozen_held_out(seed=2, n=6, rotation_index=0)
    result = await experiment_mod.run_experiment(
        champ, challenger, held, agent_llm_factory=lambda: None, prospect_llm_factory=lambda: None,
        seed=2, runner=_dominating_runner(champ.version),
    )
    assert result.comparison.challenger_better is True

    decision = evaluate_promotion(result, is_extreme=challenger.is_extreme)
    assert decision.status == "promoted", f"expected promote, got {decision.status}: {decision.reasons}"

    lineage = await promotion_mod.promote(challenger, result, seed=2)
    assert lineage.version == challenger.challenger_version and lineage.is_champion is True

    champ_node = await store.get_champion()
    assert champ_node is not None
    assert champ_node.version == challenger.challenger_version, "challenger must be the new champion"
    assert champ_node.parent_version == champ.version


@_db_required
async def test_run_improvement_round_orchestrates_generate_to_promotion(_schema):
    """run_improvement_round wires generator -> run_experiment -> evaluate_promotion -> promote():
    with a dominating runner a clean challenger promotes and the lineage node is recorded; an extreme
    one would block. Proves the orchestrator returns a coherent RoundResult."""
    champ = load_config("champion_v0")
    await _seed_champion(champ)

    losers = [_ep(f"L{i}", ladder_tier=0, outcome="walked", qualified=True) for i in range(4)]
    winners = [_ep(f"W{i}", ladder_tier=3, outcome="trial_booked", qualified=True) for i in range(4)]
    held = frozen_held_out(seed=4, n=6, rotation_index=0)

    rr = await promotion_mod.run_improvement_round(
        champ, training_episodes=losers + winners, held_out=held,
        agent_llm_factory=lambda: None, prospect_llm_factory=lambda: None, llm=None, seed=7,
        runner=_dominating_runner(champ.version), persist_promotion=True,
    )
    assert rr.challenger is not None and rr.experiment is not None
    if not rr.challenger.is_extreme:
        assert rr.decision.status == "promoted" and rr.lineage is not None
        champ_node = await store.get_champion()
        assert champ_node.version == rr.challenger.challenger_version
    else:
        assert rr.decision.status == "pending_approval" and rr.lineage is None

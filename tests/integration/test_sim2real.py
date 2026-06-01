# Integration tests for U14 (B): the SIM-TO-REAL divergence harness (plan R36/R37) — the central
# risk measurement (an agent that wins in sim but fails with a real human). DB-FREE + LIVEKIT-FREE:
# both arms use the routed MockLLMClient (agent vs prospect, KTD5) and selfplay.run_episode is called
# with persist=False, so no Postgres is required. Proves: (1) PARITY — a matched run with voice_noise=0
# under a deterministic MockLLM yields divergence_score ~0 (same brain + same scripted inputs -> same
# decisions); (2) the GAP appears — voice_noise>0 yields divergence_score > 0; (3) the report's
# mean_divergence feeds promotion.evaluate_promotion(divergence=mean): below threshold -> not paused,
# over threshold -> status "paused" (R36, the gate consumes the gap). Mirrors test_selfplay's mock
# builders so the two arms share one brain.
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from src.config.settings import load_config
from src.core.llm import Message, MockLLMClient
from src.loop import promotion
from src.loop.experiment import ExperimentResult
from src.loop.grading import ComparisonResult, GuardrailReport
from src.loop.sim2real import (
    DivergenceReport,
    MatchedRun,
    MatchedScenario,
    Sim2RealReport,
    divergence,
    run_matched,
    sim_to_real_report,
)
from src.sim.personas import Persona


# --- Mock builders (same routing contract as test_selfplay) -------------------------------------


def _is_json_call(opts: dict[str, Any]) -> bool:
    rf = opts.get("response_format")
    return isinstance(rf, dict) and rf.get("type") == "json_object"


def _agent_mock(
    *,
    act: str = "ask",
    target_slot: Optional[str] = "goal",
    tier: Optional[str] = None,
    confidence: float = 0.8,
    reply: str = "Sure, tell me a bit about what your child is working on.",
) -> MockLLMClient:
    decision: dict[str, Any] = {
        "act": act,
        "target_slot": target_slot,
        "tier": tier,
        "confidence": confidence,
        "rationale": "test-routed combined proposal",
        "trust": 0.05,
        "need_intensity": 0.05,
        "urgency": 0.0,
        "purchase_intent": 0.03,
        "bail_risk": -0.02,
    }
    decision_json = json.dumps(decision)

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        if _is_json_call(opts):
            return decision_json
        return reply

    return MockLLMClient(serve)


def _prospect_mock() -> MockLLMClient:
    # Slot-rich utterance (grade + subject + timeline) so the DST's DETERMINISTIC regex extraction
    # has something to extract — and so ASR-style noise on the voice arm corrupts that extraction,
    # surfacing the real sim-to-real gap (under voice_noise=0 both arms extract identically -> parity).
    payload = json.dumps(
        {
            "utterance": "My daughter is in 9th grade and needs help with SAT math by next month.",
            "deltas": {"trust": 0.0, "need": 0.0, "urgency": 0.0, "purchase_intent": 0.0},
        }
    )

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        return payload

    return MockLLMClient(serve)


def _persona() -> Persona:
    """A qualified, patient persona that does NOT commit under the neutral prospect mock, so both
    arms run several matched turns (the divergence is computed over those matched turns)."""
    return Persona(
        persona_id="match_0001",
        archetype="anxious_parent",
        style="chatty",
        utility={"budget": 0.8, "need": 0.8, "trust": 0.4, "urgency": 0.6, "patience": 0.95},
        resistance={"price": 0.3, "efficacy_doubt": 0.3, "diy_free": 0.2, "timing": 0.3},
        decision_factors=["proof_of_results"],
        difficulty=0.4,
        qualified=True,
        disqualifier=None,
        max_commitment="enrollment",
    )


def _scenario() -> MatchedScenario:
    return MatchedScenario(persona=_persona(), prospect_script_or_seed=11, label="anxious_parent_baseline")


# =============================== PARITY: noise=0 -> divergence ~0 ================================


async def test_matched_run_zero_noise_parity_divergence_near_zero():
    """PARITY (R37): with voice_noise=0 and a deterministic MockLLM, the SIM arm (text self-play) and
    the VOICE arm (same brain driven on the same scripted prospect turns) make the SAME decisions on
    matched turns -> divergence_score ~0. Same brain, same inputs -> same behavior."""
    config = load_config("champion_v0")
    scenario = _scenario()

    run = await run_matched(
        scenario,
        config,
        _agent_mock,
        _prospect_mock,
        seed=11,
        voice_noise=0.0,
    )
    assert isinstance(run, MatchedRun)
    # Both arms produced an episode with matched turns (neither persisted to the DB).
    assert run.sim_episode.turns and run.voice_episode.turns

    report = divergence(run.sim_episode, run.voice_episode)
    assert isinstance(report, DivergenceReport)
    # Parity: identical decisions on matched turns + identical outcome + identical belief trajectory.
    assert report.decision_disagreement == 0.0
    assert report.ladder_gap == 0.0
    assert report.belief_distance == 0.0
    assert report.divergence_score == 0.0


# =============================== THE GAP: noise>0 -> divergence > 0 ==============================


async def test_matched_run_with_noise_shows_divergence_gap():
    """With voice_noise>0 the voice arm's perceived user text is ASR-corrupted (inject_noise), so the
    robust-but-not-identical DST/decisions drift from the clean sim arm -> divergence_score > 0. The
    gap the promotion gate exists to catch appears once realism enters."""
    config = load_config("champion_v0")
    scenario = _scenario()

    run = await run_matched(
        scenario,
        config,
        _agent_mock,
        _prospect_mock,
        seed=11,
        voice_noise=0.8,  # heavy ASR-style corruption on the voice arm's perceived text
    )
    report = divergence(run.sim_episode, run.voice_episode)
    assert report.divergence_score > 0.0, (
        f"expected a sim-vs-voice gap with noise>0, got {report}"
    )
    assert 0.0 <= report.divergence_score <= 1.0


# =============================== THE GATE: report feeds evaluate_promotion (R36) =================


def _passing_experiment() -> ExperimentResult:
    """A clean, would-otherwise-promote experiment: significant lift, no guardrail regression,
    qualification held. So the ONLY thing that can pause it is the sim-to-real divergence (R36) —
    isolating that the divergence scalar is what trips the pause."""
    comparison = ComparisonResult(
        champion_kpi=2.0,
        challenger_kpi=2.6,
        delta=0.6,
        champion_ci=(1.8, 2.2),
        challenger_ci=(2.4, 2.8),
        delta_ci=(0.2, 1.0),  # excludes 0 -> challenger_better True
        challenger_better=True,
        n_champion=20,
        n_challenger=20,
    )
    clean = GuardrailReport(
        pushiness_rate=0.0,
        pushiness_violation_turns=[],
        false_promise_flags=0,
        escalation_appropriate=True,
        pushiness_over_cap=False,
        agent_turns=40,
    )
    return ExperimentResult(
        dimension="discovery_sequence",
        comparison=comparison,
        champion_guardrails=clean,
        challenger_guardrails=clean,
        champion_qual_acc=0.9,
        challenger_qual_acc=0.9,
        champion_eps=[],
        challenger_eps=[],
    )


async def test_report_mean_divergence_feeds_promotion_pause_gate():
    """R36: the Sim2RealReport's mean_divergence is the scalar fed to evaluate_promotion(divergence=).
    Below the threshold the (otherwise-passing) challenger is NOT paused; an over-threshold divergence
    flips the gate to status "paused" — the promotion gate consumes the gap."""
    config = load_config("champion_v0")
    scenarios = [_scenario(), _scenario()]

    # A clean (zero-noise) report -> low/zero divergence -> below the threshold -> NOT paused.
    clean_report = await sim_to_real_report(
        scenarios, config, _agent_mock, _prospect_mock, seed=11, voice_noise=0.0
    )
    assert isinstance(clean_report, Sim2RealReport)
    assert clean_report.mean_divergence <= promotion.DIVERGENCE_THRESHOLD
    decision_ok = promotion.evaluate_promotion(
        _passing_experiment(), is_extreme=False, divergence=clean_report.mean_divergence
    )
    assert decision_ok.status != "paused"
    assert decision_ok.status == "promoted"  # the clean pass auto-promotes (nothing else blocks it)

    # An over-threshold divergence (simulated as the scalar the report would yield in a bad gap) ->
    # the SAME passing experiment is now PAUSED (R36 — the gap dominates the bar).
    over_threshold = promotion.DIVERGENCE_THRESHOLD + 0.1
    decision_paused = promotion.evaluate_promotion(
        _passing_experiment(), is_extreme=False, divergence=over_threshold
    )
    assert decision_paused.status == "paused"
    assert decision_paused.promote is False


async def test_sim_to_real_report_aggregates_per_scenario_mean_and_max():
    """The report aggregates divergence across scenarios: per_scenario list, mean_divergence and
    max_divergence (the scalars U10 can consume). mean <= max and both in [0,1]."""
    config = load_config("champion_v0")
    scenarios = [_scenario(), _scenario(), _scenario()]
    report = await sim_to_real_report(
        scenarios, config, _agent_mock, _prospect_mock, seed=11, voice_noise=0.5
    )
    assert len(report.per_scenario) == len(scenarios)
    assert all(isinstance(r, DivergenceReport) for r in report.per_scenario)
    assert 0.0 <= report.mean_divergence <= 1.0
    assert 0.0 <= report.max_divergence <= 1.0
    assert report.mean_divergence <= report.max_divergence + 1e-9

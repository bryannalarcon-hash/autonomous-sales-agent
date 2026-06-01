# Integration tests for U9 hybrid grading + guardrails (src/loop/grading.py). DB-FREE: builds
# Episode/Turn/BeliefSnapshot objects in memory and stubs the judge with MockLLMClient (scripted
# list OR callable) so there is NO network and full determinism. Covers the six U9 integrity
# scenarios: deterministic groundedness flags an off-KB claim; the pairwise judge prefers the
# known-better transcript AND ties on position-bias flip; a higher pushiness-over-cap challenger is
# flagged as a guardrail regression; the R39 headline gate is withheld until n + agreement clear;
# compare_versions ranks a clearly-better challenger with bootstrap CIs (and does not for a worse
# one); and the bootstrap CI is seed-deterministic and brackets the mean.
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from src.core.llm import Message, MockLLMClient
from src.loop import grading
from src.memory.schema import BeliefSnapshot, Episode, Turn


# --- Episode builders (in-memory; mirrors test_selfplay's explicit-construction style) ---------


def _belief(*, bail_risk: float = 0.1, decision_confidence: float = 0.8) -> BeliefSnapshot:
    return BeliefSnapshot(
        drivers={
            "trust": 0.6,
            "need_intensity": 0.6,
            "price_sensitivity": 0.4,
            "urgency": 0.5,
            "purchase_intent": 0.5,
            "bail_risk": bail_risk,
        },
        decision_confidence=decision_confidence,
    )


def _turn(
    tid: int,
    speaker: str,
    text: str,
    *,
    decision: Optional[str] = None,
    bail_risk: float = 0.1,
) -> Turn:
    return Turn(
        turn_id=tid,
        speaker=speaker,
        text=text,
        decision=decision,
        belief=_belief(bail_risk=bail_risk) if speaker == "agent" else None,
    )


def _episode(
    eid: str,
    turns: list[Turn],
    *,
    outcome: str = "consult_booked",
    ladder_tier: int = 2,
    version: str = "champion_v0",
    persona: str = "anxious_parent",
    escalated: bool = False,
) -> Episode:
    return Episode(
        episode_id=eid,
        turns=turns,
        outcome=outcome,
        ladder_tier=ladder_tier,
        qualified=True,
        version=version,
        kb_version="kb_v0",
        channel="sim",
        persona=persona,
        cohort="held_out",
        escalated=escalated,
    )


# A tiny KB the groundedness check runs against. Plain strings are accepted by retriever.grounded
# (its _support_text handles Chunk OR str). The price/feature claims here are the "supported" facts.
_KB_CHUNKS = [
    "Our tutoring plans start at $199 per month for weekly one-on-one sessions with a vetted tutor.",
    "We offer a free initial consultation to match your child with the right tutor and build a plan.",
    "Sessions are personalized to your child's grade, subject, and learning goals.",
]


# --- 1. Deterministic groundedness (R23) -------------------------------------------------------


def test_ungrounded_claim_is_flagged():
    """An agent turn asserting a fact absent from the KB (an invented price + guarantee) is recorded
    as a groundedness violation; a grounded answer drawn from the KB is not."""
    grounded_turn = _turn(
        1,
        "agent",
        "Our tutoring plans start at $199 per month for personalized one-on-one sessions.",
        decision="answer_via_kb",
    )
    ungrounded_turn = _turn(
        3,
        "agent",
        "We guarantee a full grade jump in 14 days or your $499 is refunded, no questions asked.",
        decision="answer_via_kb",
    )
    ep = _episode("ep_ground", [_turn(0, "agent", "Hi there!", decision="greeting"),
                                grounded_turn,
                                _turn(2, "prospect", "How much does it cost?"),
                                ungrounded_turn])

    summary = grading.episode_groundedness(ep, _KB_CHUNKS)

    # The invented $499 / 14-day guarantee turn is flagged; the $199 KB-backed turn is not.
    violation_turn_ids = {v.turn_id for v in summary.violations}
    assert 3 in violation_turn_ids, f"expected turn 3 flagged, got {summary.violations}"
    assert 1 not in violation_turn_ids, "grounded $199 answer should NOT be flagged"
    assert 0.0 <= summary.grounded_rate <= 1.0
    assert summary.grounded_rate < 1.0  # at least one violation drags the rate below 1
    # The violation names the offending claim text so the decision log can explain WHY.
    bad = next(v for v in summary.violations if v.turn_id == 3)
    assert bad.claim, "violation should carry the unsupported claim text"


# --- 2. Pairwise LLM-judge (R24) + position-bias mitigation ------------------------------------


def _better_episode() -> Episode:
    """A consultative, non-pushy transcript that ends at a real ladder commit (the 'better' arm)."""
    return _episode(
        "ep_better",
        [
            _turn(0, "agent", "Hi! Tell me a bit about what your child is working on.", decision="ask"),
            _turn(1, "prospect", "She's struggling with algebra."),
            _turn(2, "agent", "Got it — algebra trips up a lot of students. What's her goal this term?",
                  decision="ask"),
            _turn(3, "agent", "A free consultation would let us match her with the right tutor.",
                  decision="attempt_close", bail_risk=0.1),
        ],
        outcome="consult_booked",
        ladder_tier=2,
    )


def _worse_episode() -> Episode:
    """A pushy transcript that keeps closing while the prospect signals walk (the 'worse' arm)."""
    return _episode(
        "ep_worse",
        [
            _turn(0, "agent", "Sign up now! This deal ends today!", decision="attempt_close", bail_risk=0.9),
            _turn(1, "prospect", "I need to go."),
            _turn(2, "agent", "Just enroll right now, don't miss out!", decision="attempt_close",
                  bail_risk=0.95),
        ],
        outcome="walked",
        ladder_tier=0,
    )


def _verdict_json(winner: str) -> str:
    return json.dumps({"winner": winner, "rationale": "scripted test verdict"})


async def test_pairwise_judge_prefers_known_better_transcript():
    """A judge scripted to ALWAYS pick the consultative transcript names it as the winner under
    position-bias mitigation (it stays consistent across both orders)."""
    better, worse = _better_episode(), _worse_episode()

    # The judge always prefers whichever transcript is the consultative one, regardless of order,
    # so both orderings agree -> a confident verdict (not a forced tie).
    def judge(messages: Sequence[Message], **opts: Any) -> str:
        prompt = messages[-1]["content"]
        # "consultation" / "tell me a bit" appear only in the better transcript; pick that side.
        a_block, b_block = grading._split_transcript_blocks(prompt)
        better_is_a = "consultation" in a_block.lower()
        return _verdict_json("a" if better_is_a else "b")

    verdict = await grading.judge_pairwise(
        better, worse, "consultative_quality", MockLLMClient(judge), seed=1
    )
    assert verdict.winner == "a"  # the better episode was passed as A
    assert verdict.rubric == "consultative_quality"


async def test_position_bias_flip_yields_tie():
    """A judge that ALWAYS picks position 'a' (a position-biased judge) disagrees across the two
    orders, so mitigation conservatively returns 'tie' — the judge isn't robust on this pair."""
    better, worse = _better_episode(), _worse_episode()

    # Pathological judge: always answers "a" regardless of content -> order (a,b) says a wins,
    # order (b,a) also says a (== worse) wins -> the two disagree -> tie.
    biased = MockLLMClient(lambda messages, **opts: _verdict_json("a"))

    verdict = await grading.judge_pairwise(
        better, worse, "non_pushy", biased, seed=1, mitigate_position_bias=True
    )
    assert verdict.winner == "tie", "position-biased judge must be neutralized to a tie"


async def test_unparseable_judge_json_is_conservative_tie():
    """A judge returning garbage (no parseable JSON verdict) yields 'tie' (conservative)."""
    junk = MockLLMClient(["not json at all", "still not json"])
    verdict = await grading.judge_pairwise(
        _better_episode(), _worse_episode(), "sounds_human", junk, seed=1,
        mitigate_position_bias=False,
    )
    assert verdict.winner == "tie"


# --- 3. Guardrail metrics + regression (R24) ---------------------------------------------------


def _pushy_episode(eid: str, *, over_cap_turns: int) -> Episode:
    """An episode with `over_cap_turns` pressure acts fired while bail_risk is over the cap."""
    turns = [_turn(0, "agent", "Hi!", decision="greeting")]
    for i in range(1, over_cap_turns + 1):
        turns.append(_turn(i, "agent", "You should enroll today.", decision="attempt_close",
                           bail_risk=0.95))
    # One clean, non-pressure turn so the rate is a fraction < 1.
    turns.append(_turn(over_cap_turns + 1, "agent", "Happy to answer any questions.",
                       decision="answer_via_kb", bail_risk=0.1))
    return _episode(eid, turns, outcome="walked", ladder_tier=0)


def test_guardrail_regression_detected():
    """A challenger whose pushiness-over-cap rate exceeds the champion's is flagged as regressed;
    an equal-or-cleaner challenger is not."""
    champion = grading.guardrail_report(_pushy_episode("champ", over_cap_turns=1))
    challenger_worse = grading.guardrail_report(_pushy_episode("chal_worse", over_cap_turns=3))
    challenger_clean = grading.guardrail_report(
        _episode("chal_clean", [_turn(0, "agent", "Hi!", decision="greeting"),
                                _turn(1, "agent", "Happy to help.", decision="answer_via_kb",
                                      bail_risk=0.1)],
                 outcome="consult_booked", ladder_tier=2)
    )

    assert challenger_worse.pushiness_rate > champion.pushiness_rate
    assert grading.guardrails_regressed(champion, challenger_worse) is True
    assert grading.guardrails_regressed(champion, challenger_clean) is False


# --- 4. R39 headline gate ----------------------------------------------------------------------


def test_headline_withheld_until_agreement():
    """can_report_headline is False when n < min_sample OR agreement < min_agreement, and True once
    BOTH clear — the R39 integrity guard on reporting a headline KPI."""
    # 8 of 10 agree -> agreement 0.8.
    human = ["a", "a", "b", "b", "a", "b", "a", "b", "a", "b"]
    judge = ["a", "a", "b", "b", "a", "b", "a", "a", "b", "b"]  # disagree on idx 7 & 8

    # Too-small sample (min_sample 20) -> withheld even though agreement is high.
    small = grading.can_report_headline(human, judge, min_sample=20, min_agreement=0.7)
    assert small.can_report is False
    assert small.n == 10

    # Agreement too low (need 0.95) -> withheld even though the sample is big enough.
    low_agree = grading.can_report_headline(human, judge, min_sample=10, min_agreement=0.95)
    assert low_agree.can_report is False
    assert low_agree.agreement < 0.95

    # Both clear (n>=10 and agreement>=0.7) -> headline may be reported.
    ok = grading.can_report_headline(human, judge, min_sample=10, min_agreement=0.7)
    assert ok.can_report is True
    assert ok.n == 10
    assert ok.agreement >= 0.7


# --- 5. KPI + champion-vs-challenger ranking (the U9 verification) -----------------------------


def _episodes_at_tier(prefix: str, tier: int, outcome: str, n: int, version: str) -> list[Episode]:
    return [
        _episode(f"{prefix}_{i}", [_turn(0, "agent", "Hi!", decision="greeting")],
                 outcome=outcome, ladder_tier=tier, version=version)
        for i in range(n)
    ]


def test_kpi_score_is_weighted_ladder_mean():
    """kpi_score is the mean of ladder_tier across episodes (the rung weight)."""
    eps = (_episodes_at_tier("a", 4, "enrolled", 2, "v")  # two at tier 4
           + _episodes_at_tier("b", 0, "walked", 2, "v"))  # two at tier 0
    assert grading.kpi_score(eps) == 2.0  # (4+4+0+0)/4


def test_compare_versions_ranks_better_challenger_with_ci():
    """A challenger with clearly higher ladder outcomes is ranked challenger_better=True with the
    per-arm bootstrap CIs reported; a clearly-worse challenger is not. (U9 verification.)"""
    # Champion: all walk (tier 0). Challenger-better: all enroll (tier 4). Separation is total.
    champ = _episodes_at_tier("champ", 0, "walked", 12, "champion_v0")
    better = _episodes_at_tier("better", 4, "enrolled", 12, "challenger_v1")
    worse = _episodes_at_tier("worse", 0, "walked", 12, "challenger_v1")

    res_better = grading.compare_versions(champ, better, seed=7)
    assert res_better.champion_kpi == 0.0
    assert res_better.challenger_kpi == 4.0
    assert res_better.delta == 4.0
    assert res_better.challenger_better is True
    # Bootstrap CIs are reported per arm (low <= high, and they bracket the constant means here).
    assert res_better.champion_ci[0] <= res_better.champion_ci[1]
    assert res_better.challenger_ci[0] <= res_better.challenger_ci[1]

    # A challenger no better than the champion does not clear the bar.
    res_worse = grading.compare_versions(champ, worse, seed=7)
    assert res_worse.delta == 0.0
    assert res_worse.challenger_better is False


# --- 6. Bootstrap CI (pure stdlib) -------------------------------------------------------------


def test_bootstrap_ci_is_seed_deterministic():
    """Same values + seed -> identical CI; the CI brackets the sample mean; never uses global random."""
    values = [0.0, 1.0, 2.0, 3.0, 4.0, 1.0, 2.0, 3.0, 0.0, 4.0]
    mean = sum(values) / len(values)

    ci_a = grading.bootstrap_ci(values, seed=42, n_resamples=500)
    ci_b = grading.bootstrap_ci(values, seed=42, n_resamples=500)
    assert ci_a == ci_b, "same seed must yield an identical CI (reproducible)"

    low, high = ci_a
    assert low <= high
    assert low <= mean <= high, "the CI of the mean should bracket the sample mean"

    # A different seed generally moves the bounds (not asserted equal) but stays well-formed.
    ci_c = grading.bootstrap_ci(values, seed=43, n_resamples=500)
    assert ci_c[0] <= ci_c[1]


def test_bootstrap_ci_empty_is_zero_zero():
    """An empty value list returns a degenerate (0.0, 0.0) CI rather than raising."""
    assert grading.bootstrap_ci([], seed=1) == (0.0, 0.0)

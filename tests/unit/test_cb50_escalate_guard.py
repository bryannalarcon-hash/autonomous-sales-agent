# Regression tests (TEST-FIRST) for CB-50 — the agent terminally escalated far too early, reaching
# for "let me connect you with a specialist who'll call you back" as a DEFAULT forward move. These
# cover the two pieces of the fix that live outside test_policy.py's gate section:
#   - escalate_only_if_warranted (gates): a TRIGGERLESS escalate is non-terminal — gatekeeper -> a
#     callback offer; qualified -> a non-terminal selling act; a GENUINE reason still escalates.
#   - _extract_grade (dst, secondary): a HEDGED/disjunctive grade answer ("7th or 8th, not sure") must
#     NOT lock an uncertain grade slot (Pat's "Got it — eighth-grade math" false confirmation).
# Network-free: scripted/no LLM; asserts only the DETERMINISTIC extraction + gate behavior.
from __future__ import annotations

from src.config.settings import AgentConfig, Persona
from src.core import dst, gates
from src.core.belief_state import BeliefState
from src.core.policy import Decision


def make_config(**threshold_overrides) -> AgentConfig:
    thresholds = {
        "trust_gate_open_price": 0.5,
        "pushiness_cap": 0.7,
        "pushiness_pressure_count_cap": 2,
        "escalate_low_confidence_turns": 2,
        "low_confidence_level": 0.4,
        "max_concession_band": 0.15,
        "discovery_slots_required": 0,
        "min_turns_before_close": 4,
        "discovery_slots_before_close": 1,
        "extreme_bail_deescalate": 0.9,
        "max_slot_asks": 2,
    }
    thresholds.update(threshold_overrides)
    return AgentConfig(
        version="test_v0",
        kb_version="kb_test",
        persona=Persona(name="Alex", role="learning advisor", style="warm-consultative"),
        prompts={"policy": "Propose the next act as JSON.", "nlg": "Reply warmly."},
        playbooks={"discovery_sequence": ["grade_level", "subject", "goal", "timeline", "budget"]},
        thresholds=thresholds,
    )


# --- escalate_only_if_warranted: 3rd-party "for my <relative>" phrase read -----------------------

def test_third_party_phrase_reads_as_no_authority_and_books_callback():
    """Pat's t1 defect: the caller speaks FOR someone ('my sister's kid') without the explicit
    decision_maker objection being set. The cheap 3rd-party phrase read makes the guard treat a
    triggerless escalate as a CALLBACK offer for the real decider, not a wasted human hand-off."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "this is for my sister's kid — do you cover middle-school math?"
    proposed = Decision(act="escalate", rationale="not the parent")
    out = gates.escalate_only_if_warranted(proposed, b, cfg, history=[])
    assert out.act == "attempt_close" and out.tier == "callback"


def test_third_party_phrase_variants_detected():
    """A handful of natural 3rd-party phrasings with an explicit 'for my / on behalf of my' lead-in are
    recognized so the gatekeeper read is robust. The detector is deliberately TIGHT (it requires the
    lead-in) so a first-person caller is never misread — a bare possessive like 'my sister's kid' alone
    is NOT enough (it would over-match 'my daughter is in 8th'); the lead-in disambiguates intent."""
    cfg = make_config()
    for phrase in (
        "I'm calling for my nephew",
        "it's for my daughter's friend",
        "asking on behalf of my coworker",
        "this is for my sister's kid, do you do middle-school math?",
    ):
        b = BeliefState.fresh()
        b.open_question = phrase
        out = gates.escalate_only_if_warranted(Decision(act="escalate"), b, cfg, history=[])
        assert out.act == "attempt_close" and out.tier == "callback", phrase


def test_first_person_caller_is_not_read_as_third_party():
    """A normal first-person caller (no 'for my <relative>' lead-in) is NOT a 3rd-party/gatekeeper
    read, so a triggerless escalate keeps the agent SELLING (pitch) rather than booking a callback.
    No open question here, so the redirect is a value pitch — not a callback, not an escalate."""
    cfg = make_config()
    b = BeliefState.fresh()
    fill = None  # no open question — exercise the pitch branch
    b.open_question = fill
    out = gates.escalate_only_if_warranted(Decision(act="escalate"), b, cfg, history=[])
    assert out.act == "pitch"
    assert out.tier != "callback"


def test_first_person_open_question_answers_not_callback():
    """A first-person caller WITH an open question ('my goal is to improve') is not a gatekeeper, so a
    triggerless escalate answers the question (keeps selling) — never a callback, never an escalate."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "my goal is to get my grades up this semester — can you help?"
    out = gates.escalate_only_if_warranted(Decision(act="escalate"), b, cfg, history=[])
    assert out.act == "answer_via_kb"
    assert out.act != "escalate" and out.tier != "callback"


# --- secondary: hedged/disjunctive grade must not LOCK an uncertain slot -------------------------

def test_hedged_grade_disjunction_not_locked():
    """SECONDARY (Pat: 'Got it — eighth-grade math' on a '7th or 8th, not sure' answer): a hedged /
    disjunctive grade answer must NOT be extracted as a CONFIRMED (lock-confidence) slot. Either we
    extract nothing, or we extract below the lock threshold so the slot stays soft (re-confirmable)."""
    res = dst._extract_grade("7th or 8th, not sure", asked_grade=True)
    if res is not None:
        _, conf = res
        assert conf < dst._LOCK_CONFIDENCE, f"hedged grade locked at {conf}"


def test_hedged_grade_not_sure_alone_not_locked():
    """'not sure, maybe 7th' is hedged — same rule: do not lock it."""
    res = dst._extract_grade("not sure, maybe 7th", asked_grade=True)
    if res is not None:
        _, conf = res
        assert conf < dst._LOCK_CONFIDENCE


def test_unhedged_grade_still_locks():
    """Guardrail: a CONFIDENT unhedged answer ('8th grade') still locks at/above the lock threshold —
    the hedge softening must not regress a clean confirmation."""
    res = dst._extract_grade("8th grade", asked_grade=True)
    assert res is not None
    _, conf = res
    assert conf >= dst._LOCK_CONFIDENCE

# U4 tests (TEST-FIRST) for the deterministic gates (src/core/gates.py) and the gated
# neuro-symbolic policy (src/core/policy.py). The gates are safety/correctness logic so they are
# tested directly + exhaustively here; the policy is tested as "LLM proposes (MockLLMClient) ->
# gates decide", asserting the gate has final say over the LLM proposal. Covers (plan U4):
#   AE2 price raised before trust -> re-anchor value, do NOT quote/open price;
#   AE3 unqualified no-budget prospect -> graceful release/disqualify, not a forced close;
#   AE8 high trust+need -> attempt_close at enrollment tier; warm-but-not-ready -> trial/consult;
#   skip_known never asks a filled slot; objection gate blocks attempt_close while objection open;
#   pushiness cap fires on repeated pressure; escalation fires on a pricing-concession-beyond-band.
# All network-free: MockLLMClient supplies scripted policy proposals.
from __future__ import annotations

import json

import pytest

from src.config.settings import AgentConfig, Persona
from src.core.belief_state import BeliefState
from src.core import gates
from src.core.gates import apply_gates
from src.core.llm import MockLLMClient
from src.core.policy import Decision, decide


# --- shared fixtures --------------------------------------------------------------------------

# Threshold names the gates read from AgentConfig.thresholds. Defaults mirror champion_v0.yaml
# plus the extra gate thresholds U4 introduces.
_THRESHOLDS = {
    "trust_gate_open_price": 0.5,
    "pushiness_cap": 0.7,
    "pushiness_pressure_count_cap": 2,
    "escalate_low_confidence_turns": 2,
    "low_confidence_level": 0.4,
    "max_concession_band": 0.15,
    "discovery_slots_required": 0,  # overridden per-test; 0 = "no slots needed to open price"
}


def make_config(**threshold_overrides) -> AgentConfig:
    thresholds = dict(_THRESHOLDS)
    thresholds.update(threshold_overrides)
    return AgentConfig(
        version="test_v0",
        kb_version="kb_test",
        persona=Persona(name="Alex", role="learning advisor", style="warm-consultative"),
        prompts={"policy": "PROPOSE the next act as JSON.", "nlg": "Reply warmly."},
        playbooks={"discovery_sequence": ["grade_level", "subject", "goal", "timeline", "budget"]},
        thresholds=thresholds,
    )


def fill_slot(belief: BeliefState, name: str, value, conf: float = 0.9) -> None:
    belief.set_slot(name, value, conf)


# ==============================================================================================
# GATES — tested directly (the safety/correctness layer; gates have FINAL say)
# ==============================================================================================

# --- skip_known -------------------------------------------------------------------------------

def test_skip_known_overrides_ask_for_a_locked_slot():
    """skip_known never re-asks a slot already filled at/above lock confidence."""
    cfg = make_config()
    b = BeliefState.fresh()
    fill_slot(b, "grade_level", "11", 0.9)  # locked
    proposed = Decision(act="ask", target_slot="grade_level", rationale="ask grade")

    out = gates.skip_known(proposed, b, cfg)
    # The gate must NOT leave an ask for the known slot in place.
    assert not (out.act == "ask" and out.target_slot == "grade_level")


def test_skip_known_allows_ask_for_an_unfilled_slot():
    """A slot that is not yet filled may still be asked."""
    cfg = make_config()
    b = BeliefState.fresh()  # no slots filled
    proposed = Decision(act="ask", target_slot="budget", rationale="need budget")
    out = gates.skip_known(proposed, b, cfg)
    assert out.act == "ask" and out.target_slot == "budget"


def test_skip_known_allows_ask_for_a_soft_slot():
    """A slot filled only at LOW confidence is not 'known' — re-asking/confirming is allowed."""
    cfg = make_config()
    b = BeliefState.fresh()
    fill_slot(b, "budget", 300, 0.4)  # below lock -> soft
    proposed = Decision(act="ask", target_slot="budget", rationale="confirm budget")
    out = gates.skip_known(proposed, b, cfg)
    assert out.act == "ask" and out.target_slot == "budget"


# --- must_clear_objection ---------------------------------------------------------------------

def test_objection_gate_blocks_attempt_close_when_objection_open():
    """While an objection is open, the gate forbids pivot-to-close and routes to handle_objection."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "price"
    proposed = Decision(act="attempt_close", tier="enrollment", rationale="they sound ready")

    out = gates.must_clear_objection(proposed, b, cfg)
    assert out.act != "attempt_close"
    assert out.act == "handle_objection"


def test_objection_gate_passes_when_no_objection():
    """With no active objection, attempt_close is left untouched by this gate."""
    cfg = make_config()
    b = BeliefState.fresh()
    proposed = Decision(act="attempt_close", tier="trial", rationale="ready")
    out = gates.must_clear_objection(proposed, b, cfg)
    assert out.act == "attempt_close" and out.tier == "trial"


# --- price_gate -------------------------------------------------------------------------------

def test_price_gate_blocks_opening_price_before_trust():
    """AE2: opening price/budget before a trust event is blocked -> re-anchor value (pitch), no quote."""
    cfg = make_config(trust_gate_open_price=0.6, discovery_slots_required=3)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.3  # below the open-price threshold
    # No unprompted price question, no value-ack, discovery not complete.
    proposed = Decision(act="ask", target_slot="budget", rationale="get budget")

    out = gates.price_gate(proposed, b, cfg)
    # Must not ask/open price; the gate re-anchors to a value pitch instead.
    assert not (out.act == "ask" and out.target_slot == "budget")
    assert out.act == "pitch"


def test_price_gate_opens_when_prospect_asked_price_unprompted():
    """If the prospect asked price unprompted, the gate lets a price answer through (trust irrelevant)."""
    cfg = make_config(trust_gate_open_price=0.9)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.2
    b.last_user_act = "price_inquiry"  # they asked first
    proposed = Decision(act="answer_via_kb", target_slot="budget", rationale="they asked cost")
    out = gates.price_gate(proposed, b, cfg)
    assert out.act == "answer_via_kb"


def test_price_gate_opens_after_value_acknowledgment_high_trust():
    """A value-acknowledgment (trust at/above the open-price threshold) opens the price gate."""
    cfg = make_config(trust_gate_open_price=0.6)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.75  # trust event has happened
    proposed = Decision(act="ask", target_slot="budget", rationale="now ask budget")
    out = gates.price_gate(proposed, b, cfg)
    assert out.act == "ask" and out.target_slot == "budget"


def test_price_gate_opens_when_required_discovery_slots_filled():
    """Once the required discovery slots are filled, budget may be opened even at modest trust."""
    cfg = make_config(trust_gate_open_price=0.9, discovery_slots_required=3)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.3
    fill_slot(b, "grade_level", "11")
    fill_slot(b, "subject", "SAT math")
    fill_slot(b, "goal", "raise score")
    proposed = Decision(act="ask", target_slot="budget", rationale="discovery done")
    out = gates.price_gate(proposed, b, cfg)
    assert out.act == "ask" and out.target_slot == "budget"


def test_price_gate_ignores_non_price_acts():
    """The price gate only touches acts that open price/budget — everything else passes through."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.1
    proposed = Decision(act="ask", target_slot="grade_level", rationale="ask grade")
    out = gates.price_gate(proposed, b, cfg)
    assert out.act == "ask" and out.target_slot == "grade_level"


# --- pushiness_cap ----------------------------------------------------------------------------

def test_pushiness_cap_blocks_pressure_on_repeated_pressure_count():
    """Repeated back-to-back pressure (count over cap) forbids further pressure acts."""
    cfg = make_config(pushiness_pressure_count_cap=2)
    b = BeliefState.fresh()
    # History where the agent has already pressured twice in a row.
    history = [
        {"role": "assistant", "act": "attempt_close"},
        {"role": "user", "content": "not sure"},
        {"role": "assistant", "act": "attempt_close"},
        {"role": "user", "content": "still not sure"},
    ]
    proposed = Decision(act="attempt_close", tier="enrollment", rationale="push again")
    out = gates.pushiness_cap(proposed, b, cfg, history=history)
    assert out.act != "attempt_close"


def test_pushiness_cap_blocks_pressure_on_high_pushiness_signal():
    """A pushiness signal (bail_risk at/above the cap level) forbids further pressure."""
    cfg = make_config(pushiness_cap=0.7)
    b = BeliefState.fresh()
    b.drivers["bail_risk"] = 0.85  # they are about to walk; pressing harder is forbidden
    proposed = Decision(act="attempt_close", tier="trial", rationale="press")
    out = gates.pushiness_cap(proposed, b, cfg, history=[])
    assert out.act != "attempt_close"


def test_pushiness_cap_allows_pressure_when_under_cap():
    """Under the pressure cap and with low bail_risk, an attempt_close is allowed."""
    cfg = make_config(pushiness_cap=0.7, pushiness_pressure_count_cap=2)
    b = BeliefState.fresh()
    b.drivers["bail_risk"] = 0.2
    history = [{"role": "assistant", "act": "pitch"}, {"role": "user", "content": "tell me more"}]
    proposed = Decision(act="attempt_close", tier="enrollment", rationale="first close attempt")
    out = gates.pushiness_cap(proposed, b, cfg, history=history)
    assert out.act == "attempt_close"


# --- escalation triggers ----------------------------------------------------------------------

def test_escalation_fires_on_pricing_concession_beyond_band():
    """A proposed concession beyond the allowed band is EXTREME -> escalate + mark imminent."""
    cfg = make_config(max_concession_band=0.15)
    b = BeliefState.fresh()
    proposed = Decision(
        act="handle_objection", rationale="offer 30% off", concession=0.30
    )
    out = gates.escalation_triggers(proposed, b, cfg, history=[])
    assert out.act == "escalate"
    assert b.escalation_imminent is True


def test_escalation_allows_concession_within_band():
    """A concession inside the allowed band does NOT escalate."""
    cfg = make_config(max_concession_band=0.15)
    b = BeliefState.fresh()
    proposed = Decision(act="handle_objection", rationale="offer 10% off", concession=0.10)
    out = gates.escalation_triggers(proposed, b, cfg, history=[])
    assert out.act != "escalate"
    assert b.escalation_imminent is False


def test_escalation_fires_on_explicit_human_request():
    """An explicit request for a human is EXTREME -> escalate."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.last_user_act = "human_request"
    proposed = Decision(act="answer_via_kb", rationale="they want a person")
    out = gates.escalation_triggers(proposed, b, cfg, history=[])
    assert out.act == "escalate"


def test_escalation_fires_on_compliance_territory():
    """Compliance territory is EXTREME -> escalate."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.last_user_act = "compliance"
    proposed = Decision(act="pitch", rationale="careful")
    out = gates.escalation_triggers(proposed, b, cfg, history=[])
    assert out.act == "escalate"


def test_escalation_fires_on_two_consecutive_low_confidence_turns():
    """Two consecutive low-confidence turns -> escalate (the agent is lost)."""
    cfg = make_config(escalate_low_confidence_turns=2, low_confidence_level=0.4)
    b = BeliefState.fresh()
    b.decision_confidence = 0.2  # this turn is low-confidence too
    # Prior turn decision was also low-confidence.
    history = [
        {"role": "assistant", "act": "ask", "confidence": 0.25},
        {"role": "user", "content": "what?"},
    ]
    proposed = Decision(act="ask", target_slot="goal", rationale="confused", confidence=0.2)
    out = gates.escalation_triggers(proposed, b, cfg, history=history)
    assert out.act == "escalate"


def test_escalation_does_not_fire_on_single_low_confidence_turn():
    """A single low-confidence turn does not escalate (needs the configured streak)."""
    cfg = make_config(escalate_low_confidence_turns=2, low_confidence_level=0.4)
    b = BeliefState.fresh()
    b.decision_confidence = 0.2
    history = [
        {"role": "assistant", "act": "ask", "confidence": 0.9},  # prior turn was confident
        {"role": "user", "content": "ok"},
    ]
    proposed = Decision(act="ask", target_slot="goal", rationale="one bad turn", confidence=0.2)
    out = gates.escalation_triggers(proposed, b, cfg, history=history)
    assert out.act != "escalate"


# --- apply_gates ordering (escalation wins; gates have final say) -----------------------------

def test_apply_gates_escalation_takes_precedence_over_everything():
    """The full chain: an extreme trigger overrides even a benign proposal."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.last_user_act = "human_request"
    proposed = Decision(act="attempt_close", tier="enrollment", rationale="ready")
    out = apply_gates(proposed, b, cfg, history=[])
    assert out.act == "escalate"


def test_apply_gates_objection_then_no_close():
    """Full chain with an open objection: attempt_close is converted to handle_objection."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "price"
    b.drivers["bail_risk"] = 0.2
    proposed = Decision(act="attempt_close", tier="enrollment", rationale="close")
    out = apply_gates(proposed, b, cfg, history=[])
    assert out.act == "handle_objection"


# ==============================================================================================
# POLICY — LLM PROPOSES, gates DECIDE (neuro-symbolic)
# ==============================================================================================

def _proposal_json(**kwargs) -> str:
    """Build a scripted policy-proposal JSON the MockLLMClient returns for decide()."""
    payload = {"act": kwargs.get("act", "pitch"), "rationale": kwargs.get("rationale", "")}
    for k in ("target_slot", "tier", "confidence", "concession"):
        if k in kwargs:
            payload[k] = kwargs[k]
    return json.dumps(payload)


async def test_policy_returns_decision_dataclass():
    """decide() returns a Decision with the proposed act when no gate overrides it."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.7
    llm = MockLLMClient([_proposal_json(act="pitch", rationale="build value")])
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert isinstance(d, Decision)
    assert d.act == "pitch"


async def test_policy_skip_known_overrides_llm_ask(monkeypatch):
    """AE/skip-known via the policy: LLM proposes asking a known slot; the gate overrides it."""
    cfg = make_config()
    b = BeliefState.fresh()
    fill_slot(b, "grade_level", "11", 0.9)
    b.drivers["trust"] = 0.7
    llm = MockLLMClient([_proposal_json(act="ask", target_slot="grade_level", rationale="ask")])
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert not (d.act == "ask" and d.target_slot == "grade_level")


async def test_policy_ae2_price_before_trust_reanchors_value():
    """AE2: prospect raises price before trust; LLM proposes opening budget; gate re-anchors value."""
    cfg = make_config(trust_gate_open_price=0.6, discovery_slots_required=3)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.25  # trust not established
    llm = MockLLMClient([_proposal_json(act="ask", target_slot="budget", rationale="quote price")])
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert not (d.act == "ask" and d.target_slot == "budget")
    assert d.act == "pitch"  # re-anchor on value, do not quote/open price


async def test_policy_ae3_no_budget_prospect_releases_not_forced_close():
    """AE3: an unqualified (no-budget) prospect -> graceful disqualify, NOT a forced close."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.last_user_act = "disqualify"
    # LLM (correctly, here) proposes a disqualify; the policy/gates must NOT turn it into a close.
    llm = MockLLMClient([_proposal_json(act="disqualify", rationale="no budget, genuinely unqualified")])
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert d.act == "disqualify"
    assert d.act != "attempt_close"


async def test_policy_ae8_hot_prospect_pivots_to_enrollment():
    """AE8: high trust + high need + high intent -> attempt_close at the enrollment tier."""
    cfg = make_config(trust_gate_open_price=0.5, pushiness_cap=0.7, pushiness_pressure_count_cap=3)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.85
    b.drivers["need_intensity"] = 0.8
    b.drivers["purchase_intent"] = 0.8
    b.drivers["bail_risk"] = 0.15
    llm = MockLLMClient([_proposal_json(act="attempt_close", tier="enrollment", rationale="hot")])
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert d.act == "attempt_close"
    assert d.tier == "enrollment"


async def test_policy_ae8_warm_prospect_pivots_to_trial_or_consult():
    """AE8: warm-but-not-ready -> a lower commitment-ladder tier (trial/consultation), not enrollment."""
    cfg = make_config(trust_gate_open_price=0.5, pushiness_cap=0.7, pushiness_pressure_count_cap=3)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.6
    b.drivers["need_intensity"] = 0.55
    b.drivers["purchase_intent"] = 0.45
    b.drivers["bail_risk"] = 0.25
    llm = MockLLMClient([_proposal_json(act="attempt_close", tier="trial", rationale="warm")])
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert d.act == "attempt_close"
    assert d.tier in ("trial", "consultation", "callback")
    assert d.tier != "enrollment"


async def test_policy_objection_gate_blocks_llm_close():
    """LLM proposes attempt_close while an objection is open; the gate blocks the close."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "efficacy_doubt"
    b.drivers["trust"] = 0.7
    llm = MockLLMClient([_proposal_json(act="attempt_close", tier="enrollment", rationale="close")])
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert d.act != "attempt_close"
    assert d.act == "handle_objection"


async def test_policy_pushiness_cap_fires_on_repeated_pressure():
    """LLM keeps proposing pressure; after the cap the policy refuses another pressure act."""
    cfg = make_config(pushiness_pressure_count_cap=2, pushiness_cap=0.7)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.7
    history = [
        {"role": "assistant", "act": "attempt_close"},
        {"role": "user", "content": "no"},
        {"role": "assistant", "act": "attempt_close"},
        {"role": "user", "content": "no again"},
    ]
    llm = MockLLMClient([_proposal_json(act="attempt_close", tier="enrollment", rationale="push")])
    d = await decide(b, history=history, config=cfg, llm_client=llm)
    assert d.act != "attempt_close"


async def test_policy_escalates_on_concession_beyond_band():
    """LLM proposes a deep discount beyond the band; the policy escalates (gates have final say)."""
    cfg = make_config(max_concession_band=0.15)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.7
    llm = MockLLMClient(
        [_proposal_json(act="handle_objection", rationale="offer 40% off", concession=0.40)]
    )
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert d.act == "escalate"


async def test_policy_malformed_proposal_is_safe():
    """A malformed LLM proposal does not crash the policy; it falls back to a safe non-pressure act."""
    cfg = make_config()
    b = BeliefState.fresh()
    llm = MockLLMClient(["not json at all"])
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert isinstance(d, Decision)
    # A safe fallback never silently pressures/closes.
    assert d.act not in ("attempt_close",)

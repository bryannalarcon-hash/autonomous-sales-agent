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

import httpx
import pytest

from src.config.settings import AgentConfig, Persona
from src.core.belief_state import BeliefState
from src.core import gates
from src.core.gates import apply_gates
from src.core.llm import MockLLMClient
from src.core.nlg import realize
from src.core.policy import Decision, decide


class _RaisingLLMClient:
    """A stub LLMClient whose complete()/complete_json() ALWAYS raise a transient httpx error,
    simulating an OpenRouter 429/5xx/timeout that survives the client's bounded retry budget.
    Used to prove the brain degrades gracefully (no exception propagates) — FINDING 2."""

    def __init__(self, exc: Exception | None = None) -> None:
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(503, request=request)
        self._exc = exc or httpx.HTTPStatusError(
            "service unavailable", request=request, response=response
        )

    async def complete(self, messages, *, model=None, response_format=None, **opts):
        raise self._exc

    async def complete_json(self, messages, *, model=None, **opts):
        raise self._exc


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
    # Production history shape: utterances live under "text" (selfplay/voice), not "content".
    history = [
        {"role": "assistant", "act": "attempt_close"},
        {"role": "user", "text": "not sure"},
        {"role": "assistant", "act": "attempt_close"},
        {"role": "user", "text": "still not sure"},
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
    history = [{"role": "assistant", "act": "pitch"}, {"role": "user", "text": "tell me more"}]
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
        {"role": "user", "text": "what?"},
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
        {"role": "user", "text": "ok"},
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
    """AE8: warm-but-not-ready -> a lower commitment-ladder tier (trial/consultation), not enrollment.

    CB-29: discovery has actually happened (grade + subject confirmed) so the close_floor blind-close
    guard leaves this EARNED close alone; the test pins the TIER selection (warm -> lower than
    enrollment), not premature closing (the blind-close block is covered by test_cb28_cb29)."""
    cfg = make_config(trust_gate_open_price=0.5, pushiness_cap=0.7, pushiness_pressure_count_cap=3)
    b = BeliefState.fresh()
    fill_slot(b, "grade_level", "10", 0.95)  # discovery actually happened
    fill_slot(b, "subject", "algebra", 0.95)
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
        {"role": "user", "text": "no"},
        {"role": "assistant", "act": "attempt_close"},
        {"role": "user", "text": "no again"},
    ]
    llm = MockLLMClient([_proposal_json(act="attempt_close", tier="enrollment", rationale="push")])
    d = await decide(b, history=history, config=cfg, llm_client=llm)
    assert d.act != "attempt_close"


async def test_policy_escalates_on_concession_beyond_band():
    """LLM proposes a deep discount beyond the band DURING live price talk; the policy escalates
    (gates have final say). CB-68 re-spec: the concession must be GROUNDED in price context — a
    price_inquiry turn here; without it the fraction is a hallucination and is stripped instead
    (see test_cb68_concession_grounding for that arm)."""
    cfg = make_config(max_concession_band=0.15)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.7
    b.last_user_act = "price_inquiry"  # CB-68: live price context — the band rule applies
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


# --- FINDING 1: the policy LLM must actually SEE the conversation ----------------------------

async def test_policy_user_message_contains_production_history_text():
    """Regression (FINDING 1): the RECENT_TURNS block sent to the policy LLM must contain the
    prospect's utterance text under the PRODUCTION history key ("text", what selfplay/voice write),
    not the stale "content" key. We capture the rendered policy user message via a callable mock
    and assert the utterance is actually present in the JSON it sends."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.7

    # Production history shape (selfplay.py line 190-192 / voice/session.py): user text under "text".
    utterance = "My daughter needs SAT math help before finals"
    history = [
        {"role": "agent", "act": "greeting", "confidence": None, "text": "Hi, how can I help?"},
        {"role": "user", "text": utterance},
    ]

    captured: dict[str, str] = {}

    def responder(messages, **opts):
        # Record the user-role prompt the policy assembled, then return a benign proposal.
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return _proposal_json(act="pitch", rationale="build value")

    llm = MockLLMClient(responder)
    await decide(b, history=history, config=cfg, llm_client=llm)

    # The prospect's actual words must reach the policy LLM (else the policy is blind in production).
    assert utterance in captured["user"], (
        f"utterance text missing from policy prompt; the RECENT_TURNS block is blind. "
        f"prompt={captured['user']!r}"
    )


# --- FINDING 2: a transient LLM failure must NOT crash the turn ------------------------------

async def test_policy_degrades_safely_on_httpx_error():
    """A real OpenRouter failure (httpx.HTTPStatusError, NOT ValueError) must not crash decide();
    the policy falls back to the SAME safe non-pressure act as the bad-JSON path (FINDING 2)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.7
    llm = _RaisingLLMClient()  # complete_json raises httpx.HTTPStatusError
    d = await decide(b, history=[], config=cfg, llm_client=llm)  # must not raise
    assert isinstance(d, Decision)
    # Safe fallback: never silently closes/pressures on a failed proposal.
    assert d.act not in ("attempt_close",)


async def test_policy_degrades_safely_on_request_error():
    """A transport-level RequestError (timeout/connection) likewise degrades, not crashes."""
    cfg = make_config()
    b = BeliefState.fresh()
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    llm = _RaisingLLMClient(exc=httpx.ConnectTimeout("timed out", request=request))
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert isinstance(d, Decision)
    assert d.act not in ("attempt_close",)


async def test_nlg_returns_filler_on_httpx_error():
    """nlg.realize() must return the safe filler (non-empty turn) when the LLM call fails with a
    transient httpx error rather than letting the exception propagate (FINDING 2)."""
    cfg = make_config()
    b = BeliefState.fresh()
    decision = Decision(act="pitch", rationale="value")
    llm = _RaisingLLMClient()
    reply = await realize(decision, b, cfg, llm)  # must not raise
    assert isinstance(reply, str)
    assert reply.strip(), "realize() must return a non-empty filler on call failure"


# --- address_direct_input (M2: never ignore a direct question/objection for discovery) ---------

def test_address_direct_input_routes_objection_to_handle_objection():
    """A live objection pre-empts a discovery/pitch act -> handle_objection (was: agent looped 'what
    grade?' at a prospect who objected). Close/escalate/already-addressing acts are left alone."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "diy_free"
    for proposed in (
        gates.address_direct_input(Decision(act="ask", target_slot="grade_level"), b, cfg),
        gates.address_direct_input(Decision(act="pitch"), b, cfg),
        gates.address_direct_input(Decision(act="confirm_known"), b, cfg),
    ):
        assert proposed.act == "handle_objection"
    # An attempt_close is NOT rerouted here (must_clear_objection / pushiness own the close path).
    assert gates.address_direct_input(Decision(act="attempt_close", tier="trial"), b, cfg).act == "attempt_close"


def test_address_direct_input_routes_open_question_to_answer_via_kb():
    """A direct question (open_question + question/price_inquiry) pre-empts discovery -> answer_via_kb,
    so the agent answers what was asked instead of re-asking a discovery slot."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "How much per month?"
    b.last_user_act = "price_inquiry"
    out = gates.address_direct_input(Decision(act="ask", target_slot="grade_level"), b, cfg)
    assert out.act == "answer_via_kb"
    # With no question and no objection, a discovery ask is untouched.
    assert gates.address_direct_input(Decision(act="ask", target_slot="goal"), BeliefState.fresh(), cfg).act == "ask"


def test_address_direct_input_lets_pitch_advance_after_an_answer():
    """Reactive-loop fix: a prospect who asks a question EVERY turn must not pin the agent to
    answer_via_kb forever (the can't-close bug). If the agent ALREADY answered last turn, a proposed
    `pitch` is allowed to ADVANCE (not re-deferred); `ask`/`confirm_known` still defer to answer the
    question first (M2 intact); with no prior answer, `pitch` still defers (answer first)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "And what about scheduling?"
    b.last_user_act = "question"
    just_answered = [
        {"role": "agent", "act": "answer_via_kb", "text": "Here's the pricing…"},
        {"role": "user", "text": "And what about scheduling?"},
    ]
    # We just answered -> a pitch may advance the sale this turn.
    assert gates.address_direct_input(Decision(act="pitch"), b, cfg, history=just_answered).act == "pitch"
    # ask/confirm still defer (answer the question before more discovery).
    assert (
        gates.address_direct_input(Decision(act="ask", target_slot="grade_level"), b, cfg, history=just_answered).act
        == "answer_via_kb"
    )
    # No prior answer in history -> pitch still defers to answer the question first.
    assert gates.address_direct_input(Decision(act="pitch"), b, cfg, history=[]).act == "answer_via_kb"


# --- advance_to_close (the can't-close fix: take initiative when the belief is closeable) ----------

def test_advance_to_close_initiates_close_when_warmed():
    """The reactive-agent fix: when trust has warmed above neutral, enough turns have passed, and no
    objection is open, a reactive/stalling act is converted to an attempt_close at the best low-
    commitment tier — so the agent stops answering questions forever and actually asks for the sale.
    The prospect's honest buy-gate still decides; NLG still acknowledges any pending question."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 6
    b.drivers["trust"] = 0.65  # warmed above the 0.5 neutral prior
    out = gates.advance_to_close(Decision(act="answer_via_kb"), b, cfg)
    assert out.act == "attempt_close" and out.tier == "consultation"
    # Very warm + buying intent -> the higher trial rung.
    b.drivers["trust"] = 0.75
    b.drivers["purchase_intent"] = 0.6
    assert gates.advance_to_close(Decision(act="pitch"), b, cfg).tier == "trial"


def test_advance_to_close_holds_off_when_not_ready():
    """No premature/aggressive closing: a neutral or early belief, an open objection, or a non-
    advanceable act (already closing/escalating) is left untouched."""
    cfg = make_config()
    # Neutral fresh belief (trust 0.5) -> never fires.
    assert gates.advance_to_close(Decision(act="answer_via_kb"), BeliefState.fresh(), cfg).act == "answer_via_kb"
    # Warmed but an objection is open -> handle it first, don't close.
    b = BeliefState.fresh(); b.turn_count = 6; b.drivers["trust"] = 0.7; b.active_objection = "price"
    assert gates.advance_to_close(Decision(act="answer_via_kb"), b, cfg).act == "answer_via_kb"
    # Warmed but too early (few turns) -> don't close yet.
    b2 = BeliefState.fresh(); b2.turn_count = 2; b2.drivers["trust"] = 0.7
    assert gates.advance_to_close(Decision(act="answer_via_kb"), b2, cfg).act == "answer_via_kb"
    # Already closing / escalating is left alone.
    b3 = BeliefState.fresh(); b3.turn_count = 6; b3.drivers["trust"] = 0.7
    assert gates.advance_to_close(Decision(act="escalate"), b3, cfg).act == "escalate"
    assert gates.advance_to_close(Decision(act="attempt_close", tier="trial"), b3, cfg).act == "attempt_close"


def test_objection_breakthrough_now_routes_through_the_unified_loop_breaker():
    """The objection-DEADLOCK breakthrough is no longer a special case inside advance_to_close — it is
    SUBSUMED by SAFEGUARD 1's break_no_progress_loop (a relentlessly re-raised objection handled K
    times with no progress is exactly a no-progress reactive run). So advance_to_close LEAVES an open
    objection alone (handle it first), and the deadlock-break now comes from the unified gate via
    apply_gates. (Consolidation: the old `_recent_objection_handles` / `_BREAKTHROUGH_OBJECTION_HANDLES`
    special case was removed and folded here — the unified mechanism is the single owner of loop-breaking.)
    """
    cfg = make_config()
    # advance_to_close alone no longer breaks through an open objection (it handles it first).
    b = BeliefState.fresh(); b.turn_count = 8; b.drivers["trust"] = 0.55; b.active_objection = "efficacy_doubt"
    handled_twice = [
        {"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "..."},
        {"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "..."},
    ]
    assert gates.advance_to_close(Decision(act="handle_objection"), b, cfg, history=handled_twice).act == "handle_objection"

    # The unified breaker (via apply_gates) breaks the deadlock: K reactive handle_objection acts with
    # the objection never clearing (no progress) forces a COMMIT — down-tiered to the free callback rung
    # because the close would be blind (no discovery, neutral intent), so close_floor doesn't veto it.
    from src.core.gates import _LOOP_BREAK_K
    b2 = BeliefState.fresh(); b2.turn_count = 8; b2.drivers["trust"] = 0.55; b2.active_objection = "efficacy_doubt"
    b2.meta = {
        "recent_acts": ["handle_objection"] * _LOOP_BREAK_K,
        "progress_snapshot": {"locked_slots": 0, "active_objection": "efficacy_doubt", "purchase_intent": 0.5},
    }
    history = [
        {"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "still no"},
    ] * _LOOP_BREAK_K
    out = apply_gates(Decision(act="handle_objection"), b2, cfg, history=history)
    assert out.act == "attempt_close", "the unified loop-breaker must break the objection deadlock"

    # Below K reactive acts (only one prior handle), the breaker does NOT fire — keep handling.
    b3 = BeliefState.fresh(); b3.turn_count = 8; b3.drivers["trust"] = 0.55; b3.active_objection = "efficacy_doubt"
    b3.meta = {
        "recent_acts": ["handle_objection"],
        "progress_snapshot": {"locked_slots": 0, "active_objection": "efficacy_doubt", "purchase_intent": 0.5},
    }
    out_keep = apply_gates(Decision(act="handle_objection"), b3, cfg, history=[
        {"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "..."},
    ])
    assert out_keep.act == "handle_objection"


def test_advance_to_close_offers_free_callback_when_budget_constrained():
    """A budget-constrained prospect (high price-sensitivity OR firm refusals) gets the FREE callback
    rung — never a paid consultation they'll reject (your 'unqualified still get a meeting' rule)."""
    cfg = make_config()
    b = BeliefState.fresh(); b.turn_count = 6; b.drivers["trust"] = 0.7; b.drivers["price_sensitivity"] = 0.8
    assert gates.advance_to_close(Decision(act="answer_via_kb"), b, cfg).tier == "callback"
    b2 = BeliefState.fresh(); b2.turn_count = 6; b2.drivers["trust"] = 0.7; b2.meta["affordability_refusal_count"] = 2
    assert gates.advance_to_close(Decision(act="answer_via_kb"), b2, cfg).tier == "callback"


# --- no_authority_prefers_callback (CB-50: gatekeeper -> callback, not human escalation) -------

def test_no_authority_redirects_proposed_escalate_to_callback():
    """CB-50: a no-authority gatekeeper (decision_maker objection, no genuine escalation trigger) gets
    a low-touch callback offer, NOT a wasted live-human escalation. The LLM proposed `escalate`
    purely because the caller isn't the decision-maker — the gate down-tiers it to attempt_close@callback."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "decision_maker"  # "I'll have to check with my sister"
    b.last_user_act = "objection"
    proposed = Decision(act="escalate", rationale="caller isn't the decision-maker")
    out = gates.no_authority_prefers_callback(proposed, b, cfg)
    assert out.act == "attempt_close"
    assert out.tier == "callback"
    assert out.act != "escalate"


def test_no_authority_does_not_redirect_a_genuine_escalation():
    """The gate is conservative: it only redirects an escalate that's driven by the no-authority read.
    A REAL trigger (explicit human request) still escalates — escalation_triggers owns that and runs
    before/after this gate, so the full chain keeps escalating; the gate itself leaves it alone when
    there is no no_authority signal."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.last_user_act = "human_request"  # a genuine escalation cue, NOT the gatekeeper situation
    proposed = Decision(act="escalate", rationale="they asked for a person")
    out = gates.no_authority_prefers_callback(proposed, b, cfg)
    assert out.act == "escalate"  # no decision_maker signal -> the gate doesn't touch it


def test_no_authority_leaves_non_escalate_acts_alone():
    """The gate only rewrites an `escalate`; a normal answer/pitch on a decision_maker objection is
    untouched (must_clear_objection / handle_objection own the in-conversation handling)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "decision_maker"
    for proposed in (Decision(act="handle_objection"), Decision(act="answer_via_kb"), Decision(act="pitch")):
        out = gates.no_authority_prefers_callback(proposed, b, cfg)
        assert out.act == proposed.act


def test_apply_gates_no_authority_gatekeeper_books_callback_not_escalate():
    """Full chain (the Pat defect, CB-50): on a no-authority read with NO genuine escalation trigger,
    apply_gates must NOT yield `escalate` — it routes to a callback close. Mirrors a turn-1/2
    gatekeeper where the LLM proposed escalate."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "decision_maker"
    b.last_user_act = "objection"
    b.turn_count = 1
    proposed = Decision(act="escalate", rationale="not the decision-maker")
    out = apply_gates(proposed, b, cfg, history=[])
    assert out.act != "escalate"
    assert out.act == "attempt_close" and out.tier == "callback"


def test_apply_gates_genuine_human_request_still_escalates():
    """Guardrail: a real escalation trigger (explicit human request) still escalates through the full
    chain even when a decision_maker objection is ALSO present — escalation_triggers wins. We never
    suppress a genuine escalation just because the caller is also a gatekeeper."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "decision_maker"
    b.last_user_act = "human_request"  # genuine trigger present
    proposed = Decision(act="pitch", rationale="benign")
    out = apply_gates(proposed, b, cfg, history=[])
    assert out.act == "escalate"


async def test_policy_no_authority_prefers_callback_over_escalate():
    """Policy-level (LLM proposes -> gates decide): the LLM proposes `escalate` for a no-authority
    gatekeeper; the policy returns a callback close instead of wasting a human (CB-50)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "decision_maker"
    b.last_user_act = "objection"
    b.turn_count = 1
    llm = MockLLMClient([_proposal_json(act="escalate", rationale="caller can't decide")])
    d = await decide(b, history=[], config=cfg, llm_client=llm)
    assert d.act != "escalate"
    assert d.act == "attempt_close" and d.tier == "callback"


# --- escalate_only_if_warranted (CB-50 broadening: a TRIGGERLESS escalate must not terminate) --
# The eval showed the agent reach for "let me connect you with a specialist who'll call you back"
# as a DEFAULT forward move — terminally escalating with zero discovery (Pat t1) or punting a
# question to "a consultant will explain" (Dana). A proposed `escalate` with NO genuine escalation
# reason (distress / explicit human request / compliance / low-confidence streak / over-band
# concession) is NOT a hand-off — it is the agent bailing. So it is redirected: a gatekeeper /
# no-authority read books a callback; otherwise the agent keeps selling (answer the open question,
# else pitch). A GENUINE escalation reason still escalates immediately.

def test_triggerless_escalate_on_qualified_belief_redirects_to_pitch():
    """A triggerless escalate on a QUALIFIED belief with NO open question keeps the agent selling —
    redirected to a non-terminal pitch, never `escalate` (the Dana/Marcus over-eager-escalate)."""
    cfg = make_config()
    b = BeliefState.fresh()
    fill_slot(b, "grade_level", "8", 0.9)
    fill_slot(b, "subject", "math", 0.9)  # qualified, no gatekeeper read, no open question
    b.turn_count = 4
    proposed = Decision(act="escalate", rationale="let me hand you to a specialist")
    out = gates.escalate_only_if_warranted(proposed, b, cfg, history=[])
    assert out.act != "escalate"
    assert out.act == "pitch"


def test_triggerless_escalate_with_open_question_answers_via_kb():
    """A triggerless escalate while the prospect has an OPEN QUESTION answers it (don't punt the
    proof question to 'a consultant will explain' — the Dana regression)."""
    cfg = make_config()
    b = BeliefState.fresh()
    fill_slot(b, "grade_level", "8", 0.9)
    b.open_question = "do you have proof it actually works?"
    b.last_user_act = "question"
    b.turn_count = 4
    proposed = Decision(act="escalate", rationale="a consultant will explain")
    out = gates.escalate_only_if_warranted(proposed, b, cfg, history=[])
    assert out.act == "answer_via_kb"
    assert out.act != "escalate"


def test_triggerless_escalate_on_no_authority_books_callback():
    """A triggerless escalate on a no-authority/gatekeeper read books a CALLBACK for the real decider
    (the existing CB-50 behavior, now also reachable from this guard, not just the explicit objection)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.meta["no_authority"] = True  # gatekeeper read WITHOUT the explicit decision_maker objection
    proposed = Decision(act="escalate", rationale="caller isn't the decider")
    out = gates.escalate_only_if_warranted(proposed, b, cfg, history=[])
    assert out.act == "attempt_close" and out.tier == "callback"
    assert out.act != "escalate"


def test_genuine_escalation_still_escalates_through_the_guard():
    """A GENUINE escalation reason (explicit human request) is NOT redirected — it still escalates.
    Distress / compliance / low-confidence streak / over-band concession behave the same."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.last_user_act = "human_request"  # genuine trigger
    proposed = Decision(act="escalate", rationale="they asked for a person")
    out = gates.escalate_only_if_warranted(proposed, b, cfg, history=[])
    assert out.act == "escalate"


def test_guard_leaves_non_escalate_acts_alone():
    """The guard ONLY rewrites a proposed `escalate`; any other act passes untouched."""
    cfg = make_config()
    b = BeliefState.fresh()
    for proposed in (Decision(act="pitch"), Decision(act="answer_via_kb"), Decision(act="ask", target_slot="goal")):
        out = gates.escalate_only_if_warranted(proposed, b, cfg, history=[])
        assert out.act == proposed.act


def test_apply_gates_triggerless_escalate_no_authority_phrase_books_callback():
    """Full chain (Pat t1, CB-50): a gatekeeper speaking FOR someone ('my sister's kid') is read as
    no-authority via the cheap 3rd-party phrase even WITHOUT the explicit decision_maker objection;
    a triggerless escalate routes to a callback close, never a terminal hand-off."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "I'm calling for my sister's kid, do you do middle-school math?"
    b.turn_count = 1
    proposed = Decision(act="escalate", rationale="not the parent")
    out = apply_gates(proposed, b, cfg, history=[])
    assert out.act != "escalate"
    assert out.act == "attempt_close" and out.tier == "callback"


def test_apply_gates_triggerless_escalate_qualified_keeps_selling():
    """Full chain (Dana): a triggerless escalate on a qualified prospect with an open question is
    redirected to a non-terminal selling act so the agent can still reach a proper close later —
    NOT a terminal escalate."""
    cfg = make_config()
    b = BeliefState.fresh()
    fill_slot(b, "grade_level", "8", 0.9)
    fill_slot(b, "subject", "math", 0.9)
    b.open_question = "can you prove the results are real?"
    b.last_user_act = "question"
    b.turn_count = 4
    proposed = Decision(act="escalate", rationale="hand off the proof question")
    out = apply_gates(proposed, b, cfg, history=[])
    assert out.act != "escalate"
    assert out.act in ("answer_via_kb", "pitch", "attempt_close", "handle_objection")


def test_apply_gates_genuine_escalation_survives_full_chain():
    """Full chain guardrail: a real escalation trigger still escalates end-to-end (Marcus's truly-
    unanswerable scheduling case / distress / explicit request) — the guard never suppresses it."""
    cfg = make_config()
    b = BeliefState.fresh()
    fill_slot(b, "grade_level", "8", 0.9)
    b.last_user_act = "human_request"
    b.turn_count = 6
    proposed = Decision(act="escalate", rationale="explicitly asked for a person")
    out = apply_gates(proposed, b, cfg, history=[])
    assert out.act == "escalate"


# --- close_floor (CB-29: no blind premature/pushy paid close) ----------------------------------

def test_close_floor_blocks_blind_paid_close():
    """A paid close with NO discovery slots filled AND no readiness signal (purchase_intent at/below
    the neutral prior) is the blind premature close the real call exhibited — backed off, not fired."""
    cfg = make_config(discovery_slots_before_close=1)
    b = BeliefState.fresh()
    b.turn_count = 9  # the real call's pushy-close turn — turns alone don't make it legitimate
    b.drivers["trust"] = 0.7
    b.drivers["purchase_intent"] = 0.5  # neutral — no buying signal
    out = gates.close_floor(Decision(act="attempt_close", tier="trial", rationale="push"), b, cfg)
    assert out.act != "attempt_close"


def test_close_floor_allows_close_when_discovery_happened():
    """Once a real discovery slot is filled, the close is EARNED and the floor leaves it alone."""
    cfg = make_config(discovery_slots_before_close=1)
    b = BeliefState.fresh()
    b.drivers["purchase_intent"] = 0.4  # not yet ready, but discovery has happened
    fill_slot(b, "grade_level", "11", 0.95)
    out = gates.close_floor(Decision(act="attempt_close", tier="consultation"), b, cfg)
    assert out.act == "attempt_close" and out.tier == "consultation"


def test_close_floor_allows_close_on_readiness_signal():
    """A genuinely hot prospect (purchase_intent above the neutral prior) can close even before a slot
    is recorded — the agent can read readiness directly; the floor only blocks BLIND closes."""
    cfg = make_config(discovery_slots_before_close=1)
    b = BeliefState.fresh()
    b.drivers["purchase_intent"] = 0.82  # explicit buying signal
    out = gates.close_floor(Decision(act="attempt_close", tier="enrollment"), b, cfg)
    assert out.act == "attempt_close" and out.tier == "enrollment"


def test_close_floor_exempts_free_callback_rung():
    """The free callback rung is never pushy — the floor never blocks it (even closing blind)."""
    cfg = make_config(discovery_slots_before_close=1)
    b = BeliefState.fresh()  # no discovery, no readiness
    out = gates.close_floor(Decision(act="attempt_close", tier="callback"), b, cfg)
    assert out.act == "attempt_close" and out.tier == "callback"

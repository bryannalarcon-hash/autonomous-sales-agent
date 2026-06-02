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
        {"role": "user", "text": "no"},
        {"role": "assistant", "act": "attempt_close"},
        {"role": "user", "text": "no again"},
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


def test_advance_to_close_breaks_through_repeatedly_handled_objection():
    """A relentless objector (re-raising the same objection every turn) would otherwise pin the agent
    to handle_objection forever. After the objection has been handled repeatedly, break the deadlock
    with a low-pressure consultation offer instead of handling it yet again."""
    cfg = make_config()
    b = BeliefState.fresh(); b.turn_count = 8; b.drivers["trust"] = 0.55; b.active_objection = "efficacy_doubt"
    handled_twice = [
        {"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "..."},
        {"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "..."},
    ]
    out = gates.advance_to_close(Decision(act="handle_objection"), b, cfg, history=handled_twice)
    assert out.act == "attempt_close" and out.tier == "consultation"
    # With only ONE prior handle, keep handling (no premature break-through).
    handled_once = [{"role": "agent", "act": "handle_objection"}, {"role": "user", "text": "..."}]
    assert gates.advance_to_close(Decision(act="handle_objection"), b, cfg, history=handled_once).act == "handle_objection"


def test_advance_to_close_offers_free_callback_when_budget_constrained():
    """A budget-constrained prospect (high price-sensitivity OR firm refusals) gets the FREE callback
    rung — never a paid consultation they'll reject (your 'unqualified still get a meeting' rule)."""
    cfg = make_config()
    b = BeliefState.fresh(); b.turn_count = 6; b.drivers["trust"] = 0.7; b.drivers["price_sensitivity"] = 0.8
    assert gates.advance_to_close(Decision(act="answer_via_kb"), b, cfg).tier == "callback"
    b2 = BeliefState.fresh(); b2.turn_count = 6; b2.drivers["trust"] = 0.7; b2.meta["affordability_refusal_count"] = 2
    assert gates.advance_to_close(Decision(act="answer_via_kb"), b2, cfg).tier == "callback"

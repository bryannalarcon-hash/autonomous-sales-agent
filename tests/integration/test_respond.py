# U4 integration tests (TEST-FIRST) for the transport-agnostic respond() core (src/core/respond.py).
# respond() orchestrates dst.update -> policy.decide (gates already applied inside) -> nlg.realize,
# returning (Decision, reply_text, new_belief). These tests drive the WHOLE brain with a scripted
# MockLLMClient (no network, no DB, NO LiveKit) and assert the end-to-end contract for the adversarial
# examples: AE2 (price before trust -> re-anchor, no quote), AE3 (no-budget -> graceful release),
# AE8 (hot -> enrollment / warm -> trial), plus the gate guarantees surfacing through respond():
# skip-known, objection-blocks-close, pushiness cap, and concession-beyond-band escalation.
# The MockLLMClient is scripted call-by-call: respond() makes a DST driver-delta call, then a policy
# proposal call, then an NLG realization call — so each script is [dst_deltas, policy_json, nlg_text].
from __future__ import annotations

import json

import pytest

from src.config.settings import AgentConfig, Persona
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import MockLLMClient
from src.core.policy import Decision
from src.core.respond import respond


# --- helpers ----------------------------------------------------------------------------------

def make_config(**threshold_overrides) -> AgentConfig:
    thresholds = {
        "trust_gate_open_price": 0.5,
        "pushiness_cap": 0.7,
        "pushiness_pressure_count_cap": 2,
        "escalate_low_confidence_turns": 2,
        "low_confidence_level": 0.4,
        "max_concession_band": 0.15,
        "discovery_slots_required": 0,
    }
    thresholds.update(threshold_overrides)
    return AgentConfig(
        version="test_v0",
        kb_version="kb_test",
        persona=Persona(name="Alex", role="learning advisor", style="warm-consultative"),
        prompts={
            "policy": "Propose the next system act as JSON.",
            "nlg": "Reply as a warm, low-pressure learning advisor. Be concise.",
        },
        playbooks={"discovery_sequence": ["grade_level", "subject", "goal", "timeline", "budget"]},
        thresholds=thresholds,
    )


def _flat_deltas() -> str:
    return json.dumps({d: 0.0 for d in DRIVERS})


def _policy_json(**kwargs) -> str:
    payload = {"act": kwargs.get("act", "pitch"), "rationale": kwargs.get("rationale", "")}
    for k in ("target_slot", "tier", "confidence", "concession"):
        if k in kwargs:
            payload[k] = kwargs[k]
    return json.dumps(payload)


def script(*, deltas: str | None = None, policy: str, nlg: str = "Sure, happy to help.") -> MockLLMClient:
    """A 3-call script for one respond() turn: DST deltas, policy proposal, NLG text."""
    return MockLLMClient([deltas or _flat_deltas(), policy, nlg])


# --- the respond() contract -------------------------------------------------------------------

async def test_respond_returns_decision_reply_and_new_belief():
    """respond() returns (Decision, reply_text:str, new_belief:BeliefState) and advances the turn."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.7
    llm = script(policy=_policy_json(act="pitch", rationale="value"), nlg="Here is how we help.")

    decision, reply, new_belief = await respond(
        b, history=[], user_utterance="Tell me about your tutoring.", llm_client=llm, config=cfg
    )

    assert isinstance(decision, Decision)
    assert isinstance(reply, str) and reply.strip()
    assert isinstance(new_belief, BeliefState)
    # DST advanced the turn; the original belief is not mutated (pure core).
    assert new_belief.turn_count == b.turn_count + 1
    assert b.turn_count == 0


def test_respond_does_not_leak_livekit_types():
    """The core boundary is transport-agnostic: no U4 core module IMPORTS livekit (prose mentioning
    the boundary in a header comment is fine; an `import livekit` / `from livekit ...` is not)."""
    import ast
    import importlib
    from pathlib import Path

    # Import the modules explicitly (not the re-exported symbols, which shadow the module names in
    # src.core.__init__) and parse their imports to assert the import ban structurally.
    module_names = (
        "src.core.respond",
        "src.core.policy",
        "src.core.gates",
        "src.core.nlg",
    )
    for name in module_names:
        mod = importlib.import_module(name)
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "livekit" not in alias.name.lower(), f"{name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                assert "livekit" not in (node.module or "").lower(), f"{name} imports from livekit"


# --- AE2: prospect asks price before trust -> answer it, never ask THEIR budget ---------------

async def test_respond_ae2_price_inquiry_is_answered_not_budget_asked():
    """AE2 (evolved for M2): when the prospect ASKS about price before trust, the agent ADDRESSES the
    question (answer_via_kb, grounded in the value-framed pricing KB) instead of ignoring it to keep
    doing discovery — and it still never ASKS the prospect's budget prematurely. The 'don't ask budget
    before trust' property is enforced by price_gate and unit-tested in
    test_policy.test_price_gate_blocks_opening_price_before_trust; here we assert the prospect's direct
    price question is not dodged (the M2 fix: address_direct_input routes the discovery proposal to
    answer_via_kb)."""
    cfg = make_config(trust_gate_open_price=0.6, discovery_slots_required=3)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.25
    # DST keeps trust low; policy proposes asking budget; the prospect ASKED price, so the gate routes
    # to answering their question (never to asking THEIR budget).
    llm = MockLLMClient(
        [
            json.dumps({"trust": 0.0, "price_sensitivity": 0.1}),  # dst deltas
            _policy_json(act="ask", target_slot="budget", rationale="ask budget"),  # policy
            "Our memberships are tiered, so I can walk you through what fits.",  # nlg
        ]
    )
    decision, reply, new_belief = await respond(
        b, history=[], user_utterance="How much does this cost?", llm_client=llm, config=cfg
    )
    # Never ask the prospect's budget before trust...
    assert not (decision.act == "ask" and decision.target_slot == "budget")
    # ...and ADDRESS the price question rather than loop discovery / cold-deflect (M2).
    assert decision.act == "answer_via_kb"
    assert new_belief.last_user_act == "price_inquiry"


# --- AE3: no-budget prospect -> graceful release, not a forced close --------------------------

async def test_respond_ae3_no_budget_prospect_is_released_not_closed():
    """AE3: a genuinely-unqualified (no-budget) prospect is gracefully released (disqualify)."""
    cfg = make_config()
    b = BeliefState.fresh()
    llm = MockLLMClient(
        [
            json.dumps({"purchase_intent": -0.2, "bail_risk": 0.2}),  # dst deltas
            _policy_json(act="disqualify", rationale="states there is genuinely no budget"),  # policy
            "Totally understand — when the timing is right, here's how to reach us.",  # nlg
        ]
    )
    decision, reply, new_belief = await respond(
        b,
        history=[],
        user_utterance="Honestly we have no budget for tutoring at all.",
        llm_client=llm,
        config=cfg,
    )
    assert decision.act == "disqualify"
    assert decision.act != "attempt_close"


# --- AE8: hot -> enrollment, warm -> trial/consult --------------------------------------------

async def test_respond_ae8_hot_prospect_closes_at_enrollment():
    """AE8: a hot prospect (high trust/need/intent) closes at the enrollment tier through respond()."""
    cfg = make_config(pushiness_pressure_count_cap=3)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.85
    b.drivers["need_intensity"] = 0.8
    b.drivers["purchase_intent"] = 0.82
    b.drivers["bail_risk"] = 0.1
    llm = MockLLMClient(
        [
            json.dumps({"purchase_intent": 0.05}),
            _policy_json(act="attempt_close", tier="enrollment", rationale="hot, ready"),
            "Let's get your daughter enrolled and matched with a tutor this week.",
        ]
    )
    decision, reply, new_belief = await respond(
        b,
        history=[],
        user_utterance="This sounds perfect, how do we get started?",
        llm_client=llm,
        config=cfg,
    )
    assert decision.act == "attempt_close"
    assert decision.tier == "enrollment"


async def test_respond_ae8_warm_prospect_offers_lower_ladder_tier():
    """AE8: a warm-but-not-ready prospect is offered a lower commitment-ladder tier, not enrollment."""
    cfg = make_config(pushiness_pressure_count_cap=3)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.6
    b.drivers["need_intensity"] = 0.5
    b.drivers["purchase_intent"] = 0.4
    b.drivers["bail_risk"] = 0.3
    llm = MockLLMClient(
        [
            json.dumps({"trust": 0.05}),
            _policy_json(act="attempt_close", tier="trial", rationale="warm, ease in"),
            "How about we start with a free trial session so you can see the fit?",
        ]
    )
    decision, reply, new_belief = await respond(
        b,
        history=[],
        user_utterance="Sounds interesting but I'm not sure yet.",
        llm_client=llm,
        config=cfg,
    )
    assert decision.act == "attempt_close"
    assert decision.tier in ("trial", "consultation", "callback")
    assert decision.tier != "enrollment"


# --- gate guarantees surfacing through respond() ----------------------------------------------

async def test_respond_skip_known_never_reasks_a_filled_slot():
    """skip-known surfaces through respond(): a slot already known is not re-asked."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.set_slot("grade_level", "11", 0.9)
    b.drivers["trust"] = 0.7
    llm = MockLLMClient(
        [
            _flat_deltas(),
            _policy_json(act="ask", target_slot="grade_level", rationale="re-ask grade"),
            "Got it — and what subject are we focusing on?",
        ]
    )
    decision, reply, new_belief = await respond(
        b, history=[], user_utterance="ok", llm_client=llm, config=cfg
    )
    assert not (decision.act == "ask" and decision.target_slot == "grade_level")


async def test_respond_objection_blocks_close():
    """An open objection blocks a close through respond(); it routes to handle_objection."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.active_objection = "price"
    b.drivers["trust"] = 0.7
    llm = MockLLMClient(
        [
            _flat_deltas(),
            _policy_json(act="attempt_close", tier="enrollment", rationale="close now"),
            "I hear you on cost — let me put that in perspective.",
        ]
    )
    decision, reply, new_belief = await respond(
        b, history=[], user_utterance="It just seems expensive.", llm_client=llm, config=cfg
    )
    assert decision.act != "attempt_close"
    assert decision.act == "handle_objection"


async def test_respond_escalates_on_concession_beyond_band():
    """A concession beyond the allowed band escalates through respond() and marks the belief."""
    cfg = make_config(max_concession_band=0.15)
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.7
    llm = MockLLMClient(
        [
            _flat_deltas(),
            _policy_json(act="handle_objection", rationale="give them 50% off", concession=0.50),
            "Let me bring in a specialist who can look at options with you.",
        ]
    )
    decision, reply, new_belief = await respond(
        b,
        history=[],
        user_utterance="I'll only do it if you cut the price in half.",
        llm_client=llm,
        config=cfg,
    )
    assert decision.act == "escalate"
    assert new_belief.escalation_imminent is True

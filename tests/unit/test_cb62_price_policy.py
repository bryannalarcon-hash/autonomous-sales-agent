# Regression tests (TEST-FIRST) for CB-62 — price-question policy: offer the KB-grounded ballpark
# instead of pure deflection. QA evidence: a direct price ask with stated budget concern yielded a
# tone-deaf "Perfect — let me get you connected…"; the KB-grounded range was never offered.
#
# What we assert (deterministic, no real LLM calls):
#   1. _build_messages injects _BUDGET_CONCERN_NOTE when last_user_act == "price_inquiry" AND the
#      gated act is answer_via_kb or handle_objection.
#   2. _BUDGET_CONCERN_NOTE is NOT injected for a bare pitch / attempt_close (agent-initiated acts
#      don't carry a direct question signal).
#   3. price_gate passes an answer_via_kb act on a price_inquiry (the gate is already open because
#      the prospect asked unprompted — address_direct_input routes to answer_via_kb, price_gate
#      checks "prospect asked price unprompted" so it does NOT redirect to a pitch).
#   4. address_direct_input routes a price_inquiry to answer_via_kb even from a deferrable proposal
#      (ask / pitch) — the decision path yields an answer act on a direct price question.
#   5. The recurring-price-ask path (CB-48 machinery) also yields answer_via_kb over a proposed
#      attempt_close — the budget-concern ack instruction is still injected.
#   6. The open_question / "how much" / "cost" text-based price detection also triggers the note
#      (covers cases where last_user_act may not be set but the question text is price-related).
# Network-free: MockLLMClient + direct gate / nlg function calls.
from __future__ import annotations

import json

import pytest

from src.config.settings import AgentConfig, Persona
from src.core import gates
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import MockLLMClient
from src.core.nlg import _BUDGET_CONCERN_NOTE, _build_messages, realize
from src.core.policy import Decision
from src.core.respond import respond


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _cfg(**threshold_overrides) -> AgentConfig:
    thresholds = {
        "trust_gate_open_price": 0.5,
        "pushiness_cap": 0.7,
        "pushiness_pressure_count_cap": 2,
        "escalate_low_confidence_turns": 2,
        "low_confidence_level": 0.4,
        "max_concession_band": 0.15,
        "discovery_slots_required": 0,
        "min_turns_before_close": 99,
        "discovery_slots_before_close": 0,
        "extreme_bail_deescalate": 0.9,
        "max_slot_asks": 2,
    }
    thresholds.update(threshold_overrides)
    return AgentConfig(
        version="test_v0",
        kb_version="kb_test",
        persona=Persona(name="Nerdy", role="learning advisor", style="warm-consultative"),
        prompts={"policy": "Propose next act as JSON.", "nlg": "Reply concisely."},
        playbooks={"discovery_sequence": ["grade_level", "subject", "goal", "timeline", "budget"]},
        thresholds=thresholds,
    )


def _decision(act: str = "answer_via_kb", **kw) -> Decision:
    return Decision(act=act, rationale="test", confidence=0.75, **kw)


def _flat_deltas() -> str:
    return json.dumps({d: 0.0 for d in DRIVERS})


def _policy_json(act: str) -> str:
    return json.dumps({"act": act, "rationale": "test", "confidence": 0.75})


# ---------------------------------------------------------------------------
# CB-62 test 1: _BUDGET_CONCERN_NOTE injected on price_inquiry + answer_via_kb
# ---------------------------------------------------------------------------

def test_budget_concern_note_injected_on_price_inquiry_answer_via_kb():
    """_build_messages must inject _BUDGET_CONCERN_NOTE when the belief signals a direct price question
    (last_user_act == 'price_inquiry') and the gated act is answer_via_kb — the shape produced by
    address_direct_input routing a price_inquiry to an answer act."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "price_inquiry"
    belief.open_question = "How much does this cost? We're on a tight budget."
    dec = _decision("answer_via_kb")

    msgs = _build_messages(dec, belief, cfg, retrieved_facts=None)
    user_content = msgs[-1]["content"]

    assert "PRICE QUESTION POLICY" in user_content, (
        "_BUDGET_CONCERN_NOTE must be present in the NLG prompt on a price_inquiry answer turn"
    )
    assert _BUDGET_CONCERN_NOTE[:40] in user_content


def test_budget_concern_note_injected_on_price_inquiry_handle_objection():
    """_BUDGET_CONCERN_NOTE is also injected for handle_objection on a price question — a 'too
    expensive' objection is a price question variant that deserves the same empathetic ack."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "price_inquiry"
    belief.active_objection = "price"
    belief.open_question = "That sounds expensive — what does it cost?"
    dec = _decision("handle_objection")

    msgs = _build_messages(dec, belief, cfg, retrieved_facts=None)
    user_content = msgs[-1]["content"]

    assert "PRICE QUESTION POLICY" in user_content, (
        "_BUDGET_CONCERN_NOTE must appear on handle_objection when prospect asked a price question"
    )


def test_budget_concern_note_injected_on_open_question_text_how_much():
    """Price detection via open_question text ('how much') also triggers the note — covers cases where
    last_user_act is not set to price_inquiry but the question is clearly about cost."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"  # not price_inquiry — text-based detection path
    belief.open_question = "How much would this program run us per month?"
    dec = _decision("answer_via_kb")

    msgs = _build_messages(dec, belief, cfg, retrieved_facts=None)
    user_content = msgs[-1]["content"]

    assert "PRICE QUESTION POLICY" in user_content, (
        "open_question text containing 'how much' must trigger the budget-concern note"
    )


def test_budget_concern_note_injected_on_open_question_text_cost():
    """Price detection via open_question text ('cost') triggers the note."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.open_question = "What is the cost for a typical family?"
    dec = _decision("answer_via_kb")

    msgs = _build_messages(dec, belief, cfg, retrieved_facts=None)
    assert "PRICE QUESTION POLICY" in msgs[-1]["content"]


# ---------------------------------------------------------------------------
# CB-62 test 2: _BUDGET_CONCERN_NOTE NOT injected on agent-initiated acts
# ---------------------------------------------------------------------------

def test_budget_concern_note_not_injected_on_pitch():
    """A bare pitch (agent-initiated value re-anchor) must NOT carry the budget-concern note — the
    prospect did not ask a price question, so the tone-deaf ack instruction is irrelevant."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"
    belief.open_question = "Tell me more about the program."
    dec = _decision("pitch")

    msgs = _build_messages(dec, belief, cfg, retrieved_facts=None)
    assert "PRICE QUESTION POLICY" not in msgs[-1]["content"], (
        "_BUDGET_CONCERN_NOTE must NOT be injected for a pitch with no price question"
    )


def test_budget_concern_note_not_injected_on_attempt_close():
    """An attempt_close is not a response to a direct price question — no budget-concern note."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"
    dec = _decision("attempt_close", tier="consultation")

    msgs = _build_messages(dec, belief, cfg, retrieved_facts=None)
    assert "PRICE QUESTION POLICY" not in msgs[-1]["content"]


def test_budget_concern_note_not_injected_on_ask():
    """A bare discovery ask carries no budget-concern note."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"
    dec = _decision("ask", target_slot="grade_level")

    msgs = _build_messages(dec, belief, cfg, retrieved_facts=None)
    assert "PRICE QUESTION POLICY" not in msgs[-1]["content"]


# ---------------------------------------------------------------------------
# CB-62 test 3: price_gate passes answer_via_kb on a direct price_inquiry
# ---------------------------------------------------------------------------

def test_price_gate_passes_answer_via_kb_on_price_inquiry():
    """price_gate._opens_price() only triggers on ask/answer_via_kb/confirm_known with
    target_slot in _PRICE_SLOTS (i.e. target_slot='budget'). An answer_via_kb with NO target_slot
    (the typical price answer routing) is NOT intercepted by price_gate — the gate is already open
    because last_user_act == 'price_inquiry' satisfies _price_gate_open()."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "price_inquiry"
    dec = _decision("answer_via_kb")  # no target_slot

    gated = gates.price_gate(dec, belief, cfg)
    assert gated.act == "answer_via_kb", (
        "price_gate must leave an answer_via_kb with no budget target_slot untouched on a price_inquiry"
    )


def test_price_gate_open_on_price_inquiry_regardless_of_trust():
    """The price gate is open when the prospect asked about price (last_user_act == price_inquiry),
    regardless of trust level — the prospect's own question unlocks the answer."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "price_inquiry"
    belief.drivers["trust"] = 0.1  # low trust — would normally be gated
    dec = _decision("answer_via_kb", target_slot="budget")  # target_slot triggers _opens_price

    gated = gates.price_gate(dec, belief, cfg)
    assert gated.act == "answer_via_kb", (
        "price_gate must be open on a direct price_inquiry even at low trust"
    )


# ---------------------------------------------------------------------------
# CB-62 test 4: address_direct_input routes price_inquiry to answer_via_kb
# ---------------------------------------------------------------------------

def test_address_direct_input_routes_price_inquiry_to_answer_via_kb_from_pitch():
    """A proposed `pitch` is deflected to answer_via_kb when open_question is set and
    last_user_act is price_inquiry — the agent must answer the price question, not pitch over it."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "price_inquiry"
    belief.open_question = "How much does tutoring cost?"
    dec = _decision("pitch")

    gated = gates.address_direct_input(dec, belief, cfg)
    assert gated.act == "answer_via_kb", (
        "address_direct_input must redirect a pitch to answer_via_kb on a price_inquiry"
    )


def test_address_direct_input_routes_price_inquiry_to_answer_via_kb_from_ask():
    """A proposed `ask` (discovery) is also deflected to answer_via_kb on a price_inquiry — never
    slot-fill over an unanswered direct price question."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "price_inquiry"
    belief.open_question = "What would this cost me monthly?"
    dec = _decision("ask", target_slot="grade_level")

    gated = gates.address_direct_input(dec, belief, cfg)
    assert gated.act == "answer_via_kb", (
        "address_direct_input must redirect an ask to answer_via_kb on a price_inquiry"
    )


# ---------------------------------------------------------------------------
# CB-62 test 5: recurring price ask forces answer_via_kb over an attempt_close
# ---------------------------------------------------------------------------

def test_recurring_price_ask_forces_answer_over_close():
    """CB-48 recurring-question machinery: when the prospect asks about price, then asks AGAIN, the
    second ask is a recurring price_inquiry — address_direct_input must force answer_via_kb even over
    a proposed attempt_close. The budget-concern ack instruction is still injected in the prompt.

    _question_recurs compares the CURRENT open_question against PRIOR user turns (all turns except the
    most recent user turn). We supply 3 turns (2 user + 1 agent) so the earlier user turn is available
    for comparison against the current open_question."""
    cfg = _cfg(min_turns_before_close=0)  # allow closes to be proposed
    belief = BeliefState.fresh()
    belief.last_user_act = "price_inquiry"
    belief.open_question = "Seriously, how much does this cost?"
    # Two user turns so _question_recurs sees the earlier one as a "prior" turn:
    # the earlier "how much" question overlaps with "how much does this cost" content words.
    history = [
        {"role": "user", "text": "How much does this cost per month?"},  # prior user turn
        {"role": "assistant", "text": "We build the price in the consultation.", "act": "answer_via_kb"},
        {"role": "user", "text": "Seriously, how much does this cost?"},  # the current (most recent) user turn
    ]
    dec = _decision("attempt_close", tier="consultation")

    gated = gates.address_direct_input(dec, belief, cfg, history=history)
    assert gated.act == "answer_via_kb", (
        "address_direct_input must force answer_via_kb over a close on a recurring price question (CB-48)"
    )

    # The NLG prompt for this answer_via_kb turn must also carry the budget-concern note.
    msgs = _build_messages(gated, belief, cfg, retrieved_facts=None)
    assert "PRICE QUESTION POLICY" in msgs[-1]["content"], (
        "_BUDGET_CONCERN_NOTE must be present in the NLG prompt even after the CB-48 recurring redirect"
    )


# ---------------------------------------------------------------------------
# CB-62 test 6: KB facts in the prompt when retrieved_facts are supplied
# ---------------------------------------------------------------------------

def test_kb_pricing_facts_appear_in_prompt_when_supplied():
    """When retrieved_facts containing a pricing range are passed to _build_messages, the grounding
    block surfaces them in the prompt — this is the mechanism by which the agent's ballpark is
    KB-grounded rather than hardcoded. The _BUDGET_CONCERN_NOTE instructs the model to USE those facts."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "price_inquiry"
    belief.open_question = "What does this cost monthly?"
    facts = [
        "Typical monthly spend: most families land around $300-$600/month depending on hours and plan.",
        "The most reliable ARPM anchor is ~$335 (Q1 2025) rising to ~$364-374 by late 2025 (VERIFIED).",
    ]
    dec = _decision("answer_via_kb")

    msgs = _build_messages(dec, belief, cfg, retrieved_facts=facts)
    user_content = msgs[-1]["content"]

    assert "$300" in user_content or "300-$600" in user_content or "$335" in user_content, (
        "KB pricing facts must appear in the NLG prompt so the model can ground its ballpark in them"
    )
    assert "PRICE QUESTION POLICY" in user_content, (
        "_BUDGET_CONCERN_NOTE must still be present alongside the KB facts"
    )


# ---------------------------------------------------------------------------
# CB-62 test 7: end-to-end respond() — decision act is answer_via_kb, not pure deflection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_respond_price_inquiry_yields_answer_act_not_pure_deflection():
    """Integration: respond() on a direct price_inquiry must yield a gated decision of answer_via_kb
    (not pitch / escalate / attempt_close), showing the policy path chooses to answer rather than
    deflect. The NLG script returns a clean, grounded reply; the grounding guard passes it through.
    Canned LLM replies + fake retrieval — no network.

    The clean reply is carefully crafted to avoid CLAIM_TOKENS ("free", "guarantee", etc.) and
    invented specifics, so the grounding guard does not regenerate it — letting the test assert the
    committed reply is the scripted one, not a fallback."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.open_question = "How much does this cost? We're on a tight budget."

    pricing_facts = [
        "Most families land around $300-$600/month depending on hours and plan (REPORTED).",
        "ARPM anchor: ~$335 per month (VERIFIED, Nerdy Q1 2025 financial results).",
        "The exact monthly price is confirmed in a no-obligation consultation (VERIFIED).",
    ]
    # Reply: grounded against facts, no CLAIM_TOKEN ("free", "guarantee", etc.), no invented number.
    # "$300 to $600" is supported by the first fact; "no-obligation consultation" is in facts.
    clean_reply = (
        "Budget matters — that's a fair place to start. Most families land around $300 to $600 "
        "per month depending on the plan and how many hours they need. The exact monthly number "
        "is built around your goals in a no-obligation consultation."
    )

    # Scripted: DST deltas, policy proposes answer_via_kb, NLG returns clean reply (no fabrication).
    llm = MockLLMClient([
        _flat_deltas(),
        _policy_json("answer_via_kb"),
        clean_reply,
    ])

    decision, reply, _new = await respond(
        belief, [], "How much does this cost? We're on a tight budget.",
        llm, cfg,
        retrieved_facts=pricing_facts,
        last_user_act="price_inquiry",
    )

    assert decision.act == "answer_via_kb", (
        f"A direct price_inquiry must result in answer_via_kb, got {decision.act!r}"
    )
    assert reply == clean_reply, (
        f"The grounded clean reply must be committed as-is; got: {reply!r}"
    )
    # The reply must not be a pure deflection with no pricing information.
    deflection_patterns = ["perfect —", "perfect!", "let me get you connected", "i don't have a rate"]
    for pat in deflection_patterns:
        assert pat not in reply.lower(), (
            f"Reply must not be a tone-deaf deflection on a tight-budget price ask; found {pat!r}"
        )

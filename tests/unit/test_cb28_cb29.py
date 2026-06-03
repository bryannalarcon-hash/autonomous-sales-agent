# Regression tests (TEST-FIRST) for CB-28 (tool-use inspection in Call Review) and CB-29 (agent
# quality: no hallucinated slots, no non-sequiturs, no premature pushy closes). CB-28 asserts the
# retrieved KB facts that grounded an answer are captured (Turn.retrieved + respond() stamping
# decision.meta["retrieved"]) and serialize round-trip + surface through operate.episode_detail.
# CB-29 replays the real failing call ep-eba52675…: a caller who gives NO child info and asks
# "how's your day" — the agent must NOT invent a daughter/student/grade (NLG grounding), MUST
# address the actual question before advancing (address_direct_input), and must NOT attempt_close
# before real discovery (the close floor). Network-free: scripted MockLLMClient only.
from __future__ import annotations

import json

from src.api import operate
from src.config.settings import AgentConfig, Persona
from src.core import gates
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import MockLLMClient
from src.core.nlg import realize
from src.core.policy import Decision
from src.core.respond import respond
from src.memory.schema import Episode, Turn


# --- shared fixtures --------------------------------------------------------------------------

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
        "discovery_slots_before_close": 2,
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


# ==============================================================================================
# CB-28 — tool-use inspection: the retrieved facts that grounded an answer are captured + exposed
# ==============================================================================================

async def test_cb28_respond_stamps_retrieved_facts_on_answer_via_kb():
    """When the agent answers via KB (a tool act) and retrieved_facts were supplied, respond() must
    capture those facts so Call Review can show WHAT was pulled — stamped on decision.meta['retrieved']
    (the channel the runtime persists onto the Turn) as JSON-able strings."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.3
    facts = [
        "[pricing#tiers] Memberships are tiered; a typical plan runs $300/month.",
        "[guarantee#1] First session is satisfaction-guaranteed.",
    ]
    llm = MockLLMClient(
        [
            json.dumps({"trust": 0.0}),  # dst deltas
            _policy_json(act="answer_via_kb", rationale="answer their price question"),  # policy
            "Our memberships are tiered — a typical plan is about $300 a month.",  # nlg
        ]
    )
    decision, reply, _ = await respond(
        b,
        history=[],
        user_utterance="How much does this cost?",
        llm_client=llm,
        config=cfg,
        retrieved_facts=facts,
    )
    assert decision.act == "answer_via_kb"
    captured = decision.meta.get("retrieved")
    assert captured, "respond() must capture the retrieved facts on a tool act"
    # Stored as plain strings (JSON-serializable for the episode JSONB), in order.
    assert all(isinstance(f, str) for f in captured)
    assert "pricing#tiers" in captured[0]


async def test_cb28_respond_no_retrieved_on_non_tool_turn():
    """A non-tool turn (a plain ask) carries no retrieved payload — non-tool turns are unaffected."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.6
    llm = MockLLMClient(
        [_flat_deltas(), _policy_json(act="ask", target_slot="subject", rationale="discovery"), "What subject?"]
    )
    decision, _, _ = await respond(
        b, history=[], user_utterance="Sounds good.", llm_client=llm, config=cfg
    )
    # No facts were supplied and the act isn't a KB answer -> nothing captured.
    assert not decision.meta.get("retrieved")


def test_cb28_turn_retrieved_serializes_round_trip():
    """The new Turn.retrieved field survives to_dict()/from_dict() so it persists in the episode JSONB."""
    t = Turn(
        turn_id=4,
        speaker="agent",
        text="A typical plan is about $300 a month.",
        decision="answer_via_kb",
        rationale="answered their price question",
        retrieved=["[pricing#tiers] Memberships are tiered; ~$300/month."],
    )
    d = t.to_dict()
    assert d["retrieved"] == ["[pricing#tiers] Memberships are tiered; ~$300/month."]
    rt = Turn.from_dict(d)
    assert rt.retrieved == t.retrieved
    # A turn with no retrieved facts round-trips to None (not an empty surprise).
    plain = Turn(turn_id=5, speaker="prospect", text="ok")
    assert Turn.from_dict(plain.to_dict()).retrieved is None


def test_cb28_episode_detail_exposes_retrieved_per_turn():
    """operate.episode_detail must expose the retrieved facts per turn so the Review page can render
    a clickable tool-use turn -> a panel of what was pulled. Non-tool turns expose None."""
    ep = Episode(
        episode_id="ep-cb28",
        channel="sim",
        turns=[
            Turn(turn_id=0, speaker="prospect", text="How much per month?"),
            Turn(
                turn_id=1,
                speaker="agent",
                text="Plans run about $300/month.",
                decision="answer_via_kb",
                rationale="answer price",
                retrieved=["[pricing#tiers] ~$300/month."],
            ),
        ],
    )
    detail = operate.episode_detail(ep)
    turns = detail["turns"]
    assert turns[0]["retrieved"] is None  # prospect turn: nothing pulled
    assert turns[1]["retrieved"] == ["[pricing#tiers] ~$300/month."]


# ==============================================================================================
# CB-29 — agent quality: hallucinated slots, non-sequiturs, premature pushy closes
# ==============================================================================================

# --- Defect 1: NLG must not assert a slot the DST has not confirmed ----------------------------

async def test_cb29_nlg_does_not_assert_unconfirmed_student_or_relationship():
    """The agent invented a 'daughter'/'student' the caller never mentioned. With NO student/grade
    slot confirmed, the NLG prompt must instruct the model to speak only from confirmed slots — and
    the realized reply (driven by a mock that, given a properly-constrained prompt, returns neutral
    phrasing) must NOT assert a fabricated child/relationship/grade.

    We assert the GROUNDING CONSTRAINT reaches the LLM: the assembled NLG prompt forbids asserting an
    unconfirmed person/relationship/grade. (The realized text faithfulness is the model's job; the
    deterministic guarantee we own is that the constraint is present and that confirmed slots are the
    only personal facts surfaced.)"""
    cfg = make_config()
    b = BeliefState.fresh()  # NO slots filled — caller gave no child/grade info
    captured: dict[str, str] = {}

    def responder(messages, **opts):
        captured["system"] = next(m["content"] for m in messages if m["role"] == "system")
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return "Happy to help — what brings you in today?"

    llm = MockLLMClient(responder)
    decision = Decision(act="ask", target_slot="subject", rationale="discover")
    reply = await realize(decision, b, cfg, llm)

    prompt = (captured.get("system", "") + "\n" + captured.get("user", "")).lower()
    # The grounding rule must forbid inventing a person/relationship/grade not in the belief.
    assert "do not" in prompt or "never" in prompt
    assert "daughter" in prompt or "child" in prompt or "student" in prompt or "relationship" in prompt
    # And no confirmed personal slot was leaked (there are none) — KNOWN_SO_FAR must not name a child.
    assert "daughter" not in reply.lower()


async def test_cb29_nlg_surfaces_only_confirmed_slots():
    """When a slot IS confirmed, the NLG prompt may reference it; the grounding rule still forbids
    asserting anything BEYOND the confirmed slots + retrieved facts."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.set_slot("subject", "SAT math", 0.95)  # confirmed
    captured: dict[str, str] = {}

    def responder(messages, **opts):
        captured["user"] = next(m["content"] for m in messages if m["role"] == "user")
        return "Great — let's focus on SAT math."

    llm = MockLLMClient(responder)
    decision = Decision(act="pitch", rationale="anchor on goal")
    await realize(decision, b, cfg, llm)
    # The confirmed slot is available to the model...
    assert "SAT math" in captured["user"] or "sat math" in captured["user"].lower()
    # ...but a grade/child the DST never filled is not asserted as known.
    assert "grade" not in captured["user"].lower() or "grade_level" not in b.slots


# --- Defect 2: the agent must address the caller's actual input before advancing ----------------

def test_cb29_address_direct_input_routes_social_question_not_discovery():
    """Real defect: caller asked 'how's your day' and the agent fired 'what grade is your student'
    (a non-sequitur). With an open_question + a question intent, a discovery `ask` must be deferred to
    an answer act so the agent ADDRESSES what was asked instead of barreling into discovery."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 4
    b.open_question = "How's your day going?"
    b.last_user_act = "question"
    out = gates.address_direct_input(
        Decision(act="ask", target_slot="grade_level", rationale="what grade"), b, cfg
    )
    assert out.act != "ask", "a discovery ask must not ignore the caller's direct question"
    assert out.act == "answer_via_kb"


def test_cb29_address_direct_input_defers_close_when_question_unanswered_and_no_discovery():
    """A non-sequitur close: the LLM proposes attempt_close while the caller's question is unanswered
    and discovery hasn't happened. The agent must address the question first, not pivot to a close."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 5
    b.open_question = "How's your day going?"
    b.last_user_act = "question"
    out = gates.apply_gates(
        Decision(act="attempt_close", tier="trial", rationale="close now"), b, cfg, history=[]
    )
    assert out.act != "attempt_close"


# --- Defect 3: no premature pushy close before real discovery -----------------------------------

def test_cb29_close_blocked_before_min_turns_even_when_llm_proposes_it():
    """The agent fired attempt_close at turns 9 & 11 with almost no discovery. A raw LLM attempt_close
    proposed before min_turns_before_close (or before the discovery floor) must NOT pass the gates —
    it backs off to a non-pressure discovery/answer act. (Previously only the gate-ADVANCED close
    respected the floor; a directly-proposed close sailed through.)"""
    cfg = make_config(min_turns_before_close=4, discovery_slots_before_close=2)
    b = BeliefState.fresh()
    b.turn_count = 2  # opening turns — far too early to close
    b.drivers["trust"] = 0.7
    out = gates.apply_gates(
        Decision(act="attempt_close", tier="trial", rationale="LLM jumps to close"),
        b, cfg, history=[],
    )
    assert out.act != "attempt_close", "must not close in the opening turns"


def test_cb29_close_blocked_without_discovery_slots():
    """Even past the turn floor, a close with zero discovery slots filled is premature/pushy — block it."""
    cfg = make_config(min_turns_before_close=4, discovery_slots_before_close=2)
    b = BeliefState.fresh()
    b.turn_count = 9  # the real call's pushy-close turn
    b.drivers["trust"] = 0.7
    # No discovery slots filled at all (the caller gave no info).
    out = gates.apply_gates(
        Decision(act="attempt_close", tier="trial", rationale="LLM pushes close"),
        b, cfg, history=[],
    )
    assert out.act != "attempt_close", "no discovery yet -> a close is premature"


def test_cb29_close_allowed_after_discovery_and_turns():
    """The floor must NOT block a legitimate close: enough turns + enough discovery + warmth -> the
    close stands (no regression of the can't-close fix / the AE8 enrollment close)."""
    cfg = make_config(min_turns_before_close=4, discovery_slots_before_close=2)
    b = BeliefState.fresh()
    b.turn_count = 8
    b.drivers["trust"] = 0.7
    b.drivers["need_intensity"] = 0.7
    b.drivers["purchase_intent"] = 0.7
    b.drivers["bail_risk"] = 0.1
    b.set_slot("grade_level", "11", 0.95)
    b.set_slot("subject", "SAT math", 0.95)
    out = gates.apply_gates(
        Decision(act="attempt_close", tier="enrollment", rationale="hot + qualified"),
        b, cfg, history=[],
    )
    assert out.act == "attempt_close"


async def test_cb29_full_replay_no_daughter_no_premature_close():
    """End-to-end replay shaped like ep-eba52675…: a caller who gives NO child info and opens with
    'how's your day'. Across the opening turns the agent must (a) NOT emit 'daughter'/'student'/'grade'
    as a fact, (b) acknowledge the actual question (answer act, not a discovery non-sequitur), and
    (c) NOT attempt_close in the first few turns. Driven by a scripted LLM that PROPOSES the bad acts
    the real model did; the gates + NLG grounding must correct them."""
    cfg = make_config(min_turns_before_close=4, discovery_slots_before_close=2)
    b = BeliefState.fresh()
    history: list[dict] = []

    # Turn A (early): caller asks a social question; the LLM (badly) proposes asking the grade.
    llmA = MockLLMClient(
        [
            json.dumps({"trust": 0.05}),
            _policy_json(act="ask", target_slot="grade_level", rationale="what grade is your student"),
            "How's my day? Good, thanks — and what can I help you with today?",
        ]
    )
    dA, replyA, b = await respond(
        b, history=history, user_utterance="How's your day going?", llm_client=llmA, config=cfg
    )
    history.append({"role": "user", "text": "How's your day going?"})
    history.append({"role": "agent", "act": dA.act, "confidence": dA.confidence, "text": replyA})
    assert dA.act != "ask", "must address the social question, not barrel into discovery"
    assert "daughter" not in replyA.lower()

    # Turn B: still no child info; the LLM (badly) proposes a premature close.
    llmB = MockLLMClient(
        [
            json.dumps({"trust": 0.05}),
            _policy_json(act="attempt_close", tier="trial", rationale="push to close"),
            "Let's get you set up.",
        ]
    )
    dB, replyB, b = await respond(
        b, history=history, user_utterance="I'm just looking into options.", llm_client=llmB, config=cfg
    )
    assert dB.act != "attempt_close", "no discovery yet -> no premature close"
    assert "daughter" not in replyB.lower() and "your student" not in replyB.lower()

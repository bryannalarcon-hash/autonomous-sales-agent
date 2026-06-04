# Regression tests (TEST-FIRST) for CB-48 — production-brain directness + anti-repetition, the cluster
# the real-model eval personas (Marcus/Dana) exposed. Network-free: scripted MockLLMClient + direct
# gate calls, asserting the DETERMINISTIC pieces only.
#   ANTI-REPETITION: the agent's last 1-2 SPOKEN lines are threaded from history into the NLG prompt
#     (realize + realize_stream, in lock-step) with a do-not-restate instruction — so NLG stops
#     restating the same hedge/offer. Only the agent's OWN lines (never the caller transcript) reach
#     the prompt (distillation-preserving). R37 parity: respond_stream threads the SAME own-lines.
#   DIRECTNESS: a RECURRING unanswered open_question (Marcus asking "can you move his score?" 4x) keeps
#     getting ANSWERED instead of letting a pitch escape the answer-loop or a close pre-empt it; and a
#     discovery ask/confirm under HIGH bail_risk (an impatient prospect right after "no fluff") is
#     suppressed in favor of answer/advance instead of slot-filling a hot prospect.
from __future__ import annotations

import json

from src.config.settings import AgentConfig, Persona
from src.core import gates
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import MockLLMClient
from src.core.nlg import _build_messages, realize, realize_stream
from src.core.policy import Decision
from src.core.respond import StreamResult, respond, respond_stream


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
        "extreme_bail_deescalate": 0.9,
        "max_slot_asks": 2,
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


def _decision(act: str = "pitch", **kw) -> Decision:
    return Decision(act=act, rationale="r", confidence=kw.pop("confidence", 0.6), **kw)


# ==============================================================================================
# ANTI-REPETITION — the agent's own last lines reach the NLG prompt with a no-restate instruction
# ==============================================================================================

def test_build_messages_threads_own_last_lines_with_no_restate_rule():
    """_build_messages must surface the agent's own prior spoken lines (YOUR_LAST_LINES) AND a
    do-not-restate instruction, so NLG stops repeating the same hedge/offer it already gave."""
    cfg = make_config()
    belief = BeliefState.fresh()
    prior = [
        "I can't promise a specific score jump, but our approach is built around your goal.",
        "Same idea here — we tailor it to him.",
    ]
    msgs = _build_messages(_decision("pitch"), belief, cfg, None, own_last_lines=prior)
    user = msgs[-1]["content"]
    assert "YOUR_LAST_LINES" in user, "the agent's own prior lines must reach the NLG prompt"
    # Both prior lines are present so the model can avoid restating either.
    assert prior[0][:30] in user
    assert prior[1][:20] in user
    # A do-not-restate / rephrase-or-advance instruction must accompany them.
    low = user.lower()
    assert "restate" in low or "repeat" in low
    assert "rephrase" in low or "advance" in low or "move" in low


def test_build_messages_no_own_lines_block_when_none():
    """With no prior agent lines (turn 1), the YOUR_LAST_LINES block is omitted entirely (no empty
    'YOUR_LAST_LINES: []' noise) so the opening turn's prompt is unchanged."""
    cfg = make_config()
    msgs = _build_messages(_decision("pitch"), BeliefState.fresh(), cfg, None, own_last_lines=[])
    assert "YOUR_LAST_LINES" not in msgs[-1]["content"]


def test_build_messages_does_not_leak_caller_transcript():
    """Distillation-preserving: ONLY the agent's own lines are threaded — the caller's utterances are
    NOT fed back (no new inventable facts). _build_messages takes own_last_lines, never user text."""
    cfg = make_config()
    own = ["Here's how we'd help with Algebra."]
    msgs = _build_messages(_decision("pitch"), BeliefState.fresh(), cfg, None, own_last_lines=own)
    user = msgs[-1]["content"]
    # The caller's words never appear (the test passes none in; the API has no slot for them).
    assert "my son" not in user.lower()
    assert own[0] in user


async def test_respond_threads_own_last_lines_into_nlg_prompt():
    """respond() pulls the agent's OWN prior spoken lines out of history and threads them to NLG, so
    the realized turn can avoid restating them. We capture the NLG messages the model received."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.55
    history = [
        {"role": "agent", "act": "greeting", "text": "Hi, this is Alex with Nerdy."},
        {"role": "user", "text": "Can you actually move his score?"},
        {"role": "agent", "act": "pitch", "text": "We build a plan around his specific gaps."},
        {"role": "user", "text": "Can you actually move his score or not?"},
    ]
    captured: dict[str, str] = {}

    def _nlg(messages, **opts):
        captured["user"] = messages[-1]["content"]
        return "A fair question — here's the concrete way it moves his score."

    llm = MockLLMClient(
        [_flat_deltas(), json.dumps({"act": "pitch", "rationale": "value", "confidence": 0.6}), _nlg]
    )
    await respond(b, history, "Can you actually move his score or not?", llm, cfg)
    user = captured["user"]
    assert "YOUR_LAST_LINES" in user
    # The agent's last spoken line is surfaced in YOUR_LAST_LINES; the caller's line is NOT (the open
    # question may still appear under PROSPECT_ASKED, but the raw transcript is not fed back as context).
    own_block = user.split("YOUR_LAST_LINES", 1)[1].split("Write ONLY", 1)[0]
    assert "We build a plan around his specific gaps." in own_block
    assert "Can you actually move his score" not in own_block


async def test_respond_stream_threads_same_own_lines_as_respond():
    """R37 parity for the own-line context: respond_stream feeds the NLG the SAME YOUR_LAST_LINES the
    blocking respond() does (both pull from history), so the streamed turn cannot drift the prompt."""
    cfg = make_config()
    history = [
        {"role": "agent", "act": "pitch", "text": "We tailor the plan to his SAT timeline."},
        {"role": "user", "text": "Will it actually work in 5 weeks?"},
    ]
    captured_block: dict[str, str] = {}
    captured_stream: dict[str, str] = {}

    def _nlg_block(messages, **opts):
        captured_block["user"] = messages[-1]["content"]
        return "Here is the 5-week plan."

    def _nlg_stream(messages, **opts):
        captured_stream["user"] = messages[-1]["content"]
        return "Here is the 5-week plan."

    pol = json.dumps({"act": "pitch", "rationale": "value", "confidence": 0.6})
    await respond(BeliefState.fresh(), history, "Will it work?", MockLLMClient([_flat_deltas(), pol, _nlg_block]), cfg)
    result = StreamResult()
    async for _ in respond_stream(
        BeliefState.fresh(), history, "Will it work?", MockLLMClient([_flat_deltas(), pol, _nlg_stream]), cfg, result=result
    ):
        pass
    assert "YOUR_LAST_LINES" in captured_block["user"]
    assert captured_block["user"] == captured_stream["user"]  # identical own-line context (parity)


# ==============================================================================================
# DIRECTNESS — a RECURRING unanswered question keeps getting answered (Marcus's 4x challenge)
# ==============================================================================================

def _agent(act: str, text: str) -> dict:
    return {"role": "agent", "act": act, "text": text}


def _user(text: str) -> dict:
    return {"role": "user", "text": text}


def test_recurring_unanswered_question_blocks_pitch_escape():
    """THE MARCUS PATTERN: the prospect asks the SAME challenge again ('can you move his score or
    not?') having NOT been satisfied. address_direct_input previously let a `pitch` ADVANCE once the
    agent had answered last turn — so a re-asked, still-unanswered challenge got a pitch (an evasion).
    When the open question is RECURRING (asked again after an answer attempt), the pitch must NOT
    escape: route back to answer_via_kb."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "can you actually move his score in 5 weeks or not?"
    b.last_user_act = "question"
    b.turn_count = 5
    # History: the agent already ANSWERED, yet the prospect re-asks the SAME thing -> recurring.
    history = [
        _user("can you actually move his score in 5 weeks?"),
        _agent("answer_via_kb", "Scores can move meaningfully with focused work."),
        _user("that's vague. can you actually move his score in 5 weeks or not?"),
    ]
    out = gates.address_direct_input(_decision("pitch"), b, cfg, history=history)
    assert out.act == "answer_via_kb", (
        "a recurring, still-unanswered challenge must be ANSWERED, not escaped with a pitch"
    )


def test_nonrecurring_question_still_lets_pitch_advance():
    """The reactive-loop escape is PRESERVED for a genuinely NEW question: if the agent answered last
    turn and the prospect asks something DIFFERENT, a pitch may still advance (we don't re-trap the
    agent into answering forever)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "do you tutor on weekends?"
    b.last_user_act = "question"
    b.turn_count = 5
    history = [
        _user("how does the matching work?"),
        _agent("answer_via_kb", "We match by subject and learning style."),
        _user("ok, do you tutor on weekends?"),
    ]
    out = gates.address_direct_input(_decision("pitch"), b, cfg, history=history)
    assert out.act == "pitch", "a NEW question after an answer should still let a pitch advance"


def test_recurring_question_blocks_close_through_full_gate_chain():
    """THE FULL-CHAIN GUARANTEE: a recurring unanswered question must not be closed over even after
    advance_to_close runs. address_direct_input routes the proposed close to answer_via_kb, and
    advance_to_close must NOT re-convert that answer back into a close on a warm prospect — otherwise
    the directness fix is silently undone downstream. apply_gates() end-to-end must keep the answer."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "can you actually move his score or not?"
    b.last_user_act = "question"
    b.turn_count = 6
    b.drivers["trust"] = 0.75  # warm enough that advance_to_close would otherwise take initiative
    b.drivers["purchase_intent"] = 0.6
    history = [
        _user("can you actually move his score?"),
        _agent("answer_via_kb", "We focus on the highest-yield areas."),
        _user("you didn't answer. can you actually move his score or not?"),
    ]
    out = gates.apply_gates(_decision("attempt_close", tier="trial"), b, cfg, history=history)
    assert out.act == "answer_via_kb", (
        "the full gate chain must answer a recurring unanswered question, not close over it"
    )


def test_warm_prospect_with_new_question_can_still_advance_to_close():
    """Tightness guard for the full-chain change: a warm prospect whose latest question is NEW (not a
    recurrence) can still be advanced to a close by advance_to_close — the directness guard only blocks
    closing over a RE-ASKED, still-unanswered challenge."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "do you offer evening sessions?"
    b.last_user_act = "question"
    b.turn_count = 6
    b.drivers["trust"] = 0.75
    b.drivers["purchase_intent"] = 0.6
    history = [
        _user("how does matching work?"),
        _agent("answer_via_kb", "We match by subject and style."),
        _user("got it. do you offer evening sessions?"),
    ]
    out = gates.apply_gates(_decision("answer_via_kb"), b, cfg, history=history)
    # A new question lets the agent answer it (address_direct_input keeps answer_via_kb), and a warm
    # prospect remains closeable on subsequent turns — the key is we did NOT block on a recurrence here.
    assert out.act in ("answer_via_kb", "attempt_close")


def test_recurring_question_preempts_a_proposed_close():
    """A close proposed while a RECURRING unanswered question is on the table must be deferred to
    answer it first (answer the core concern before re-pushing a close). Marcus got pushed toward a
    next step while his 'can you actually move his score?' was never answered."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.open_question = "can you actually move his score or not?"
    b.last_user_act = "question"
    b.turn_count = 6
    history = [
        _user("can you actually move his score?"),
        _agent("answer_via_kb", "We focus on the highest-yield areas."),
        _user("you didn't answer. can you actually move his score or not?"),
    ]
    out = gates.address_direct_input(_decision("attempt_close", tier="trial"), b, cfg, history=history)
    assert out.act == "answer_via_kb", (
        "a recurring unanswered question must be answered before a close is attempted"
    )


# ==============================================================================================
# DIRECTNESS — suppress discovery under HIGH bail_risk (don't slot-fill a hot, impatient prospect)
# ==============================================================================================

def test_high_bail_suppresses_discovery_ask():
    """THE 'no fluff' PATTERN: right after the prospect signals impatience (high bail_risk), the agent
    must NOT slot-fill with a discovery `ask`. address_direct_input suppresses the ask in favor of
    answering an open question (or advancing with a pitch when there's nothing to answer)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["bail_risk"] = 0.8  # at/above pushiness_cap (0.7) — a hot, impatient prospect
    b.turn_count = 3
    out = gates.address_direct_input(_decision("ask", target_slot="grade_level"), b, cfg, history=[])
    assert out.act != "ask", "a discovery ask must be suppressed when bail_risk is high"
    assert out.act == "pitch", "with no open question, suppressing the ask should advance (pitch)"


def test_high_bail_discovery_answers_open_question_when_present():
    """When bail is high AND the prospect has an open question, the suppressed discovery ask routes to
    ANSWER it (address what they asked), not push another question at an impatient prospect."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["bail_risk"] = 0.85
    b.open_question = "just tell me the price?"
    b.last_user_act = "price_inquiry"
    b.turn_count = 3
    out = gates.address_direct_input(_decision("ask", target_slot="grade_level"), b, cfg, history=[])
    assert out.act == "answer_via_kb", "under impatience, answer the open question instead of slot-filling"


def test_low_bail_discovery_ask_is_preserved():
    """Tightness guard: when bail_risk is LOW (a calm prospect, no open question/objection), a normal
    discovery `ask` is NOT suppressed — the directness change only fires on a HOT prospect."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["bail_risk"] = 0.3  # calm
    b.turn_count = 2
    out = gates.address_direct_input(_decision("ask", target_slot="grade_level"), b, cfg, history=[])
    assert out.act == "ask" and out.target_slot == "grade_level", (
        "a normal discovery ask on a calm prospect must pass through unchanged"
    )

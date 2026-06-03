# Regression tests (TEST-FIRST) for CB-34/35/36/37 — four coupled brain behavioral fixes seen on the
# live call ep-2b8ad5bc. Network-free: scripted MockLLMClient only, asserting the DETERMINISTIC pieces
# (DST extraction/detection, the gates, respond() bookkeeping, and the NLG prompt-rule presence).
#   CB-35: DST extracts grade from a bare ordinal/number answer; skip_known + no_repeat_discovery never
#          re-ask a filled/declined/over-asked slot.
#   CB-34: DST tracks learner_established/learner_denied; establish_who_first + the NLG no-presumption
#          rule keep the agent from presuming a learner exists before a who-is-this-for step.
#   CB-36: DST flags an emotional disclosure (-> active_objection='concern'); the gates acknowledge it
#          before any pitch; the NLG no-invented-context rule reaches the prompt.
#   CB-37: DST flags hostility; pushiness_cap blocks a pitch over the bail cap; de_escalate hands off at
#          extreme bail / hostility instead of pitching or re-asking.
from __future__ import annotations

import json

from src.config.settings import AgentConfig, Persona
from src.core import dst, gates
from src.core.belief_state import DRIVERS, BeliefState
from src.core.dst import update
from src.core.llm import MockLLMClient
from src.core.nlg import realize
from src.core.policy import Decision
from src.core.respond import respond


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


def _policy_json(**kwargs) -> str:
    payload = {"act": kwargs.get("act", "pitch"), "rationale": kwargs.get("rationale", "")}
    for k in ("target_slot", "tier", "confidence", "concession"):
        if k in kwargs:
            payload[k] = kwargs[k]
    return json.dumps(payload)


# ==============================================================================================
# CB-35 — never re-ask an answered / declined / over-asked discovery question
# ==============================================================================================

# --- DST: extract grade from a bare ordinal/number answer --------------------------------------

def test_cb35_extract_grade_bare_ordinal_word():
    """A bare spelled ordinal is a grade ONLY as the answer to a just-asked grade question; an explicit
    'third grade' / a grade word is always trusted; a bare 'Third' out of context is NOT a grade."""
    assert dst._extract_grade("Third", asked_grade=True) == ("3", 0.85)
    assert dst._extract_grade("he is in fifth", asked_grade=True) == ("5", 0.85)
    assert dst._extract_grade("third grade") == ("3", 0.9)   # explicit ordinal+grade: always
    assert dst._extract_grade("kindergarten") == ("K", 0.9)  # grade word: always
    assert dst._extract_grade("Third") is None               # bare + not the answer turn -> not a grade


def test_cb35_extract_grade_bare_numeric_ordinal():
    """A bare numeric ordinal fills the grade only as the just-asked answer: '3rd' -> 3, 'he is in 5th' -> 5."""
    assert dst._extract_grade("3rd", asked_grade=True) == ("3", 0.8)
    assert dst._extract_grade("he is in 5th", asked_grade=True) == ("5", 0.8)
    assert dst._extract_grade("3rd") is None  # bare numeric ordinal + not asked -> not a grade


def test_cb35_extract_grade_bare_number_only_when_asked():
    """A bare standalone number is taken as the grade ONLY right after the grade was asked, and only
    within the sane 1-12 bound (an age like 16 is rejected)."""
    assert dst._extract_grade("8", asked_grade=True) == ("8", 0.75)
    assert dst._extract_grade("8", asked_grade=False) is None  # a stray number is not a grade
    assert dst._extract_grade("16", asked_grade=True) is None  # out of the 1-12 grade range


def test_cb35_extract_grade_no_false_positive_on_non_answer_ordinals():
    """CB-35 follow-up: a bare ordinal/number in a NON-answer context must not masquerade as (or LOCK) a
    grade; and even on the answer turn an explicit age phrase is an age, not a grade."""
    # stray ordinals/counts/dates when grade was NOT just asked -> no grade
    assert dst._extract_grade("1st time calling") is None
    assert dst._extract_grade("on the 3rd of June") is None
    assert dst._extract_grade("my 2nd child needs help") is None
    assert dst._extract_grade("she is 7 years old") is None
    # even on the answer turn, an explicit age phrase is rejected (age 7 != grade 7)
    assert dst._extract_grade("she is 7 years old", asked_grade=True) is None
    assert dst._extract_grade("he's 9 years old", asked_grade=True) is None
    # but a genuine ordinal answer on the answer turn still works
    assert dst._extract_grade("she's in 4th", asked_grade=True) == ("4", 0.8)


async def test_cb35_update_fills_grade_from_bare_third_after_ask():
    """End-to-end DST: with the agent's last act an `ask` whose pending slot is grade_level, the bare
    answer 'Third' fills grade_level at lock confidence (so it never gets re-asked)."""
    flat = MockLLMClient([_flat_deltas()])
    b = BeliefState.fresh()
    b.meta["pending_ask_slot"] = "grade_level"
    new = await update(b, "ask", "Third.", flat)
    assert new.slot_value("grade_level") == "3"
    assert new.slot_confidence("grade_level") >= 0.8


# --- gates: skip_known suppresses re-asking a filled slot --------------------------------------

def test_cb35_skip_known_suppresses_reask_of_filled_grade():
    """A filled grade slot must not be re-asked: skip_known overrides an `ask` to confirm_known."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.set_slot("grade_level", "3", 0.95)
    out = gates.skip_known(Decision(act="ask", target_slot="grade_level", rationale="grade?"), b, cfg)
    assert out.act == "confirm_known"


# --- gates: no_repeat_discovery hard-drops a declined / over-asked slot -------------------------

def test_cb35_no_repeat_drops_declined_slot():
    """A slot the prospect DECLINED (recorded in meta) is never asked again — the gate drops it."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.meta["declined_slots"] = ["grade_level"]
    out = gates.no_repeat_discovery(
        Decision(act="ask", target_slot="grade_level", rationale="grade?"), b, cfg
    )
    assert out.act != "ask", "a declined slot must never be re-asked"
    assert out.target_slot != "grade_level"


def test_cb35_no_repeat_drops_over_asked_slot():
    """A slot asked >= max_slot_asks times is dropped (never asked a 3rd time)."""
    cfg = make_config(max_slot_asks=2)
    b = BeliefState.fresh()
    b.meta["asked_slots"] = {"grade_level": 2}
    out = gates.no_repeat_discovery(
        Decision(act="ask", target_slot="grade_level", rationale="grade?"), b, cfg
    )
    assert out.act != "ask"


def test_cb35_no_repeat_allows_first_asks():
    """The first asks (below the cap) are allowed through — the guard only kills repeats."""
    cfg = make_config(max_slot_asks=2)
    b = BeliefState.fresh()
    b.meta["asked_slots"] = {"grade_level": 1}
    out = gates.no_repeat_discovery(
        Decision(act="ask", target_slot="grade_level", rationale="grade?"), b, cfg
    )
    assert out.act == "ask" and out.target_slot == "grade_level"


async def test_cb35_respond_counts_asked_slots():
    """respond() records each asked slot on belief.meta so the no-repeat guard can count re-asks."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.drivers["trust"] = 0.3
    b.meta["learner_established"] = True  # so establish_who_first doesn't redirect the ask
    llm = MockLLMClient(
        [_flat_deltas(), _policy_json(act="ask", target_slot="subject", rationale="discover"), "What subject?"]
    )
    _, _, nb = await respond(b, history=[], user_utterance="ok", llm_client=llm, config=cfg)
    assert nb.meta.get("asked_slots", {}).get("subject") == 1
    assert nb.meta.get("pending_ask_slot") == "subject"


async def test_cb35_replay_answered_grade_never_reasked():
    """ACCEPTANCE: caller answers 'Third' once -> the agent NEVER asks grade again. After the answer is
    extracted, a proposed grade ask is suppressed by skip_known/no_repeat across subsequent turns."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.meta["learner_established"] = True
    # The agent asked grade last turn (both the history act AND the pending-slot marker the DST keys on),
    # so the bare answer 'Third' is recognized as the grade answer (asked_grade=True).
    history: list[dict] = [
        {"role": "agent", "act": "ask", "target_slot": "grade_level", "text": "What grade is the learner in?"}
    ]
    b.meta["pending_ask_slot"] = "grade_level"
    llm1 = MockLLMClient(
        [_flat_deltas(), _policy_json(act="ask", target_slot="grade_level", rationale="grade again"), "What grade?"]
    )
    d1, _, b = await respond(b, history=history, user_utterance="Third.", llm_client=llm1, config=cfg)
    history.append({"role": "user", "text": "Third."})
    history.append({"role": "agent", "act": d1.act, "text": "x"})
    assert b.slot_value("grade_level") == "3"
    assert d1.act != "ask" or d1.target_slot != "grade_level", "grade was answered — don't re-ask it"

    # Next turn the LLM (badly) proposes asking grade AGAIN; the gates must not let it through.
    llm2 = MockLLMClient(
        [_flat_deltas(), _policy_json(act="ask", target_slot="grade_level", rationale="re-ask grade"), "What grade?"]
    )
    d2, _, b = await respond(
        b, history=history, user_utterance="I told you, third.", llm_client=llm2, config=cfg
    )
    assert not (d2.act == "ask" and d2.target_slot == "grade_level"), "must never re-ask an answered grade"


async def test_cb35_declining_twice_drops_slot():
    """ACCEPTANCE: declining a slot drops it. After the prospect declines the pending grade question,
    the slot is recorded declined and a later proposed grade ask is dropped."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.meta["learner_established"] = True
    b.meta["pending_ask_slot"] = "grade_level"
    flat = MockLLMClient([_flat_deltas()])
    nb = await update(b, "ask", "I'd rather not say.", flat)
    assert "grade_level" in nb.meta.get("declined_slots", []), "a declined pending slot is recorded"
    # A later grade ask is dropped by the gate.
    out = gates.no_repeat_discovery(
        Decision(act="ask", target_slot="grade_level", rationale="grade?"), nb, cfg
    )
    assert out.act != "ask"


# ==============================================================================================
# CB-34 — don't presume a learner exists before discovery establishes one
# ==============================================================================================

async def test_cb34_dst_flags_learner_denial():
    """The DST flags an explicit learner denial so the gates stop presuming a learner exists."""
    flat = MockLLMClient([_flat_deltas()])
    b = BeliefState.fresh()
    nb = await update(b, "ask", "I did not say I had a learner.", flat)
    assert nb.meta.get("learner_denied") is True
    assert nb.meta.get("learner_established") is False


async def test_cb34_dst_marks_learner_established_on_grade():
    """Once a learner-specific slot is filled, the learner is established (so who-first stops gating)."""
    flat = MockLLMClient([_flat_deltas()])
    b = BeliefState.fresh()
    b.meta["pending_ask_slot"] = "grade_level"
    nb = await update(b, "ask", "She's in 11th grade.", flat)
    assert nb.meta.get("learner_established") is True


async def test_cb34_dst_establishes_learner_when_caller_says_who():
    """CB-34 follow-up: the caller ANSWERING who-is-this-for (themselves or a specific child) establishes
    a learner — this must NOT be read as a denial, and it overrides a prior denial so discovery unblocks."""
    flat = MockLLMClient([_flat_deltas()])
    # "it's for me" — the caller IS the learner; who is now known.
    nb = await update(BeliefState.fresh(), "ask", "Actually it's for me.", flat)
    assert nb.meta.get("learner_established") is True
    assert nb.meta.get("learner_denied") is False
    # "it's for my son" — a specific child; matched nothing before this fix.
    nb2 = await update(BeliefState.fresh(), "ask", "It's for my son.", flat)
    assert nb2.meta.get("learner_established") is True
    # answering who-for overrides an EARLIER denial within the call (carried in meta).
    denied = BeliefState.fresh()
    denied.meta["learner_denied"] = True
    denied.meta["learner_established"] = False
    nb3 = await update(denied, "ask", "My daughter needs help with algebra.", flat)
    assert nb3.meta.get("learner_established") is True
    assert nb3.meta.get("learner_denied") is False


def test_cb34_establish_who_first_redirects_learner_ask():
    """A learner-specific ask with NO learner established is redirected to a who-is-this-for step."""
    cfg = make_config()
    b = BeliefState.fresh()  # nothing established
    out = gates.establish_who_first(
        Decision(act="ask", target_slot="grade_level", rationale="what grade is the learner in"), b, cfg
    )
    assert not (out.act == "ask" and out.target_slot == "grade_level")
    # It asks who the tutoring is for instead.
    assert out.act == "ask" and out.target_slot == "who_for"


def test_cb34_establish_who_first_allows_ask_once_established():
    """Once the learner is established, learner-specific discovery proceeds normally."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.meta["learner_established"] = True
    out = gates.establish_who_first(
        Decision(act="ask", target_slot="grade_level", rationale="grade?"), b, cfg
    )
    assert out.act == "ask" and out.target_slot == "grade_level"


def test_cb34_establish_who_first_denied_does_not_reask_learner():
    """If the caller DENIED a learner, the agent does not keep asking learner questions."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.meta["learner_denied"] = True
    out = gates.establish_who_first(
        Decision(act="ask", target_slot="grade_level", rationale="grade?"), b, cfg
    )
    assert out.act != "ask" or out.target_slot != "grade_level"


def test_cb34_establish_who_first_gates_pitch_after_denial():
    """CB-34 follow-up: a PITCH/close right after the caller DENIED a learner is redirected to who_for —
    you can't pitch tutoring for a learner the caller just said doesn't exist (the real call did exactly
    this and produced a context-less broken reply). An open question is answered first; once who_for is
    asked the cap times it gives up gracefully; a warm pitch with NO denial is left to close_floor."""
    cfg = make_config()
    denied = BeliefState.fresh()
    denied.meta["learner_denied"] = True
    out = gates.establish_who_first(Decision(act="pitch", tier="core", rationale="pitch value"), denied, cfg)
    assert out.act == "ask" and out.target_slot == "who_for", "pitch after a denial -> establish who first"

    b2 = BeliefState.fresh()
    b2.meta["learner_denied"] = True
    b2.open_question = "how much does it cost?"
    out2 = gates.establish_who_first(Decision(act="pitch", rationale="pitch"), b2, cfg)
    assert out2.act == "answer_via_kb", "an open question is answered before any who-push or pitch"

    b3 = BeliefState.fresh()
    b3.meta["learner_denied"] = True
    b3.meta["asked_slots"] = {"who_for": 2}  # already asked the cap number of times
    out3 = gates.establish_who_first(Decision(act="pitch", tier="core", rationale="pitch"), b3, cfg)
    assert out3.act == "pitch", "after asking who the cap times, stop redirecting (no who_for loop)"

    # A warm pitch with NO denial is NOT blocked here (close_floor/advance_to_close govern it).
    out4 = gates.establish_who_first(Decision(act="pitch", tier="core", rationale="pitch"),
                                     BeliefState.fresh(), cfg)
    assert out4.act == "pitch", "a warm pitch with no learner-denial passes (not this gate's job)"


async def test_cb34_acceptance_services_question_no_presumed_learner():
    """ACCEPTANCE: caller only asks 'what services do you offer?' -> the agent does NOT ask a presumed-
    learner question before establishing who it's for. The LLM (badly) proposes asking the grade; the
    gates redirect it away from a learner-specific ask."""
    cfg = make_config()
    b = BeliefState.fresh()
    llm = MockLLMClient(
        [
            json.dumps({"trust": 0.0, "user_act": "question", "open_question": "what services do you offer?"}),
            _policy_json(act="ask", target_slot="grade_level", rationale="what grade is the learner in"),
            "We offer 1:1 online tutoring across subjects.",
        ]
    )
    d, _, _ = await respond(
        b, history=[], user_utterance="What services do you offer?", llm_client=llm, config=cfg
    )
    assert not (d.act == "ask" and d.target_slot == "grade_level"), "must not presume a learner"


async def test_cb34_nlg_prompt_forbids_presuming_a_learner_exists():
    """The NLG grounding rule reaching the model must forbid presuming a learner even EXISTS (CB-34)."""
    cfg = make_config()
    b = BeliefState.fresh()
    captured: dict[str, str] = {}

    def responder(messages, **opts):
        captured["system"] = next(m["content"] for m in messages if m["role"] == "system")
        return "Happy to help — who would the tutoring be for?"

    llm = MockLLMClient(responder)
    await realize(Decision(act="ask", target_slot="who_for", rationale="who"), b, cfg, llm)
    sys = captured["system"].lower()
    assert "presume a learner" in sys
    assert "who the tutoring is for" in sys


# ==============================================================================================
# CB-36 — respond only to actual input; acknowledge a disclosure before any pitch
# ==============================================================================================

async def test_cb36_dst_flags_disclosure_as_active_concern():
    """A mistreatment/bad-experience disclosure sets a live concern the policy must address first."""
    flat = MockLLMClient([_flat_deltas()])
    b = BeliefState.fresh()
    nb = await update(b, "pitch", "My last tutor treated me really badly.", flat)
    assert nb.meta.get("disclosure") is True
    assert nb.active_objection == "concern"


async def test_cb36_disclosure_overrides_llm_objection_to_concern():
    """ACCEPTANCE (real-call regression): a mistreatment/bad-experience disclosure surfaces as 'concern'
    (acknowledge first) even when the LLM labeled it a sales objection to REBUT — on the live call
    'the last tutor wasted our money' was mislabeled 'efficacy_doubt' and the agent pitched instead of
    acknowledging."""
    intent = {**{d: 0.0 for d in DRIVERS}, "user_act": "objection", "objection": "efficacy_doubt",
              "open_question": None, "affordability_refusal": False}
    llm = MockLLMClient([json.dumps(intent)])
    nb = await update(BeliefState.fresh(), "pitch",
                      "The last tutor treated us terribly and wasted our money.", llm)
    assert nb.meta.get("disclosure") is True
    assert nb.active_objection == "concern", "a disclosure overrides an LLM objection mislabel to 'concern'"


def test_cb36_ground_objection_drops_unsupported_label():
    """_ground_objection keeps a canonical objection ONLY with a lexical cue; drops a hallucinated one;
    passes non-canonical/None through. Guards the real-call failure: 'timing' labeled on a non-timing turn."""
    assert dst._ground_objection("timing", "I didn't say I had a learner.") is None
    assert dst._ground_objection("timing", "the timing is bad, I'm too busy right now") == "timing"
    assert dst._ground_objection("price", "that's way too expensive for us") == "price"
    assert dst._ground_objection("price", "tell me about your tutors") is None
    assert dst._ground_objection("efficacy_doubt", "does this actually work?") == "efficacy_doubt"
    assert dst._ground_objection("concern", "anything") == "concern"  # non-canonical: pass through
    assert dst._ground_objection(None, "anything") is None


async def test_cb36_dst_grounds_hallucinated_llm_objection():
    """ACCEPTANCE (real-call regression): on a learner WHO-statement the LLM intent classifier mislabels
    'I didn't say I had a learner' as a 'timing' objection (a hallucination — no timing cue). The DST must
    DROP it (active_objection=None, coarse act downgraded) so no invented scheduling context propagates —
    while a genuine objection CO-STATED with the who-answer (and grounded on its own cue) is KEPT. A plain
    objection turn (not a who-statement) is left entirely to the LLM (paraphrase robustness preserved)."""
    timing = {**{d: 0.0 for d in DRIVERS}, "user_act": "objection", "objection": "timing",
              "open_question": None, "affordability_refusal": False}
    # learner-denial turn + ungrounded 'timing' -> dropped.
    nb = await update(BeliefState.fresh(), "ask", "I didn't say I had a learner.", MockLLMClient([json.dumps(timing)]))
    assert nb.active_objection is None, "an ungrounded 'timing' on a who-statement must be grounded away"
    assert nb.last_user_act != "objection", "the coarse act downgrades when the phantom objection is dropped"

    # learner-establish turn + a GENUINE price objection (grounded on 'expensive') -> kept.
    price = {**{d: 0.0 for d in DRIVERS}, "user_act": "objection", "objection": "price",
             "open_question": None, "affordability_refusal": False}
    nb2 = await update(BeliefState.fresh(), "ask", "It's for my son, but honestly it's too expensive.",
                       MockLLMClient([json.dumps(price)]))
    assert nb2.active_objection == "price", "a grounded objection co-stated with the who-answer is kept"
    assert nb2.meta.get("learner_established") is True


def test_cb36_pitch_deferred_when_disclosure_concern_live():
    """A proposed pitch right after a disclosure is routed to handle_objection (acknowledge, not pitch)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 3
    b.active_objection = "concern"  # a live disclosure surfaced by the DST
    out = gates.address_direct_input(Decision(act="pitch", rationale="pitch value"), b, cfg)
    assert out.act == "handle_objection", "acknowledge the disclosure before any pitch"


async def test_cb36_acceptance_mistreatment_acknowledged_not_pitched():
    """ACCEPTANCE: caller raises a mistreatment concern -> the agent acknowledges it (not a pitch). The
    LLM (badly) proposes a pitch; the gates route to handle_objection."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 3
    b.drivers["trust"] = 0.4
    llm = MockLLMClient(
        [
            json.dumps({"trust": -0.1, "user_act": "statement"}),
            _policy_json(act="pitch", rationale="pitch our value"),
            "I'm really sorry you went through that.",
        ]
    )
    d, _, _ = await respond(
        b, history=[], user_utterance="My previous tutoring company treated us terribly.",
        llm_client=llm, config=cfg,
    )
    assert d.act != "pitch", "a disclosure must be acknowledged, not met with a pitch"
    assert d.act == "handle_objection"


async def test_cb36_nlg_prompt_forbids_invented_context():
    """The NLG no-invented-context rule must reach the model: forbid topics the prospect did not raise
    and require acknowledging a disclosure before a pitch (CB-36)."""
    cfg = make_config()
    b = BeliefState.fresh()
    captured: dict[str, str] = {}

    def responder(messages, **opts):
        captured["system"] = next(m["content"] for m in messages if m["role"] == "system")
        return "I hear you."

    llm = MockLLMClient(responder)
    await realize(Decision(act="handle_objection", rationale="ack"), b, cfg, llm)
    sys = captured["system"].lower()
    assert "only to what the prospect actually said" in sys
    assert "disclos" in sys  # must mention acknowledging a disclosure
    assert "timing" in sys  # the named example of an invented topic


# ==============================================================================================
# CB-37 — enforce the pushiness cap; de-escalate on extreme bail / hostility
# ==============================================================================================

async def test_cb37_dst_flags_hostility():
    """A profane/abusive turn sets a sticky hostility flag on the belief (de-escalation signal)."""
    flat = MockLLMClient([_flat_deltas()])
    b = BeliefState.fresh()
    nb = await update(b, "pitch", "Fuck you.", flat)
    assert nb.meta.get("hostility") is True


def test_cb37_pushiness_cap_blocks_pitch_over_bail_cap():
    """ENFORCE pushiness_cap: when bail_risk exceeds the cap, a pitch is backed off to a non-pressure act."""
    cfg = make_config(pushiness_cap=0.7)
    b = BeliefState.fresh()
    b.drivers["bail_risk"] = 0.77  # over the cap, like the live call's t15
    out = gates.pushiness_cap(Decision(act="pitch", rationale="keep pitching"), b, cfg, history=[])
    assert out.act not in ("pitch", "attempt_close"), "no pitch/close once bail is over the cap"


def test_cb37_pushiness_cap_blocks_close_over_bail_cap():
    """The cap blocks an attempt_close too (not just a pitch) once bail crosses it."""
    cfg = make_config(pushiness_cap=0.7)
    b = BeliefState.fresh()
    b.drivers["bail_risk"] = 0.8
    out = gates.pushiness_cap(
        Decision(act="attempt_close", tier="trial", rationale="close"), b, cfg, history=[]
    )
    assert out.act != "attempt_close"


def test_cb37_de_escalate_on_extreme_bail():
    """At EXTREME bail (>= extreme_bail_deescalate), a pitch/close/re-ask is overridden to a graceful
    hand-off (escalate) — not merely backed off to a low-pressure act."""
    cfg = make_config(extreme_bail_deescalate=0.9)
    b = BeliefState.fresh()
    b.drivers["bail_risk"] = 0.95
    out = gates.de_escalate(Decision(act="pitch", rationale="pitch"), b, cfg)
    assert out.act == "escalate"
    assert b.escalation_imminent is True


def test_cb37_de_escalate_on_hostility():
    """Detected hostility forces a graceful hand-off regardless of the exact bail level."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.meta["hostility"] = True
    b.drivers["bail_risk"] = 0.6  # not extreme, but hostile -> still de-escalate
    out = gates.de_escalate(Decision(act="attempt_close", tier="trial", rationale="close"), b, cfg)
    assert out.act == "escalate"


def test_cb37_de_escalate_leaves_answer_alone():
    """De-escalation blocks selling/discovery, but a plain answer_via_kb (answering a final question)
    is left alone — the agent can still answer, just not push."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.meta["hostility"] = True
    out = gates.de_escalate(Decision(act="answer_via_kb", rationale="answer"), b, cfg)
    assert out.act == "answer_via_kb"


def test_cb37_apply_gates_deescalates_pitch_on_hostility():
    """Through the full chain: a hostile caller + an LLM-proposed pitch -> escalate (no pitch survives)."""
    cfg = make_config()
    b = BeliefState.fresh()
    b.turn_count = 6
    b.meta["hostility"] = True
    b.drivers["bail_risk"] = 0.85
    out = gates.apply_gates(Decision(act="pitch", rationale="pitch anyway"), b, cfg, history=[])
    assert out.act == "escalate"


def test_cb37_apply_gates_deescalates_reask_on_extreme_bail():
    """At extreme bail, even a discovery RE-ASK is dropped in favor of a graceful hand-off."""
    cfg = make_config(extreme_bail_deescalate=0.9)
    b = BeliefState.fresh()
    b.turn_count = 6
    b.drivers["bail_risk"] = 0.92
    out = gates.apply_gates(
        Decision(act="ask", target_slot="grade_level", rationale="re-ask grade"), b, cfg, history=[]
    )
    assert out.act != "ask", "no re-asking once the caller is about to walk"
    assert out.act == "escalate"


async def test_cb37_acceptance_rising_bail_abuse_stops_pitching():
    """ACCEPTANCE: a replay with rising bail + an abusive turn -> the agent stops pitching at the cap and
    de-escalates/wraps by bail ~= 0.9. We drive bail up across two turns, then an abusive turn."""
    cfg = make_config(pushiness_cap=0.7, extreme_bail_deescalate=0.9)
    b = BeliefState.fresh()
    b.turn_count = 5
    history: list[dict] = []

    # Turn 1: bail already over the cap; LLM proposes a pitch -> backed off (not a pitch).
    b.drivers["bail_risk"] = 0.75
    llm1 = MockLLMClient([json.dumps({"bail_risk": 0.0}), _policy_json(act="pitch", rationale="pitch"), "..."])
    d1, _, b = await respond(
        b, history=history, user_utterance="I'm really not interested.", llm_client=llm1, config=cfg
    )
    assert d1.act not in ("pitch", "attempt_close"), "over the cap -> stop pitching"

    # Turn 2: an abusive turn -> hostility flag -> de-escalate to a graceful hand-off (escalate).
    llm2 = MockLLMClient([json.dumps({"bail_risk": 0.2}), _policy_json(act="pitch", rationale="push harder"), "..."])
    d2, _, b = await respond(
        b, history=history, user_utterance="Fuck you, stop calling me.", llm_client=llm2, config=cfg
    )
    assert b.meta.get("hostility") is True
    assert d2.act == "escalate", "an abusive turn must de-escalate, never pitch or re-ask"

# Regression tests (TEST-FIRST) for SAFEGUARD 2 — the runtime grounding guard on the live BLOCKING
# respond() path (src.core.respond). The agent fabricated specific student outcomes ("went from a D
# to a B+", "scored an A on the final") under pressure: retriever.grounded() existed but was only
# called offline in loop/grading.py, never on the live path. This enforces it on the blocking path:
# for a CLAIM turn (answer_via_kb / handle_objection / pitch), after NLG we run grounded(reply, facts);
# only a GENUINE fabricated SPECIFIC is REGENERATED once + then a safe line — a normal on-topic answer
# passes untouched even when grounded()'s strict ratio is low.
#   - A claim reply with a fabricated specific is regenerated/replaced (scripted fabricate->clean).
#   - A grounded claim passes through untouched (no regenerate; one NLG call).
#   - A bare ask turn is NOT grounding-checked (the guard only runs on claim acts).
#   - OVER-FIRE FIX: a normal efficacy/objection answer with no fabricated specific PASSES UNTOUCHED
#     even when grounded()'s ratio is low (the broad SOFT ratio arm was dropped — it sprayed the canned
#     fallback ~4x/turn on Marcus/Dana). The D->B+ STORY fabrication is still caught (outcome-pattern arm).
# Network-free via MockLLMClient (scripted DST/policy/NLG replies). Asserts the FINAL committed reply.
from __future__ import annotations

import json

from src.config.settings import AgentConfig, Persona
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import MockLLMClient
from src.core.respond import respond


def _cfg() -> AgentConfig:
    return AgentConfig(
        version="test_v0",
        kb_version="kb_test",
        persona=Persona(name="Alex", role="learning advisor", style="warm-consultative"),
        prompts={"policy": "Propose the next act as JSON.", "nlg": "Reply concisely."},
        playbooks={"discovery_sequence": ["grade_level", "subject"]},
        thresholds={
            "trust_gate_open_price": 0.5,
            "pushiness_cap": 0.7,
            "pushiness_pressure_count_cap": 2,
            "escalate_low_confidence_turns": 2,
            "low_confidence_level": 0.4,
            "max_concession_band": 0.15,
            "discovery_slots_required": 0,
            "min_turns_before_close": 99,  # keep advance_to_close from converting the act under test
        },
    )


# A KB fact that supports a generic efficacy answer but NOT a specific letter-grade story. The
# fabricated reply invents "D", "B+", "final" — none in the facts -> grounded() must flag it.
_FACT = "Our tutoring is personalized and most families report steady improvement over a few months."
_GROUNDED_FACTS = [_FACT]

_FABRICATED = "One student went from a D to a B plus and scored an A on the final exam after six weeks."
_CLEAN = "Most families report steady improvement over a few months with personalized tutoring."


def _zero_deltas() -> str:
    return json.dumps({d: 0.0 for d in DRIVERS})


def _policy(act: str) -> str:
    return json.dumps({"act": act, "rationale": "test", "confidence": 0.7})


async def test_fabricated_claim_is_regenerated_to_a_grounded_reply():
    """SAFEGUARD 2 core: a CLAIM turn (answer_via_kb) whose NLG reply invents a specific outcome not in
    the facts is detected by grounded() and REGENERATED once with the hardened instruction; the FINAL
    committed reply is the clean, grounded second attempt — never the fabricated first one."""
    cfg = _cfg()
    # 4 scripted calls: DST deltas, policy proposal, NLG (fabricated), NLG regenerate (clean).
    llm = MockLLMClient([_zero_deltas(), _policy("answer_via_kb"), _FABRICATED, _CLEAN])
    b = BeliefState.fresh()
    b.open_question = "do you have proof it works?"
    b.last_user_act = "question"
    decision, reply, _new = await respond(
        b, [], "does it work?", llm, cfg, retrieved_facts=_GROUNDED_FACTS, last_user_act="question"
    )
    assert decision.act == "answer_via_kb"
    assert reply == _CLEAN, f"the fabricated reply must be replaced by the grounded regeneration: {reply!r}"
    assert "D to a B" not in reply and "final exam" not in reply


async def test_fabrication_twice_falls_back_to_safe_line():
    """If BOTH the first reply AND the regeneration are still ungrounded, the guard falls back to a
    safe honest line rather than speaking either fabrication. The committed reply contains no invented
    specific (no letter grade, no fabricated figure)."""
    cfg = _cfg()
    fab2 = "Another student jumped from a C minus to an A in just three weeks flat."
    llm = MockLLMClient([_zero_deltas(), _policy("handle_objection"), _FABRICATED, fab2])
    b = BeliefState.fresh()
    b.active_objection = "efficacy_doubt"
    b.open_question = "prove it actually works"
    b.last_user_act = "objection"
    _decision, reply, _new = await respond(
        b, [], "prove it", llm, cfg, retrieved_facts=_GROUNDED_FACTS, last_user_act="objection"
    )
    assert "D to a B" not in reply and "C minus" not in reply and "A in just three weeks" not in reply
    assert reply.strip() != ""


async def test_grounded_claim_passes_through_untouched_no_regenerate():
    """A grounded claim reply passes straight through — NO regeneration call is made (only the single
    NLG call is consumed). Asserting the script still has the would-be regenerate entry unused proves
    we didn't regenerate a clean reply."""
    cfg = _cfg()
    # Only 3 entries: DST, policy, NLG (already clean). If the guard wrongly regenerated, it would
    # IndexError on the exhausted script — so a clean pass-through is required for this to succeed.
    llm = MockLLMClient([_zero_deltas(), _policy("answer_via_kb"), _CLEAN])
    b = BeliefState.fresh()
    b.open_question = "how does it work?"
    b.last_user_act = "question"
    decision, reply, _new = await respond(
        b, [], "how does it work?", llm, cfg, retrieved_facts=_GROUNDED_FACTS, last_user_act="question"
    )
    assert decision.act == "answer_via_kb"
    assert reply == _CLEAN


async def test_bare_ask_turn_is_not_grounding_checked():
    """A bare `ask` (discovery question) asserts no product fact, so the grounding guard must NOT run
    on it — even with NO retrieved_facts and a reply that would 'fail' grounding, the ask passes
    through with a single NLG call (no regenerate)."""
    cfg = _cfg()
    ask_reply = "What grade is your learner in right now?"
    # Only 3 entries (DST, policy, NLG). A grounding check on an ask would try to regenerate and
    # IndexError; the test passing proves the guard skipped the ask act.
    llm = MockLLMClient([_zero_deltas(), _policy("ask"), ask_reply])
    b = BeliefState.fresh()
    decision, reply, _new = await respond(b, [], "Hi", llm, cfg, retrieved_facts=None)
    assert decision.act == "ask"
    assert reply == ask_reply


async def test_normal_efficacy_answer_with_low_ratio_passes_untouched_no_overfire():
    """OVER-FIRE FIX (ISSUE 1): a normal on-topic efficacy / "is this a scam" answer with NO fabricated
    specific must PASS UNTOUCHED — even though grounded()'s strict term-overlap ratio scores it as
    ungrounded against the brief KB fact. The old broad ratio arm replaced it with the canned fallback
    (60 fires across 15 transcripts, dana_11 12x). Only 3 script entries (DST, policy, NLG) are
    supplied: if the guard wrongly regenerated, it would IndexError on the exhausted script — so the
    test passing PROVES no regenerate/fallback fired."""
    cfg = _cfg()
    normal = (
        "I completely understand wanting proof before you commit — that is fair. We are an established, "
        "legitimate service, and you can start with a low-commitment first session and judge for yourself."
    )
    llm = MockLLMClient([_zero_deltas(), _policy("handle_objection"), normal])
    b = BeliefState.fresh()
    b.active_objection = "efficacy_doubt"
    b.open_question = "how do I know this isn't a scam?"
    b.last_user_act = "objection"
    decision, reply, _new = await respond(
        b, [], "is this a scam?", llm, cfg, retrieved_facts=_GROUNDED_FACTS, last_user_act="objection"
    )
    assert decision.act == "handle_objection"
    assert reply == normal  # untouched — the canned fallback did NOT fire
    assert decision.meta.get("grounding", {}).get("action") == "passthrough"


async def test_real_kb_stat_is_not_flagged_as_fabrication():
    """A REAL KB stat (e.g. the 97% satisfaction figure that IS in kb_v0/results.json) cited against
    facts containing it is grounded() -> supported, so the guard passes it through. This guards against
    the over-fire regressing onto legitimate cited stats."""
    cfg = _cfg()
    facts = ["Nerdy reports a 97% satisfaction rate across families and an independent study found 2x growth."]
    cited = "We have a 97% satisfaction rate, and an independent study found 2x growth."
    llm = MockLLMClient([_zero_deltas(), _policy("answer_via_kb"), cited])
    b = BeliefState.fresh()
    b.open_question = "what proof do you have?"
    b.last_user_act = "question"
    _decision, reply, _new = await respond(
        b, [], "what proof?", llm, cfg, retrieved_facts=facts, last_user_act="question"
    )
    assert reply == cited  # a real cited stat is supported -> untouched


async def test_invented_student_outcome_story_is_still_caught():
    """The DANGEROUS fabrication SAFEGUARD 2 was built for — an invented grade/score-jump STORY ("from
    a D to a B+", "scored an A on the final") — must STILL be caught by the outcome-pattern arm even
    though the broad ratio arm is gone (the story has no price/percent and no CLAIM_TOKEN). It is
    regenerated to the clean reply."""
    cfg = _cfg()
    story = "One of my students went from a D to a B plus and scored an A on the final after eight weeks."
    llm = MockLLMClient([_zero_deltas(), _policy("handle_objection"), story, _CLEAN])
    b = BeliefState.fresh()
    b.active_objection = "efficacy_doubt"
    b.open_question = "does it actually raise grades?"
    b.last_user_act = "objection"
    _decision, reply, _new = await respond(
        b, [], "does it raise grades?", llm, cfg, retrieved_facts=_GROUNDED_FACTS, last_user_act="objection"
    )
    assert "D to a B" not in reply and "scored an A on the final" not in reply
    assert reply == _CLEAN


async def test_no_retrieved_facts_pushes_invented_specific_toward_honest_line():
    """A CLAIM turn with NO retrieved_facts that invents a SPECIFIC figure or a strict promise word (an
    unsupported number / a CLAIM_TOKEN like "guaranteed") IS detectable deterministically even with no
    support text, so the guard regenerates/falls back to an honest line rather than letting the invented
    specific through. (A pure narrative with NO digit/promise word and NO facts is not deterministically
    distinguishable from generic prose — the consolidated first-line NLG grounding rule is the constraint
    there, mirroring the streaming path; this asserts the part the runtime guard can actually enforce.)"""
    cfg = _cfg()
    invented = "We guarantee a 95% jump and a full refund if your scores don't rise 200 points."
    honest = "I do not have a specific number to quote you, but I can connect you with the details."
    llm = MockLLMClient([_zero_deltas(), _policy("pitch"), invented, honest])
    b = BeliefState.fresh()
    b.open_question = "what results do you guarantee?"
    b.last_user_act = "question"
    _decision, reply, _new = await respond(
        b, [], "what results?", llm, cfg, retrieved_facts=None, last_user_act="question"
    )
    assert "95%" not in reply and "200 points" not in reply and "guarantee" not in reply.lower()
    assert reply.strip() != ""

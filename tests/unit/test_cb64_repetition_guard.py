# Regression tests (TEST-FIRST) for CB-64 — repetition residual: near-verbatim re-pitch detector.
# QA evidence: core pitch delivered near-verbatim in replies 1 and 3 (both inside the YOUR_LAST_LINES
# n=4 window — the _NO_RESTATE_INSTRUCTION existed and was ignored); "connect you with my team…exact
# pricing…fifteen minutes" pitched 3× and "fifteen minutes" 2×.
#
# Mechanism (option b from the spec): deterministic post-NLG near-verbatim check via
# nlg._near_verbatim_repetition(): >8 shared content words between the new reply and any prior agent
# line triggers a blocking-path regenerate once with _REPETITION_RETRY_RULE (mirroring the CB-53
# grounding-retry pattern). The streaming path flags decision.meta["repetition"] for observability only.
#
# What we assert:
#   1. _near_verbatim_repetition() catches the QA6 reply-1-vs-3 verbatim re-pitch (>8 shared content
#      words between two near-identical pitch sentences).
#   2. _near_verbatim_repetition() does NOT flag legitimately similar short acks (≤8 shared words).
#   3. _near_verbatim_repetition() does NOT flag a reply with no own_last_lines (turn 1).
#   4. The blocking-path guard (_derepetition_blocking_reply) regenerates once when repetition fires
#      and returns the non-repeating second attempt.
#   5. The blocking-path guard accepts the regenerated reply even if it STILL repeats (one retry cap).
#   6. respond() end-to-end: the final committed reply is the regenerated non-repeating version, not
#      the first near-verbatim re-pitch. The NLG call count confirms a regenerate happened.
#   7. respond_stream() end-to-end: the stream flags decision.meta["repetition"]["action"] == "stream_flag_only"
#      when repetition is detected; no regeneration on the stream path.
#   8. A genuinely different reply (low overlap) passes through untouched (no regenerate on the
#      blocking path; single NLG call consumed).
# Network-free: scripted MockLLMClient + direct function calls.
from __future__ import annotations

import json

import pytest

from src.config.settings import AgentConfig, Persona
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import MockLLMClient
from src.core.nlg import _near_verbatim_repetition
from src.core.respond import StreamResult, respond, respond_stream


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


def _flat_deltas() -> str:
    return json.dumps({d: 0.0 for d in DRIVERS})


def _policy_json(act: str) -> str:
    return json.dumps({"act": act, "rationale": "test", "confidence": 0.75})


# QA6 defect pair: the exact re-pitch delivered in reply 1 and reply 3 of the QA6 conversation.
# These 10+ shared content words must be caught by the detector.
_QA6_PITCH_REPLY_1 = (
    "Let me connect you with my team — they can walk you through exact pricing and get you set up "
    "for a fifteen-minute call to see if it's the right fit."
)
_QA6_PITCH_REPLY_3 = (
    "What I can do is connect you with my team who'll share exact pricing and book a "
    "fifteen-minute call to confirm it's the right fit for you."
)

# A legitimately similar SHORT ack that must NOT be flagged (≤8 content words shared).
_ACK_1 = "That makes sense — I understand."
_ACK_2 = "That makes sense, absolutely."

# A genuinely different reply (low content-word overlap with _QA6_PITCH_REPLY_1).
_DIFFERENT_REPLY = (
    "Our tutors are matched based on your learner's specific subject and grade level, which is "
    "why the consultation helps us find the right fit — no two plans are the same."
)

# Retrieved facts used for answer_via_kb tests so the grounding guard does not interfere.
_PRICING_FACTS = [
    "Most families land around $300-$600/month depending on hours and plan (REPORTED).",
    "Typical ARPM: ~$335/month (VERIFIED, Nerdy Q1 2025).",
]


# ---------------------------------------------------------------------------
# CB-64 unit: _near_verbatim_repetition() — the deterministic detector
# ---------------------------------------------------------------------------

def test_near_verbatim_repetition_catches_qa6_repitch():
    """_near_verbatim_repetition() must flag the QA6 defect: reply-1 and reply-3 share well over 8
    content words ('connect', 'team', 'exact', 'pricing', 'fifteen', 'minute', 'call', 'fit', etc.)
    — these are the near-verbatim pairs the _NO_RESTATE_INSTRUCTION failed to prevent."""
    own_lines = [_QA6_PITCH_REPLY_1]
    assert _near_verbatim_repetition(_QA6_PITCH_REPLY_3, own_lines), (
        "_near_verbatim_repetition must detect the QA6 reply-1-vs-reply-3 near-verbatim re-pitch "
        "(>8 shared content words): prior=%r, new=%r"
        % (_QA6_PITCH_REPLY_1[:60], _QA6_PITCH_REPLY_3[:60])
    )


def test_near_verbatim_repetition_symmetric():
    """The detector fires in both directions — if reply-1 is used as 'own_lines' against reply-3,
    and also if reply-3 is used as 'own_lines' against reply-1 (order doesn't matter)."""
    assert _near_verbatim_repetition(_QA6_PITCH_REPLY_1, [_QA6_PITCH_REPLY_3])


def test_near_verbatim_repetition_does_not_flag_short_similar_acks():
    """Short, legitimately-similar acks ('That makes sense — I understand.' vs 'That makes sense,
    absolutely.') share ≤8 content words and must NOT be flagged — the threshold is calibrated to
    catch pitch-level verbatim re-pitches only, not brief conversational echoes."""
    assert not _near_verbatim_repetition(_ACK_2, [_ACK_1]), (
        f"Short similar acks must not be flagged as repetition: {_ACK_1!r} vs {_ACK_2!r}"
    )


def test_near_verbatim_repetition_no_prior_lines_returns_false():
    """With no own_last_lines (turn 1), _near_verbatim_repetition must return False — there is
    nothing to compare against, and the opening turn is always allowed through."""
    assert not _near_verbatim_repetition(_QA6_PITCH_REPLY_1, [])
    assert not _near_verbatim_repetition(_QA6_PITCH_REPLY_1, None)


def test_near_verbatim_repetition_does_not_flag_genuinely_different_reply():
    """A reply that shares fewer than 9 content words with own_last_lines passes through untouched."""
    assert not _near_verbatim_repetition(_DIFFERENT_REPLY, [_QA6_PITCH_REPLY_1]), (
        "A genuinely different reply must NOT be flagged as near-verbatim repetition"
    )


def test_near_verbatim_repetition_short_reply_not_flagged():
    """A reply that is itself too short (≤8 content words) is never flagged — a brief response
    cannot be a verbatim re-pitch."""
    short = "Got it, that helps."
    assert not _near_verbatim_repetition(short, [_QA6_PITCH_REPLY_1])


def test_near_verbatim_repetition_checks_all_prior_lines():
    """When own_last_lines contains multiple prior lines, the detector checks each one — a later
    line that repeats an earlier one (not the immediately prior one) is still caught."""
    earlier_line = _QA6_PITCH_REPLY_1
    middle_line = "Our program is fully online and flexible."
    prior_lines = [earlier_line, middle_line]
    # _QA6_PITCH_REPLY_3 repeats the EARLIER line, not the immediately prior middle line.
    assert _near_verbatim_repetition(_QA6_PITCH_REPLY_3, prior_lines), (
        "Detector must catch repetition against any prior line, not just the most recent one"
    )


# ---------------------------------------------------------------------------
# CB-64 integration: blocking path regenerates on detected repetition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_blocking_path_regenerates_once_on_detected_repetition():
    """respond() must regenerate exactly once when _near_verbatim_repetition() fires. The scripted
    NLG returns the re-pitch on the first call and a non-repeating advancement on the second. The
    final committed reply must be the non-repeating second attempt, not the re-pitch.

    Uses answer_via_kb with pricing facts so the grounding guard passes cleanly (the replies are
    grounded against the supplied facts and contain no fabricated specifics)."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"
    belief.open_question = "Tell me more about the program."

    non_repeating = (
        "Our curriculum adapts in real time based on how the learner responds — so the tutoring "
        "adjusts as they grow, not just session to session."
    )

    # History: reply 1 was the QA6 pitch variant (now in own_last_lines).
    history = [
        {"role": "user", "text": "Tell me about the program."},
        {"role": "assistant", "text": _QA6_PITCH_REPLY_1, "act": "answer_via_kb"},
    ]

    # Scripted: deltas, policy, NLG first attempt (re-pitch), grounding check pass (no extra call
    # since grounded() is called on the result, not via LLM), NLG regenerate (non-repeating).
    # The grounding guard passes because: (a) the replies contain no CLAIM_TOKEN or invented number,
    # (b) facts are supplied so grounded() has something to check against.
    llm = MockLLMClient([
        _flat_deltas(),
        _policy_json("answer_via_kb"),
        _QA6_PITCH_REPLY_3,   # first NLG: near-verbatim re-pitch -> repetition guard regenerates
        non_repeating,         # second NLG: genuinely different -> committed reply
    ])

    decision, reply, _new = await respond(
        belief, history, "What else can you tell me?", llm, cfg,
        retrieved_facts=_PRICING_FACTS,
    )

    assert reply == non_repeating, (
        f"The blocking path must return the non-repeating regenerated reply, got: {reply!r}"
    )
    rep_meta = decision.meta.get("repetition", {})
    assert rep_meta.get("action") == "regenerated", (
        f"decision.meta['repetition']['action'] must be 'regenerated' when the guard fires; got {rep_meta}"
    )


@pytest.mark.asyncio
async def test_blocking_path_accepts_retry_when_still_repeating():
    """If BOTH the first reply AND the regenerated reply are still near-verbatim (the retry was also
    ignored), the guard accepts the regenerated reply anyway — one retry is the cap. The committed
    reply is the second attempt (not the first), and action is 'retry_accepted'."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"
    belief.open_question = "Tell me more about the program."

    history = [
        {"role": "user", "text": "Tell me about the program."},
        {"role": "assistant", "text": _QA6_PITCH_REPLY_1, "act": "answer_via_kb"},
    ]
    # Both NLG calls return near-verbatim re-pitches (the retry was also ignored by the LLM).
    also_repeating = (
        "You can connect with my team who'll walk through exact pricing and set up a "
        "fifteen-minute call to confirm the right fit for you."
    )

    llm = MockLLMClient([
        _flat_deltas(),
        _policy_json("answer_via_kb"),
        _QA6_PITCH_REPLY_3,    # first: near-verbatim
        also_repeating,         # second: still near-verbatim (retry) -> accepted as-is
    ])

    decision, reply, _new = await respond(
        belief, history, "Tell me more.", llm, cfg,
        retrieved_facts=_PRICING_FACTS,
    )

    # The reply must be the second attempt (not the first — the guard always prefers the retry).
    assert reply == also_repeating, (
        "When both attempts repeat, the guard must commit the second attempt, not the first"
    )
    rep_meta = decision.meta.get("repetition", {})
    assert rep_meta.get("action") == "retry_accepted", (
        f"action must be 'retry_accepted' when both attempts repeat; got {rep_meta}"
    )


@pytest.mark.asyncio
async def test_blocking_path_passthrough_on_genuinely_different_reply():
    """A non-repeating first reply passes through untouched — only ONE NLG call is consumed. The
    mock script has only 3 entries (deltas, policy, NLG); if the guard wrongly regenerated it would
    IndexError on the exhausted script, proving single-call behaviour."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"
    belief.open_question = "Tell me more."

    history = [
        {"role": "user", "text": "Tell me about the program."},
        {"role": "assistant", "text": _QA6_PITCH_REPLY_1, "act": "answer_via_kb"},
    ]

    # Only 3 script entries: deltas, policy, NLG. If guard regenerates, IndexError proves the bug.
    llm = MockLLMClient([
        _flat_deltas(),
        _policy_json("answer_via_kb"),
        _DIFFERENT_REPLY,  # non-repeating -> no regenerate
    ])

    decision, reply, _new = await respond(
        belief, history, "Tell me more.", llm, cfg,
        retrieved_facts=_PRICING_FACTS,
    )

    assert reply == _DIFFERENT_REPLY
    rep_meta = decision.meta.get("repetition", {})
    assert rep_meta.get("action") == "passthrough", (
        f"action must be 'passthrough' for a non-repeating reply; got {rep_meta}"
    )


@pytest.mark.asyncio
async def test_blocking_path_no_own_lines_no_regenerate():
    """On the first turn (no history / no own_last_lines), the detector always returns False and the
    guard never regenerates — even a re-pitchy reply is fine on turn 1 (nothing to compare against).

    Uses answer_via_kb with facts so the grounding guard passes on the first-turn reply cleanly
    (no extra LLM calls). Only 3 script entries total: if the repetition guard wrongly regenerated
    it would IndexError on the exhausted script, proving passthrough behaviour on turn 1."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"
    belief.open_question = "Tell me about the program."

    # A reply grounded in the supplied facts: mentions $300-$600 (which is in _PRICING_FACTS).
    grounded_first_turn = (
        "Our plans typically run around $300 to $600 per month depending on hours — your exact "
        "figure comes from a quick, no-obligation consultation."
    )

    # Only 3 script entries — proves no regenerate fired on an empty history.
    llm = MockLLMClient([
        _flat_deltas(),
        _policy_json("answer_via_kb"),
        grounded_first_turn,
    ])

    decision, reply, _new = await respond(
        belief, [], "Hi, tell me about the program.", llm, cfg,
        retrieved_facts=_PRICING_FACTS,
    )

    assert reply == grounded_first_turn  # untouched
    rep_meta = decision.meta.get("repetition", {})
    assert rep_meta.get("action") == "passthrough"


# ---------------------------------------------------------------------------
# CB-64 integration: streaming path flags only, does not regenerate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_path_flags_repetition_without_regenerating():
    """respond_stream() must flag decision.meta["repetition"]["action"] == "stream_flag_only" when
    near-verbatim repetition is detected — it does NOT regenerate (a streamed token can't be un-spoken,
    mirroring the SAFEGUARD 2 stream discipline). The full reply is the near-verbatim first reply."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"

    history = [
        {"role": "user", "text": "Tell me about the program."},
        {"role": "assistant", "text": _QA6_PITCH_REPLY_1, "act": "pitch"},
    ]

    # Stream path: only 3 script entries (no regenerate call is ever made). If the guard wrongly
    # tried to regenerate it would IndexError, proving the stream is flag-only.
    llm = MockLLMClient([
        _flat_deltas(),
        _policy_json("pitch"),
        _QA6_PITCH_REPLY_3,  # near-verbatim re-pitch -> must be flagged, NOT regenerated
    ])

    holder = StreamResult()
    tokens: list[str] = []
    async for token in respond_stream(
        belief, history, "What else?", llm, cfg, result=holder,
    ):
        tokens.append(token)

    assert holder.ready
    assert "".join(tokens) == holder.reply
    # The streamed reply is the re-pitch (not regenerated — flag only).
    assert _QA6_PITCH_REPLY_3 in holder.reply or holder.reply in _QA6_PITCH_REPLY_3
    rep_meta = holder.decision.meta.get("repetition", {})
    assert rep_meta.get("action") == "stream_flag_only", (
        f"Stream path must set action='stream_flag_only' on detected repetition; got {rep_meta}"
    )


@pytest.mark.asyncio
async def test_stream_path_passthrough_on_non_repeating_reply():
    """respond_stream() sets action='passthrough' when the reply does not repeat own_last_lines."""
    cfg = _cfg()
    belief = BeliefState.fresh()
    belief.last_user_act = "question"

    history = [
        {"role": "user", "text": "Tell me about the program."},
        {"role": "assistant", "text": _QA6_PITCH_REPLY_1, "act": "pitch"},
    ]

    llm = MockLLMClient([
        _flat_deltas(),
        _policy_json("pitch"),
        _DIFFERENT_REPLY,
    ])

    holder = StreamResult()
    async for _ in respond_stream(
        belief, history, "Tell me more.", llm, cfg, result=holder,
    ):
        pass

    rep_meta = holder.decision.meta.get("repetition", {})
    assert rep_meta.get("action") == "passthrough"

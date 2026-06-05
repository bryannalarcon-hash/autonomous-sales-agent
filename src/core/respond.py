# The transport-agnostic brain core (plan KTD2 / U4 — THE load-bearing boundary). respond() ties
# the whole brain together for ONE turn and is the single entry point both the headless text self-
# play (loop/) and the live LiveKit voice adapter (voice/) call — so what is optimized in text is
# exactly what ships in voice. Orchestration: dst.update(belief, last_agent_act, user_utterance) ->
# policy.decide(...) [LLM proposes; deterministic gates already applied inside] -> nlg.realize(...).
# Returns (Decision, reply_text, new_belief): the Decision for logging/decision-trace, the realized
# reply, and the NEW BeliefState (the input is never mutated). NO LiveKit types cross this boundary.
# CB-41 (stream NLG -> TTS): respond_stream() is the STREAMING entry point the voice worker consumes.
# It runs DST -> gated Policy through the EXACT SAME _decide_turn() path respond() uses (so the gated
# decision + new_belief are byte-for-byte what respond() would produce — the gate logic is NOT
# duplicated), then STREAMS the NLG via nlg.realize_stream, yielding reply tokens as they generate.
# It exposes the final (decision, full_reply, new_belief) on a StreamResult holder the caller reads
# AFTER the stream ends (an async generator cannot both yield tokens and return a tuple), so the
# worker commits the SAME reliable state (CB-22) the blocking path commits. concat(tokens) == reply.
# TIMING: each of the three stages is wrapped in time.perf_counter() and the breakdown (dst/policy/nlg
# + total) is attached to decision.meta["timings"] and logged once per turn on the "brain.timing"
# logger — so per-turn AND per-model-call latency is measurable on both the text and voice paths.
# CB-28: on a TOOL-USE act (answer_via_kb) grounded in retrieved_facts, the grounding facts are also
# stamped on decision.meta["retrieved"] (JSON-able strings) so the runtime can persist them onto
# Turn.retrieved and Call Review can show WHAT information was pulled for that answer.
# CB-35: after the gated decision, respond() records the asked discovery slot on new_belief.meta
# (asked_slots count + pending_ask_slot) so the no-repeat guard drops over-asked slots and the next
# turn's DST grade-extraction knows the grade was just asked. Carried across the call by BeliefState.copy().
# CB-48 (anti-repetition): respond/respond_stream extract the agent's OWN last 1-2 spoken lines from
# `history` and thread them to NLG (realize/realize_stream, in lock-step) so the prompt can forbid
# restating the same hedge/offer. ONLY the agent's lines are passed (never the caller transcript) — the
# distillation-preserving choice (no new inventable facts). Public respond/respond_stream signatures are
# unchanged (history is already a param), so the voice worker is unaffected.
# SAFEGUARD 1 (loop-breaker bookkeeping): _decide_turn maintains belief.meta["recent_acts"] (the last K
# gated acts) + ["progress_snapshot"] (prior-turn locked-slot count / active_objection / purchase_intent)
# so gates.break_no_progress_loop can detect a stalled K-reactive-no-progress run and force a commit.
# Both are stamped from the GATED decision/new_belief and carried across the call by BeliefState.copy(),
# so the blocking + streaming paths feed the breaker IDENTICAL inputs (it runs inside the shared decide()).
# ISSUE 2 stickiness: once a forced commit fires (decision.meta["loop_break"]) we latch
# meta["loop_break_fired"] so a SECOND re-stall escalates to a firm terminal, not the same soft close;
# and once the terminal escalate fires (meta["loop_terminal"]) we latch meta["loop_terminal_fired"] so
# the breaker stays escalated (escalate-and-stay) instead of relapsing into the loop if the call continues.
# SAFEGUARD 2 (runtime grounding) — ASYMMETRIC by design and TIGHTLY scoped: the BLOCKING respond()
# path runs retriever.grounded() on a CLAIM reply (answer_via_kb/handle_objection/pitch) AFTER NLG, but
# only REGENERATES (then falls back to a safe honest line) when the reply contains a genuine fabricated
# SPECIFIC — an unsupported NUMBER, an unsupported strict CLAIM_TOKEN, or an invented STUDENT-OUTCOME
# pattern (grade/score jump). A normal on-topic efficacy/objection answer PASSES UNTOUCHED even when
# grounded()'s strict term-overlap ratio is low (the broad ratio arm was dropped — it sprayed the canned
# fallback ~4x/turn on Marcus/Dana). The STREAMING respond_stream() path CANNOT un-speak a streamed
# token, so it does NOT regenerate; it relies on the same hardened first-line grounding rule in the
# prompt and only FLAGS an ungrounded streamed reply on decision.meta["grounding"] for observability.
# R37 parity preserved: both paths run the SAME brain; only the blocking path gets the regenerate backstop.
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Sequence

from src.config.settings import AgentConfig
from src.core import dst, gates
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient
from src.core.nlg import realize, realize_stream
from src.core.policy import Decision, decide
from src.kb import retriever

# One structured line per agent turn with the three model-call stage timings + total. Each stage is
# dominated by exactly ONE LLM round-trip (the slot/gate/trend work between calls is sub-millisecond),
# so a stage's time IS that model call's latency. Filter/route via logging.getLogger("brain.timing").
_timing_log = logging.getLogger("brain.timing")

# CB-28: acts that GROUND their reply in retrieved KB facts (tool-use). Only these capture the
# retrieved payload onto decision.meta["retrieved"] for Call Review's tool-use inspection panel.
_TOOL_ACTS = frozenset({"answer_via_kb"})


# CB-35: discovery acts that ASK the prospect for a slot value (used to count re-asks). confirm_known
# is excluded — it confirms what we already have, it does not re-prompt the prospect for the answer.
_ASK_ACTS = frozenset({"ask"})


# SAFEGUARD 2 (runtime grounding): acts that ASSERT a product fact/proof — the only turns the grounding
# guard checks. A bare ask/confirm_known/escalate asserts nothing, so it is never grounding-checked.
# Mirrors loop/grading._FACTUAL_ACTS (minus confirm_known, which only echoes an already-known slot).
_CLAIM_ACTS = frozenset({"answer_via_kb", "handle_objection", "pitch"})

# The honest fallback line spoken when a claim reply is STILL ungrounded after one regenerate — better
# to say plainly that we don't have the specific than to speak a fabrication (the failure SAFEGUARD 2
# fixes: invented "D to a B+" student outcomes under pressure). Keeps a concrete next step.
_UNGROUNDED_FALLBACK = (
    "I don't have a specific figure I can quote you on that, but I can connect you with the exact "
    "details and a plan that fits — would that help?"
)


# SAFEGUARD 2 — an INVENTED STUDENT-OUTCOME pattern: a grade-jump / score-jump narrative ("from a D to
# a B+", "went from a 2 to a 4", "scored an A on the final", "jumped 120 points on the SAT"). This is
# the dangerous fabrication Safeguard 2 caught (the D->B+ story) — it has no price/percent so the NUMBER
# arm misses it, and "scored"/"jumped" aren't CLAIM_TOKENS. We detect the shape directly so it's flagged
# even when the broad ratio arm is gone. Tight + literal so it does NOT fire on a normal efficacy answer.
_OUTCOME_FABRICATION_RES = (
    # "from a D to a B(+)", "from a C- to an A", "from a 2 to a 4" — a graded before->after jump.
    re.compile(r"\bfrom\s+(?:a|an)?\s*[A-F][+\-]?\b.*?\bto\s+(?:a|an)?\s*[A-F][+\-]?\b", re.I),
    re.compile(r"\bfrom\s+(?:a|an)?\s*\d+\b.*?\bto\s+(?:a|an)?\s*\d+\b", re.I),
    # "went/jumped/improved/rose/climbed/raised ... from ... to ..." (a movement verb + a jump).
    re.compile(
        r"\b(?:went|jump(?:ed)?|improv(?:ed)?|ros(?:e)?|climb(?:ed)?|rais(?:ed)?|"
        r"bump(?:ed)?|boost(?:ed)?|gain(?:ed)?)\b.{0,40}?\bfrom\b.{0,40}?\bto\b",
        re.I,
    ),
    # "scored/got/earned an A/B/... on the final/test/exam/quiz" — an invented graded result.
    re.compile(
        r"\b(?:scored|got|earned|aced)\b.{0,20}?\b(?:an?\s+)?[A-F][+\-]?\b.{0,20}?"
        r"\b(?:final|exam|test|quiz|midterm)\b",
        re.I,
    ),
    # "jumped/gained/rose N points" — an invented point-gain stat presented as a specific outcome.
    re.compile(
        r"\b(?:jump(?:ed)?|gain(?:ed)?|ros(?:e)?|improv(?:ed)?|rais(?:ed)?|up)\b.{0,15}?\b\d+\s*points?\b",
        re.I,
    ),
)


def _has_invented_outcome(reply: str, report: Any) -> bool:
    """True when the reply contains an invented STUDENT-OUTCOME pattern (grade/score jump) that the KB
    does NOT support. We only treat it as invented when the report is ungrounded (grounded() already
    confirmed the specifics aren't backed) AND the literal outcome shape is present — so a real KB-backed
    outcome (none currently exist in kb_v0/results.json, which carries only aggregate proof) wouldn't be
    flagged, and a normal efficacy answer with no before->after narrative never matches the regexes."""
    return any(rx.search(reply) for rx in _OUTCOME_FABRICATION_RES)


def _is_fabrication(reply: str, report: Any, facts: Sequence[Any]) -> bool:
    """Decide whether an ungrounded reply is a genuine FABRICATION worth replacing — TIGHTLY scoped to
    invented SPECIFICS only (the over-fire fix). grounded() is a strict term-overlap check that scores
    almost any non-verbatim prose as ungrounded, so a broad ratio test sprayed the canned fallback over
    normal efficacy / "is this a scam" / proof answers (60 fires across 15 transcripts). We therefore
    fire ONLY on a real fabricated specific and let an ordinary on-topic answer PASS UNTOUCHED even when
    grounded()'s ratio is low:
      (a) an unsupported NUMBER presented as a stat/figure (an invented "120 points", "8 weeks", "$299"
          — report.unsupported_numbers);
      (b) an unsupported strict CLAIM_TOKEN (a curated promise word: guarantee/refund/proven/
          accredited/free/... not backed by the KB);
      (c) an invented STUDENT-OUTCOME pattern (grade-jump / score-jump narrative — the D->B+ story shape
          the NUMBER + CLAIM_TOKEN arms both miss).
    The broad SOFT ratio arm is GONE: a normal objection/efficacy/value answer with NO fabricated
    specific is NOT a fabrication, regardless of grounded()'s ratio. grounded() returns grounded=True for
    a clean reply, so this is only consulted on a ratio/number/claim failure."""
    # (a) an unsupported number presented as a figure.
    if report.unsupported_numbers:
        return True
    # (b) an unsupported strict claim token in the reply (re-derive the strict subset; the report's
    # surfaced term list mixes claim tokens with ordinary ratio-failure words).
    reply_words = {w.lower() for w in retriever._WORD_RE.findall(reply)}
    if reply_words & retriever.CLAIM_TOKENS & set(report.unsupported_terms or []):
        return True
    if reply_words & retriever.CLAIM_TOKENS and not facts:
        return True  # a promise word with no facts at all to support it is unsupported by definition
    # (c) an invented student-outcome narrative (grade/score jump) the KB doesn't back.
    if _has_invented_outcome(reply, report):
        return True
    # NO broad ratio arm — a normal on-topic answer with no fabricated specific passes untouched.
    return False


async def _ground_blocking_reply(
    reply: str,
    decision: Decision,
    belief: BeliefState,
    config: AgentConfig,
    llm_client: LLMClient,
    retrieved_facts: Optional[Sequence[Any]],
    own_last_lines: Sequence[str],
) -> str:
    """SAFEGUARD 2 — enforce grounding on the live BLOCKING reply for a CLAIM turn (no-op otherwise).

    For a claim act (answer_via_kb/handle_objection/pitch) only, run retriever.grounded(reply, facts)
    and consult _is_fabrication() — which fires ONLY on a genuine fabricated SPECIFIC (an unsupported
    number, an unsupported strict CLAIM_TOKEN, or an invented grade/score-jump outcome), NOT on a merely
    low-ratio on-topic answer. On a fabrication, REGENERATE once via NLG with the hardened grounding
    instruction (grounding_retry=True); if it is STILL a fabrication, fall back to a safe honest line.
    A grounded reply, a non-claim turn, OR an ungrounded-but-not-fabrication reply passes through
    untouched and makes NO extra LLM call. NEVER raises — any guard failure returns the original reply
    (the call path must not crash). The chosen reply + whether it was regenerated/replaced is stamped on
    decision.meta["grounding"] for observability."""
    if decision.act not in _CLAIM_ACTS:
        return reply
    facts = list(retrieved_facts or [])
    try:
        report = retriever.grounded(reply, facts)
    except Exception:
        return reply  # a guard failure must never crash the turn — keep the original reply
    # A grounded reply, OR an ungrounded-but-not-fabrication reply (a benign on-topic prose pitch with
    # no invented specific), passes through untouched — no extra LLM call. Only a FABRICATION (an
    # invented figure/promise/outcome-story) is regenerated.
    if report.grounded or not _is_fabrication(reply, report, facts):
        decision.meta["grounding"] = {
            "checked": True, "grounded": bool(report.grounded), "action": "passthrough",
        }
        return reply

    # Fabrication: regenerate ONCE with the hardened instruction.
    try:
        regenerated = await realize(
            decision, belief, config, llm_client,
            retrieved_facts=retrieved_facts,
            own_last_lines=own_last_lines,
            grounding_retry=True,
        )
        regen_report = retriever.grounded(regenerated, facts)
    except Exception:
        # Regeneration call failed — do NOT speak the flagged original; use the safe honest line.
        decision.meta["grounding"] = {
            "checked": True, "grounded": False, "action": "fallback",
            "unsupported": list(report.unsupported_numbers) + list(report.unsupported_terms),
        }
        return _UNGROUNDED_FALLBACK
    # The regeneration is accepted if it is grounded OR at least no longer a fabrication (the hardened
    # instruction steered it to an honest, specific-free line that the strict ratio bar may still trip).
    if regen_report.grounded or not _is_fabrication(regenerated, regen_report, facts):
        decision.meta["grounding"] = {"checked": True, "grounded": False, "action": "regenerated"}
        return regenerated
    # Still a fabrication after the regenerate -> the safe honest line (never speak the invention).
    decision.meta["grounding"] = {
        "checked": True, "grounded": False, "action": "fallback",
        "unsupported": list(regen_report.unsupported_numbers) + list(regen_report.unsupported_terms),
    }
    return _UNGROUNDED_FALLBACK


def _record_asked_slot(belief: BeliefState, decision: Any) -> None:
    """Maintain the per-call ASKED-slot bookkeeping on belief.meta from the GATED decision (CB-35).

    On an `ask` for a slot: bump belief.meta["asked_slots"][slot] and set ["pending_ask_slot"] = slot
    (so the no_repeat_discovery gate can drop an over-asked slot next turn, and the DST knows the grade
    was just asked). Any non-ask act clears the pending marker so a stale "we just asked X" can't carry
    into an unrelated turn. Mutates the belief in place; copy() carries meta to the next turn's input."""
    if decision.act in _ASK_ACTS and decision.target_slot:
        slot = decision.target_slot
        asked = dict(belief.meta.get("asked_slots", {}))
        asked[slot] = int(asked.get(slot, 0)) + 1
        belief.meta["asked_slots"] = asked
        belief.meta["pending_ask_slot"] = slot
    else:
        belief.meta["pending_ask_slot"] = None


def _last_agent_act(history: Optional[Sequence[Any]]) -> Optional[str]:
    """The most recent agent act in history — fed to the DST so driver deltas can condition on it."""
    if not history:
        return None
    for turn in reversed(list(history)):
        if isinstance(turn, dict) and turn.get("role") in ("assistant", "agent"):
            act = turn.get("act")
            return str(act) if act is not None else None
    return None


def _own_last_lines(history: Optional[Sequence[Any]], n: int = 4) -> list[str]:
    """The agent's OWN last `n` spoken lines from history, oldest-first (CB-48 anti-repetition).

    CB-51: widened 2 -> 4 because a thin-proof persona (Dana) restated the SAME stat several turns
    apart and a 2-line window missed it; 4 lines covers the recent cycle the model tends to loop on
    without bloating the prompt (still only the agent's OWN lines — no caller transcript).

    Scans history most-recent-first for agent turns (role in assistant/agent) and collects their
    spoken text (the selfplay/voice shape writes it under "text"; older fixtures used "content"),
    skipping blanks, until `n` are gathered. ONLY the agent's lines are returned — the caller
    transcript is intentionally excluded so threading these into NLG cannot introduce a new fact
    (distillation-preserving). Returned oldest->newest so the prompt reads chronologically."""
    if not history:
        return []
    collected: list[str] = []
    for turn in reversed(list(history)):
        if not isinstance(turn, dict) or turn.get("role") not in ("assistant", "agent"):
            continue
        text = str(turn.get("text", turn.get("content", "")) or "").strip()
        if not text:
            continue
        collected.append(text)
        if len(collected) >= n:
            break
    return list(reversed(collected))


@dataclass
class StreamResult:
    """The final outcome of respond_stream(), filled in AFTER the token stream ends (CB-41).

    An async generator cannot both yield tokens and return a tuple, so respond_stream yields the NLG
    tokens and stamps the gated `decision`, the assembled `reply` (== concat of the yielded tokens),
    and the `new_belief` here once the stream completes. The voice worker reads these to commit the
    SAME reliable state (CB-22) the blocking respond() path commits, with R37 parity guaranteed by
    reply == ''.join(tokens). `ready` flips True only after the stream finishes."""

    decision: Optional[Decision] = None
    reply: str = ""
    new_belief: Optional[BeliefState] = None
    ready: bool = False
    tokens: list[str] = field(default_factory=list)


async def _decide_turn(
    belief: BeliefState,
    history: Sequence[Any],
    user_utterance: str,
    llm_client: LLMClient,
    config: AgentConfig,
    *,
    retrieved_facts: Optional[Sequence[Any]],
    last_user_act: Optional[str],
) -> tuple[Decision, BeliefState, float, float, float]:
    """Run DST -> gated Policy + the per-turn bookkeeping, the SHARED prelude of respond() and
    respond_stream() (CB-41 — the gated decision must be IDENTICAL on both paths, so the gate logic
    lives here exactly once and is never duplicated into the worker).

    Returns (gated_decision, new_belief, t0, t_after_dst, t_after_policy): the gated Decision, the NEW
    belief (input never mutated), and the three perf_counter marks the caller uses to finish the stage
    timings once NLG completes. Steps 1-2 of the per-turn flow:
      1. DST update — fold the utterance into a NEW belief (slots + driver deltas + trends + meta).
      2. Policy decide — LLM proposes; deterministic gates filter/override (skip-known,
         must-clear-objection, trust-gated price, pushiness cap, escalation). Returns the GATED act.
    Also records decision confidence onto the belief, the CB-35 asked-slot bookkeeping, and the CB-28
    tool-use retrieved-facts stamp — all BEFORE NLG, identical for both the blocking + streaming path.
    """
    t0 = time.perf_counter()
    new_belief = await dst.update(
        belief,
        _last_agent_act(history),
        user_utterance,
        llm_client,
        last_user_act=last_user_act,
    )
    t1 = time.perf_counter()

    decision = await decide(new_belief, history=history, config=config, llm_client=llm_client)
    t2 = time.perf_counter()

    # Record the decision's confidence onto the belief for the decision-trace / low-confidence
    # escalation streak (escalation_imminent is set inside the gates when an EXTREME trigger fires).
    if decision.confidence is not None:
        new_belief.decision_confidence = float(decision.confidence)

    # CB-35: record which discovery slot the agent actually ASKS this turn so the no-repeat guard can
    # drop a slot asked too many times, and so next turn's DST grade-extraction knows the grade was just
    # asked (bare-number acceptance). Counted from the GATED decision (what the agent really does), and
    # carried across the call on belief.meta by BeliefState.copy(). Only the `ask` act sets a pending
    # slot + increments the count; any non-ask clears the pending marker so a stale one can't linger.
    _record_asked_slot(new_belief, decision)

    # SAFEGUARD 1 bookkeeping: maintain the loop-breaker's inputs on new_belief.meta (carried to the
    # next turn by BeliefState.copy()). PRIOR-turn recent_acts + progress_snapshot were already on the
    # belief when apply_gates ran (so the breaker saw them); now record THIS turn's GATED act into the
    # K-act window and overwrite the snapshot with the CURRENT state so next turn can detect progress
    # (a slot newly locked / an objection cleared / purchase_intent risen) since this turn.
    gates.record_recent_act(new_belief, decision.act)
    new_belief.meta["progress_snapshot"] = gates.progress_snapshot(new_belief)
    # ISSUE 2 (stickiness): once the loop-breaker has fired a forced commit this call, latch it so a
    # SUBSEQUENT re-stall escalates to a firm terminal instead of re-offering the same soft close. A
    # sticky one-way flag carried by copy(); the breaker reads it on the next stall. A second flag,
    # loop_terminal_fired, latches once the terminal escalate has fired so the breaker stays escalated
    # (escalate-and-stay) rather than relapsing into the loop if the call continues past the hand-off.
    if decision.meta.get("loop_break"):
        new_belief.meta["loop_break_fired"] = True
    if decision.meta.get("loop_terminal"):
        new_belief.meta["loop_terminal_fired"] = True

    # CB-28: when this turn is a TOOL-USE act (answer_via_kb) grounded in retrieved facts, capture
    # the facts that grounded it on decision.meta["retrieved"] (JSON-able strings) so the runtime can
    # persist them onto Turn.retrieved and Call Review can show WHAT was pulled. Stamped only for the
    # grounded-answer act so non-tool turns stay clean; str()-ing the chunks keeps it transport- and
    # DB-agnostic (a Chunk str()s to "[source] text"; a plain str passes through). Best-effort: a
    # non-iterable / unstringifiable facts payload is dropped rather than crashing the turn.
    if decision.act in _TOOL_ACTS and retrieved_facts:
        try:
            decision.meta["retrieved"] = [str(f) for f in retrieved_facts]
        except TypeError:
            pass

    return decision, new_belief, t0, t1, t2


def _stamp_timings(
    decision: Decision,
    new_belief: BeliefState,
    llm_client: LLMClient,
    *,
    t0: float,
    t1: float,
    t2: float,
    t3: float,
) -> None:
    """Attach the three stage timings + total to decision.meta["timings"] and log one line per turn.

    Shared by respond() (NLG = a blocking complete()) and respond_stream() (NLG = the full streamed
    realize_stream, so nlg_ms here measures generation start -> last token, the real perceived NLG
    latency). Each stage is dominated by one LLM round-trip so the stage time IS that call's latency.
    `model` is best-effort (real OpenRouterClient exposes .model; the test MockLLMClient does not)."""
    dst_ms = round((t1 - t0) * 1000.0, 1)
    policy_ms = round((t2 - t1) * 1000.0, 1)
    nlg_ms = round((t3 - t2) * 1000.0, 1)
    total_ms = round((t3 - t0) * 1000.0, 1)
    # `model` is the policy+NLG (agent default) model; the DST call may ride a cheaper one via DST_MODEL
    # (mixed-model setup), so report it separately — otherwise the log misattributes the DST latency.
    agent_model = getattr(llm_client, "model", None)
    dst_call_model = dst.dst_model() or agent_model
    decision.meta["timings"] = {
        "dst_ms": dst_ms,
        "policy_ms": policy_ms,
        "nlg_ms": nlg_ms,
        "total_ms": total_ms,
        "model": agent_model,
        "dst_model": dst_call_model,
    }
    _timing_log.info(
        "turn=%s total=%.1fms dst=%.1fms[%s] policy=%.1fms nlg=%.1fms model=%s act=%s",
        new_belief.turn_count,
        total_ms,
        dst_ms,
        dst_call_model,
        policy_ms,
        nlg_ms,
        agent_model,
        decision.act,
    )


async def respond(
    belief: BeliefState,
    history: Sequence[Any],
    user_utterance: str,
    llm_client: LLMClient,
    config: AgentConfig,
    *,
    retrieved_facts: Optional[Sequence[Any]] = None,
    last_user_act: Optional[str] = None,
) -> tuple[Decision, str, BeliefState]:
    """Run one full agent turn and return (decision, reply_text, new_belief).

    Steps (the per-turn flow both channels share):
      1. DST update — fold the prospect's utterance into a NEW belief (slots + driver deltas +
         derived trends + meta). The input `belief` is not mutated.
      2. Policy decide — the LLM proposes a structured act; the deterministic gates filter/override
         it (skip-known, must-clear-objection, trust-gated price, pushiness cap, escalation). The
         returned Decision is the GATED one. Note gates may set new_belief.escalation_imminent.
      3. NLG realize — phrase the gated decision into a persona-consistent, voice-shaped reply,
         grounded in retrieved_facts when provided (U5).

    retrieved_facts is the KB grounding the voice/text adapters retrieve concurrently (U5); it is
    threaded straight to NLG. last_user_act, when the adapter has an NLU label, is recorded on the
    new belief so the gates (e.g. price_gate's 'asked unprompted', escalation's human-request) see
    it. NO LiveKit types appear in or cross this signature.
    """
    decision, new_belief, t0, t1, t2 = await _decide_turn(
        belief, history, user_utterance, llm_client, config,
        retrieved_facts=retrieved_facts, last_user_act=last_user_act,
    )

    own_lines = _own_last_lines(history)  # CB-48: no-restate context (own lines only)
    reply_text = await realize(
        decision, new_belief, config, llm_client,
        retrieved_facts=retrieved_facts,
        own_last_lines=own_lines,
    )
    # SAFEGUARD 2: on the BLOCKING path enforce grounding for a CLAIM turn — regenerate once on an
    # ungrounded reply (a fabricated figure/outcome), then fall back to a safe honest line. A grounded
    # reply or a non-claim (bare ask/confirm/escalate) passes through with no extra LLM call. The
    # streaming path cannot un-speak a token, so it does NOT get this regenerate backstop (see below).
    reply_text = await _ground_blocking_reply(
        reply_text, decision, new_belief, config, llm_client, retrieved_facts, own_lines,
    )
    t3 = time.perf_counter()

    _stamp_timings(decision, new_belief, llm_client, t0=t0, t1=t1, t2=t2, t3=t3)
    return decision, reply_text, new_belief


async def respond_stream(
    belief: BeliefState,
    history: Sequence[Any],
    user_utterance: str,
    llm_client: LLMClient,
    config: AgentConfig,
    *,
    retrieved_facts: Optional[Sequence[Any]] = None,
    last_user_act: Optional[str] = None,
    result: Optional[StreamResult] = None,
) -> AsyncIterator[str]:
    """STREAM one full agent turn: run DST -> gated Policy, then yield the NLG reply token-by-token.

    CB-41 — the streaming entry point the voice worker consumes so TTS speaks the first clause at NLG
    first-token instead of after the whole reply. The DST + gated Policy + bookkeeping run through the
    SHARED _decide_turn() prelude, so the gated decision and new_belief are IDENTICAL to what respond()
    would produce (the gate logic is NOT re-implemented here). NLG is then streamed via
    nlg.realize_stream and each token is yielded straight to the caller (the worker forwards it to TTS
    and the live monitor).

    The final (decision, reply, new_belief) is exposed on `result` (a StreamResult the caller passes
    in) AFTER the stream completes — an async generator cannot return a tuple alongside yielding. The
    worker reads result.decision/reply/new_belief to commit the SAME reliable state (CB-22) the
    blocking path commits. R37 PARITY: result.reply == ''.join(every yielded token) — no word is
    re-decided; the brain decided the content, streaming only changes WHEN each token surfaces.
    """
    holder = result if result is not None else StreamResult()
    decision, new_belief, t0, t1, t2 = await _decide_turn(
        belief, history, user_utterance, llm_client, config,
        retrieved_facts=retrieved_facts, last_user_act=last_user_act,
    )
    holder.decision = decision
    holder.new_belief = new_belief

    # CB-48: the SAME own-line context the blocking path threads (R37 parity — respond + respond_stream
    # feed NLG identical YOUR_LAST_LINES, so the streamed prompt cannot drift the no-restate guidance).
    own_lines = _own_last_lines(history)
    parts: list[str] = []
    async for token in realize_stream(
        decision, new_belief, config, llm_client,
        retrieved_facts=retrieved_facts, own_last_lines=own_lines,
    ):
        parts.append(token)
        yield token
    t3 = time.perf_counter()

    # Assemble the final reply == concat of the yielded tokens (the parity invariant) and finalize the
    # holder so the caller can commit. Then stamp the same stage timings respond() does.
    holder.tokens = parts
    holder.reply = "".join(parts)
    holder.ready = True
    # SAFEGUARD 2 (streaming asymmetry): a streamed token CANNOT be un-spoken, so we do NOT regenerate
    # mid-stream — the hardened first-line grounding rule in the prompt is the constraint here. We only
    # FLAG an ungrounded claim reply on decision.meta["grounding"] for observability (so a downstream
    # monitor/grader can surface it). The blocking respond() path keeps the regenerate-on-ungrounded
    # backstop; R37 parity holds — both paths run the SAME brain, only the blocking path can rewrite.
    if decision.act in _CLAIM_ACTS:
        try:
            report = retriever.grounded(holder.reply, list(retrieved_facts or []))
            decision.meta["grounding"] = {
                "checked": True,
                "grounded": bool(report.grounded),
                "action": "stream_flag_only",
                "unsupported": (
                    list(report.unsupported_numbers) + list(report.unsupported_terms)
                    if not report.grounded else []
                ),
            }
        except Exception:
            pass  # observability only — never affect the streamed turn
    _stamp_timings(decision, new_belief, llm_client, t0=t0, t1=t1, t2=t2, t3=t3)

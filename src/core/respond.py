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
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Sequence

from src.config.settings import AgentConfig
from src.core import dst
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient
from src.core.nlg import realize, realize_stream
from src.core.policy import Decision, decide

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

    reply_text = await realize(
        decision, new_belief, config, llm_client,
        retrieved_facts=retrieved_facts,
        own_last_lines=_own_last_lines(history),  # CB-48: no-restate context (own lines only)
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
    _stamp_timings(decision, new_belief, llm_client, t0=t0, t1=t1, t2=t2, t3=t3)

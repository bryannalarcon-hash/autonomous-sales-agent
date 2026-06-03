# The transport-agnostic brain core (plan KTD2 / U4 — THE load-bearing boundary). respond() ties
# the whole brain together for ONE turn and is the single entry point both the headless text self-
# play (loop/) and the live LiveKit voice adapter (voice/) call — so what is optimized in text is
# exactly what ships in voice. Orchestration: dst.update(belief, last_agent_act, user_utterance) ->
# policy.decide(...) [LLM proposes; deterministic gates already applied inside] -> nlg.realize(...).
# Returns (Decision, reply_text, new_belief): the Decision for logging/decision-trace, the realized
# reply, and the NEW BeliefState (the input is never mutated). NO LiveKit types cross this boundary.
# TIMING: each of the three stages is wrapped in time.perf_counter() and the breakdown (dst/policy/nlg
# + total) is attached to decision.meta["timings"] and logged once per turn on the "brain.timing"
# logger — so per-turn AND per-model-call latency is measurable on both the text and voice paths.
# CB-28: on a TOOL-USE act (answer_via_kb) grounded in retrieved_facts, the grounding facts are also
# stamped on decision.meta["retrieved"] (JSON-able strings) so the runtime can persist them onto
# Turn.retrieved and Call Review can show WHAT information was pulled for that answer.
from __future__ import annotations

import logging
import time
from typing import Any, Optional, Sequence

from src.config.settings import AgentConfig
from src.core import dst
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient
from src.core.nlg import realize
from src.core.policy import Decision, decide

# One structured line per agent turn with the three model-call stage timings + total. Each stage is
# dominated by exactly ONE LLM round-trip (the slot/gate/trend work between calls is sub-millisecond),
# so a stage's time IS that model call's latency. Filter/route via logging.getLogger("brain.timing").
_timing_log = logging.getLogger("brain.timing")

# CB-28: acts that GROUND their reply in retrieved KB facts (tool-use). Only these capture the
# retrieved payload onto decision.meta["retrieved"] for Call Review's tool-use inspection panel.
_TOOL_ACTS = frozenset({"answer_via_kb"})


def _last_agent_act(history: Optional[Sequence[Any]]) -> Optional[str]:
    """The most recent agent act in history — fed to the DST so driver deltas can condition on it."""
    if not history:
        return None
    for turn in reversed(list(history)):
        if isinstance(turn, dict) and turn.get("role") in ("assistant", "agent"):
            act = turn.get("act")
            return str(act) if act is not None else None
    return None


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

    reply_text = await realize(
        decision, new_belief, config, llm_client, retrieved_facts=retrieved_facts
    )
    t3 = time.perf_counter()

    # Stage timings == per-model-call latency (one LLM round-trip dominates each stage). Attach to the
    # decision so callers persist the total onto Turn.latency_ms, and log one line per turn. `model` is
    # best-effort (real OpenRouterClient exposes .model; the test MockLLMClient does not -> None).
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

    return decision, reply_text, new_belief

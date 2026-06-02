# U14 (A) — graceful async escalation deferral + operator manual takeover (plan R10). The policy's
# deterministic gates (src/core/gates.escalation_triggers) already DETECT the EXTREME moment and emit
# act="escalate"; this module HANDLES that decision at runtime WITHOUT any live human:
#   handle_escalation(decision, belief, ...) -> (1) an IN-PERSONA, deterministic deferral utterance
#     ("a specialist will follow up", warm, persona-named — no LLM call on this path, so no dead air
#     and no failure mode), (2) a SECURED next-best step (a structured NextStep follow-up/callback
#     capture), (3) an EscalationLog(lifecycle="unreviewed") persisted to the REVIEW QUEUE via the
#     store (or an injected store_hook in tests). Async deferral, reviewed later (R10) — NO live human.
# Operator manual takeover: snapshot_for_takeover(session) captures a plain, INDEPENDENT TakeoverState
#   (belief/history/turns/version copied deep) so control transfers to a human WITH STATE INTACT, and
#   mutating the live session afterward never corrupts the snapshot; restore_from_takeover(session,
#   state) resumes exactly there. The snapshot is plain data (rides on session.userdata for LiveKit)
#   so it is testable without livekit. NO livekit import here. Collaborators: src.core.belief_state,
#   src.memory.schema.EscalationLog, src.memory.store (lazily), src.config.settings, src.voice.session.
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.memory.schema import EscalationLog, Turn

# A persistence hook: (EscalationLog) -> awaitable. Injected in tests so the queue write is captured
# without a DB; defaults at runtime to store.save_escalation (the real review-queue write side).
StoreHook = Callable[[EscalationLog], Awaitable[Any]]

# In-persona deferral templates per matched reason — warm, low-pressure, persona-named (R3/R10). The
# template is filled deterministically (NO LLM at runtime) so the deferral can never fail / stall:
# {name} is the agent persona name; the line always names a specialist follow-up + the secured step.
_DEFERRAL_TEMPLATES: dict[str, str] = {
    "concession": (
        "That's a great question about pricing, and I want to get you the right answer — "
        "I'm {name}, and I'll have a specialist follow up with the exact options shortly. "
        "Can I grab the best number and time to reach you?"
    ),
    "human_request": (
        "Absolutely — this is {name}, and I'll connect you with a specialist who can take it from "
        "here. They'll follow up with you directly; what's the best number and time to reach you?"
    ),
    "compliance": (
        "I want to make sure you get an accurate, careful answer on that — I'm {name}, and a "
        "specialist will follow up to go over it properly. What's the best number and time for them "
        "to reach you?"
    ),
    "low_confidence": (
        "I want to make sure I give you the right guidance here — this is {name}, and I'll have a "
        "specialist follow up so we get it exactly right. What's the best number and time to reach you?"
    ),
}
_DEFAULT_DEFERRAL = (
    "I want to make sure you're taken care of properly — I'm {name}, and a specialist will follow up "
    "with you shortly. What's the best number and time to reach you?"
)

# Reason category -> the next-best step we secure (the structured follow-up the agent captures so the
# call ends on a concrete commitment, not dead air). A human request / compliance still secures a
# callback; a pricing question secures a specialist follow-up.
_NEXT_STEP_INTENT: dict[str, str] = {
    "concession": "specialist_pricing_followup",
    "human_request": "specialist_callback",
    "compliance": "specialist_followup",
    "low_confidence": "specialist_followup",
}


@dataclass
class NextStep:
    """The SECURED next-best step captured at deferral (so an EXTREME moment still ends with a concrete
    follow-up, not dead air). `intent` is the structured follow-up kind (callback / specialist
    follow-up); `contact_hint`/`when` carry an optional captured number/time the operator uses later."""

    intent: str
    contact_hint: Optional[str] = None
    when: Optional[str] = None
    note: str = ""


@dataclass
class EscalationOutcome:
    """The result of handling an escalate decision (plan R10): the in-persona deferral utterance the
    agent speaks, the secured next_step, and the EscalationLog written to the review queue."""

    deferral_text: str
    next_step: NextStep
    escalation_log: EscalationLog


def _reason_category(decision: Any, belief: BeliefState, config: AgentConfig) -> str:
    """Classify the EXTREME moment into a template/next-step category, mirroring gates' triggers.

    Priority matches gates._escalation_reason: an over-band concession first, then explicit human
    request, then compliance, then a low-confidence streak. Falls back by scanning the gate rationale
    so a decision that only carries the gate's one-line reason still classifies correctly.
    """
    # 1. Pricing concession beyond the allowed band.
    if decision.concession is not None:
        try:
            band = config.thresholds.get("max_concession_band", 0.15)
            if float(decision.concession) > float(band):
                return "concession"
        except (TypeError, ValueError):
            pass
    # 2/3. Explicit human request / compliance territory (read off the belief's last user act).
    if belief.last_user_act == "human_request":
        return "human_request"
    if belief.last_user_act == "compliance":
        return "compliance"
    # Fall back to the gate's rationale text (the decision may only carry the one-line reason).
    rationale = (getattr(decision, "rationale", "") or "").lower()
    if "concession" in rationale:
        return "concession"
    if "human request" in rationale:
        return "human_request"
    if "compliance" in rationale:
        return "compliance"
    if "low-confidence" in rationale or "low confidence" in rationale:
        return "low_confidence"
    return "human_request"  # safe default: route to a specialist callback


_REASON_LABEL: dict[str, str] = {
    "concession": "pricing concession beyond allowed band",
    "human_request": "explicit human request",
    "compliance": "compliance territory",
    "low_confidence": "consecutive low-confidence turns",
}


def _deferral_text(category: str, config: AgentConfig) -> str:
    """Deterministically realize the in-persona deferral (NO LLM at runtime). Persona-named so it is
    consistent with the agent's voice; warm + low-pressure; always secures a follow-up (no dead air)."""
    name = getattr(getattr(config, "persona", None), "name", None) or "your advisor"
    template = _DEFERRAL_TEMPLATES.get(category, _DEFAULT_DEFERRAL)
    return template.format(name=name)


async def handle_escalation(
    decision: Any,
    belief: BeliefState,
    *,
    episode_id: str,
    turn_id: int,
    config: AgentConfig,
    store_hook: Optional[StoreHook] = None,
    moment: Optional[str] = None,
) -> EscalationOutcome:
    """Handle a gated `escalate` decision at runtime (plan R10) — graceful ASYNC deferral, NO live human.

    Steps (all pure/deterministic — there is NO llm_client argument, so the brain never runs and no
    human is phoned on this path):
      1. Classify the EXTREME moment (concession / human-request / compliance / low-confidence).
      2. Produce an IN-PERSONA deferral utterance ("a specialist will follow up…", warm, persona-named).
      3. Secure a structured NEXT-BEST step (a callback / specialist follow-up capture).
      4. Build an EscalationLog(reason from the matched trigger, moment=the quote/turn, turn_id,
         lifecycle="unreviewed") and PERSIST it to the review queue via `store_hook` (tests) or the
         default store.save_escalation (runtime). The escalation is reviewed later, not live.

    `moment` is the offending quote/turn to record on the log (the dashboard P5 shows it); if blank it
    is backfilled (deferral line → reason) so the queue NEVER holds a contentless row (CB-18). Returns
    an EscalationOutcome carrying the three deliverables.
    """
    category = _reason_category(decision, belief, config)
    deferral_text = _deferral_text(category, config)

    reason = _REASON_LABEL.get(category, "extreme moment")
    # Prefer the gate's own one-line rationale when present (it is the auditable trigger string).
    rationale = getattr(decision, "rationale", "") or ""
    if rationale:
        reason = rationale

    next_step = NextStep(
        intent=_NEXT_STEP_INTENT.get(category, "specialist_callback"),
        note="captured at async deferral; specialist follows up (no live human required)",
    )

    # CB-18: NEVER persist a contentless escalation — an empty/blank `moment` renders as a blank,
    # useless review-queue row (the "empty escalations" the operator kept seeing). When no trigger
    # quote was captured, fall back to the in-persona deferral line the agent actually spoke (and, as
    # a last resort, the human reason) so every queued escalation has meaningful, reviewable content.
    clean_moment = (moment or "").strip() or (deferral_text or "").strip() or f"Escalation: {reason}"

    log = EscalationLog(
        escalation_id=f"esc-{uuid.uuid4().hex}",
        episode_id=episode_id,
        reason=reason,
        moment=clean_moment,
        turn_id=turn_id,
        lifecycle="unreviewed",
    )

    # Persist to the REVIEW QUEUE. Default to the real store lazily (keeps schema-only/DB-free imports
    # from dragging in asyncpg); tests inject a store_hook to capture the write without a DB.
    if store_hook is not None:
        await store_hook(log)
    else:
        from src.memory import store

        await store.save_escalation(log)

    # Belief surfaces the imminence for the live monitor / SessionReport (mirrors the gate).
    belief.escalation_imminent = True

    return EscalationOutcome(deferral_text=deferral_text, next_step=next_step, escalation_log=log)


# =============================== Operator manual takeover ========================================


@dataclass
class TakeoverState:
    """A plain, INDEPENDENT snapshot of a VoiceSession's per-call state for operator takeover (R10).

    Control transfers to a human WITH STATE INTACT: the belief (slots/drivers/meta), the selfplay-
    shaped history, the captured turns, the version/kb_version stamps, and the next-turn id cursor are
    all carried so the operator resumes exactly where the agent was. Plain data so it rides on
    session.userdata for a LiveKit handoff and stays testable without livekit. Copied DEEP at snapshot
    time so mutating the live session afterward cannot corrupt this snapshot."""

    belief: BeliefState
    history: list[dict[str, Any]] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    version: str = ""
    kb_version: str = ""
    speech_id_cursor: int = 0


def snapshot_for_takeover(session: Any) -> TakeoverState:
    """Capture the live session's committed state into an INDEPENDENT TakeoverState (operator takeover).

    Deep-copies the belief, history, and captured turns so the snapshot does NOT alias the live
    session's objects — a subsequent committed turn (or an in-place mutation) on the live session
    leaves this snapshot unchanged. version/kb_version + the next-turn id cursor travel so the operator
    arm resumes with the SAME attribution and turn numbering.
    """
    state = session.state
    return TakeoverState(
        belief=state.belief.copy(),  # BeliefState.copy duplicates slots/drivers/trends/meta
        history=copy.deepcopy(list(state.history)),
        turns=copy.deepcopy(list(state.turns)),
        version=state.version,
        kb_version=state.kb_version,
        speech_id_cursor=int(getattr(session, "_next_turn_id", len(state.turns))),
    )


def restore_from_takeover(session: Any, state: TakeoverState) -> None:
    """Restore a TakeoverState onto a session so the operator (or the agent) resumes WITH STATE INTACT.

    Copies the snapshot back into the target session's committed state (belief/history/turns +
    version/kb_version) and resets the next-turn id cursor, so the handed-off control continues exactly
    where the snapshot was taken. Defensive copies again so the restored session and the snapshot stay
    independent.
    """
    session.state.belief = state.belief.copy()
    session.state.committed = session.state.belief
    session.state.history = copy.deepcopy(list(state.history))
    session.state.turns = copy.deepcopy(list(state.turns))
    session.state.version = state.version
    session.state.kb_version = state.kb_version
    session._next_turn_id = int(state.speech_id_cursor)

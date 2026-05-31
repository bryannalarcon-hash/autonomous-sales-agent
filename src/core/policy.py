# Gated neuro-symbolic policy (plan U4; R5/R7/R29/R35): the LLM PROPOSES a structured next-act
# decision, then the deterministic gates (src/core/gates.py) FILTER/OVERRIDE it — gates have final
# say. Defines the Decision dataclass (the policy<->NLG act interface, CraigslistBargain-style:
# strategy decoupled from generation) and an async decide(belief, history, config, llm_client).
# Acts: confirm_known | ask(slot) | answer_via_kb | pitch | handle_objection | attempt_close(tier)
# | escalate | disqualify. Encodes SPIN-grounded discovery sequencing (budget asked late, trust-
# gated) and the commitment ladder (enrollment > trial > consultation > callback) for close tiers.
# Pure + async, NO LiveKit imports — both text self-play and the voice adapter call this.
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.core.gates import apply_gates
from src.core.llm import LLMClient, Message

# The act vocabulary the policy chooses among (the policy<->NLG interface). Anything the LLM emits
# outside this set is treated as malformed and falls back to a safe non-pressure act.
ACTS: tuple[str, ...] = (
    "confirm_known",
    "ask",
    "answer_via_kb",
    "pitch",
    "handle_objection",
    "attempt_close",
    "escalate",
    "disqualify",
)

# Commitment ladder, strongest -> weakest (R29). attempt_close carries one of these as `tier`.
LADDER_TIERS: tuple[str, ...] = ("enrollment", "trial", "consultation", "callback")

# A safe default act when the LLM proposal is unusable: never silently pressure/close.
_SAFE_FALLBACK_ACT = "pitch"


@dataclass
class Decision:
    """The structured next-act decision (the policy<->NLG interface) — what gates filter and NLG
    realizes. `target_slot` is the slot for `ask`/`answer_via_kb`; `tier` is the commitment-ladder
    rung for `attempt_close`; `concession` is an optional proposed discount fraction the price/
    escalation gates inspect; `confidence` feeds the low-confidence escalation streak. `rationale`
    is the one-line decision-log line (docs/belief-state-schema.md)."""

    act: str
    target_slot: Optional[str] = None
    tier: Optional[str] = None
    rationale: str = ""
    confidence: Optional[float] = None
    # Proposed price concession as a fraction (0.30 == 30% off). The price/escalation gates read it.
    concession: Optional[float] = None
    # Free-form metadata (e.g. a KB query hint) that downstream units may attach; never gated on.
    meta: dict[str, Any] = field(default_factory=dict)

    def copy(self) -> "Decision":
        return Decision(
            act=self.act,
            target_slot=self.target_slot,
            tier=self.tier,
            rationale=self.rationale,
            confidence=self.confidence,
            concession=self.concession,
            meta=dict(self.meta),
        )


# --- LLM proposal -----------------------------------------------------------------------------

_POLICY_RUBRIC = (
    "You are the POLICY for a warm, low-pressure tutoring sales advisor (Nerdy / Varsity Tutors). "
    "Given the agent's belief state, choose the single best NEXT system act. Return ONLY a JSON "
    "object: {\"act\": <one of "
    "confirm_known|ask|answer_via_kb|pitch|handle_objection|attempt_close|escalate|disqualify>, "
    "\"target_slot\": <slot name or null>, "
    "\"tier\": <enrollment|trial|consultation|callback or null, only for attempt_close>, "
    "\"concession\": <discount fraction 0..1 or null, only if proposing a price concession>, "
    "\"confidence\": <0..1 how sure you are>, "
    "\"rationale\": <one short sentence>}.\n"
    "SPIN-grounded discovery: gather grade/subject/goal/timeline BEFORE budget; budget is asked "
    "LATE and only once trust is established. Commitment ladder (strongest first): enrollment > "
    "trial > consultation > callback — match the rung to readiness (hot -> enrollment; warm -> "
    "trial/consultation; cool -> callback). Never invent facts; use answer_via_kb for factual "
    "questions. If they are genuinely unqualified, disqualify gracefully — do NOT force a close."
)


def _summarize_belief(belief: BeliefState, config: AgentConfig) -> dict[str, Any]:
    """A compact, JSON-able view of the belief state for the policy prompt (the state feature vector)."""
    known_slots = {
        name: slot.get("value")
        for name, slot in belief.slots.items()
        if slot.get("value") is not None
    }
    return {
        "stage": belief.stage,
        "known_slots": known_slots,
        "drivers": {k: round(float(v), 3) for k, v in belief.drivers.items()},
        "trends": {k: round(float(v), 3) for k, v in belief.trends.items()},
        "active_objection": belief.active_objection,
        "last_user_act": belief.last_user_act,
        "open_question": belief.open_question,
        "turn_count": belief.turn_count,
        "discovery_sequence": config.playbooks.get("discovery_sequence", []),
    }


def _build_policy_messages(
    belief: BeliefState, history: Sequence[Any], config: AgentConfig
) -> list[Message]:
    """Assemble the rubric-driven chat messages for the policy proposal call."""
    system = config.prompts.get("policy") or ""
    system = f"{_POLICY_RUBRIC}\n\n{system}".strip()
    user = (
        f"BELIEF_STATE: {json.dumps(_summarize_belief(belief, config))}\n"
        f"RECENT_TURNS: {json.dumps(_recent_history(history))}\n\n"
        "Return the decision JSON now."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _recent_history(history: Sequence[Any], n: int = 6) -> list[dict[str, Any]]:
    """Project the last n turns to small JSON dicts (role/act/content) for the prompt."""
    out: list[dict[str, Any]] = []
    for turn in list(history)[-n:]:
        if isinstance(turn, dict):
            out.append(
                {
                    "role": turn.get("role"),
                    "act": turn.get("act"),
                    "content": str(turn.get("content", ""))[:200],
                }
            )
    return out


def _parse_proposal(raw: Any) -> Decision:
    """Coerce an LLM proposal (dict already parsed by complete_json) into a safe Decision.

    Robustness contract: any malformed / out-of-vocabulary proposal degrades to a safe non-pressure
    fallback act rather than crashing or silently closing. The gates still run on the result.
    """
    if not isinstance(raw, dict):
        return Decision(act=_SAFE_FALLBACK_ACT, rationale="malformed policy proposal; safe fallback")
    act = raw.get("act")
    if act not in ACTS:
        return Decision(act=_SAFE_FALLBACK_ACT, rationale="unknown act proposed; safe fallback")

    tier = raw.get("tier")
    if act == "attempt_close":
        tier = tier if tier in LADDER_TIERS else "consultation"  # default to a low-pressure rung
    else:
        tier = None

    concession = _as_float(raw.get("concession"))
    confidence = _as_float(raw.get("confidence"))
    return Decision(
        act=act,
        target_slot=raw.get("target_slot") or None,
        tier=tier,
        rationale=str(raw.get("rationale", "")),
        confidence=confidence,
        concession=concession,
    )


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def decide(
    belief: BeliefState,
    history: Sequence[Any],
    config: AgentConfig,
    llm_client: LLMClient,
) -> Decision:
    """Neuro-symbolic decision: LLM proposes -> deterministic gates decide (gates have final say).

    The LLM is asked for a structured proposal (complete_json). The proposal is parsed defensively,
    then run through apply_gates(...) which enforces skip-known, must-clear-objection-before-close,
    the trust-gated price gate, the pushiness cap, and the EXTREME escalation triggers. The returned
    Decision is the GATED one — never the raw LLM proposal — so safety/correctness is deterministic.
    """
    messages = _build_policy_messages(belief, history, config)
    try:
        raw = await llm_client.complete_json(messages)
    except ValueError:
        raw = None  # malformed JSON -> safe fallback below
    proposed = _parse_proposal(raw)
    return apply_gates(proposed, belief, config, history=history)

# Deterministic safety/correctness gates over the BeliefState (plan U4; R5/R7/R29). Pure functions:
# each takes a PROPOSED Decision (from src/core/policy.py), the belief, and the AgentConfig, and
# returns the ALLOWED/OVERRIDDEN Decision — gates have FINAL say over the LLM proposal (neuro-
# symbolic: LLM proposes, gates decide). apply_gates() chains them in priority order.
#   skip_known          — never ask a slot already filled at/above lock confidence.
#   address_direct_input— a live objection or a direct question (from the DST intent classifier)
#                         pre-empts a discovery/pitch act (-> handle_objection / answer_via_kb) so the
#                         agent never ignores what the prospect just asked to keep doing discovery.
#   must_clear_objection— forbid pivot-to-close while active_objection is set (-> handle_objection).
#   price_gate          — don't open price/budget before a trust event (prospect asked unprompted
#                         OR a value-ack/trust threshold OR required discovery slots filled).
#   pushiness_cap       — forbid further pressure when a pushiness signal (bail_risk) or repeated-
#                         pressure count exceeds the configured cap.
#   escalation_triggers — EXTREME only: pricing concession beyond band, explicit human request,
#                         compliance territory, or N consecutive low-confidence turns -> escalate.
# Thresholds are read from AgentConfig.thresholds. NO LiveKit imports; no construction of Decision
# (gates copy + mutate the incoming Decision) so there is no import cycle with policy.py.
from __future__ import annotations

from typing import Any, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState

# Slots that are price/budget-related — the price_gate guards opening these.
_PRICE_SLOTS = frozenset({"budget"})
# Acts that apply commercial pressure — the pushiness cap guards repeating these.
_PRESSURE_ACTS = frozenset({"attempt_close", "pitch"})
# Lock confidence above which a slot is "known" (mirrors the DST's _LOCK_CONFIDENCE).
_LOCK_CONFIDENCE = 0.8

# Threshold defaults so champion_v0.yaml (which only ships a subset) still drives every gate. Each
# is overridable in AgentConfig.thresholds; these are cold-start priors pending sim calibration.
_DEFAULTS: dict[str, float] = {
    "trust_gate_open_price": 0.5,
    "pushiness_cap": 0.8,
    "pushiness_pressure_count_cap": 2,
    "escalate_low_confidence_turns": 2,
    "low_confidence_level": 0.4,
    "max_concession_band": 0.15,
    "discovery_slots_required": 4,
}


def _threshold(config: AgentConfig, name: str) -> float:
    """Read a gate threshold from config, falling back to the cold-start default."""
    value = config.thresholds.get(name)
    return float(value) if value is not None else float(_DEFAULTS[name])


def _required_discovery_slots(config: AgentConfig) -> list[str]:
    """The non-budget discovery slots that, once filled, count as 'discovery done' for the price gate."""
    seq = config.playbooks.get("discovery_sequence") or []
    return [s for s in seq if s not in _PRICE_SLOTS]


# --- skip_known -------------------------------------------------------------------------------

def skip_known(decision: Any, belief: BeliefState, config: AgentConfig) -> Any:
    """Never ask a slot already filled at/above lock confidence; confirm it instead.

    If the proposal is `ask` for a locked slot, override to `confirm_known` (we already have it) so
    discovery does not waste a turn re-asking the prospect for what we know.
    """
    if decision.act != "ask" or not decision.target_slot:
        return decision
    if belief.slot_confidence(decision.target_slot) >= _LOCK_CONFIDENCE:
        out = decision.copy()
        out.act = "confirm_known"
        out.rationale = f"skip_known: {decision.target_slot} already known; confirming not re-asking"
        return out
    return decision


# --- address_direct_input ---------------------------------------------------------------------

# Discovery/pitch acts that must yield to an unanswered question or a live objection (R35 discovery
# is good, but never at the cost of ignoring what the prospect just said).
_DEFERRABLE_ACTS = frozenset({"ask", "confirm_known", "pitch"})


def address_direct_input(decision: Any, belief: BeliefState, config: AgentConfig) -> Any:
    """Ensure a direct question or objection is ADDRESSED this turn — not ignored to keep doing
    discovery. This is the fix for the agent looping "what grade?" at a prospect who asked "how much?"
    or objected "why not Khan Academy?": the DST now classifies the utterance (active_objection /
    open_question / price_inquiry), and this gate routes a discovery/pitch proposal accordingly —
    handle_objection when an objection is live, answer_via_kb when they asked a question. Acts that
    already address the prospect (answer_via_kb, handle_objection) or advance/close/escalate/disqualify
    are left to the close-path + escalation gates."""
    if decision.act not in _DEFERRABLE_ACTS:
        return decision
    if belief.active_objection:
        out = decision.copy()
        out.act = "handle_objection"
        out.target_slot = None
        out.tier = None
        out.rationale = (
            f"address_direct_input: prospect raised '{belief.active_objection}'; "
            "handling it before any more discovery"
        )
        return out
    if belief.open_question and belief.last_user_act in ("question", "price_inquiry"):
        out = decision.copy()
        out.act = "answer_via_kb"
        out.tier = None
        out.rationale = "address_direct_input: prospect asked a direct question; answering before more discovery"
        return out
    return decision


# --- must_clear_objection ---------------------------------------------------------------------

def must_clear_objection(decision: Any, belief: BeliefState, config: AgentConfig) -> Any:
    """Forbid pivot-to-close while an objection is open; route to handle_objection first.

    You cannot advance to close with an unresolved objection on the table (docs/belief-state-schema
    Layer C). An `attempt_close` proposed against an open objection is overridden.
    """
    if decision.act != "attempt_close":
        return decision
    if not belief.active_objection:
        return decision
    out = decision.copy()
    out.act = "handle_objection"
    out.tier = None
    out.rationale = (
        f"must_clear_objection: '{belief.active_objection}' open; "
        "handling it before any close attempt"
    )
    return out


# --- price_gate -------------------------------------------------------------------------------

def _opens_price(decision: Any) -> bool:
    """True if the proposed act would open price/budget talk (ask/answer/confirm a price slot)."""
    return decision.act in ("ask", "answer_via_kb", "confirm_known") and (
        decision.target_slot in _PRICE_SLOTS
    )


def _price_gate_open(belief: BeliefState, config: AgentConfig) -> bool:
    """The trust-event predicate: prospect asked price unprompted OR a value-ack (trust at/above the
    open-price threshold) OR the required discovery slots are filled."""
    # 1. Prospect asked about price/cost unprompted -> we may answer it.
    asked_price = belief.last_user_act == "price_inquiry" or (
        bool(belief.open_question) and "price" in str(belief.open_question).lower()
    )
    if asked_price:
        return True
    # 2. A value-acknowledgment has landed -> trust is at/above the open-price threshold.
    if float(belief.drivers.get("trust", 0.0)) >= _threshold(config, "trust_gate_open_price"):
        return True
    # 3. Required discovery slots filled (we've earned the right to talk packaging).
    required = _required_discovery_slots(config)
    need = int(_threshold(config, "discovery_slots_required"))
    filled = sum(1 for s in required if belief.slot_confidence(s) >= _LOCK_CONFIDENCE)
    if need > 0 and filled >= need:
        return True
    return False


def price_gate(decision: Any, belief: BeliefState, config: AgentConfig) -> Any:
    """Don't open price/budget before a trust event; re-anchor on value (pitch) instead (AE2).

    Only touches acts that open price/budget. If the gate is closed, the proposal is overridden to a
    value pitch — the agent re-anchors value rather than quoting or asking budget prematurely.
    """
    if not _opens_price(decision):
        return decision
    if _price_gate_open(belief, config):
        return decision
    out = decision.copy()
    out.act = "pitch"
    out.target_slot = None
    out.tier = None
    out.rationale = "price_gate: trust not yet established; re-anchoring on value before any price talk"
    return out


# --- pushiness_cap ----------------------------------------------------------------------------

def _consecutive_pressure_count(history: Optional[Sequence[Any]]) -> int:
    """Count the trailing run of consecutive agent pressure acts in history (most-recent-first).

    Walks back over agent turns; a non-pressure agent act breaks the run. User turns are skipped so
    "close, (user), close" still counts as two consecutive pressure attempts.
    """
    if not history:
        return 0
    count = 0
    for turn in reversed(list(history)):
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        if role in ("assistant", "agent"):
            if turn.get("act") in _PRESSURE_ACTS:
                count += 1
            else:
                break  # a non-pressure agent act resets the streak
        # user/system turns are transparent to the streak
    return count


def pushiness_cap(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """Forbid further pressure when bail_risk crosses the cap OR pressure has repeated past the cap.

    Two independent triggers (either fires): a pushiness SIGNAL (bail_risk >= pushiness_cap level)
    or a repeated-pressure COUNT (consecutive pressure acts >= pushiness_pressure_count_cap). When
    tripped, a pressure act (attempt_close/pitch) is backed off to a low-pressure confirm/answer.
    """
    if decision.act not in _PRESSURE_ACTS:
        return decision

    signal_tripped = float(belief.drivers.get("bail_risk", 0.0)) >= _threshold(config, "pushiness_cap")
    count_cap = int(_threshold(config, "pushiness_pressure_count_cap"))
    count_tripped = _consecutive_pressure_count(history) >= count_cap

    if not (signal_tripped or count_tripped):
        return decision

    out = decision.copy()
    # Back off to a non-pressure act: surface their open question or simply confirm understanding.
    out.act = "answer_via_kb" if belief.open_question else "confirm_known"
    out.tier = None
    why = "bail_risk over cap" if signal_tripped else "repeated pressure over cap"
    out.rationale = f"pushiness_cap: {why}; backing off pressure to avoid pushing them to walk"
    return out


# --- escalation_triggers ----------------------------------------------------------------------

def _low_confidence_streak(
    belief: BeliefState,
    history: Optional[Sequence[Any]],
    threshold_level: float,
) -> int:
    """Length of the trailing run of low-confidence turns, counting this turn + recent agent turns.

    This turn counts if belief.decision_confidence is below the level; then we walk back over recent
    agent turns counting their logged `confidence` until one is at/above the level.
    """
    streak = 0
    if belief.decision_confidence is not None and float(belief.decision_confidence) < threshold_level:
        streak += 1
    else:
        return 0  # this turn is confident -> no active streak
    if not history:
        return streak
    for turn in reversed(list(history)):
        if not isinstance(turn, dict):
            continue
        if turn.get("role") not in ("assistant", "agent"):
            continue
        conf = turn.get("confidence")
        if conf is None:
            break  # unknown confidence breaks the run conservatively
        try:
            if float(conf) < threshold_level:
                streak += 1
            else:
                break
        except (TypeError, ValueError):
            break
    return streak


def escalation_triggers(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """EXTREME-only escalation (the final-say gate): override to `escalate` + mark belief imminent.

    Triggers (any): a proposed pricing concession beyond max_concession_band; an explicit human
    request; compliance territory; or N consecutive low-confidence turns. These are the moments the
    agent must defer rather than improvise (plan R10; gates beat the LLM proposal absolutely).
    """
    reason = _escalation_reason(decision, belief, config, history)
    if reason is None:
        return decision
    belief.escalation_imminent = True  # surface to the live monitor / SessionReport
    out = decision.copy()
    out.act = "escalate"
    out.tier = None
    out.rationale = f"escalation_triggers: {reason}; deferring to a specialist"
    return out


def _escalation_reason(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    history: Optional[Sequence[Any]],
) -> Optional[str]:
    """Return the first matched EXTREME escalation reason, or None if none apply."""
    # 1. Pricing concession beyond the allowed band.
    if decision.concession is not None:
        try:
            if float(decision.concession) > _threshold(config, "max_concession_band"):
                return "pricing concession beyond allowed band"
        except (TypeError, ValueError):
            pass
    # 2. Explicit request for a human.
    if belief.last_user_act == "human_request":
        return "explicit human request"
    # 3. Compliance territory (legal/medical/regulatory cues flagged upstream).
    if belief.last_user_act == "compliance":
        return "compliance territory"
    # 4. N consecutive low-confidence turns.
    need = int(_threshold(config, "escalate_low_confidence_turns"))
    level = _threshold(config, "low_confidence_level")
    if _low_confidence_streak(belief, history, level) >= need:
        return f"{need} consecutive low-confidence turns"
    return None


# --- the chain --------------------------------------------------------------------------------

def apply_gates(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """Run every gate in priority order and return the final ALLOWED decision (gates have final say).

    Order matters: escalation is checked FIRST and LAST — first so an EXTREME moment short-circuits
    everything, and again last so an override another gate produced still can't slip past an extreme
    trigger. Between them: skip_known -> address_direct_input -> must_clear_objection -> price_gate
    -> pushiness_cap.
    """
    # Extreme moments dominate: nothing else matters if we must defer.
    escalated = escalation_triggers(decision, belief, config, history=history)
    if escalated.act == "escalate":
        return escalated

    d = skip_known(decision, belief, config)
    d = address_direct_input(d, belief, config)  # answer/handle what they just said before discovery
    d = must_clear_objection(d, belief, config)
    d = price_gate(d, belief, config)
    d = pushiness_cap(d, belief, config, history=history)

    # Re-check escalation on the (possibly rewritten) decision so a gate-introduced concession or
    # state change can't escape the extreme-deferral guarantee.
    return escalation_triggers(d, belief, config, history=history)

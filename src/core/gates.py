# Deterministic safety/correctness gates over the BeliefState (plan U4; R5/R7/R29). Pure functions:
# each takes a PROPOSED Decision (from src/core/policy.py), the belief, and the AgentConfig, and
# returns the ALLOWED/OVERRIDDEN Decision — gates have FINAL say over the LLM proposal (neuro-
# symbolic: LLM proposes, gates decide). apply_gates() chains them in priority order.
#   skip_known          — never ask a slot already filled at/above lock confidence.
#   offer_low_commitment_on_budget— repeated FIRM budget refusals (cumulative count in belief.meta) ->
#                         offer the FREE callback rung (help + a future meeting), denying only the paid
#                         tiers, instead of looping or releasing. Runs LAST (final say).
#   address_direct_input— a live objection or a direct question (from the DST intent classifier)
#                         pre-empts a discovery/pitch act (-> handle_objection / answer_via_kb) so the
#                         agent never ignores what the prospect just asked — BUT once it has answered
#                         it lets a `pitch` ADVANCE (escapes the reactive answer-loop; never blocks close).
#   must_clear_objection— forbid pivot-to-close while active_objection is set (-> handle_objection).
#   price_gate          — don't open price/budget before a trust event (prospect asked unprompted
#                         OR a value-ack/trust threshold OR required discovery slots filled).
#   advance_to_close    — once the belief is closeable (no objection, warmed trust, enough turns),
#                         convert a reactive act into attempt_close so the agent ASKS for the sale
#                         instead of answering questions forever (the can't-close fix).
#   close_floor         — CB-29: forbid a PAID close (consultation/trial/enrollment) until real
#                         discovery (min turns + enough filled discovery slots), applied to a directly
#                         LLM-PROPOSED close too — kills the premature/pushy close. callback is exempt.
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
    # CUMULATIVE firm affordability refusals after which the agent offers the FREE callback rung
    # (help + a future meeting) instead of pushing paid tiers — the reframe gets its first 1–2 shots,
    # then we down-tier. Cumulative (not consecutive) so it's robust to the prospect rephrasing.
    "budget_refusals_before_callback": 2,
    # advance_to_close: take the initiative to close once the prospect has warmed (trust ABOVE the 0.5
    # neutral prior) and enough turns have passed — the agent was staying reactive (answering questions
    # forever) and never asking for the sale. trust is the reliable warmth signal; the trial rung also
    # needs purchase_intent. The prospect's honest buy-gate still decides acceptance.
    "close_ready_trust": 0.6,         # trust that warrants offering the low-commitment consultation
    "close_ready_trial_trust": 0.7,   # trust for the higher trial rung
    "close_ready_trial_intent": 0.55, # purchase_intent ALSO required for the trial rung
    "min_turns_before_close": 4,      # never auto-close in the opening turns
    # A budget-constrained prospect (price_sensitivity at/above this, OR firm budget refusals) gets the
    # FREE callback rung from advance_to_close instead of a paid consultation they'd reject.
    "callback_price_sensitivity": 0.75,
    # CB-29 (premature/pushy close): the MINIMUM number of non-budget discovery slots that must be
    # filled before ANY paid close (consultation/trial/enrollment) may fire — applies to a directly
    # LLM-PROPOSED close too, not just a gate-advanced one (the real call fired attempt_close at turns
    # 9 & 11 with ZERO discovery). The free 'callback' rung is exempt (a no-cost follow-up is never
    # pushy). Default 1: the agent must have learned at least one concrete thing about the learner.
    "discovery_slots_before_close": 1,
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


# --- offer_low_commitment_on_budget -----------------------------------------------------------

# The lowest, no-cost rung of the commitment ladder — a future follow-up that needs NO budget, so a
# prospect who can't afford the paid tiers still gets help + a touchpoint (only the paid tiers are
# denied them, never help itself).
_FREE_LADDER_TIER = "callback"


def offer_low_commitment_on_budget(decision: Any, belief: BeliefState, config: AgentConfig) -> Any:
    """When a prospect can't afford the higher tiers, offer the FREE rung instead of looping/releasing.

    A prospect who FIRMLY refuses on budget repeatedly (the cumulative count the DST tracks in
    belief.meta["affordability_refusal_count"]) won't buy a paid tier — but they should STILL get help
    and a future meeting (product rule: deny the higher commitment-ladder tiers, never help itself).
    So once the reframe has had its first 1–2 shots and the refusals persist, steer the agent to a
    CALLBACK — the no-cost rung (a future follow-up) — rather than acknowledging forever. Runs LAST so
    must_clear_objection/pushiness don't undo the low-pressure offer; never overrides an escalate or an
    already low-tier (callback/consultation) close. The buy-gate still decides if they take it."""
    if decision.act == "escalate":
        return decision
    if decision.act == "attempt_close" and decision.tier in ("callback", "consultation"):
        return decision  # already offering a low/no-cost rung
    refusals = int(belief.meta.get("affordability_refusal_count", 0))
    if refusals < int(_threshold(config, "budget_refusals_before_callback")):
        return decision
    out = decision.copy()
    out.act = "attempt_close"
    out.tier = _FREE_LADDER_TIER
    out.target_slot = None
    out.concession = None
    out.rationale = (
        f"offer_low_commitment_on_budget: {refusals} firm budget refusals — paid tiers are off the "
        "table, so offering a free future follow-up (callback) rather than pushing or releasing"
    )
    return out


# --- address_direct_input ---------------------------------------------------------------------

# Discovery/pitch acts that must yield to an unanswered question or a live objection (R35 discovery
# is good, but never at the cost of ignoring what the prospect just said).
_DEFERRABLE_ACTS = frozenset({"ask", "confirm_known", "pitch"})


def _prev_agent_act(history: Optional[Sequence[Any]]) -> Optional[str]:
    """The most recent agent act in history (most-recent-first), or None — used to detect that the
    agent JUST answered, so it can advance instead of answering again."""
    if not history:
        return None
    for turn in reversed(list(history)):
        if isinstance(turn, dict) and turn.get("role") in ("assistant", "agent"):
            act = turn.get("act")
            return str(act) if act is not None else None
    return None


def address_direct_input(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """Ensure a direct question or objection is ADDRESSED this turn — not ignored to keep doing
    discovery — WITHOUT trapping the agent in an endless answer-loop.

    The DST classifies the utterance (active_objection / open_question / price_inquiry); this gate
    routes a discovery/pitch proposal: handle_objection when an objection is live, answer_via_kb when
    they asked a question. BUT a prospect who asks a question every single turn would otherwise pin
    the agent to answer_via_kb forever and it would never advance to a close (the reactive-loop bug).
    So if the agent ALREADY answered last turn, a proposed `pitch` is allowed to ADVANCE this turn
    instead of answering yet again; `ask`/`confirm_known` still defer (answer before MORE discovery —
    M2). Acts that already address the prospect (answer_via_kb/handle_objection) or advance/close/
    escalate/disqualify are left to the close-path + escalation gates (attempt_close is never deferred
    here, so the policy can always close)."""
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
        # Escape the answer-loop: if we just answered, let a pitch advance the sale rather than
        # answering the next question too (otherwise an inquisitive prospect blocks every close).
        if decision.act == "pitch" and _prev_agent_act(history) == "answer_via_kb":
            return decision
        out = decision.copy()
        out.act = "answer_via_kb"
        out.tier = None
        out.rationale = "address_direct_input: prospect asked a direct question; answering before more discovery"
        return out
    return decision


# --- advance_to_close -------------------------------------------------------------------------

# Reactive/stalling acts the agent gets stuck in; advance_to_close converts these to a close when the
# belief is closeable. attempt_close/escalate/disqualify are never advanceable; handle_objection is
# advanceable ONLY in the break-through case (a repeatedly-handled objection).
_ADVANCEABLE_ACTS = frozenset({"answer_via_kb", "confirm_known", "pitch", "ask"})
# After the agent has handled objections this many times recently, break the deadlock and advance
# (a relentless objector who re-raises every turn would otherwise block every close).
_BREAKTHROUGH_OBJECTION_HANDLES = 2


def _recent_objection_handles(history: Optional[Sequence[Any]], lookback: int = 6) -> int:
    """Count handle_objection acts among the last `lookback` agent turns — detects the agent being
    stuck handling the same objection over and over (so it can advance instead)."""
    if not history:
        return 0
    seen = handled = 0
    for turn in reversed(list(history)):
        if not isinstance(turn, dict) or turn.get("role") not in ("assistant", "agent"):
            continue
        seen += 1
        if turn.get("act") == "handle_objection":
            handled += 1
        if seen >= lookback:
            break
    return handled


def advance_to_close(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """Take the INITIATIVE to close once the belief says the prospect is closeable.

    The LLM policy tends to stay reactive — answering an inquisitive prospect's questions forever (or
    handling a relentless objector turn after turn) and never asking for the sale (the can't-close
    failure: 0 close attempts even for warm, qualified prospects). This gate converts a reactive/
    stalling act into an attempt_close at the right tier once at least `min_turns_before_close` turns
    have passed and EITHER there is no open objection (warmth-gated close) OR the objection has been
    handled repeatedly (break the deadlock with a low-pressure consultation). Tier selection:
      • budget-constrained (price_sensitivity high OR firm refusals) -> the FREE 'callback' rung — a
        no-budget prospect still gets a meeting, never a paid tier they'll reject;
      • else very warm + buying intent -> 'trial';
      • else warmed (or breaking through an objection) -> 'consultation'.
    trust is the reliable warmth signal (purchase_intent in the agent's belief barely moves). The
    prospect's HONEST buy-gate still decides acceptance; NLG still has the open_question so the close
    acknowledges what they asked (not a dodge). Runs BEFORE pushiness_cap (which can veto an over-
    aggressive close) and offer_low_commitment_on_budget."""
    if int(belief.turn_count) < int(_threshold(config, "min_turns_before_close")):
        return decision
    objection_open = bool(belief.active_objection)
    breakthrough = objection_open and _recent_objection_handles(history) >= _BREAKTHROUGH_OBJECTION_HANDLES
    can_advance = decision.act in _ADVANCEABLE_ACTS or (breakthrough and decision.act == "handle_objection")
    if not can_advance:
        return decision
    # An open objection that has NOT yet been handled enough times: keep handling, don't close over it.
    if objection_open and not breakthrough:
        return decision

    trust = float(belief.drivers.get("trust", 0.0))
    intent = float(belief.drivers.get("purchase_intent", 0.0))
    price_sens = float(belief.drivers.get("price_sensitivity", 0.0))
    refusals = int(belief.meta.get("affordability_refusal_count", 0))
    budget_constrained = (
        price_sens >= _threshold(config, "callback_price_sensitivity")
        or refusals >= int(_threshold(config, "budget_refusals_before_callback"))
    )

    if budget_constrained:
        tier = "callback"
    elif trust >= _threshold(config, "close_ready_trial_trust") and intent >= _threshold(
        config, "close_ready_trial_intent"
    ):
        tier = "trial"
    elif trust >= _threshold(config, "close_ready_trust") or breakthrough:
        tier = "consultation"
    else:
        return decision

    out = decision.copy()
    out.act = "attempt_close"
    out.tier = tier
    out.target_slot = None
    out.rationale = (
        f"advance_to_close: tier='{tier}' (trust={trust:.2f}, purchase_intent={intent:.2f}, "
        f"breakthrough={breakthrough}, budget_constrained={budget_constrained}), "
        f"{belief.turn_count} turns in — taking initiative instead of staying reactive"
    )
    return out


# --- close_floor (CB-29: no premature/pushy close before real discovery) -----------------------

# Paid commitment-ladder rungs that REQUIRE earned discovery before they may fire. The free
# 'callback' rung is intentionally absent: offering a no-cost future follow-up is never pushy, even
# early (and offer_low_commitment_on_budget legitimately steers a budget-refuser straight to it).
_PAID_CLOSE_TIERS = frozenset({"consultation", "trial", "enrollment"})


# The neutral driver prior — purchase_intent at/below this means the prospect has NOT signaled any
# buying readiness yet, so a paid close with zero discovery is the agent closing BLIND (the defect).
_NEUTRAL_PRIOR = 0.5


def close_floor(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """Forbid the BLIND premature/pushy paid close the real call exhibited (CB-29 fix).

    In ep-eba52675… the agent fired attempt_close at turns 9 & 11 with ZERO discovery against a
    HOSTILE/disengaged caller who had signaled no readiness — a pushy, premature close. advance_to_close
    already respects min_turns_before_close, but a close the LLM proposes DIRECTLY bypassed every
    discovery check. This gate closes that hole with a TIGHT, honest trigger that only fires on the
    actual defect shape: a PAID attempt_close (consultation/trial/enrollment) is backed off to a
    non-pressure act ONLY when the agent is closing BLIND —
      • NO non-budget discovery slot is filled (it has learned nothing concrete about the learner), AND
      • purchase_intent has NOT risen above the neutral prior (the prospect has signaled no readiness).
    When EITHER holds — some discovery happened, OR the prospect is signaling buying readiness — the
    close stands (a genuinely hot prospect, or one who has done real discovery, can still close; this
    is why the warmed/qualified close paths and the buy-gate are untouched). The free 'callback' rung is
    always exempt (a no-cost follow-up is never pushy). On a trip it backs off to answering the
    prospect's open question if any, else a value pitch — keep building, don't push. Runs AFTER
    advance_to_close (so a gate-advanced close is also checked) and BEFORE pushiness_cap. R37 intact.
    """
    if decision.act != "attempt_close":
        return decision
    if decision.tier == "callback":
        return decision  # the free rung is exempt — never pushy

    required = _required_discovery_slots(config)
    filled = sum(1 for s in required if belief.slot_confidence(s) >= _LOCK_CONFIDENCE)
    has_discovery = filled >= int(_threshold(config, "discovery_slots_before_close"))
    signals_readiness = float(belief.drivers.get("purchase_intent", 0.0)) > _NEUTRAL_PRIOR
    # The close stands unless the agent is closing BLIND: no discovery AND no readiness signal.
    if has_discovery or signals_readiness:
        return decision

    out = decision.copy()
    # Back off to value-building / answering, never silence: keep advancing discovery instead.
    out.act = "answer_via_kb" if belief.open_question else "pitch"
    out.tier = None
    out.target_slot = None
    out.rationale = (
        f"close_floor: closing blind ({filled} discovery slot(s), no readiness signal); "
        f"holding off a {decision.tier or 'paid'} close — building value instead of pushing"
    )
    return out


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
    -> pushiness_cap -> offer_low_commitment_on_budget (last, so the free-callback offer has final say).
    """
    # Extreme moments dominate: nothing else matters if we must defer.
    escalated = escalation_triggers(decision, belief, config, history=history)
    if escalated.act == "escalate":
        return escalated

    d = skip_known(decision, belief, config)
    d = address_direct_input(d, belief, config, history=history)  # answer/handle input, but let pitch advance
    d = must_clear_objection(d, belief, config)
    d = price_gate(d, belief, config)
    # Take initiative to close when the belief is closeable (escape the reactive answer-loop); placed
    # before pushiness_cap so an over-aggressive close can still be backed off on a high bail signal.
    d = advance_to_close(d, belief, config, history=history)
    # CB-29: hold off ANY paid close (LLM-proposed OR gate-advanced) until real discovery has happened
    # (min turns + enough filled discovery slots) — kills the premature/pushy close at turns 9 & 11.
    d = close_floor(d, belief, config, history=history)
    d = pushiness_cap(d, belief, config, history=history)
    # LAST override: a budget-constrained prospect gets the free callback rung (help + future meeting),
    # not a loop or a release — placed here so must_clear_objection/pushiness don't undo the offer.
    d = offer_low_commitment_on_budget(d, belief, config)

    # Re-check escalation on the (possibly rewritten) decision so a gate-introduced concession or
    # state change can't escape the extreme-deferral guarantee.
    return escalation_triggers(d, belief, config, history=history)

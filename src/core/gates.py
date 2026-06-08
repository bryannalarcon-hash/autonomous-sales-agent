# Deterministic safety/correctness gates over the BeliefState (plan U4; R5/R7/R29). Pure functions:
# each takes a PROPOSED Decision (from src/core/policy.py), the belief, and the AgentConfig, and
# returns the ALLOWED/OVERRIDDEN Decision — gates have FINAL say over the LLM proposal (neuro-
# symbolic: LLM proposes, gates decide). apply_gates() chains them in priority order.
#   de_escalate         — CB-37: hostility (belief.meta["hostility"]) or EXTREME bail
#                         (>= extreme_bail_deescalate) -> escalate (graceful hand-off/exit); runs FIRST
#                         so nothing downstream can re-introduce a pitch/close/re-ask on a hostile call.
#   no_repeat_discovery — CB-35: drop a re-ask of a slot already FILLED / DECLINED
#                         (belief.meta["declined_slots"]) / asked >= max_slot_asks
#                         (belief.meta["asked_slots"]); never re-ask the same question a 3rd time.
#   establish_who_first — CB-34: a learner-specific ask first establishes WHO the tutoring is for
#                         (belief.meta["learner_established"]/["learner_denied"]) — never presume a learner.
#   skip_known          — never ask a slot already filled at/above lock confidence; CB-61 extension:
#                         on a confirm_request, any deferrable act for a known slot is also converted
#                         to confirm_known so the echo path fires.
#   no_authority_prefers_callback— CB-50: a proposed `escalate` driven SOLELY by the no-authority
#                         gatekeeper read (belief.active_objection == "decision_maker", no genuine
#                         escalation trigger) is down-tiered to attempt_close@callback — book a
#                         follow-up for the real decision-maker instead of wasting a live human.
#   escalate_only_if_warranted— CB-50: ANY surviving TRIGGERLESS escalate (no genuine reason — the
#                         agent bailing, not handing off) is redirected — gatekeeper / 3rd-party
#                         ("for my <relative>") read -> attempt_close@callback; otherwise a NON-
#                         terminal selling act (answer_via_kb on an open question, else pitch) so the
#                         agent keeps selling and reaches a proper close later. Genuine reasons still
#                         escalate. Mirrors close_floor: the terminal move is gated until warranted.
#   offer_low_commitment_on_budget— repeated FIRM budget refusals (cumulative count in belief.meta) ->
#                         offer the FREE callback rung (help + a future meeting), denying only the paid
#                         tiers, instead of looping or releasing. Runs LAST (final say).
#   address_direct_input— a live objection or a direct question (from the DST intent classifier)
#                         pre-empts a discovery/pitch act (-> handle_objection / answer_via_kb) so the
#                         agent never ignores what the prospect just asked — BUT once it has answered a
#                         NEW question it lets a `pitch` ADVANCE (escapes the reactive answer-loop).
#                         CB-48: a RECURRING unanswered question (the prospect re-asking the SAME thing
#                         after an answer attempt) is ANSWERED again — even over a proposed pitch OR a
#                         close — so the agent stops evading the core concern; AND a discovery ask/
#                         confirm under HIGH bail_risk is suppressed (answer/advance, never slot-fill a
#                         hot, impatient prospect).
#                         CB-61: a confirm_request (explicit "please confirm <X>") routes a deferrable
#                         act to confirm_known targeting the most relevant known slot, so the NLG path
#                         echoes the stated time instead of replying vaguely.
#                         CB-61 adversarial (Defect 3): active_objection check runs BEFORE the
#                         confirm_request branch — a live objection always wins over a confirm echo.
#   must_clear_objection— forbid pivot-to-close while active_objection is set (-> handle_objection).
#   price_gate          — don't open price/budget before a trust event (prospect asked unprompted
#                         OR a value-ack/trust threshold OR required discovery slots filled).
#   advance_to_close    — once the belief is closeable (no objection, warmed trust, enough turns),
#                         convert a reactive act into attempt_close so the agent ASKS for the sale
#                         instead of answering questions forever (the can't-close fix). CB-48: it will
#                         NOT take close-initiative over a RECURRING unanswered question (don't re-
#                         convert address_direct_input's answer back into a close on a warm prospect).
#                         Shares its close-readiness/tier test (_closeable_tier) with the loop-breaker.
#   break_no_progress_loop— SAFEGUARD 1 (the ONE general loop-breaker): if the last K gated acts were
#                         all REACTIVE (ask/confirm_known/pitch/answer_via_kb/handle_objection) AND
#                         nothing PROGRESSED across them (no slot newly locked, no objection cleared,
#                         no purchase_intent rise > epsilon — prior-turn snapshot kept by respond() in
#                         meta), force a COMMIT: a close if closeable (_closeable_tier), else a genuine
#                         escalation, else an honest bottom-line answer (NOT a (K+1)th loop turn).
#                         SUBSUMES the old objection-deadlock special case AND catches the free-form
#                         re-ask/re-offer loops the slot/objection-specific guards never covered.
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

import re
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
    # CB-37: at/above this bail_risk (OR any detected hostility), the agent must DE-ESCALATE / offer a
    # graceful exit / escalate — never pitch, close, or re-ask. This is stricter than pushiness_cap
    # (which merely backs pressure off to a low-pressure act): extreme bail means the call is over, so
    # we hand off rather than keep engaging. Set ABOVE pushiness_cap so there's a back-off band first.
    "extreme_bail_deescalate": 0.9,
    # CB-35: a discovery slot ASKED at least this many times within one call is dropped permanently —
    # the agent never re-asks the same question a third time (the live call re-asked grade 5x). Asked
    # TWICE is the ceiling; the third proposed ask is dropped.
    "max_slot_asks": 2,
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


# --- no_repeat_discovery (CB-35: never re-ask an answered / declined / over-asked slot) ---------

# Learner-specific discovery slots whose presumption a learner-denial invalidates (CB-34): if the
# caller denied having a learner, asking any of these is presuming the learner exists.
_LEARNER_SLOTS = frozenset({"grade_level", "subject", "goal", "timeline", "prior_tutoring"})


def _redirect_after_dropped_ask(decision: Any, belief: BeliefState, reason: str) -> Any:
    """Replace a dropped discovery `ask` with a non-repeating act: answer an open question if one is
    pending, otherwise re-anchor on value (pitch). Never silence, never re-ask the dropped slot."""
    out = decision.copy()
    out.act = "answer_via_kb" if belief.open_question else "pitch"
    out.target_slot = None
    out.tier = None
    out.rationale = reason
    return out


def no_repeat_discovery(decision: Any, belief: BeliefState, config: AgentConfig) -> Any:
    """Never re-ask a discovery slot that is already FILLED, DECLINED, or asked too many times (CB-35).

    The live call re-asked "what grade?" five times even after the caller answered "Third" and after
    declining a learner. This HARD guard drops the repeat: an `ask` (or `confirm_known` that would
    re-prompt) for a slot that is (a) already filled at lock confidence, (b) recorded as DECLINED in
    belief.meta["declined_slots"], or (c) already asked >= max_slot_asks times
    (belief.meta["asked_slots"][slot]) is redirected to a non-repeating act. skip_known still handles
    the confirm-instead-of-ask for a freshly filled slot; this catches the declined / over-asked cases
    skip_known does not. asked_slots is maintained in respond() from the GATED decision's target_slot."""
    if decision.act != "ask" or not decision.target_slot:
        return decision
    slot = decision.target_slot
    # Already known -> skip_known handles it (confirm, don't re-ask); leave it for that gate.
    if belief.slot_confidence(slot) >= _LOCK_CONFIDENCE:
        return decision
    declined = slot in set(belief.meta.get("declined_slots", []))
    asked = int(dict(belief.meta.get("asked_slots", {})).get(slot, 0))
    over_asked = asked >= int(_threshold(config, "max_slot_asks"))
    if declined:
        return _redirect_after_dropped_ask(
            decision, belief, f"no_repeat_discovery: '{slot}' was declined; dropping it permanently"
        )
    if over_asked:
        return _redirect_after_dropped_ask(
            decision, belief,
            f"no_repeat_discovery: '{slot}' already asked {asked}x; dropping to avoid repeating",
        )
    return decision


# --- establish_who_first (CB-34: don't presume a learner exists before discovery establishes one) ---

def establish_who_first(decision: Any, belief: BeliefState, config: AgentConfig) -> Any:
    """Establish WHO the tutoring is for before any learner-specific question (CB-34).

    The live call asked "what grade is the learner in?" before discovery had established a learner even
    existed — the caller protested "I did not say I had a learner" — and then later PITCHED after the
    denial, producing a broken context-less reply. A decision PRESUMES a learner when it asks a
    learner-specific slot (grade/subject/goal/timeline/prior_tutoring) OR tries to sell (pitch/close).
    Until a learner is ESTABLISHED (belief.meta["learner_established"]), any presuming decision is
    redirected: answer the caller's open question if they raised one, else ask the 'who_for' step. A
    non-presuming act (answer_via_kb / handle_objection / escalate / the who_for ask itself) passes
    untouched. To avoid a who-is-this-for loop, once who_for has been asked max_slot_asks times with no
    answer we stop redirecting and let the (give-up) decision through."""
    # A learner is "known" when the DST flagged it (meta) OR any learner-specific slot is already filled
    # (if we know the grade/subject, a learner obviously exists — covers beliefs built straight from
    # slots without the meta flag, the normal post-discovery state).
    learner_known = bool(belief.meta.get("learner_established")) or any(
        belief.slot_confidence(s) >= _LOCK_CONFIDENCE for s in _LEARNER_SLOTS
    )
    if learner_known:
        return decision  # we know who it's for — learner-specific discovery / pitch / close is fine
    learner_ask = decision.act == "ask" and decision.target_slot in _LEARNER_SLOTS
    # A PITCH/CLOSE is blocked here ONLY when the caller EXPLICITLY DENIED a learner and we haven't
    # re-established one — the demonstrated failure (pitching right after "I didn't say I had a learner",
    # which produced a context-less broken reply). A normal warm pre-close pitch is governed by
    # close_floor / advance_to_close, NOT this gate, so we don't block legitimate closes.
    sell_after_denial = decision.act in ("pitch", "attempt_close") and bool(belief.meta.get("learner_denied"))
    if not (learner_ask or sell_after_denial):
        return decision  # answer_via_kb / handle_objection / escalate / who_for ask / warm pitch — fine
    # Don't loop forever: if who-it's-for was already asked the cap number of times with no answer,
    # stop redirecting and let the decision through (graceful give-up; matches no_repeat_discovery).
    asked_who = int(dict(belief.meta.get("asked_slots", {})).get("who_for", 0))
    if asked_who >= int(_threshold(config, "max_slot_asks")):
        return decision
    out = decision.copy()
    if belief.open_question:
        # The caller asked something — answer it rather than pushing another who-question at them.
        out.act = "answer_via_kb"
        out.target_slot = None
        out.rationale = (
            "establish_who_first: no learner established; answering the caller's question before any "
            "learner-specific step or pitch"
        )
        return out
    # No learner established (or just denied), no open question -> find out who it's for before any
    # learner-specific question OR pitch/close.
    out.act = "ask"
    out.target_slot = "who_for"
    out.tier = None
    out.rationale = (
        f"establish_who_first: no learner established yet; establishing who the tutoring is for before "
        f"'{decision.target_slot or decision.act}'"
    )
    return out


# --- de_escalate (CB-37: extreme bail / hostility -> de-escalate, never pitch/close/re-ask) --------

def _hostile_or_extreme_bail(belief: BeliefState, config: AgentConfig) -> Optional[str]:
    """Return a de-escalation reason if the caller is hostile/abusive OR bail_risk is extreme; else None."""
    if bool(belief.meta.get("hostility")):
        return "caller hostile/abusive"
    if float(belief.drivers.get("bail_risk", 0.0)) >= _threshold(config, "extreme_bail_deescalate"):
        return "bail_risk extreme"
    return None


# Acts that are NEVER acceptable once the caller is hostile or about to walk: any pressure/close, any
# discovery re-ask. We force a graceful de-escalation (escalate to a human / offer an exit) instead.
_DEESCALATE_BLOCKED_ACTS = frozenset({"attempt_close", "pitch", "ask", "confirm_known", "handle_objection"})


def de_escalate(decision: Any, belief: BeliefState, config: AgentConfig) -> Any:
    """At extreme bail OR detected hostility, STOP selling and de-escalate (CB-37).

    The live call kept pitching at bail=0.77 and even after "Fuck you". pushiness_cap backs pressure
    off to a low-pressure act, but at EXTREME bail (>= extreme_bail_deescalate) or any detected
    hostility the right move is to disengage gracefully — never pitch, close, or re-ask. We override
    any selling/discovery act to `escalate` (a graceful human hand-off / exit). answer_via_kb and an
    already-`escalate`/`disqualify` act are left alone so the agent can still answer a final direct
    question or release. Runs FIRST in the chain so nothing downstream can re-introduce pressure."""
    reason = _hostile_or_extreme_bail(belief, config)
    if reason is None:
        return decision
    if decision.act not in _DEESCALATE_BLOCKED_ACTS:
        return decision  # answer_via_kb / escalate / disqualify are fine — not pressure
    belief.escalation_imminent = True  # surface to the live monitor / SessionReport
    out = decision.copy()
    out.act = "escalate"
    out.target_slot = None
    out.tier = None
    out.concession = None
    out.rationale = (
        f"de_escalate: {reason}; stopping the pitch and offering a graceful hand-off/exit instead of "
        "pushing or re-asking"
    )
    return out


# --- no_authority_prefers_callback (CB-50: gatekeeper -> callback, not human escalation) -------

# The DST classifies the no-authority gatekeeper situation ("I'm not the one deciding / I'll check
# with my sister / not my call") as the `decision_maker` objection. meta keys a future DST/bridge
# could also set are honored so the gate works even when the objection has since been handled.
_NO_AUTHORITY_OBJECTION = "decision_maker"
_NO_AUTHORITY_META_KEYS = frozenset({"no_authority", "decision_maker", "gatekeeper"})

# CB-50 broadening (the Pat-t1 defect): a gatekeeper who merely SPEAKS FOR someone ("my sister's
# kid", "calling for my nephew", "on behalf of my coworker") does NOT trip the explicit
# decision_maker objection on turn 1, so the no-authority escalate slipped through. This cheap,
# tightly-scoped phrase read catches the 3rd-party framing: "for/on behalf of my <relative/person>".
# Deliberately narrow — it requires an explicit "for my/on behalf of my" lead-in so a first-person
# caller ("my goal is…", "my daughter is in 8th") is NOT misread as a gatekeeper.
_THIRD_PARTY_RE = re.compile(
    r"\b(?:for|on\s+behalf\s+of|representing)\s+(?:my|our|the|a|his|her|their)\b",
    re.I,
)


def _third_party_read(belief: BeliefState) -> bool:
    """Cheap heuristic: the caller is asking FOR a third party (a gatekeeper speaking on someone's
    behalf) rather than for themselves. Looks at the current open_question only — the freshest thing
    the prospect said — so it's a low-cost read with no history scan. Conservative: needs an explicit
    "for my / on behalf of my" lead-in (see _THIRD_PARTY_RE), so "my goal is…" won't match."""
    text = getattr(belief, "open_question", None)
    if not text:
        return False
    return bool(_THIRD_PARTY_RE.search(str(text)))


def _is_no_authority(belief: BeliefState) -> bool:
    """True when the belief indicates the caller is NOT the decision-maker (the gatekeeper read).

    Primary signal is the DST's `decision_maker` objection (dst._OBJECTION_PATTERNS: "check with my
    sister", "talk to my", "run it by", "not my call"). Also honors an explicit meta flag a future
    DST/memory bridge could set, so the read survives after the objection itself is cleared. CB-50:
    additionally a cheap 3rd-party phrase read ("for my sister's kid") so a gatekeeper who only
    SPEAKS FOR someone — without tripping the explicit objection — is still treated as no-authority."""
    if belief.active_objection == _NO_AUTHORITY_OBJECTION:
        return True
    if any(bool(belief.meta.get(k)) for k in _NO_AUTHORITY_META_KEYS):
        return True
    return _third_party_read(belief)


def no_authority_prefers_callback(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """Down-tier a no-authority ESCALATION to a low-touch CALLBACK offer (CB-50 — the Pat defect).

    The eval (2026-06-04) showed the agent ESCALATE a no-authority gatekeeper to a live specialist on
    turn 1 — the LLM proposed `escalate` purely because the caller isn't the decision-maker (the DST
    surfaces that as the `decision_maker` objection). That wastes a human on an unqualified lead; the
    correct, low-touch move is to offer to schedule a CALLBACK for the actual decision-maker (capture
    how/when to reach them). The personas cap such a caller at the free `callback` rung, so the agent
    SHOULD be offering a callback close, not handing off.

    Conservative + tight: it ONLY rewrites a proposed `escalate`, ONLY when the belief shows the
    no-authority read, AND ONLY when there is NO genuine escalation trigger (a real human request,
    compliance territory, a low-confidence streak, or a beyond-band concession — reusing the same
    predicate escalation_triggers fires on). When a genuine trigger IS present we leave the escalate
    alone so the real escalation still stands. Output is attempt_close@callback — the free rung, which
    close_floor/pushiness/offer_low_commitment_on_budget all already treat as never-pushy and never
    re-tier, and which trips NO escalation reason, so the final escalation re-check leaves it intact."""
    if decision.act != "escalate":
        return decision
    if not _is_no_authority(belief):
        return decision  # not the gatekeeper situation — leave the escalate to the escalation gate
    # A GENUINE escalation reason (human request / compliance / low-confidence streak / over-band
    # concession) must still escalate — only an escalate driven SOLELY by the no-authority read is
    # redirected. Reuse the escalation predicate so the two gates can never disagree.
    if _escalation_reason(decision, belief, config, history) is not None:
        return decision
    out = decision.copy()
    out.act = "attempt_close"
    out.tier = _FREE_LADDER_TIER  # the free callback rung — book a follow-up for the real decider
    out.target_slot = None
    out.concession = None
    out.rationale = (
        "no_authority_prefers_callback: caller isn't the decision-maker and there's no genuine "
        "escalation trigger — offering a callback for the actual decider instead of wasting a human"
    )
    return out


# --- escalate_only_if_warranted (CB-50: a TRIGGERLESS escalate must not terminate the call) ----

def escalate_only_if_warranted(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """A TRIGGERLESS `escalate` is the agent BAILING, not handing off — redirect it (CB-50).

    The real-model eval showed the agent reach for "let me connect you with a specialist who'll call
    you back" as a DEFAULT forward move: Pat got escalated on turn 1 with zero discovery; Dana's proof
    question got punted to "a consultant will explain" instead of answered + closed at the consult
    tier. Mirroring close_floor (CB-29), the terminal/hand-off move is GATED until it is actually
    warranted. So a proposed `escalate` with NO genuine escalation reason (distress / explicit human
    request / compliance / a low-confidence streak / an over-band concession — the EXACT predicate
    escalation_triggers fires on, reused so the two can never disagree) is converted:
      • no-authority / gatekeeper read (decision_maker objection, a meta flag, OR the cheap 3rd-party
        "for my <relative>" phrase) -> attempt_close@callback (book a follow-up for the real decider;
        the same redirect no_authority_prefers_callback produces, now also reachable from this guard);
      • otherwise -> a NON-terminal selling act: answer_via_kb if the prospect has an open question
        (answer it, don't punt it), else pitch (keep discovering/selling so a proper close can land on
        a LATER turn).
    A GENUINE escalation reason is left untouched — Marcus's truly-unanswerable cases, distress, and
    explicit requests still escalate (escalation_triggers owns those and re-checks after this gate).
    A break_no_progress_loop STICKY terminal (decision.meta["loop_terminal"], ISSUE 2) is ALSO left
    untouched — it is a deliberate firm hand-off AFTER a soft commit was already tried and declined, so
    redirecting it back into another selling/answer turn would resume the death-march this guard is
    meant to avoid creating. Conservative + tight: it ONLY rewrites a proposed `escalate`, and ONLY when
    no genuine reason exists. R37: copy + mutate the Decision, never construct a fresh one."""
    if decision.act != "escalate":
        return decision
    # A break_no_progress_loop sticky terminal is warranted by the loop policy (a declined soft commit
    # already fired) — do NOT redirect it back into a selling/answer turn (that would re-loop).
    if bool(decision.meta.get("loop_terminal")):
        return decision
    # A GENUINE escalation reason still escalates immediately — only a TRIGGERLESS escalate (the agent
    # bailing) is redirected. Reuse escalation_triggers' predicate so the gates can never disagree.
    if _escalation_reason(decision, belief, config, history) is not None:
        return decision
    out = decision.copy()
    out.target_slot = None
    out.tier = None
    out.concession = None
    if _is_no_authority(belief):
        out.act = "attempt_close"
        out.tier = _FREE_LADDER_TIER  # book a callback for the actual decision-maker
        out.rationale = (
            "escalate_only_if_warranted: triggerless escalate on a no-authority/gatekeeper read — "
            "booking a callback for the real decider instead of terminally handing off"
        )
        return out
    # Qualified / first-person prospect with no genuine trigger: keep SELLING. Answer their open
    # question if any (don't punt it to 'a consultant will explain'), else re-anchor on value.
    out.act = "answer_via_kb" if belief.open_question else "pitch"
    out.rationale = (
        "escalate_only_if_warranted: triggerless escalate with no genuine reason — keeping the agent "
        "selling (answering the open question / pitching) so it can reach a proper close, not bailing"
    )
    return out


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
# Discovery acts the agent must NOT use to slot-fill a hot, impatient prospect (CB-48: suppress under
# high bail). pitch is excluded — re-anchoring on value is a legitimate move on a hot prospect, and
# the recurring-question / objection checks already redirect a pitch when there's something to address.
_DISCOVERY_ACTS = frozenset({"ask", "confirm_known"})
# Stop-words stripped before comparing two questions for recurrence (CB-48): they carry no topical
# signal, so two questions are "the same" when their CONTENT words substantially overlap.
_RECUR_STOPWORDS = frozenset({
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are", "do", "does", "did",
    "can", "could", "you", "your", "i", "we", "he", "she", "it", "they", "me", "my", "him", "her",
    "this", "that", "what", "how", "when", "why", "who", "which", "will", "would", "so", "just",
    "not", "or", "really", "actually", "tell", "give", "with", "at", "be", "have", "has", "out",
})


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


def _content_words(text: Optional[str]) -> set[str]:
    """Lower-cased alphanumeric content words of a question, minus stop-words (CB-48 recurrence)."""
    if not text:
        return set()
    words = re.findall(r"[a-z0-9']+", str(text).lower())
    return {w for w in words if w not in _RECUR_STOPWORDS and len(w) > 1}


def _question_recurs(belief: BeliefState, history: Optional[Sequence[Any]]) -> bool:
    """True when the prospect's CURRENT open_question substantially repeats one they asked EARLIER in
    the call (CB-48). This is the "Marcus asked 4x and got evaded" signal: the same challenge re-asked
    after an answer attempt. We compare CONTENT-word overlap (stop-words stripped) between the current
    open_question and each PRIOR user turn; >=2 shared content words (or a near-total overlap of a
    short question) counts as a recurrence. Conservative: a brand-new question won't match, so the
    reactive-loop escape still fires for genuinely new questions."""
    if not history:
        return False
    current = _content_words(getattr(belief, "open_question", None))
    if len(current) < 2:
        return False  # too thin to judge recurrence reliably; treat as new (don't trap the agent)
    # Look at PRIOR user turns only (skip the latest, which mirrors the current open_question).
    user_turns = [t for t in history if isinstance(t, dict) and t.get("role") == "user"]
    for turn in user_turns[:-1] if user_turns else []:
        prior = _content_words(turn.get("text", turn.get("content", "")))
        if not prior:
            continue
        shared = current & prior
        if len(shared) >= 2 or (shared and len(shared) >= max(1, len(current) - 1)):
            return True
    return False


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
    So if the agent ALREADY answered a NEW question last turn, a proposed `pitch` is allowed to ADVANCE
    this turn instead of answering yet again; `ask`/`confirm_known` still defer (answer before MORE
    discovery — M2).

    CB-48 (directness — Marcus/Dana failures):
      • RECURRING unanswered question: when the prospect re-asks the SAME thing (content-word overlap
        with an earlier turn), the agent was EVADING it — the pitch-escape fired and even a close got
        pushed over the unanswered challenge. So a recurring open_question forces answer_via_kb even
        over a proposed `pitch` AND a proposed `attempt_close` (answer the core concern before re-
        pushing). A genuinely NEW question keeps the reactive-loop escape (pitch may advance).
      • HIGH bail_risk suppresses discovery: a discovery `ask`/`confirm_known` proposed while bail_risk
        is at/above the pushiness cap (a hot, impatient prospect — "no fluff") is redirected to answer
        their open question if any, else a value pitch. We do NOT slot-fill someone about to walk.

    CB-61 (confirm-echo):
      • A confirm_request (explicit "please confirm <X>") routes a deferrable act to confirm_known
        targeting the most relevant known slot, so NLG echoes the stated time rather than replying
        vaguely. The best target is the callback_window slot (the typical confirm target for a just-
        stated time); fallback to the open_question itself surfacing in KNOWN_SO_FAR.

    Acts that already address the prospect (answer_via_kb/handle_objection) or escalate/disqualify are
    left to the close-path + escalation gates."""
    recurring_q = (
        bool(belief.open_question)
        and belief.last_user_act in ("question", "price_inquiry")
        and _question_recurs(belief, history)
    )
    # CB-48: a RECURRING unanswered question must be answered BEFORE a close — pre-empt a proposed
    # attempt_close (which is otherwise never deferred here) so the agent stops re-pushing over it.
    if recurring_q and decision.act == "attempt_close":
        out = decision.copy()
        out.act = "answer_via_kb"
        out.tier = None
        out.target_slot = None
        out.rationale = (
            "address_direct_input: prospect re-asked an unanswered question; answering it before any "
            "close attempt (CB-48 directness)"
        )
        return out

    if decision.act not in _DEFERRABLE_ACTS:
        return decision

    # CB-61 adversarial fix (Defect 3): a LIVE objection ALWAYS wins over a confirm_request.
    # The original order put confirm_request BEFORE active_objection, so "just confirm Thursday —
    # also it's really too expensive" converted to confirm_known and the price objection was never
    # addressed. A prospect who raises an objection in the same turn as a confirm request must have
    # the objection handled first; the confirm echo can ride along after the objection clears.
    if belief.active_objection:
        out = decision.copy()
        out.act = "handle_objection"
        out.target_slot = None
        out.tier = None
        out.rationale = (
            f"address_direct_input: prospect raised '{belief.active_objection}'; "
            "handling it before any more discovery or confirm echo"
        )
        return out

    # CB-61: an explicit confirm-request ("please confirm they'll call Thursday after 3pm") — route
    # any deferrable act to confirm_known so the agent echoes the stated time back. The target is the
    # most useful known slot for the echo; callback_window is the primary target (contains the exact
    # time the prospect just proposed); fallback to no target_slot (NLG will echo from open_question).
    # Only fires when there is NO live objection (objection check above takes priority).
    if belief.last_user_act == "confirm_request":
        out = decision.copy()
        out.act = "confirm_known"
        out.tier = None
        # Use callback_window if it's filled (likely the thing they want confirmed); else no target.
        if belief.slot_confidence("callback_window") >= _LOCK_CONFIDENCE:
            out.target_slot = "callback_window"
        else:
            out.target_slot = None
        out.rationale = (
            "address_direct_input: prospect asked for an explicit confirmation; routing to "
            "confirm_known so the agent echoes the stated time (CB-61 confirm-echo)"
        )
        return out
    if belief.open_question and belief.last_user_act in ("question", "price_inquiry"):
        # Escape the answer-loop ONLY for a genuinely NEW question: if we just answered and the prospect
        # asked something DIFFERENT, let a pitch advance the sale (an inquisitive prospect shouldn't
        # block every close). A RECURRING, still-unanswered question does NOT escape — keep answering it
        # (CB-48: Marcus asked the same challenge 4x and got pitched at instead of answered).
        if decision.act == "pitch" and _prev_agent_act(history) == "answer_via_kb" and not recurring_q:
            return decision
        out = decision.copy()
        out.act = "answer_via_kb"
        out.tier = None
        out.rationale = "address_direct_input: prospect asked a direct question; answering before more discovery"
        return out
    # CB-48: suppress a discovery ask/confirm on a HOT, impatient prospect (high bail) — don't slot-fill
    # someone about to walk. With nothing to answer (no open question handled above), advance with a
    # value pitch instead of pushing another discovery question. Tight: only fires at/above the cap.
    if decision.act in _DISCOVERY_ACTS:
        bail = float(belief.drivers.get("bail_risk", 0.0))
        if bail >= _threshold(config, "pushiness_cap"):
            out = decision.copy()
            out.act = "pitch"
            out.target_slot = None
            out.tier = None
            out.rationale = (
                f"address_direct_input: bail_risk {bail:.2f} high; not slot-filling an impatient "
                "prospect — re-anchoring on value instead (CB-48 directness)"
            )
            return out
    return decision


# --- advance_to_close + the SHARED closeable/tier-selection logic ------------------------------

# Reactive/stalling acts advance_to_close converts to a close when the belief is closeable.
# attempt_close/escalate/disqualify/handle_objection are never advanceable here — an open objection is
# handled first, and the objection-DEADLOCK breakthrough is now break_no_progress_loop's job (a no-
# progress reactive run), so the old _recent_objection_handles / _BREAKTHROUGH_OBJECTION_HANDLES
# special case is GONE — folded into the unified loop-breaker.
_ADVANCEABLE_ACTS = frozenset({"answer_via_kb", "confirm_known", "pitch", "ask"})


def _closeable_tier(belief: BeliefState, config: AgentConfig, *, breakthrough: bool = False) -> Optional[str]:
    """The SINGLE source of truth for "is this belief closeable, and at which tier?" (shared by
    advance_to_close AND break_no_progress_loop so the close-readiness logic is never duplicated).

    Returns the commitment-ladder tier to close at, or None when the belief is NOT closeable yet.
    Mirrors advance_to_close's selection: budget-constrained -> the FREE 'callback' rung; very warm +
    buying intent -> 'trial'; warmed (or breaking through a stalled objection) -> 'consultation';
    otherwise not closeable (None). `breakthrough` lets the consultation rung fire on a stalled
    objection even when trust hasn't reached the warm bar (the deadlock break)."""
    trust = float(belief.drivers.get("trust", 0.0))
    intent = float(belief.drivers.get("purchase_intent", 0.0))
    price_sens = float(belief.drivers.get("price_sensitivity", 0.0))
    refusals = int(belief.meta.get("affordability_refusal_count", 0))
    budget_constrained = (
        price_sens >= _threshold(config, "callback_price_sensitivity")
        or refusals >= int(_threshold(config, "budget_refusals_before_callback"))
    )
    if budget_constrained:
        return "callback"
    if trust >= _threshold(config, "close_ready_trial_trust") and intent >= _threshold(
        config, "close_ready_trial_intent"
    ):
        return "trial"
    if trust >= _threshold(config, "close_ready_trust") or breakthrough:
        return "consultation"
    return None


def advance_to_close(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """Take the INITIATIVE to close once the belief says the prospect is closeable.

    The LLM policy tends to stay reactive — answering an inquisitive prospect's questions forever and
    never asking for the sale (the can't-close failure: 0 close attempts even for warm, qualified
    prospects). This gate converts a reactive/stalling act into an attempt_close at the right tier once
    at least `min_turns_before_close` turns have passed AND there is no open objection (warmth-gated
    close). Tier selection is shared with break_no_progress_loop via _closeable_tier():
      • budget-constrained (price_sensitivity high OR firm refusals) -> the FREE 'callback' rung — a
        no-budget prospect still gets a meeting, never a paid tier they'll reject;
      • else very warm + buying intent -> 'trial';
      • else warmed -> 'consultation'.
    The OBJECTION-DEADLOCK breakthrough (a relentless objector handled repeatedly) is NO LONGER a
    special case here — it is subsumed by break_no_progress_loop (SAFEGUARD 1): a relentless objector
    handled K times with the objection never clearing is exactly a no-progress reactive run, so the
    breakthrough close falls out of the general loop-breaker. advance_to_close therefore leaves an open
    objection ALONE (handle it first); the breaker breaks the deadlock when it's a genuine stall.
    trust is the reliable warmth signal (purchase_intent in the agent's belief barely moves). The
    prospect's HONEST buy-gate still decides acceptance; NLG still has the open_question so the close
    acknowledges what they asked (not a dodge). Runs BEFORE pushiness_cap (which can veto an over-
    aggressive close) and offer_low_commitment_on_budget."""
    if int(belief.turn_count) < int(_threshold(config, "min_turns_before_close")):
        return decision
    # CB-48: never take close-initiative OVER a RECURRING unanswered question (the prospect re-asking
    # the same challenge). address_direct_input already routes such a turn to answer_via_kb; without
    # this guard advance_to_close would re-convert that answer back into a close on a warm prospect,
    # silently undoing the directness fix (answer the core concern before re-pushing the close).
    if (
        belief.open_question
        and belief.last_user_act in ("question", "price_inquiry")
        and _question_recurs(belief, history)
    ):
        return decision
    if decision.act not in _ADVANCEABLE_ACTS:
        return decision
    # An open objection: keep handling it, don't close over it. The break-through (a relentlessly
    # re-raised objection) is now break_no_progress_loop's job — a no-progress reactive run.
    if bool(belief.active_objection):
        return decision

    tier = _closeable_tier(belief, config)
    if tier is None:
        return decision

    trust = float(belief.drivers.get("trust", 0.0))
    intent = float(belief.drivers.get("purchase_intent", 0.0))
    out = decision.copy()
    out.act = "attempt_close"
    out.tier = tier
    out.target_slot = None
    out.rationale = (
        f"advance_to_close: tier='{tier}' (trust={trust:.2f}, purchase_intent={intent:.2f}), "
        f"{belief.turn_count} turns in — taking initiative instead of staying reactive"
    )
    return out


# --- break_no_progress_loop (SAFEGUARD 1: ONE general progress-based loop-breaker) -------------

# Acts that are REACTIVE — the agent responding to the prospect without committing to a next step.
# A run of these with NO progress is the free-form loop (Marcus re-asking an untracked "SAT score?"
# ~7x; Dana re-offering "judge for yourself" 5x) that no slot/objection-specific guard caught.
_REACTIVE_ACTS = frozenset({"ask", "confirm_known", "pitch", "answer_via_kb", "handle_objection"})
# K — how many CONSECUTIVE reactive gated acts (with no progress across them) trip the breaker.
# Conservative: a normal multi-objection call makes real progress (a slot locks / an objection clears /
# intent rises) almost every turn, so 3 reactive-AND-stalled turns in a row is a genuine stall, not a
# healthy call. Tuned against the pitch/close/objection tests so a real progressing call is NOT cut
# short. The recent_acts window respond() maintains carries at most K acts.
_LOOP_BREAK_K = 3
# purchase_intent must rise by MORE than this since the prior turn to count as progress (the agent's
# own intent barely moves, so a tiny jitter is not real progress).
_PROGRESS_INTENT_EPSILON = 0.03


def _locked_slot_count(belief: BeliefState) -> int:
    """How many slots are at/above lock confidence right now (a slot reaching lock = progress)."""
    return sum(1 for s in belief.slots if belief.slot_confidence(s) >= _LOCK_CONFIDENCE)


def _made_progress_since_prior(belief: BeliefState) -> bool:
    """PROGRESS this window = a discovery slot newly reached lock confidence, OR active_objection went
    set->None, OR purchase_intent rose by > epsilon, compared to the prior-turn snapshot respond()
    stamped on belief.meta["progress_snapshot"]. With no prior snapshot we conservatively treat the
    state as progressing (don't trip the breaker before there's a window to judge)."""
    snap = belief.meta.get("progress_snapshot")
    if not isinstance(snap, dict):
        return True  # no baseline yet -> not a stall
    prior_locked = int(snap.get("locked_slots", 0))
    prior_obj = snap.get("active_objection")
    prior_intent = float(snap.get("purchase_intent", _NEUTRAL_PRIOR))
    if _locked_slot_count(belief) > prior_locked:
        return True  # a slot reached lock confidence
    if prior_obj is not None and belief.active_objection is None:
        return True  # an objection cleared
    if float(belief.drivers.get("purchase_intent", 0.0)) - prior_intent > _PROGRESS_INTENT_EPSILON:
        return True  # buying intent rose meaningfully
    return False


def _stalled_reactive_run(belief: BeliefState) -> bool:
    """True when the last K gated acts (belief.meta["recent_acts"]) were ALL reactive — i.e. the agent
    committed to nothing across the window. A non-reactive act (an attempt_close / escalate / etc.)
    anywhere in the window breaks the run (we're not actually stuck looping)."""
    recent = list(belief.meta.get("recent_acts", []))
    if len(recent) < _LOOP_BREAK_K:
        return False
    window = recent[-_LOOP_BREAK_K:]
    return all(a in _REACTIVE_ACTS for a in window)


def break_no_progress_loop(
    decision: Any,
    belief: BeliefState,
    config: AgentConfig,
    *,
    history: Optional[Sequence[Any]] = None,
) -> Any:
    """SAFEGUARD 1 — ONE general progress-based loop-breaker (subsumes the objection-deadlock special
    case + catches the free-form re-ask/re-offer loops no slot/objection guard covered).

    When the last K (=_LOOP_BREAK_K) consecutive GATED acts were all REACTIVE (ask / confirm_known /
    pitch / answer_via_kb / handle_objection) AND there was NO progress across them — no discovery slot
    newly locked, no active_objection cleared, no purchase_intent rise > epsilon (the prior-turn
    snapshot is maintained by respond() in belief.meta) — the agent is genuinely stuck looping, so we
    FORCE a COMMIT this turn instead of a (K+1)th identical reactive act:
      • if the belief is closeable (the SHARED _closeable_tier test advance_to_close uses), attempt_close
        at the lowest sensible reachable tier;
      • else, if a genuine escalation reason fires (the EXACT _escalation_reason predicate
        escalation_triggers owns), escalate;
      • else, an honest bottom-line answer_via_kb (a real next-step turn, NOT another loop turn),
        tagged decision.meta["loop_break"] so it's observable and so advance_to_close / the directness
        guards treat it as a deliberate break, not a fresh reactive act.
    Conservative + tight: it ONLY acts on a genuine stall (K reactive acts AND zero progress), so a
    normal multi-objection call that advances each turn is never cut short. R37: copy + mutate the
    Decision, never construct a fresh one. This is the unified replacement for the old
    _recent_objection_handles / _BREAKTHROUGH_OBJECTION_HANDLES special case (now reachable here too):
    a relentless objector handled K times with the objection never clearing is exactly a no-progress
    reactive run, so the breakthrough close falls out of the general rule.

    STICKY (ISSUE 2 — the dana_11 death-march): a soft forced-commit the prospect DECLINES must NOT be
    re-offered every time the call re-stalls. respond() carries belief.meta["loop_break_fired"] once a
    soft commit has fired; if the call STALLS AGAIN after that (another K reactive no-progress turns
    following the declined commit), this is the SECOND break — we ESCALATE to a firm honest terminal
    (escalate, marked loop_terminal so escalate_only_if_warranted leaves it alone) instead of offering
    the same soft close once more. One soft commit, then escalate-and-stop; never re-loop.
    """
    if decision.act not in _REACTIVE_ACTS:
        return decision  # already committing (close/escalate/disqualify) — nothing to break

    # ESCALATE-AND-STAY: once the breaker has issued its terminal escalate for this loop
    # (loop_terminal_fired latched by respond()), the loop decision is FINAL — any subsequent reactive
    # turn re-escalates immediately (no need to re-accumulate another full K-stall). This keeps the
    # call terminally escalated instead of relapsing into the reactive death-march the moment the
    # escalate act reset the recent-acts run. (The runtime normally ends the call on escalation_imminent;
    # this guarantees the brain itself won't resume answering if the call continues.)
    if bool(belief.meta.get("loop_terminal_fired")):
        out = decision.copy()
        out.act = "escalate"
        out.target_slot = None
        out.tier = None
        out.concession = None
        out.meta = dict(out.meta)
        out.meta["loop_break"] = True
        out.meta["loop_terminal"] = True
        out.rationale = (
            "break_no_progress_loop: loop already escalated terminally — staying escalated rather than "
            "relapsing into the reactive loop"
        )
        belief.escalation_imminent = True
        return out

    if not _stalled_reactive_run(belief):
        return decision  # fewer than K reactive acts, or a commit broke the run — no stall
    if _made_progress_since_prior(belief):
        return decision  # something advanced this window — a healthy call, not a loop

    # STICKY ESCALATION: a soft forced-commit already fired earlier and the call re-stalled (the prospect
    # declined it and the agent relapsed into the loop). Don't re-offer the same soft close — give a firm
    # honest terminal that asks for the decision and stops. Marked loop_terminal so escalate_only_if_
    # warranted treats it as warranted (it is — we already tried the soft commit) and does NOT redirect
    # it back into another selling/answer turn (which would resume the death-march).
    if bool(belief.meta.get("loop_break_fired")):
        out = decision.copy()
        out.act = "escalate"
        out.target_slot = None
        out.tier = None
        out.concession = None
        out.meta = dict(out.meta)
        out.meta["loop_break"] = True
        out.meta["loop_terminal"] = True
        out.rationale = (
            f"break_no_progress_loop: STALLED AGAIN after a prior soft commit ({_LOOP_BREAK_K} more "
            "reactive turns, no progress) — firm honest terminal (escalate) instead of re-offering the "
            "same soft close; one soft commit then stop, never re-loop"
        )
        belief.escalation_imminent = True
        return out

    # FIRST break — we are genuinely stalled. Force a soft commit, lowest-friction first.
    objection_open = bool(belief.active_objection)
    tier = _closeable_tier(belief, config, breakthrough=objection_open)
    if tier is not None:
        # A PAID close that would be BLIND (no discovery + no readiness) is down-tiered to the FREE
        # 'callback' rung: close_floor would veto a blind paid close anyway, but a loop must still BREAK
        # — so we commit to the no-cost next step (a callback) instead of bouncing back to a loop turn.
        # callback keeps the buy-gate pure (the no-budget rung) and is exempt from close_floor.
        if tier in _PAID_CLOSE_TIERS and _paid_close_is_blind(belief, config):
            tier = _FREE_LADDER_TIER
        out = decision.copy()
        out.act = "attempt_close"
        out.tier = tier
        out.target_slot = None
        out.concession = None
        out.meta = dict(out.meta)
        out.meta["loop_break"] = True
        out.rationale = (
            f"break_no_progress_loop: {_LOOP_BREAK_K} reactive turns with no progress — forcing a "
            f"commit (attempt_close@{tier}) instead of looping"
        )
        return out

    # Not closeable: a genuine escalation reason short-circuits to a real hand-off.
    if _escalation_reason(decision, belief, config, history) is not None:
        out = decision.copy()
        out.act = "escalate"
        out.target_slot = None
        out.tier = None
        out.concession = None
        out.meta = dict(out.meta)
        out.meta["loop_break"] = True
        out.meta["loop_terminal"] = True
        out.rationale = (
            "break_no_progress_loop: stalled with no progress and a genuine escalation reason — "
            "handing off instead of looping"
        )
        belief.escalation_imminent = True
        return out

    # Neither closeable nor escalatable: give an HONEST bottom-line + a concrete next step (answer the
    # open question with the facts on hand, or state plainly what we can't do and offer the next step).
    # NOT another loop turn — tagged so it's observable and not re-counted as a fresh reactive act.
    out = decision.copy()
    out.act = "answer_via_kb"
    out.target_slot = None
    out.tier = None
    out.meta = dict(out.meta)
    out.meta["loop_break"] = True
    out.rationale = (
        f"break_no_progress_loop: {_LOOP_BREAK_K} reactive turns with no progress and not closeable — "
        "giving an honest bottom-line + a concrete next step instead of looping"
    )
    return out


# --- loop-breaker bookkeeping (maintained by respond.py, consumed by the breaker) -------------

def progress_snapshot(belief: BeliefState) -> dict[str, Any]:
    """Capture the progress-relevant state of `belief` for the loop-breaker's next-turn comparison.

    respond() stamps this on the NEW belief's meta["progress_snapshot"] AFTER each turn; on the next
    turn break_no_progress_loop compares the (DST-updated) current belief to it to detect PROGRESS (a
    slot newly locked / an objection cleared / purchase_intent risen). Kept here so the snapshot shape
    is owned by the same module that reads it — respond.py just calls this, never hard-codes the keys."""
    return {
        "locked_slots": _locked_slot_count(belief),
        "active_objection": belief.active_objection,
        "purchase_intent": float(belief.drivers.get("purchase_intent", _NEUTRAL_PRIOR)),
    }


def record_recent_act(belief: BeliefState, act: Optional[str]) -> None:
    """Append the GATED act to belief.meta["recent_acts"], trimmed to the breaker's K-act window.

    respond() calls this with the final gated act each turn so break_no_progress_loop sees the trailing
    run of acts. Trimmed to _LOOP_BREAK_K so the list never grows unbounded and the window is exactly
    what the breaker inspects. A None act (defensive) is ignored. Mutates belief in place; copy()
    carries meta to the next turn's input."""
    if not act:
        return
    recent = list(belief.meta.get("recent_acts", []))
    recent.append(str(act))
    belief.meta["recent_acts"] = recent[-_LOOP_BREAK_K:]


# --- close_floor (CB-29: no premature/pushy close before real discovery) -----------------------

# Paid commitment-ladder rungs that REQUIRE earned discovery before they may fire. The free
# 'callback' rung is intentionally absent: offering a no-cost future follow-up is never pushy, even
# early (and offer_low_commitment_on_budget legitimately steers a budget-refuser straight to it).
_PAID_CLOSE_TIERS = frozenset({"consultation", "trial", "enrollment"})


# The neutral driver prior — purchase_intent at/below this means the prospect has NOT signaled any
# buying readiness yet, so a paid close with zero discovery is the agent closing BLIND (the defect).
_NEUTRAL_PRIOR = 0.5


def _paid_close_is_blind(belief: BeliefState, config: AgentConfig) -> bool:
    """The close_floor blind-close predicate, factored out so break_no_progress_loop can reuse it
    (no duplication): a PAID close is BLIND when NO non-budget discovery slot is filled AND
    purchase_intent has NOT risen above the neutral prior. The free 'callback' rung is never blind."""
    required = _required_discovery_slots(config)
    filled = sum(1 for s in required if belief.slot_confidence(s) >= _LOCK_CONFIDENCE)
    has_discovery = filled >= int(_threshold(config, "discovery_slots_before_close"))
    signals_readiness = float(belief.drivers.get("purchase_intent", 0.0)) > _NEUTRAL_PRIOR
    return not (has_discovery or signals_readiness)


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

    # The close stands unless the agent is closing BLIND: no discovery AND no readiness signal.
    if not _paid_close_is_blind(belief, config):
        return decision

    out = decision.copy()
    # Back off to value-building / answering, never silence: keep advancing discovery instead.
    out.act = "answer_via_kb" if belief.open_question else "pitch"
    out.tier = None
    out.target_slot = None
    out.rationale = (
        "close_floor: closing blind (no discovery slot, no readiness signal); "
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


def _price_context_live(belief: BeliefState) -> bool:
    """CB-68: is the conversation actually IN price talk this turn? True when the caller's last act
    is a price inquiry, a price/affordability objection is open, or a firm affordability refusal has
    been recorded. Only then is an LLM-proposed `concession` meaningful — outside this context a
    concession fraction is a hallucination and must be stripped before gates act on it."""
    if belief.last_user_act == "price_inquiry":
        return True
    if (belief.active_objection or "") in ("price", "affordability"):
        return True
    try:
        if int((belief.meta or {}).get("affordability_refusals", 0)) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


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
    trigger. de_escalate runs right after the first escalation check (CB-37): hostility or extreme bail
    hands off gracefully before any gate can propose pressure. Between them: skip_known ->
    no_repeat_discovery (CB-35) -> establish_who_first (CB-34) -> address_direct_input ->
    must_clear_objection -> price_gate -> advance_to_close -> close_floor -> pushiness_cap ->
    break_no_progress_loop (SAFEGUARD 1: a stalled K-reactive-no-progress run that survived the close/
    pushiness back-offs -> a forced commit) -> no_authority_prefers_callback (CB-50: a no-authority
    escalate -> a callback offer) ->
    escalate_only_if_warranted (CB-50: ANY surviving triggerless escalate -> a callback offer on a
    gatekeeper read, else a non-terminal selling act — the agent keeps selling instead of bailing) ->
    offer_low_commitment_on_budget (last, so the free-callback offer has final say). The closing
    escalation re-check leaves a callback close intact (no trigger) but still catches genuine ones.
    """
    # CB-68: GROUND the LLM-proposed concession before anything reads it (the concession analogue of
    # CB-67's human_request grounding). A concession only means something inside live price talk —
    # the caller is asking about price or a price/affordability objection is open. Outside that, the
    # policy LLM has hallucinated a discount fraction (seen live: concession>band proposed on a
    # scheduling turn — "Thursday works, what do you need?" — which terminally escalated a healthy
    # close). Strip it so neither escalation_triggers nor price_gate acts on a phantom discount.
    if decision.concession is not None and not _price_context_live(belief):
        decision = decision.copy()
        decision.concession = None

    # Extreme moments dominate: nothing else matters if we must defer. The short-circuit fires only on
    # a GENUINE escalation REASON (over-band concession / human request / compliance / low-confidence
    # streak) — NOT merely because the LLM already proposed `escalate`. CB-50: an LLM escalate with no
    # genuine trigger (the no-authority gatekeeper) must flow into the chain so no_authority_prefers_
    # callback can down-tier it; the closing escalation re-check still catches any genuine trigger.
    if _escalation_reason(decision, belief, config, history) is not None:
        escalated = escalation_triggers(decision, belief, config, history=history)
        return escalated

    # CB-37: hostility or EXTREME bail short-circuits to a graceful hand-off BEFORE anything else can
    # propose pressure or a discovery re-ask — the call is over, disengage rather than keep selling.
    # Short-circuit only on an ACTUAL de-escalation REASON (hostility / extreme bail) — not merely
    # because the LLM already proposed `escalate` (CB-50: that no-authority escalate must reach the
    # chain so no_authority_prefers_callback can down-tier it).
    if _hostile_or_extreme_bail(belief, config) is not None:
        deescalated = de_escalate(decision, belief, config)
        if deescalated.act == "escalate":
            belief.escalation_imminent = True
            return deescalated

    d = skip_known(decision, belief, config)
    # CB-35: drop a re-ask of an answered/declined/over-asked discovery slot (HARD no-repeat guard).
    d = no_repeat_discovery(d, belief, config)
    # CB-34: don't presume a learner exists — a learner-specific ask first establishes who it's for.
    d = establish_who_first(d, belief, config)
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
    # SAFEGUARD 1: ONE general progress-based loop-breaker. If the last K gated acts were all REACTIVE
    # with NO progress (slot lock / objection clear / intent rise), force a COMMIT — a close if
    # closeable, else a genuine escalation, else an honest bottom-line. Subsumes the old objection-
    # deadlock special case AND catches the free-form re-ask/re-offer loops the slot/objection guards
    # missed. Placed AFTER close_floor + pushiness_cap so it sees the FINAL reactive act those gates may
    # have backed a blind/over-aggressive close DOWN to (a warm-but-no-discovery prospect that bounces
    # advance_to_close's close back to a pitch is exactly the silent loop the breaker must catch). The
    # breaker itself down-tiers a blind PAID forced close to the free callback rung, so it never closes
    # blind — close_floor protection is preserved without re-running it.
    d = break_no_progress_loop(d, belief, config, history=history)
    # CB-50: a no-authority gatekeeper escalate (LLM-proposed, no genuine trigger) is down-tiered to a
    # free callback offer for the real decision-maker — don't waste a human on a lead who can't decide.
    # Placed late so it sees the (possibly rewritten) act; the final escalation re-check below leaves a
    # callback close untouched (it trips no escalation reason), while a GENUINE trigger still escalates.
    d = no_authority_prefers_callback(d, belief, config, history=history)
    # CB-50: ANY surviving triggerless escalate is the agent BAILING — redirect it. A gatekeeper read
    # (now incl. the cheap 3rd-party "for my <relative>" phrase) books a callback; an otherwise-
    # qualified prospect keeps SELLING (answer their open question, else pitch) so it can close on a
    # later turn instead of terminally handing off. A GENUINE reason still escalates (re-checked below).
    d = escalate_only_if_warranted(d, belief, config, history=history)
    # LAST override: a budget-constrained prospect gets the free callback rung (help + future meeting),
    # not a loop or a release — placed here so must_clear_objection/pushiness don't undo the offer.
    d = offer_low_commitment_on_budget(d, belief, config)

    # Re-check escalation on the (possibly rewritten) decision so a gate-introduced concession or
    # state change can't escape the extreme-deferral guarantee.
    return escalation_triggers(d, belief, config, history=history)

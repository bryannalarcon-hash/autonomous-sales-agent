# Hybrid Dialogue State Tracker (plan U3; docs/belief-state-schema.md "the belief update").
# `update(belief, last_agent_act, user_utterance, llm_client)` runs three factored sub-updates and
# returns a NEW BeliefState (pure: the input is not mutated):
#   1. Deterministic slot extraction — structured/regex pulls grade/subject/budget/timeline/etc.
#      with a per-slot confidence, merged under confidence arbitration so a garbled low-signal turn
#      cannot overwrite a high-confidence slot (near-monotonic accumulation, per the schema).
#   2. LLM latent-driver delta-update — a rubric-driven prompt asks the llm_client for a JSON of
#      *deltas* per driver; deltas are clamped into the six known drivers (hallucinated keys
#      ignored; unparseable JSON leaves drivers unchanged — no corruption).
#   3. Deterministically-DERIVED trends — `<driver>_velocity` = level(t) - level(t-1), computed from
#      the logged trajectory, NOT emitted by the LLM (Markov-preserving, zero extra latency).
#   4. EFSM stage derivation — advance belief.stage (greeting->discovery->pitch->close, or objection)
#      deterministically from the updated belief, so the policy isn't perpetually stuck at 'greeting'.
# Pure functions, NO LiveKit imports — both the text self-play and the voice adapter call this.
# MIXED-MODEL: the driver/intent call is mechanical strict-JSON that runs EVERY turn, so it can ride a
# cheaper/faster model than the policy & NLG calls. Set env DST_MODEL (e.g. openai/gpt-5-nano) to
# override JUST this call's model; unset -> the shared llm_client default (AGENT_MODEL). The robust
# fallback already tolerates a weaker model's bad JSON (drivers unchanged + regex intent fallback).
from __future__ import annotations

import os
import re
from typing import Any, Optional

from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import LLMClient, Message

# Below this confidence a slot is "soft" and may be overwritten; at/above it the slot is "locked"
# and only a strictly-higher-confidence extraction can replace its value (confidence arbitration).
_LOCK_CONFIDENCE = 0.8

# --- Deterministic slot extractors ------------------------------------------------------------
# Each extractor returns (value, confidence) or None. Confidences are cold-start priors: explicit,
# unambiguous patterns score high; loose/inferred matches score low so they yield to firmer turns.

_GRADE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s*grade\b", re.I), 0.95),
    (re.compile(r"\bgrade\s*(\d{1,2})\b", re.I), 0.9),
]
_GRADE_WORDS = {
    "kindergarten": ("K", 0.9),
    "freshman": ("9", 0.7),
    "sophomore": ("10", 0.7),
    "junior": ("11", 0.7),
    "senior": ("12", 0.7),
}

# Subjects we recognize (controlled vocabulary — MultiWOZ-style ontology discipline, no free text).
_SUBJECT_PATTERNS = [
    (re.compile(r"\bSAT\s*math\b", re.I), "SAT math", 0.95),
    (re.compile(r"\bACT\s*math\b", re.I), "ACT math", 0.95),
    (re.compile(r"\bSAT\b", re.I), "SAT", 0.85),
    (re.compile(r"\bACT\b", re.I), "ACT", 0.85),
    (re.compile(r"\b(algebra|geometry|calculus|trigonometry|pre-?calculus)\b", re.I), None, 0.9),
    (re.compile(r"\b(math|reading|writing|english|science|chemistry|physics|biology)\b", re.I), None, 0.85),
]

# Budget: a dollar figure, optionally with a cadence. We capture the numeric amount.
# Two tiers: a $-/cadence-ANCHORED match (unambiguous money) is preferred over a bare number that
# merely appears alongside budget-context words — so "$300 a month" wins over the "11" in
# "11th grade" within the same sentence.
_BUDGET_ANCHORED_RE = re.compile(
    r"(?:\$\s*(\d{1,3}(?:,\d{3})*|\d+)"  # $300
    r"|(\d{1,3}(?:,\d{3})*|\d+)\s*(?:dollars?|bucks?)"  # 300 dollars
    r"|(\d{1,3}(?:,\d{3})*|\d+)\s*(?:/|per|a|each)\s*(?:month|week|hour|session))",  # 300 a month
    re.I,
)
_BUDGET_BARE_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})*|\d{2,})\b")
_BUDGET_CONTEXT_RE = re.compile(r"\b(budget|afford|spend|pay|cost|price)\b|\$", re.I)

# Timeline: a test/term date or a relative window.
_TIMELINE_RE = re.compile(
    r"\b(?:by\s+)?("
    r"next\s+(?:week|month|year|spring|fall|summer|semester|term)|"
    r"in\s+\d+\s+(?:weeks?|months?)|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"|spring|fall|summer|finals|midterms"
    r")\b",
    re.I,
)


def _extract_grade(text: str) -> Optional[tuple[Any, float]]:
    for pat, conf in _GRADE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1), conf
    low = text.lower()
    for word, (val, conf) in _GRADE_WORDS.items():
        if re.search(rf"\b{word}\b", low):
            return val, conf
    return None


def _extract_subject(text: str) -> Optional[tuple[Any, float]]:
    for pat, label, conf in _SUBJECT_PATTERNS:
        m = pat.search(text)
        if m:
            value = label if label is not None else m.group(1).lower()
            return value, conf
    return None


def _extract_budget(text: str) -> Optional[tuple[Any, float]]:
    # Prefer a $-/cadence-anchored amount (unambiguous money). This is what keeps "$300 a month"
    # from losing to the "11" in "11th grade" elsewhere in the same sentence.
    anchored = _BUDGET_ANCHORED_RE.search(text)
    if anchored:
        raw = next(g for g in anchored.groups() if g is not None).replace(",", "")
        try:
            return int(raw), 0.95
        except ValueError:
            return None
    # Fall back to a bare number ONLY when budget-context words are present, at lower confidence,
    # so it yields to any firmer extraction later.
    if not _BUDGET_CONTEXT_RE.search(text):
        return None
    bare = _BUDGET_BARE_RE.search(text)
    if not bare:
        return None
    try:
        return int(bare.group(1).replace(",", "")), 0.7
    except ValueError:
        return None


def _extract_timeline(text: str) -> Optional[tuple[Any, float]]:
    m = _TIMELINE_RE.search(text)
    if m:
        return m.group(1).lower(), 0.85
    return None


# Slot name -> extractor. Adding a slot is a one-line addition here.
_EXTRACTORS = {
    "grade_level": _extract_grade,
    "subject": _extract_subject,
    "budget": _extract_budget,
    "timeline": _extract_timeline,
}


def _extract_slots(belief: BeliefState, utterance: str) -> None:
    """Run each extractor and merge with confidence arbitration (mutates `belief` in place).

    Merge rule (near-monotonic accumulation): a new extraction replaces an existing slot only if
    the slot is not yet locked (existing confidence < _LOCK_CONFIDENCE) OR the new confidence is
    strictly higher than the locked one. This is what stops a garbled/contradictory turn from
    corrupting a high-confidence slot.
    """
    for name, extractor in _EXTRACTORS.items():
        result = extractor(utterance)
        if result is None:
            continue
        value, new_conf = result
        existing = belief.slots.get(name)
        if existing is None:
            belief.set_slot(name, value, new_conf)
            continue
        existing_conf = float(existing.get("confidence", 0.0))
        if existing_conf >= _LOCK_CONFIDENCE and new_conf <= existing_conf:
            # Locked and the new evidence is not stronger — keep the established value.
            continue
        if existing.get("value") == value:
            # Same value re-observed: take the higher confidence (reinforcement, never lower it).
            belief.set_slot(name, value, max(existing_conf, new_conf))
        elif new_conf > existing_conf:
            # Different value but stronger evidence — allowed to overwrite.
            belief.set_slot(name, value, new_conf)
        # else: weaker conflicting evidence — ignored.


# --- Deterministic intent / objection classification -----------------------------------------
# Why deterministic (no LLM): the policy is BLIND without these signals — it loops discovery and
# ignores direct questions/objections (a real prospect who asks "how much?" or "why not Khan
# Academy?" got re-asked "what grade?"). A keyword classifier is zero-latency, testable, and
# parity-clean (text + voice get the same signals). The objection taxonomy mirrors the config
# rebuttals + the kb_chunk `objections#*` corpus the agent grounds on.

# Objection cues -> the canonical objection key (priority order: first match wins). Word-boundaried.
_OBJECTION_PATTERNS: list[tuple[Any, str]] = [
    (re.compile(r"\b(khan|youtube|free|do it myself|on my own|google it|self[- ]?study)\b", re.I), "diy_free"),
    (re.compile(r"\b(my (husband|wife|spouse|partner)|other parent|check with|talk to my|discuss (it )?with|run it by)\b", re.I), "decision_maker"),
    (re.compile(r"\b(will it (really )?(work|help)|does it (really )?(work|help)|worth it|actually help|guarantee|skeptic)\b", re.I), "efficacy_doubt"),
    (re.compile(r"\b(right time|too early|too late|not sure (if )?now|maybe later|wait (a|until)|down the road)\b", re.I), "timing"),
    (re.compile(r"\b(expensive|too much|can'?t afford|pricey|out of (our|my) budget|cost(s|ly)? too)\b", re.I), "price"),
]
# A FIRM affordability refusal — "we genuinely can't / don't have the budget" — distinct from a
# soft "that's pricey" (which is handleable). The agent reframes value on the first couple; once the
# CUMULATIVE count crosses the gate threshold the prospect is genuinely unqualified and the agent
# should gracefully RELEASE (disqualify) rather than loop acknowledgments. Deliberately BROAD (an LLM
# prospect rephrases the refusal every turn), and COUNTED CUMULATIVELY (not as a consecutive streak)
# so the count still climbs across a refusal loop even when a turn's phrasing slips past the regex.
_AFFORDABILITY_REFUSAL_RE = re.compile(
    r"(can'?t afford|cannot afford|no budget|don'?t have (the |a )?budget|do not have (the |a )?budget|"
    r"don'?t think (i|we) (can|have) (the )?budget|out of (our|my) budget|not in (a |our |my )?(position|place) to afford|"
    r"can'?t swing|can'?t make this work|can'?t justify|can'?t fit (this|it) (in|into)|"
    r"tight on (money|budget|cash)|just can'?t (do|swing|afford|justify) (this|it)|"
    r"too expensive for (us|me)|just don'?t have (the |a |it )?(budget|money|funds))",
    re.IGNORECASE,
)
# A bare price/cost question (not necessarily an objection) -> opens price talk via the gate.
_PRICE_INQUIRY_RE = re.compile(r"\b(how much|what(?:'s| is| does it) cost|price|pricing|per (month|hour|session)|monthly|fees?|rates?)\b", re.I)
# An explicit ask for a human — routes to escalate via the escalation gate.
_HUMAN_REQUEST_RE = re.compile(r"\b(speak|talk|connect me) (to|with) (a |an )?(human|person|representative|rep|agent|advisor|someone)\b|real (person|human)", re.I)
# A question at all: a trailing '?' or a leading interrogative/request-to-explain.
_QUESTION_RE = re.compile(r"\?|\b(what|how|when|where|why|who|which|can you|could you|do you|are there|is there|tell me about)\b", re.I)


def _normalize_punct(text: str) -> str:
    """Fold smart quotes/apostrophes to ASCII so the keyword regexes (which use straight ' ) match
    LLM output. LLMs emit curly apostrophes (don't, can't) constantly; without this fold EVERY
    intent/objection/refusal pattern silently misses them — which left objections unhandled and the
    affordability-refusal count stuck at 0 (the agent looped instead of releasing)."""
    if not text:
        return text
    return (
        text.replace("’", "'").replace("‘", "'")
        .replace("“", '"').replace("”", '"')
    )


def _classify_intent(utterance: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Classify a prospect utterance into (last_user_act, active_objection, open_question).

    Deterministic + zero-latency. `active_objection` is one of the canonical keys (price /
    efficacy_doubt / diy_free / timing / decision_maker) when an objection cue fires. `open_question`
    carries the trimmed utterance when the prospect asked something (so NLG knows WHAT to answer and
    the price_gate's substring check can fire). `last_user_act` is the coarse intent the gates read:
    human_request > objection > price_inquiry > question > statement (None when nothing fires)."""
    text = _normalize_punct((utterance or "").strip())
    if not text:
        return None, None, None
    is_question = bool(_QUESTION_RE.search(text))
    open_question = text[:300] if is_question else None

    if _HUMAN_REQUEST_RE.search(text):
        return "human_request", None, open_question

    objection: Optional[str] = None
    for pat, key in _OBJECTION_PATTERNS:
        if pat.search(text):
            objection = key
            break
    # A bare price question with no complaint cue is a price INQUIRY, not a price objection.
    if objection is None and _PRICE_INQUIRY_RE.search(text):
        return "price_inquiry", None, open_question or text[:300]
    if objection is not None:
        # A price objection still counts as price talk the gate should open.
        return ("price_inquiry" if objection == "price" else "objection"), objection, open_question
    if is_question:
        return "question", None, open_question
    return None, None, None


# --- LLM latent-driver delta-update -----------------------------------------------------------

_DRIVER_RUBRIC = (
    "You are the latent-state tracker + intent classifier for a tutoring sales agent. Given the "
    "agent's last act and the prospect's latest utterance, return ONLY a JSON object with BOTH: "
    "(A) the six driver DELTAS, each a top-level key -> signed delta in [-1, 1] (0 = no change): "
    "trust (credibility/rapport; rises on acknowledgment, falls on pushback/price-shock), "
    "need_intensity (felt problem severity; rises with pain/stakes language), "
    "price_sensitivity (reaction to cost; rises on sticker-shock/comparison-shopping), "
    "urgency (felt time pressure; rises with deadline cues), "
    "purchase_intent (buying signals; MEASURED from explicit intent language), "
    "bail_risk (disengagement/walk-away; rises with terseness, dodges, or frustration); "
    "deltas are LEVEL changes only — no trends/velocities. "
    "(B) these classification fields about the prospect's LATEST utterance: "
    '"user_act": one of "question" | "price_inquiry" | "human_request" | "objection" | "statement"; '
    '"objection": the objection RAISED, one of "price" | "efficacy_doubt" | "diy_free" | "timing" | '
    '"decision_maker", or null if none; '
    '"open_question": the question the prospect is asking (short text) or null if they are not asking one; '
    '"affordability_refusal": true ONLY when the prospect is FIRMLY declining on cost/budget '
    '(e.g. "we just can\'t afford it", "there\'s no budget for this") — NOT merely asking the price '
    "or noting it's pricey; false otherwise. "
    "IMPORTANT: human_request means ONLY an explicit ask to speak with a human, person, "
    "representative, or advisor. Wanting to check with a spouse or partner, run it by someone, or "
    "think it over is NOT a human_request — classify that as the decision_maker objection. General "
    "wariness, skepticism, or hesitation is an objection or statement, NEVER human_request "
    "(mislabeling it forces a premature escalation). "
    "Judge meaning, not keywords (handle paraphrase, typos, and curly apostrophes)."
)


def _build_driver_messages(
    belief: BeliefState, last_agent_act: Optional[str], utterance: str
) -> list[Message]:
    """Assemble the rubric-driven chat messages for the driver delta-update call."""
    prior = {d: round(belief.drivers.get(d, 0.5), 3) for d in DRIVERS}
    user = (
        f"PRIOR_DRIVER_LEVELS: {prior}\n"
        f"LAST_AGENT_ACT: {last_agent_act or 'none'}\n"
        f"PROSPECT_UTTERANCE: {utterance!r}\n\n"
        "Return the JSON delta object now."
    )
    return [
        {"role": "system", "content": _DRIVER_RUBRIC},
        {"role": "user", "content": user},
    ]


# The intent/classification keys the LLM returns alongside the driver deltas. Their PRESENCE in the
# reply is how we know the LLM classified intent (vs a driver-only reply from a test mock / old prompt),
# which decides LLM-primary vs the regex fallback in update().
_INTENT_KEYS = ("user_act", "objection", "open_question", "affordability_refusal")
_VALID_OBJECTIONS = frozenset({"price", "efficacy_doubt", "diy_free", "timing", "decision_maker"})


def dst_model() -> Optional[str]:
    """The model slug for JUST the DST belief/intent call, or None to use the client default.

    Read PER CALL (not at import) so a .env loaded after import still takes effect. Set env DST_MODEL
    to ride a cheaper/faster model here (e.g. openai/gpt-5-nano) while policy/NLG stay on the agent
    default — the mixed-model latency/cost lever. Empty/unset -> None -> llm_client's default model."""
    return os.environ.get("DST_MODEL") or None


def _dst_call_opts() -> dict[str, Any]:
    """Per-call opts for the DST model JSON call.

    When a DST_MODEL override is set we ALSO pin a low reasoning effort: GPT-5-family slugs are
    REASONING models that, left at default, 'think' for ~18s on this rubric (measured) — far slower
    than Sonnet's ~3s; at minimal effort the SAME call is ~1.6s and still returns valid JSON. So the
    override is only a win with reasoning held low. Effort is tunable via DST_REASONING_EFFORT
    (default 'minimal'); set it to 'none' to omit the param. When NO override is set we pass nothing,
    leaving the default (Sonnet) path byte-for-byte unchanged."""
    model = dst_model()
    if not model:
        return {}
    opts: dict[str, Any] = {"model": model}
    effort = os.environ.get("DST_REASONING_EFFORT", "minimal").strip()
    if effort and effort.lower() != "none":
        opts["reasoning"] = {"effort": effort}
    return opts


async def _update_drivers(
    belief: BeliefState,
    last_agent_act: Optional[str],
    utterance: str,
    llm_client: LLMClient,
) -> Optional[dict[str, Any]]:
    """Ask the LLM for per-driver deltas + an intent classification; apply the deltas clamped (mutates
    `belief` in place) and RETURN the parsed intent fields (so update() routes objections/questions/
    refusals by MEANING, not brittle keywords).

    Returns the intent dict {user_act, objection, open_question, affordability_refusal} when the LLM
    actually classified (any _INTENT_KEY present), else None -> caller falls back to the deterministic
    keyword classifier. Robustness contract unchanged: an unparseable/non-dict reply or a call failure
    leaves drivers at their priors AND returns None (regex fallback); hallucinated/garbled values are
    dropped, never corrupting state.
    """
    messages = _build_driver_messages(belief, last_agent_act, utterance)
    try:
        deltas = await llm_client.complete_json(messages, **_dst_call_opts())
    except Exception:
        # FINDING 2: degrade on ANY driver-call failure, not just bad JSON. ValueError is the
        # malformed-JSON path; a real OpenRouter outage raises httpx.HTTPStatusError/RequestError
        # (outliving the client's bounded retry). Either way leave drivers at their priors — the
        # deterministic slot extraction + trend derivation still run — so the turn never crashes.
        return None
    if not isinstance(deltas, dict):
        return None
    for name, delta in deltas.items():
        if name not in belief.drivers:
            continue  # ignore hallucinated/intent keys; keep the six-driver set intact
        try:
            belief.apply_driver_delta(name, float(delta))
        except (TypeError, ValueError):
            continue  # skip a non-numeric delta rather than corrupt the level

    # The LLM intent classification (only when present — else the caller uses the regex fallback).
    if not any(k in deltas for k in _INTENT_KEYS):
        return None
    objection = deltas.get("objection")
    objection = objection if objection in _VALID_OBJECTIONS else None
    open_q = deltas.get("open_question")
    open_q = str(open_q)[:300] if isinstance(open_q, str) and open_q.strip() else None
    user_act = deltas.get("user_act")
    user_act = str(user_act) if isinstance(user_act, str) and user_act.strip() else None
    return {
        "user_act": user_act,
        "objection": objection,
        "open_question": open_q,
        "affordability_refusal": bool(deltas.get("affordability_refusal")),
    }


# --- Deterministically-derived trends ---------------------------------------------------------

def _derive_trends(prior: BeliefState, updated: BeliefState) -> None:
    """Compute `<driver>_velocity` = level(t) - level(t-1) for each driver (mutates `updated`).

    Velocity is a DETERMINISTIC function of the logged trajectory (the prior level vs the new
    level) — never emitted by the LLM. This keeps the state Markov while letting the policy see
    direction-of-travel (frame-stacking pattern, docs/belief-state-schema.md).
    """
    for d in DRIVERS:
        prev = float(prior.drivers.get(d, 0.5))
        now = float(updated.drivers.get(d, 0.5))
        updated.trends[f"{d}_velocity"] = round(now - prev, 6)


# --- EFSM stage derivation (Layer C) ----------------------------------------------------------
# The stage was previously FROZEN at 'greeting' (nothing ever assigned belief.stage), so the policy
# perpetually believed the call was in its opening and never entered a closing frame — a load-bearing
# bug behind the "agent answers questions forever, never closes" failure. Derive it deterministically
# from the belief each turn: greeting -> discovery -> pitch -> close, with a live objection surfacing
# as 'objection'. Heuristic framing for the policy prompt (the deterministic close ACTION is the
# advance_to_close gate); thresholds sit ABOVE the 0.5 neutral prior so a fresh belief stays in
# discovery rather than jumping straight to pitch/close.
_STAGE_PITCH_TRUST = 0.55
_STAGE_CLOSE_TRUST = 0.6
_STAGE_CLOSE_INTENT = 0.45
_STAGE_PITCH_SLOTS = 2


def _derive_stage(belief: BeliefState) -> str:
    """Map the current belief to an EFSM stage label (greeting/discovery/pitch/close/objection)."""
    if belief.turn_count <= 0:
        return "greeting"
    if belief.active_objection:
        return "objection"
    trust = float(belief.drivers.get("trust", 0.0))
    intent = float(belief.drivers.get("purchase_intent", 0.0))
    if trust >= _STAGE_CLOSE_TRUST and intent >= _STAGE_CLOSE_INTENT:
        return "close"
    known = sum(1 for s in belief.slots.values() if float(s.get("confidence", 0.0)) >= _LOCK_CONFIDENCE)
    if trust >= _STAGE_PITCH_TRUST or known >= _STAGE_PITCH_SLOTS:
        return "pitch"
    return "discovery"


# --- The per-turn hybrid update ---------------------------------------------------------------

async def update(
    belief: BeliefState,
    last_agent_act: Optional[str],
    user_utterance: str,
    llm_client: LLMClient,
    *,
    last_user_act: Optional[str] = None,
) -> BeliefState:
    """Run one hybrid belief update and return a NEW BeliefState (the input is not mutated).

    Order: deterministic slot extraction -> ONE LLM call (driver deltas + intent/objection/refusal
    classification) -> deterministic trend derivation -> meta bookkeeping (turn_count++, intent
    signals + the firm-refusal count). The LLM classifies intent by MEANING; a keyword classifier is
    the fallback when the call fails. The trend step reads the PRIOR levels (captured before the
    driver update), so velocity reflects this turn's actual change.
    """
    prior = belief  # keep a reference to the pre-update levels for trend derivation
    updated = belief.copy()

    # 1. Deterministic slots (confidence-arbitrated merge).
    _extract_slots(updated, user_utterance)

    # 2. LLM latent-driver deltas + intent classification in ONE call (clamped, robust to bad output).
    #    The LLM reads the nuance (objection / question / firm-budget-refusal — paraphrase, typos,
    #    curly apostrophes and all); the deterministic gates downstream still DECIDE. llm_intent is
    #    None on a call failure / driver-only reply -> we fall back to the keyword classifier so the
    #    turn always degrades gracefully (regex was the brittle PRIMARY before; it's now the fallback).
    llm_intent = await _update_drivers(updated, last_agent_act, user_utterance, llm_client)

    # 3. Deterministically-derived trends from the logged trajectory (prior -> updated levels).
    _derive_trends(prior, updated)

    # 4. Meta bookkeeping + intent/objection signals (LLM-primary, regex fallback). These fill the
    #    signals the policy/gates/NLG need to ADDRESS the prospect (objection, open question,
    #    price-inquiry, firm refusal); an explicit last_user_act passed in (an upstream NLU label) wins.
    updated.turn_count = prior.turn_count + 1
    if llm_intent is not None:
        classified_act = llm_intent["user_act"]
        objection = llm_intent["objection"]
        open_question = llm_intent["open_question"]
        refused = llm_intent["affordability_refusal"]
    else:
        classified_act, objection, open_question = _classify_intent(user_utterance)
        refused = bool(_AFFORDABILITY_REFUSAL_RE.search(_normalize_punct(user_utterance or "")))
    updated.last_user_act = last_user_act if last_user_act is not None else classified_act
    updated.active_objection = objection
    updated.open_question = open_question

    # Track the CUMULATIVE firm-affordability-refusal count in meta (carried across turns by copy()).
    # A refusal also counts as a live price objection so the FIRST ones get a value reframe
    # (handle_objection); once the count crosses the gate threshold the reframe has demonstrably not
    # landed and the agent disqualifies/releases. Cumulative (not reset each turn) so it survives the
    # prospect rephrasing the same refusal differently turn to turn.
    prior_refusals = int(prior.meta.get("affordability_refusal_count", 0))
    if refused:  # LLM-judged firm refusal (or the regex fallback when the LLM call failed)
        updated.meta["affordability_refusal_count"] = prior_refusals + 1
        if updated.active_objection is None:
            updated.active_objection = "price"
    else:
        updated.meta["affordability_refusal_count"] = prior_refusals  # cumulative — never reset down

    # Advance the EFSM stage from the (now-updated) belief so the policy knows WHERE the call is
    # (greeting -> discovery -> pitch -> close / objection) instead of being stuck at 'greeting'.
    updated.stage = _derive_stage(updated)

    return updated

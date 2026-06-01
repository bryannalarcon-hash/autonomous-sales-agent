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
# Pure functions, NO LiveKit imports — both the text self-play and the voice adapter call this.
from __future__ import annotations

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


# --- LLM latent-driver delta-update -----------------------------------------------------------

_DRIVER_RUBRIC = (
    "You are the latent-state tracker for a tutoring sales agent. Given the agent's last act and "
    "the prospect's latest utterance, estimate how each hidden driver should CHANGE this turn. "
    "Return ONLY a JSON object mapping driver name -> signed delta in [-1, 1] (0 = no change). "
    "Drivers: "
    "trust (credibility/rapport; rises on acknowledgment, falls on pushback/price-shock), "
    "need_intensity (felt problem severity; rises with pain/stakes language), "
    "price_sensitivity (reaction to cost; rises on sticker-shock/comparison-shopping), "
    "urgency (felt time pressure; rises with deadline cues), "
    "purchase_intent (buying signals; MEASURED from explicit intent language, not inferred from "
    "the other drivers), "
    "bail_risk (disengagement/walk-away; rises with terseness, dodges, or frustration). "
    "Estimate LEVELS' deltas only — do NOT emit trends or velocities."
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


async def _update_drivers(
    belief: BeliefState,
    last_agent_act: Optional[str],
    utterance: str,
    llm_client: LLMClient,
) -> None:
    """Ask the LLM for per-driver deltas and apply them clamped (mutates `belief` in place).

    Robustness contract: an unparseable / non-dict reply leaves all drivers at their priors (no
    corruption); hallucinated driver keys are ignored; non-numeric delta values are skipped.
    """
    messages = _build_driver_messages(belief, last_agent_act, utterance)
    try:
        deltas = await llm_client.complete_json(messages)
    except Exception:
        # FINDING 2: degrade on ANY driver-call failure, not just bad JSON. ValueError is the
        # malformed-JSON path; a real OpenRouter outage raises httpx.HTTPStatusError/RequestError
        # (outliving the client's bounded retry). Either way leave drivers at their priors — the
        # deterministic slot extraction + trend derivation still run — so the turn never crashes.
        return
    if not isinstance(deltas, dict):
        return
    for name, delta in deltas.items():
        if name not in belief.drivers:
            continue  # ignore hallucinated keys; keep the six-driver set intact
        try:
            belief.apply_driver_delta(name, float(delta))
        except (TypeError, ValueError):
            continue  # skip a non-numeric delta rather than corrupt the level


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

    Order: deterministic slot extraction -> LLM driver delta-update -> deterministic trend
    derivation -> meta bookkeeping (turn_count++, last_user_act). The trend step reads the PRIOR
    levels (captured before the driver update), so velocity reflects this turn's actual change.
    """
    prior = belief  # keep a reference to the pre-update levels for trend derivation
    updated = belief.copy()

    # 1. Deterministic slots (confidence-arbitrated merge).
    _extract_slots(updated, user_utterance)

    # 2. LLM latent-driver deltas (clamped, robust to bad output).
    await _update_drivers(updated, last_agent_act, user_utterance, llm_client)

    # 3. Deterministically-derived trends from the logged trajectory (prior -> updated levels).
    _derive_trends(prior, updated)

    # 4. Meta bookkeeping.
    updated.turn_count = prior.turn_count + 1
    if last_user_act is not None:
        updated.last_user_act = last_user_act

    return updated

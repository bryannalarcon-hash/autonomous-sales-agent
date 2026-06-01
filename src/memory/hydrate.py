# Cross-call per-lead memory hydration bridge (plan U6; R2/R7/R11/R42). The ONLY seam allowed to
# import BOTH src.core (belief_state) AND src.memory (store + schema) — it bridges the DB-free core
# to the Postgres store so src/core/ itself stays DB-free. At call start, hydrate_belief() looks up
# the lead by phone-hash and seeds a fresh BeliefState with the caller's known slots at HIGH
# confidence (>= the DST/gate lock confidence 0.8) so src.core.gates.skip_known treats them as known
# and the policy CONFIRMS rather than re-asks; prior objections are carried as context only (never
# forced into active_objection). At call end, persist_lead_after_call() upserts the final belief's
# filled slots (value-only) + outcome/voice/persona back to the store so a later call hydrates them.
from __future__ import annotations

from typing import Any, Optional

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.memory import store
from src.memory.schema import Lead, phone_hash

# Confidence at which hydrated (already-known, cross-call) slots are seeded. Equals the DST/gate
# lock confidence (src.core.dst._LOCK_CONFIDENCE / src.core.gates._LOCK_CONFIDENCE = 0.8): at/above
# this, src.core.gates.skip_known overrides an `ask` for the slot to `confirm_known`. Hydrated
# memory is treated as firmly-known so discovery does not waste a turn re-asking what we already have.
HYDRATED_SLOT_CONFIDENCE = 0.8


def seed_slots(
    belief: BeliefState,
    slots: dict[str, Any],
    confidence: float = HYDRATED_SLOT_CONFIDENCE,
) -> BeliefState:
    """Seed `belief` IN PLACE with `{name: value}` slots at `confidence`, returning it for chaining.

    Tolerates two stored slot shapes so it round-trips with persist_lead_after_call's value-only
    writes AND with any legacy `{value, confidence}` lead rows: a bare value is taken verbatim; a
    `{"value": ..., "confidence": ...}` dict has its inner value/confidence unwrapped (the stored
    confidence wins only if it is at/above the hydrate floor — we never DOWNgrade a known slot below
    the lock threshold, or skip_known would re-ask it). Slots with a None value are skipped.
    """
    for name, raw in (slots or {}).items():
        value, conf = _unwrap_slot(raw, confidence)
        if value is None:
            continue
        belief.set_slot(name, value, conf)
    return belief


def _unwrap_slot(raw: Any, floor: float) -> tuple[Any, float]:
    """Normalize a stored slot to (value, confidence). Accepts a bare value or a {value, confidence}
    dict; the returned confidence is never below `floor` so a hydrated slot always reads as known."""
    if isinstance(raw, dict) and "value" in raw:
        value = raw.get("value")
        stored_conf = raw.get("confidence")
        try:
            conf = max(float(stored_conf), floor) if stored_conf is not None else floor
        except (TypeError, ValueError):
            conf = floor
        return value, conf
    return raw, floor


async def hydrate_belief(raw_phone: Optional[str], config: AgentConfig) -> BeliefState:
    """Build the call-start BeliefState for `raw_phone`, hydrating any prior per-lead memory (U6).

    Anonymous / no-phone callers, and callers with no stored lead, get BeliefState.fresh() (full
    discovery — nothing is known yet). A returning caller (lead found by phone-hash) gets a fresh
    state SEEDED with their known slots at HIGH confidence (>= 0.8) so src.core.gates.skip_known
    confirms instead of re-asking; the caller's assigned (sticky) TTS voice and latest persona read
    are carried on the belief meta, and prior objections are recorded as CONTEXT (belief.meta
    "prior_objections") — deliberately NOT forced into active_objection, which only the live DST may
    set when an objection is raised THIS call. `config` is accepted for parity with the per-turn core
    signature and future config-driven seeding; the seed confidence is the gate lock threshold.
    """
    belief = BeliefState.fresh()
    if raw_phone is None:
        return belief
    try:
        phash = phone_hash(raw_phone)
    except (ValueError, TypeError):
        # No usable digits (anonymous / blocked caller id) -> fresh, full discovery.
        return belief

    lead = await store.get_lead_by_phone(phash)
    if lead is None:
        return belief  # NEW caller — nothing known, full discovery.

    seed_slots(belief, lead.slots)
    # Carry the sticky voice + last persona read + prior objections as CONTEXT only. These ride on
    # belief.meta so the policy/NLG can open with familiarity and the voice adapter can reuse the
    # sticky voice — without polluting Layer-A slots or setting active_objection (a live-turn signal).
    belief.meta["assigned_voice"] = lead.assigned_voice
    belief.meta["persona_read"] = lead.persona_read
    belief.meta["prior_objections"] = list(lead.prior_objections or [])
    belief.meta["last_outcome"] = lead.last_outcome
    belief.meta["returning_caller"] = True
    belief.meta["lead_phone_hash"] = phash
    return belief


async def persist_lead_after_call(
    raw_phone: Optional[str],
    belief: BeliefState,
    *,
    outcome: Optional[str] = None,
    assigned_voice: Optional[str] = None,
) -> Optional[Lead]:
    """Upsert the lead from the FINAL belief at call end so the next call hydrates the learned slots.

    Filled slots are written VALUE-ONLY ({name: value}) — the store merges them into the lead's
    existing slots (existing || new, new wins), so partial info accumulates across calls. The
    sticky voice / last outcome / latest persona read are passed through (the store's upsert keeps a
    prior non-None value when the incoming one is None, so omitting `assigned_voice` does not wipe a
    previously-assigned voice). Returns the merged Lead, or None for an anonymous / no-phone call
    (nothing to key the memory on — we never invent a phone-hash).
    """
    if raw_phone is None:
        return None
    try:
        phash = phone_hash(raw_phone)
    except (ValueError, TypeError):
        return None

    value_slots = _filled_slot_values(belief)
    persona_read = belief.meta.get("persona_read") if isinstance(belief.meta, dict) else None
    lead = Lead(
        phone_hash=phash,
        slots=value_slots,
        last_outcome=outcome,
        assigned_voice=assigned_voice,
        persona_read=persona_read,
    )
    return await store.upsert_lead_by_phone(lead)


def _filled_slot_values(belief: BeliefState) -> dict[str, Any]:
    """Project the belief's filled slots to a value-only {name: value} dict for lead storage.

    Slots with a None value are dropped (nothing learned). Confidence is intentionally NOT stored on
    the lead — it is a live-call artifact; re-hydration re-asserts the lock confidence (a remembered
    fact is, by definition, known). This keeps the lead body compact and free of per-call noise.
    """
    out: dict[str, Any] = {}
    for name, slot in (belief.slots or {}).items():
        if not isinstance(slot, dict):
            continue
        value = slot.get("value")
        if value is not None:
            out[name] = value
    return out

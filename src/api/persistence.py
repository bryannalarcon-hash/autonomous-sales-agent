# Demo-call persistence bridge (plan U2/U13/U14/U15) — turns a finished in-memory VoiceSession into
# the durable rows the Operate dashboard reads, so a live /api/chat call STOPS vanishing. Pure
# mapping + store calls, kept out of src.api.server so the FastAPI module stays thin and this stays
# unit-testable. Four jobs:
#   - episode_from_session(session, ...) builds a unified Episode from the committed turns + belief
#     trajectory, deriving the terminal OUTCOME + commitment-LADDER tier from the last committed agent
#     decision, stamping version+kb_version, persona, channel, lead_phone_hash, and
#     metrics["recorded"]; pure (no DB) so it is trivially testable.
#   - persist_call_live(session, ...) upserts an in_progress Episode per turn so the Live monitor
#     can query calls WHILE they are happening. Uses a stable episode_id + created_at passed by the
#     caller (both must stay constant across upserts). Anonymous (lead_phone_hash=None). Swallows DB
#     errors so a persistence hiccup never crashes the call path. CB-31: an optional `live_partial`
#     carries the in-progress (not-yet-committed) agent reply being streamed to TTS, written to
#     metrics["live_partial"] so the monitor can render words filling in before the turn commits.
#     CB-33: cohort defaults to "live" but reads LIVE_PERSIST_COHORT — the DB-gated tests set it to
#     "test" so their live upserts are excluded from the operator monitor (no phantom active call).
#     CB-44 (timing observability): an optional `turn_timings` (committed agent turn_id -> the 4 ms
#     numbers, stamped by the voice worker) is written to metrics["turn_timings"] so operate can attach
#     per-turn timing onto episode_detail/summary; an optional `live_timing` (first_token_ms + a stream-
#     start stamp for the active turn) is written to metrics["live_timing"] so live_snapshot can surface
#     the in-flight time-to-first-token + streaming elapsed. Both are metadata only (no decision change).
#   - persist_call_end(session, ...) saves it via the store in FK-safe order (lead -> episode ->
#     escalations) and persists per-lead memory (persist_lead_after_call, phone-hash only). The lead
#     key is SINGLE-SOURCED server-side: raw_phone -> schema.phone_hash(raw_phone); a bound-hash call
#     with no raw_phone upserts a minimal lead so the episode FK target always exists; anonymous ->
#     lead_phone_hash=None. A client-supplied phone_hash is never trusted as the key on its own.
#   - the outcome/ladder/escalation derivation helpers the above share.
# Raw phone is NEVER stored (only the phone-hash key). Collaborators: src.memory.store / .hydrate /
# .schema, src.voice.session.VoiceSession, src.config.settings. NO LiveKit imports.
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from src.config.settings import AgentConfig
from src.memory.schema import Episode, Turn

logger = logging.getLogger(__name__)

# Last-committed agent decision act -> (terminal outcome label, commitment-ladder tier int). Mirrors
# src.api.labels' tier scale (0 none < 1 callback < 2 consult < 3 trial < 4 enrollment) and the
# OUTCOME_LABEL keys, so a persisted demo call renders correctly on the dashboard with no retrofit.
_CLOSE_TIER_OUTCOME: dict[str, tuple[str, int]] = {
    "enrollment": ("enrolled", 4),
    "trial": ("trial_booked", 3),
    "consultation": ("consult_booked", 2),
    "callback": ("callback_booked", 1),
}


def derive_outcome(last_act: Optional[str], last_tier: Optional[str]) -> tuple[str, int, bool, bool]:
    """Map the last committed agent decision to (outcome, ladder_tier, qualified, escalated).

    attempt_close maps by its ladder tier to the matching booked/enrolled outcome (qualified=True);
    escalate -> "escalated" with escalated=True; disqualify -> "disqualified" (qualified=False); any
    other / no terminal act -> "in_progress" at tier 0 (the call ended mid-conversation). Deterministic
    so the demo call lands in the same outcome buckets the dashboard P3/P4 already aggregate.
    """
    if last_act == "attempt_close":
        outcome, tier = _CLOSE_TIER_OUTCOME.get(last_tier or "", ("consult_booked", 2))
        return outcome, tier, True, False
    if last_act == "escalate":
        return "escalated", 0, False, True
    if last_act == "disqualify":
        return "disqualified", 0, False, False
    return "in_progress", 0, False, False


def _last_agent_decision(turns: list[Turn]) -> tuple[Optional[str], Optional[Turn]]:
    """The act of the most recent agent turn carrying a decision (and that turn), or (None, None)."""
    for turn in reversed(turns):
        if turn.speaker == "agent" and turn.decision:
            return turn.decision, turn
    return None, None


def episode_from_session(
    session: Any,
    *,
    config: AgentConfig,
    channel: str,
    phone_hash: Optional[str] = None,
    cohort: str = "live",
    persona: Optional[str] = None,
    episode_id: Optional[str] = None,
    last_tier: Optional[str] = None,
    created_at: Optional[datetime] = None,
    outcome_override: Optional[str] = None,
    turn_timings: Optional[dict[int, dict[str, Any]]] = None,
) -> Episode:
    """Build a unified Episode from a finished VoiceSession's committed state (pure — no DB).

    Carries the captured turns (already PII-scrubbed at commit) + their belief trajectory, derives the
    terminal outcome + ladder tier from the last committed agent decision (escalate/disqualify/close),
    stamps version+kb_version from the running config, records the channel ("text" for the console /
    "voice" for the worker), the lead phone-hash key (raw phone NEVER stored), persona, cohort, the
    escalated flag, and metrics including ["recorded"]=session.recorded + turn_count. `last_tier`
    overrides the close tier when the caller tracked it out-of-band (the schema Turn carries no tier).
    `created_at`, when passed (the stable call-start stamped at session creation), is preserved on the
    Episode so the final /end upsert keeps the call's true start AND records metrics["duration_ms"]
    (now − start) so the Live monitor + Review show how long the call ran. `outcome_override` lets a
    caller force a terminal outcome for an end that reached no terminal ACT (e.g. a mid-call hang-up →
    "abandoned"/"released") so the call no longer lingers as in_progress.
    """
    state = session.state
    turns: list[Turn] = list(state.turns)
    last_act, last_turn = _last_agent_decision(turns)
    # Prefer the explicitly-passed tier; else fall back to none (derive_outcome defaults safely).
    outcome, ladder_tier, qualified, escalated = derive_outcome(last_act, last_tier)
    # An explicit override only applies when the conversation reached NO terminal act (outcome is the
    # in_progress sentinel) — never overrides a real close/escalate/disqualify the brain actually made.
    if outcome_override is not None and outcome == "in_progress":
        outcome = outcome_override

    stamp = config.stamp()
    persona_name = persona or getattr(getattr(config, "persona", None), "name", None)

    metrics: dict[str, Any] = {
        "recorded": bool(getattr(session, "recorded", False)),
        "turn_count": len(turns),
    }
    # duration_ms (now − stable start): only when created_at is known. Clamped at 0 (clock skew safe).
    if created_at is not None:
        elapsed_ms = int((datetime.now(timezone.utc) - created_at).total_seconds() * 1000)
        metrics["duration_ms"] = max(0, elapsed_ms)
    # CB-44: stamp the per-turn timing (committed agent turn_id -> the 4 ms numbers) so operate can
    # surface "timing" on episode_detail/summary. JSON keys must be strings (jsonb round-trip), so the
    # int turn_id is stringified here and parsed back as int in operate._turn_timings_from_metrics.
    if turn_timings:
        metrics["turn_timings"] = {str(tid): t for tid, t in turn_timings.items()}

    return Episode(
        episode_id=episode_id or f"ep-{uuid.uuid4().hex}",
        turns=turns,
        outcome=outcome,
        ladder_tier=ladder_tier,
        qualified=qualified,
        version=stamp.get("version", ""),
        kb_version=stamp.get("kb_version", ""),
        channel=channel,
        lead_phone_hash=phone_hash,
        persona=persona_name,
        cohort=cohort,
        escalated=escalated,
        metrics=metrics,
        **({"created_at": created_at} if created_at is not None else {}),
    )


async def persist_call_live(
    session: Any,
    *,
    config: AgentConfig,
    channel: str,
    created_at: datetime,
    episode_id: str,
    live_partial: Optional[str] = None,
    live_timing: Optional[dict[str, Any]] = None,
    turn_timings: Optional[dict[int, dict[str, Any]]] = None,
) -> Optional[Episode]:
    """Upsert an in_progress Episode each turn so the Live monitor can query calls mid-call.

    Builds an Episode with outcome="in_progress", lead_phone_hash=None (anonymous — avoids the lead
    FK requirement AND any PII during the call), cohort="live", and the SAME episode_id + created_at
    passed on every call (so consecutive upserts land on the same row via save_episode's ON CONFLICT).
    Accumulated turns (with belief snapshots) and turn_count reflect the state AFTER the current turn.
    The store is imported lazily so this module stays DB-free at import time. Swallows/logs DB errors
    like persist_call_end does — a persistence hiccup must NEVER crash the live call path. Returns the
    Episode on success or None on error.

    CB-31: `live_partial` is the in-progress agent reply text being STREAMED to TTS for the turn that
    has NOT committed yet — when set it is written to metrics["live_partial"] so operate.live_snapshot
    can surface it and the monitor renders the words filling in before the turn commits. It is metadata
    only (NOT a committed Turn); when absent (the per-turn / connect-time upsert) the key is omitted, so
    the next upsert after the turn commits clears it.

    CB-33: the cohort is normally "live" (a real call). The DB-gated tests that drive THIS function
    against the shared dev Postgres set LIVE_PERSIST_COHORT="test" so their in_progress upserts are
    tagged "test" — operate._is_active excludes that cohort, so a pytest run can't flash a phantom
    "active" call on the operator monitor. Prod leaves the env unset -> "live" (unchanged behavior).

    CB-44 (timing): `turn_timings` (committed agent turn_id -> the 4 ms numbers) is written to
    metrics["turn_timings"] so the live call's already-committed turns carry timing too; `live_timing`
    (the active streaming turn's first_token_ms + stream-start stamp) is written to metrics["live_timing"]
    so operate.live_snapshot can surface the in-flight time-to-first-token + streaming elapsed. Both are
    metadata only; absent (a per-turn / connect-time upsert) the keys are omitted so a committed/quiet
    state shows no live_timing (the next upsert clears it).
    """
    try:
        from src.memory import store

        state = session.state
        turns: list[Turn] = list(state.turns)
        stamp = config.stamp()
        persona_name = getattr(getattr(config, "persona", None), "name", None)

        metrics: dict[str, Any] = {
            # live_heartbeat = the moment of THIS upsert (last activity). _is_active (operate.py) reads
            # it to keep a call "live" only while fresh — so a real call stays live for its whole run
            # (the beat advances every turn) while an abandoned partial goes stale and drops out.
            # duration_ms = now − stable start, so the Live monitor's running clock advances each turn
            # (the per-second UI tick interpolates between these; clamped ≥0 for clock-skew safety).
            "turn_count": len(turns),
            "live_heartbeat": datetime.now(timezone.utc).isoformat(),
            "duration_ms": max(0, int((datetime.now(timezone.utc) - created_at).total_seconds() * 1000)),
        }
        # CB-31: only carry the partial when one was passed (a mid-stream upsert) so the per-turn /
        # connect-time upsert leaves the key absent and the monitor stops showing it once committed.
        if live_partial:
            metrics["live_partial"] = live_partial
        # CB-44: the active streaming turn's in-flight timing (only on a mid-stream upsert) so the live
        # monitor shows time-to-first-token + elapsed; absent on the per-turn/connect upsert (cleared
        # once the turn commits). The committed turns' timings (so the just-committed turn's numbers are
        # queryable on the live call too) round-trip with int turn_id stringified for jsonb.
        if live_timing:
            metrics["live_timing"] = live_timing
        if turn_timings:
            metrics["turn_timings"] = {str(tid): t for tid, t in turn_timings.items()}

        # CB-33: "live" in prod; the DB-gated tests set LIVE_PERSIST_COHORT="test" so their upserts are
        # tagged "test" (excluded from the live monitor by operate._is_active) — never a phantom call.
        cohort = os.environ.get("LIVE_PERSIST_COHORT", "live")

        episode = Episode(
            episode_id=episode_id,
            turns=turns,
            outcome="in_progress",
            ladder_tier=0,
            qualified=False,
            version=stamp.get("version", ""),
            kb_version=stamp.get("kb_version", ""),
            channel=channel,
            lead_phone_hash=None,
            persona=persona_name,
            cohort=cohort,
            escalated=False,
            metrics=metrics,
            created_at=created_at,
        )
        await store.save_episode(episode)
        return episode
    except Exception as exc:
        logger.warning("persist_call_live failed (call continues): %s", type(exc).__name__)
        return None


async def persist_call_end(
    session: Any,
    *,
    config: AgentConfig,
    channel: str,
    phone_hash: Optional[str] = None,
    raw_phone: Optional[str] = None,
    cohort: str = "live",
    escalation_logs: Optional[list[Any]] = None,
    last_tier: Optional[str] = None,
    episode_id: Optional[str] = None,
    created_at: Optional[datetime] = None,
    outcome_override: Optional[str] = None,
    turn_timings: Optional[dict[int, dict[str, Any]]] = None,
) -> Episode:
    """Persist a finished demo call: lead -> episode -> escalations, plus per-lead memory (U2/U14/U15).

    `created_at` (the stable call-start stamped at session creation) is threaded to episode_from_session
    so the final upsert preserves the call's true start AND records metrics["duration_ms"] (call length).
    `outcome_override` forces a terminal outcome ("abandoned"/"released") for an end that reached no
    terminal act (a mid-call hang-up) so the call no longer lingers as in_progress / invisible in Calls.

    SINGLE-SOURCED LEAD KEY (FK-safe): the episode's lead_phone_hash must reference an EXISTING lead
    row (migrations/001_init.sql), so the key is derived/guaranteed server-side — a client-supplied
    `phone_hash` is NEVER trusted as the lead key on its own:
      - raw_phone present -> the SERVER hash schema.phone_hash(raw_phone) is the key; persist_lead_after_
        call writes the lead under it (raw phone never stored), and the episode references THAT hash
        (any client-supplied phone_hash is ignored as the key — server-derived wins).
      - raw_phone absent but a phone_hash was bound -> upsert a MINIMAL lead by that hash so the FK
        target exists, then key the episode by it (a bound-hash call no longer dangles -> no FK error).
      - neither -> lead_phone_hash is None (anonymous call), no lead written.
    Order (FK-safe): lead first (the episode's FK target), then save_episode (the escalations' FK
    target), then the buffered escalation_logs re-pointed onto this episode_id. episode_id, when
    supplied, is the stable live_episode_id stamped at session creation — passing it makes the final
    save upsert the same row as all in_progress live upserts (no orphan/duplicate). Returns the saved
    Episode. The store is imported lazily so DB-free imports don't drag in asyncpg.
    """
    from src.memory import store
    from src.memory.hydrate import persist_lead_after_call
    from src.memory.schema import Lead
    from src.memory.schema import phone_hash as hash_phone

    final_belief = session.state.belief
    # 1. Single-source the lead key + guarantee the FK target exists BEFORE the episode is saved.
    effective_hash: Optional[str] = None
    if raw_phone is not None:
        # Server derives the key (never trust a client hash) + remembers the caller from the belief.
        lead = await persist_lead_after_call(
            raw_phone,
            final_belief,
            outcome=None,  # outcome is set on the episode; the lead keeps its last_outcome via merge
            assigned_voice=getattr(session, "assigned_voice", None),
        )
        # persist_lead_after_call returns None only for an unusable phone (no digits); fall back to the
        # server hash when it stored a lead so the episode references the exact PK that was written.
        if lead is not None:
            effective_hash = lead.phone_hash
        else:
            try:
                effective_hash = hash_phone(raw_phone)
            except (ValueError, TypeError):
                effective_hash = None
    elif phone_hash:
        # A bound-hash call with no raw_phone: upsert a minimal lead so the episode FK is satisfied.
        await store.upsert_lead_by_phone(Lead(phone_hash=phone_hash))
        effective_hash = phone_hash

    episode = episode_from_session(
        session,
        config=config,
        channel=channel,
        phone_hash=effective_hash,
        cohort=cohort,
        last_tier=last_tier,
        episode_id=episode_id,
        created_at=created_at,
        outcome_override=outcome_override,
        turn_timings=turn_timings,  # CB-44: persist the per-turn timing onto the final Episode.
    )
    # 2. Episode (the FK target for escalations).
    await store.save_episode(episode)

    # 3. Escalations captured during the call now have a valid episode_id to FK to.
    for log in escalation_logs or []:
        log.episode_id = episode.episode_id
        await store.save_escalation(log)

    return episode

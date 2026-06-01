# Demo-call persistence bridge (plan U2/U13/U14/U15) — turns a finished in-memory VoiceSession into
# the durable rows the Operate dashboard reads, so a live /api/chat call STOPS vanishing. Pure
# mapping + store calls, kept out of src.api.server so the FastAPI module stays thin and this stays
# unit-testable. Three jobs:
#   - episode_from_session(session, ...) builds a unified Episode from the committed turns + belief
#     trajectory, deriving the terminal OUTCOME + commitment-LADDER tier from the last committed agent
#     decision, stamping version+kb_version, persona, channel, lead_phone_hash, and
#     metrics["recorded"]; pure (no DB) so it is trivially testable.
#   - persist_call_end(session, ...) saves it via the store in FK-safe order (lead -> episode ->
#     escalations) and persists per-lead memory (persist_lead_after_call, phone-hash only).
#   - the outcome/ladder/escalation derivation helpers the above share.
# Raw phone is NEVER stored (only the phone-hash key). Collaborators: src.memory.store / .hydrate /
# .schema, src.voice.session.VoiceSession, src.config.settings. NO LiveKit imports.
from __future__ import annotations

import uuid
from typing import Any, Optional

from src.config.settings import AgentConfig
from src.memory.schema import Episode, Turn

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
) -> Episode:
    """Build a unified Episode from a finished VoiceSession's committed state (pure — no DB).

    Carries the captured turns (already PII-scrubbed at commit) + their belief trajectory, derives the
    terminal outcome + ladder tier from the last committed agent decision (escalate/disqualify/close),
    stamps version+kb_version from the running config, records the channel ("text" for the console /
    "voice" for the worker), the lead phone-hash key (raw phone NEVER stored), persona, cohort, the
    escalated flag, and metrics including ["recorded"]=session.recorded + turn_count. `last_tier`
    overrides the close tier when the caller tracked it out-of-band (the schema Turn carries no tier).
    """
    state = session.state
    turns: list[Turn] = list(state.turns)
    last_act, last_turn = _last_agent_decision(turns)
    # Prefer the explicitly-passed tier; else fall back to none (derive_outcome defaults safely).
    outcome, ladder_tier, qualified, escalated = derive_outcome(last_act, last_tier)

    stamp = config.stamp()
    persona_name = persona or getattr(getattr(config, "persona", None), "name", None)

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
        metrics={
            "recorded": bool(getattr(session, "recorded", False)),
            "turn_count": len(turns),
        },
    )


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
) -> Episode:
    """Persist a finished demo call: lead -> episode -> escalations, plus per-lead memory (U2/U14/U15).

    FK-safe order (migrations/001_init.sql): the lead row must exist before an episode that references
    its phone-hash, and the episode must exist before any escalation_log row that FKs to it. So we:
      1. persist_lead_after_call(raw_phone, final belief, ...) — upserts the lead from the FINAL belief
         (value-only slots, phone-hash key; raw phone never stored). This also creates the lead row the
         episode's lead_phone_hash references. Skipped for anonymous callers.
      2. save_episode(episode_from_session(...)) — the call appears in /operate calls + KPIs.
      3. re-point + save any escalation_logs captured DURING the call onto this episode_id, so they
         show in /operate/escalations (their FK target now exists).
    Returns the saved Episode. The store is imported lazily so DB-free imports don't drag in asyncpg.
    """
    from src.memory import store
    from src.memory.hydrate import persist_lead_after_call

    final_belief = session.state.belief
    # 1. Lead first (creates the FK target for the episode's lead_phone_hash) + remembers the caller.
    if raw_phone is not None:
        await persist_lead_after_call(
            raw_phone,
            final_belief,
            outcome=None,  # outcome is set on the episode; the lead keeps its last_outcome via merge
            assigned_voice=getattr(session, "assigned_voice", None),
        )

    episode = episode_from_session(
        session,
        config=config,
        channel=channel,
        phone_hash=phone_hash,
        cohort=cohort,
        last_tier=last_tier,
    )
    # 2. Episode (the FK target for escalations).
    await store.save_episode(episode)

    # 3. Escalations captured during the call now have a valid episode_id to FK to.
    for log in escalation_logs or []:
        log.episode_id = episode.episode_id
        await store.save_escalation(log)

    return episode

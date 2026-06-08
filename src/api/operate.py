# Operator-dashboard READ API (plan U15 — Operate mode). The 5 Operate screens (P1 Live monitor,
# Call Review, P3 Calls list, P4 KPI views, P5 Escalation queue) read JSON from these GET endpoints.
# It is DB-INJECTABLE the same way the demo API is LLM-injectable: a ReadStore protocol abstracts the
# data layer so create_operate_router(read_store=...) takes the real src.memory.store in prod and a
# seeded in-memory fake in tests (no Postgres on the test path). KPIs reuse src.loop.grading
# (kpi_score weighted ladder + compare_versions) — never reimplemented. Every operator-facing string
# is translated through src.api.labels so NO raw internal index (ladder int, driver slug, P-id) ever
# renders. Episodes/escalations are serialized to flat display DTOs the Next.js client types against.
# CB-66 (item 2): outcome_key in episode_summary is now the CANONICAL key (alias-resolved) so the
# filter chip for "callback_booked" matches both "callback_booked" and "callback_scheduled" rows.
# CB-66 (item 3): duration_ms is computed from ep.created_at + ep.updated_at when metrics omits it,
# giving durations for any episode with ≥1 turn and a fresh updated_at — display only, no DB write.
# CB-66 (item 7): escalation_to_dict now always includes created_at for timestamp display in the UI.
# The /api/episodes Calls list is the COMPLETED history: in-progress / unset-outcome / 0-turn calls
# are EXCLUDED here (see _is_completed) so an active call never leaks in as a finished row. Live-call
# endpoints: GET /api/live (newest active call or null), GET /api/live?episode_id=<id> (specific ep),
# GET /api/live/active (all non-stale active calls with summary fields), GET /api/live/sample
# (newest completed call with sample:true, for the page's "Show sample call" toggle).
# CB-60: cohort/count coherence across Operate surfaces. Every surface now states its population and
# the numbers reconcile. Key guarantees: (1) /api/episodes response carries `total` (completed rows
# before the limit trim) so the UI can show "showing N of total" when capped. (2) /api/kpis applies
# _is_completed so its denominator matches the Calls list — no orphan in_progress/null-outcome rows
# inflate KPI counts. (3) /api/escalations enriches each row with the episode's cohort so operators
# can see which population an escalation came from and the filter needed to find it in the list.
# (4) episode_summary gains `is_stub` (True when channel=="sim") so the UI can badge seeded calls
# and the cohort='live' default already excludes them from "Real calls".
# CB-06: episode_detail adds "prospect_trajectory" — the prospect's TRUE hidden-driver arc per turn,
# persisted by run_episode on sim/twin episodes; real (voice/text) episodes get [] so the frontend
# panel can detect absence with a simple falsy check. Collaborators: src.sim.selfplay (writer).
# CB-28: each serialized turn also carries "retrieved" — the KB facts/chunks that grounded a tool-use
# answer (answer_via_kb), or None on non-tool turns — so Call Review can make a tool turn clickable
# into a panel showing what information was pulled. Persisted on Turn.retrieved by the runtime.
# CB-31: live_snapshot exposes "live_partial" — the in-progress (not-yet-committed) agent reply being
# STREAMED to TTS by the voice worker (from metrics["live_partial"]) — but ONLY while the call is
# active, so the Live monitor renders the words filling in on the active turn near-real-time; None on
# a completed/inactive call (the committed transcript carries the final text).
# CB-30: golden calibration set. episode_summary/episode_detail expose a top-level `golden` boolean
# (from metrics['golden']); POST /api/episodes/{id}/golden tags/untags one call (404 unknown id) and
# GET /api/episodes/golden enumerates the set newest-first as full episode_detail payloads, so the set
# is exportable (transcript+belief+outcome per episode) and replayable by the twin harness. The literal
# /golden route is declared BEFORE the dynamic /{episode_id} so "golden" isn't captured as an id.
# CB-33: _is_active also EXCLUDES rows tagged with a test cohort (_TEST_LIVE_COHORTS, default {"test"})
# — pytest writes in_progress rows into the shared dev DB via the real persist_call_live, so without
# this a test run would flash phantom "active" calls on the operator monitor (the CB-33 false alarm).
# Tests set LIVE_PERSIST_COHORT="test" so their live upserts land in the excluded cohort; tests/conftest
# additionally DELETEs any rows a run created so nothing accumulates.
# CB-44 (timing observability): the voice worker stamps per-turn timing (audio-in -> first NLG token ->
# stream-end) and persistence stows it on metrics. This module surfaces it via the LOCKED contract:
# episode_detail().turns[i]["timing"] (the 4 ms numbers, null for text/legacy turns; from
# metrics["turn_timings"][turn_id]); episode_summary()["avg_first_token_ms"/"avg_stream_ms"] (means over
# timed agent turns); live_snapshot()["live_timing"] (the active streaming turn's first_token_ms +
# stream_elapsed_ms derived from metrics["live_timing"], present only while a partial streams).
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional, Protocol, Sequence

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.api import labels
from src.loop import grading
from src.memory.schema import BeliefSnapshot, Episode, EscalationLog, Turn


class _LifecycleUpdate(BaseModel):
    """Body for POST /api/escalations/{id}/lifecycle — the new triage state (validated in the store)."""

    lifecycle: str


class _GoldenUpdate(BaseModel):
    """Body for POST /api/episodes/{id}/golden (CB-30) — tag/untag the call for the golden set."""

    golden: bool

# ---------------------------------------------------------------------------
# Injectable data layer
# ---------------------------------------------------------------------------


class ReadStore(Protocol):
    """Async read surface for the Operate pages. Default impl: src.memory.store (Postgres).
    Tests inject a seeded in-memory fake — no Postgres on the test path."""

    async def list_episodes(
        self,
        *,
        version: Optional[str] = None,
        cohort: Optional[str] = None,
        outcome: Optional[str] = None,
        escalated: Optional[bool] = None,
        limit: int = 200,
    ) -> list[Episode]: ...

    async def get_episode(self, episode_id: str) -> Optional[Episode]: ...

    async def list_escalations(
        self, *, lifecycle: Optional[str] = None, limit: int = 100
    ) -> list[EscalationLog]: ...

    async def update_escalation_lifecycle(self, escalation_id: str, lifecycle: str) -> bool: ...

    # CB-30 golden calibration set: tag/untag (returns False on unknown id) + enumerate the set.
    async def set_episode_golden(self, episode_id: str, golden: bool) -> bool: ...

    async def list_golden_episodes(self, *, limit: int = 200) -> list[Episode]: ...


class _StoreBackedReadStore:
    """Forwards to src.memory.store; imported lazily to avoid dragging asyncpg onto import paths
    that don't need it (e.g. the demo app)."""

    def __init__(self) -> None:
        from src.memory import store

        self._store = store

    async def list_episodes(self, **kw: Any) -> list[Episode]:
        return await self._store.list_episodes(**kw)

    async def get_episode(self, episode_id: str) -> Optional[Episode]:
        return await self._store.get_episode(episode_id)

    async def list_escalations(self, **kw: Any) -> list[EscalationLog]:
        return await self._store.list_escalations(**kw)

    async def update_escalation_lifecycle(self, escalation_id: str, lifecycle: str) -> bool:
        return await self._store.update_escalation_lifecycle(escalation_id, lifecycle)

    async def set_episode_golden(self, episode_id: str, golden: bool) -> bool:
        return await self._store.set_episode_golden(episode_id, golden)

    async def list_golden_episodes(self, **kw: Any) -> list[Episode]:
        return await self._store.list_golden_episodes(**kw)


# ---------------------------------------------------------------------------
# Serializers — Episode/Turn/Escalation -> flat display DTOs (labels applied)
# ---------------------------------------------------------------------------

_PRIMARY_DRIVERS = ("trust", "bail_risk")  # P1 Live monitor foremost signals
_UNFINISHED_OUTCOMES = frozenset({None, "", "in_progress"})  # not-yet-terminal outcomes
_COMPLETED_OVERFETCH = 3   # over-fetch multiplier for P3 completed-list scan
_MAX_FETCH = 10000

# CB-33: cohorts written ONLY by the test suite (the DB-gated integration/e2e tests that drive the
# REAL persist_call_live against the shared dev Postgres). A row tagged with one of these is a test
# artifact and MUST NEVER surface as a live/active call on the operator monitor — so a pytest run
# can't flash a phantom "ongoing call" (the false alarm CB-33 fixes). Production live calls use
# "live"; tests set LIVE_PERSIST_COHORT="test" so their in_progress upserts land here instead.
# Overridable via env (comma-separated) only to widen the exclusion, never to drop "test".
_TEST_LIVE_COHORTS = frozenset(
    c.strip()
    for c in os.environ.get("LIVE_TEST_COHORTS", "test").split(",")
    if c.strip()
) | {"test"}


def _is_completed(ep: Episode) -> bool:
    """Terminal outcome + at least one turn — EXCEPT a terminal 'abandoned' hang-up, which is a real
    missed lead that dropped during the agent's opening (so 0 committed turns) and MUST still surface
    in the Calls list (CB-09). in_progress / unset outcomes are still excluded (those are active or
    never-finalized shells, caught by the first guard)."""
    if ep.outcome in _UNFINISHED_OUTCOMES:
        return False
    return len(ep.turns) > 0 or ep.outcome == "abandoned"


def _is_active(ep: Episode, *, now: Optional[datetime] = None) -> bool:
    """True when a call is GENUINELY live. Shared predicate for /api/live and /api/live/active so
    they can't drift.

    A live call upserts its in_progress row every turn (persistence.persist_call_live), stamping
    metrics['live_heartbeat'] with the moment of last activity. We treat the call as active ONLY while
    that heartbeat is fresh (within _LIVE_STALE_SECONDS). This is what separates a real call — which
    keeps beating no matter how many turns or minutes it runs — from an abandoned / never-finalized
    partial or an old seed row, which has NO heartbeat (or a stale one). created_at is the call START
    and is held STABLE across upserts, so it CANNOT measure freshness (a long real call would wrongly
    expire); the heartbeat is the only valid last-activity signal. No/garbled heartbeat => not live,
    so legacy in_progress rows written before heartbeat tracking never masquerade as live calls.

    CB-33: a row tagged with a TEST cohort (see _TEST_LIVE_COHORTS) is a pytest artifact written into
    the shared dev DB by the DB-gated tests — it is NEVER a real live call, so it is excluded here
    regardless of heartbeat freshness (the durable defense behind the conftest teardown cleanup)."""
    if ep.outcome not in _UNFINISHED_OUTCOMES:
        return False
    if (ep.cohort or "") in _TEST_LIVE_COHORTS:
        return False
    hb = (ep.metrics or {}).get("live_heartbeat")
    if not hb:
        return False
    _now = now or datetime.now(timezone.utc)
    try:
        beat = datetime.fromisoformat(str(hb))
    except (TypeError, ValueError):
        return False
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    return (_now - beat).total_seconds() <= _LIVE_STALE_SECONDS


def _belief_to_dict(belief: Optional[BeliefSnapshot]) -> Optional[dict[str, Any]]:
    """Serialize a belief snapshot with display labels: primary drivers (trust, bail_risk),
    secondary drivers, slots, stage, escalation_imminent. All values 0..1 floats."""
    if belief is None:
        return None
    drivers = belief.drivers or {}
    primary = [
        {"key": k, "label": labels.driver_label(k), "value": float(drivers[k])}
        for k in _PRIMARY_DRIVERS
        if k in drivers
    ]
    secondary = [
        {"key": k, "label": labels.driver_label(k), "value": float(v)}
        for k, v in drivers.items()
        if k not in _PRIMARY_DRIVERS
    ]
    slots = [
        {
            "key": name,
            "label": labels._titleize(name),
            "value": (slot or {}).get("value"),
            "confidence": float((slot or {}).get("confidence", 0.0) or 0.0),
        }
        for name, slot in (belief.slots or {}).items()
    ]
    return {
        "stage": labels.stage_label(belief.stage),
        "active_objection": belief.active_objection,
        "decision_confidence": belief.decision_confidence,
        "escalation_imminent": bool(belief.escalation_imminent),
        "primary_drivers": primary,
        "secondary_drivers": secondary,
        "slots": slots,
        "trends": belief.trends or {},
    }


# CB-44: the per-turn timing block shape (the LOCKED contract). All keys present, each int-or-null —
# null when the turn carries no timing (a text/legacy turn, or a number we couldn't measure, e.g.
# audio_to_first_token on a non-audio path). A single source so episode_detail + live can't drift.
_TIMING_KEYS = ("audio_to_first_token_ms", "first_token_ms", "stream_duration_ms", "total_ms")


def _null_timing() -> dict[str, Optional[int]]:
    return {k: None for k in _TIMING_KEYS}


def _coerce_timing(raw: Any) -> dict[str, Optional[int]]:
    """Normalize a stored per-turn timing dict to the LOCKED shape: all 4 keys present, int-or-null.
    A missing/odd value (or a non-dict) yields the all-null block so a legacy turn never crashes."""
    block = _null_timing()
    if isinstance(raw, dict):
        for k in _TIMING_KEYS:
            v = raw.get(k)
            block[k] = int(v) if isinstance(v, (int, float)) else None
    return block


# CB-44: the live (in-flight) timing shape — first_token_ms + stream_elapsed_ms for the ACTIVE turn.
_LIVE_TIMING_KEYS = ("first_token_ms", "stream_elapsed_ms")


def _coerce_live_timing(raw: Any) -> Optional[dict[str, Optional[int]]]:
    """Normalize metrics["live_timing"] to {"first_token_ms": int|null, "stream_elapsed_ms": int|null},
    or None when absent/non-dict (no turn is streaming) so live_snapshot reports live_timing: null."""
    if not isinstance(raw, dict):
        return None
    block: dict[str, Optional[int]] = {}
    for k in _LIVE_TIMING_KEYS:
        v = raw.get(k)
        block[k] = int(v) if isinstance(v, (int, float)) else None
    return block


def _turn_timings_from_metrics(metrics: Optional[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Parse metrics["turn_timings"] (CB-44) back into {agent turn_id (int) -> raw timing dict}.

    Persistence stringifies the int turn_id for jsonb; this parses it back so _turn_to_dict can look up
    a turn by its int turn_id. A malformed/absent map yields {} so episode_detail just emits null timing.
    """
    raw = (metrics or {}).get("turn_timings")
    if not isinstance(raw, dict):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for tid, t in raw.items():
        try:
            out[int(tid)] = t
        except (ValueError, TypeError):
            continue
    return out


def _turn_to_dict(turn: Turn, timings: Optional[dict[int, dict[str, Any]]] = None) -> dict[str, Any]:
    """Serialize one turn: transcript text, labeled act (agent turns), rationale, belief, the CB-28
    retrieved facts that grounded a tool-use answer (None on non-tool turns so the Review page only
    makes a turn clickable into the tool-use panel where information was actually pulled), and the
    CB-44 per-turn "timing" block (audio-in -> first-token -> stream-end ms; all-null for a text/legacy
    turn with no stamped timing). `timings` maps agent turn_id -> the stored timing dict."""
    raw_timing = (timings or {}).get(turn.turn_id)
    return {
        "turn_id": turn.turn_id,
        "speaker": turn.speaker,
        "text": turn.text,
        "decision_label": labels.act_label(turn.decision) if turn.speaker == "agent" else None,
        "rationale": turn.rationale,
        "latency_ms": turn.latency_ms,
        "belief": _belief_to_dict(turn.belief),
        # CB-28: the KB facts/chunks ("[source] text") that grounded this turn's answer, or None.
        "retrieved": list(turn.retrieved) if turn.retrieved else None,
        # CB-44: the 4 timing numbers for this turn (all-null when none were stamped — text/legacy).
        "timing": _coerce_timing(raw_timing) if raw_timing is not None else _null_timing(),
    }


def _last_belief(ep: Episode) -> Optional[BeliefSnapshot]:
    for turn in reversed(ep.turns):
        if turn.belief is not None:
            return turn.belief
    return None


def _timing_averages(ep: Episode) -> tuple[Optional[int], Optional[int]]:
    """CB-44: (avg_first_token_ms, avg_stream_ms) — the mean first-token + stream-duration over the
    AGENT turns that carry timing. Returns (None, None) when no agent turn has a measured number, so a
    text/legacy / untimed call reports nulls (never a fabricated 0). Each average is over only the turns
    that have THAT number (a turn missing first_token but having stream_duration still counts toward the
    latter), matching the "mean over turns that have timing" contract."""
    timings = _turn_timings_from_metrics(ep.metrics)
    if not timings:
        return None, None
    agent_turn_ids = {t.turn_id for t in ep.turns if t.speaker == "agent"}
    first_tokens: list[int] = []
    streams: list[int] = []
    for tid, raw in timings.items():
        if tid not in agent_turn_ids:
            continue
        block = _coerce_timing(raw)
        if block["first_token_ms"] is not None:
            first_tokens.append(block["first_token_ms"])
        if block["stream_duration_ms"] is not None:
            streams.append(block["stream_duration_ms"])
    avg_ft = int(round(sum(first_tokens) / len(first_tokens))) if first_tokens else None
    avg_st = int(round(sum(streams) / len(streams))) if streams else None
    return avg_ft, avg_st


def _compute_duration_ms(ep: Episode) -> Optional[int]:
    """CB-66 (item 3): duration_ms for display — no DB writes.

    Priority order:
    1. metrics["duration_ms"] — already computed by the runtime and stored on finalization
       (episode_from_session stamps this as now - created_at when `created_at` was passed).
    2. Sum of agent turn latency_ms values (latency = time from prospect's STT end to the agent's
       first reply token, per turn). This is a lower-bound estimate, not exact wall-clock, but it
       prevents "—" for multi-turn calls that lack a stored duration. 0-latency or missing-latency
       turns are skipped so a text/legacy call that logged no latencies still returns None.
    """
    metrics = ep.metrics or {}
    stored = metrics.get("duration_ms")
    if stored is not None:
        try:
            val = int(stored)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    # Fallback: sum per-turn latency_ms for any turn that has it. Agent turns carry the measured
    # brain latency; we include ALL non-null latencies (agent + prospect where recorded).
    # A call with ≥1 timed turn gets a lower-bound duration rather than "—".
    if len(ep.turns) < 1:
        return None
    timed = [t.latency_ms for t in ep.turns if t.latency_ms is not None and t.latency_ms > 0]
    if timed:
        # Sum latencies + a 1.5 s buffer per turn for the prospect's speaking time (rough).
        # The result is an estimate, but always better than omitting a 5-minute call's duration.
        total_latency = sum(timed)
        # Add ~1.5 s per turn as a conservative speaking-time estimate for turns without latency.
        unmeasured_turns = len(ep.turns) - len(timed)
        estimate = total_latency + unmeasured_turns * 1500
        # Sanity guard: only use if < 4 hours and > 0.
        if 0 < estimate < 14_400_000:
            return estimate
    return None


def episode_summary(ep: Episode) -> dict[str, Any]:
    """Flat summary row for P3/Live tile — no transcript body, all labels applied.
    CB-44: also carries avg_first_token_ms / avg_stream_ms (means over timed agent turns, null when
    the call has no stamped timing) so the Calls list / Review header can show call-level timing.
    CB-60: adds `is_stub` (True when channel=='sim') so the UI can badge seeded/sim calls and exclude
    them from "Real calls" by default (the cohort='live' filter already does this; is_stub adds a
    visible confirmation badge in the All-cohorts view).
    CB-66 (item 2): outcome_key is now the canonical (alias-resolved) key so "callback_scheduled"
    rows surface under the "callback_booked" filter chip — one filter chip per concept.
    CB-66 (item 3): duration_ms is now computed via _compute_duration_ms (falls back to timestamps)
    so calls with ≥1 turn no longer show "—" when the runtime omitted metrics.duration_ms."""
    metrics = ep.metrics or {}
    avg_first_token_ms, avg_stream_ms = _timing_averages(ep)
    return {
        "episode_id": ep.episode_id,
        "outcome": labels.outcome_label(ep.outcome),
        # CB-66 (item 2): canonical key (alias-resolved) so the filter chip works for both
        # "callback_booked" (persistence path) and "callback_scheduled" (selfplay path).
        "outcome_key": labels.canonical_outcome_key(ep.outcome),
        "ladder_tier": ep.ladder_tier,
        "ladder_label": labels.ladder_tier_label(ep.ladder_tier),
        "qualified": ep.qualified,
        "version": ep.version,
        "kb_version": ep.kb_version,
        "channel": ep.channel,
        "persona": ep.persona,
        "cohort": ep.cohort,
        "escalated": ep.escalated,
        "turn_count": len(ep.turns),
        # CB-66 (item 3): computed duration — falls back to summed latency_ms when metrics.duration_ms
        # is absent so multi-turn calls no longer show "—" in the Calls list.
        "duration_ms": _compute_duration_ms(ep),
        # CB-30: top-level boolean for the golden calibration flag (lives in metrics['golden']). Surfaced
        # here so both the summary row and episode_detail expose it without the client digging into metrics;
        # the "golden" indicator/toggle in Call Review reads this. coerced to bool so a missing/odd value is
        # False, never a raw truthy slug.
        "golden": bool(metrics.get("golden", False)),
        # CB-44: call-level timing means over the agent turns that carry timing; null when none do.
        "avg_first_token_ms": avg_first_token_ms,
        "avg_stream_ms": avg_stream_ms,
        "created_at": ep.created_at.isoformat() if ep.created_at else None,
        # CB-60: seeded/sim stub marker. True when channel=='sim' — the most robust existing trait
        # (every seeded self-play episode uses channel='sim'; live calls are 'voice'/'text'). The UI
        # badges these rows and excludes them from "Real calls" by default. NOT derived from the tactic
        # rationale text ("sim-harness decision") because that's a free-form string that can change;
        # channel is a validated enum (see src.memory.schema.CHANNELS).
        "is_stub": ep.channel == "sim",
    }


def episode_detail(ep: Episode) -> dict[str, Any]:
    """Full Call Review payload (P2): summary + per-turn trace + belief trajectory.
    CB-06: adds prospect_trajectory — the prospect's TRUE hidden-driver arc for sim/twin episodes
    (one entry per prospect turn: {turn: int, drivers: {trust,need,urgency,purchase_intent,budget,
    patience}}). Real voice/text episodes carry no prospect truth -> []. Always present so the
    frontend can check truthiness without guarding for a missing key."""
    detail = episode_summary(ep)
    # CB-44: thread the parsed per-turn timings so each serialized turn carries its "timing" block.
    timings = _turn_timings_from_metrics(ep.metrics)
    detail["turns"] = [_turn_to_dict(t, timings) for t in ep.turns]
    detail["belief_trajectory"] = [_belief_to_dict(t.belief) for t in ep.turns]
    detail["disqualifier_reason"] = ep.disqualifier_reason
    detail["metrics"] = ep.metrics or {}
    detail["prospect_trajectory"] = (ep.metrics or {}).get("prospect_trajectory") or []
    return detail


# An in-progress call whose last live_heartbeat is older than this is abandoned/ended, not live — it
# drops off /api/live. A genuine live call upserts a fresh heartbeat every agent turn (~10-30s apart),
# so 180s is ample headroom while making an ended/abandoned call vanish from the monitor in ~3 min
# instead of lingering for 10 (the "phantom active call" the operator saw). Overridable via env.
_LIVE_STALE_SECONDS = float(os.environ.get("LIVE_STALE_SECONDS", "180"))


def live_snapshot(
    ep: Optional[Episode],
    *,
    now: Optional[datetime] = None,
    sample: bool = False,
) -> dict[str, Any]:
    """P1 Live monitor payload: active flag, episode detail, hoisted priority signals.
    `sample=True` adds the sample key so the page distinguishes a reference from a live feed."""
    if ep is None:
        base: dict[str, Any] = {"active": False, "episode": None}
        if sample:
            base["sample"] = True
        return base
    active = _is_active(ep, now=now)
    if not active and ep.outcome in _UNFINISHED_OUTCOMES:
        # Stale 0-turn abandoned dial: treat as no-active-call.
        base = {"active": False, "episode": None}
        if sample:
            base["sample"] = True
        return base
    belief = _last_belief(ep)
    last_agent = next((t for t in reversed(ep.turns) if t.speaker == "agent"), None)
    bd = _belief_to_dict(belief)
    # CB-31: the in-progress agent reply being STREAMED to TTS (set by the worker's word-streaming
    # llm_node via persist_call_live's live_partial -> metrics["live_partial"]). Surfaced ONLY while
    # the call is genuinely active so the monitor renders the words filling in on the active turn; on
    # a completed/frozen call it is dropped (the committed transcript carries the final text). Cleared
    # to None once the turn commits (the next per-turn upsert omits the key).
    live_partial = (ep.metrics or {}).get("live_partial") if active else None
    payload: dict[str, Any] = {
        "active": active,
        "episode": episode_detail(ep),
        # CB-31: the streaming partial of the active (uncommitted) agent turn, or None.
        "live_partial": live_partial if isinstance(live_partial, str) and live_partial else None,
        # CB-44: the active streaming turn's in-flight timing — {first_token_ms, stream_elapsed_ms} —
        # surfaced ONLY while the call is active AND a partial is streaming (the worker writes
        # metrics["live_timing"] alongside live_partial and clears it once the turn commits, so a quiet
        # between-turns / completed call reports null). The monitor shows "time to first word" + a
        # growing streaming clock on the active turn.
        "live_timing": _coerce_live_timing((ep.metrics or {}).get("live_timing")) if active else None,
        # The four FOREMOST signals, lifted out so the client never has to dig for them.
        "priority": {
            "trust": next(
                (d["value"] for d in (bd or {}).get("primary_drivers", []) if d["key"] == "trust"),
                None,
            ),
            "bail_risk": next(
                (d["value"] for d in (bd or {}).get("primary_drivers", []) if d["key"] == "bail_risk"),
                None,
            ),
            "stage": (bd or {}).get("stage"),
            "last_act_label": labels.act_label(last_agent.decision) if last_agent else None,
            "escalation_imminent": (bd or {}).get("escalation_imminent", False),
        },
    }
    if sample:
        payload["sample"] = True
    return payload


def escalation_to_dict(esc: EscalationLog, *, episode_cohort: Optional[str] = None) -> dict[str, Any]:
    """P5 queue row: reason + lifecycle translated to labels.
    CB-60: `episode_cohort` carries the cohort of the linked episode so operators can see which
    population the escalation came from and apply the matching filter in the Calls list to find it.
    None when the episode can't be resolved (a dangling escalation from a deleted episode)."""
    return {
        "escalation_id": esc.escalation_id,
        "episode_id": esc.episode_id,
        "reason": labels.escalation_reason_label(esc.reason),
        "reason_key": esc.reason,
        "moment": esc.moment,
        "turn_id": esc.turn_id,
        "lifecycle": esc.lifecycle,
        "lifecycle_label": labels.lifecycle_label(esc.lifecycle),
        "created_at": esc.created_at.isoformat() if esc.created_at else None,
        # CB-60: the cohort the linked call belongs to. Use this to find the call in the Calls list:
        # if cohort=='live' use "Real calls"; otherwise switch to "All cohorts" and filter by cohort.
        "episode_cohort": episode_cohort,
    }


# ---------------------------------------------------------------------------
# KPI aggregation (reuses src.loop.grading — never reimplemented)
# ---------------------------------------------------------------------------

# Outcomes that count as same-call enrollment for the enrollment-rate headline (distinct from
# the weighted-ladder score — a config can lift the ladder without lifting enrollment).
_ENROLLED_OUTCOMES = frozenset({"enrolled"})
_LADDER_MAX = 4  # max ladder tier for the 0..MAX headline scale


def _enrollment_rate(episodes: Sequence[Episode]) -> float:
    """Same-call enrollment rate from Episode.outcome — distinct from the ladder score."""
    if not episodes:
        return 0.0
    enrolled = sum(1 for ep in episodes if (ep.outcome or "") in _ENROLLED_OUTCOMES)
    return enrolled / len(episodes)


def _qualification_accuracy(episodes: Sequence[Episode]) -> float:
    """Share of episodes qualified correctly (reached tier≥2 ↔ qualified=True). 1.0 for empty."""
    if not episodes:
        return 1.0
    correct = 0
    for ep in episodes:
        reached_commitment = ep.ladder_tier and int(ep.ladder_tier) >= 2
        expected_qualified = bool(reached_commitment)
        if bool(ep.qualified) == expected_qualified:
            correct += 1
    return correct / len(episodes)


def compute_kpis(
    episodes: Sequence[Episode],
    *,
    compare_episodes: Optional[Sequence[Episode]] = None,
) -> dict[str, Any]:
    """P4 KPI payload: weighted-ladder headline + DISTINCT enrollment_rate, ladder-tier distribution,
    per-archetype conversion. `compare_episodes` adds a baseline arm via grading.compare_versions."""
    ladder_score = grading.kpi_score(episodes)
    enrollment_rate = _enrollment_rate(episodes)

    dist_counts: dict[int, int] = {}
    for ep in episodes:
        dist_counts[int(ep.ladder_tier)] = dist_counts.get(int(ep.ladder_tier), 0) + 1
    total = len(episodes) or 1
    ladder_distribution = [
        {
            "tier": tier,
            "label": labels.ladder_tier_label(tier),
            "count": dist_counts.get(tier, 0),
            "rate": dist_counts.get(tier, 0) / total,
        }
        for tier in sorted(set(list(dist_counts.keys()) + list(range(_LADDER_MAX + 1))), reverse=True)
    ]

    by_persona: dict[str, list[Episode]] = {}
    for ep in episodes:
        by_persona.setdefault(ep.persona or "Unknown", []).append(ep)
    archetype_conversion = [
        {"label": persona, "conversion": _enrollment_rate(eps), "n": len(eps)}
        for persona, eps in sorted(by_persona.items())
    ]

    payload: dict[str, Any] = {
        "n": len(episodes),
        "weighted_ladder_score": round(ladder_score, 3),
        "ladder_max": _LADDER_MAX,
        "enrollment_rate": round(enrollment_rate, 4),
        "qualification_accuracy": round(_qualification_accuracy(episodes), 4),
        "escalation_rate": round(
            (sum(1 for ep in episodes if ep.escalated) / total) if episodes else 0.0, 4
        ),
        "ladder_distribution": ladder_distribution,
        "archetype_conversion": archetype_conversion,
    }

    if compare_episodes is not None:
        cmp = grading.compare_versions(compare_episodes, episodes, seed=1234)
        payload["compare"] = {
            "baseline_ladder_score": round(cmp.champion_kpi, 3),
            "champion_ladder_score": round(cmp.challenger_kpi, 3),
            "delta": round(cmp.delta, 3),
            "delta_ci": [round(cmp.delta_ci[0], 3), round(cmp.delta_ci[1], 3)],
            "challenger_better": cmp.challenger_better,
            "baseline_enrollment_rate": round(_enrollment_rate(compare_episodes), 4),
            "champion_enrollment_rate": round(enrollment_rate, 4),
            "n_baseline": cmp.n_champion,
            "n_champion": cmp.n_challenger,
        }
    return payload


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_operate_router(read_store: Optional[ReadStore] = None) -> APIRouter:
    """Build the /api Operate read router. `read_store` is injectable (defaults to the Postgres
    store; tests pass a seeded in-memory fake). All endpoints are async, return labeled JSON, and
    guard empty data gracefully (empty list / null episode, never a 500)."""
    rs: ReadStore = read_store if read_store is not None else _StoreBackedReadStore()
    router = APIRouter()

    @router.get("/api/episodes")
    async def list_episodes_ep(
        version: Optional[str] = None,
        cohort: Optional[str] = None,
        outcome: Optional[str] = None,
        escalated: Optional[bool] = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """P3 Calls list — COMPLETED-calls history; active/0-turn calls excluded. Filters AND
        together; over-fetches then trims so completed rows fill the page even with interleaved
        active calls. An in-progress `outcome` filter yields an empty completed list (correct).
        CB-60: response now carries `total` = number of completed rows matching the filter BEFORE
        the per-page `limit` cap, so the UI can show "showing N of total" when the list is capped.
        The over-fetch scan uses _MAX_FETCH rows to count all matches (bounded at 10 000).
        CB-66 (item 2): when the outcome filter is a canonical key that has aliases (e.g.
        "callback_booked" also covers "callback_scheduled"), we fetch BOTH outcome keys and merge
        the result so alias rows are not silently excluded from a canonical-key filter."""
        fetch_limit = min(limit * _COMPLETED_OVERFETCH, _MAX_FETCH)
        # CB-66: expand alias outcomes. If the requested outcome key has a reverse-alias (e.g.
        # "callback_booked" -> also fetch "callback_scheduled"), run a second query and merge.
        # _OUTCOME_ALIASES maps canonical -> [alias, ...] for the few cases where two DB keys
        # represent the same concept. We fetch each separately and merge (union by episode_id).
        _OUTCOME_ALIASES: dict[str, list[str]] = {
            "callback_booked": ["callback_scheduled"],
        }
        alias_outcomes = _OUTCOME_ALIASES.get(outcome or "", []) if outcome else []
        eps = await rs.list_episodes(
            version=version, cohort=cohort, outcome=outcome, escalated=escalated, limit=fetch_limit
        )
        if alias_outcomes:
            seen_ids: set[str] = {e.episode_id for e in eps}
            for alias_key in alias_outcomes:
                alias_eps = await rs.list_episodes(
                    version=version, cohort=cohort, outcome=alias_key,
                    escalated=escalated, limit=fetch_limit,
                )
                for ae in alias_eps:
                    if ae.episode_id not in seen_ids:
                        eps.append(ae)
                        seen_ids.add(ae.episode_id)
            # Re-sort newest-first after merging (list_episodes already sorts per-query).
            eps.sort(key=lambda e: e.created_at, reverse=True)
        # CB-60: separate the full completed set (for total) from the capped page (for episodes).
        # The overfetch multiplier means fetch_limit is usually large enough to count all matches
        # up to _MAX_FETCH; if the total exceeds that the count is still honest (bounded at _MAX_FETCH).
        all_completed = [e for e in eps if _is_completed(e)]
        completed = all_completed[:limit]
        return {
            "episodes": [episode_summary(e) for e in completed],
            "count": len(completed),
            # total = how many completed rows match the filter (before the page cap). When
            # total > count, the UI knows the list is capped and can show "showing N of total".
            "total": len(all_completed),
        }

    # CB-30: the golden calibration SET. Declared BEFORE the dynamic /api/episodes/{episode_id} route
    # so the literal "golden" segment isn't captured as an episode_id (FastAPI matches in declaration
    # order). Returns the same episode_detail shape the Calls list/Review use, newest-first, so the set
    # is enumerable + exportable (each detail = transcript + belief trajectory + outcome) and the twin
    # harness can replay any of them — a golden episode is simply a flagged one.
    @router.get("/api/episodes/golden")
    async def list_golden_ep(limit: int = 200) -> dict[str, Any]:
        """CB-30: enumerate the golden calibration set, newest-first, as full Call Review payloads."""
        eps = await rs.list_golden_episodes(limit=limit)
        return {"episodes": [episode_detail(e) for e in eps], "count": len(eps)}

    @router.post("/api/episodes/{episode_id}/golden")
    async def set_golden_ep(episode_id: str, req: _GoldenUpdate) -> dict[str, Any]:
        """CB-30 WRITE: tag/untag a real call as golden (persists in metrics['golden']). 404 unknown id.
        Targeted update — does not rewrite the transcript/belief trajectory."""
        ok = await rs.set_episode_golden(episode_id, req.golden)
        if not ok:
            raise HTTPException(status_code=404, detail=f"unknown episode {episode_id!r}")
        return {"episode_id": episode_id, "golden": req.golden}

    @router.get("/api/episodes/{episode_id}")
    async def get_episode_ep(episode_id: str) -> dict[str, Any]:
        """Call Review (P2): the full episode with per-turn decision trace + belief trajectory."""
        ep = await rs.get_episode(episode_id)
        if ep is None:
            raise HTTPException(status_code=404, detail=f"unknown episode {episode_id!r}")
        return episode_detail(ep)

    @router.get("/api/kpis")
    async def kpis_ep(
        version: Optional[str] = None,
        cohort: Optional[str] = None,
        compare_version: Optional[str] = None,
    ) -> dict[str, Any]:
        """P4 KPI views: ladder headline + distinct enrollment rate; optional compare arm.
        CB-60: KPI now filters through _is_completed so its denominator is IDENTICAL to the Calls
        list — no orphan in_progress / null-outcome / 0-turn rows inflate the count or distort
        rates (those 150 orphans were making KPI say 2,842 while the list said 2,692). The
        enrollment_rate headline and the ladder_distribution both computed from the same set, so
        the two panels on one screen cannot contradict each other."""
        raw_eps = await rs.list_episodes(version=version, cohort=cohort, limit=10000)
        eps = [e for e in raw_eps if _is_completed(e)]
        compare_eps: Optional[list[Episode]] = None
        if compare_version is not None:
            raw_cmp = await rs.list_episodes(version=compare_version, cohort=cohort, limit=10000)
            compare_eps = [e for e in raw_cmp if _is_completed(e)]
        payload = compute_kpis(eps, compare_episodes=compare_eps)
        payload["version"] = version
        payload["cohort"] = cohort
        payload["compare_version"] = compare_version
        return payload

    @router.get("/api/escalations")
    async def escalations_ep(
        lifecycle: Optional[str] = None, limit: int = 100
    ) -> dict[str, Any]:
        """P5 Escalation queue: list by lifecycle with per-state badge counts.
        CB-60: each escalation row now carries `episode_cohort` (the cohort of the linked call)
        so operators know which population the escalation came from and can apply the right filter
        in the Calls list to find the call. The sidebar badge uses counts['unreviewed'] which
        matches the segmented-control total, ensuring badge == reachable count.
        Episode lookups for cohort are batched via a single de-duped episode-id fetch."""
        rows = await rs.list_escalations(lifecycle=lifecycle, limit=limit)
        all_rows = await rs.list_escalations(lifecycle=None, limit=10000)
        counts = {"unreviewed": 0, "reviewed": 0, "resolved": 0, "dismissed": 0}
        for r in all_rows:
            counts[r.lifecycle] = counts.get(r.lifecycle, 0) + 1
        # CB-60: resolve cohort for each escalation's linked episode. Batch: one get_episode call
        # per unique episode_id referenced in the page (bounded by `limit`, typically ≤100). We
        # swallow any lookup failure so a dangling escalation returns episode_cohort=None rather
        # than causing a 500 — the UI can show "unknown cohort" as a fallback label.
        unique_ids = list(dict.fromkeys(e.episode_id for e in rows if e.episode_id))
        cohort_by_ep: dict[str, Optional[str]] = {}
        for eid in unique_ids:
            try:
                ep = await rs.get_episode(eid)
                cohort_by_ep[eid] = ep.cohort if ep is not None else None
            except Exception:
                cohort_by_ep[eid] = None
        return {
            "escalations": [
                escalation_to_dict(e, episode_cohort=cohort_by_ep.get(e.episode_id or ""))
                for e in rows
            ],
            "count": len(rows),
            "counts": counts,
        }

    @router.post("/api/escalations/{escalation_id}/lifecycle")
    async def update_escalation_ep(escalation_id: str, req: _LifecycleUpdate) -> dict[str, Any]:
        """P5 triage WRITE: advance lifecycle state. 400 invalid, 404 unknown id."""
        try:
            ok = await rs.update_escalation_lifecycle(escalation_id, req.lifecycle)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            raise HTTPException(status_code=404, detail=f"unknown escalation {escalation_id!r}")
        return {"escalation_id": escalation_id, "lifecycle": req.lifecycle}

    _ACTIVE_FETCH_LIMIT = 50  # bounded scan for active/sample sub-routes

    @router.get("/api/live/active")
    async def live_active_ep() -> dict[str, Any]:
        """All non-stale active calls, newest-first. Humanized row fields; no raw slugs."""
        now = datetime.now(timezone.utc)
        eps = await rs.list_episodes(limit=_ACTIVE_FETCH_LIMIT)
        active_eps = [e for e in eps if _is_active(e, now=now)]
        calls = []
        for ep in active_eps:
            belief = _last_belief(ep)
            bd = _belief_to_dict(belief)
            trust_val = next(
                (d["value"] for d in (bd or {}).get("primary_drivers", []) if d["key"] == "trust"),
                None,
            )
            bail_val = next(
                (d["value"] for d in (bd or {}).get("primary_drivers", []) if d["key"] == "bail_risk"),
                None,
            )
            calls.append({
                "episode_id": ep.episode_id,
                "channel": ep.channel,
                # Humanize persona via labels — never emit a raw cohort/version slug or persona key.
                "persona_label": labels._titleize(ep.persona) if ep.persona else None,
                "stage": (bd or {}).get("stage") if bd else None,
                "trust": trust_val,
                "bail_risk": bail_val,
                "turn_count": len(ep.turns),
                "escalation_imminent": bool((bd or {}).get("escalation_imminent", False)) if bd else False,
                "started_at": ep.created_at.isoformat() if ep.created_at else None,
            })
        return {"count": len(calls), "calls": calls}

    @router.get("/api/live/sample")
    async def live_sample_ep() -> dict[str, Any]:
        """Newest completed episode with sample:true (page's 'Show sample call' toggle)."""
        eps = await rs.list_episodes(limit=_ACTIVE_FETCH_LIMIT)
        completed = [e for e in eps if _is_completed(e)]
        newest_completed = completed[0] if completed else None
        return live_snapshot(newest_completed, sample=True)

    @router.get("/api/live")
    async def live_ep(episode_id: Optional[str] = None) -> dict[str, Any]:
        """P1 Live monitor. No params: newest active call or null (never a completed fallback).
        ?episode_id=<id>: snapshot for that specific episode; 404 if unknown."""
        if episode_id is not None:
            ep = await rs.get_episode(episode_id)
            if ep is None:
                raise HTTPException(status_code=404, detail=f"unknown episode {episode_id!r}")
            return live_snapshot(ep)
        # Find the newest active non-stale call from a bounded page of recent episodes.
        now = datetime.now(timezone.utc)
        eps = await rs.list_episodes(limit=_ACTIVE_FETCH_LIMIT)
        for ep in eps:  # already newest-first
            if _is_active(ep, now=now):
                return live_snapshot(ep, now=now)
        return {"active": False, "episode": None}

    return router

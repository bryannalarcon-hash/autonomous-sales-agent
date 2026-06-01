# Operator-dashboard READ API (plan U15 — Operate mode). The 5 Operate screens (P1 Live monitor,
# Call Review, P3 Calls list, P4 KPI views, P5 Escalation queue) read JSON from these GET endpoints.
# It is DB-INJECTABLE the same way the demo API is LLM-injectable: a ReadStore protocol abstracts the
# data layer so create_operate_router(read_store=...) takes the real src.memory.store in prod and a
# seeded in-memory fake in tests (no Postgres on the test path). KPIs reuse src.loop.grading
# (kpi_score weighted ladder + compare_versions) — never reimplemented. Every operator-facing string
# is translated through src.api.labels so NO raw internal index (ladder int, driver slug, P-id) ever
# renders. Episodes/escalations are serialized to flat display DTOs the Next.js client types against.
# The /api/episodes Calls list is the COMPLETED history: in-progress / unset-outcome / 0-turn calls
# are EXCLUDED here (see _is_completed) so an active call never leaks in as a finished row — those
# live/unfinished calls surface only on /api/live (KPI aggregation is intentionally left untouched).
from __future__ import annotations

from typing import Any, Optional, Protocol, Sequence

from fastapi import APIRouter, HTTPException

from src.api import labels
from src.loop import grading
from src.memory.schema import BeliefSnapshot, Episode, EscalationLog, Turn

# ---------------------------------------------------------------------------
# Injectable data layer
# ---------------------------------------------------------------------------


class ReadStore(Protocol):
    """The read surface the Operate pages need. The default impl forwards to src.memory.store
    (Postgres); tests pass a seeded in-memory fake so the endpoints run DB-free, mirroring how the
    demo API injects llm_client_factory. All methods are async (the store is asyncpg-backed)."""

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


class _StoreBackedReadStore:
    """Default ReadStore that forwards to the module-level async functions in src.memory.store.

    Imported lazily inside __init__ so merely building the demo app (which doesn't touch the store)
    never drags asyncpg onto an import path that doesn't need it."""

    def __init__(self) -> None:
        from src.memory import store

        self._store = store

    async def list_episodes(self, **kw: Any) -> list[Episode]:
        return await self._store.list_episodes(**kw)

    async def get_episode(self, episode_id: str) -> Optional[Episode]:
        return await self._store.get_episode(episode_id)

    async def list_escalations(self, **kw: Any) -> list[EscalationLog]:
        return await self._store.list_escalations(**kw)


# ---------------------------------------------------------------------------
# Serializers — Episode/Turn/Escalation -> flat display DTOs (labels applied)
# ---------------------------------------------------------------------------

# Drivers the Live monitor (P1) treats as PRIMARY, always-visible, in priority order. Trust +
# walk-away risk lead per the IA spec; everything else is secondary (the "full belief state").
_PRIMARY_DRIVERS = ("trust", "bail_risk")

# Outcome keys that mean "not finished yet" — an unset/empty outcome or an explicit in-progress one.
# These (and any 0-turn episode) are LIVE/unfinished calls, surfaced on /api/live, never on the
# COMPLETED Calls list (P3).
_UNFINISHED_OUTCOMES = frozenset({None, "", "in_progress"})


# The completed-list filter runs after the store query, so we over-fetch to still fill a page when
# unfinished calls are interleaved newest-first. The multiple covers a heavy minority of live/0-turn
# rows; _MAX_FETCH caps the over-fetch so a huge `limit` can't ask the store for an unbounded scan.
_COMPLETED_OVERFETCH = 3
_MAX_FETCH = 10000


def _is_completed(ep: Episode) -> bool:
    """A genuinely COMPLETED call for the P3 Calls list: it reached a terminal outcome AND has at
    least one logged turn. In-progress / unset-outcome / 0-turn episodes are the live, still-running
    (or never-started) calls — they belong on /api/live, not in the completed history."""
    if ep.outcome in _UNFINISHED_OUTCOMES:
        return False
    return len(ep.turns) > 0


def _belief_to_dict(belief: Optional[BeliefSnapshot]) -> Optional[dict[str, Any]]:
    """Serialize a belief snapshot with display labels. Splits drivers into the prioritized primary
    pair (trust, walk-away risk) and the rest (secondary), and surfaces stage + escalation_imminent —
    exactly the IA the Live monitor prioritizes. Slot/driver values pass through as floats 0..1."""
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


def _turn_to_dict(turn: Turn) -> dict[str, Any]:
    """Serialize one turn for the transcript + decision trace (P1/P2). The agent's decision slug is
    translated to a readable act label; the raw decision is NOT included in operator-facing fields."""
    return {
        "turn_id": turn.turn_id,
        "speaker": turn.speaker,
        "text": turn.text,
        "decision_label": labels.act_label(turn.decision) if turn.speaker == "agent" else None,
        "rationale": turn.rationale,
        "latency_ms": turn.latency_ms,
        "belief": _belief_to_dict(turn.belief),
    }


def _last_belief(ep: Episode) -> Optional[BeliefSnapshot]:
    for turn in reversed(ep.turns):
        if turn.belief is not None:
            return turn.belief
    return None


def episode_summary(ep: Episode) -> dict[str, Any]:
    """The flat row the Calls list (P3) + Live tile read — no transcript body, all labels applied."""
    metrics = ep.metrics or {}
    return {
        "episode_id": ep.episode_id,
        "outcome": labels.outcome_label(ep.outcome),
        "outcome_key": ep.outcome,
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
        "duration_ms": metrics.get("duration_ms"),
        "created_at": ep.created_at.isoformat() if ep.created_at else None,
    }


def episode_detail(ep: Episode) -> dict[str, Any]:
    """The full Call Review payload (P2): the summary + the per-turn transcript/decision-trace and the
    belief trajectory (each turn's belief snapshot, in order) the scrubber replays."""
    detail = episode_summary(ep)
    detail["turns"] = [_turn_to_dict(t) for t in ep.turns]
    detail["belief_trajectory"] = [_belief_to_dict(t.belief) for t in ep.turns]
    detail["disqualifier_reason"] = ep.disqualifier_reason
    detail["metrics"] = ep.metrics or {}
    return detail


def live_snapshot(ep: Optional[Episode]) -> dict[str, Any]:
    """P1 Live monitor payload. There is no realtime infra in this build, so "live" = the most recent
    episode; the page polls this. When the most recent call is still in progress (outcome unset /
    'in_progress') we mark it active; otherwise we hand back the most-recent COMPLETED call and flag
    active=False so the page can show its "no active call" affordance. The PRIORITIZED belief IA —
    trust, walk-away risk, current stage + last act, escalation-imminent — is hoisted to the top."""
    if ep is None:
        return {"active": False, "episode": None}
    belief = _last_belief(ep)
    last_agent = next((t for t in reversed(ep.turns) if t.speaker == "agent"), None)
    active = ep.outcome in (None, "", "in_progress")
    bd = _belief_to_dict(belief)
    return {
        "active": active,
        "episode": episode_detail(ep),
        # The four FOREMOST signals, lifted out so the client never has to dig for them.
        "priority": {
            "trust": next((d["value"] for d in (bd or {}).get("primary_drivers", []) if d["key"] == "trust"), None),
            "bail_risk": next((d["value"] for d in (bd or {}).get("primary_drivers", []) if d["key"] == "bail_risk"), None),
            "stage": (bd or {}).get("stage"),
            "last_act_label": labels.act_label(last_agent.decision) if last_agent else None,
            "escalation_imminent": (bd or {}).get("escalation_imminent", False),
        },
    }


def escalation_to_dict(esc: EscalationLog) -> dict[str, Any]:
    """Serialize one escalation for the queue (P5), reason + lifecycle translated to labels."""
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
    }


# ---------------------------------------------------------------------------
# KPI aggregation (reuses src.loop.grading — never reimplemented)
# ---------------------------------------------------------------------------

# Outcome keys that count as a same-call enrollment for the DISTINCT enrollment-rate headline. This
# rate is deliberately SEPARATE from the weighted-ladder score: the ladder rewards moving up ANY rung
# (grading.kpi_score), while the enrollment rate is the share of calls that closed at full enrollment.
_ENROLLED_OUTCOMES = frozenset({"enrolled"})
# Max ladder tier — used to normalize the weighted ladder mean onto a 0..MAX headline scale.
_LADDER_MAX = 4


def _enrollment_rate(episodes: Sequence[Episode]) -> float:
    """Same-call enrollment rate = fraction of episodes whose outcome is a full enrollment. Computed
    from Episode.outcome, NOT from ladder_tier, so it stays a distinct number from the ladder score."""
    if not episodes:
        return 0.0
    enrolled = sum(1 for ep in episodes if (ep.outcome or "") in _ENROLLED_OUTCOMES)
    return enrolled / len(episodes)


def _qualification_accuracy(episodes: Sequence[Episode]) -> float:
    """Share of episodes the agent qualified correctly. Best-effort from the stored qualified flag:
    an episode that reached a booking/enrollment outcome SHOULD be qualified; one that disqualified
    SHOULD NOT be. Returns 1.0 for an empty set (nothing to be wrong about)."""
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
    """The P4 KPI payload for a (filtered) set of episodes.

    HEADLINE (primary): the weighted commitment-ladder score (grading.kpi_score — the mean ladder
    rung, the meaningful KPI) AND, as a DISTINCT number, the same-call enrollment_rate (share of
    calls that closed at enrollment). Keeping them separate is the whole point of P4: a config can
    lift the ladder without lifting enrollment, and the operator must see both.

    When compare_episodes is given (the baseline arm), grading.compare_versions ranks the two on the
    ladder KPI with bootstrap CIs and the over-claim-resistant decision rule — surfaced as `compare`.
    Ladder-tier distribution + per-archetype conversion are derived for the secondary panels.
    """
    ladder_score = grading.kpi_score(episodes)
    enrollment_rate = _enrollment_rate(episodes)

    # Ladder-tier distribution (share of calls at each rung), labeled.
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

    # Per-archetype (persona) conversion = enrollment rate within each persona group.
    by_persona: dict[str, list[Episode]] = {}
    for ep in episodes:
        by_persona.setdefault(ep.persona or "Unknown", []).append(ep)
    archetype_conversion = [
        {"label": persona, "conversion": _enrollment_rate(eps), "n": len(eps)}
        for persona, eps in sorted(by_persona.items())
    ]

    payload: dict[str, Any] = {
        "n": len(episodes),
        # The two DISTINCT headline numbers, each on its own scale.
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
    """Build the /api Operate read router. `read_store` is injectable: defaults to the Postgres-backed
    store, tests pass a seeded in-memory fake. All endpoints are async, return JSON the Next.js client
    types against, and guard empty data gracefully (empty list / null episode, never a 500)."""
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
        """P3 Calls list — the COMPLETED-calls history. Filters (version / cohort / outcome /
        escalated) AND together; returns summary rows newest-first. `outcome` is the raw outcome key
        (the UI maps its labeled filter back to the key before calling).

        Honest contract: still-in-progress / unset-outcome / 0-turn episodes are LIVE/unfinished
        calls and are EXCLUDED here (they belong on /api/live), so an active call never leaks in as
        the top row of the completed list. The filter runs AFTER the store query (store.py owns the
        SQL and isn't ours to change), so we over-fetch a bounded multiple of `limit` first and then
        trim back to `limit` — this keeps a full page of completed rows even when unfinished calls
        are interleaved newest-first. An explicit unfinished `outcome` filter therefore yields an
        empty completed list (correct: those calls aren't completed)."""
        fetch_limit = min(limit * _COMPLETED_OVERFETCH, _MAX_FETCH)
        eps = await rs.list_episodes(
            version=version, cohort=cohort, outcome=outcome, escalated=escalated, limit=fetch_limit
        )
        completed = [e for e in eps if _is_completed(e)][:limit]
        return {"episodes": [episode_summary(e) for e in completed], "count": len(completed)}

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
        """P4 KPI views. Aggregates the version/cohort-filtered episodes into the weighted-ladder
        headline AND the DISTINCT enrollment rate. `compare_version` adds a baseline arm ranked via
        grading.compare_versions. Empty data yields zeroed KPIs, never an error."""
        eps = await rs.list_episodes(version=version, cohort=cohort, limit=10000)
        compare_eps: Optional[list[Episode]] = None
        if compare_version is not None:
            compare_eps = await rs.list_episodes(version=compare_version, cohort=cohort, limit=10000)
        payload = compute_kpis(eps, compare_episodes=compare_eps)
        payload["version"] = version
        payload["cohort"] = cohort
        payload["compare_version"] = compare_version
        return payload

    @router.get("/api/escalations")
    async def escalations_ep(
        lifecycle: Optional[str] = None, limit: int = 100
    ) -> dict[str, Any]:
        """P5 Escalation queue. Lists by lifecycle (unreviewed / reviewed / resolved); also returns
        per-lifecycle counts so the segmented control shows badges. Empty -> empty list + zero counts."""
        rows = await rs.list_escalations(lifecycle=lifecycle, limit=limit)
        all_rows = await rs.list_escalations(lifecycle=None, limit=10000)
        counts = {"unreviewed": 0, "reviewed": 0, "resolved": 0, "dismissed": 0}
        for r in all_rows:
            counts[r.lifecycle] = counts.get(r.lifecycle, 0) + 1
        return {
            "escalations": [escalation_to_dict(e) for e in rows],
            "count": len(rows),
            "counts": counts,
        }

    @router.get("/api/live")
    async def live_ep() -> dict[str, Any]:
        """P1 Live monitor. No realtime infra here: returns the most-recent call as the "live" call
        (active=True only if it's still in progress) with the prioritized belief IA hoisted up."""
        eps = await rs.list_episodes(limit=1)
        return live_snapshot(eps[0] if eps else None)

    return router

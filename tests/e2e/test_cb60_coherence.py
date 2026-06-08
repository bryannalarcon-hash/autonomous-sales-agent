# CB-60 coherence tests — cohort/count reconciliation across Operate surfaces (API layer).
# Uses TestClient + seeded FakeReadStore (no Postgres, no LLM, $0). Assertions:
#   - /api/episodes `total` ≥ `count`; subtotals by cohort sum to the All-cohorts total.
#   - /api/kpis denominator matches /api/episodes total (both use _is_completed filter).
#   - Escalation queue rows carry `episode_cohort`; each linked call is findable via the list
#     endpoint with the matching cohort filter.
#   - KPI headline enrollment_rate and ladder_distribution share the same denominator (no orphan
#     in_progress rows inflating one metric but not the other).
#   - Stub detection: channel='sim' episodes get is_stub=True; channel='voice' gets is_stub=False.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.memory.schema import BeliefSnapshot, Episode, EscalationLog, Turn


# ---------------------------------------------------------------------------
# Minimal FakeReadStore (mirrors the one in test_operate.py — kept local so
# these tests don't import from another test file)
# ---------------------------------------------------------------------------


class _FakeReadStore:
    def __init__(self, episodes: list[Episode], escalations: list[EscalationLog]) -> None:
        self._episodes = episodes
        self._escalations = escalations

    async def list_episodes(
        self,
        *,
        version: Optional[str] = None,
        cohort: Optional[str] = None,
        outcome: Optional[str] = None,
        escalated: Optional[bool] = None,
        limit: int = 200,
    ) -> list[Episode]:
        rows = [
            e
            for e in self._episodes
            if (version is None or e.version == version)
            and (cohort is None or e.cohort == cohort)
            and (outcome is None or e.outcome == outcome)
            and (escalated is None or e.escalated == escalated)
        ]
        rows.sort(key=lambda e: e.created_at, reverse=True)
        return rows[:limit]

    async def get_episode(self, episode_id: str) -> Optional[Episode]:
        return next((e for e in self._episodes if e.episode_id == episode_id), None)

    async def list_escalations(
        self, *, lifecycle: Optional[str] = None, limit: int = 100
    ) -> list[EscalationLog]:
        rows = [e for e in self._escalations if lifecycle is None or e.lifecycle == lifecycle]
        rows.sort(key=lambda e: e.created_at)
        return rows[:limit]

    async def update_escalation_lifecycle(self, escalation_id: str, lifecycle: str) -> bool:
        if lifecycle not in {"unreviewed", "reviewed", "resolved", "dismissed"}:
            raise ValueError(f"invalid lifecycle {lifecycle!r}")
        for e in self._escalations:
            if e.escalation_id == escalation_id:
                e.lifecycle = lifecycle
                return True
        return False

    async def set_episode_golden(self, episode_id: str, golden: bool) -> bool:
        for e in self._episodes:
            if e.episode_id == episode_id:
                if e.metrics is None:
                    e.metrics = {}
                e.metrics["golden"] = golden
                return True
        return False

    async def list_golden_episodes(self, *, limit: int = 200) -> list[Episode]:
        rows = [e for e in self._episodes if bool((e.metrics or {}).get("golden"))]
        rows.sort(key=lambda e: e.created_at, reverse=True)
        return rows[:limit]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _belief() -> BeliefSnapshot:
    return BeliefSnapshot(
        slots={},
        drivers={"trust": 0.6, "bail_risk": 0.3},
        trends={},
        stage="closing",
    )


def _ep(
    eid: str,
    *,
    outcome: str,
    tier: int,
    cohort: str,
    channel: str = "voice",
    version: str = "v1",
    escalated: bool = False,
    minutes_ago: int = 10,
) -> Episode:
    """Build a completed Episode fixture — always has turns (so _is_completed passes)."""
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    turn = Turn(turn_id=0, speaker="agent", text="Hi!", decision="greeting",
                rationale="open", belief=_belief(), latency_ms=500)
    return Episode(
        episode_id=eid, turns=[turn], outcome=outcome, ladder_tier=tier,
        qualified=tier >= 2, version=version, kb_version="kb-1",
        channel=channel, persona="Busy Decider", cohort=cohort,
        escalated=escalated, metrics={"duration_ms": 60000}, created_at=created,
    )


def _orphan(eid: str, *, cohort: str, minutes_ago: int = 5) -> Episode:
    """Build an in_progress (orphan) episode that _is_completed must exclude."""
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return Episode(
        episode_id=eid, turns=[], outcome="in_progress", ladder_tier=0,
        qualified=False, version="v1", kb_version="kb-1",
        channel="voice", persona="Unknown", cohort=cohort,
        escalated=False, metrics={}, created_at=created,
    )


# ---------------------------------------------------------------------------
# CB-60 deliverable 1: POPULATION TRUTH — subtotals sum, total disclosed
# ---------------------------------------------------------------------------


def test_list_total_field_present_and_gte_count():
    """CB-60 D1: /api/episodes response carries `total` ≥ `count` always."""
    eps = [
        _ep("E1", outcome="enrolled", tier=4, cohort="live"),
        _ep("E2", outcome="consult_booked", tier=2, cohort="live"),
        _ep("E3", outcome="consult_booked", tier=2, cohort="held_out"),
    ]
    client = TestClient(create_app(read_store=_FakeReadStore(eps, [])))
    r = client.get("/api/episodes")
    body = r.json()
    assert "total" in body, "CB-60: /api/episodes must return `total` field"
    assert body["total"] >= body["count"], "total must be >= count (count is the capped page)"


def test_cohort_subtotals_sum_to_total():
    """CB-60 D1: subtotals for each cohort must sum to the All-cohorts total.
    Verifies the 150-orphan fix: in_progress rows are excluded from both the total and subtotals."""
    live_eps = [
        _ep("L1", outcome="enrolled", tier=4, cohort="live"),
        _ep("L2", outcome="consult_booked", tier=2, cohort="live"),
    ]
    held_eps = [
        _ep("H1", outcome="consult_booked", tier=2, cohort="held_out"),
        _ep("H2", outcome="disqualified", tier=0, cohort="held_out"),
    ]
    # These in_progress orphans must be excluded from EVERY count.
    orphans = [
        _orphan("O1", cohort="live"),
        _orphan("O2", cohort="held_out"),
        _orphan("O3", cohort=None),  # null cohort — the "150 orphans" case
    ]
    all_eps = live_eps + held_eps + orphans
    client = TestClient(create_app(read_store=_FakeReadStore(all_eps, [])))

    all_body = client.get("/api/episodes").json()
    live_body = client.get("/api/episodes", params={"cohort": "live"}).json()
    held_body = client.get("/api/episodes", params={"cohort": "held_out"}).json()

    # The orphans must not appear in any completed count.
    all_ids = {e["episode_id"] for e in all_body["episodes"]}
    assert "O1" not in all_ids and "O2" not in all_ids and "O3" not in all_ids, (
        "in_progress orphans must be excluded from the completed Calls list"
    )

    # Subtotals sum check.
    assert live_body["total"] + held_body["total"] == all_body["total"], (
        f"cohort subtotals {live_body['total']} + {held_body['total']} "
        f"must sum to all-cohorts total {all_body['total']}"
    )

    # Total matches expected completed count (4 real episodes, 0 orphans).
    assert all_body["total"] == 4, f"expected 4 completed episodes, got {all_body['total']}"


def test_cap_disclosed_via_total_gt_count():
    """CB-60 D1: when more completed rows exist than the limit, total > count reveals the cap.
    The UI reads total to show 'showing N of total'."""
    # Build 5 completed episodes but request limit=3.
    eps = [_ep(f"E{i}", outcome="consult_booked", tier=2, cohort="live", minutes_ago=i)
           for i in range(5)]
    client = TestClient(create_app(read_store=_FakeReadStore(eps, [])))
    r = client.get("/api/episodes", params={"limit": "3"})
    body = r.json()
    assert body["count"] == 3, f"expected page of 3, got {body['count']}"
    assert body["total"] >= 5, f"total should be ≥5 (all completed), got {body['total']}"
    assert body["total"] > body["count"], "total must exceed count when list is capped"


# ---------------------------------------------------------------------------
# CB-60 deliverable 2: ESCALATION COHERENCE — queue carries cohort, linked
# call is findable via the list endpoint's cohort filter
# ---------------------------------------------------------------------------


def test_escalation_carries_episode_cohort():
    """CB-60 D2: each escalation row has `episode_cohort` matching the linked episode's cohort."""
    live_ep = _ep("LIVE-1", outcome="escalated", tier=0, cohort="live", escalated=True)
    held_ep = _ep("HELD-1", outcome="escalated", tier=0, cohort="held_out", escalated=True)
    escs = [
        EscalationLog(escalation_id="ESC-L1", episode_id="LIVE-1", reason="human_requested",
                      moment="Caller wanted a human.", turn_id=0, lifecycle="unreviewed",
                      created_at=datetime.now(timezone.utc) - timedelta(minutes=5)),
        EscalationLog(escalation_id="ESC-H1", episode_id="HELD-1", reason="pricing_concession",
                      moment="Demanded 30% off.", turn_id=0, lifecycle="unreviewed",
                      created_at=datetime.now(timezone.utc) - timedelta(minutes=10)),
    ]
    client = TestClient(create_app(read_store=_FakeReadStore([live_ep, held_ep], escs)))
    body = client.get("/api/escalations").json()
    by_esc = {e["escalation_id"]: e for e in body["escalations"]}

    assert "episode_cohort" in by_esc["ESC-L1"], "escalation must carry episode_cohort"
    assert by_esc["ESC-L1"]["episode_cohort"] == "live", (
        f"expected 'live', got {by_esc['ESC-L1']['episode_cohort']!r}"
    )
    assert by_esc["ESC-H1"]["episode_cohort"] == "held_out", (
        f"expected 'held_out', got {by_esc['ESC-H1']['episode_cohort']!r}"
    )


def test_escalation_linked_call_findable_via_cohort_filter():
    """CB-60 D2: the episode referenced by an escalation must be findable from /api/episodes
    using the episode_cohort as the cohort filter — this is the 'search loses them' fix."""
    # An escalation pointing at a held_out episode: invisible under cohort='live' (the default),
    # but findable under cohort='held_out'.
    held_ep = _ep("HELD-ESC", outcome="escalated", tier=0, cohort="held_out", escalated=True)
    esc = EscalationLog(escalation_id="ESC-HE", episode_id="HELD-ESC", reason="human_requested",
                        moment="Caller wanted a human.", turn_id=0, lifecycle="unreviewed",
                        created_at=datetime.now(timezone.utc))
    client = TestClient(create_app(read_store=_FakeReadStore([held_ep], [esc])))

    # The escalation reveals the cohort.
    esc_body = client.get("/api/escalations").json()
    ep_cohort = esc_body["escalations"][0]["episode_cohort"]
    assert ep_cohort == "held_out"

    # Using that cohort, the call IS findable via the list endpoint.
    list_body = client.get("/api/episodes", params={"cohort": ep_cohort}).json()
    ids = {e["episode_id"] for e in list_body["episodes"]}
    assert "HELD-ESC" in ids, (
        f"escalation's call must be findable via cohort filter {ep_cohort!r}; got ids {ids}"
    )

    # Without the cohort filter (default 'live' behavior): NOT in the list.
    live_body = client.get("/api/episodes", params={"cohort": "live"}).json()
    live_ids = {e["episode_id"] for e in live_body["episodes"]}
    assert "HELD-ESC" not in live_ids, (
        "a non-live escalation's call must NOT appear under cohort='live'"
    )


def test_escalation_dangling_episode_returns_none_cohort():
    """CB-60 D2: an escalation pointing at a nonexistent episode_id gets episode_cohort=None
    rather than a 500 — the queue remains usable even with dangling escalation rows."""
    esc = EscalationLog(escalation_id="ESC-DANGLE", episode_id="MISSING-EP",
                        reason="human_requested", moment="Gone.", turn_id=0,
                        lifecycle="unreviewed", created_at=datetime.now(timezone.utc))
    client = TestClient(create_app(read_store=_FakeReadStore([], [esc])))
    r = client.get("/api/escalations")
    assert r.status_code == 200, r.text
    esc_row = r.json()["escalations"][0]
    assert "episode_cohort" in esc_row
    assert esc_row["episode_cohort"] is None, (
        f"dangling escalation should have episode_cohort=None, got {esc_row['episode_cohort']!r}"
    )


# ---------------------------------------------------------------------------
# CB-60 deliverable 3: KPI SELF-CONSISTENCY — headline and ladder share denominator
# ---------------------------------------------------------------------------


def test_kpi_denominator_excludes_in_progress_orphans():
    """CB-60 D3: /api/kpis must NOT count orphan in_progress rows in its denominator.
    Root cause of the 2,842 vs 2,692 symptom: KPI was including in_progress rows while
    the Calls list excluded them, so two surfaces on one screen showed different N."""
    completed = [
        _ep("C1", outcome="enrolled", tier=4, cohort="live"),
        _ep("C2", outcome="consult_booked", tier=2, cohort="live"),
        _ep("C3", outcome="disqualified", tier=0, cohort="live"),
    ]
    orphans = [
        _orphan("O1", cohort="live"),
        _orphan("O2", cohort="live"),
    ]
    client = TestClient(create_app(read_store=_FakeReadStore(completed + orphans, [])))

    kpi = client.get("/api/kpis", params={"cohort": "live"}).json()
    # KPI n must equal the completed-list total, NOT completed + orphans.
    assert kpi["n"] == 3, (
        f"KPI n={kpi['n']} must equal completed count=3, not include the 2 orphans"
    )

    list_body = client.get("/api/episodes", params={"cohort": "live"}).json()
    assert kpi["n"] == list_body["total"], (
        f"KPI n={kpi['n']} must equal list total={list_body['total']} — same denominator"
    )


def test_kpi_enrollment_rate_and_ladder_distribution_same_denominator():
    """CB-60 D3: enrollment_rate and ladder_distribution are computed from the SAME episode list.
    The '67% vs 0%' contradiction happened when one used a wider set than the other.
    Here vA has 3 episodes: 2 enrolled (tier 4) + 1 consult_booked (tier 2). Enrollment = 2/3 = 67%.
    The tier-4 rate in ladder_distribution must also be 2/3 ≈ 67%, not 0%."""
    eps = [
        _ep("A1", outcome="enrolled", tier=4, cohort="live", version="vA"),
        _ep("A2", outcome="enrolled", tier=4, cohort="live", version="vA"),
        _ep("A3", outcome="consult_booked", tier=2, cohort="live", version="vA"),
    ]
    client = TestClient(create_app(read_store=_FakeReadStore(eps, [])))
    kpi = client.get("/api/kpis", params={"version": "vA"}).json()

    enrollment = kpi["enrollment_rate"]  # 2/3
    tier4_rate = next(
        (d["rate"] for d in kpi["ladder_distribution"] if d["tier"] == 4), None
    )
    assert tier4_rate is not None, "tier-4 entry must exist in ladder_distribution"
    # enrollment_rate is rounded to 4dp by compute_kpis; 2/3 ≈ 0.6667.
    # tier4_rate comes from dist_counts/total (unrounded float). Both must be ≈ 2/3.
    # Tolerance 1e-4 covers the rounding gap (0.6667 vs 0.66667 differ by < 0.0001).
    assert abs(enrollment - 2 / 3) < 1e-4, f"enrollment_rate must be ≈2/3, got {enrollment}"
    assert abs(tier4_rate - 2 / 3) < 1e-4, (
        f"tier-4 rate must be ≈2/3; got tier4={tier4_rate}, enrollment={enrollment}"
    )
    # Key same-denominator invariant: both metrics agree within the rounding margin.
    assert abs(enrollment - tier4_rate) < 1e-4, (
        f"enrollment_rate ({enrollment}) and tier4 rate ({tier4_rate}) must agree within 1e-4 "
        f"(same denominator, only rounding differs); actual gap = {abs(enrollment - tier4_rate)}"
    )
    assert kpi["n"] == 3, f"KPI n must be 3, got {kpi['n']}"


def test_kpi_small_n_flag_present_in_response():
    """CB-60 D3: the KPI endpoint returns `n` so the frontend can surface the small-n warning
    for n=1 vB-style cohorts. The backend doesn't gate on n; the UI renders the caveat."""
    ep = _ep("B1", outcome="enrolled", tier=4, cohort="live", version="vB")
    client = TestClient(create_app(read_store=_FakeReadStore([ep], [])))
    kpi = client.get("/api/kpis", params={"version": "vB"}).json()
    assert kpi["n"] == 1, "n=1 vB scenario must report n=1 so UI can show the caveat"
    # The rates are 100% — that's correct for n=1; the UI (not the API) shows the warning.
    assert kpi["enrollment_rate"] == 1.0
    assert kpi["escalation_rate"] == 0.0


# ---------------------------------------------------------------------------
# CB-60 deliverable 4: STUB LABELING — is_stub on sim-channel episodes
# ---------------------------------------------------------------------------


def test_sim_channel_episodes_get_is_stub_true():
    """CB-60 D4: episodes with channel='sim' (seeded/self-play stubs) get is_stub=True.
    These are the 39 identical seeded stubs (all released, latency 0, sim-harness decision).
    The most robust existing trait is channel='sim' (a validated enum, not free-form text)."""
    sim_ep = _ep("SIM-1", outcome="released", tier=0, cohort="training", channel="sim")
    voice_ep = _ep("VOICE-1", outcome="consult_booked", tier=2, cohort="live", channel="voice")
    # channel='text' is a real text-console call (not a stub).
    text_ep = _ep("TEXT-1", outcome="disqualified", tier=0, cohort="live", channel="text")
    # Override channel validation by building the Episode directly with channel='text'.
    # (The _ep helper uses channel='voice' by default; passing channel='text' hits the validator.)
    client = TestClient(create_app(read_store=_FakeReadStore([sim_ep, voice_ep, text_ep], [])))

    body = client.get("/api/episodes").json()
    by_id = {e["episode_id"]: e for e in body["episodes"]}

    # sim episode -> is_stub True.
    assert "is_stub" in by_id["SIM-1"], "is_stub must be present on all summary rows"
    assert by_id["SIM-1"]["is_stub"] is True, f"sim episode must have is_stub=True"

    # voice/text episodes -> is_stub False.
    assert by_id["VOICE-1"]["is_stub"] is False, "voice call must have is_stub=False"
    assert by_id["TEXT-1"]["is_stub"] is False, "text call must have is_stub=False"


def test_sim_stubs_excluded_from_live_cohort_filter():
    """CB-60 D4: sim stubs tagged with a non-live cohort (e.g. 'training') do NOT appear under
    cohort='live'. The cohort='live' filter is the semantic boundary for 'Real calls' — stubs
    use 'training'/'held_out'/etc. so the filter naturally excludes them."""
    sim_ep = _ep("SIM-STUB", outcome="released", tier=0, cohort="training", channel="sim")
    live_ep = _ep("LIVE-REAL", outcome="consult_booked", tier=2, cohort="live", channel="voice")
    client = TestClient(create_app(read_store=_FakeReadStore([sim_ep, live_ep], [])))

    live_body = client.get("/api/episodes", params={"cohort": "live"}).json()
    live_ids = {e["episode_id"] for e in live_body["episodes"]}
    assert "SIM-STUB" not in live_ids, "sim stub must be excluded from cohort='live'"
    assert "LIVE-REAL" in live_ids, "real voice call must appear under cohort='live'"

    # Via All cohorts: the stub IS reachable.
    all_body = client.get("/api/episodes").json()
    all_ids = {e["episode_id"] for e in all_body["episodes"]}
    assert "SIM-STUB" in all_ids, "sim stub must be reachable via All cohorts (no cohort filter)"


# ---------------------------------------------------------------------------
# CB-60 deliverable 5: sidebar badge = list-reachable unreviewed count
# ---------------------------------------------------------------------------


def test_sidebar_badge_agrees_with_unreviewed_list_count():
    """CB-60 D2 (badge coherence): the `counts.unreviewed` field used by the sidebar badge
    must equal the number of rows in the unreviewed list. They both come from the same source
    (list_escalations) so this is a consistency regression guard."""
    escs = [
        EscalationLog(escalation_id="ESC-U1", episode_id="E1", reason="human_requested",
                      moment="Wanted a human.", turn_id=0, lifecycle="unreviewed",
                      created_at=datetime.now(timezone.utc) - timedelta(minutes=1)),
        EscalationLog(escalation_id="ESC-U2", episode_id="E2", reason="pricing_concession",
                      moment="Demanded 40% off.", turn_id=0, lifecycle="unreviewed",
                      created_at=datetime.now(timezone.utc) - timedelta(minutes=2)),
        EscalationLog(escalation_id="ESC-R1", episode_id="E3", reason="human_requested",
                      moment="Already reviewed.", turn_id=0, lifecycle="reviewed",
                      created_at=datetime.now(timezone.utc) - timedelta(minutes=3)),
    ]
    client = TestClient(create_app(read_store=_FakeReadStore([], escs)))

    # Full (no lifecycle filter) to get counts.
    full = client.get("/api/escalations").json()
    badge_count = full["counts"]["unreviewed"]

    # The filtered list must have the same count.
    unreviewed = client.get("/api/escalations", params={"lifecycle": "unreviewed"}).json()
    list_count = unreviewed["count"]

    assert badge_count == list_count == 2, (
        f"sidebar badge ({badge_count}) must match list count ({list_count}), both should be 2"
    )

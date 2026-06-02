# End-to-end tests for the operator-dashboard READ API (plan U15 — Operate mode). Drives the
# FastAPI surface (src.api.server.create_app) with fastapi.testclient.TestClient and an INJECTED
# in-memory ReadStore seeded with Episodes + EscalationLogs — so nothing hits Postgres or the
# network (mirrors how test_demo_call injects a MockLLMClient). Asserts the gradeable contract the
# 5 Operate pages depend on: /api/episodes filters by version+cohort+outcome+escalated; /api/kpis
# carries the weighted-ladder headline AND a DISTINCT enrollment_rate (and a compare arm); the
# escalation queue lists by lifecycle with counts; Call Review returns the decision trace + belief
# trajectory; /api/live hoists the prioritized belief IA; /api/live/active lists all non-stale
# active calls; /api/live/sample returns the newest completed call. Also guards empty data +
# verifies NO raw internal index (ladder int alone, driver slug) leaks into operator-facing labels.
# CB-06: episode_detail exposes prospect_trajectory for sim/twin episodes; real (voice) episodes
# get [] so the frontend panel is correctly absent there.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.memory.schema import BeliefSnapshot, Episode, EscalationLog, Turn


# --- a seeded in-memory ReadStore (DB-free) ------------------------------------------------------


class FakeReadStore:
    """In-memory ReadStore satisfying src.api.operate.ReadStore. Applies the same optional-AND filter
    semantics the real store pushes to SQL, sorts episodes newest-first, and FIFO-orders escalations."""

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


def _belief(trust: float, bail: float, stage: str, imminent: bool = False) -> BeliefSnapshot:
    return BeliefSnapshot(
        slots={"grade_level": {"value": "11", "confidence": 0.9}, "budget": {"value": "tight", "confidence": 0.5}},
        drivers={"trust": trust, "bail_risk": bail, "price_sensitivity": 0.6, "urgency": 0.3},
        trends={"trust_velocity": 0.05},
        stage=stage,
        decision_confidence=0.83,
        escalation_imminent=imminent,
    )


def _episode(
    eid: str,
    *,
    outcome: str,
    tier: int,
    version: str,
    cohort: str,
    persona: str,
    qualified: bool,
    escalated: bool = False,
    minutes_ago: int = 0,
    turn_count: int = 4,
) -> Episode:
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    all_turns = [
        Turn(turn_id=0, speaker="agent", text="Hi, thanks for hopping on. What pulled you in?",
             decision="greeting", rationale="Warm open.", belief=_belief(0.42, 0.28, "discovery"), latency_ms=600),
        Turn(turn_id=1, speaker="prospect", text="We keep missing follow-ups."),
        Turn(turn_id=2, speaker="agent", text="Most teams your size make it back the first month.",
             decision="reframe_cost", rationale="Validate before reframing to ROI.",
             belief=_belief(0.54, 0.33, "objection"), latency_ms=700),
        Turn(turn_id=3, speaker="agent", text="Want me to set the pilot up now?",
             decision="trial_close", rationale="Buying signal — move to close.",
             belief=_belief(0.66, 0.34, "closing", imminent=escalated), latency_ms=800),
    ]
    # turn_count lets a test seed a still-connecting (0-turn) or barely-started episode — the
    # in-progress/0-turn shapes the completed Calls list must EXCLUDE and Live/Review must handle.
    turns = all_turns[:turn_count]
    metrics: dict = {"duration_ms": 348000, "talk_listen": [44, 56]}
    # A live call stamps metrics['live_heartbeat'] (last activity) every upsert; _is_active gates on
    # its freshness. Mirror that for unfinished-outcome fixtures by beating at `created` — so a fixture's
    # liveness tracks its `minutes_ago` exactly as before (fresh => active, stale => abandoned), while a
    # completed call (or a row with no heartbeat) is correctly never "live".
    if outcome in (None, "", "in_progress"):
        metrics["live_heartbeat"] = created.isoformat()
    return Episode(
        episode_id=eid, turns=turns, outcome=outcome, ladder_tier=tier, qualified=qualified,
        version=version, kb_version="kb-37", channel="voice", persona=persona, cohort=cohort,
        escalated=escalated, metrics=metrics, created_at=created,
    )


def _seeded_client() -> TestClient:
    episodes = [
        # Newest call is still connecting: in_progress + 0 turns. It must NOT pollute the COMPLETED
        # Calls list, but IS the call /api/live hands back (active, connecting state). Kept on v12 so
        # the v12 KPI math the KPI tests below assert is unchanged (KPI aggregation is out of scope).
        _episode("CALL-4821", outcome="in_progress", tier=0, version="v12", cohort="live",
                 persona="Skeptical Analyzer", qualified=False, minutes_ago=0, turn_count=0),
        # A second unfinished shape: outcome unset AND 0 turns (the ep-esc-* artifact) — also excluded
        # from the completed list. On version "v_live" so it doesn't perturb the v12 KPI math asserted
        # below (the in-progress CALL-4821 above stays on v12 exactly as the original KPI seed had it).
        _episode("CALL-4822", outcome=None, tier=0, version="v_live", cohort="live",
                 persona="Busy Decider", qualified=False, minutes_ago=1, turn_count=0),
        _episode("CALL-4820", outcome="enrolled", tier=4, version="v12", cohort="held_out",
                 persona="Busy Decider", qualified=True, minutes_ago=6),
        _episode("CALL-4819", outcome="escalated", tier=2, version="v12", cohort="held_out",
                 persona="Skeptical Analyzer", qualified=True, escalated=True, minutes_ago=22),
        _episode("CALL-4818", outcome="disqualified", tier=0, version="v12", cohort="held_out",
                 persona="Price Hawk", qualified=False, minutes_ago=38),
        _episode("CALL-4816", outcome="booked", tier=2, version="v11", cohort="training",
                 persona="Busy Decider", qualified=True, minutes_ago=60),
        _episode("CALL-4810", outcome="enrolled", tier=4, version="v11", cohort="training",
                 persona="Warm Champion", qualified=True, minutes_ago=240),
    ]
    escalations = [
        EscalationLog(escalation_id="ESC-204", episode_id="CALL-4819", reason="pricing_concession",
                      moment="Prospect demanded 30% off or walk; agent deferred.", turn_id=3,
                      lifecycle="unreviewed", created_at=datetime.now(timezone.utc) - timedelta(minutes=22)),
        EscalationLog(escalation_id="ESC-203", episode_id="CALL-4815", reason="human_requested",
                      moment="Asked for a real person; agent deferred to queue.", turn_id=2,
                      lifecycle="unreviewed", created_at=datetime.now(timezone.utc) - timedelta(minutes=60)),
        EscalationLog(escalation_id="ESC-201", episode_id="CALL-4802", reason="pricing_concession",
                      moment="Repeated discount pressure across 4 calls.", turn_id=5,
                      lifecycle="reviewed", created_at=datetime.now(timezone.utc) - timedelta(days=1)),
        EscalationLog(escalation_id="ESC-200", episode_id="CALL-4790", reason="human_requested",
                      moment="Manager callback booked.", turn_id=4,
                      lifecycle="resolved", created_at=datetime.now(timezone.utc) - timedelta(days=1)),
    ]
    app = create_app(read_store=FakeReadStore(episodes, escalations))
    return TestClient(app)


# =============================== P3 — CALLS LIST FILTERS =========================================


def test_episodes_list_returns_summaries_newest_first():
    client = _seeded_client()
    r = client.get("/api/episodes")
    assert r.status_code == 200, r.text
    body = r.json()
    # COMPLETED list only: the two unfinished calls (CALL-4821 in_progress/0-turn, CALL-4822
    # unset-outcome/0-turn) are excluded, leaving the 5 genuinely completed episodes.
    assert body["count"] == 5
    # newest-first among completed: the most recent COMPLETED call leads (not the live one).
    row = body["episodes"][0]
    assert row["episode_id"] == "CALL-4820"
    # labels applied — no bare ladder int as the only outcome signal.
    assert row["outcome"] == "Enrolled"
    assert row["ladder_label"] == "Same-call enrollment"
    assert "transcript" not in row and "turns" not in row  # summary, not full body


def test_episodes_list_excludes_in_progress_and_zero_turn():
    """The completed Calls list must surface ONLY genuinely completed episodes — never an
    in-progress or 0-turn (still-connecting) call (the leak QA found at the top of P3)."""
    client = _seeded_client()
    ids = {e["episode_id"] for e in client.get("/api/episodes").json()["episodes"]}
    assert "CALL-4821" not in ids  # in_progress + 0 turns
    assert "CALL-4822" not in ids  # outcome unset + 0 turns
    assert ids == {"CALL-4820", "CALL-4819", "CALL-4818", "CALL-4816", "CALL-4810"}
    # an explicit outcome=in_progress filter must STILL return nothing on the completed list.
    r = client.get("/api/episodes", params={"outcome": "in_progress"})
    assert r.json()["count"] == 0


def test_episodes_filter_by_version_and_cohort():
    client = _seeded_client()
    r = client.get("/api/episodes", params={"version": "v12", "cohort": "held_out"})
    assert r.status_code == 200, r.text
    ids = {e["episode_id"] for e in r.json()["episodes"]}
    assert ids == {"CALL-4820", "CALL-4819", "CALL-4818"}  # v12 AND held_out only

    r2 = client.get("/api/episodes", params={"version": "v11"})
    ids2 = {e["episode_id"] for e in r2.json()["episodes"]}
    assert ids2 == {"CALL-4816", "CALL-4810"}


def test_episodes_filter_by_outcome_and_escalated():
    client = _seeded_client()
    r = client.get("/api/episodes", params={"outcome": "enrolled"})
    ids = {e["episode_id"] for e in r.json()["episodes"]}
    assert ids == {"CALL-4820", "CALL-4810"}

    r2 = client.get("/api/episodes", params={"escalated": "true"})
    ids2 = {e["episode_id"] for e in r2.json()["episodes"]}
    assert ids2 == {"CALL-4819"}


# =============================== P2 — CALL REVIEW (decision trace + trajectory) ==================


def test_episode_detail_returns_decision_trace_and_belief_trajectory():
    client = _seeded_client()
    r = client.get("/api/episodes/CALL-4820")
    assert r.status_code == 200, r.text
    ep = r.json()
    assert ep["episode_id"] == "CALL-4820"
    # per-turn decision trace: agent turns carry a readable decision label + rationale.
    agent_turns = [t for t in ep["turns"] if t["speaker"] == "agent"]
    assert agent_turns and all(t["decision_label"] for t in agent_turns)
    assert any("close" in (t["decision_label"] or "").lower() for t in agent_turns)
    # belief trajectory: one entry per turn, in order, carrying the prioritized signals.
    assert len(ep["belief_trajectory"]) == len(ep["turns"])
    last_belief = ep["turns"][-1]["belief"]
    assert last_belief is not None
    trust = next(d for d in last_belief["primary_drivers"] if d["key"] == "trust")
    assert abs(trust["value"] - 0.66) < 1e-6


def test_episode_detail_404_for_unknown():
    client = _seeded_client()
    r = client.get("/api/episodes/NOPE-9999")
    assert r.status_code == 404, r.text


def test_episode_detail_zero_turn_is_empty_not_404():
    """Opening Review for a still-connecting (0-turn) episode returns a valid, empty payload (the
    page renders a graceful empty state) — never a 404 or a blank screen."""
    client = _seeded_client()
    r = client.get("/api/episodes/CALL-4821")  # in_progress, 0 turns
    assert r.status_code == 200, r.text
    ep = r.json()
    assert ep["episode_id"] == "CALL-4821"
    assert ep["turns"] == []
    assert ep["belief_trajectory"] == []
    assert ep["turn_count"] == 0


# =============================== P4 — KPI: LADDER HEADLINE + DISTINCT ENROLLMENT =================


def test_kpis_carry_ladder_headline_and_distinct_enrollment_rate():
    client = _seeded_client()
    r = client.get("/api/kpis", params={"version": "v12"})
    assert r.status_code == 200, r.text
    k = r.json()
    # v12 set: tiers [0,4,2,0] -> weighted-ladder mean = 1.5; enrollment = 1 of 4 = 0.25.
    assert abs(k["weighted_ladder_score"] - 1.5) < 1e-6
    assert abs(k["enrollment_rate"] - 0.25) < 1e-6
    # the two headline numbers are DISTINCT (different scales, computed from different fields).
    assert k["weighted_ladder_score"] != k["enrollment_rate"]
    assert k["ladder_max"] == 4
    assert k["n"] == 4
    # ladder distribution + archetype conversion present for the secondary panels, labeled.
    assert any(d["label"] == "Same-call enrollment" for d in k["ladder_distribution"])
    assert k["archetype_conversion"]


def test_kpis_compare_versions_arm():
    client = _seeded_client()
    r = client.get("/api/kpis", params={"version": "v12", "compare_version": "v11"})
    assert r.status_code == 200, r.text
    cmp = r.json()["compare"]
    # v11 set: tiers [2,4] -> ladder mean 3.0; v12 ladder mean 1.5; champion arm is v12 (the filter).
    assert abs(cmp["champion_ladder_score"] - 1.5) < 1e-6
    assert abs(cmp["baseline_ladder_score"] - 3.0) < 1e-6
    assert "delta" in cmp and "delta_ci" in cmp and "challenger_better" in cmp
    # enrollment rate carried distinctly in the compare arm too.
    assert "champion_enrollment_rate" in cmp and "baseline_enrollment_rate" in cmp


def test_kpis_empty_data_is_zeroed_not_error():
    app = create_app(read_store=FakeReadStore([], []))
    client = TestClient(app)
    r = client.get("/api/kpis", params={"version": "vX"})
    assert r.status_code == 200, r.text
    k = r.json()
    assert k["n"] == 0
    assert k["weighted_ladder_score"] == 0.0
    assert k["enrollment_rate"] == 0.0


# =============================== P5 — ESCALATION QUEUE (lifecycle) ===============================


def test_escalations_list_by_lifecycle_with_counts():
    client = _seeded_client()
    r = client.get("/api/escalations", params={"lifecycle": "unreviewed"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert all(e["lifecycle"] == "unreviewed" for e in body["escalations"])
    # reason translated to a label, raw key preserved for analytics (internal only). FIFO order
    # (oldest first), so ESC-203 (human-requested, 60m ago) precedes ESC-204 (concession, 22m ago).
    e_concession = next(e for e in body["escalations"] if e["escalation_id"] == "ESC-204")
    assert e_concession["reason"] == "Pricing concession"
    assert e_concession["reason_key"] == "pricing_concession"
    e_human = next(e for e in body["escalations"] if e["escalation_id"] == "ESC-203")
    assert e_human["reason"] == "Human requested"
    # counts cover all lifecycle states regardless of the filter.
    assert body["counts"]["unreviewed"] == 2
    assert body["counts"]["reviewed"] == 1
    assert body["counts"]["resolved"] == 1


def test_escalations_links_to_call():
    client = _seeded_client()
    r = client.get("/api/escalations", params={"lifecycle": "unreviewed"})
    esc = next(e for e in r.json()["escalations"] if e["escalation_id"] == "ESC-204")
    assert esc["episode_id"] == "CALL-4819"  # links to the call for the "Open call" action


def test_escalations_empty_state():
    app = create_app(read_store=FakeReadStore([], []))
    client = TestClient(app)
    r = client.get("/api/escalations", params={"lifecycle": "unreviewed"})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0
    assert r.json()["counts"]["unreviewed"] == 0


# =============================== P1 — LIVE MONITOR (prioritized belief IA) =======================


def test_live_returns_most_recent_call_with_prioritized_belief():
    # A still-connecting newest call (in_progress, 0 turns) is returned as the LIVE call with
    # active=True; its belief signals are simply null (the page shows a "connecting…" state, not a
    # broken wall of dashes). The full prioritized-IA values are exercised below with a live call
    # that HAS turns.
    eps = [
        _episode("CALL-LIVE", outcome="in_progress", tier=0, version="v12", cohort="live",
                 persona="Skeptical Analyzer", qualified=False, minutes_ago=0, turn_count=4),
    ]
    app = create_app(read_store=FakeReadStore(eps, []))
    client = TestClient(app)
    snap = client.get("/api/live").json()
    assert snap["active"] is True  # in_progress call is active
    assert snap["episode"]["episode_id"] == "CALL-LIVE"
    pr = snap["priority"]
    # the FOUR foremost signals are hoisted: trust, walk-away risk, stage, last act, imminence.
    assert pr["trust"] is not None
    assert pr["bail_risk"] is not None
    assert pr["stage"] == "Closing"
    assert pr["last_act_label"] is not None
    assert "escalation_imminent" in pr


def test_live_zero_turn_active_call_is_connecting_not_dashes():
    """A 0-turn in_progress call is the 'connecting…' state: active True, an episode with no turns,
    and null priority signals — NOT a completed row dressed up as live with a wall of dashes."""
    eps = [
        _episode("CALL-CONNECTING", outcome="in_progress", tier=0, version="v12", cohort="live",
                 persona="Skeptical Analyzer", qualified=False, minutes_ago=0, turn_count=0),
    ]
    app = create_app(read_store=FakeReadStore(eps, []))
    client = TestClient(app)
    snap = client.get("/api/live").json()
    assert snap["active"] is True
    assert snap["episode"]["episode_id"] == "CALL-CONNECTING"
    assert snap["episode"]["turns"] == []  # nothing said yet
    pr = snap["priority"]
    assert pr["trust"] is None and pr["bail_risk"] is None  # no belief yet -> null, page shows connecting


def test_live_no_active_call_when_most_recent_is_completed():
    # only completed calls -> active False, episode is NULL (no completed-fallback in new contract).
    eps = [
        _episode("CALL-1", outcome="enrolled", tier=4, version="v12", cohort="live",
                 persona="Warm Champion", qualified=True, minutes_ago=5),
    ]
    app = create_app(read_store=FakeReadStore(eps, []))
    client = TestClient(app)
    snap = client.get("/api/live").json()
    assert snap["active"] is False
    assert snap["episode"] is None  # new: no completed-episode fallback; use /api/live/sample instead


def test_live_empty_store():
    app = create_app(read_store=FakeReadStore([], []))
    client = TestClient(app)
    snap = client.get("/api/live").json()
    assert snap["active"] is False
    assert snap["episode"] is None


# =============================== NO INTERNAL INDEX LEAKS INTO LABELS =============================


def test_no_raw_driver_slug_in_operator_facing_labels():
    client = _seeded_client()
    ep = client.get("/api/episodes/CALL-4820").json()
    belief = ep["turns"][-1]["belief"]
    labels_shown = [d["label"] for d in belief["primary_drivers"] + belief["secondary_drivers"]]
    # raw slugs must be translated; "bail_risk" must never be a shown label.
    assert "bail_risk" not in labels_shown
    assert "Walk-away risk" in labels_shown
    assert "Trust" in labels_shown


# --- live monitor: a stale 0-turn in-progress call is NOT shown as perpetually "Connecting…" -----

def test_live_snapshot_stale_zero_turn_in_progress_is_not_active():
    """A 0-turn in-progress episode older than the stale window is an abandoned/never-started dial —
    live_snapshot must return no-active-call so the monitor doesn't hang on "Connecting…" forever.
    A RECENT 0-turn in-progress call (a genuine just-started call) is still active."""
    from src.api.operate import live_snapshot

    stale = _episode("CALL-STALE", outcome="in_progress", tier=0, version="v", cohort="live",
                     persona="anxious_parent", qualified=False, minutes_ago=120, turn_count=0)
    snap = live_snapshot(stale)
    assert snap == {"active": False, "episode": None}

    fresh = _episode("CALL-FRESH", outcome="in_progress", tier=0, version="v", cohort="live",
                     persona="anxious_parent", qualified=False, minutes_ago=0, turn_count=0)
    assert live_snapshot(fresh)["active"] is True

    # Liveness is judged by LAST ACTIVITY (the heartbeat), NOT by "has turns". An in-progress call with
    # turns whose heartbeat has gone stale is an abandoned/never-finalized partial — NOT live (this is
    # the seed-cruft case that otherwise lingers in /api/live/active forever).
    abandoned = _episode("CALL-ABANDONED", outcome="in_progress", tier=0, version="v", cohort="live",
                         persona="anxious_parent", qualified=False, minutes_ago=120, turn_count=2)
    assert live_snapshot(abandoned) == {"active": False, "episode": None}

    # A call mid-conversation that is STILL beating (fresh heartbeat) is live regardless of turn count.
    live = _episode("CALL-LIVE", outcome="in_progress", tier=0, version="v", cohort="live",
                    persona="anxious_parent", qualified=False, minutes_ago=0, turn_count=2)
    assert live_snapshot(live)["active"] is True


def test_escalation_lifecycle_update_advances_state():
    """P5 triage WRITE: POST /api/escalations/{id}/lifecycle advances an escalation's state so the
    queue's Mark-reviewed/Resolve buttons drive a real workflow (was a GET-only queue with dead
    buttons). 400 on an unknown lifecycle, 404 on an unknown id."""
    client = _seeded_client()
    r = client.post("/api/escalations/ESC-204/lifecycle", json={"lifecycle": "reviewed"})
    assert r.status_code == 200, r.text
    assert r.json()["lifecycle"] == "reviewed"
    # It now appears under "reviewed" and the unreviewed count dropped.
    rev = client.get("/api/escalations?lifecycle=reviewed").json()
    assert any(e["escalation_id"] == "ESC-204" for e in rev["escalations"])
    assert client.post("/api/escalations/ESC-204/lifecycle", json={"lifecycle": "bogus"}).status_code == 400
    assert client.post("/api/escalations/NOPE/lifecycle", json={"lifecycle": "resolved"}).status_code == 404


# =============================== P1 — LIVE: NEW CONTRACT (dummy-kill + sub-routes) ===============


def test_live_get_active_call_by_episode_id():
    """`GET /api/live?episode_id=<id>` returns live_snapshot for that specific episode.
    This lets the page watch a selected queued call. 404 when the id is unknown."""
    client = _seeded_client()
    # CALL-4821 is in_progress — fetch it explicitly by id.
    r = client.get("/api/live", params={"episode_id": "CALL-4821"})
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap["episode"]["episode_id"] == "CALL-4821"
    # active is determined by outcome, not by query param
    assert snap["active"] is True

    # A completed episode can also be fetched by id (its active flag is False).
    r2 = client.get("/api/live", params={"episode_id": "CALL-4820"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["episode"]["episode_id"] == "CALL-4820"
    assert r2.json()["active"] is False

    # Unknown id -> 404.
    r3 = client.get("/api/live", params={"episode_id": "NOPE-9999"})
    assert r3.status_code == 404, r3.text


def test_live_no_params_returns_active_not_completed_fallback():
    """`GET /api/live` (no params) returns the newest ACTIVE call, not the newest overall.
    When the store has mixed active+completed rows, the completed ones must never pollute
    the no-param live feed (the dummy-kill: completed episodes were leaking to P1 before)."""
    client = _seeded_client()
    snap = client.get("/api/live").json()
    # The seeded store has CALL-4821 (in_progress, 0 turns, 0 min ago) as the newest active call.
    assert snap["active"] is True
    assert snap["episode"]["episode_id"] == "CALL-4821"


def test_live_active_list_returns_all_non_stale_active_calls():
    """`GET /api/live/active` returns ALL non-stale active calls in newest-first order, with the
    required summary fields: episode_id, channel, persona_label, stage, trust, bail_risk,
    turn_count, escalation_imminent, started_at. persona_label must be humanized (no raw slug).
    No raw cohort/version slugs in any returned field."""
    client = _seeded_client()
    r = client.get("/api/live/active")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "count" in body and "calls" in body
    # Two active non-stale calls in the seeded store: CALL-4821 (in_progress, 0 min) and
    # CALL-4822 (outcome=None, 0 turns, 1 min ago — also active and non-stale).
    assert body["count"] == 2
    ids = {c["episode_id"] for c in body["calls"]}
    assert ids == {"CALL-4821", "CALL-4822"}

    # Required fields present on each call row.
    for call in body["calls"]:
        assert "episode_id" in call
        assert "channel" in call
        assert "persona_label" in call          # humanized — never a raw persona key
        assert "stage" in call
        assert "trust" in call
        assert "bail_risk" in call
        assert "turn_count" in call
        assert "escalation_imminent" in call
        assert "started_at" in call

    # Persona label must be human-readable: no raw internal key like "anxious_parent".
    # (The seeded personas are already human-readable strings like "Skeptical Analyzer"; the point
    # is that the field is persona_label, not the raw persona slug from the config.)
    for call in body["calls"]:
        # No raw cohort slug in any returned field (cohort is an internal tag, not displayed).
        assert "cohort" not in call
        # No version slug in the call row.
        assert "version" not in call

    # Newest-first: CALL-4821 (0 min ago) before CALL-4822 (1 min ago).
    assert body["calls"][0]["episode_id"] == "CALL-4821"


def test_live_active_list_excludes_completed_and_stale():
    """`/api/live/active` must never include completed episodes or stale 0-turn in-progress rows."""
    from src.api.operate import _LIVE_STALE_SECONDS

    stale_ep = _episode(
        "CALL-STALE-A", outcome="in_progress", tier=0, version="v", cohort="live",
        persona="Skeptical Analyzer", qualified=False,
        minutes_ago=int(_LIVE_STALE_SECONDS / 60) + 5, turn_count=0,
    )
    completed_ep = _episode(
        "CALL-DONE", outcome="enrolled", tier=4, version="v", cohort="live",
        persona="Warm Champion", qualified=True, minutes_ago=2,
    )
    fresh_active_ep = _episode(
        "CALL-FRESH-A", outcome="in_progress", tier=0, version="v", cohort="live",
        persona="Busy Decider", qualified=False, minutes_ago=1, turn_count=1,
    )
    app = create_app(read_store=FakeReadStore([stale_ep, completed_ep, fresh_active_ep], []))
    client = TestClient(app)
    r = client.get("/api/live/active")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {c["episode_id"] for c in body["calls"]}
    assert "CALL-STALE-A" not in ids   # stale 0-turn in-progress
    assert "CALL-DONE" not in ids      # completed
    assert "CALL-FRESH-A" in ids       # fresh active


def test_live_active_list_empty_when_no_active_calls():
    """`/api/live/active` returns count=0 and an empty calls list when no active calls exist."""
    eps = [
        _episode("CALL-DONE-1", outcome="enrolled", tier=4, version="v", cohort="c",
                 persona="Warm Champion", qualified=True, minutes_ago=10),
        _episode("CALL-DONE-2", outcome="disqualified", tier=0, version="v", cohort="c",
                 persona="Price Hawk", qualified=False, minutes_ago=20),
    ]
    app = create_app(read_store=FakeReadStore(eps, []))
    client = TestClient(app)
    r = client.get("/api/live/active")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0
    assert body["calls"] == []


def test_live_sample_returns_newest_completed_with_sample_flag():
    """`GET /api/live/sample` returns live_snapshot of the newest COMPLETED episode with
    `"sample": true` and `active: false`. The page's "Show sample call" toggle hits this to
    give operators a reference call to orient the monitor when nothing is live."""
    client = _seeded_client()
    r = client.get("/api/live/sample")
    assert r.status_code == 200, r.text
    snap = r.json()
    # sample: true must be present and active must be false.
    assert snap.get("sample") is True
    assert snap["active"] is False
    # Must be a real completed episode (not the in-progress CALL-4821/CALL-4822).
    assert snap["episode"] is not None
    ep_id = snap["episode"]["episode_id"]
    assert ep_id not in {"CALL-4821", "CALL-4822"}
    # The newest completed episode in the seeded store is CALL-4820 (enrolled, 6 min ago).
    assert ep_id == "CALL-4820"


def test_live_sample_empty_when_no_completed_calls():
    """`/api/live/sample` returns the null-payload with sample:true when there are no completed
    episodes (e.g. fresh store or all-active store)."""
    eps = [
        _episode("CALL-ACTIVE", outcome="in_progress", tier=0, version="v", cohort="c",
                 persona="Skeptical Analyzer", qualified=False, minutes_ago=0, turn_count=1),
    ]
    app = create_app(read_store=FakeReadStore(eps, []))
    client = TestClient(app)
    r = client.get("/api/live/sample")
    assert r.status_code == 200, r.text
    snap = r.json()
    assert snap == {"active": False, "episode": None, "sample": True}


def test_live_active_uses_same_predicate_as_live_snapshot():
    """The active+stale predicate in /api/live/active must be consistent with live_snapshot's
    active determination — a call active in live_snapshot is active in /api/live/active and
    vice versa. Regression guard so the two code paths can't silently drift."""
    from src.api.operate import _is_active

    now = datetime.now(timezone.utc)
    # fresh in_progress with turns -> active
    ep_fresh = _episode("EP-F", outcome="in_progress", tier=0, version="v", cohort="c",
                        persona="Busy Decider", qualified=False, minutes_ago=1, turn_count=2)
    assert _is_active(ep_fresh, now=now) is True

    # completed -> not active
    ep_done = _episode("EP-D", outcome="enrolled", tier=4, version="v", cohort="c",
                       persona="Warm Champion", qualified=True, minutes_ago=5)
    assert _is_active(ep_done, now=now) is False

    # stale 0-turn in_progress -> not active
    from src.api.operate import _LIVE_STALE_SECONDS
    ep_stale = _episode("EP-S", outcome="in_progress", tier=0, version="v", cohort="c",
                        persona="Skeptical Analyzer", qualified=False,
                        minutes_ago=int(_LIVE_STALE_SECONDS / 60) + 10, turn_count=0)
    assert _is_active(ep_stale, now=now) is False


# =============================== CB-06 — PROSPECT TRAJECTORY IN EPISODE DETAIL ====================

# The 6 frozen driver keys the contract mandates in each trajectory entry.
_TRAJ_DRIVER_KEYS = frozenset({"trust", "need", "urgency", "purchase_intent", "budget", "patience"})

# Canonical prospect trajectory used to seed the sim-channel episode below.
_SAMPLE_TRAJ = [
    {"turn": 1, "drivers": {"trust": 0.32, "need": 0.55, "urgency": 0.4, "purchase_intent": 0.1, "budget": 0.9, "patience": 0.47}},
    {"turn": 2, "drivers": {"trust": 0.46, "need": 0.69, "urgency": 0.54, "purchase_intent": 0.24, "budget": 0.9, "patience": 0.44}},
    {"turn": 3, "drivers": {"trust": 0.6, "need": 0.83, "urgency": 0.68, "purchase_intent": 0.38, "budget": 0.9, "patience": 0.41}},
]


def _sim_episode(eid: str, *, traj=None) -> Episode:
    """A channel='sim' episode with an optional prospect_trajectory in its metrics. Simulates what
    run_episode produces for a sim/twin episode (CB-06). Real voice episodes use _episode() above and
    do NOT have prospect_trajectory in their metrics."""
    created = datetime.now(timezone.utc) - timedelta(minutes=10)
    turns = [
        Turn(turn_id=0, speaker="agent", text="Hi, how can I help?",
             decision="greeting", rationale="opener", belief=_belief(0.3, 0.2, "discovery"), latency_ms=500),
        Turn(turn_id=1, speaker="prospect", text="We have a kid in grade 9 struggling with algebra."),
        Turn(turn_id=2, speaker="agent", text="Got it — what subject is most urgent?",
             decision="ask", rationale="discovery", belief=_belief(0.4, 0.25, "discovery"), latency_ms=600),
        Turn(turn_id=3, speaker="prospect", text="Math, for sure."),
        Turn(turn_id=4, speaker="agent", text="Want me to set up a consultation?",
             decision="attempt_close", rationale="buying signal", belief=_belief(0.6, 0.3, "closing"), latency_ms=700),
        Turn(turn_id=5, speaker="prospect", text="Sure, let's set up a consultation."),
    ]
    metrics: dict = {
        "turn_count": 3,
        "committed_tier": "consultation",
        "prospect_walked": False,
    }
    if traj is not None:
        metrics["prospect_trajectory"] = traj
    return Episode(
        episode_id=eid, turns=turns, outcome="consult_booked", ladder_tier=2, qualified=True,
        version="champion_v0", kb_version="kb-37", channel="sim", persona="anxious_parent",
        cohort="training", escalated=False, metrics=metrics, created_at=created,
    )


def test_episode_detail_includes_prospect_trajectory_for_sim_episode():
    """CB-06: episode_detail surfaces prospect_trajectory for a sim-channel episode that has it in
    metrics. The list must be present at the top level as 'prospect_trajectory', be non-empty, and
    each entry must have 'turn' + 'drivers' with the 6 frozen keys."""
    sim_ep = _sim_episode("SIM-0001", traj=_SAMPLE_TRAJ)
    app = create_app(read_store=FakeReadStore([sim_ep], []))
    client = TestClient(app)

    r = client.get("/api/episodes/SIM-0001")
    assert r.status_code == 200, r.text
    detail = r.json()

    assert "prospect_trajectory" in detail, "prospect_trajectory missing from episode_detail"
    traj = detail["prospect_trajectory"]
    assert isinstance(traj, list), f"expected list, got {type(traj)}"
    assert len(traj) == len(_SAMPLE_TRAJ), f"expected {len(_SAMPLE_TRAJ)} entries, got {len(traj)}"

    for i, entry in enumerate(traj):
        assert "turn" in entry, f"entry {i} missing 'turn' key"
        assert "drivers" in entry, f"entry {i} missing 'drivers' key"
        assert entry["turn"] == i + 1, f"entry {i} has turn={entry['turn']}, expected {i + 1}"
        assert set(entry["drivers"].keys()) == _TRAJ_DRIVER_KEYS, (
            f"entry {i} driver keys: {set(entry['drivers'].keys())}"
        )


def test_episode_detail_prospect_trajectory_empty_for_real_voice_episode():
    """CB-06: episode_detail returns prospect_trajectory=[] for a real (voice/text) episode that
    has no prospect_trajectory in its metrics. The field must be present (not absent) to let the
    frontend reliably detect 'no truth data, hide the panel'."""
    # Use the standard voice episode fixture — it has no prospect_trajectory in metrics.
    voice_ep = _episode("VOICE-0001", outcome="enrolled", tier=4, version="v12",
                        cohort="held_out", persona="Warm Champion", qualified=True, minutes_ago=5)
    app = create_app(read_store=FakeReadStore([voice_ep], []))
    client = TestClient(app)

    r = client.get("/api/episodes/VOICE-0001")
    assert r.status_code == 200, r.text
    detail = r.json()

    assert "prospect_trajectory" in detail, (
        "prospect_trajectory must always be present in episode_detail ([] for non-sim episodes)"
    )
    assert detail["prospect_trajectory"] == [], (
        f"expected [] for a voice episode, got {detail['prospect_trajectory']!r}"
    )


def test_episode_detail_prospect_trajectory_empty_when_metrics_is_none():
    """CB-06: episode_detail returns prospect_trajectory=[] even when metrics is None/absent —
    the field must never raise or be missing regardless of how sparse the stored episode is."""
    from src.api.operate import episode_detail

    sparse_ep = Episode(
        episode_id="SPARSE-001", turns=[], outcome="walked", ladder_tier=0, qualified=False,
        version="v1", kb_version="kb-0", channel="voice", persona="unknown", cohort="live",
        escalated=False, metrics=None,
    )
    detail = episode_detail(sparse_ep)
    assert "prospect_trajectory" in detail
    assert detail["prospect_trajectory"] == []

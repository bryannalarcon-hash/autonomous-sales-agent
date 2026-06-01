# End-to-end tests for the operator-dashboard READ API (plan U15 — Operate mode). Drives the
# FastAPI surface (src.api.server.create_app) with fastapi.testclient.TestClient and an INJECTED
# in-memory ReadStore seeded with Episodes + EscalationLogs — so nothing hits Postgres or the
# network (mirrors how test_demo_call injects a MockLLMClient). Asserts the gradeable contract the
# 5 Operate pages depend on: /api/episodes filters by version+cohort+outcome+escalated; /api/kpis
# carries the weighted-ladder headline AND a DISTINCT enrollment_rate (and a compare arm); the
# escalation queue lists by lifecycle with counts; Call Review returns the decision trace + belief
# trajectory; /api/live hoists the prioritized belief IA. Also guards empty data + verifies NO raw
# internal index (ladder int alone, driver slug) leaks into operator-facing labels.
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
    return Episode(
        episode_id=eid, turns=turns, outcome=outcome, ladder_tier=tier, qualified=qualified,
        version=version, kb_version="kb-37", channel="voice", persona=persona, cohort=cohort,
        escalated=escalated, metrics={"duration_ms": 348000, "talk_listen": [44, 56]}, created_at=created,
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
    # only completed calls -> active False, but still hands back the most recent for context.
    eps = [
        _episode("CALL-1", outcome="enrolled", tier=4, version="v12", cohort="live",
                 persona="Warm Champion", qualified=True, minutes_ago=5),
    ]
    app = create_app(read_store=FakeReadStore(eps, []))
    client = TestClient(app)
    snap = client.get("/api/live").json()
    assert snap["active"] is False
    assert snap["episode"]["episode_id"] == "CALL-1"


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

    # An in-progress call that HAS turns is genuinely live regardless of age.
    started = _episode("CALL-LIVE", outcome="in_progress", tier=0, version="v", cohort="live",
                       persona="anxious_parent", qualified=False, minutes_ago=120, turn_count=2)
    assert live_snapshot(started)["active"] is True


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

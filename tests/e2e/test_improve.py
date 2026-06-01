# End-to-end tests for the operator-dashboard IMPROVE API (plan U16 — Improve mode). Drives the
# FastAPI surface (src.api.server.create_app) with fastapi.testclient.TestClient and an INJECTED
# in-memory ImproveStore seeded with ExperimentRecords + a VersionLineage tree — so nothing hits
# Postgres or the network (mirrors how test_operate injects a FakeReadStore). Asserts the gradeable
# Improve contract: an EXTREME challenger surfaces in /api/approvals and ONLY .../approve promotes it
# (champion changes; reject leaves it unchanged) (AE6); a KB/playbook SAVE creates a DRAFT challenger
# WITHOUT mutating the live champion config/version (R20); /api/versions/{v}/rollback restores a prior
# version as champion; /api/experiments returns the discovery-sequencing before/after (delta +
# significance + declared diff) — the demo artifact, queryable end-to-end. Also verifies NO raw
# internal index (dimension slug, state slug) leaks into operator-facing labels.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.config.settings import load_config
from src.memory.schema import ExperimentRecord, VersionLineage


# --- a seeded in-memory ImproveStore (DB-free) ---------------------------------------------------


class FakeImproveStore:
    """In-memory ImproveStore satisfying src.api.improve.ImproveStore. Single-champion is enforced on
    record_version exactly like the real store (demote any other champion first), and list_lineage
    returns the seeded tree newest-first so P9 renders without a Postgres list-all."""

    def __init__(
        self, experiments: list[ExperimentRecord], lineage: list[VersionLineage]
    ) -> None:
        self._experiments = {e.experiment_id: e for e in experiments}
        self._lineage = {n.version: n for n in lineage}

    async def list_experiments(
        self, *, state: Optional[str] = None, limit: int = 200
    ) -> list[ExperimentRecord]:
        rows = [e for e in self._experiments.values() if state is None or e.state == state]
        rows.sort(key=lambda e: e.created_at, reverse=True)
        return rows[:limit]

    async def get_experiment(self, experiment_id: str) -> Optional[ExperimentRecord]:
        return self._experiments.get(experiment_id)

    async def save_experiment(self, experiment: ExperimentRecord) -> str:
        self._experiments[experiment.experiment_id] = experiment
        return experiment.experiment_id

    async def get_lineage(self, version: str) -> Optional[VersionLineage]:
        return self._lineage.get(version)

    async def get_champion(self) -> Optional[VersionLineage]:
        return next((n for n in self._lineage.values() if n.is_champion), None)

    async def record_version(self, lineage: VersionLineage) -> str:
        # Single-champion invariant (mirrors store.record_version): demote any other champion first.
        if lineage.is_champion:
            for v, node in self._lineage.items():
                if v != lineage.version and node.is_champion:
                    node.is_champion = False
        self._lineage[lineage.version] = lineage
        return lineage.version

    async def list_lineage(self, *, limit: int = 200) -> list[VersionLineage]:
        rows = list(self._lineage.values())
        rows.sort(key=lambda n: n.created_at, reverse=True)
        return rows[:limit]


def _exp(
    eid: str,
    *,
    challenger: str,
    parent: str,
    dimension: str,
    state: str,
    is_extreme: bool,
    guardrail: str = "pass",
    delta: float = 0.12,
    delta_ci: Optional[list[float]] = None,
    significance: float = 0.91,
    enroll_delta: float = 0.04,
    minutes_ago: int = 0,
    name: str = "",
    diff_description: str = "",
    guardrail_reason: Optional[str] = None,
) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=eid,
        challenger_version=challenger,
        parent_version=parent,
        name=name or eid,
        dimension=dimension,
        declared_diff=[dimension],
        diff_description=diff_description or f"changed {dimension}",
        population="Skeptical Analyzer · 20%",
        n=184,
        target=400,
        kb_version="kb_v0",
        champion_kpi=3.05,
        challenger_kpi=round(3.05 + delta, 4),
        delta=delta,
        delta_ci=delta_ci or [0.01, 0.23],
        challenger_better=(delta_ci or [0.01, 0.23])[0] > 0,
        enroll_delta=enroll_delta,
        significance=significance,
        guardrail=guardrail,
        guardrail_reason=guardrail_reason,
        champion_qual_acc=0.93,
        challenger_qual_acc=0.93,
        is_extreme=is_extreme,
        state=state,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


def _lineage(
    version: str, *, parent: Optional[str], kb: str, champion: bool, days_ago: int, ladder: float
) -> VersionLineage:
    return VersionLineage(
        version=version,
        parent_version=parent,
        kb_version=kb,
        kpi={"dimension": "playbooks.discovery_sequence", "challenger_kpi": ladder},
        is_champion=champion,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


def _seeded_client() -> TestClient:
    experiments = [
        # The HEADLINE demo artifact: the discovery-sequencing before/after, result-ready (passed).
        _exp(
            "EXP-31", challenger="v12-c2", parent="v12", dimension="playbooks.discovery_sequence",
            state="passed", is_extreme=False, guardrail="pass", delta=0.12,
            delta_ci=[0.01, 0.23], significance=0.91, enroll_delta=0.04, minutes_ago=5,
            name="ROI-first rebuttal ordering",
            diff_description="Reorder: lead ROI proof before pilot de-risk",
        ),
        # An EXTREME (pricing-concession) challenger that passed the bar -> blocked (pending approval).
        _exp(
            "EXP-29", challenger="v13-c2", parent="v12", dimension="thresholds.max_concession_band",
            state="blocked", is_extreme=True, guardrail="trip", delta=0.18,
            delta_ci=[0.05, 0.31], significance=0.95, enroll_delta=0.11, minutes_ago=20,
            name="Pilot price-floor concession",
            diff_description="max_concession_band 0.15 -> 0.25",
            guardrail_reason="Allows 25% pilot discount (cap 0.15)",
        ),
        # A failed challenger (past tab).
        _exp(
            "EXP-28", challenger="v9", parent="v12", dimension="persona",
            state="rejected", is_extreme=True, guardrail="warn", delta=-0.34,
            delta_ci=[-0.45, -0.21], significance=0.97, enroll_delta=-0.14, minutes_ago=240,
            name="Brisk-Expert persona",
        ),
    ]
    lineage = [
        _lineage("v12", parent="v10", kb="kb-37", champion=True, days_ago=2, ladder=3.42),
        _lineage("v10", parent="v7", kb="kb-35", champion=False, days_ago=10, ladder=3.05),
        _lineage("v7", parent=None, kb="kb-31", champion=False, days_ago=20, ladder=2.66),
    ]
    app = create_app(
        improve_store=FakeImproveStore(experiments, lineage),
        config=load_config("champion_v0"),
    )
    return TestClient(app)


# =============================== P6 — EXPERIMENT LAB (demo artifact) =============================


def test_experiments_return_discovery_sequencing_before_after():
    """AE: the discovery-sequencing experiment's before/after (delta + significance + declared diff)
    is queryable end-to-end — the headline demo artifact."""
    client = _seeded_client()
    r = client.get("/api/experiments")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    exp = next(e for e in body["experiments"] if e["experiment_id"] == "EXP-31")
    # BEFORE/AFTER: champion vs challenger KPI + the signed delta.
    assert exp["champion_kpi"] == 3.05
    assert abs(exp["challenger_kpi"] - 3.17) < 1e-6
    assert abs(exp["delta"] - 0.12) < 1e-6
    # significance + the delta CI (excludes 0 -> challenger_better True).
    assert abs(exp["significance"] - 0.91) < 1e-6
    assert exp["delta_ci"] == [0.01, 0.23]
    assert exp["challenger_better"] is True
    # the declared ONE-dimension diff (R17), translated to an operator-facing name.
    assert exp["declared_diff"] == ["playbooks.discovery_sequence"]
    assert exp["dimension_label"] == "Discovery sequencing"
    assert "Reorder" in exp["diff_description"]
    # state -> a readable chip; active/past split available for the lab tabs.
    assert exp["state"] == "passed"
    assert exp["state_label"] == "Result ready"
    assert body["counts"]["active"] >= 1


def test_experiments_filter_by_state():
    client = _seeded_client()
    r = client.get("/api/experiments", params={"state": "blocked"})
    ids = {e["experiment_id"] for e in r.json()["experiments"]}
    assert ids == {"EXP-29"}


def test_experiments_empty_is_not_error():
    app = create_app(improve_store=FakeImproveStore([], []), config=load_config("champion_v0"))
    client = TestClient(app)
    r = client.get("/api/experiments")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0


# =============================== P7 — APPROVAL QUEUE (AE6) =======================================


def test_extreme_challenger_appears_in_approvals_queue():
    """AE6: an extreme (guardrail-tripping) challenger that passed the bar is in the approval queue,
    and shows WHY it's extreme (the guardrail reason)."""
    client = _seeded_client()
    r = client.get("/api/approvals")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    apr = body["approvals"][0]
    assert apr["experiment_id"] == "EXP-29"
    assert apr["is_extreme"] is True
    assert apr["guardrail"] == "trip"
    assert "discount" in (apr["guardrail_reason"] or "").lower()


def test_approve_promotes_challenger_to_champion():
    """AE6: ONLY approve promotes the extreme challenger — the champion changes to the challenger."""
    client = _seeded_client()
    # before: v12 is champion.
    before = client.get("/api/versions").json()
    assert before["champion_version"] == "v12"

    r = client.post("/api/approvals/EXP-29/approve")
    assert r.status_code == 200, r.text
    assert r.json()["champion_version"] == "v13-c2"
    assert r.json()["experiment"]["state"] == "promoted"

    # after: the challenger is now the live champion (single-champion invariant restored it).
    after = client.get("/api/versions").json()
    assert after["champion_version"] == "v13-c2"
    # the experiment has left the approval queue (no longer blocked).
    assert client.get("/api/approvals").json()["count"] == 0


def test_reject_leaves_champion_unchanged():
    """AE6: reject does NOT promote — the champion is unchanged; the experiment is marked rejected."""
    client = _seeded_client()
    r = client.post("/api/approvals/EXP-29/reject")
    assert r.status_code == 200, r.text
    assert r.json()["experiment"]["state"] == "rejected"
    # champion unchanged.
    assert client.get("/api/versions").json()["champion_version"] == "v12"
    # cleared from the queue.
    assert client.get("/api/approvals").json()["count"] == 0


def test_approve_only_blocked_experiment():
    client = _seeded_client()
    # EXP-31 is `passed`, not `blocked` -> cannot be approved.
    r = client.post("/api/approvals/EXP-31/approve")
    assert r.status_code == 409, r.text
    r2 = client.post("/api/approvals/NOPE/approve")
    assert r2.status_code == 404, r2.text


# =============================== P8 — KB / PLAYBOOK SAVE = DRAFT (R20) ===========================


def test_playbook_save_creates_draft_without_mutating_champion():
    """R20: a playbook SAVE creates a DRAFT challenger; the live champion config/version is UNCHANGED
    (not a live mutation)."""
    client = _seeded_client()
    cfg_before = client.get("/api/playbook").json()
    champ_before = client.get("/api/versions").json()["champion_version"]

    r = client.post("/api/playbook", json={"name": "Reorder discovery"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_draft"] is True
    draft = body["draft"]
    # the draft is a NEW running experiment for a discovery-sequencing diff (one dimension, R17).
    assert draft["state"] == "running"
    assert draft["declared_diff"] == ["playbooks.discovery_sequence"]
    assert draft["dimension_label"] == "Discovery sequencing"
    assert draft["is_extreme"] is False

    # the live champion config is UNCHANGED (same version, same playbook).
    cfg_after = client.get("/api/playbook").json()
    assert cfg_after["version"] == cfg_before["version"]
    assert cfg_after["discovery_sequence"] == cfg_before["discovery_sequence"]
    # the champion lineage row is UNCHANGED — a save never calls record_version.
    assert client.get("/api/versions").json()["champion_version"] == champ_before
    # the draft IS queryable in the lab (it was persisted as a running experiment).
    lab = client.get("/api/experiments", params={"state": "running"}).json()
    assert any(e["experiment_id"] == draft["experiment_id"] for e in lab["experiments"])


def test_kb_save_creates_draft_without_mutating_champion():
    client = _seeded_client()
    cfg_before = client.get("/api/kb").json()
    r = client.post("/api/kb", json={"name": "ROI-first rebuttal"})
    assert r.status_code == 200, r.text
    draft = r.json()["draft"]
    assert r.json()["is_draft"] is True
    assert draft["state"] == "running"
    assert draft["is_extreme"] is False
    # the draft kb_version is a draft suffix; the live champion kb_version is unchanged.
    assert draft["kb_version"].endswith("-d1")
    cfg_after = client.get("/api/kb").json()
    assert cfg_after["kb_version"] == cfg_before["kb_version"]
    assert not cfg_after["kb_version"].endswith("-d1")


# =============================== P9 — VERSION HISTORY + ROLLBACK =================================


def test_versions_list_marks_champion():
    client = _seeded_client()
    r = client.get("/api/versions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["champion_version"] == "v12"
    champ = next(v for v in body["versions"] if v["version"] == "v12")
    assert champ["is_champion"] is True
    # the parent links form the tree.
    assert champ["parent_version"] == "v10"


def test_rollback_restores_prior_version_as_champion():
    """A rollback re-promotes a prior version -> get_champion() returns it (single-champion invariant
    demotes the current champion)."""
    client = _seeded_client()
    assert client.get("/api/versions").json()["champion_version"] == "v12"

    r = client.post("/api/versions/v10/rollback")
    assert r.status_code == 200, r.text
    assert r.json()["champion_version"] == "v10"

    after = client.get("/api/versions").json()
    assert after["champion_version"] == "v10"
    # exactly one champion (the prior one was demoted).
    champions = [v for v in after["versions"] if v["is_champion"]]
    assert [c["version"] for c in champions] == ["v10"]


def test_rollback_unknown_version_404():
    client = _seeded_client()
    r = client.post("/api/versions/v999/rollback")
    assert r.status_code == 404, r.text


# =============================== NO INTERNAL INDEX LEAKS INTO LABELS =============================


def test_no_raw_dimension_or_state_slug_in_operator_labels():
    client = _seeded_client()
    body = client.get("/api/experiments").json()
    for e in body["experiments"]:
        # the operator-facing label fields must be translated, never the raw namespaced slug.
        assert "." not in e["dimension_label"]  # no "playbooks.discovery_sequence" leaking
        assert "_" not in e["state_label"]  # no raw state slug as the shown chip text
        # the raw slug is still present under the internal-only keys (for client logic), not as label.
        assert e["state_label"] != e["state"] or e["state"] in ("draft",)

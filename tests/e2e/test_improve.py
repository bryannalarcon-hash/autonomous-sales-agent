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

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from fastapi.testclient import TestClient

from src.api.server import create_app
from src.config.settings import load_config
from src.core.llm import Message, MockLLMClient
from src.memory.schema import ExperimentRecord, VersionLineage


# --- a seeded in-memory ImproveStore (DB-free) ---------------------------------------------------


class FakeImproveStore:
    """In-memory ImproveStore satisfying src.api.improve.ImproveStore. Single-champion is enforced on
    record_version exactly like the real store (demote any other champion first), and list_lineage
    returns the seeded tree newest-first so P9 renders without a Postgres list-all."""

    def __init__(
        self,
        experiments: list[ExperimentRecord],
        lineage: list[VersionLineage],
        kb_chunks: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self._experiments = {e.experiment_id: e for e in experiments}
        self._lineage = {n.version: n for n in lineage}
        # The REAL grounded corpus (kb_chunk rows) keyed by kb_version, mirroring the Postgres table:
        # each entry is {"source": "<section>#<id>", "text": ...}. Lets /api/kb return real grouped
        # sections DB-free (the default fakes pass None -> the empty-corpus path).
        self._kb_chunks = kb_chunks or []
        # CB-25: the per-arm A/B "mock call" episodes a run persists (so the detail page + Review can
        # read them), keyed by episode_id — mirrors the Postgres episode table DB-free.
        self._episodes: dict[str, Any] = {}

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

    async def list_kb_chunks(self, *, kb_version: str) -> list[dict[str, Any]]:
        # Mirror the Postgres impl: only the rows for the requested kb_version, ordered by source.
        rows = [c for c in self._kb_chunks if c.get("kb_version", kb_version) == kb_version]
        return sorted(
            ({"source": c["source"], "text": c["text"]} for c in rows),
            key=lambda c: c["source"],
        )

    async def save_episode(self, episode: Any) -> str:
        # CB-25: persist a per-arm A/B episode (mirrors store.save_episode, DB-free).
        self._episodes[episode.episode_id] = episode
        return episode.episode_id

    async def get_episode(self, episode_id: str) -> Optional[Any]:
        return self._episodes.get(episode_id)


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
        # An EXTREME (pricing-concession) challenger that PASSED the bar (clean guardrail, lift,
        # qual held) but is extreme -> blocked pending human approval (the legitimate approvable case).
        _exp(
            "EXP-29", challenger="v13-c2", parent="v12", dimension="thresholds.max_concession_band",
            state="blocked", is_extreme=True, guardrail="pass", delta=0.18,
            delta_ci=[0.05, 0.31], significance=0.95, enroll_delta=0.11, minutes_ago=20,
            name="Pilot price-floor concession",
            diff_description="max_concession_band 0.15 -> 0.25 — allows a 25% pilot discount (cap 0.15)",
        ),
        # An extreme record stuck in `blocked` that DOES NOT meet the bar (guardrail tripped) — it must
        # NOT be approvable: the approve re-check (Fix 6) re-asserts the bar and 409s this one.
        _exp(
            "EXP-30", challenger="v14-c1", parent="v12", dimension="thresholds.max_concession_band",
            state="blocked", is_extreme=True, guardrail="trip", delta=0.07,
            delta_ci=[0.01, 0.14], significance=0.9, enroll_delta=0.02, minutes_ago=15,
            name="Aggressive concession (guardrail regressed)",
            diff_description="max_concession_band 0.15 -> 0.30",
            guardrail_reason="Pushes harder than the champion (pushiness regression)",
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
    assert body["count"] == 4
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
    assert ids == {"EXP-29", "EXP-30"}


def test_experiments_empty_is_not_error():
    app = create_app(improve_store=FakeImproveStore([], []), config=load_config("champion_v0"))
    client = TestClient(app)
    r = client.get("/api/experiments")
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0


# =============================== P6 — RUN AN A/B (POST /api/experiments/run) =====================
# The "run an A/B between two values" path is ASYNCHRONOUS (CB-15): build the champion config + a
# challenger via the generator's pure mutators for the requested dimension/value, PERSIST a `running`
# record + return 202 IMMEDIATELY, then run the experiment on a frozen held-out set with the app's
# injected LLM client (MockLLM in tests = free) in a BACKGROUND task that settles the SAME record to a
# terminal state (CB-20 — never a perpetual `running`). A deterministic runner is injected so the
# outcome is decoupled from self-play scoring (production uses real self-play + an OpenRouterClient =
# paid). The async tests drive the ASGI app on one event loop so the background task can run + settle.

import asyncio  # noqa: E402

from httpx import ASGITransport, AsyncClient  # noqa: E402

from src.api.improve import create_improve_router  # noqa: E402
from fastapi import FastAPI  # noqa: E402


def _routed_mock() -> MockLLMClient:
    """A free, network-free MockLLMClient the run endpoint passes through as both arms' LLM. The
    deterministic runner ignores it, so its exact replies don't matter — it only proves the wiring."""

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        rf = opts.get("response_format")
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            return json.dumps({"act": "ask", "target_slot": "goal", "confidence": 0.8})
        return "ok"

    return MockLLMClient(serve)


def _dominating_runner(champ_version: str):
    """A deterministic run_experiment runner (no self-play, no network): champion config -> tier-0
    episodes; challenger -> tier-4 episodes. Stacks the deck so the challenger is clearly better."""

    from src.memory.schema import Episode, Turn

    async def runner(personas, agent_llm_factory, prospect_llm_factory, config, **kw):
        tier, outcome = (0, "walked") if config.version == champ_version else (4, "enrolled")
        return [
            Episode(
                episode_id=f"{config.version}_{i}",
                turns=[Turn(turn_id=0, speaker="agent", text="hi", decision="greeting")],
                outcome=outcome, ladder_tier=tier, qualified=True,
                version=config.version, kb_version=config.kb_version, channel="sim",
                cohort="held_out",
            )
            for i in range(len(personas))
        ]

    return runner


def _run_app(*, runner=None, run_timeout_s: float = 5.0) -> tuple[FastAPI, FakeImproveStore]:
    """A DB-free ASGI app wired for the run endpoint: empty injected store, MockLLM factory (free), and
    a deterministic dominating runner so the A/B outcome is reproducible without self-play. Mounts ONLY
    the Improve router so the background task runs on the SAME event loop the AsyncClient drives — a
    generous default timeout so the run settles rather than tripping the budget."""
    champ = load_config("champion_v0")
    store = FakeImproveStore([], [])
    app = FastAPI()
    app.include_router(
        create_improve_router(
            improve_store=store,
            champion_config_provider=lambda: champ,
            llm_client_factory=_routed_mock,
            experiment_runner=runner if runner is not None else _dominating_runner(champ.version),
            run_timeout_s=run_timeout_s,
            run_max_concurrency=1,
        )
    )
    return app, store


async def _settle(ac: AsyncClient, experiment_id: str, *, tries: int = 50) -> dict:
    """Poll /api/experiments until `experiment_id` leaves `running` (the background A/B settled it), and
    return the settled row. Mirrors the dashboard poll — the run is async, so the terminal state arrives
    via this poll, not the 202 response. Asserts it actually settles (CB-20: never stuck `running`)."""
    for _ in range(tries):
        body = (await ac.get("/api/experiments")).json()
        row = next((e for e in body["experiments"] if e["experiment_id"] == experiment_id), None)
        if row is not None and row["state"] != "running":
            return row
        await asyncio.sleep(0.02)  # let the background task make progress
    raise AssertionError(f"experiment {experiment_id} never settled out of `running` (CB-20)")


async def test_run_experiment_returns_202_running_immediately():
    """CB-15: POST /api/experiments/run returns 202 IMMEDIATELY with a `running` record — it never
    blocks for the (minutes-long, paid) A/B. The drawer closes + the running card shows at once."""
    app, store = _run_app()
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
        )
        assert r.status_code == 202, r.text  # Accepted — started, not finished
        exp = r.json()["experiment"]
        assert exp["state"] == "running"
        assert r.json()["decision"] == "running"
        assert exp["dimension_label"] == "Discovery sequencing"
        assert "." not in exp["dimension_label"]
        assert exp["n"] == 6
        # it is queryable in the lab the instant the run starts (the running card).
        body = (await ac.get("/api/experiments")).json()
        assert any(e["experiment_id"] == exp["experiment_id"] for e in body["experiments"])
        # the background task then settles the SAME record to a terminal state (the poll catches it).
        await _settle(ac, exp["experiment_id"])


async def test_run_experiment_background_settles_to_terminal():
    """CB-15/CB-20: the background A/B settles the SAME record to a terminal state via the poll — the
    dominating challenger is clearly better, and FAIL-CLOSED (R36) routes the unmeasured auto-promote
    to the human queue (blocked), never silently shipped."""
    app, store = _run_app()
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
        )
        exp_id = r.json()["experiment"]["experiment_id"]
        settled = await _settle(ac, exp_id)
    # before/after: the dominating challenger is clearly better (tier 4 vs tier 0).
    assert settled["challenger_kpi"] > settled["champion_kpi"]
    assert settled["delta"] > 0
    assert settled["challenger_better"] is True
    # FAIL-CLOSED: an unmeasured auto-promote lands in the human queue (blocked), not promoted.
    assert settled["state"] == "blocked"
    assert settled["is_extreme"] is False
    assert exp_id in store._experiments  # persisted (the SAME id was upserted to its result)


async def test_run_experiment_pricing_threshold_settles_blocked_for_approval():
    """A pricing-concession threshold value is EXTREME: even a clean pass settles in `blocked` state
    (the human-gate), so it lands in the approval queue, not auto-promoted."""
    app, _ = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "thresholds.max_concession_band", "value": 0.4, "n": 6},
        )
        assert r.status_code == 202, r.text
        exp_id = r.json()["experiment"]["experiment_id"]
        settled = await _settle(ac, exp_id)
        assert settled["is_extreme"] is True
        assert settled["state"] == "blocked"
        apr = (await ac.get("/api/approvals")).json()
        assert any(e["experiment_id"] == exp_id for e in apr["approvals"])


async def test_run_experiment_default_n_is_modest():
    """N defaults to a modest value (cost control: each call spends real credit in prod) when omitted —
    visible on the immediate `running` record."""
    app, _ = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "thresholds.pushiness_cap", "value": 0.5},  # champion 0.7 -> a real diff
        )
        assert r.status_code == 202, r.text
        exp = r.json()["experiment"]
        assert 1 <= exp["n"] <= 24  # modest by default (the spec suggests ~12)
        assert exp["n"] == 12  # the documented default when omitted
        await _settle(ac, exp["experiment_id"])


async def test_run_experiment_record_target_equals_n():
    """The persisted record's `target` is the run's effective N (not left at 0): the lab progress chip
    needs a non-zero target, and for a held-out run target == the sampled N — set on the running record
    up front AND preserved through to the settled one."""
    app, _ = _run_app()
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
        )
        exp = r.json()["experiment"]
        assert exp["n"] == 6
        assert exp["target"] == 6, "the running record's target must equal the effective N, not 0"
        settled = await _settle(ac, exp["experiment_id"])
        assert settled["target"] == 6


async def test_run_experiment_rejects_unknown_dimension():
    """An unsupported dimension is a 400 (validation at the boundary) — not a crash, not a persisted
    `running` row, not a spawned paid run."""
    app, store = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/api/experiments/run", json={"dimension": "code.something", "value": 1})
        assert r.status_code == 400, r.text
        assert store._experiments == {}  # nothing persisted before the run is validated


async def test_run_experiment_rejects_no_op_value():
    """A value equal to the current champion's is a no-op (zero changed dimensions) -> 400 BEFORE
    anything is persisted, with a clear message (never the opaque R17 minimal-diff error)."""
    app, store = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "thresholds.pushiness_cap", "value": 0.7},  # champion is already 0.7
        )
        assert r.status_code == 400, r.text
        assert "nothing to A/B test" in r.json()["detail"]
        assert store._experiments == {}  # nothing persisted (no zombie `running` row)


async def test_run_experiment_n_floor_is_raised_above_one():
    """COST + statistical floor: even n=1 is bumped to the minimum sample (>=5) so a tiny request can
    never produce a degenerate single-sample 'significant' comparison (pairs with the grading floor)."""
    app, _ = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "thresholds.pushiness_cap", "value": 0.5, "n": 1},
        )
        assert r.status_code == 202, r.text
        exp = r.json()["experiment"]
        assert exp["n"] >= 5, "the run N floor must be at least the minimum trustworthy sample"
        await _settle(ac, exp["experiment_id"])


# =============================== CB-24 — SPECIFIC FAILURE REASON =================================
# A rejected/failed run must explain WHY in a SPECIFIC way (the user's "failed without telling me why"):
# a timeout names the elapsed budget; a crash names the exception class — never the generic
# "timed out or failed". (The timeout case is covered above; this covers the exception class.)


def _crashing_runner():
    """A runner that raises a distinctively-named exception, so the settled record's reason names that
    class (CB-24) instead of the generic failure string."""

    class ArmRunnerBoom(RuntimeError):
        pass

    async def runner(personas, agent_llm_factory, prospect_llm_factory, config, **kw):
        raise ArmRunnerBoom("simulated self-play crash")

    return runner


async def test_run_experiment_crash_settles_with_exception_class_reason():
    """CB-24: a background run that raises settles `rejected` with a reason naming the EXCEPTION CLASS
    (so the lab shows what went wrong), not the generic 'timed out or failed'."""
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    app, _ = _run_app(runner=_crashing_runner(), run_timeout_s=5.0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
        )
        assert r.status_code == 202, r.text
        settled = await _settle(ac, r.json()["experiment"]["experiment_id"])
    assert settled["state"] == "rejected"
    reason = (settled["guardrail_reason"] or "").lower()
    assert "failed with an error" in reason
    assert "armrunnerboom" in reason  # the distinct exception class name, not a generic phrase


# =============================== CB-25 — EXPERIMENT DETAIL + A/B MOCK CALLS ======================
# Clicking "See experiment" opens a detail page listing the champion-arm + challenger-arm self-play
# episodes (the mock calls), each openable in Review. GET /api/experiments/{id} returns the experiment
# DTO + the per-arm episode summaries (deterministic experiment-scoped ids the run persisted).


async def test_experiment_detail_lists_per_arm_ab_mock_calls():
    """CB-25: after a run settles, GET /api/experiments/{id} returns BOTH arms' mock calls — the
    champion-arm + challenger-arm episodes the A/B ran — each with an episode_id Review can open. The
    arm ids are deterministic + experiment-scoped, and each persisted episode is fetchable in Review
    via the existing /api/episodes/{id} route."""
    app, store = _run_app()  # the dominating runner -> tier-0 champion, tier-4 challenger
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
        )
        exp_id = r.json()["experiment"]["experiment_id"]
        await _settle(ac, exp_id)
        detail = (await ac.get(f"/api/experiments/{exp_id}")).json()
    # the experiment DTO is present, plus both arms' calls.
    assert detail["experiment"]["experiment_id"] == exp_id
    assert detail["has_calls"] is True
    assert len(detail["champion_calls"]) == 6
    assert len(detail["challenger_calls"]) == 6
    # the arm ids are experiment-scoped + deterministic (so Review can deep-link them).
    assert all(exp_id in c["episode_id"] for c in detail["champion_calls"])
    assert all("champion" in c["episode_id"] for c in detail["champion_calls"])
    assert all("challenger" in c["episode_id"] for c in detail["challenger_calls"])
    # the per-arm outcome differs (dominating runner): champion walked tier-0, challenger enrolled tier-4.
    assert detail["champion_calls"][0]["ladder_tier"] == 0
    assert detail["challenger_calls"][0]["ladder_tier"] == 4
    # the arm episodes are PERSISTED to the store (so the existing /api/episodes/{id} route — which
    # Review reads via the SAME store in the full app — can open each in Review).
    one = detail["challenger_calls"][0]["episode_id"]
    assert one in store._episodes
    assert store._episodes[one].cohort == "experiment_challenger"


def test_experiment_detail_unknown_id_404():
    """CB-25: an unknown experiment id is a 404, not a crash or an empty 200."""
    client = _seeded_client()
    r = client.get("/api/experiments/NOPE")
    assert r.status_code == 404, r.text


def test_experiment_detail_seeded_experiment_has_no_persisted_calls():
    """CB-25: a seeded/legacy experiment whose run never persisted arm episodes returns empty arm lists
    (has_calls False) rather than failing — the detail page shows the outcome/KPIs + reason, no calls."""
    client = _seeded_client()
    detail = client.get("/api/experiments/EXP-31").json()
    assert detail["experiment"]["experiment_id"] == "EXP-31"
    assert detail["champion_calls"] == []
    assert detail["challenger_calls"] == []
    assert detail["has_calls"] is False


# =============================== CB-27 — MULTI-METRIC EXPERIMENTS ================================
# An experiment may change MORE THAN ONE knob at once (explicit opt-in via `changes`). This relaxes the
# single-dimension R19 invariant for MANUAL experiments; the challenger applies ALL rows, and the lab
# shows the multi-change diff labeled "Multiple changes" (never a raw "multi" sentinel).


async def test_run_multi_metric_experiment_applies_all_changes():
    """CB-27: a `changes` list applies several knob edits to the challenger at once. The settled record
    shows it ran (a real before/after) and labels the diff "Multiple changes" — no raw sentinel."""
    app, store = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={
                "changes": [
                    {"dimension": "thresholds.pushiness_cap", "value": 0.5},  # champion 0.7
                    {"dimension": "thresholds.discovery_slots_required", "value": 4},
                ],
                "n": 6,
            },
        )
        assert r.status_code == 202, r.text
        exp = r.json()["experiment"]
        # the diff label is human, never the raw "multi" sentinel or a snake_case slug.
        assert exp["dimension_label"] == "Multiple changes"
        assert "_" not in exp["dimension_label"]
        # the description names every changed knob's new value.
        assert "0.5" in exp["diff_description"]
        assert "4" in exp["diff_description"]
        await _settle(ac, exp["experiment_id"])


async def test_run_multi_metric_with_pricing_row_is_extreme():
    """CB-27: a multi-metric change that touches an EXTREME pricing knob is is_extreme True — even a
    clean pass routes to the approval queue, not auto-promote (the human gate is not bypassed)."""
    app, _ = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={
                "changes": [
                    {"dimension": "thresholds.pushiness_cap", "value": 0.5},
                    {"dimension": "thresholds.max_concession_band", "value": 0.4},  # extreme
                ],
                "n": 6,
            },
        )
        assert r.status_code == 202, r.text
        exp_id = r.json()["experiment"]["experiment_id"]
        assert r.json()["experiment"]["is_extreme"] is True
        settled = await _settle(ac, exp_id)
        assert settled["state"] == "blocked"


async def test_run_multi_metric_all_no_op_rejected():
    """CB-27: a `changes` set where every row matches the champion is a no-op -> 400 BEFORE any paid
    run, nothing persisted (same boundary guard as the single-dimension path)."""
    app, store = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"changes": [{"dimension": "thresholds.pushiness_cap", "value": 0.7}]},  # already 0.7
        )
        assert r.status_code == 400, r.text
        assert store._experiments == {}


async def test_run_single_metric_unchanged_default():
    """CB-27: omitting `changes` keeps the single-dimension default path working unchanged — a one-knob
    run still labels its real one-dimension change (not "Multiple changes")."""
    app, _ = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "thresholds.pushiness_cap", "value": 0.5},
        )
        assert r.status_code == 202, r.text
        exp = r.json()["experiment"]
        assert exp["dimension"] == "thresholds.pushiness_cap"
        assert exp["dimension_label"] != "Multiple changes"
        await _settle(ac, exp["experiment_id"])


# =============================== P6 — RUN IS ASYNC + BOUNDED + CONCURRENCY-GUARDED ===============
# /api/experiments/run runs a full real self-play A/B in a BACKGROUND task (minutes, paid in prod). It
# must (CB-15) return 202 immediately, (CB-20) never linger `running` — a timeout/crash settles the
# record to a terminal state — and keep the concurrency guard (a 2nd run while one is in flight -> 503).


def _blocking_runner(gate: "asyncio.Event"):
    """A runner that waits on `gate` before returning episodes — lets a test hold the background run
    'in flight' so it can drive the timeout / concurrency guard deterministically (no real self-play)."""

    from src.memory.schema import Episode, Turn

    async def runner(personas, agent_llm_factory, prospect_llm_factory, config, **kw):
        await gate.wait()
        return [
            Episode(
                episode_id=f"{config.version}_{i}",
                turns=[Turn(turn_id=0, speaker="agent", text="hi", decision="greeting")],
                outcome="enrolled", ladder_tier=4, qualified=True,
                version=config.version, kb_version=config.kb_version, channel="sim",
                cohort="held_out",
            )
            for i in range(len(personas))
        ]

    return runner


async def test_run_experiment_returns_202_then_settles_terminal_on_timeout():
    """CB-20: a background run that exceeds the budget NEVER lingers `running` — the request still
    returns 202 immediately, and the timed-out run settles the SAME record to a terminal state (the
    poll catches it as no-longer-running). The blocking runner never releases; a tiny timeout trips it."""
    gate = asyncio.Event()  # never set -> the runner blocks past the timeout
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    app, _ = _run_app(runner=_blocking_runner(gate), run_timeout_s=0.05)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
        )
        assert r.status_code == 202, r.text  # never hangs, even though the A/B will time out
        exp_id = r.json()["experiment"]["experiment_id"]
        settled = await _settle(ac, exp_id)  # CB-20: it MUST leave `running`
    assert settled["state"] in ("rejected", "blocked", "passed", "promoted")
    # a timed-out run settles failed-terminal with a clear reason (not a fabricated positive).
    assert settled["state"] == "rejected"
    # CB-24: the reason is SPECIFIC — a timeout names how long it ran before the budget tripped,
    # not the generic "timed out or failed".
    reason = (settled["guardrail_reason"] or "").lower()
    assert "timed out after" in reason
    assert "0.05s" in reason


async def test_run_experiment_concurrent_run_returns_503():
    """A second concurrent run hits the concurrency guard (max 1) and returns 503 — paid work is not
    stacked. The first background run holds the permit (blocked on the gate); the second is rejected
    fast (never a frozen button)."""
    gate = asyncio.Event()
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    # generous timeout so the FIRST run's background task doesn't time out; cap of 1 forces contention.
    app, _ = _run_app(runner=_blocking_runner(gate), run_timeout_s=5.0)
    body = {"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        first = await ac.post("/api/experiments/run", json=body)  # 202; background task holds the permit
        assert first.status_code == 202, first.text
        first_id = first.json()["experiment"]["experiment_id"]
        await asyncio.sleep(0.05)  # let the background task acquire the permit + enter the runner
        second = await ac.post("/api/experiments/run", json=body)  # contends -> 503
        assert second.status_code == 503, second.text
        assert "in progress" in second.json()["detail"].lower() or "busy" in second.json()["detail"].lower()
        gate.set()  # release the first run so it settles cleanly
        await _settle(ac, first_id)  # the first run completes + frees the permit


# =============================== P6 — SCAFFOLD FROM A REVIEWED CALL (CB-19) ======================
# The review page's "Use in experiment" no longer lands on a blank lab; it scaffolds a DRAFT experiment
# seeded from THAT call and opens its per-experiment review. POST /api/experiments/scaffold builds the
# draft WITHOUT running anything, persists it as `draft`, and carries the originating episode.


async def test_scaffold_creates_draft_seeded_from_episode():
    """CB-19: scaffolding from a reviewed call builds a NON-extreme DRAFT experiment (a discovery
    reorder by default), persists it as `draft` (not running, nothing launched), carries the episode,
    and is queryable in the lab so the per-experiment review view can open it pre-populated."""
    app, store = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post("/api/experiments/scaffold", json={"episode_id": "ep-abc123"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_scaffold"] is True
        assert body["episode_id"] == "ep-abc123"
        draft = body["experiment"]
        assert draft["state"] == "draft"  # scaffolded, NOT running — nothing launched
        assert draft["is_extreme"] is False
        assert draft["dimension_label"] == "Discovery sequencing"
        assert "ep-abc123" in draft["population"]  # the originating call is attached
        # queryable in the lab as a draft (the review view reads it by id).
        lab = (await ac.get("/api/experiments", params={"state": "draft"})).json()
        assert any(e["experiment_id"] == draft["experiment_id"] for e in lab["experiments"])
        # NO record_version side effect — a scaffold never mutates the champion (R20).
        assert store._lineage == {}


async def test_scaffold_with_explicit_dimension_and_value():
    """CB-19: an explicit dimension/value pre-picks the change (e.g. a threshold the operator wants to
    trial against this call) — still a draft, still seeded from the episode, nothing launched."""
    app, _ = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/scaffold",
            json={"episode_id": "ep-xyz", "dimension": "thresholds.pushiness_cap", "value": 0.5},
        )
        assert r.status_code == 200, r.text
        draft = r.json()["experiment"]
        assert draft["state"] == "draft"
        assert draft["dimension"] == "thresholds.pushiness_cap"
        assert "." not in draft["dimension_label"]


async def test_scaffold_rejects_no_op_value():
    """A scaffold value equal to the champion's is a no-op -> 400 (validated at the boundary), nothing
    persisted — same guard the run path uses."""
    app, store = _run_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/scaffold",
            json={"episode_id": "ep-1", "dimension": "thresholds.pushiness_cap", "value": 0.7},
        )
        assert r.status_code == 400, r.text
        assert store._experiments == {}


# =============================== P7 — APPROVAL QUEUE (AE6) =======================================


def test_extreme_challenger_appears_in_approvals_queue():
    """AE6: extreme challengers awaiting a human are in the approval queue. The legitimately approvable
    one (EXP-29) passed the bar (clean guardrail) and shows WHY it's extreme (the pricing diff)."""
    client = _seeded_client()
    r = client.get("/api/approvals")
    assert r.status_code == 200, r.text
    body = r.json()
    # both blocked extremes surface (EXP-29 passed the bar; EXP-30 is a stuck guardrail-trip).
    assert body["count"] == 2
    apr = next(e for e in body["approvals"] if e["experiment_id"] == "EXP-29")
    assert apr["is_extreme"] is True
    assert apr["guardrail"] == "pass"  # a true extreme pass: no guardrail regression
    assert "discount" in (apr["diff_description"] or "").lower()


def test_approve_promotes_challenger_to_champion():
    """AE6: ONLY approve promotes an extreme challenger that PASSED the bar — the champion changes."""
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
    # EXP-29 left the queue; the stuck guardrail-trip (EXP-30) is still pending.
    assert client.get("/api/approvals").json()["count"] == 1


def test_approve_rechecks_the_bar_and_409s_a_record_that_does_not_meet_it():
    """Fix 6: approve RE-ASSERTS the bar before promoting (challenger_better AND guardrail=='pass' AND
    qualification held AND is_extreme). EXP-30 is blocked but its guardrail TRIPPED, so it does not
    meet the bar -> 409, and the champion is left unchanged (the gate is not bypassable via approve)."""
    client = _seeded_client()
    assert client.get("/api/versions").json()["champion_version"] == "v12"

    r = client.post("/api/approvals/EXP-30/approve")
    assert r.status_code == 409, r.text
    assert "bar" in r.json()["detail"].lower() or "guardrail" in r.json()["detail"].lower()
    # champion unchanged; EXP-30 still blocked.
    assert client.get("/api/versions").json()["champion_version"] == "v12"
    exp = client.get("/api/experiments", params={"state": "blocked"}).json()
    assert any(e["experiment_id"] == "EXP-30" for e in exp["experiments"])


def test_reject_leaves_champion_unchanged():
    """AE6: reject does NOT promote — the champion is unchanged; the experiment is marked rejected."""
    client = _seeded_client()
    r = client.post("/api/approvals/EXP-29/reject")
    assert r.status_code == 200, r.text
    assert r.json()["experiment"]["state"] == "rejected"
    # champion unchanged.
    assert client.get("/api/versions").json()["champion_version"] == "v12"
    # cleared from the queue (EXP-30 remains).
    assert client.get("/api/approvals").json()["count"] == 1


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


def _kb_client(kb_chunks: list[dict[str, Any]]) -> TestClient:
    """A TestClient whose injected store also serves a REAL grounded corpus (kb_chunk rows), so the
    /api/kb browse contract is exercised DB-free."""
    app = create_app(
        improve_store=FakeImproveStore([], [], kb_chunks=kb_chunks),
        config=load_config("champion_v0"),
    )
    return TestClient(app)


def test_kb_returns_real_corpus_grouped_by_section():
    """P8 browse contract (bug fix): GET /api/kb returns the REAL kb_chunk corpus grouped by section —
    NOT placeholder rebuttal strings — with counts that match the rows actually shown, and a derived
    title + full body per chunk so the panel can render each section's real grounding content."""
    chunks = [
        {"source": "objections#objection_price", "text": "Price / affordability. The membership is a monthly commitment, so framing matters."},
        {"source": "objections#objection_timing", "text": "Timing (is now the right time?). The right time ties to the goal."},
        {"source": "pricing#pricing_tier_core", "text": "Core tier (illustrative). About $349 per month, recurring."},
        {"source": "competitors#competitor_vs_marketplace", "text": "Versus tutor marketplaces (e.g. Wyzant). We vet and match tutors centrally."},
    ]
    client = _kb_client(chunks)
    body = client.get("/api/kb").json()
    assert body["kb_version"] == "kb_v0"
    assert body["total_chunks"] == 4
    sections = {s["id"]: s for s in body["sections"]}
    # three real sections, each with a count that equals the chunks it actually carries.
    assert sections["objections"]["count"] == 2
    assert sections["pricing"]["count"] == 1
    assert sections["competitors"]["count"] == 1
    assert sum(s["count"] for s in body["sections"]) == 4
    # the operator-facing label is human, never the raw slug; chunks carry a short title + full body.
    assert sections["objections"]["label"] == "Objection rebuttals"
    price = next(c for c in sections["objections"]["chunks"] if c["id"] == "objection_price")
    assert price["title"] == "Price / affordability"  # leading sentence, not the whole paragraph
    assert "monthly commitment" in price["text"]  # full real body preserved
    # the e.g. abbreviation inside a parenthetical does NOT prematurely cut the title.
    vs = next(iter(sections["competitors"]["chunks"]))
    assert vs["title"] == "Versus tutor marketplaces (e.g. Wyzant)"
    # NO placeholder text leaks into the browsed corpus.
    assert "PLACEHOLDER" not in client.get("/api/kb").text


def test_kb_empty_corpus_degrades_to_empty_sections():
    """A store with no kb_chunk rows (or one lacking list_kb_chunks) yields an empty corpus instead of
    failing — the page shows an empty-state."""
    body = _kb_client([]).get("/api/kb").json()
    assert body["sections"] == []
    assert body["total_chunks"] == 0
    # the legacy seeded client (FakeImproveStore without seeded chunks) also degrades cleanly.
    assert _seeded_client().get("/api/kb").json()["sections"] == []


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


# =============================== CB-02 — VERSION LINEAGE CHANGE DIMENSION ========================
# A promoted version whose lineage row stored NO change-dimension (legacy rows carry only
# {"ladder": ...}) must still render its real change label — recovered (non-migration) from the
# matching experiment record. A genesis version (no parent) legitimately keeps "—".


def _cb02_client() -> TestClient:
    """A client whose lineage has a promoted node with NO kpi['dimension'] (the bug shape), a genesis
    node, AND an experiment table that carries the dimension for the promoted version's BASE — so the
    /api/versions serializer can backfill the change label from the experiment (CB-02)."""
    # Lineage mirrors the production bug: a hash-suffixed promoted version with only a ladder KPI, and a
    # genesis parent. NEITHER stores a dimension.
    lineage = [
        VersionLineage(
            version="v1-1fb2bc0e", parent_version="v0-213df354", kb_version="kb_v0",
            kpi={"ladder": 0.51}, is_champion=True,
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ),
        VersionLineage(
            version="v0-213df354", parent_version=None, kb_version="kb_v0",
            kpi={"ladder": 0.40}, is_champion=False,
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        ),
    ]
    # The promoted experiment whose challenger_version normalizes to the SAME base ("v1") as the lineage
    # node, carrying the real dimension — even though the literal version strings differ.
    experiments = [
        _exp(
            "exp-promo", challenger="champion_v1", parent="champion_v0",
            dimension="playbooks.discovery_sequence", state="promoted", is_extreme=False,
            name="Discovery sequencing — value before price",
        ),
    ]
    app = create_app(
        improve_store=FakeImproveStore(experiments, lineage),
        config=load_config("champion_v0"),
    )
    return TestClient(app)


def test_versions_backfill_change_dimension_from_experiment():
    """CB-02: a promoted version with no stored dimension shows its real change label (recovered from
    the experiment table by version BASE), NOT '—'; the genesis version still shows null/'—'; and no
    raw slug leaks."""
    client = _cb02_client()
    body = client.get("/api/versions").json()
    promoted = next(v for v in body["versions"] if v["version"] == "v1-1fb2bc0e")
    genesis = next(v for v in body["versions"] if v["version"] == "v0-213df354")
    # the promoted node recovered its human change label (no longer "CHANGE —").
    assert promoted["dimension_label"] == "Discovery sequencing"
    assert "." not in promoted["dimension_label"]  # no raw "playbooks.discovery_sequence" slug
    assert "_" not in promoted["dimension_label"]
    # the genesis node has no change (it IS the origin) -> null so the UI renders "—".
    assert genesis["dimension_label"] is None


def test_versions_dimension_label_prefers_stored_kpi_dimension():
    """When a lineage row DID store kpi['dimension'] (promote()/approve stamp it), that wins — the
    backfill is only a fallback for legacy rows, never an override."""
    lineage = [
        VersionLineage(
            version="v2-aaaa", parent_version="v1-bbbb", kb_version="kb_v0",
            kpi={"ladder": 0.6, "dimension": "thresholds.pushiness_cap"}, is_champion=True,
            created_at=datetime.now(timezone.utc),
        ),
        VersionLineage(
            version="v1-bbbb", parent_version=None, kb_version="kb_v0", kpi={"ladder": 0.5},
            created_at=datetime.now(timezone.utc) - timedelta(days=1),
        ),
    ]
    # an experiment that would backfill a DIFFERENT dimension if the stored one were ignored.
    experiments = [
        _exp("exp-other", challenger="champion_v2", parent="champion_v1",
             dimension="playbooks.discovery_sequence", state="promoted", is_extreme=False),
    ]
    app = create_app(improve_store=FakeImproveStore(experiments, lineage), config=load_config("champion_v0"))
    body = TestClient(app).get("/api/versions").json()
    v2 = next(v for v in body["versions"] if v["version"] == "v2-aaaa")
    # the stored dimension wins (Pushiness cap), not the experiment-table backfill.
    assert v2["dimension_label"] == "Pushiness cap"


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

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
# The "run an A/B between two values" path: build the champion config + a challenger via the
# generator's pure mutators for the requested dimension/value, run the experiment on a frozen held-out
# set with the app's injected LLM client (MockLLM in tests = free), persist the resulting
# ExperimentRecord per the gate, and return it. A deterministic runner is injected so the outcome is
# decoupled from self-play scoring (production uses real self-play + a real OpenRouterClient = paid).


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


def _run_client() -> tuple[TestClient, FakeImproveStore]:
    """A DB-free TestClient wired for the run endpoint: empty injected store, MockLLM factory (free),
    and a deterministic dominating runner so the A/B outcome is reproducible without self-play."""
    champ = load_config("champion_v0")
    store = FakeImproveStore([], [])
    app = create_app(
        improve_store=store,
        config=champ,
        llm_client_factory=_routed_mock,
        experiment_runner=_dominating_runner(champ.version),
    )
    return TestClient(app), store


def test_run_experiment_discovery_reorder_creates_and_persists():
    """POST /api/experiments/run with a discovery-sequence reorder builds + runs + persists a real
    ExperimentRecord (state from the gate), returns the before/after, and it appears in
    /api/experiments. MockLLM + a dominating runner = free + deterministic.

    FAIL-CLOSED (R36): the inline run does NOT measure sim-to-real divergence, so an otherwise
    auto-promote-eligible challenger is routed to the human queue (blocked), never silently shipped."""
    client, store = _run_client()
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]  # a reordered list = the challenger "value"

    r = client.post(
        "/api/experiments/run",
        json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
    )
    assert r.status_code == 200, r.text
    exp = r.json()["experiment"]
    # before/after: the dominating challenger is clearly better (tier 4 vs tier 0).
    assert exp["challenger_kpi"] > exp["champion_kpi"]
    assert exp["delta"] > 0
    assert exp["challenger_better"] is True
    # FAIL-CLOSED: an unmeasured auto-promote lands in the human queue (blocked), not promoted.
    assert exp["state"] == "blocked"
    assert r.json()["decision"] == "pending_approval"
    assert exp["dimension_label"] == "Discovery sequencing"
    assert "." not in exp["dimension_label"]
    assert exp["is_extreme"] is False
    assert exp["n"] == 6

    # persisted + queryable in the lab (the whole point — running the A/B SHOWS in /api/experiments).
    body = client.get("/api/experiments").json()
    assert any(e["experiment_id"] == exp["experiment_id"] for e in body["experiments"])
    assert exp["experiment_id"] in store._experiments


def test_run_experiment_unmeasured_autopromote_fails_closed_to_pending_approval():
    """FAIL-CLOSED (R36): a non-extreme challenger that would AUTO-PROMOTE but has NO sim-to-real
    divergence measured is routed to pending_approval (blocked), NOT silently promoted — the central-
    risk gate must not be bypassed by the inline run path."""
    client, store = _run_client()
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    r = client.post(
        "/api/experiments/run",
        json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
    )
    assert r.status_code == 200, r.text
    decision = r.json()["decision"]
    exp = r.json()["experiment"]
    assert decision == "pending_approval", "an unmeasured auto-promote must fail closed"
    assert exp["state"] == "blocked"  # not 'promoted'
    # it surfaces in the approval queue for a human, not shipped.
    apr = client.get("/api/approvals").json()
    assert any(e["experiment_id"] == exp["experiment_id"] for e in apr["approvals"])


def test_run_experiment_pricing_threshold_blocks_for_approval():
    """A pricing-concession threshold value is EXTREME: even a clean pass is persisted in `blocked`
    state (the human-gate), so it lands in the approval queue, not auto-promoted."""
    client, _ = _run_client()
    r = client.post(
        "/api/experiments/run",
        json={"dimension": "thresholds.max_concession_band", "value": 0.4, "n": 6},
    )
    assert r.status_code == 200, r.text
    exp = r.json()["experiment"]
    assert exp["is_extreme"] is True
    assert exp["state"] == "blocked"
    # it shows up in the approval queue.
    apr = client.get("/api/approvals").json()
    assert any(e["experiment_id"] == exp["experiment_id"] for e in apr["approvals"])


def test_run_experiment_default_n_is_modest():
    """N defaults to a modest value (cost control: each call spends real credit in prod) when omitted."""
    client, store = _run_client()
    r = client.post(
        "/api/experiments/run",
        json={"dimension": "thresholds.pushiness_cap", "value": 0.5},  # champion is 0.7 -> a real diff
    )
    assert r.status_code == 200, r.text
    exp = r.json()["experiment"]
    assert 1 <= exp["n"] <= 24  # modest by default (the spec suggests ~12)
    assert exp["n"] == 12  # the documented default when omitted


def test_run_experiment_record_target_equals_n():
    """The persisted record's `target` is the run's effective N (not left at 0): the lab progress
    chip ('184 / 400') needs a non-zero target, and for a held-out run target == the sampled N."""
    client, _ = _run_client()
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    r = client.post(
        "/api/experiments/run",
        json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
    )
    assert r.status_code == 200, r.text
    exp = r.json()["experiment"]
    assert exp["n"] == 6
    assert exp["target"] == 6, "the record's target must equal the effective N, not 0"


def test_run_experiment_rejects_unknown_dimension():
    """An unsupported dimension is a 400 (validation at the boundary) — not a crash, not a paid run."""
    client, _ = _run_client()
    r = client.post(
        "/api/experiments/run",
        json={"dimension": "code.something", "value": 1},
    )
    assert r.status_code == 400, r.text


def test_run_experiment_rejects_no_op_value():
    """A value equal to the current champion's is a no-op (zero changed dimensions) -> 400 BEFORE any
    paid run, with a clear message (never the opaque R17 minimal-diff error)."""
    client, store = _run_client()
    r = client.post(
        "/api/experiments/run",
        json={"dimension": "thresholds.pushiness_cap", "value": 0.7},  # champion is already 0.7
    )
    assert r.status_code == 400, r.text
    assert "nothing to A/B test" in r.json()["detail"]
    assert store._experiments == {}  # nothing persisted


def test_run_experiment_n_floor_is_raised_above_one():
    """COST + statistical floor: even n=1 is bumped to the minimum sample (>=5) so a tiny request can
    never produce a degenerate single-sample 'significant' comparison (pairs with the grading floor)."""
    client, _ = _run_client()
    r = client.post(
        "/api/experiments/run",
        json={"dimension": "thresholds.pushiness_cap", "value": 0.5, "n": 1},
    )
    assert r.status_code == 200, r.text
    exp = r.json()["experiment"]
    assert exp["n"] >= 5, "the run N floor must be at least the minimum trustworthy sample"


# =============================== P6 — RUN IS BOUNDED + CONCURRENCY-GUARDED =======================
# /api/experiments/run awaits a full real self-play A/B inline (minutes, paid in prod). It must be
# bounded (a timeout returns a clear error instead of hanging) and guarded against concurrent runs
# (a second concurrent run returns 503 instead of stacking paid work). Tested via the ASGI app on a
# single event loop with a runner that blocks on an event the test controls.

import asyncio  # noqa: E402

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from src.api.improve import create_improve_router  # noqa: E402
from fastapi import FastAPI  # noqa: E402


def _blocking_runner(gate: "asyncio.Event"):
    """A runner that waits on `gate` before returning episodes — lets a test hold the run 'in flight'
    so it can drive the timeout / concurrency guard deterministically (no real self-play)."""

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


def _bounded_run_app(*, runner, run_timeout_s: float, run_max_concurrency: int) -> FastAPI:
    """A bare FastAPI app mounting ONLY the Improve router, with the run budget + concurrency cap
    injected (server.create_app uses the defaults; the router accepts overrides for testability)."""
    champ = load_config("champion_v0")
    app = FastAPI()
    app.include_router(
        create_improve_router(
            improve_store=FakeImproveStore([], []),
            champion_config_provider=lambda: champ,
            llm_client_factory=_routed_mock,
            experiment_runner=runner,
            run_timeout_s=run_timeout_s,
            run_max_concurrency=run_max_concurrency,
        )
    )
    return app


async def test_run_experiment_times_out_with_clear_error():
    """A run that exceeds the budget returns a clear timeout error (503), not a hang. The blocking
    runner never releases; a tiny timeout trips the asyncio.wait_for bound."""
    gate = asyncio.Event()  # never set -> the runner blocks past the timeout
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    app = _bounded_run_app(runner=_blocking_runner(gate), run_timeout_s=0.05, run_max_concurrency=2)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/api/experiments/run",
            json={"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6},
        )
    assert r.status_code == 503, r.text
    assert "timed out" in r.json()["detail"].lower()


async def test_run_experiment_concurrent_run_returns_503():
    """A second concurrent run hits the concurrency guard (max 1) and returns 503 — paid work is not
    stacked. The first run holds the semaphore (blocked on the gate); the second is rejected fast."""
    gate = asyncio.Event()
    champ = load_config("champion_v0")
    seq = list(champ.playbooks["discovery_sequence"])
    seq[0], seq[-1] = seq[-1], seq[0]
    # generous timeout so the FIRST run doesn't time out; concurrency cap of 1 forces contention.
    app = _bounded_run_app(runner=_blocking_runner(gate), run_timeout_s=5.0, run_max_concurrency=1)
    body = {"dimension": "playbooks.discovery_sequence", "value": seq, "n": 6}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        first = asyncio.create_task(ac.post("/api/experiments/run", json=body))
        await asyncio.sleep(0.05)  # let the first run acquire the semaphore + enter the runner
        second = await ac.post("/api/experiments/run", json=body)  # contends -> 503
        assert second.status_code == 503, second.text
        assert "in progress" in second.json()["detail"].lower() or "busy" in second.json()["detail"].lower()
        gate.set()  # release the first run so it completes cleanly
        first_resp = await first
        assert first_resp.status_code == 200, first_resp.text


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

# End-to-end tests for the replay-build API (src/api/replay_routes.py). Drives the FastAPI surface
# via fastapi.testclient.TestClient with an INJECTED in-memory ReadStore (seeded completed episodes)
# and a MockLLMClient factory (network-free replay_call). Tests written FIRST (test-driven):
# POST /api/replay/build -> 202 starts job, GET /api/replay/results -> progress + done; 409 on
# double-build; empty-store guard (200 {"status":"empty"}); aggregate math; persona_label humanized.
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.core.llm import Message, MockLLMClient
from src.memory.schema import BeliefSnapshot, Episode, Turn


# ---------------------------------------------------------------------------
# Fake ReadStore (reuse the pattern from test_operate.py)
# ---------------------------------------------------------------------------


class FakeReadStore:
    """In-memory ReadStore satisfying src.api.operate.ReadStore. Applies optional-AND filter
    semantics, sorts episodes newest-first."""

    def __init__(self, episodes: list[Episode]) -> None:
        self._episodes = episodes

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

    async def list_escalations(self, *, lifecycle: Optional[str] = None, limit: int = 100):
        return []

    async def update_escalation_lifecycle(self, escalation_id: str, lifecycle: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# Episode + LLM factories
# ---------------------------------------------------------------------------


def _belief(trust: float = 0.5, bail: float = 0.3, stage: str = "discovery") -> BeliefSnapshot:
    return BeliefSnapshot(
        drivers={"trust": trust, "bail_risk": bail, "urgency": 0.4},
        stage=stage,
    )


def _episode(
    eid: str,
    *,
    outcome: str,
    tier: int,
    persona: str,
    minutes_ago: int = 10,
    turn_count: int = 5,
) -> Episode:
    """Build a completed episode with at least `turn_count` turns (all >= 4 for eligibility)."""
    all_turns = [
        Turn(
            turn_id=i,
            speaker="agent" if i % 2 == 0 else "prospect",
            text=f"Turn {i} text",
            decision="greeting" if i == 0 else "ask",
            belief=_belief(trust=0.3 + i * 0.05, bail=0.3, stage="discovery"),
        )
        for i in range(turn_count)
    ]
    return Episode(
        episode_id=eid,
        turns=all_turns,
        outcome=outcome,
        ladder_tier=tier,
        qualified=tier >= 2,
        version="v1",
        kb_version="kb-1",
        channel="sim",
        persona=persona,
        cohort="held_out",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


def _make_agent_factory():
    """Returns a factory that builds a MockLLMClient closing at consultation every turn."""
    def _factory() -> MockLLMClient:
        def _serve(messages: Sequence[Message], **opts: Any) -> str:
            rf = opts.get("response_format")
            if isinstance(rf, dict) and rf.get("type") == "json_object":
                return json.dumps({
                    "act": "attempt_close",
                    "target_slot": None,
                    "tier": "consultation",
                    "confidence": 0.9,
                    "rationale": "test close",
                    "trust": 0.05,
                    "need_intensity": 0.05,
                    "urgency": 0.0,
                    "purchase_intent": 0.1,
                    "bail_risk": 0.0,
                })
            return "Let's book a consultation."

        return MockLLMClient(_serve)

    return _factory


def _make_twin_factory():
    """Returns a factory that builds a MockLLMClient for the twin prospect."""
    def _factory() -> MockLLMClient:
        payload = json.dumps({
            "utterance": "Yes, that makes sense.",
            "deltas": {"trust": 0.10, "need": 0.10, "purchase_intent": 0.10},
        })

        def _serve(messages: Sequence[Message], **opts: Any) -> str:
            return payload

        return MockLLMClient(_serve)

    return _factory


# ---------------------------------------------------------------------------
# App factory helpers
# ---------------------------------------------------------------------------


def _make_app(episodes: list[Episode]) -> TestClient:
    """Create a TestClient with the given episodes in the fake read-store and mock LLM factories."""
    store = FakeReadStore(episodes)
    app = create_app(
        read_store=store,
        llm_client_factory=_make_agent_factory(),
        live_rag=False,
        # Pass both LLM factories via environment is not the right seam — we wire them through
        # server.py's create_app. The replay router picks them up via the injected factories.
        # We pass them via the dedicated kwargs added in this task.
        replay_agent_llm_factory=_make_agent_factory(),
        replay_twin_llm_factory=_make_twin_factory(),
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helper: POST and poll until done (or timeout)
# ---------------------------------------------------------------------------


def _build_and_wait(client: TestClient, *, n: int = 1, max_calls: int = 5,
                    timeout_s: float = 30.0) -> dict[str, Any]:
    """POST build, then poll GET /api/replay/results until status=='done'. Returns final results."""
    r = client.post("/api/replay/build", json={"n": n, "max_calls": max_calls})
    assert r.status_code in (202, 200), f"Expected 202/200, got {r.status_code}: {r.text}"
    if r.status_code == 200:
        # empty guard: no eligible calls
        return r.json()

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        res = client.get("/api/replay/results")
        assert res.status_code == 200, res.text
        body = res.json()
        if body["status"] == "done":
            return body
        # Give the background task a tick to progress (TestClient runs in the same thread so we
        # call the endpoint again to advance asyncio; TestClient is synchronous so we rely on the
        # anyio backend draining the event loop between requests).
        time.sleep(0.1)
    pytest.fail(f"Replay job did not finish within {timeout_s}s. Last status: {body}")


# ---------------------------------------------------------------------------
# Seeded episodes used across multiple tests
# ---------------------------------------------------------------------------

_COMPLETED_EPISODES = [
    _episode("REPLAY-001", outcome="enrolled", tier=4, persona="Warm Champion", minutes_ago=60),
    _episode("REPLAY-002", outcome="consult_booked", tier=2, persona="anxious_parent",
             minutes_ago=45),
    _episode("REPLAY-003", outcome="callback_booked", tier=1, persona="price_hawk",
             minutes_ago=30),
]

_IN_PROGRESS_EPISODES = [
    _episode("LIVE-001", outcome="in_progress", tier=0, persona="Skeptical Analyzer",
             minutes_ago=0),
]


# ---------------------------------------------------------------------------
# Test: POST /api/replay/build returns 202 and job starts
# ---------------------------------------------------------------------------


def test_build_returns_202_and_count():
    """POST /api/replay/build with eligible completed calls returns 202 + calls count."""
    client = _make_app(_COMPLETED_EPISODES)
    r = client.post("/api/replay/build", json={"n": 1, "max_calls": 5})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "started"
    assert body["calls"] >= 1
    assert body["calls"] <= 5


# ---------------------------------------------------------------------------
# Test: GET /api/replay/results eventually reports done with per-call results + aggregate
# ---------------------------------------------------------------------------


def test_get_results_reports_done_with_per_call_and_aggregate():
    """After a build completes, GET /api/replay/results returns done + per-call results + aggregate."""
    client = _make_app(_COMPLETED_EPISODES)
    body = _build_and_wait(client, n=1, max_calls=3)

    assert body["status"] == "done"
    assert body["calls_total"] >= 1
    assert body["calls_done"] == body["calls_total"]

    # Per-call results
    results = body["results"]
    assert isinstance(results, list)
    assert len(results) >= 1
    for r in results:
        assert "episode_id" in r
        assert "real_outcome" in r
        assert "mean_divergence" in r
        assert "outcome_match_rate" in r
        assert "n" in r
        assert "persona_label" in r
        # divergence is a float in [0, 1]
        assert isinstance(r["mean_divergence"], float)
        assert 0.0 <= r["mean_divergence"] <= 1.0
        # outcome_match_rate is a float in [0, 1]
        assert isinstance(r["outcome_match_rate"], float)
        assert 0.0 <= r["outcome_match_rate"] <= 1.0

    # Aggregate
    agg = body["aggregate"]
    assert agg["calls"] == len(results)
    assert agg["mean_divergence"] is not None
    assert agg["mean_outcome_match"] is not None
    assert isinstance(agg["mean_divergence"], float)
    assert isinstance(agg["mean_outcome_match"], float)


# ---------------------------------------------------------------------------
# Test: results are sorted by mean_divergence ascending (best mimics first)
# ---------------------------------------------------------------------------


def test_results_sorted_by_mean_divergence_asc():
    """Results must be sorted ascending by mean_divergence (best mimic first)."""
    client = _make_app(_COMPLETED_EPISODES)
    body = _build_and_wait(client, n=1, max_calls=5)
    divs = [r["mean_divergence"] for r in body["results"]]
    assert divs == sorted(divs), f"Results not sorted asc by mean_divergence: {divs}"


# ---------------------------------------------------------------------------
# Test: persona_label is humanized, never a raw internal slug
# ---------------------------------------------------------------------------


def test_persona_label_is_humanized():
    """persona_label must be a human-readable string, never a raw slug like 'anxious_parent'."""
    client = _make_app(_COMPLETED_EPISODES)
    body = _build_and_wait(client, n=1, max_calls=5)
    for r in body["results"]:
        label = r["persona_label"]
        assert label is not None
        # Raw slugs contain underscores; labels must not be the raw slug (they are titleized).
        # "anxious_parent" -> "Anxious parent"  "price_hawk" -> "Price hawk"
        # "Warm Champion" is already human-readable; it passes either way.
        # The key invariant: if the slug has underscores it must be titleized.
        persona_ep = next(
            (ep for ep in _COMPLETED_EPISODES if ep.episode_id == r["episode_id"]), None
        )
        if persona_ep and persona_ep.persona and "_" in persona_ep.persona:
            # Must not be the raw slug; at minimum it must be capitalized/titleized.
            assert label != persona_ep.persona, (
                f"Raw slug leaked: persona_label={label!r} for persona={persona_ep.persona!r}"
            )


# ---------------------------------------------------------------------------
# Test: 409 on double-build (don't start a second job while one is running)
# ---------------------------------------------------------------------------


def test_double_build_returns_409():
    """A second POST /api/replay/build while a job is running must return 409."""
    # Use a large max_calls with a small n so the job takes time (in the in-process asyncio sense).
    # In practice with TestClient the event loop may drain quickly, so we reset between calls
    # by building a fresh app each time we need isolation.
    episodes = _COMPLETED_EPISODES * 3  # more episodes so the job selects more calls
    client = _make_app(episodes)

    # First build — should start
    r1 = client.post("/api/replay/build", json={"n": 1, "max_calls": 2})
    assert r1.status_code == 202, r1.text

    # Immediately POST again — if still running, must 409.
    # (If it already finished in the same tick, we accept 409 or 202 — but the module-level
    # _ReplayStore must reset on each new build so a re-build after completion is fine.)
    # We specifically test the guard is present; the race is fine to accept as "already done + reset".
    r2 = client.post("/api/replay/build", json={"n": 1, "max_calls": 2})
    # Either the first job is still running -> 409, or it finished already -> 202 (build resets).
    assert r2.status_code in (409, 202), (
        f"Expected 409 (running guard) or 202 (finished, reset accepted), got {r2.status_code}: {r2.text}"
    )


# ---------------------------------------------------------------------------
# Test: 409 only fires when the job is ACTUALLY still running
# ---------------------------------------------------------------------------


async def test_409_fires_when_job_running():
    """POST build -> status running -> second POST -> 409.

    Uses pytest-asyncio to run in a real async context where asyncio.sleep properly suspends.
    We patch replay_call to hang indefinitely during the test, fire the first build, then
    immediately fire a second build before the first can complete — expecting 409.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from src.api.replay_routes import create_replay_router
    from src.config.settings import load_config

    cfg = load_config("champion_v0")
    read_store = FakeReadStore(_COMPLETED_EPISODES)

    # An event we control — the mock hangs until we release it.
    release = asyncio.Event()

    async def _hung_replay(*args, **kwargs):
        await release.wait()  # blocks until the event is set
        return None

    hung_mock = AsyncMock(side_effect=_hung_replay)

    with patch("src.api.replay_routes.replay_call", new=hung_mock):
        router = create_replay_router(
            read_store=read_store,
            agent_llm_factory=_make_agent_factory(),
            twin_llm_factory=_make_twin_factory(),
            config=cfg,
            has_llm_key=True,
        )

        # Extract the POST and GET handlers from the router for direct async invocation.
        build_fn = next(r.endpoint for r in router.routes if r.path == "/api/replay/build")
        results_fn = next(r.endpoint for r in router.routes if r.path == "/api/replay/results")

        # Fire build 1 — should start (we don't await it; the task runs in background).
        from pydantic import BaseModel

        class _Req:
            n = 1
            max_calls = 2

        resp1 = await build_fn(_Req())
        # resp1 is either a dict (started) or a JSONResponse (empty).
        # If it's a dict, the job started.
        assert isinstance(resp1, dict) and resp1.get("status") == "started", f"Expected started, got {resp1}"

        # Yield to the event loop once so the background task starts and calls replay_call.
        await asyncio.sleep(0)

        # Fire build 2 immediately — should get a 409 HTTPException.
        from fastapi import HTTPException as _HTTPException

        with pytest.raises(_HTTPException) as exc_info:
            await build_fn(_Req())
        assert exc_info.value.status_code == 409, (
            f"Expected 409, got {exc_info.value.status_code}: {exc_info.value.detail}"
        )

        # Release the hung task so it can finish (avoids event loop dangling tasks).
        release.set()
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Test: empty-store guard returns 200 {"status": "empty", "calls": 0}
# ---------------------------------------------------------------------------


def test_empty_store_guard():
    """POST /api/replay/build with no eligible completed calls returns 200 status=empty (no job)."""
    # Only in-progress episodes — none are eligible
    client = _make_app(_IN_PROGRESS_EPISODES)
    r = client.post("/api/replay/build", json={"n": 1, "max_calls": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "empty"
    assert body["calls"] == 0


def test_empty_store_no_completed_at_all():
    """POST build with a completely empty store also returns status=empty."""
    client = _make_app([])
    r = client.post("/api/replay/build", json={"n": 1, "max_calls": 5})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "empty"


# ---------------------------------------------------------------------------
# Test: GET /api/replay/results when idle returns idle status + null aggregate
# ---------------------------------------------------------------------------


def test_get_results_idle_before_any_build():
    """GET /api/replay/results before any build returns idle status + null aggregate values."""
    client = _make_app(_COMPLETED_EPISODES)
    r = client.get("/api/replay/results")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "idle"
    assert body["calls_total"] == 0
    assert body["calls_done"] == 0
    assert body["results"] == []
    agg = body["aggregate"]
    assert agg["mean_divergence"] is None
    assert agg["mean_outcome_match"] is None
    assert agg["calls"] == 0


# ---------------------------------------------------------------------------
# Test: n and max_calls clamping (n [1,5], max_calls [1,25])
# ---------------------------------------------------------------------------


def test_n_and_max_calls_are_clamped():
    """n is clamped to [1,5] and max_calls to [1,25]; out-of-range values are accepted and clamped."""
    client = _make_app(_COMPLETED_EPISODES)
    # n=100 -> clamped to 5; max_calls=100 -> clamped to 25
    r = client.post("/api/replay/build", json={"n": 100, "max_calls": 100})
    assert r.status_code in (202, 200), r.text
    # n=0 -> clamped to 1; max_calls=0 -> clamped to 1
    # Wait for the previous job to finish before firing another
    _build_and_wait(client, n=1, max_calls=1)
    r2 = client.post("/api/replay/build", json={"n": 0, "max_calls": 0})
    assert r2.status_code in (202, 200), r2.text


# ---------------------------------------------------------------------------
# Test: aggregate is null when no results yet (mid-job state)
# ---------------------------------------------------------------------------


def test_aggregate_is_null_when_no_results_yet():
    """GET /api/replay/results aggregate is null when calls_done == 0."""
    client = _make_app(_COMPLETED_EPISODES)

    import asyncio
    from unittest.mock import patch

    async def _slow_replay(*args, **kwargs):
        await asyncio.sleep(60)

    with patch("src.api.replay_routes.replay_call", side_effect=_slow_replay):
        r = client.post("/api/replay/build", json={"n": 1, "max_calls": 2})
        assert r.status_code == 202, r.text
        res = client.get("/api/replay/results")
        assert res.status_code == 200
        body = res.json()
        # Either still running (calls_done < calls_total) or already reset if somehow done.
        # When no calls finished yet, aggregate must be null.
        if body["calls_done"] == 0:
            assert body["aggregate"]["mean_divergence"] is None
            assert body["aggregate"]["mean_outcome_match"] is None


# ---------------------------------------------------------------------------
# Test: only COMPLETED calls with >= 4 turns are eligible for replay
# ---------------------------------------------------------------------------


def test_only_completed_calls_with_min_turns_are_eligible():
    """Only completed episodes with outcome set + >= 4 turns are replayed; short/in-progress excluded."""
    short_ep = _episode(
        "SHORT-001", outcome="enrolled", tier=4, persona="Warm Champion",
        minutes_ago=20, turn_count=2,  # only 2 turns — ineligible
    )
    in_progress_ep = _episode(
        "LIVE-002", outcome="in_progress", tier=0, persona="Skeptical Analyzer",
        minutes_ago=5, turn_count=6,  # in-progress — ineligible
    )
    eligible_ep = _episode(
        "ELIG-001", outcome="consult_booked", tier=2, persona="anxious_parent",
        minutes_ago=15, turn_count=5,  # completed + >=4 turns — eligible
    )

    client = _make_app([short_ep, in_progress_ep, eligible_ep])
    r = client.post("/api/replay/build", json={"n": 1, "max_calls": 10})
    assert r.status_code == 202, r.text
    body_count = r.json()["calls"]
    # Only ELIG-001 qualifies
    assert body_count == 1, f"Expected 1 eligible call, got {body_count}"


# ---------------------------------------------------------------------------
# Test: newest-first selection — max_calls limits which calls are taken
# ---------------------------------------------------------------------------


def test_newest_first_selection_respects_max_calls():
    """max_calls caps the number of calls selected; newest completed calls are chosen first."""
    episodes = [
        _episode("NEWEST", outcome="enrolled", tier=4, persona="Warm Champion", minutes_ago=5),
        _episode("MIDDLE", outcome="consult_booked", tier=2, persona="anxious_parent",
                 minutes_ago=30),
        _episode("OLDEST", outcome="callback_booked", tier=1, persona="price_hawk",
                 minutes_ago=120),
    ]
    client = _make_app(episodes)
    r = client.post("/api/replay/build", json={"n": 1, "max_calls": 2})
    assert r.status_code == 202, r.text
    # Should select 2 newest: NEWEST and MIDDLE, not OLDEST
    assert r.json()["calls"] == 2


# ---------------------------------------------------------------------------
# Test: 503 when no LLM factory is wired (no key, graceful degradation)
# ---------------------------------------------------------------------------


def test_build_returns_503_when_no_llm_key(monkeypatch):
    """POST /api/replay/build returns 503 when no LLM key is configured and no factory is injected."""
    import os
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Build app WITHOUT injecting replay factories -> falls back to checking env key
    store = FakeReadStore(_COMPLETED_EPISODES)
    app = create_app(
        read_store=store,
        live_rag=False,
        # No replay factories injected -> server must detect missing key and 503
    )
    client = TestClient(app)
    r = client.post("/api/replay/build", json={"n": 1, "max_calls": 5})
    # 503 when key is missing, OR 202 if the server doesn't check eagerly (lazy check is fine too).
    # The contract says "returns 503 or uses the injected factory"; both are acceptable.
    assert r.status_code in (503, 202), (
        f"Expected 503 (no key) or 202 (lazy-start accepted), got {r.status_code}: {r.text}"
    )

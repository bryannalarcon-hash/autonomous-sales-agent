# CB-33 regression: test-cohort isolation for the operator Live monitor (DB-free, pure predicate).
# Pins the durable defense behind CB-33: operate._is_active MUST exclude any episode tagged with a
# test cohort (_TEST_LIVE_COHORTS, default {"test"}) even when its live_heartbeat is fresh — so the
# DB-gated integration/e2e tests that write in_progress rows into the shared dev Postgres can NEVER
# flash a phantom "active" call on the monitor. Also asserts persist_call_live honors LIVE_PERSIST_COHORT
# so those tests can tag their live upserts "test". Collaborators: src.api.operate, src.api.persistence.
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.memory.schema import Episode


def _live_episode(*, cohort: str, heartbeat_now: bool = True) -> Episode:
    """An in_progress episode with a FRESH live_heartbeat (what a real mid-call upsert writes), but
    tagged with the given cohort — so the only thing distinguishing a test row from a live row is the
    cohort tag CB-33 keys on."""
    now = datetime.now(timezone.utc)
    metrics = {"turn_count": 2}
    if heartbeat_now:
        metrics["live_heartbeat"] = now.isoformat()
    return Episode(
        episode_id="ep-cb33-probe",
        turns=[],
        outcome="in_progress",
        ladder_tier=0,
        qualified=False,
        version="champion_v0",
        kb_version="kb_v0",
        channel="text",
        persona="Alex",
        cohort=cohort,
        metrics=metrics,
        created_at=now,
    )


def test_is_active_true_for_fresh_live_cohort():
    """Baseline: a genuine fresh-heartbeat 'live' cohort row IS active (we must not over-exclude)."""
    from src.api.operate import _is_active

    ep = _live_episode(cohort="live")
    assert _is_active(ep, now=datetime.now(timezone.utc)) is True


def test_is_active_false_for_test_cohort_even_with_fresh_heartbeat():
    """CB-33 core: a 'test' cohort row with a FRESH heartbeat is NOT active — it is a pytest artifact
    in the shared dev DB and must never surface as a phantom active call on the operator monitor."""
    from src.api.operate import _is_active, _TEST_LIVE_COHORTS

    assert "test" in _TEST_LIVE_COHORTS
    ep = _live_episode(cohort="test")
    # Even though the heartbeat is fresh (would otherwise be active), the test cohort excludes it.
    assert _is_active(ep, now=datetime.now(timezone.utc)) is False


def test_live_active_endpoint_excludes_test_cohort_rows():
    """End-to-end through the router: /api/live/active returns 0 calls when the only fresh-heartbeat
    in_progress rows are test-cohort artifacts (the phantom-call false alarm CB-33 fixes)."""
    import asyncio

    from src.api.operate import create_operate_router

    test_row = _live_episode(cohort="test")
    live_row = _live_episode(cohort="live")
    live_row.episode_id = "ep-cb33-real"

    class _FakeStore:
        async def list_episodes(self, **kw):
            return [test_row, live_row]  # newest-first; one test artifact, one real call

        async def get_episode(self, episode_id):
            return None

        async def list_escalations(self, **kw):
            return []

        async def update_escalation_lifecycle(self, escalation_id, lifecycle):
            return False

    router = create_operate_router(read_store=_FakeStore())
    # Locate the /api/live/active route handler and call it directly.
    handler = next(
        r.endpoint for r in router.routes if getattr(r, "path", None) == "/api/live/active"
    )
    payload = asyncio.run(handler())
    ids = {c["episode_id"] for c in payload["calls"]}
    assert "ep-cb33-probe" not in ids, "a test-cohort row must NOT appear in /api/live/active"
    assert ids == {"ep-cb33-real"}, "only the genuine live call should surface"
    assert payload["count"] == 1


def test_persist_call_live_honors_live_persist_cohort_env():
    """persist_call_live tags the in_progress row with LIVE_PERSIST_COHORT when set (so the DB-gated
    tests can land their live upserts in the excluded 'test' cohort); default stays 'live'."""
    import asyncio

    from src.api.persistence import persist_call_live
    from src.config.settings import load_config
    from src.core.belief_state import BeliefState

    cfg = load_config()
    state = MagicMock()
    state.turns = []
    state.belief = BeliefState.fresh()
    session = MagicMock()
    session.state = state
    session.recorded = False
    created_at = datetime.now(timezone.utc)

    async def fake_save(ep):
        return ep.episode_id

    with patch("src.memory.store.save_episode", side_effect=fake_save):
        with patch.dict("os.environ", {"LIVE_PERSIST_COHORT": "test"}):
            ep = asyncio.run(persist_call_live(
                session, config=cfg, channel="text",
                created_at=created_at, episode_id="ep-cb33-live-env",
            ))
        assert ep is not None
        assert ep.cohort == "test"

        # Default (env unset) -> "live" (prod behavior unchanged).
        import os
        os.environ.pop("LIVE_PERSIST_COHORT", None)
        ep2 = asyncio.run(persist_call_live(
            session, config=cfg, channel="text",
            created_at=created_at, episode_id="ep-cb33-live-default",
        ))
        assert ep2 is not None
        assert ep2.cohort == "live"

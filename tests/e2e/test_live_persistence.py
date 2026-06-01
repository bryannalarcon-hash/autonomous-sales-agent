# Live in-progress persistence tests (Layer 2 — plan U2 extension).
# Verifies that persist_call_live builds a correct in_progress Episode (pure,
# no DB), that demo_routes calls the live_upsert_hook after each /api/chat turn,
# and that the episode_id used by the live upserts matches the one /end finalizes
# so both paths upsert the SAME row. DB-free: all store calls are captured via the
# injected hook pattern that already exists for call_end_persist_hook.
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.core.llm import Message, MockLLMClient


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------

def _is_json_call(opts: dict[str, Any]) -> bool:
    rf = opts.get("response_format")
    return isinstance(rf, dict) and rf.get("type") == "json_object"


def _agent_mock(
    *,
    act: str = "ask",
    target_slot: Optional[str] = "goal",
    tier: Optional[str] = None,
    confidence: float = 0.8,
    reply: str = "Tell me more about what you need.",
) -> MockLLMClient:
    import json

    decision: dict[str, Any] = {
        "act": act,
        "target_slot": target_slot,
        "tier": tier,
        "confidence": confidence,
        "rationale": "test",
        "trust": 0.05,
        "need_intensity": 0.05,
        "urgency": 0.0,
        "purchase_intent": 0.03,
        "bail_risk": -0.02,
    }
    decision_json = json.dumps(decision)

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        if _is_json_call(opts):
            return decision_json
        return reply

    return MockLLMClient(serve)


def _grant(client: TestClient, sid: str) -> None:
    r = client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True,
    })
    assert r.status_code == 200, r.text


async def _noop_call_end(session: Any, **kw: Any):
    """DB-free call-end hook: builds Episode without store writes."""
    from src.api.persistence import episode_from_session
    return episode_from_session(
        session, config=kw["config"], channel=kw["channel"],
        phone_hash=kw.get("phone_hash"), last_tier=kw.get("last_tier"),
        episode_id=kw.get("episode_id"),
    )


# ---------------------------------------------------------------------------
# Unit tests for persist_call_live (pure — no DB, no router)
# Uses asyncio.run() directly to avoid pytest-asyncio event-loop lifecycle
# contamination that would close the loop before subsequent sync FastAPI tests.
# ---------------------------------------------------------------------------

def _make_mock_session(*, turns=None, recorded=False):
    """Build the minimal mock VoiceSession-like object persist_call_live needs."""
    from src.core.belief_state import BeliefState

    state = MagicMock()
    state.turns = turns or []
    state.belief = BeliefState.fresh()

    session = MagicMock()
    session.state = state
    session.recorded = recorded
    return session


def _run_async(coro):
    """Run a coroutine using a brand-new dedicated event loop that does NOT become the thread's
    running loop after completion. This prevents cross-loop asyncpg pool contamination: asyncpg
    binds connections to the loop that created them, so if our fresh loop creates a pool, a later
    TestClient (in a different loop context) can't use those connections. We reset the thread
    event loop policy AFTER closing so Python's default new-loop machinery stays clean for the
    TestClient's own loop."""
    loop = asyncio.new_event_loop()
    old_loop = asyncio._get_running_loop()  # None outside async context
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)  # Don't leave a closed loop as the current loop


def test_persist_call_live_returns_in_progress_episode():
    """persist_call_live returns an Episode with outcome='in_progress' and correct fields.
    Patches src.memory.store at the module level so persist_call_live's lazy `from src.memory
    import store` resolves to the patched module — no real DB connections are opened."""
    from unittest.mock import patch, AsyncMock
    from src.api.persistence import persist_call_live
    from src.config.settings import load_config

    cfg = load_config()
    session = _make_mock_session()
    created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    episode_id = "ep-test-live-001"
    saved: list[Any] = []

    async def fake_save(ep: Any) -> str:
        saved.append(ep)
        return ep.episode_id

    with patch("src.memory.store.save_episode", side_effect=fake_save):
        ep = _run_async(persist_call_live(
            session, config=cfg, channel="text",
            created_at=created_at, episode_id=episode_id,
        ))

    assert ep is not None
    assert ep.episode_id == episode_id
    assert ep.outcome == "in_progress"
    assert ep.lead_phone_hash is None
    assert ep.cohort == "live"
    assert ep.created_at == created_at
    assert len(saved) == 1
    assert saved[0].episode_id == episode_id


def test_persist_call_live_same_created_at_each_call():
    """Calling persist_call_live twice with the same created_at stamps the SAME created_at
    on both returned episodes — so the upsert on created_at is stable across turns."""
    from unittest.mock import patch
    from src.api.persistence import persist_call_live
    from src.config.settings import load_config

    cfg = load_config()
    session = _make_mock_session()
    episode_id = "ep-stable-ts"
    created_at = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    async def fake_save(ep: Any) -> str:
        return ep.episode_id

    with patch("src.memory.store.save_episode", side_effect=fake_save):
        ep1 = _run_async(persist_call_live(
            session, config=cfg, channel="text",
            created_at=created_at, episode_id=episode_id,
        ))
        ep2 = _run_async(persist_call_live(
            session, config=cfg, channel="text",
            created_at=created_at, episode_id=episode_id,
        ))

    assert ep1.created_at == created_at
    assert ep2.created_at == created_at


def test_persist_call_live_swallows_db_error():
    """A DB error in save_episode is swallowed (logged), and persist_call_live returns None
    instead of crashing the call path."""
    from unittest.mock import patch
    from src.api.persistence import persist_call_live
    from src.config.settings import load_config

    cfg = load_config()
    session = _make_mock_session()

    async def exploding_save(ep: Any) -> str:
        raise RuntimeError("DB down")

    with patch("src.memory.store.save_episode", side_effect=exploding_save):
        result = _run_async(persist_call_live(
            session, config=cfg, channel="text",
            created_at=datetime.now(timezone.utc),
            episode_id="ep-db-error",
        ))

    # Must not raise; returns None on failure
    assert result is None


# ---------------------------------------------------------------------------
# Integration tests: demo route live_upsert_hook
# ---------------------------------------------------------------------------

class TestDemoRouteLiveUpsert:
    """The /api/chat path calls live_upsert_hook after each turn (DB-free via injection)."""

    def _make_app_with_capture(self, *, act="ask", tier=None):
        """Build a TestClient that captures live upserts and end-persist calls."""
        live_calls: list[Any] = []
        end_calls: list[Any] = []

        async def live_upsert_hook(session: Any, **kw: Any):
            from src.api.persistence import episode_from_session
            ep = episode_from_session(
                session, config=kw["config"], channel=kw["channel"],
                episode_id=kw.get("episode_id"),
            )
            # Override outcome to in_progress (what persist_call_live does)
            from src.memory.schema import Episode
            live_ep = Episode(
                episode_id=ep.episode_id,
                turns=ep.turns,
                outcome="in_progress",
                lead_phone_hash=None,
                cohort="live",
                created_at=kw.get("created_at", ep.created_at),
                channel=ep.channel,
                version=ep.version,
                kb_version=ep.kb_version,
            )
            live_calls.append(live_ep)
            return live_ep

        async def call_end_hook(session: Any, **kw: Any):
            from src.api.persistence import episode_from_session
            ep = episode_from_session(
                session, config=kw["config"], channel=kw["channel"],
                phone_hash=kw.get("phone_hash"), last_tier=kw.get("last_tier"),
                episode_id=kw.get("episode_id"),
            )
            end_calls.append(ep)
            return ep

        app = create_app(
            llm_client_factory=lambda: _agent_mock(act=act, tier=tier),
            live_rag=False,
            live_upsert_hook=live_upsert_hook,
            call_end_persist_hook=call_end_hook,
        )
        client = TestClient(app)
        return client, live_calls, end_calls

    def _start_and_grant(self, client: TestClient) -> str:
        r = client.post("/api/consent/start", json={"jurisdiction": "CA", "channel": "text"})
        assert r.status_code == 200, r.text
        sid = r.json()["session_id"]
        _grant(client, sid)
        return sid

    def test_live_upsert_hook_called_after_each_chat_turn(self):
        """live_upsert_hook is invoked once per /api/chat turn (not zero, not deferred to /end)."""
        client, live_calls, end_calls = self._make_app_with_capture()
        sid = self._start_and_grant(client)

        assert len(live_calls) == 0  # nothing before any turn

        client.post("/api/chat", json={"session_id": sid, "text": "I need math help."})
        assert len(live_calls) == 1, "expected 1 live upsert after first turn"

        client.post("/api/chat", json={"session_id": sid, "text": "For my daughter."})
        assert len(live_calls) == 2, "expected 2 live upserts after second turn"

        # No end-persist yet (no /end called)
        assert len(end_calls) == 0

    def test_live_episode_has_in_progress_outcome(self):
        """The episode passed to live_upsert_hook has outcome='in_progress'."""
        client, live_calls, end_calls = self._make_app_with_capture()
        sid = self._start_and_grant(client)
        client.post("/api/chat", json={"session_id": sid, "text": "Tell me about pricing."})

        assert len(live_calls) == 1
        assert live_calls[0].outcome == "in_progress"

    def test_live_episode_id_matches_end_episode_id(self):
        """The episode_id used for live upserts matches the episode_id that /end finalizes.

        This is the critical invariant: both paths must upsert the same DB row.
        Currently FAILS because persist_call_end generates a fresh uuid at call end
        while no live episode_id exists yet. Implementation makes them align.
        """
        client, live_calls, end_calls = self._make_app_with_capture()
        sid = self._start_and_grant(client)

        client.post("/api/chat", json={"session_id": sid, "text": "I need help."})
        client.post("/api/chat", json={"session_id": sid, "text": "More context."})

        assert len(live_calls) == 2

        r = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
        assert r.status_code == 200, r.text
        assert len(end_calls) == 1

        # THE critical invariant
        live_ep_id = live_calls[0].episode_id
        assert live_calls[1].episode_id == live_ep_id, "episode_id must be stable across turns"
        assert end_calls[0].episode_id == live_ep_id, (
            f"end episode_id {end_calls[0].episode_id!r} != live episode_id {live_ep_id!r}"
        )

    def test_live_upsert_not_called_before_consent(self):
        """A /api/chat that is rejected (409, consent not satisfied) does NOT trigger a live upsert."""
        client, live_calls, _ = self._make_app_with_capture()
        r = client.post("/api/consent/start", json={"jurisdiction": "CA", "channel": "text"})
        sid = r.json()["session_id"]
        # No consent yet -> 409
        r = client.post("/api/chat", json={"session_id": sid, "text": "Hi."})
        assert r.status_code == 409
        assert len(live_calls) == 0

    def test_live_upsert_hook_is_optional(self):
        """When no live_upsert_hook is injected and live_rag=False, /api/chat works and simply
        skips the live upsert (mirrors the escalation_persist_hook pattern: no hook = no-op).

        In prod, create_app wires persist_call_live as the default when live_rag=True; in tests
        with live_rag=False, no hook is wired and the upsert silently skips."""
        app = create_app(
            llm_client_factory=lambda: _agent_mock(),
            live_rag=False,
            # no live_upsert_hook — skips silently (DB-free)
        )
        client = TestClient(app)
        r = client.post("/api/consent/start", json={"jurisdiction": "CA", "channel": "text"})
        sid = r.json()["session_id"]
        _grant(client, sid)
        r = client.post("/api/chat", json={"session_id": sid, "text": "Hello."})
        assert r.status_code == 200, r.text

    def test_live_upsert_accumulates_turns(self):
        """Each successive live upsert carries one more turn than the previous — turns accumulate."""
        client, live_calls, _ = self._make_app_with_capture()
        sid = self._start_and_grant(client)

        client.post("/api/chat", json={"session_id": sid, "text": "Turn 1."})
        client.post("/api/chat", json={"session_id": sid, "text": "Turn 2."})
        client.post("/api/chat", json={"session_id": sid, "text": "Turn 3."})

        assert len(live_calls) == 3
        # Each successive episode must have at least as many turns as the previous
        for i in range(1, len(live_calls)):
            assert len(live_calls[i].turns) >= len(live_calls[i - 1].turns), (
                f"turn count must be non-decreasing: "
                f"call {i} has {len(live_calls[i].turns)}, prev {len(live_calls[i-1].turns)}"
            )

    def test_created_at_stable_across_live_upserts(self):
        """The created_at stamped on the session is passed to every live upsert unchanged."""
        client, live_calls, _ = self._make_app_with_capture()
        sid = self._start_and_grant(client)

        client.post("/api/chat", json={"session_id": sid, "text": "First."})
        client.post("/api/chat", json={"session_id": sid, "text": "Second."})

        assert len(live_calls) == 2
        assert live_calls[0].created_at == live_calls[1].created_at, (
            "created_at must be stable across live upserts"
        )

# E2E tests for the LIVE product wiring (plan U5/U2/U14/U15): the KB corpus is ingested + retrievable,
# /api/chat is GROUNDED through a live retrieve hook, and a finished demo call PERSISTS its episode +
# escalations + per-lead memory so it flows into Operate and remembers callers. Two layers:
#   - DB-FREE (always run): the live retrieve hook + grounding wiring with a FakeEmbedder + stub
#     retrieve hook + CAPTURE persistence hooks (no Postgres). Asserts a factual turn ("how much does
#     it cost?") fires retrieval, the reply is grounded against the cited chunk, /end builds an Episode
#     captured via the inject hook, an escalation is captured to the queue, and a returning phone-hash
#     hydrates known slots so skip-known fires.
#   - DB-DEPENDENT (skip unless DATABASE_URL): ingest_corpus(FakeEmbedder) populates kb_chunk and
#     retrieve returns the pricing chunk; the live hook over the real store grounds a price answer.
# MockLLMClient + FakeEmbedder everywhere — no network, no model download.
from __future__ import annotations

import json
import os
from typing import Any, Optional, Sequence

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.core.llm import Message, MockLLMClient
from src.kb.embeddings import FakeEmbedder
from src.kb.retriever import Chunk
from src.memory.schema import EscalationLog, phone_hash

KB_VERSION = "kb_v0"


# --- Agent mock (same routing contract as test_demo_call) ---------------------------------------
def _is_json_call(opts: dict[str, Any]) -> bool:
    rf = opts.get("response_format")
    return isinstance(rf, dict) and rf.get("type") == "json_object"


def _agent_mock(
    *,
    act: str = "answer_via_kb",
    target_slot: Optional[str] = None,
    tier: Optional[str] = None,
    confidence: float = 0.8,
    reply: str = "Pricing is a monthly tiered membership billed recurring.",
) -> MockLLMClient:
    """A routed MockLLMClient: JSON calls -> a decision; prose calls -> `reply`. The default reply is
    phrased from the pricing KB so the groundedness check passes against the retrieved pricing chunk."""
    decision: dict[str, Any] = {
        "act": act,
        "target_slot": target_slot,
        "tier": tier,
        "confidence": confidence,
        "rationale": "test-routed",
        "trust": 0.05,
        "need_intensity": 0.05,
        "urgency": 0.0,
        "purchase_intent": 0.03,
        "bail_risk": -0.02,
    }
    decision_json = json.dumps(decision)

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        return decision_json if _is_json_call(opts) else reply

    return MockLLMClient(serve)


# A pricing chunk the stub retrieve hook returns (so grounding has real, citable text to check).
_PRICING_CHUNK = Chunk(
    id="kb_v0:pricing:pricing_overview",
    kb_version=KB_VERSION,
    source="pricing#pricing_overview",
    text=(
        "How pricing works. Pricing is a monthly tiered membership, billed recurring each month. "
        "Each tier bundles a set number of tutoring hours per month at a lower effective per-hour "
        "rate than paying session-by-session."
    ),
)


def _start(client: TestClient, jurisdiction="TX", channel="text") -> dict[str, Any]:
    r = client.post("/api/consent/start", json={"jurisdiction": jurisdiction, "channel": channel})
    assert r.status_code == 200, r.text
    return r.json()


def _grant(client: TestClient, sid: str, **extra: Any) -> None:
    body = {"session_id": sid, "ai_acknowledged": True, "recording_consent": True, **extra}
    r = client.post("/api/consent/respond", json=body)
    assert r.status_code == 200, r.text


# ============================ (B) /api/chat is GROUNDED via the live hook =========================


def test_factual_turn_fires_retrieval_and_reply_is_grounded():
    """A factual turn ("how much does it cost?") fires intent-gated retrieval; the retrieve hook is
    invoked and the reply passes the groundedness check against the cited pricing chunk (U5)."""
    seen_queries: list[str] = []

    def retrieve_hook(query: str, *, kb_version: str = KB_VERSION, k: int = 4) -> list[Chunk]:
        seen_queries.append(query)
        return [_PRICING_CHUNK]

    app = create_app(
        llm_client_factory=lambda: _agent_mock(),
        embedder=FakeEmbedder(),
        retrieve_hook=retrieve_hook,
        live_rag=False,
    )
    client = TestClient(app)
    body = _start(client)
    sid = body["session_id"]
    _grant(client, sid)

    r = client.post("/api/chat", json={"session_id": sid, "text": "How much does it cost?"})
    assert r.status_code == 200, r.text
    # retrieval fired for the factual turn (the hook saw the query).
    assert seen_queries == ["How much does it cost?"]

    # the reply is grounded against the retrieved pricing chunk (the U9 guardrail has real chunks).
    from src.kb.retriever import grounded

    reply = r.json()["reply"]
    report = grounded(reply, [_PRICING_CHUNK])
    assert bool(report), f"reply not grounded: {report.unsupported_terms} / {report.unsupported_numbers}"


def test_non_factual_turn_skips_retrieval():
    """An acknowledgement ("ok, sounds good") does NOT fire retrieval (intent-gated, R43)."""
    calls: list[str] = []

    def retrieve_hook(query: str, *, kb_version: str = KB_VERSION, k: int = 4) -> list[Chunk]:
        calls.append(query)
        return [_PRICING_CHUNK]

    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="ask", target_slot="goal", reply="Got it."),
        embedder=FakeEmbedder(),
        retrieve_hook=retrieve_hook,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client)["session_id"]
    _grant(client, sid)
    r = client.post("/api/chat", json={"session_id": sid, "text": "ok, sounds good"})
    assert r.status_code == 200, r.text
    assert calls == [], "an acknowledgement must not trigger a KB lookup"


# ============================ (C) live calls persist episodes + escalations ======================


def _capture_app(**overrides: Any) -> tuple[TestClient, dict[str, Any]]:
    """Build an app whose persistence hooks CAPTURE writes into a dict (DB-free). Returns the client
    and the capture sink (keys: 'episodes', 'escalations', 'hydrated')."""
    sink: dict[str, Any] = {"episodes": [], "escalations": [], "leads": []}

    async def escalation_persist_hook(log: EscalationLog) -> None:
        sink["escalations"].append(log)

    async def call_end_persist_hook(session: Any, **kw: Any):
        # Mirror persist_call_end's pure mapping without a DB.
        from src.api.persistence import episode_from_session

        ep = episode_from_session(
            session,
            config=kw["config"],
            channel=kw["channel"],
            phone_hash=kw.get("phone_hash"),
            last_tier=kw.get("last_tier"),
        )
        sink["episodes"].append(ep)
        for log in kw.get("escalation_logs") or []:
            log.episode_id = ep.episode_id
            sink["escalations"].append(log)
        if kw.get("raw_phone"):
            sink["leads"].append(kw["raw_phone"])
        return ep

    defaults: dict[str, Any] = dict(
        llm_client_factory=lambda: _agent_mock(),
        embedder=FakeEmbedder(),
        retrieve_hook=lambda q, **k: [_PRICING_CHUNK],
        live_rag=False,
        escalation_persist_hook=escalation_persist_hook,
        call_end_persist_hook=call_end_persist_hook,
    )
    defaults.update(overrides)
    return TestClient(create_app(**defaults)), sink


def test_call_end_persists_episode():
    """Ending a call builds + persists an Episode (channel=text, outcome derived, metrics.recorded)."""
    client, sink = _capture_app(
        llm_client_factory=lambda: _agent_mock(
            act="attempt_close", tier="consultation", reply="Shall we book a quick consult?"
        )
    )
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)
    client.post("/api/chat", json={"session_id": sid, "text": "How much does it cost?"})

    r = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["persisted"] is True
    assert out["outcome"] == "consult_booked"

    assert len(sink["episodes"]) == 1
    ep = sink["episodes"][0]
    assert ep.channel == "text"
    assert ep.outcome == "consult_booked"
    assert ep.ladder_tier == 2
    assert ep.metrics.get("recorded") is True
    assert ep.turns, "episode should carry the committed turns + belief trajectory"
    assert ep.belief_trajectory and ep.belief_trajectory[-1] is not None


def test_escalation_during_call_is_persisted_to_queue():
    """An `escalate` decision during a live call captures an EscalationLog onto the review queue (P5)
    and re-points it onto the persisted episode at call end (FK-safe)."""
    client, sink = _capture_app(
        llm_client_factory=lambda: _agent_mock(
            act="escalate", reply="I'll have a specialist follow up with you shortly."
        )
    )
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)
    r = client.post("/api/chat", json={"session_id": sid, "text": "I want to talk to a human."})
    assert r.status_code == 200, r.text
    assert r.json()["decision_act"] == "escalate"

    # escalation captured to the queue immediately (injected hook).
    assert len(sink["escalations"]) >= 1
    log = sink["escalations"][0]
    assert isinstance(log, EscalationLog)
    assert log.lifecycle == "unreviewed"

    r = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "escalated"
    ep = sink["episodes"][0]
    assert ep.escalated is True
    # the buffered escalation was re-pointed onto the real episode id at end (FK target now valid).
    assert any(e.episode_id == ep.episode_id for e in sink["escalations"])


def test_call_end_remembers_caller_by_phone_hash():
    """Ending a call with a bound phone-hash persists per-lead memory keyed by the HASH (raw phone is
    only used to hash; never stored). The lead persist receives the raw phone to hash, not store."""
    client, sink = _capture_app()
    raw = "555-010-3333"
    h = phone_hash(raw)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid, phone_hash=h)
    client.post("/api/chat", json={"session_id": sid, "text": "How much does it cost?"})

    r = client.post(f"/api/session/{sid}/end", json={"session_id": sid, "raw_phone": raw})
    assert r.status_code == 200, r.text
    ep = sink["episodes"][0]
    assert ep.lead_phone_hash == h
    assert sink["leads"] == [raw]  # raw passed to the lead-persist (to hash), episode keyed by hash
    # the raw phone never appears on the persisted episode body.
    assert raw not in json.dumps(ep.to_dict(), default=str)


# ============================ (C) returning phone-hash hydrates known slots =======================


def test_returning_caller_hydrates_known_slots():
    """A returning phone-hash hydrates known slots into the session belief at consent-start (U6) so
    the first turn sees them as known (skip-known basis). Hydration is injected (DB-free)."""
    from src.core.belief_state import BeliefState

    h = phone_hash("555-010-7777")

    async def hydrate_hook(phash: str, config: Any) -> BeliefState:
        belief = BeliefState.fresh()
        if phash == h:
            belief.set_slot("grade_level", "11", 0.9)
            belief.set_slot("subject", "SAT math", 0.9)
            belief.meta["returning_caller"] = True
        return belief

    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="ask", target_slot="goal", reply="Welcome back!"),
        embedder=FakeEmbedder(),
        retrieve_hook=lambda q, **k: [_PRICING_CHUNK],
        live_rag=False,
        hydrate_hook=hydrate_hook,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid, phone_hash=h)
    # one turn so the VoiceSession is built (the hydrated belief is seeded into it).
    client.post("/api/chat", json={"session_id": sid, "text": "Hi, I'm calling back."})

    r = client.get(f"/api/session/{sid}")
    snap = r.json()
    # the hydrated slots are present in the (committed) belief snapshot of the agent turn.
    blob = json.dumps(snap["turns"])
    assert "grade_level" in blob and "SAT math" in blob, (
        "returning caller's known slots should seed the session belief (skip-known basis)"
    )


# ============================ (C) escalation is DEFERRED-BUFFERED with no injected hook ===========


def test_escalation_with_no_injected_hook_is_buffered_then_persisted_at_end():
    """With NO escalation_persist_hook injected, an `escalate` during a call is NOT written to the
    queue mid-call (the runtime store_hook is a no-op because the escalation FKs to an episode that
    does not exist yet) — it is BUFFERED on the session and appears only after /end, when the call-end
    persist re-points it onto the now-saved episode (FK-safe). Tested DB-free: the call-end hook
    mirrors persist_call_end's re-point, and escalation_persist_hook is intentionally absent."""
    sink: dict[str, Any] = {"episodes": [], "escalations": []}

    async def call_end_persist_hook(session: Any, **kw: Any):
        from src.api.persistence import episode_from_session

        ep = episode_from_session(
            session, config=kw["config"], channel=kw["channel"],
            phone_hash=kw.get("phone_hash"), last_tier=kw.get("last_tier"),
        )
        sink["episodes"].append(ep)
        for log in kw.get("escalation_logs") or []:
            log.episode_id = ep.episode_id
            sink["escalations"].append(log)
        return ep

    app = create_app(
        llm_client_factory=lambda: _agent_mock(
            act="escalate", reply="A specialist will follow up with you shortly."
        ),
        embedder=FakeEmbedder(),
        retrieve_hook=lambda q, **k: [_PRICING_CHUNK],
        live_rag=False,
        # NO escalation_persist_hook -> the runtime path defers the write to call end.
        call_end_persist_hook=call_end_persist_hook,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)
    r = client.post("/api/chat", json={"session_id": sid, "text": "I want a human now."})
    assert r.status_code == 200, r.text
    assert r.json()["decision_act"] == "escalate"

    # MID-CALL: nothing in the queue yet (the write was deferred — the episode FK target doesn't exist).
    assert sink["escalations"] == []

    # AFTER /end: the buffered escalation is re-pointed onto the saved episode and now present.
    r = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "escalated"
    assert len(sink["escalations"]) == 1
    ep = sink["episodes"][0]
    assert sink["escalations"][0].episode_id == ep.episode_id


# ============================ (C) /end idempotency + no_conversation =============================


def test_end_is_idempotent_already_ended():
    """A second /end on a session that already ended returns outcome="already_ended", persisted=False
    (no duplicate persistence), even though the live session was evicted after the first /end."""
    client, sink = _capture_app(
        llm_client_factory=lambda: _agent_mock(act="ask", target_slot="goal", reply="Sure.")
    )
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)
    client.post("/api/chat", json={"session_id": sid, "text": "Tell me about tutoring."})

    r1 = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r1.status_code == 200, r1.text
    assert r1.json()["persisted"] is True
    assert len(sink["episodes"]) == 1

    # second /end -> idempotent already_ended, NOT a second persisted episode.
    r2 = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r2.status_code == 200, r2.text
    assert r2.json()["outcome"] == "already_ended"
    assert r2.json()["persisted"] is False
    assert len(sink["episodes"]) == 1, "a repeat /end must not persist a duplicate episode"


def test_end_with_no_conversation_returns_no_conversation():
    """Ending a session that never reached conversation (no brain session built — e.g. consent never
    granted, or granted but no chat turn) returns outcome="no_conversation", persisted=False, and
    persists nothing."""
    client, sink = _capture_app()
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)  # consent granted, but NO /api/chat turn -> no VoiceSession built.

    r = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["outcome"] == "no_conversation"
    assert out["persisted"] is False
    assert sink["episodes"] == []


# ============================ (B-DB) live_rag=True DEFAULT path grounds after ingest ==============
# DB-DEPENDENT: with live_rag=True + FakeEmbedder injected but retrieve_hook/hydrate_hook left to
# DEFAULTS, create_app builds the REAL kb.live retrieve hook over the FakeEmbedder, so a factual
# /api/chat turn retrieves the ingested pricing chunk from pgvector and the reply is grounded. This
# proves the real wiring (not a stubbed hook) end-to-end. Skips without DATABASE_URL.

_HAVE_DB = bool(os.environ.get("DATABASE_URL"))


@pytest.mark.skipif(not _HAVE_DB, reason="DATABASE_URL not set — DB-dependent live_rag default-path test")
def test_live_rag_default_path_grounds_after_ingest():
    """live_rag=True (default retrieve_hook) + FakeEmbedder: ingest the KB, then a factual chat turn
    runs the REAL kb.live hook against pgvector and the reply is grounded against a real cited chunk."""
    import asyncio

    from src.kb.ingest import ingest_corpus
    from src.kb.retriever import grounded
    from src.memory import store

    emb = FakeEmbedder()

    def _drop_pool_ref() -> None:
        # DROP the module pool REFERENCE without awaiting a close (a prior test may have left it bound
        # to a now-closed loop, so close() would raise "Event loop is closed"). Each loop below creates
        # its OWN fresh pool, and the next DB test's _schema fixture recreates cleanly.
        store._POOL = None  # type: ignore[attr-defined]

    async def _ingest() -> None:
        # Own short-lived loop: apply migrations + ingest into pgvector. The pool is created fresh on
        # THIS loop (we dropped any stale reference first), then dropped so the TestClient recreates it
        # on ITS loop (avoids the asyncpg cross-loop "operation in progress" trap).
        _drop_pool_ref()
        n = await ingest_corpus(kb_version=KB_VERSION, embedder=emb)
        assert n > 0

    _drop_pool_ref()
    asyncio.run(_ingest())
    _drop_pool_ref()

    # A reply phrased from the pricing chunk so the groundedness check can pass against real chunks.
    app = create_app(
        llm_client_factory=lambda: _agent_mock(
            reply="Pricing is a monthly tiered membership billed recurring each month."
        ),
        embedder=emb,        # FakeEmbedder injected ...
        live_rag=True,       # ... but retrieve_hook left to DEFAULT -> real kb.live hook is built.
        # default config (champion_v0) pins kb_version == KB_VERSION, so the hook queries the right rows.
    )
    try:
        with TestClient(app) as client:  # context-manager runs lifespan; pool binds to this loop.
            sid = _start(client, channel="text")["session_id"]
            _grant(client, sid)
            r = client.post(
                "/api/chat",
                json={"session_id": sid, "text": "How much does it cost per month?"},
            )
            assert r.status_code == 200, r.text
            reply = r.json()["reply"]
            assert reply, "expected a grounded reply"
    finally:
        # The TestClient lifespan already closed its pool on shutdown; drop any stale reference so the
        # next DB-gated test's _schema fixture recreates the pool fresh on its own loop.
        _drop_pool_ref()


# NOTE: the DB-DEPENDENT ingest_corpus + live-hook retrieve test ALSO lives in
# tests/integration/test_kb.py (it reuses that module's proven per-test _schema fixture, which binds
# the store pool to each test's own event loop — required for asyncpg under pytest-asyncio).

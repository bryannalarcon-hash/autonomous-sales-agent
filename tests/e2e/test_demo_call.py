# End-to-end tests for the U13 web-demo call (plan U13; AE9 + R1/R3/R33/R40/R41/R42). Drives the
# thin FastAPI surface (src.api.server) with fastapi.testclient.TestClient and an injected
# MockLLMClient so NOTHING hits the network or a DB. Asserts the consent gate guards /api/chat
# (409 until satisfied), the refusal path proceeds unrecorded (episode flagged not-recorded), the
# suspected-minor path blocks on need_parental, sticky-voice assignment (new + returning caller),
# and that any logged turn body is PII-scrubbed while the lead key stays the phone-hash (raw absent).
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.core.llm import Message, MockLLMClient
from src.memory.schema import phone_hash


# --- Agent mock (same routing contract as test_voice_adapter / test_selfplay) --------------------
def _is_json_call(opts: dict[str, Any]) -> bool:
    rf = opts.get("response_format")
    return isinstance(rf, dict) and rf.get("type") == "json_object"


def _agent_mock(
    *,
    act: str = "ask",
    target_slot: Optional[str] = "goal",
    tier: Optional[str] = None,
    confidence: float = 0.8,
    reply: str = "Tell me a bit about what your child is working on.",
) -> MockLLMClient:
    decision: dict[str, Any] = {
        "act": act,
        "target_slot": target_slot,
        "tier": tier,
        "confidence": confidence,
        "rationale": "test-routed combined proposal",
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


def _client(**kw) -> TestClient:
    """Build a TestClient over an app whose LLM client is a fresh routed MockLLMClient per session
    (so call-state never collides) and whose lead lookup is overridable for the returning-caller
    test. DB-free: live_rag is OFF (no real embedder / no pgvector retrieve) and no store calls — these
    consent/voice/token tests don't exercise grounding (see tests/e2e/test_live_rag_persistence.py)."""
    app = create_app(llm_client_factory=lambda: _agent_mock(), live_rag=False, **kw)
    return TestClient(app)


def _start(client: TestClient, jurisdiction="CA", channel="voice") -> dict[str, Any]:
    r = client.post("/api/consent/start", json={"jurisdiction": jurisdiction, "channel": channel})
    assert r.status_code == 200, r.text
    return r.json()


# =============================== AE9: DISCLOSURE + CONSENT GATE ==================================


def test_call_opens_with_ai_and_recording_disclosure():
    client = _client()
    body = _start(client)
    assert "session_id" in body
    assert body["consent_mode"] == "all_party"  # CA is all-party
    disc = body["disclosure_text"].lower()
    assert "ai" in disc
    assert "record" in disc


def test_chat_is_refused_until_consent_then_proceeds():
    client = _client()
    body = _start(client)
    sid = body["session_id"]

    # BEFORE consent -> 409
    r = client.post("/api/chat", json={"session_id": sid, "text": "Hi, my daughter needs algebra help."})
    assert r.status_code == 409, r.text

    # grant consent
    r = client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ready"

    # AFTER consent -> proceeds
    r = client.post("/api/chat", json={"session_id": sid, "text": "Hi, my daughter needs algebra help."})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "reply" in payload and payload["reply"]
    assert "decision_act" in payload
    assert payload["done"] in (True, False)


# =============================== REFUSAL PATH (R41) =============================================


def test_recording_refused_proceeds_unrecorded_and_episode_flagged():
    client = _client()
    body = _start(client)
    sid = body["session_id"]

    r = client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": False,
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "unrecorded"

    # the call STILL proceeds (unrecorded), so /api/chat works
    r = client.post("/api/chat", json={"session_id": sid, "text": "How much is tutoring?"})
    assert r.status_code == 200, r.text

    # the episode is flagged not-recorded
    r = client.get(f"/api/session/{sid}")
    assert r.status_code == 200, r.text
    assert r.json()["recorded"] is False
    assert r.json()["state"] == "unrecorded"


# =============================== MINOR -> PARENTAL (R40) ========================================


def test_suspected_minor_blocks_until_parental_consent():
    client = _client()
    body = _start(client, jurisdiction="NY")
    sid = body["session_id"]

    r = client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True, "is_minor": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "need_parental"

    # chat is GATED while need_parental
    r = client.post("/api/chat", json={"session_id": sid, "text": "I'm in 9th grade calling for myself."})
    assert r.status_code == 409, r.text

    # parental consent -> proceeds
    r = client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True,
        "is_minor": True, "parental_consent": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ready"

    r = client.post("/api/chat", json={"session_id": sid, "text": "I need algebra help."})
    assert r.status_code == 200, r.text


# =============================== STICKY VOICE (R3) =============================================


def test_new_caller_gets_deterministic_sticky_voice():
    client = _client()
    h = phone_hash("555-010-0001")
    body = _start(client)
    sid = body["session_id"]
    # bind the caller's phone-hash to the session
    r = client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True, "phone_hash": h,
    })
    assert r.status_code == 200, r.text

    r = client.get(f"/api/session/{sid}")
    v = r.json()["assigned_voice"]
    assert v in ("voice_a", "voice_b")

    # a SECOND session for the same phone-hash gets the SAME (sticky) voice
    body2 = _start(client)
    sid2 = body2["session_id"]
    client.post("/api/consent/respond", json={
        "session_id": sid2, "ai_acknowledged": True, "recording_consent": True, "phone_hash": h,
    })
    v2 = client.get(f"/api/session/{sid2}").json()["assigned_voice"]
    assert v2 == v


def test_returning_caller_reuses_assigned_voice():
    """A returning caller with a stored lead.assigned_voice reuses it (U6 hydrate), overriding the
    fresh deterministic pick. Injected via a lead lookup so the test stays DB-free."""
    h = phone_hash("555-010-9999")
    # pick the voice that is NOT the fresh deterministic assignment so the test proves reuse wins
    from src.voice.consent import assign_voice
    fresh = assign_voice(h)
    stored = "voice_b" if fresh == "voice_a" else "voice_a"

    def lead_voice_lookup(phash: str) -> Optional[str]:
        return stored if phash == h else None

    app = create_app(
        llm_client_factory=lambda: _agent_mock(),
        lead_voice_lookup=lead_voice_lookup,
        live_rag=False,
    )
    client = TestClient(app)
    body = _start(client)
    sid = body["session_id"]
    client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True, "phone_hash": h,
    })
    assert client.get(f"/api/session/{sid}").json()["assigned_voice"] == stored


# =============================== PII SCRUB IN LOGGED TURN (R42) =================================


def test_logged_turn_has_pii_scrubbed_and_key_is_phone_hash():
    client = _client()
    raw_phone = "555-010-0001"
    h = phone_hash(raw_phone)
    body = _start(client)
    sid = body["session_id"]
    client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True, "phone_hash": h,
    })

    # user turn carrying an email + a phone number in the body
    dirty = "Email me at parent@example.com or call 555-010-0001 about tutoring."
    r = client.post("/api/chat", json={"session_id": sid, "text": dirty})
    assert r.status_code == 200, r.text

    # the captured transcript turns are PII-scrubbed
    r = client.get(f"/api/session/{sid}")
    snap = r.json()
    transcript = json.dumps(snap["turns"])
    assert "parent@example.com" not in transcript
    assert "555-010-0001" not in transcript
    assert "[EMAIL]" in transcript
    assert "[PHONE]" in transcript

    # the lead key is the phone-hash; the raw phone is NEVER present anywhere in the session view
    whole = json.dumps(snap)
    assert raw_phone not in whole
    assert snap["lead_phone_hash"] == h


# =============================== TEXT CHANNEL (R1) =============================================


# =============================== LIVEKIT TOKEN GUARD ===========================================


def test_livekit_token_returns_503_without_creds(monkeypatch):
    """The token endpoint returns a clear 503 (not a crash) when LiveKit creds are absent, and only
    after consent permits voice — it never prints secrets."""
    for k in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_URL"):
        monkeypatch.delenv(k, raising=False)
    client = _client()
    body = _start(client)
    sid = body["session_id"]

    # gated before consent -> 409
    r = client.post("/api/livekit/token", json={"session_id": sid, "identity": "caller"})
    assert r.status_code == 409, r.text

    client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True,
    })
    r = client.post("/api/livekit/token", json={"session_id": sid, "identity": "caller"})
    assert r.status_code == 503, r.text


def test_livekit_token_minted_with_creds(monkeypatch):
    """With creds present + consent granted, a token/url/room is minted (no secret leaks into the
    body). The token builder is INJECTED (a fake) so this test never imports the livekit SDK — which
    would pollute the global sys.modules the core's livekit-leak guard tests assert on. The builder
    still receives the real creds from env, proving the wiring without leaking the secret."""
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret_value_should_never_appear")
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")

    seen: dict[str, str] = {}

    def fake_builder(api_key: str, api_secret: str, identity: str, room: str) -> str:
        seen.update(api_key=api_key, api_secret=api_secret, identity=identity, room=room)
        return "fake.jwt.token"  # never embeds the secret

    app = create_app(
        llm_client_factory=lambda: _agent_mock(), token_builder=fake_builder, live_rag=False
    )
    client = TestClient(app)
    body = _start(client)
    sid = body["session_id"]
    client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True,
    })
    r = client.post("/api/livekit/token", json={"session_id": sid, "identity": "caller"})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["token"] == "fake.jwt.token"
    assert payload["url"] == "wss://example.livekit.cloud"
    assert payload["room"] == f"demo-{sid}"
    # the builder got the real creds (wiring proven) ...
    assert seen["api_key"] == "devkey"
    assert seen["api_secret"] == "devsecret_value_should_never_appear"
    # ... but the raw secret never appears in the response body.
    assert "devsecret_value_should_never_appear" not in json.dumps(payload)


def test_text_channel_one_party_jurisdiction_consent_flow():
    """The text console (channel=text) in a one-party jurisdiction still discloses AI + recording;
    granting reaches ready and chat proceeds."""
    client = _client()
    body = _start(client, jurisdiction="TX", channel="text")
    assert body["consent_mode"] == "one_party"
    sid = body["session_id"]
    client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True,
    })
    r = client.post("/api/chat", json={"session_id": sid, "text": "What subjects do you tutor?"})
    assert r.status_code == 200, r.text

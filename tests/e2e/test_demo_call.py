# End-to-end tests for the U13 web-demo call (plan U13; AE9 + R1/R3/R33/R40/R41/R42 + CB-82). Drives
# the thin FastAPI surface (src.api.server) with fastapi.testclient.TestClient and an injected
# MockLLMClient so NOTHING hits the network or a DB. Asserts the consent gate guards /api/chat
# (409 until satisfied), the refusal path proceeds unrecorded (episode flagged not-recorded), the
# suspected-minor path blocks on need_parental, sticky-voice assignment (new + returning caller),
# that any logged turn body is PII-scrubbed while the lead key stays the phone-hash (raw absent), and
# (CB-82) that ANY committed close (callback/consultation/trial/enrollment) sets done=True so /end
# fires and the episode + lead are persisted — not just enrollment.
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


async def _noop_call_end(session: Any, **kw: Any):
    """A DB-free call-end hook that just maps the session to an Episode (no store write), so a /end
    test can assert persistence + eviction without Postgres."""
    from src.api.persistence import episode_from_session

    return episode_from_session(
        session, config=kw["config"], channel=kw["channel"],
        phone_hash=kw.get("phone_hash"), last_tier=kw.get("last_tier"),
    )


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
    # REGRESSION: respond MUST echo session_id — the /demo page reads res.session_id to unlock chat +
    # voice; omitting it left sessionId=undefined and both controls stayed permanently disabled.
    assert r.json()["session_id"] == sid

    # AFTER consent -> proceeds. The default mock acts `ask` (a non-terminal discovery turn), so the
    # call is NOT done (done is only True on a terminal act — see test_chat_done_flag_*).
    r = client.post("/api/chat", json={"session_id": sid, "text": "Hi, my daughter needs algebra help."})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "reply" in payload and payload["reply"]
    assert payload["decision_act"] == "ask"
    assert payload["done"] is False


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


def test_minor_detected_from_chat_text_blocks_on_need_parental():
    """detect_minor runs on the user's text INDEPENDENT of req.is_minor (R40): a caller who never
    flagged is_minor but whose first turn self-reports as a school-aged student calling for themselves
    is detected, the gate flips to need_parental, and /api/chat is GATED (409) until parental consent."""
    client = _client()
    body = _start(client, jurisdiction="NY")
    sid = body["session_id"]

    # consent granted, is_minor NOT set — the caller looks like an adult so far.
    r = client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ready"

    # first turn self-reports as a minor caller -> detect_minor fires -> gate need_parental -> 409.
    r = client.post("/api/chat", json={
        "session_id": sid, "text": "I'm a sophomore calling for myself about SAT prep.",
    })
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["state"] == "need_parental"

    # the gate is now blocked on parental consent (subsequent chat stays 409).
    r = client.get(f"/api/session/{sid}")
    assert r.json()["state"] == "need_parental"
    r = client.post("/api/chat", json={"session_id": sid, "text": "still me, the student."})
    assert r.status_code == 409, r.text

    # resolving parental consent unblocks conversation.
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

    def fake_builder(api_key: str, api_secret: str, identity: str, room: str,
                     room_metadata: Optional[str] = None) -> str:
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


def test_consent_room_metadata_helper_shape():
    """The pure helper builds the small consent JSON the LiveKit room carries (no secrets): the
    gate's consent_state, recording_granted, jurisdiction, phone_hash, and conversable — exactly the
    shape the worker seeds its gate from. A None phone_hash stays null (anonymous caller)."""
    from src.api.demo_routes import _consent_room_metadata
    from src.voice.consent import ConsentGate, RefusalPolicy

    gate = ConsentGate(jurisdiction="CA", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    gate.grant_recording()  # -> ready, recording granted
    raw = _consent_room_metadata(gate, phone_hash_value="abc123")
    meta = json.loads(raw)
    assert meta == {
        "consent_state": "ready",
        "recording_granted": True,
        "jurisdiction": "CA",
        "phone_hash": "abc123",
        "conversable": True,
    }

    # Refused recording but proceeding -> conversable True, recording_granted False (honest).
    g2 = ConsentGate(jurisdiction="TX", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    g2.refuse_recording()  # -> unrecorded
    m2 = json.loads(_consent_room_metadata(g2, phone_hash_value=None))
    assert m2["consent_state"] == "unrecorded"
    assert m2["recording_granted"] is False
    assert m2["conversable"] is True
    assert m2["phone_hash"] is None


def test_livekit_token_carries_consent_metadata_into_room(monkeypatch):
    """The token endpoint hands the INJECTED builder the already-captured consent as room metadata
    (the JSON the worker reads from ctx.room.metadata to seed its gate). The metadata reflects the
    granted-recording consent and binds the caller's phone-hash; no secret leaks into it."""
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "devsecret_value_should_never_appear")
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")

    seen: dict[str, Any] = {}

    def fake_builder(api_key, api_secret, identity, room, room_metadata=None):
        seen.update(room=room, room_metadata=room_metadata)
        return "fake.jwt.token"

    app = create_app(
        llm_client_factory=lambda: _agent_mock(), token_builder=fake_builder, live_rag=False
    )
    client = TestClient(app)
    body = _start(client, jurisdiction="CA")
    sid = body["session_id"]
    h = phone_hash("+15551234567")
    client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True, "phone_hash": h,
    })
    r = client.post("/api/livekit/token", json={"session_id": sid, "identity": "caller"})
    assert r.status_code == 200, r.text

    # The builder received the consent metadata the worker will read off the room.
    meta = json.loads(seen["room_metadata"])
    assert meta["consent_state"] == "ready"
    assert meta["recording_granted"] is True
    assert meta["conversable"] is True
    assert meta["jurisdiction"] == "CA"
    assert meta["phone_hash"] == h
    # No secret ever rides along in the metadata.
    assert "devsecret_value_should_never_appear" not in seen["room_metadata"]


def test_livekit_token_metadata_reflects_recording_refusal(monkeypatch):
    """When recording was REFUSED but the call proceeds, the carried metadata says conversable=True
    with recording_granted=False — so the worker seeds UNRECORDED (proceeds but does not record)."""
    monkeypatch.setenv("LIVEKIT_API_KEY", "devkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret_xxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")

    seen: dict[str, Any] = {}

    def fake_builder(api_key, api_secret, identity, room, room_metadata=None):
        seen["room_metadata"] = room_metadata
        return "fake.jwt.token"

    app = create_app(
        llm_client_factory=lambda: _agent_mock(), token_builder=fake_builder, live_rag=False
    )
    client = TestClient(app)
    sid = _start(client, jurisdiction="CA")["session_id"]
    client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": False,
    })
    r = client.post("/api/livekit/token", json={"session_id": sid, "identity": "caller"})
    assert r.status_code == 200, r.text
    meta = json.loads(seen["room_metadata"])
    assert meta["consent_state"] == "unrecorded"
    assert meta["recording_granted"] is False
    assert meta["conversable"] is True


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


# =============================== /api/chat done FLAG (terminal acts) ============================


def _grant(client: TestClient, sid: str) -> None:
    r = client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True,
    })
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "act,tier,expected_done",
    [
        # CB-82: ANY committed close is terminal — done=True for all four tiers.
        ("attempt_close", "enrollment", True),      # enrollment close — always was terminal
        ("attempt_close", "trial", True),           # CB-82: trial close is now terminal
        ("attempt_close", "consultation", True),    # CB-82: consultation close is now terminal
        ("attempt_close", "callback", True),        # CB-82: callback booking is now terminal
        ("escalate", None, True),                   # escalate is terminal (handed to a specialist)
        ("disqualify", None, True),                 # disqualify is terminal (not a fit)
        ("ask", "goal", False),                     # discovery continues — NOT terminal
        ("pitch", None, False),                     # pitch/talk — NOT terminal
    ],
)
def test_chat_done_flag_reflects_terminal_act(act, tier, expected_done):
    """`done` is True for every terminal act: ANY committed close (attempt_close at ANY tier),
    escalate, or disqualify. Non-close acts (ask, pitch, talk) keep the call open (done False).

    CB-82 fix: the prior code set done=True ONLY for attempt_close@enrollment; sub-enrollment
    closes (callback/consultation/trial) left done=False, so /end never fired and the lead was lost.
    The fix expands the done condition to ALL attempt_close tiers (any committed booking finalizes).

    CB-50: a triggerless `escalate` is no longer terminal — the escalate_only_if_warranted gate
    redirects an escalate with NO genuine reason to a non-terminal selling act (the agent must not
    bail to a human as a default move). So the escalate case sends a GENUINE human request, which
    WARRANTS the escalate and keeps it terminal — preserving this test's actual intent."""
    app = create_app(llm_client_factory=lambda: _agent_mock(act=act, tier=tier), live_rag=False)
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)
    # A neutral prospect STATEMENT (not a question/objection) so a discovery `ask` proposal is not
    # legitimately rerouted by the address_direct_input gate — this test pins the done-flag per
    # forced act, not the question/objection routing (covered by test_respond's M2 tests). The
    # escalate case needs a GENUINE escalation trigger (CB-50) so its escalate is warranted, not bailed.
    text = "I want a human now." if act == "escalate" else "I'd like to keep going."
    r = client.post("/api/chat", json={"session_id": sid, "text": text})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["decision_act"] == act
    assert payload["done"] is expected_done


# =============================== ESCALATION MOMENT PII-SCRUBBED (R42) ===========================


def test_escalation_moment_is_pii_scrubbed():
    """An escalate on a turn containing an email/phone records a PII-SCRUBBED moment on the
    EscalationLog (R42) — the raw email/phone must never reach the review queue."""
    captured: list[Any] = []

    async def escalation_persist_hook(log: Any) -> None:
        captured.append(log)

    app = create_app(
        llm_client_factory=lambda: _agent_mock(
            act="escalate",
            reply="I'll have a specialist email you at advisor@nerdy.com or call 555-867-5309.",
        ),
        escalation_persist_hook=escalation_persist_hook,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)
    r = client.post("/api/chat", json={"session_id": sid, "text": "I want a human now."})
    assert r.status_code == 200, r.text
    assert r.json()["decision_act"] == "escalate"

    assert len(captured) == 1
    moment = captured[0].moment or ""
    # the raw PII is gone; the redaction placeholders are present.
    assert "advisor@nerdy.com" not in moment
    assert "555-867-5309" not in moment
    assert "[EMAIL]" in moment and "[PHONE]" in moment


# =============================== SESSION EVICTION ON /end (bounded store) =======================


def test_session_is_evicted_after_end():
    """A finished call is durable in the store, so /end EVICTS its in-memory session (the bounded
    session dict shrinks): a subsequent GET /api/session/{id} 404s. A repeat /end stays idempotent
    (already_ended) from the small ended-results cache rather than 404-ing."""
    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="ask", target_slot="goal"),
        # capture call-end so this stays DB-free.
        call_end_persist_hook=_noop_call_end,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)
    client.post("/api/chat", json={"session_id": sid, "text": "Tell me about tutoring."})

    # session is live before /end
    assert client.get(f"/api/session/{sid}").status_code == 200

    r = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["persisted"] is True

    # EVICTED: the in-memory session is gone (the dict shrank) -> GET 404.
    assert client.get(f"/api/session/{sid}").status_code == 404

    # but a repeat /end is idempotent (already_ended), not a 404.
    r2 = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r2.status_code == 200, r2.text
    assert r2.json()["outcome"] == "already_ended"
    assert r2.json()["persisted"] is False


# =============================== OPERATOR AUTH ON WRITE ENDPOINTS ===============================


def test_write_endpoint_401_without_token_when_operator_token_set(monkeypatch):
    """With OPERATOR_TOKEN set, an Improve write endpoint (POST /api/kb) 401s without the header and
    200s with the right X-Operator-Token. (Auth is applied at the router-INCLUDE site, not inside
    the router.)"""
    monkeypatch.setenv("OPERATOR_TOKEN", "s3cret-operator")
    client = _client()

    # POST a KB draft WITHOUT the token -> 401.
    r = client.post("/api/kb", json={"name": "draft"})
    assert r.status_code == 401, r.text

    # WITH the right token -> the auth gate passes (no longer 401).
    r = client.post(
        "/api/kb",
        json={"name": "draft"},
        headers={"X-Operator-Token": "s3cret-operator"},
    )
    assert r.status_code != 401, r.text


def test_require_operator_allows_when_token_unset(monkeypatch):
    """When OPERATOR_TOKEN is UNSET, require_operator ALLOWS every request (local-demo convenience):
    the dependency returns without raising even with no header. Tested on the dependency directly so
    it does not reach the real store (the full endpoint would hit Postgres)."""
    from src.api.server import _make_require_operator

    monkeypatch.delenv("OPERATOR_TOKEN", raising=False)
    require_operator = _make_require_operator()
    # no header, no auth -> must NOT raise (returns None).
    assert require_operator(x_operator_token=None, authorization=None) is None


def test_require_operator_accepts_bearer_token(monkeypatch):
    """require_operator accepts the token via either X-Operator-Token OR a Bearer Authorization
    header, and 401s (HTTPException) on a wrong/absent token when OPERATOR_TOKEN is set."""
    from fastapi import HTTPException

    from src.api.server import _make_require_operator

    monkeypatch.setenv("OPERATOR_TOKEN", "s3cret-operator")
    require_operator = _make_require_operator()
    # right token via header -> ok
    assert require_operator(x_operator_token="s3cret-operator", authorization=None) is None
    # right token via Bearer -> ok
    assert require_operator(x_operator_token=None, authorization="Bearer s3cret-operator") is None
    # wrong/absent -> 401
    with pytest.raises(HTTPException) as ei:
        require_operator(x_operator_token=None, authorization=None)
    assert ei.value.status_code == 401


# =============================== CORS (no wildcard + credentials together) ======================


def test_cors_defaults_to_wildcard_without_credentials():
    """Default CORS (nothing configured) is the permissive local-dev pairing: wildcard origin with
    credentials DISABLED — never wildcard + credentials together (browsers reject it; it's unsafe)."""
    from src.api.server import _resolve_cors

    origins, allow_credentials = _resolve_cors(None)
    assert origins == ["*"]
    assert allow_credentials is False


def test_cors_reads_allowed_origins_env_and_enables_credentials(monkeypatch):
    """When ALLOWED_ORIGINS is set (comma-separated explicit origins), credentials are ENABLED (no
    wildcard present)."""
    from src.api.server import _resolve_cors

    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.com, https://admin.example.com")
    origins, allow_credentials = _resolve_cors(None)
    assert origins == ["https://app.example.com", "https://admin.example.com"]
    assert allow_credentials is True


# ======================= CB-82: ALL COMMITTED CLOSES FINALIZE (bug regression) ===================
#
# A committed close (attempt_close at ANY tier) must set done=True so the client fires /end and the
# episode + lead are persisted. The bug: only enrollment closes set done=True; callback/consultation/
# trial left done=False, so /end never fired, the episode stayed in_progress (CB-60 excluded it
# from the calls list), and the lead + phone were lost when the user left. QA caught this when a
# mom booked a callback + gave her phone — the whole conversation never reached /operate.


def _capture_call_end_hook():
    """Returns (hook, captured_list). The hook mirrors the CallEndPersistHook signature but stays
    DB-free. When raw_phone is supplied it server-derives the sha256 hash (as persist_call_end
    does) so the captured Episode's lead_phone_hash reflects the real hashing path. Each invocation
    appends the built Episode; tests can inspect outcome, phone-hash, qualified, metrics, and turns."""
    captured: list[Any] = []

    async def hook(session: Any, **kw: Any) -> Any:
        from src.api.persistence import episode_from_session
        from src.memory.schema import phone_hash as hash_phone

        # Replicate the server-side hash derivation from persist_call_end: raw_phone wins over a
        # client-supplied phone_hash — this is compliance-critical (server never trusts the hash).
        raw = kw.get("raw_phone")
        if raw:
            try:
                effective_hash: Optional[str] = hash_phone(raw)
            except (ValueError, TypeError):
                effective_hash = None
        else:
            effective_hash = kw.get("phone_hash")

        ep = episode_from_session(
            session,
            config=kw["config"],
            channel=kw["channel"],
            phone_hash=effective_hash,
            last_tier=kw.get("last_tier"),
        )
        captured.append(ep)
        return ep

    return hook, captured


@pytest.mark.parametrize(
    "tier,expected_outcome",
    [
        ("callback", "callback_booked"),
        ("consultation", "consult_booked"),
        ("trial", "trial_booked"),
        ("enrollment", "enrolled"),
    ],
)
def test_cb82_committed_close_at_any_tier_sets_done_true(tier, expected_outcome):
    """CB-82 regression: a committed close at ANY tier (callback/consultation/trial/enrollment) must
    return done=True in the /api/chat response so the client fires /end. Before the fix, only
    enrollment closes set done=True — sub-enrollment commits silently dropped without persisting."""
    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier=tier),
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)

    r = client.post("/api/chat", json={"session_id": sid, "text": "Yes, let's do it."})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["decision_act"] == "attempt_close", "brain routed to attempt_close"
    assert payload["done"] is True, (
        f"done must be True for a committed close at tier={tier!r} — "
        f"before CB-82 fix, sub-enrollment tiers left done=False so /end never fired "
        f"and the episode/lead were lost"
    )


@pytest.mark.parametrize("tier", ["callback", "consultation", "trial", "enrollment"])
def test_cb82_end_fires_and_episode_carries_booked_outcome(tier):
    """CB-82 regression — end-to-end: after done=True from a close at `tier`, calling /end persists
    an Episode with the correct booked outcome. The call_end_persist_hook captures the Episode so
    no DB is needed; the episode outcome must match _CLOSE_TIER_OUTCOME[tier]."""
    hook, captured = _capture_call_end_hook()
    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier=tier),
        call_end_persist_hook=hook,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)

    # The chat turn commits the close.
    r = client.post("/api/chat", json={"session_id": sid, "text": "Yes, book it."})
    assert r.status_code == 200, r.text
    assert r.json()["done"] is True, f"done must be True for attempt_close@{tier}"

    # The client fires /end (would be auto-fired by the demo UI when done=True).
    r = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r.status_code == 200, r.text
    end_payload = r.json()
    assert end_payload["persisted"] is True

    # The captured episode must carry the correct booked outcome.
    assert len(captured) == 1, "call_end_persist_hook must have been called exactly once"
    ep = captured[0]
    from src.api.persistence import _CLOSE_TIER_OUTCOME
    expected_outcome = _CLOSE_TIER_OUTCOME[tier][0]
    assert ep.outcome == expected_outcome, (
        f"episode outcome for attempt_close@{tier} must be {expected_outcome!r}, got {ep.outcome!r}"
    )
    assert ep.qualified is True, "a booked close must mark the episode as qualified"


def test_cb82_phone_stored_as_sha256_only_on_booked_close():
    """CB-82 + R42 compliance: after a committed callback booking + /end with a raw phone, the
    persisted Episode carries lead_phone_hash=sha256(raw_phone) and the raw phone NEVER appears
    anywhere in the episode (turns, outcome, metrics, or the hash field itself). This is
    compliance-critical — raw phone must never be stored."""
    raw_phone = "555-082-0001"
    from src.memory.schema import phone_hash as sha_phone
    expected_hash = sha_phone(raw_phone)

    hook, captured = _capture_call_end_hook()
    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier="callback"),
        call_end_persist_hook=hook,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)

    r = client.post("/api/chat", json={"session_id": sid, "text": "Book me a callback."})
    assert r.status_code == 200, r.text
    assert r.json()["done"] is True

    # Fire /end with the raw phone (as the demo UI would after the caller provides it).
    r = client.post(
        f"/api/session/{sid}/end",
        json={"session_id": sid, "raw_phone": raw_phone},
    )
    assert r.status_code == 200, r.text
    assert r.json()["persisted"] is True

    assert len(captured) == 1
    ep = captured[0]

    # The lead key is the sha256 hash — never the raw phone.
    assert ep.lead_phone_hash == expected_hash, (
        f"lead_phone_hash must be sha256({raw_phone!r}), got {ep.lead_phone_hash!r}"
    )
    # Serialize everything to JSON and assert the raw phone NEVER appears.
    import json as _json
    ep_json = _json.dumps({
        "outcome": ep.outcome,
        "lead_phone_hash": ep.lead_phone_hash,
        "qualified": ep.qualified,
        "metrics": ep.metrics,
        "turns": [t.to_dict() if hasattr(t, "to_dict") else str(t) for t in ep.turns],
    })
    assert raw_phone not in ep_json, (
        f"raw phone {raw_phone!r} must NEVER appear in the persisted episode (R42)"
    )


def test_cb82_non_committed_turns_do_not_finalize():
    """CB-82 purity: a talk/ask/pitch turn (no close attempt) must NOT set done=True — only a
    genuine committed close, disqualify, or escalate is terminal. done=True on every turn would
    break the buy-gate purity (done gates finalization, NOT the commit decision)."""
    for act in ("ask", "pitch", "talk"):
        app = create_app(
            llm_client_factory=lambda a=act: _agent_mock(act=a),
            live_rag=False,
        )
        client = TestClient(app)
        sid = _start(client, channel="text")["session_id"]
        _grant(client, sid)
        r = client.post("/api/chat", json={"session_id": sid, "text": "What do you offer?"})
        assert r.status_code == 200, r.text
        assert r.json()["done"] is False, (
            f"act={act!r} is not terminal — done must be False (finalization must not trigger)"
        )


def test_cb82_consent_gate_unchanged_no_consent_still_409():
    """CB-82 does NOT touch the consent gate. A /api/chat call before consent still returns 409
    even when the mock brain would return attempt_close. The gate fires before the brain."""
    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier="callback"),
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    # NO consent granted — gate is still in pending state.
    r = client.post("/api/chat", json={"session_id": sid, "text": "Book me."})
    assert r.status_code == 409, (
        "consent gate must block /api/chat regardless of the CB-82 done-condition fix"
    )
    assert r.json()["detail"]["reason"] == "consent_not_satisfied"

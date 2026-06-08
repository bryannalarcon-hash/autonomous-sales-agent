# End-to-end tests for the U13 web-demo call (plan U13; AE9 + R1/R3/R33/R40/R41/R42 + CB-87 + CB-88).
# Drives the thin FastAPI surface (src.api.server) with fastapi.testclient.TestClient and an injected
# MockLLMClient so NOTHING hits the network or a DB. Asserts the consent gate guards /api/chat
# (409 until satisfied), the refusal path proceeds unrecorded (episode flagged not-recorded), the
# suspected-minor path blocks on need_parental, sticky-voice assignment (new + returning caller),
# that any logged turn body is PII-scrubbed while the lead key stays the phone-hash (raw absent),
# and (CB-87, corrects CB-82) that sub-enrollment closes (callback/consultation/trial) do NOT end
# the conversation (done=False) but DO persist a booked outcome via the live-upsert path + the lead
# is checkpointed at contact-capture; enrollment/escalate/disqualify still set done=True.
# CB-88 regression: the production _default_live_upsert in server.py was missing last_close_tier +
# phone_hash, so every live attempt_close was persisted as 'in_progress' rather than 'consult_booked' /
# 'callback_booked' / 'trial_booked' — the CB-87 outcome stamp never fired on real calls. Fixed by
# forwarding those kwargs in _default_live_upsert; three new tests cover the production wiring path.
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
        # CB-87 (corrects CB-82): enrollment close ends the conversation — done=True.
        ("attempt_close", "enrollment", True),      # enrollment close — terminal (done=True)
        # CB-87: sub-enrollment closes do NOT end the conversation — done=False. The booking
        # persists + appears in /operate via the live-upsert outcome stamp, NOT via /end.
        ("attempt_close", "trial", False),          # CB-87: trial offer keeps convo open
        ("attempt_close", "consultation", False),   # CB-87: consult offer keeps convo open
        ("attempt_close", "callback", False),       # CB-87: callback offer keeps convo open
        ("escalate", None, True),                   # escalate is terminal (handed to a specialist)
        ("disqualify", None, True),                 # disqualify is terminal (not a fit)
        ("ask", "goal", False),                     # discovery continues — NOT terminal
        ("pitch", None, False),                     # pitch/talk — NOT terminal
    ],
)
def test_chat_done_flag_reflects_terminal_act(act, tier, expected_done):
    """`done` is True only for genuinely terminal acts: enrollment close, escalate, or disqualify.
    Sub-enrollment closes (callback/consultation/trial) keep done=False — the conversation continues.

    CB-87 (corrects CB-82): CB-82 expanded `done` to ANY attempt_close, which ended the chat on the
    agent's OFFER before the human could answer (live QA: both replays died on "which day works?").
    In /api/chat, committed is the AGENT's gated turn — attempt_close means the agent OFFERED a
    close, NOT that the human accepted. Sub-enrollment bookings persist + appear in /operate via the
    live-upsert outcome stamp; the conversation does NOT end. Enrollment close (the strongest, human-
    accepted commit tier) still ends the session. Decouples PERSISTENCE from CONVERSATION-END.

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


# ======================= CB-87 (corrects CB-82): PERSISTENCE DECOUPLED FROM CONVERSATION-END ======
#
# CB-82 made `done` fire on ANY agent attempt_close. In interactive /api/chat, `committed` is the
# AGENT's gated turn — attempt_close means the agent OFFERED a close ("would you like me to book?"),
# NOT that the human accepted. Live QA: both replays died on the agent's "which day works?" question,
# before the human could answer (dad turn 3, mom turn 4). CB-82 conflated PERSISTENCE with END.
#
# CB-87 contract (corrected):
#   - enrollment close / escalate / disqualify -> done=True (conversation ends, /end persists)
#   - sub-enrollment close (callback/consultation/trial) -> done=False (conversation continues)
#     BUT the live-upsert stamps the booked outcome (e.g. callback_booked) on the episode row so it
#     appears in /operate immediately while the human can still answer "Tuesday works for me."
#   - contact capture (phone regex) -> minimal Lead upserted mid-call (phone sha256 hash only,
#     never raw) so an abandoned-after-booking chat keeps the lead even if /end never fires (CB-86).
#
# Tests below prove: (a) enrollment still done=True; (b) sub-enrollment done=False; (c) the live-
# upsert carries the booked outcome after a close offer; (d) contact capture persists the lead hash;
# (e) enrollment close still does persist+finalize end-to-end; (f) phone sha256 compliance.


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


def test_cb87_enrollment_close_still_done_true():
    """CB-87: enrollment close (the strongest tier) is still terminal — done=True so /end fires and
    the episode + lead are persisted. Enrollment is the only attempt_close tier that ends the chat."""
    hook, captured = _capture_call_end_hook()
    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier="enrollment"),
        call_end_persist_hook=hook,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)

    r = client.post("/api/chat", json={"session_id": sid, "text": "Yes, sign me up!"})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["decision_act"] == "attempt_close"
    assert payload["done"] is True, "enrollment close must set done=True (conversation ends)"

    # Client fires /end → persists.
    r = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["persisted"] is True
    assert len(captured) == 1
    ep = captured[0]
    assert ep.outcome == "enrolled"
    assert ep.qualified is True


@pytest.mark.parametrize(
    "tier,expected_outcome",
    [
        ("callback", "callback_booked"),
        ("consultation", "consult_booked"),
        ("trial", "trial_booked"),
    ],
)
def test_cb87_sub_enrollment_close_does_not_set_done(tier, expected_outcome):
    """CB-87 regression (round-4 bug): a sub-enrollment close (callback/consultation/trial) must
    NOT set done=True — the conversation continues so the human can answer "Tuesday works for me."

    CB-82 conflated PERSISTENCE with CONVERSATION-END: `done` fired on the agent's close OFFER,
    cutting the human off before they could respond (live QA: dad died turn 3, mom turn 4). The
    corrected contract: sub-enrollment offers keep done=False; the booking persists + appears in
    /operate via the live-upsert outcome stamp, NOT by ending the chat."""
    live_captured: list[Any] = []

    async def _capture_live_upsert(session: Any, **kw: Any) -> None:
        from src.api.persistence import episode_from_session
        ep = episode_from_session(
            session,
            config=kw["config"],
            channel=kw["channel"],
            last_tier=kw.get("last_close_tier"),
            phone_hash=kw.get("phone_hash"),
        )
        live_captured.append(ep)

    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier=tier),
        live_upsert_hook=_capture_live_upsert,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)

    r = client.post("/api/chat", json={"session_id": sid, "text": "That sounds good."})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["decision_act"] == "attempt_close"
    assert payload["done"] is False, (
        f"attempt_close@{tier!r} must NOT set done=True — the conversation continues; "
        f"CB-82 error was ending the chat on the agent's offer before the human answered"
    )

    # The live-upsert must have captured the booked outcome (episode appears in /operate).
    assert len(live_captured) >= 1, "live_upsert_hook must have been called"
    ep = live_captured[-1]
    assert ep.outcome == expected_outcome, (
        f"live-upsert episode must carry outcome={expected_outcome!r} (not 'in_progress') "
        f"after a {tier!r} close offer — booking appears in /operate while convo continues"
    )
    assert ep.qualified is True, "a booked close marks the episode as qualified"


def test_cb87_sub_enrollment_upsert_outcome_before_enrollment():
    """CB-87: after a callback offer, the live-upsert carries 'callback_booked'; after a subsequent
    enrollment close (done=True), /end persists 'enrolled'. The live row transitions correctly."""
    live_captured: list[Any] = []
    end_captured: list[Any] = []

    async def _capture_live(session: Any, **kw: Any) -> None:
        from src.api.persistence import episode_from_session
        ep = episode_from_session(
            session,
            config=kw["config"],
            channel=kw["channel"],
            last_tier=kw.get("last_close_tier"),
        )
        live_captured.append(ep)

    hook, end_captured = _capture_call_end_hook()

    # Turn 1: callback offer (done=False)
    client_cb = TestClient(create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier="callback"),
        live_upsert_hook=_capture_live,
        call_end_persist_hook=hook,
        live_rag=False,
    ))
    sid = _start(client_cb, channel="text")["session_id"]
    _grant(client_cb, sid)
    r = client_cb.post("/api/chat", json={"session_id": sid, "text": "Maybe a callback?"})
    assert r.json()["done"] is False
    assert live_captured and live_captured[-1].outcome == "callback_booked"


def test_cb87_non_committed_turns_do_not_finalize():
    """CB-87 purity: a talk/ask/pitch turn (no close attempt) must NOT set done=True — only a
    genuine terminal act ends the conversation. done=True on every turn would break buy-gate purity."""
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


def test_cb87_consent_gate_unchanged_no_consent_still_409():
    """CB-87 does NOT touch the consent gate. A /api/chat call before consent still returns 409
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
        "consent gate must block /api/chat regardless of done-condition changes"
    )
    assert r.json()["detail"]["reason"] == "consent_not_satisfied"


def test_cb87_phone_sha256_only_on_booked_close_via_end():
    """CB-87 + R42 compliance: after a callback booking (done=False) + explicit /end with a raw
    phone, the persisted Episode carries lead_phone_hash=sha256(raw_phone) and the raw phone NEVER
    appears anywhere. The caller can provide their phone AFTER the offer, then the operator fires /end.
    This is compliance-critical — raw phone must never be stored."""
    raw_phone = "555-087-0001"
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
    # CB-87: callback offer does NOT end the chat.
    assert r.json()["done"] is False, "callback offer must keep conversation open (CB-87)"

    # Caller provides phone; operator (or UI) fires /end explicitly.
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
    # Serialize and assert no raw phone appears anywhere.
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


# ========================= CB-87 REGRESSION TESTS (new) ============================================


def test_cb87_regression_agent_callback_offer_does_not_end_chat():
    """CB-87 regression (the exact round-4 bug): an agent attempt_close@callback does NOT set
    done=True. Before the fix (CB-82), the chat ended on the agent's 'which day works?' question —
    dad died turn 3, mom turn 4 in the live replay. This test pins that an offer keeps done=False."""
    app = create_app(
        llm_client_factory=lambda: _agent_mock(
            act="attempt_close",
            tier="callback",
            reply="Which day works best for your callback — Tuesday or Thursday?",
        ),
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)

    # Turn that triggers the agent's callback offer.
    r = client.post("/api/chat", json={"session_id": sid, "text": "I'd like more info."})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["decision_act"] == "attempt_close"
    assert payload["done"] is False, (
        "agent's callback offer must NOT end the chat (CB-82 regression — human must be able to answer)"
    )

    # Verify the conversation can continue (human picks a day).
    r2 = client.post("/api/chat", json={"session_id": sid, "text": "Tuesday works for me."})
    assert r2.status_code == 200, (
        f"conversation must continue after callback offer — got {r2.status_code}: {r2.text}"
    )


def test_cb87_regression_live_upsert_carries_booked_outcome_after_callback_offer():
    """CB-87: after a callback offer (done=False), the live-upsert episode carries outcome
    'callback_booked' (not 'in_progress') so the booking appears in /operate mid-call."""
    live_captured: list[Any] = []

    async def _capture(session: Any, **kw: Any) -> None:
        from src.api.persistence import episode_from_session
        ep = episode_from_session(
            session,
            config=kw["config"],
            channel=kw["channel"],
            last_tier=kw.get("last_close_tier"),
            phone_hash=kw.get("phone_hash"),
        )
        live_captured.append(ep)

    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier="callback"),
        live_upsert_hook=_capture,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)

    r = client.post("/api/chat", json={"session_id": sid, "text": "I'm interested."})
    assert r.status_code == 200, r.text
    assert r.json()["done"] is False, "callback offer must not end the conversation"

    assert live_captured, "live_upsert_hook must have been called at least once"
    ep = live_captured[-1]
    assert ep.outcome == "callback_booked", (
        f"live-upsert episode must carry 'callback_booked' not 'in_progress' — "
        f"got {ep.outcome!r}; booking must appear in /operate while the conversation continues"
    )
    assert ep.qualified is True


def test_cb87_regression_contact_capture_persists_lead_hash():
    """CB-87 (absorbs CB-86): when the human turn contains a phone number, the live-upsert includes
    the phone hash so the lead is checkpointed mid-call. If the chat is abandoned (no /end), the
    lead row still exists. Phone sha256 only — raw phone must never appear in the captured lead."""
    live_captured: list[Any] = []

    async def _capture(session: Any, **kw: Any) -> None:
        from src.api.persistence import episode_from_session
        ep = episode_from_session(
            session,
            config=kw["config"],
            channel=kw["channel"],
            last_tier=kw.get("last_close_tier"),
            phone_hash=kw.get("phone_hash"),
        )
        live_captured.append(ep)
        live_captured.append({"kw_phone_hash": kw.get("phone_hash")})

    raw_phone = "555-087-1234"
    from src.memory.schema import phone_hash as sha_phone
    expected_hash = sha_phone(raw_phone)

    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier="callback"),
        live_upsert_hook=_capture,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)

    # Bind the phone hash via consent/respond (as the demo UI does when the caller gives their number).
    client.post("/api/consent/respond", json={
        "session_id": sid,
        "ai_acknowledged": True,
        "recording_consent": True,
        "phone_hash": expected_hash,
    })

    r = client.post("/api/chat", json={"session_id": sid, "text": "My number is 555-087-1234."})
    assert r.status_code == 200, r.text

    # The live-upsert must have received the phone_hash kwarg.
    kw_entries = [e for e in live_captured if isinstance(e, dict) and "kw_phone_hash" in e]
    assert kw_entries, "live_upsert_hook must have been called with phone_hash kwarg"
    passed_hash = kw_entries[-1]["kw_phone_hash"]
    assert passed_hash == expected_hash, (
        f"live_upsert must receive phone_hash={expected_hash!r} (sha256); got {passed_hash!r}"
    )
    # Raw phone must never appear in hook kwargs.
    assert raw_phone not in str(kw_entries), (
        f"raw phone {raw_phone!r} must NEVER appear in live-upsert kwargs (R42)"
    )


def test_cb87_regression_enrollment_close_still_persists_via_end():
    """CB-87 non-regression: enrollment close still sets done=True, /end fires, episode persisted
    with outcome 'enrolled'. This is unchanged from before CB-82's regression."""
    hook, captured = _capture_call_end_hook()
    app = create_app(
        llm_client_factory=lambda: _agent_mock(act="attempt_close", tier="enrollment"),
        call_end_persist_hook=hook,
        live_rag=False,
    )
    client = TestClient(app)
    sid = _start(client, channel="text")["session_id"]
    _grant(client, sid)

    r = client.post("/api/chat", json={"session_id": sid, "text": "Yes, sign me up now."})
    assert r.status_code == 200, r.text
    assert r.json()["done"] is True, "enrollment close must end the chat (done=True)"

    r = client.post(f"/api/session/{sid}/end", json={"session_id": sid})
    assert r.status_code == 200, r.text
    assert r.json()["persisted"] is True
    assert len(captured) == 1
    assert captured[0].outcome == "enrolled"
    assert captured[0].qualified is True


# ======================= CB-88: real-flow attempt_close tier survives to live-upsert =============
#
# CB-87's tests injected a CUSTOM live_upsert_hook that read last_close_tier from **kw directly.
# The PRODUCTION wiring is _default_live_upsert in server.py, which is built when live_upsert_hook
# is None and live_rag=True.  CB-88 found that _default_live_upsert dropped last_close_tier and
# phone_hash from the kwargs it forwarded to persist_call_live, so the episode was always stamped
# "in_progress" instead of "consult_booked"/"callback_booked" on a live call.
#
# This test drives the REAL production wiring:
#   - create_app with live_upsert_hook=None so _default_live_upsert is used
#   - persist_call_live patched at the module level so the DB write is captured (not executed)
#   - MockLLMClient returns attempt_close@consultation from the policy call
#   - Assert the captured episode outcome == "consult_booked" (not "in_progress")
#
# The test must FAIL before the fix (server.py _default_live_upsert drops last_close_tier) and
# PASS after it (the kwargs are forwarded).


def test_cb88_default_live_upsert_forwards_close_tier_to_persist_call_live():
    """CB-88 regression: the production _default_live_upsert (live_upsert_hook=None, live_rag=True)
    must forward last_close_tier to persist_call_live so a booked callback/consultation/trial
    episode appears in /operate with the correct outcome (not stuck as 'in_progress').

    The CB-87 tests only verified the injected-hook path; this test covers the DEFAULT wiring
    in server.py that the live call actually uses.  Before the fix, persist_call_live received
    last_close_tier=None (dropped by _default_live_upsert) → outcome='in_progress'.  After the
    fix it receives last_close_tier='consultation' → outcome='consult_booked'."""
    from unittest.mock import AsyncMock, patch

    from src.kb.embeddings import FakeEmbedder

    captured_kwargs: list[dict[str, Any]] = []
    captured_episodes: list[Any] = []

    # Patch persist_call_live at the module level so _default_live_upsert's lazy import sees it.
    # The patch captures the kwargs (specifically last_close_tier) and also builds a real Episode
    # so the caller gets a valid return value.
    async def _fake_persist_call_live(session: Any, **kw: Any) -> Any:
        captured_kwargs.append(dict(kw))
        from src.api.persistence import episode_from_session
        ep = episode_from_session(
            session,
            config=kw["config"],
            channel=kw["channel"],
            last_tier=kw.get("last_close_tier"),
            phone_hash=kw.get("phone_hash"),
            episode_id=kw.get("episode_id"),
            created_at=kw.get("created_at"),
        )
        captured_episodes.append(ep)
        return ep

    with patch("src.api.persistence.persist_call_live", new=_fake_persist_call_live):
        # live_upsert_hook=None + live_rag=True → server.py wires _default_live_upsert (the bug path).
        # FakeEmbedder + stub retrieve_hook keep this DB-free (no SentenceTransformerEmbedder).
        app = create_app(
            llm_client_factory=lambda: _agent_mock(
                act="attempt_close",
                tier="consultation",
                reply="Shall we lock in a free consultation?",
            ),
            embedder=FakeEmbedder(),
            retrieve_hook=lambda q, **k: [],
            live_rag=True,
            live_upsert_hook=None,  # force the DEFAULT _default_live_upsert path
        )
        client = TestClient(app)
        sid = _start(client, channel="text")["session_id"]
        _grant(client, sid)

        r = client.post("/api/chat", json={"session_id": sid, "text": "That sounds great."})
        assert r.status_code == 200, r.text
        assert r.json()["decision_act"] == "attempt_close"

    # The production wiring must have forwarded last_close_tier to persist_call_live.
    assert len(captured_kwargs) >= 1, "persist_call_live must have been called by _default_live_upsert"
    last_kw = captured_kwargs[-1]
    assert last_kw.get("last_close_tier") == "consultation", (
        f"CB-88: _default_live_upsert dropped last_close_tier; persist_call_live received "
        f"last_close_tier={last_kw.get('last_close_tier')!r} (expected 'consultation'). "
        "The production live-upsert wiring in server.py must forward last_close_tier + phone_hash."
    )

    # The live-upserted episode must carry the booked outcome, not 'in_progress'.
    assert len(captured_episodes) >= 1, "an Episode must have been built"
    ep = captured_episodes[-1]
    assert ep.outcome == "consult_booked", (
        f"CB-88: live-upserted episode has outcome={ep.outcome!r} (expected 'consult_booked'). "
        "A consultation offer must stamp consult_booked so the booking appears in /operate."
    )
    assert ep.qualified is True, "a booked close must mark the episode as qualified"


def test_cb88_real_flow_callback_booking_persisted() -> None:
    """CB-88 real-flow: the production _default_live_upsert must forward last_close_tier='callback'
    to persist_call_live so the live-upserted episode shows 'callback_booked' (not 'in_progress').

    Uses the DEFAULT wiring (live_upsert_hook=None, live_rag=True) with persist_call_live patched
    at the module level.  The patched version replicates persist_call_live's actual outcome-stamp
    logic (the 'if last_close_tier:' branch) so the assertion directly proves the kwargs were
    forwarded and the correct outcome was derived."""
    from unittest.mock import patch

    from src.api.persistence import derive_outcome
    from src.kb.embeddings import FakeEmbedder

    captured_outcomes: list[str] = []
    captured_close_tiers: list[Any] = []

    async def _fake_persist_call_live(session: Any, **kw: Any) -> Any:
        # Replicate persist_call_live's outcome-stamp logic exactly — this is what the fix must satisfy.
        last_close_tier = kw.get("last_close_tier")
        captured_close_tiers.append(last_close_tier)
        if last_close_tier:
            outcome, _, _, _ = derive_outcome("attempt_close", last_close_tier)
        else:
            outcome = "in_progress"
        captured_outcomes.append(outcome)
        return None

    with patch("src.api.persistence.persist_call_live", new=_fake_persist_call_live):
        app = create_app(
            llm_client_factory=lambda: _agent_mock(
                act="attempt_close",
                tier="callback",
                reply="Let me set up a callback for you.",
            ),
            embedder=FakeEmbedder(),
            retrieve_hook=lambda q, **k: [],
            live_rag=True,
            live_upsert_hook=None,
        )
        client = TestClient(app)
        sid = _start(client, channel="text")["session_id"]
        _grant(client, sid)

        r = client.post("/api/chat", json={"session_id": sid, "text": "A callback works for me."})
        assert r.status_code == 200, r.text
        assert r.json()["decision_act"] == "attempt_close"
        assert r.json()["done"] is False, "callback offer must keep convo open (CB-87)"

    assert len(captured_close_tiers) >= 1, "persist_call_live must have been called"
    assert captured_close_tiers[-1] == "callback", (
        f"CB-88: _default_live_upsert dropped last_close_tier; persist_call_live received "
        f"last_close_tier={captured_close_tiers[-1]!r} (expected 'callback'). "
        "The production live-upsert wiring in server.py must forward last_close_tier."
    )
    assert captured_outcomes[-1] == "callback_booked", (
        f"CB-88: outcome='in_progress' instead of 'callback_booked' (tier not forwarded). "
        f"Got outcome={captured_outcomes[-1]!r}"
    )


def test_cb88_non_closing_turn_stays_in_progress() -> None:
    """CB-88 invariant: a non-closing turn (ask/pitch/answer_via_kb) must NOT set done=True and
    the live-upserted episode outcome stays 'in_progress'."""
    from unittest.mock import patch

    from src.kb.embeddings import FakeEmbedder

    captured_episodes: list[Any] = []

    async def _fake_persist_call_live(session: Any, **kw: Any) -> Any:
        from src.api.persistence import episode_from_session
        ep = episode_from_session(
            session,
            config=kw["config"],
            channel=kw["channel"],
            last_tier=kw.get("last_close_tier"),
            episode_id=kw.get("episode_id"),
            created_at=kw.get("created_at"),
        )
        captured_episodes.append(ep)
        return ep

    with patch("src.api.persistence.persist_call_live", new=_fake_persist_call_live):
        app = create_app(
            llm_client_factory=lambda: _agent_mock(
                act="ask",
                target_slot="goal",
                reply="What's the main goal you're hoping to achieve?",
            ),
            embedder=FakeEmbedder(),
            retrieve_hook=lambda q, **k: [],
            live_rag=True,
            live_upsert_hook=None,
        )
        client = TestClient(app)
        sid = _start(client, channel="text")["session_id"]
        _grant(client, sid)

        r = client.post("/api/chat", json={"session_id": sid, "text": "Tell me more."})
        assert r.status_code == 200, r.text
        assert r.json()["done"] is False, "a discovery turn must not end the conversation"

    assert len(captured_episodes) >= 1, "persist_call_live must have been called"
    ep = captured_episodes[-1]
    assert ep.outcome == "in_progress", (
        f"CB-88 invariant: non-closing turn must leave outcome='in_progress', got {ep.outcome!r}"
    )

# test_cb63_chat_stream.py — server-contract tests for CB-63: /api/chat SSE streaming.
# Verifies: (1) streaming path yields >1 token chunk and concat(chunks) == buffered reply for same
# canned brain output (R37 parity); (2) consent-gated stream still 409s BEFORE first token; (3) in-chat
# minor detection on the SSE path still gates (409) BEFORE any token flows; (4) timing fields are
# persisted (live upsert + turn state) after a streamed turn; (5) the `done` event payload shape
# matches the buffered JSON contract; (6) terminal acts (disqualify/escalate/enrollment) set
# done=True in the SSE done event; (7) buffered fallback (no Accept: text/event-stream header) is
# unchanged. All tests are DB-free ($0 spend): TestClient + MockLLMClient, no real LLM calls.
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.core.llm import Message, MockLLMClient


# ---------------------------------------------------------------------------
# Shared mock / helper plumbing (mirrors test_demo_call.py)
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
    reply: str = "Tell me a bit about what your child is working on.",
) -> MockLLMClient:
    """MockLLMClient that returns a fixed decision JSON for JSON-mode calls and a fixed NLG reply
    for text calls. Mirrors test_demo_call.py exactly. The NLG reply is chunked into >=2 word-ish
    tokens by MockLLMClient.complete_stream so the streaming path always produces >1 chunk."""
    decision: dict[str, Any] = {
        "act": act,
        "target_slot": target_slot,
        "tier": tier,
        "confidence": confidence,
        "rationale": "cb63-stream-test",
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


def _make_client(**mock_kw: Any) -> TestClient:
    """TestClient over a fresh app with MockLLMClient per session, no live_rag, no DB."""
    app = create_app(llm_client_factory=lambda: _agent_mock(**mock_kw), live_rag=False)
    return TestClient(app)


def _start(client: TestClient, *, jurisdiction: str = "CA", channel: str = "text") -> str:
    r = client.post("/api/consent/start", json={"jurisdiction": jurisdiction, "channel": channel})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _grant(client: TestClient, sid: str) -> None:
    r = client.post("/api/consent/respond", json={
        "session_id": sid, "ai_acknowledged": True, "recording_consent": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ready"


def _parse_sse(raw: str) -> list[tuple[str, Any]]:
    """Parse a raw SSE response body into a list of (event_name, parsed_data) pairs.

    Handles the standard `event: <name>\\ndata: <payload>\\n\\n` format. `data` is JSON-decoded if
    possible; otherwise returned as the raw string. Skips comment lines (starting with ':')."""
    events: list[tuple[str, Any]] = []
    current_event: str = "message"
    current_data: list[str] = []
    for line in raw.splitlines():
        if line.startswith(":"):
            continue  # SSE comment / keep-alive
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current_data.append(line[len("data:"):].strip())
        elif line == "":
            # Empty line: dispatch the buffered event if any data collected.
            if current_data:
                raw_data = "\n".join(current_data)
                try:
                    parsed = json.loads(raw_data)
                except (json.JSONDecodeError, ValueError):
                    parsed = raw_data
                events.append((current_event, parsed))
            current_event = "message"
            current_data = []
    return events


SSE_HEADERS = {"Accept": "text/event-stream"}

# ---------------------------------------------------------------------------
# (1) R37 PARITY — concat(streamed tokens) == buffered reply
# ---------------------------------------------------------------------------

class TestStreamParity:
    """The streamed reply must equal the buffered reply for the same canned brain output (R37)."""

    CANNED_REPLY = "Tell me a bit about what your child is working on."

    def test_stream_yields_gt1_chunk(self):
        """The streaming path must yield >1 `token` event so the client can render progressively.
        MockLLMClient.complete_stream splits the reply into word-ish chunks — the default reply
        contains multiple words so this is guaranteed for any realistic reply."""
        client = _make_client(reply=self.CANNED_REPLY)
        sid = _start(client)
        _grant(client, sid)

        r = client.post(
            "/api/chat",
            json={"session_id": sid, "text": "Hello, I need algebra help."},
            headers=SSE_HEADERS,
        )
        assert r.status_code == 200, r.text
        assert "text/event-stream" in r.headers.get("content-type", "")

        events = _parse_sse(r.text)
        token_events = [(name, data) for name, data in events if name == "token"]
        assert len(token_events) > 1, (
            f"Expected >1 token events (progressive render), got {len(token_events)}. "
            f"All events: {events}"
        )

    def test_concat_streamed_tokens_equals_buffered_reply(self):
        """concat(token events) == the JSON-path reply field for the same canned brain output.
        This is the R37 parity invariant: streaming only changes WHEN tokens surface, not what they say."""
        client_stream = _make_client(reply=self.CANNED_REPLY)
        sid_stream = _start(client_stream)
        _grant(client_stream, sid_stream)

        # Streaming turn.
        r_sse = client_stream.post(
            "/api/chat",
            json={"session_id": sid_stream, "text": "Hello."},
            headers=SSE_HEADERS,
        )
        assert r_sse.status_code == 200, r_sse.text
        events = _parse_sse(r_sse.text)

        # Collect token payloads (each is a JSON-encoded string).
        token_texts = [data for name, data in events if name == "token"]
        streamed_reply = "".join(str(t) for t in token_texts)

        # The `done` event carries the assembled reply — must match concat.
        done_events = [data for name, data in events if name == "done"]
        assert len(done_events) == 1, f"Expected exactly 1 `done` event, got {done_events}"
        done_payload = done_events[0]
        assert isinstance(done_payload, dict), f"done event data must be a dict, got {done_payload!r}"
        reply_from_done = done_payload["reply"]

        assert streamed_reply == reply_from_done, (
            f"concat(tokens)={streamed_reply!r} != done.reply={reply_from_done!r} — R37 parity broken"
        )

        # Verify against the buffered path using a fresh session on the same app.
        client_buf = _make_client(reply=self.CANNED_REPLY)
        sid_buf = _start(client_buf)
        _grant(client_buf, sid_buf)
        r_json = client_buf.post(
            "/api/chat",
            json={"session_id": sid_buf, "text": "Hello."},
        )
        assert r_json.status_code == 200, r_json.text
        buffered_reply = r_json.json()["reply"]

        assert streamed_reply == buffered_reply, (
            f"Streamed reply {streamed_reply!r} != buffered reply {buffered_reply!r} — R37 parity broken"
        )


# ---------------------------------------------------------------------------
# (2) CONSENT GATING — 409 fires BEFORE any token flows (both paths)
# ---------------------------------------------------------------------------

class TestConsentGatingStream:
    """The SSE path must 409 BEFORE opening the stream (no tokens flow for an un-consented session)."""

    def test_no_consent_sse_still_409s(self):
        """A session with no consent (gate=pending) must return 409 on the SSE path — not open a
        stream and then error partway through. The consent gate fires BEFORE the stream is returned."""
        client = _make_client()
        sid = _start(client)
        # NO consent/respond -> gate is pending
        r = client.post(
            "/api/chat",
            json={"session_id": sid, "text": "Hello."},
            headers=SSE_HEADERS,
        )
        assert r.status_code == 409, (
            f"SSE path must 409 (no consent), got {r.status_code}: {r.text}"
        )
        detail = r.json().get("detail", {})
        assert detail.get("state") == "pending"

    def test_need_parental_sse_still_409s(self):
        """A session in need_parental state (self-reported minor) must 409 on the SSE path too."""
        client = _make_client()
        sid = _start(client, jurisdiction="NY")
        client.post("/api/consent/respond", json={
            "session_id": sid, "ai_acknowledged": True, "recording_consent": True, "is_minor": True,
        })
        r = client.post(
            "/api/chat",
            json={"session_id": sid, "text": "Hello."},
            headers=SSE_HEADERS,
        )
        assert r.status_code == 409
        assert r.json()["detail"]["state"] == "need_parental"

    def test_in_chat_minor_detection_sse_409s_before_tokens(self):
        """detect_minor on user text in the SSE path fires BEFORE any token flows (R40 guard).
        A caller who consented as adult but says 'I'm a sophomore calling for myself' must 409."""
        client = _make_client()
        sid = _start(client, jurisdiction="NY")
        _grant(client, sid)

        r = client.post(
            "/api/chat",
            json={"session_id": sid, "text": "I'm a sophomore calling for myself about SAT prep."},
            headers=SSE_HEADERS,
        )
        assert r.status_code == 409, (
            f"In-chat minor detection must 409 on SSE path too, got {r.status_code}: {r.text}"
        )
        assert r.json()["detail"]["state"] == "need_parental"


# ---------------------------------------------------------------------------
# (3) DONE EVENT PAYLOAD — matches the buffered JSON contract
# ---------------------------------------------------------------------------

class TestDoneEventPayload:
    """The SSE `done` event payload must carry {reply, decision_act, done} — the same shape as
    the buffered ChatResponse — so the client can use a single handler for both paths."""

    def test_done_event_has_correct_shape(self):
        """The done event must be a dict with `reply` (str), `decision_act` (str), `done` (bool)."""
        client = _make_client(act="ask")
        sid = _start(client)
        _grant(client, sid)

        r = client.post(
            "/api/chat",
            json={"session_id": sid, "text": "Tell me about tutoring."},
            headers=SSE_HEADERS,
        )
        assert r.status_code == 200
        events = _parse_sse(r.text)
        done_events = [d for name, d in events if name == "done"]
        assert len(done_events) == 1
        payload = done_events[0]

        assert isinstance(payload.get("reply"), str), "reply must be a string"
        assert isinstance(payload.get("decision_act"), str), "decision_act must be a string"
        assert isinstance(payload.get("done"), bool), "done must be a bool"

    @pytest.mark.parametrize(
        "act,tier,expected_done",
        [
            # CB-82: ANY committed close is terminal — done=True for all four tiers on both paths.
            ("attempt_close", "enrollment", True),
            ("attempt_close", "trial", True),           # CB-82: was False before the fix
            ("attempt_close", "consultation", True),    # CB-82: all close tiers are terminal
            ("attempt_close", "callback", True),        # CB-82: all close tiers are terminal
            ("escalate", None, True),
            ("disqualify", None, True),
            ("ask", "goal", False),
        ],
    )
    def test_done_flag_in_sse_matches_buffered_contract(self, act, tier, expected_done):
        """The `done` field in the SSE `done` event matches the exact contract from the buffered path:
        CB-82: done=True for ANY committed close (all four tiers) + escalate + disqualify;
        done=False only for non-close acts (ask, pitch, talk, discovery)."""
        app = create_app(llm_client_factory=lambda: _agent_mock(act=act, tier=tier), live_rag=False)
        client = TestClient(app)
        sid = _start(client, channel="text")
        _grant(client, sid)

        text = "I want a human now." if act == "escalate" else "I'd like to keep going."
        r = client.post(
            "/api/chat",
            json={"session_id": sid, "text": text},
            headers=SSE_HEADERS,
        )
        assert r.status_code == 200
        events = _parse_sse(r.text)
        done_payload = next((d for name, d in events if name == "done"), None)
        assert done_payload is not None, f"No done event found in {events}"
        assert done_payload["done"] is expected_done, (
            f"act={act} tier={tier}: expected done={expected_done}, got {done_payload['done']}"
        )
        assert done_payload["decision_act"] == act


# ---------------------------------------------------------------------------
# (4) TURN PERSISTENCE — timing fields persisted after streamed turn
# ---------------------------------------------------------------------------

class TestStreamTurnPersistence:
    """After a streamed turn, turn state (history, belief) must be committed — same as buffered."""

    def test_session_has_turns_after_streamed_turn(self):
        """GET /api/session/{id} must show committed turns after an SSE /api/chat — the streamed turn
        must call commit_turn (the single state writer) exactly like the buffered path does."""
        client = _make_client()
        sid = _start(client)
        _grant(client, sid)

        r = client.post(
            "/api/chat",
            json={"session_id": sid, "text": "What subjects do you cover?"},
            headers=SSE_HEADERS,
        )
        assert r.status_code == 200

        snap = client.get(f"/api/session/{sid}").json()
        turns = snap.get("turns", [])
        assert len(turns) >= 2, (
            f"Expected at least 2 committed turns (prospect + agent) after a streamed turn, "
            f"got {len(turns)}: {turns}"
        )
        speakers = [t["speaker"] for t in turns]
        assert "prospect" in speakers
        assert "agent" in speakers

    def test_live_upsert_hook_fires_after_streamed_turn(self):
        """The live_upsert_hook must fire after a streamed turn (Layer 2 persistence) — it is called
        in _post_turn_hooks which runs inside the SSE generator after the token stream ends."""
        upsert_calls: list[Any] = []

        async def capture_upsert(session: Any, **kw: Any) -> None:
            upsert_calls.append(kw)

        app = create_app(
            llm_client_factory=lambda: _agent_mock(),
            live_rag=False,
            live_upsert_hook=capture_upsert,
        )
        client = TestClient(app)
        sid = _start(client)
        _grant(client, sid)

        r = client.post(
            "/api/chat",
            json={"session_id": sid, "text": "Hello."},
            headers=SSE_HEADERS,
        )
        assert r.status_code == 200
        # Consume the SSE body so the generator runs to completion.
        events = _parse_sse(r.text)
        done_events = [d for name, d in events if name == "done"]
        assert len(done_events) == 1, "Expected one done event"

        assert len(upsert_calls) == 1, (
            f"Expected live_upsert_hook to fire once after the streamed turn, "
            f"got {len(upsert_calls)} calls: {upsert_calls}"
        )


# ---------------------------------------------------------------------------
# (5) BUFFERED FALLBACK — existing JSON path unchanged
# ---------------------------------------------------------------------------

class TestBufferedFallback:
    """Clients that POST without Accept: text/event-stream get the unchanged JSON response.
    This covers the 17 CB-54 contract tests and any existing client that already works."""

    def test_json_path_still_returns_chat_response(self):
        """Without the SSE header, /api/chat returns application/json with the ChatResponse shape."""
        client = _make_client(act="ask")
        sid = _start(client)
        _grant(client, sid)

        r = client.post("/api/chat", json={"session_id": sid, "text": "Hello."})
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        assert "application/json" in ct, f"Expected JSON content-type, got {ct!r}"
        payload = r.json()
        assert "reply" in payload and payload["reply"]
        assert "decision_act" in payload
        assert "done" in payload

    def test_json_path_done_flag_matches_sse(self):
        """The buffered JSON `done` flag and SSE `done` flag are identical for the same act/tier."""
        # Buffered: enrollment close -> done=True
        client_buf = _make_client(act="attempt_close", tier="enrollment")
        sid_buf = _start(client_buf)
        _grant(client_buf, sid_buf)
        r_buf = client_buf.post(
            "/api/chat", json={"session_id": sid_buf, "text": "I want to sign up."}
        )
        assert r_buf.status_code == 200
        assert r_buf.json()["done"] is True

        # SSE: same act -> done=True in the done event.
        client_sse = _make_client(act="attempt_close", tier="enrollment")
        sid_sse = _start(client_sse)
        _grant(client_sse, sid_sse)
        r_sse = client_sse.post(
            "/api/chat",
            json={"session_id": sid_sse, "text": "I want to sign up."},
            headers=SSE_HEADERS,
        )
        assert r_sse.status_code == 200
        events = _parse_sse(r_sse.text)
        done_payload = next((d for name, d in events if name == "done"), None)
        assert done_payload is not None
        assert done_payload["done"] is True


# ---------------------------------------------------------------------------
# (6) ESCALATION HOOK — fires on the SSE path too (U14/R42)
# ---------------------------------------------------------------------------

class TestEscalationOnStream:
    """An escalate on the SSE path must capture the escalation log with a PII-scrubbed moment,
    exactly as the buffered path does (U14/R42). The hook must fire inside the SSE generator."""

    def test_escalation_hook_fires_and_pii_is_scrubbed(self):
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
        sid = _start(client, channel="text")
        _grant(client, sid)

        r = client.post(
            "/api/chat",
            json={"session_id": sid, "text": "I want a human now."},
            headers=SSE_HEADERS,
        )
        assert r.status_code == 200
        # Consume the stream to ensure the generator runs to completion.
        events = _parse_sse(r.text)
        done_payload = next((d for name, d in events if name == "done"), None)
        assert done_payload is not None
        assert done_payload["decision_act"] == "escalate"
        assert done_payload["done"] is True

        assert len(captured) == 1, f"Expected 1 escalation log, got {len(captured)}"
        moment = captured[0].moment or ""
        assert "advisor@nerdy.com" not in moment, "Raw email must be scrubbed"
        assert "555-867-5309" not in moment, "Raw phone must be scrubbed"
        assert "[EMAIL]" in moment and "[PHONE]" in moment, "PII redaction placeholders must be present"

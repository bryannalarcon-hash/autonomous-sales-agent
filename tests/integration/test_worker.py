# Integration tests for the RUNNABLE LiveKit voice WORKER (src.voice.worker) — the previously
# UNCOVERED last-mile glue (WorkerVoiceAgent + the consent gate + commit/discard wiring). DB-FREE and
# does NOT spin up real LiveKit media: WorkerVoiceAgent is constructible without the plugins (the
# heavy elevenlabs/silero imports live inside entrypoint, never at module import), so we drive its
# 1.5.x lifecycle hooks against a FakeEmbedder + MockLLMClient + a local retrieve stub (same pattern
# as tests/integration/test_voice_adapter). The LOAD-BEARING test is the COMPLIANCE gate (the blocking
# finding): with a not-yet-can_converse ConsentGate a user turn is NOT run through the brain (raises
# ConsentError, no transcript captured); with a ready gate it proceeds. Also covers: empty-text
# short-circuit, llm_node surfacing the buffered reply (and "" on the None branches), the
# _wire_commit_on_speech done-callback committing on uninterrupted playout vs discarding on barge-in,
# the persist-forwarding capture (escalation EscalationLog buffered + close-tier tracked on commit),
# and the pure room-metadata helpers (_room_jurisdiction / _room_raw_phone from metadata + SIP attrs).
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

import pytest

from src.config.settings import load_config
from src.core.llm import Message, MockLLMClient
from src.kb.embeddings import FakeEmbedder
from src.kb.retriever import Chunk
from src.voice.consent import ConsentGate, RefusalPolicy
from src.voice.session import ConsentError

# IMPORTANT — src.voice.worker AND src.voice.agent are imported LAZILY (inside the helpers/tests
# below), NOT at module top. In an env where livekit-agents IS installed, importing either pulls
# `livekit` into sys.modules (agent.py / worker.py guard-import livekit.agents); test_voice_adapter's
# isolation tests (which run BEFORE these, alphabetically) assert `livekit` is absent from sys.modules.
# Deferring both imports keeps test COLLECTION livekit-free so those isolation tests stay green, while
# the worker's/agent's OWN livekit imports stay guarded (Agent->object when livekit is absent).


def _worker():
    """Lazy accessor for src.voice.worker (deferred so test collection never imports livekit)."""
    import src.voice.worker as worker_mod

    return worker_mod


def _build_voice_agent(*args: Any, **kwargs: Any):
    """Lazy proxy for src.voice.agent.build_voice_agent (deferred so collection never imports livekit)."""
    from src.voice.agent import build_voice_agent

    return build_voice_agent(*args, **kwargs)


# --- Agent mock (IDENTICAL routing contract to test_voice_adapter._agent_mock) ------------------
def _is_json_call(opts: dict[str, Any]) -> bool:
    rf = opts.get("response_format")
    return isinstance(rf, dict) and rf.get("type") == "json_object"


def _agent_mock(
    *,
    act: str = "ask",
    target_slot: Optional[str] = "goal",
    tier: Optional[str] = None,
    confidence: float = 0.8,
    reply: str = "Sure - tell me a bit about what your child is working on.",
) -> MockLLMClient:
    """An agent LLMClient whose single callable serves every respond() call by routing on the
    response_format opt (json -> combined Decision+driver JSON; else the plain reply)."""
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


def _make_retrieve_hook():
    """A deterministic local retrieve hook (no DB): embeds a tiny fixed KB with FakeEmbedder and
    returns the closest chunk — proves voice answers are RAG-grounded without Postgres."""
    embedder = FakeEmbedder()
    kb = [
        Chunk(id="c1", kb_version="kb_v0", source="pricing#cost",
              text="Tutoring plans start around 40 dollars per session with monthly bundles."),
        Chunk(id="c2", kb_version="kb_v0", source="proof#results",
              text="Families report grades improving within the first month of regular sessions."),
    ]
    kb_vecs = embedder.embed([c.text for c in kb])

    def _retrieve(query: str, *, kb_version: str, k: int = 4) -> list[Chunk]:
        qv = embedder.embed([query])[0]
        scored = []
        for chunk, vec in zip(kb, kb_vecs):
            dot = sum(a * b for a, b in zip(qv, vec))
            scored.append((dot, chunk))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [c for _, c in scored[: max(0, k)]]

    return _retrieve


def _make_worker_agent(
    consent_gate: Optional[ConsentGate] = None,
    *,
    llm: Optional[MockLLMClient] = None,
):
    """Build the worker's WorkerVoiceAgent around the SAME build_voice_agent the worker uses, with an
    optional ConsentGate threaded into the VoiceSession (the compliance fix under test)."""
    config = load_config("champion_v0")
    base = _build_voice_agent(
        config,
        llm or _agent_mock(),
        FakeEmbedder(),
        kb_chunks_or_retrieve_hook=_make_retrieve_hook(),
        consent_gate=consent_gate,
    )
    return _worker().WorkerVoiceAgent(base)


_CONFIG = load_config("champion_v0")


# --- Lightweight livekit-free stubs (the hooks only read these shapes) --------------------------
class _StubMessage:
    """Stands in for lkllm.ChatMessage — on_user_turn_completed only reads `.text_content`."""

    def __init__(self, text: str) -> None:
        self.text_content = text


class _StubSpeechHandle:
    """Stands in for SpeechHandle — _wire_commit_on_speech reads `.interrupted` and registers a
    done-callback via add_done_callback; we capture the callback and fire it manually."""

    def __init__(self, interrupted: bool = False) -> None:
        self.interrupted = interrupted
        self._done_cbs: list[Any] = []

    def add_done_callback(self, cb: Any) -> None:
        self._done_cbs.append(cb)

    def fire_done(self) -> None:
        for cb in self._done_cbs:
            cb(self)


class _StubSpeechEvent:
    """Stands in for SpeechCreatedEvent — carries user_initiated + speech_handle."""

    def __init__(self, handle: _StubSpeechHandle, user_initiated: bool = False) -> None:
        self.user_initiated = user_initiated
        self.speech_handle = handle


class _StubSession:
    """Stands in for AgentSession — _wire_commit_on_speech only calls session.on(event, handler)."""

    def __init__(self) -> None:
        self._handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler

    def emit(self, event: str, payload: Any) -> None:
        self._handlers[event](payload)


# =============================== on_user_turn_completed =========================================


async def test_on_user_turn_completed_buffers_pending_turn():
    """A non-empty user turn runs the brain speculatively and buffers a PendingTurn under a fresh
    speech_id (no commit yet) — the speech_id is remembered so llm_node can surface the reply."""
    agent = _make_worker_agent()
    await agent.on_user_turn_completed(None, _StubMessage("Hi, my daughter needs algebra help."))

    sid = agent._pending_speech_id
    assert sid is not None
    assert sid in agent.voice_session.state.pending
    assert agent.voice_session.state.pending[sid].committed is False
    # Speculative only: NOTHING committed yet.
    assert agent.voice_session.state.turns == []
    assert agent.voice_session.state.history == []


async def test_on_user_turn_completed_short_circuits_on_empty_text():
    """Empty/whitespace STT text is a no-op: no speech_id is set, the brain never runs, nothing
    is buffered (guards against firing the LLM on a phantom/blank turn)."""
    agent = _make_worker_agent()
    await agent.on_user_turn_completed(None, _StubMessage("   "))
    assert agent._pending_speech_id is None
    assert agent.voice_session.state.pending == {}


# =============================== llm_node (surface, never re-decide) ============================


async def test_llm_node_surfaces_buffered_reply():
    """llm_node returns the reply_text the brain ALREADY produced for the current speech_id (parity:
    it surfaces, never re-decides)."""
    agent = _make_worker_agent()
    await agent.on_user_turn_completed(None, _StubMessage("How much does it cost per month?"))
    sid = agent._pending_speech_id
    expected = agent.voice_session.state.pending[sid].reply_text
    assert await agent.llm_node(None, None, None) == expected


async def test_llm_node_returns_empty_on_none_branches():
    """llm_node returns "" when there is no pending speech (no turn yet) or the pending entry is
    missing (e.g. already committed/discarded) — the defensive None branches yield ""."""
    agent = _make_worker_agent()
    # (a) no turn has happened: _pending_speech_id is None
    assert await agent.llm_node(None, None, None) == ""
    # (b) a speech_id is set but its pending entry is gone (committed/discarded)
    await agent.on_user_turn_completed(None, _StubMessage("Hi there."))
    sid = agent._pending_speech_id
    agent.voice_session.state.pending.pop(sid)
    assert await agent.llm_node(None, None, None) == ""


# =============================== commit-vs-discard done-callback ================================


async def test_done_callback_commits_on_uninterrupted_playout():
    """_wire_commit_on_speech: when the agent reply plays UNINTERRUPTED, the done-callback commits
    the speculative turn (the only state write) — belief advances + the turn is captured."""
    agent = _make_worker_agent()
    session = _StubSession()
    _worker()._wire_commit_on_speech(session, agent, _CONFIG)

    await agent.on_user_turn_completed(None, _StubMessage("Hi, my daughter needs algebra help."))
    sid = agent._pending_speech_id

    handle = _StubSpeechHandle(interrupted=False)
    session.emit("speech_created", _StubSpeechEvent(handle, user_initiated=False))
    handle.fire_done()

    # Committed: pending drained, a captured agent Turn exists, history grew (user + agent).
    assert sid not in agent.voice_session.state.pending
    assert agent.voice_session.turn_for_speech_id(sid) is not None
    assert len(agent.voice_session.state.history) == 2


async def test_done_callback_discards_on_barge_in_committed_state_intact():
    """_wire_commit_on_speech: a barge-in (handle.interrupted) drops the speculative turn with NO
    committed-state write — a prior committed turn is left intact (R5/R10 self-recover)."""
    agent = _make_worker_agent()
    session = _StubSession()
    _worker()._wire_commit_on_speech(session, agent, _CONFIG)

    # Commit one real turn first so there IS committed state to protect.
    await agent.on_user_turn_completed(None, _StubMessage("Hi, my daughter needs algebra help."))
    h0 = _StubSpeechHandle(interrupted=False)
    session.emit("speech_created", _StubSpeechEvent(h0, user_initiated=False))
    h0.fire_done()
    committed_belief = agent.voice_session.state.belief
    committed_hist_len = len(agent.voice_session.state.history)

    # A second turn that gets barged in on.
    await agent.on_user_turn_completed(None, _StubMessage("How much does it cost per month?"))
    sid1 = agent._pending_speech_id
    assert sid1 in agent.voice_session.state.pending
    h1 = _StubSpeechHandle(interrupted=True)
    session.emit("speech_created", _StubSpeechEvent(h1, user_initiated=False))
    h1.fire_done()

    # Speculative turn dropped; committed state untouched; interruption recorded.
    assert sid1 not in agent.voice_session.state.pending
    assert agent.voice_session.state.belief is committed_belief
    assert len(agent.voice_session.state.history) == committed_hist_len
    assert agent.voice_session.state.last_interrupted_speech_id == sid1


async def test_done_callback_ignores_user_initiated_speech():
    """User-initiated speech carries no buffered brain turn — the done-callback path is skipped, so a
    pending speculative turn is NEITHER committed nor discarded by it."""
    agent = _make_worker_agent()
    session = _StubSession()
    _worker()._wire_commit_on_speech(session, agent, _CONFIG)

    await agent.on_user_turn_completed(None, _StubMessage("Hi there."))
    sid = agent._pending_speech_id
    session.emit("speech_created", _StubSpeechEvent(_StubSpeechHandle(), user_initiated=True))
    # Still pending: a user-initiated speech event must not touch the speculative buffer.
    assert sid in agent.voice_session.state.pending


# =============================== COMPLIANCE GATE (the blocking finding) =========================


async def test_consent_gate_blocks_brain_until_can_converse():
    """THE compliance fix: with a NOT-yet-can_converse ConsentGate threaded into the worker's
    VoiceSession, a user turn is NOT run through the brain — handle_user_turn raises ConsentError and
    captures NO transcript / pending turn (no recording before consent)."""
    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    assert gate.can_converse is False  # fresh gate: pending, conversation blocked
    agent = _make_worker_agent(consent_gate=gate)

    with pytest.raises(ConsentError):
        await agent.voice_session.handle_user_turn("How much does it cost?", "pre-consent")

    # The brain never ran: no pending turn, no transcript, no history.
    assert agent.voice_session.state.pending == {}
    assert agent.voice_session.state.turns == []
    assert agent.voice_session.state.history == []
    # And the gate is NOT recording (so metrics["recorded"] would be False).
    assert agent.voice_session.recorded is False


async def test_consent_gate_allows_brain_once_ready():
    """Once recording consent is granted (gate -> ready / can_converse), the SAME worker turn path
    proceeds: the brain runs, a pending turn is buffered, and the gate reports recording allowed."""
    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    gate.grant_recording()
    assert gate.can_converse is True and gate.can_record is True
    agent = _make_worker_agent(consent_gate=gate)

    await agent.on_user_turn_completed(None, _StubMessage("How much does it cost per month?"))
    sid = agent._pending_speech_id
    assert sid is not None
    assert sid in agent.voice_session.state.pending
    assert agent.voice_session.recorded is True


# =============================== ESCALATION / CLOSE-TIER CAPTURE (persist forwarding) ===========


async def test_commit_captures_close_tier_for_persistence():
    """On a committed attempt_close, the worker tracks the close tier (the schema Turn carries no
    tier) so persist_call_end can map it to the right ladder rung — parity with the /api/chat path."""
    agent = _make_worker_agent(llm=_agent_mock(act="attempt_close", target_slot=None, tier="trial"))
    session = _StubSession()
    _worker()._wire_commit_on_speech(session, agent, _CONFIG)

    await agent.on_user_turn_completed(None, _StubMessage("Alright, let's book a trial session."))
    handle = _StubSpeechHandle(interrupted=False)
    session.emit("speech_created", _StubSpeechEvent(handle, user_initiated=False))
    handle.fire_done()

    assert agent.last_close_tier == "trial"


async def test_commit_captures_escalation_log_for_persistence():
    """On a committed escalate, the worker BUFFERS an EscalationLog (re-pointed onto the episode at
    persist) — parity with the /api/chat path. The async handler runs with a no-op store_hook (no
    mid-call DB write); we await the scheduled task before asserting the buffer is populated."""
    import asyncio as _asyncio

    agent = _make_worker_agent(
        llm=_agent_mock(act="escalate", target_slot=None, reply="A specialist will follow up shortly.")
    )
    session = _StubSession()
    _worker()._wire_commit_on_speech(session, agent, _CONFIG)

    await agent.on_user_turn_completed(None, _StubMessage("I want to talk to a human right now."))
    handle = _StubSpeechHandle(interrupted=False)
    session.emit("speech_created", _StubSpeechEvent(handle, user_initiated=False))
    handle.fire_done()

    # The escalation handler was scheduled; drain it (it persists nothing — no-op store_hook).
    assert agent._escalation_tasks, "an escalation-handling task should have been scheduled"
    await _asyncio.gather(*agent._escalation_tasks)
    assert len(agent.escalations) == 1
    log = agent.escalations[0]
    # Buffered with an EMPTY episode_id — re-pointed onto the real episode at persist (FK-safe).
    assert log.episode_id == ""
    assert log.lifecycle == "unreviewed"


# =============================== ROOM METADATA PARSING (jurisdiction / phone) ====================


class _StubParticipant:
    def __init__(self, attributes: dict[str, str]) -> None:
        self.attributes = attributes


class _StubRoom:
    def __init__(self, metadata: Optional[str] = None,
                 remote_participants: Optional[dict[str, Any]] = None) -> None:
        self.metadata = metadata
        self.name = "demo-test"
        self.remote_participants = remote_participants or {}


class _StubCtx:
    def __init__(self, room: _StubRoom) -> None:
        self.room = room


def test_room_jurisdiction_from_metadata_else_default():
    """_room_jurisdiction reads the room metadata `jurisdiction` (or region/state), tolerates
    missing/garbage metadata, and falls back to the env default — recording stays gated regardless."""
    from src.voice import worker as w

    assert w._room_jurisdiction(_StubCtx(_StubRoom(metadata='{"jurisdiction": "CA"}'))) == "CA"
    assert w._room_jurisdiction(_StubCtx(_StubRoom(metadata='{"region": "WA"}'))) == "WA"
    # Garbage / missing metadata -> the env DEFAULT_JURISDICTION (default "").
    assert w._room_jurisdiction(_StubCtx(_StubRoom(metadata="not json"))) == w.DEFAULT_JURISDICTION
    assert w._room_jurisdiction(_StubCtx(_StubRoom(metadata=None))) == w.DEFAULT_JURISDICTION


def test_room_raw_phone_from_metadata_and_sip_attributes():
    """_room_raw_phone prefers room metadata `phone`/`caller`, falls back to a SIP participant's
    sip.phoneNumber attribute, and returns None when no source carries a phone."""
    from src.voice import worker as w

    assert w._room_raw_phone(_StubCtx(_StubRoom(metadata='{"phone": "+15551234567"}'))) == "+15551234567"
    sip_room = _StubRoom(
        metadata=None,
        remote_participants={"p1": _StubParticipant({"sip.phoneNumber": "+15559998888"})},
    )
    assert w._room_raw_phone(_StubCtx(sip_room)) == "+15559998888"
    # No phone anywhere -> None (anonymous caller).
    assert w._room_raw_phone(_StubCtx(_StubRoom(metadata="{}"))) is None

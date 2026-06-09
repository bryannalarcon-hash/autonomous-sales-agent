# Integration tests for the RUNNABLE LiveKit voice WORKER (src.voice.worker) — the previously
# UNCOVERED last-mile glue (WorkerVoiceAgent + the consent gate + commit/discard wiring). DB-FREE and
# does NOT spin up real LiveKit media: WorkerVoiceAgent is constructible without the plugins (the
# heavy elevenlabs/silero imports live inside entrypoint, never at module import), so we drive its
# 1.5.x lifecycle hooks against a FakeEmbedder + MockLLMClient + a local retrieve stub (same pattern
# as tests/integration/test_voice_adapter). The LOAD-BEARING test is the COMPLIANCE gate (the blocking
# finding): with a not-yet-can_converse ConsentGate a user turn is NOT run through the brain (raises
# ConsentError fail-fast in on_user_turn_completed, no transcript captured); with a ready gate it
# proceeds. CB-41 (stream NLG -> TTS): the brain now runs STREAMING inside llm_node (NOT in
# on_user_turn_completed, which only fail-fast-checks consent + stashes the user text), so NLG tokens
# reach TTS at generation pace. Tests therefore drive a full turn via _run_turn (on_user_turn_completed
# then drain llm_node). Also covers: empty-text short-circuit, llm_node STREAMING the GENUINE NLG
# token stream (CB-41 — tokens rejoin to the exact committed reply, >1 token, yields nothing on the
# None branches, still commits at end) + the pure _word_chunks parity/grouping helper (no longer on the
# llm_node path but retained), the _wire_commit_on_speech done-callback committing on uninterrupted
# playout (now a redundant backstop after llm_node already committed) vs discarding a still-pending
# turn on barge-in, the persist-forwarding capture (escalation EscalationLog buffered + close-tier
# tracked on commit), and the pure room-metadata helpers (_room_jurisdiction / _room_raw_phone).
# ALSO covers the SIP spoken-consent sub-flow (the PSTN path that has no browser to click consent):
# the deterministic _classify_consent_reply, _is_sip_call participant-kind detection, and the
# WorkerVoiceAgent IVR loop — the first user turn is intercepted as the consent answer (NOT run
# through the brain), "yes" -> can_converse + recorded, "no" -> unrecorded/ended per policy, a minor
# reply -> need_parental (blocked), unclear -> re-ask once then end; once consent resolves a
# subsequent turn flows to the brain. The browser path (metadata-seeded) is unchanged (regression).
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
    agent = _worker().WorkerVoiceAgent(base)
    # Mirror _build_brain: llm_node's commit -> _capture_post_commit (escalation/close-tier + the live
    # in_progress + partial upserts) needs the live config. Without it those post-commit hooks are
    # skipped (config-guarded), so the escalation/close-tier capture tests would see nothing.
    agent._config = config
    return agent


_CONFIG = load_config("champion_v0")


async def _drain_llm_node(agent: Any) -> str:
    """Run the worker's (now STREAMING) llm_node to completion and return the rejoined reply.

    CB-41: llm_node runs the WHOLE brain STREAMING (handle_user_turn_stream) and yields each NLG token
    (then commits the turn at the end). Tests drain the generator and re-join the tokens — which equals
    the committed reply (R37 parity)."""
    chunks: list[str] = []
    async for chunk in agent.llm_node(None, None, None):
        chunks.append(chunk)
    return "".join(chunks)


async def _run_turn(agent: Any, text: str) -> str:
    """Drive ONE full agent turn the way livekit's pipeline does (CB-41): on_user_turn_completed (which
    fail-fast-checks consent + stashes the user text) THEN drain llm_node (which runs the streaming
    brain, yields the NLG tokens, and commits at the end). Returns the rejoined reply tokens."""
    await agent.on_user_turn_completed(None, _StubMessage(text))
    return await _drain_llm_node(agent)


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


async def test_on_user_turn_completed_stashes_then_llm_node_runs_brain():
    """CB-41: on_user_turn_completed STASHES the user text under a fresh speech_id (the brain does NOT
    run here — it runs streaming in llm_node so NLG tokens reach TTS at generation pace). After
    on_user_turn_completed: a speech_id + pending text are set but NOTHING is buffered/committed yet.
    Draining llm_node then runs the brain (buffers + commits the turn)."""
    agent = _make_worker_agent()
    await agent.on_user_turn_completed(None, _StubMessage("Hi, my daughter needs algebra help."))

    sid = agent._pending_speech_id
    assert sid is not None
    assert agent._pending_user_text == "Hi, my daughter needs algebra help."
    # The brain has NOT run yet: nothing buffered, nothing committed.
    assert agent.voice_session.state.pending == {}
    assert agent.voice_session.state.turns == []
    assert agent.voice_session.state.history == []

    # Draining llm_node runs the streaming brain and commits the turn at the end.
    await _drain_llm_node(agent)
    assert agent.voice_session.turn_for_speech_id(sid) is not None  # captured agent Turn
    assert len(agent.voice_session.state.history) == 2  # user + agent


async def test_on_user_turn_completed_short_circuits_on_empty_text():
    """Empty/whitespace STT text is a no-op: no speech_id is set, the brain never runs, nothing
    is buffered (guards against firing the LLM on a phantom/blank turn)."""
    agent = _make_worker_agent()
    await agent.on_user_turn_completed(None, _StubMessage("   "))
    assert agent._pending_speech_id is None
    assert agent.voice_session.state.pending == {}


# =============================== llm_node (surface, never re-decide) ============================


async def test_llm_node_streams_genuine_nlg_tokens_with_parity():
    """CB-41: llm_node runs the brain STREAMING and yields the GENUINE NLG token stream for the current
    speech_id; rejoining the tokens yields the EXACT committed reply (R37 parity — no word is
    re-decided). It also yields >1 token for a multi-word reply (so TTS speaks progressively)."""
    reply = "Sure, let me get a few details about what your child is working on."
    agent = _make_worker_agent(llm=_agent_mock(reply=reply))
    await agent.on_user_turn_completed(None, _StubMessage("How much does it cost per month?"))
    sid = agent._pending_speech_id
    # Drive the generator manually so we can count tokens (CB-41: multi-word reply -> >1 token).
    tokens: list[str] = []
    async for token in agent.llm_node(None, None, None):
        tokens.append(token)
    assert len(tokens) > 1  # CB-41: the reply streamed in tokens, not one whole string
    # R37 parity: the streamed tokens rejoin to the EXACT reply the brain committed.
    committed = agent.voice_session.turn_for_speech_id(sid)
    assert committed is not None
    assert "".join(tokens) == committed.text  # streamed bytes == committed reply
    assert "".join(tokens) == reply


async def test_llm_node_yields_nothing_on_none_branches():
    """CB-41: llm_node yields NOTHING (the rejoined output is "") when there is no pending speech (no
    turn yet) or the stashed user text is gone (already consumed) — the defensive None branches return
    from the async generator before running the brain."""
    agent = _make_worker_agent()
    # (a) no turn has happened: _pending_speech_id is None
    assert await _drain_llm_node(agent) == ""
    # (b) a speech_id is set but the stashed user text was already consumed (a prior drain)
    await agent.on_user_turn_completed(None, _StubMessage("Hi there."))
    agent._pending_user_text = None  # simulate the text already consumed by an earlier llm_node run
    assert await _drain_llm_node(agent) == ""


async def test_llm_node_commits_the_turn_reliably():
    """Regression for the live-call bug: llm_node COMMITS the turn at the END of streaming (the
    reliable state write). The SpeechHandle done-callback did NOT fire on real calls, so turns/belief
    never persisted (every brain turn logged turn=1; a real call saved an empty in_progress shell that
    never reached Calls or Live). CB-41: the brain now runs streaming IN llm_node, which buffers the
    PendingTurn then commits it — the streaming refactor must NOT reintroduce the never-committed bug."""
    agent = _make_worker_agent()
    await agent.on_user_turn_completed(None, _StubMessage("How much for SAT prep?"))
    sid = agent._pending_speech_id
    # Nothing buffered yet (the brain runs in llm_node, not on_user_turn_completed).
    assert agent.voice_session.state.pending == {}
    await _drain_llm_node(agent)
    assert sid not in agent.voice_session.state.pending  # committed -> pending drained
    assert agent.voice_session.turn_for_speech_id(sid) is not None  # captured agent Turn
    assert len(agent.voice_session.state.history) == 2  # user + agent


# =============================== CB-31: word-chunking (pure, livekit-free) =======================


def test_word_chunks_rejoin_to_exact_reply():
    """CB-31 R37 parity: _word_chunks splits a reply into ordered chunks that REJOIN byte-for-byte to
    the original (no word is added/dropped/re-decided). The whole point — the chunks streamed to TTS
    are the SAME reply the brain produced."""
    w = _worker()
    for reply in (
        "one",
        "one two",
        "Sure, let me get a few details about what your child is working on.",
        "Multiple   spaces  and\nnewlines preserved here.",
        "A longer reply that should split into several word groups for natural TTS prosody units.",
    ):
        chunks = w._word_chunks(reply)
        assert "".join(chunks) == reply, (reply, chunks)


def test_word_chunks_streams_multiple_chunks_for_multiword_reply():
    """CB-31: a multi-word reply yields >1 chunk (so it streams progressively to TTS / the monitor),
    while a single word yields exactly one chunk and empty/whitespace yields none."""
    w = _worker()
    assert len(w._word_chunks("How much does it cost per month right now")) > 1
    assert w._word_chunks("Hello") == ["Hello"]
    assert w._word_chunks("") == []
    assert w._word_chunks("    ") == []


def test_word_chunks_groups_by_words_per_chunk():
    """CB-31: chunks group ~words_per_chunk words each (greedy), so the chunk count is bounded by the
    word count / group size — enough chunks to stream progressively without one-word-at-a-time TTS."""
    w = _worker()
    chunks = w._word_chunks("a b c d e f g", words_per_chunk=3)
    assert chunks == ["a b c ", "d e f ", "g"]
    assert "".join(chunks) == "a b c d e f g"


# =============================== commit-vs-discard done-callback ================================


async def test_done_callback_is_redundant_backstop_after_llm_node_commit():
    """CB-41: the turn now commits at the END of llm_node's streaming (the reliable write). When the
    agent reply plays UNINTERRUPTED, the done-callback fires AFTER llm_node already committed — it is a
    harmless, idempotent no-op backstop (commit_turn pops the pending, so a second commit finds nothing
    and the captured turn / history are unchanged). The turn must be committed exactly once."""
    agent = _make_worker_agent()
    session = _StubSession()
    _worker()._wire_commit_on_speech(session, agent, _CONFIG)

    # A full turn: on_user_turn_completed stashes; llm_node runs the brain + commits.
    await agent.on_user_turn_completed(None, _StubMessage("Hi, my daughter needs algebra help."))
    sid = agent._pending_speech_id
    await _drain_llm_node(agent)
    # Already committed by llm_node: pending drained, a captured agent Turn exists, history == 2.
    assert sid not in agent.voice_session.state.pending
    assert agent.voice_session.turn_for_speech_id(sid) is not None
    assert len(agent.voice_session.state.history) == 2

    # The done-callback now fires (the redundant backstop): it must be a no-op (commit is idempotent).
    handle = _StubSpeechHandle(interrupted=False)
    session.emit("speech_created", _StubSpeechEvent(handle, user_initiated=False))
    handle.fire_done()
    assert len(agent.voice_session.state.history) == 2  # unchanged — committed exactly once


async def test_done_callback_discards_on_barge_in_committed_state_intact():
    """_wire_commit_on_speech: a barge-in (handle.interrupted) on a turn whose llm_node stream was cut
    short (so nothing was buffered/committed) drops the speculative turn with NO committed-state write —
    a prior committed turn is left intact (R5/R10 self-recover). CB-41: a barge-in mid-NLG-stream
    cancels llm_node before it buffers/commits, so on_barge_in just records the interruption."""
    agent = _make_worker_agent()
    session = _StubSession()
    _worker()._wire_commit_on_speech(session, agent, _CONFIG)

    # Commit one real turn first (full turn through llm_node) so there IS committed state to protect.
    await _run_turn(agent, "Hi, my daughter needs algebra help.")
    committed_belief = agent.voice_session.state.belief
    committed_hist_len = len(agent.voice_session.state.history)

    # A second turn that gets barged in on BEFORE llm_node ran (the NLG stream was cut short): only
    # on_user_turn_completed ran (text stashed), nothing buffered/committed.
    await agent.on_user_turn_completed(None, _StubMessage("How much does it cost per month?"))
    sid1 = agent._pending_speech_id
    assert agent.voice_session.state.pending == {}  # nothing buffered yet (brain didn't run)
    h1 = _StubSpeechHandle(interrupted=True)
    session.emit("speech_created", _StubSpeechEvent(h1, user_initiated=False))
    h1.fire_done()

    # No speculative turn to drop; committed state untouched; interruption recorded.
    assert sid1 not in agent.voice_session.state.pending
    assert agent.voice_session.state.belief is committed_belief
    assert len(agent.voice_session.state.history) == committed_hist_len
    assert agent.voice_session.state.last_interrupted_speech_id == sid1


async def test_done_callback_ignores_user_initiated_speech():
    """User-initiated speech carries no buffered brain turn — the done-callback path is skipped, so a
    stashed (not-yet-run) turn is NEITHER committed nor discarded by it."""
    agent = _make_worker_agent()
    session = _StubSession()
    _worker()._wire_commit_on_speech(session, agent, _CONFIG)

    await agent.on_user_turn_completed(None, _StubMessage("Hi there."))
    sid = agent._pending_speech_id
    session.emit("speech_created", _StubSpeechEvent(_StubSpeechHandle(), user_initiated=True))
    # The stash is untouched: a user-initiated speech event must not run/discard the brain turn.
    assert agent._pending_speech_id == sid
    assert agent._pending_user_text == "Hi there."


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
    proceeds: on_user_turn_completed stashes the turn, llm_node runs the brain + commits it, and the
    gate reports recording allowed."""
    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    gate.grant_recording()
    assert gate.can_converse is True and gate.can_record is True
    agent = _make_worker_agent(consent_gate=gate)

    await agent.on_user_turn_completed(None, _StubMessage("How much does it cost per month?"))
    sid = agent._pending_speech_id
    assert sid is not None
    await _drain_llm_node(agent)  # llm_node runs the streaming brain + commits
    assert agent.voice_session.turn_for_speech_id(sid) is not None  # brain ran, turn captured
    assert agent.voice_session.recorded is True


# =============================== ESCALATION / CLOSE-TIER CAPTURE (persist forwarding) ===========


async def test_commit_captures_close_tier_for_persistence():
    """On a committed attempt_close, the worker tracks the close tier (the schema Turn carries no
    tier) so persist_call_end can map it to the right ladder rung — parity with the /api/chat path.
    CB-41: the commit (+ _capture_post_commit) now happens at the END of llm_node's streaming."""
    agent = _make_worker_agent(llm=_agent_mock(act="attempt_close", target_slot=None, tier="trial"))

    # A full turn: llm_node runs the brain, commits, and _capture_post_commit tracks the close tier.
    await _run_turn(agent, "Alright, let's book a trial session.")

    assert agent.last_close_tier == "trial"


async def test_commit_captures_escalation_log_for_persistence():
    """On a committed escalate, the worker BUFFERS an EscalationLog (re-pointed onto the episode at
    persist) — parity with the /api/chat path. CB-41: the commit (+ the scheduled escalation handler)
    happens at the END of llm_node's streaming; the async handler runs with a no-op store_hook (no
    mid-call DB write); we await the scheduled task before asserting the buffer is populated."""
    import asyncio as _asyncio

    agent = _make_worker_agent(
        llm=_agent_mock(act="escalate", target_slot=None, reply="A specialist will follow up shortly.")
    )

    # A full turn: llm_node runs the brain + commits; _capture_post_commit schedules the escalation.
    await _run_turn(agent, "I want to talk to a human right now.")

    # The escalation handler was scheduled; drain it (it persists nothing — no-op store_hook).
    assert agent._escalation_tasks, "an escalation-handling task should have been scheduled"
    await _asyncio.gather(*agent._escalation_tasks)
    assert len(agent.escalations) == 1
    log = agent.escalations[0]
    # Buffered with an EMPTY episode_id — re-pointed onto the real episode at persist (FK-safe).
    assert log.episode_id == ""
    assert log.lifecycle == "unreviewed"


# =============================== CB-22: escalated-then-continued disconnect settles escalated ====


async def test_escalated_then_continued_disconnect_settles_escalated():
    """CB-22 (the ep-eba52675… case): the agent ESCALATES, then KEEPS TALKING (the last committed turn
    is a non-terminal handle_objection). On disconnect the worker must settle "escalated", not leave
    the call in_progress (which the Calls list filters out, so the call vanishes).

    This drives two real committed turns through the worker (escalate, then a non-terminal turn) and
    then runs the EXACT settle computation _register_persistence._on_shutdown does — _agent_acts scan
    + _terminal_outcome_on_disconnect — asserting it lands "escalated". DB-free: no persist_call_end /
    store needed to prove the mapping integration (the pure mapping is unit-tested separately)."""
    w = _worker()
    agent = _make_worker_agent(
        llm=_agent_mock(act="escalate", target_slot=None, reply="Let me bring in a specialist.")
    )
    # Turn 1: the agent escalates (committed via llm_node, the reliable state write).
    await agent.on_user_turn_completed(None, _StubMessage("I want to speak to a human."))
    await _drain_llm_node(agent)

    # Turn 2: the agent keeps talking with a NON-terminal act (handle_objection) — the brain did not
    # stop after escalating, exactly the reported failure. Swap the agent mock's served decision.
    agent.voice_session.llm_client = _agent_mock(
        act="handle_objection", target_slot=None, reply="I understand — while we connect you…"
    )
    await agent.on_user_turn_completed(None, _StubMessage("But how much does it cost?"))
    await _drain_llm_node(agent)

    turns = agent.voice_session.state.turns
    acts = w._agent_acts(turns)
    assert "escalate" in acts and "handle_objection" in acts
    # The last committed agent act is the NON-terminal one (the bug trigger).
    assert w._last_committed_act(turns) == "handle_objection"

    # The settle computation _on_shutdown runs (escalated derived from acts, not just the buffered
    # escalation flag nor the last turn) must land "escalated" — never None/in_progress.
    escalated_any = bool(agent.escalations) or "escalate" in acts
    terminal = w._terminal_outcome_on_disconnect(
        turn_count=len(turns),
        escalated=escalated_any,
        committed_tier=agent.last_close_tier,
        last_act=w._last_committed_act(turns),
        disqualified="disqualify" in acts,
    )
    assert terminal == "escalated", f"escalated-then-continued disconnect must settle escalated, got {terminal!r}"


async def test_no_close_disconnect_settles_released_not_in_progress():
    """A substantive call with NO close/escalate/disqualify that disconnects settles "released" (not
    in_progress) — the CB-22 guarantee that EVERY disconnected call lands terminal, generalized."""
    w = _worker()
    agent = _make_worker_agent(llm=_agent_mock(act="ask", target_slot="goal"))
    await agent.on_user_turn_completed(None, _StubMessage("Hi, tell me about tutoring."))
    await _drain_llm_node(agent)

    turns = agent.voice_session.state.turns
    acts = w._agent_acts(turns)
    terminal = w._terminal_outcome_on_disconnect(
        turn_count=len(turns),
        escalated=bool(agent.escalations) or "escalate" in acts,
        committed_tier=agent.last_close_tier,
        last_act=w._last_committed_act(turns),
        disqualified="disqualify" in acts,
    )
    assert terminal == "released"


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


class _StubJob:
    """A LiveKit agent Job stub carrying the explicit-dispatch metadata (ctx.job.metadata)."""
    def __init__(self, metadata: Optional[str] = None) -> None:
        self.metadata = metadata


class _StubCtx:
    def __init__(self, room: _StubRoom, job: Optional[_StubJob] = None) -> None:
        self.room = room
        self.job = job


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


# =============================== CONSENT SEEDING FROM ROOM METADATA ==============================
# The deadlock fix: the browser captured consent via /api/consent/respond BEFORE requesting a
# LiveKit token, and the token stamped that consent onto the room metadata. The worker SEEDS its
# fresh ConsentGate from that metadata (via PUBLIC gate methods) so can_converse is True from the
# start and the brain answers — instead of blocking forever on a `pending` gate. With NO consent
# metadata (a future direct SIP call), the gate stays fail-closed (the safety property never
# regresses).


def _consent_meta(state="ready", recording_granted=True, conversable=True,
                  jurisdiction="ca", phone_hash="abc123"):
    """Build the room-metadata JSON the token stamps + the worker reads (matches the demo helper)."""
    return json.dumps({
        "consent_state": state,
        "recording_granted": recording_granted,
        "jurisdiction": jurisdiction,
        "phone_hash": phone_hash,
        "conversable": conversable,
    })


def test_seed_gate_from_metadata_recording_granted_opens_ready():
    """Recording granted in the captured consent -> the worker seeds the gate to READY: can_converse
    AND can_record are True from the start (no spoken-consent deadlock; recording stays honest)."""
    from src.voice import worker as w

    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    seeded = w._seed_gate_from_metadata(gate, _StubCtx(_StubRoom(metadata=_consent_meta())))
    assert seeded is True
    assert gate.can_converse is True
    assert gate.can_record is True
    assert gate.recorded is True


def test_seed_gate_from_metadata_recording_refused_opens_unrecorded():
    """Recording refused but the call proceeds (conversable) -> the worker seeds UNRECORDED:
    can_converse is True but can_record is False (recorded stays honest = not recording)."""
    from src.voice import worker as w

    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    meta = _consent_meta(state="unrecorded", recording_granted=False, conversable=True)
    seeded = w._seed_gate_from_metadata(gate, _StubCtx(_StubRoom(metadata=meta)))
    assert seeded is True
    assert gate.can_converse is True
    assert gate.can_record is False
    assert gate.recorded is False


def test_seed_gate_from_metadata_minor_unresolved_stays_blocked():
    """A captured minor gate that is NOT conversable (need_parental, unresolved) -> the worker does
    NOT open the gate: can_converse stays False (a suspected minor may not proceed unsupervised)."""
    from src.voice import worker as w

    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    meta = _consent_meta(state="need_parental", recording_granted=True, conversable=False)
    w._seed_gate_from_metadata(gate, _StubCtx(_StubRoom(metadata=meta)))
    assert gate.can_converse is False


def test_seed_gate_from_metadata_absent_keeps_fail_closed():
    """NO consent metadata (a future direct SIP call) -> the worker leaves the gate UNTOUCHED and
    fail-closed: can_converse stays False so the safety property never regresses."""
    from src.voice import worker as w

    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    # Metadata present but carrying ONLY jurisdiction/phone (no consent_state/conversable keys).
    meta = json.dumps({"jurisdiction": "ca", "phone": "+15551234567"})
    seeded = w._seed_gate_from_metadata(gate, _StubCtx(_StubRoom(metadata=meta)))
    assert seeded is False
    assert gate.can_converse is False
    # Truly empty/garbage metadata also leaves it gated.
    g2 = ConsentGate(jurisdiction="ca")
    assert w._seed_gate_from_metadata(g2, _StubCtx(_StubRoom(metadata=None))) is False
    assert g2.can_converse is False


def test_seed_gate_from_metadata_reads_named_dispatch_job_metadata():
    """REGRESSION (named-dispatch live call ep-e9741935): with EXPLICIT agent dispatch the consent
    arrives on the JOB metadata (ctx.job.metadata), and LiveKit does NOT reliably copy
    RoomConfiguration.metadata onto the room. Reading ONLY room.metadata left seeded=False -> gate
    pending -> ConsentError on every user turn -> the agent never replied. The worker must seed from
    the dispatch job metadata when the room metadata is empty."""
    from src.voice import worker as w

    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    # Room metadata EMPTY (as observed live), consent carried on the dispatch's job metadata.
    ctx = _StubCtx(_StubRoom(metadata=None), job=_StubJob(metadata=_consent_meta()))
    seeded = w._seed_gate_from_metadata(gate, ctx)
    assert seeded is True
    assert gate.can_converse is True
    assert gate.can_record is True
    # And jurisdiction is read from the same merged source.
    assert w._room_jurisdiction(ctx) == "ca"


async def test_seeded_gate_lets_worker_turn_proceed_and_records():
    """END-TO-END (worker side): a gate SEEDED from `recording granted` metadata lets a worker turn
    PROCEED — on_user_turn_completed stashes the turn (no ConsentError), llm_node runs the brain +
    commits, and recorded is True. This is the deadlock fix surfaced through the real turn path."""
    from src.voice import worker as w

    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    w._seed_gate_from_metadata(gate, _StubCtx(_StubRoom(metadata=_consent_meta())))
    agent = _make_worker_agent(consent_gate=gate)

    await agent.on_user_turn_completed(None, _StubMessage("How much does it cost per month?"))
    sid = agent._pending_speech_id
    assert sid is not None
    await _drain_llm_node(agent)
    assert agent.voice_session.turn_for_speech_id(sid) is not None  # brain ran, turn captured
    assert agent.voice_session.recorded is True


async def test_seeded_unrecorded_gate_proceeds_but_not_recorded():
    """A gate SEEDED from `recording refused` (conversable) metadata lets a worker turn PROCEED but
    keeps recording honest: the turn commits, yet recorded is False (nothing is recorded)."""
    from src.voice import worker as w

    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    meta = _consent_meta(state="unrecorded", recording_granted=False, conversable=True)
    w._seed_gate_from_metadata(gate, _StubCtx(_StubRoom(metadata=meta)))
    agent = _make_worker_agent(consent_gate=gate)

    await agent.on_user_turn_completed(None, _StubMessage("Tell me more about tutoring."))
    sid = agent._pending_speech_id
    assert sid is not None
    await _drain_llm_node(agent)
    assert agent.voice_session.turn_for_speech_id(sid) is not None
    assert agent.voice_session.recorded is False


async def test_unseeded_gate_keeps_worker_fail_closed():
    """FAIL-CLOSED preserved: with NO consent metadata the gate stays `pending`, so the worker turn
    raises ConsentError and the brain never runs / nothing is buffered (no regression of the safety
    property for a future direct SIP call). CB-41: on_user_turn_completed itself fail-fast-checks the
    gate (raising BEFORE stashing), so the brain in llm_node is never reached / nothing is recorded."""
    from src.voice import worker as w

    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    # No consent metadata -> seeding is a no-op, gate stays pending.
    assert w._seed_gate_from_metadata(gate, _StubCtx(_StubRoom(metadata=None))) is False
    agent = _make_worker_agent(consent_gate=gate)

    # CB-41: the fail-fast happens in on_user_turn_completed (before any stash), so the brain in
    # llm_node is never reached and no user text is stashed.
    with pytest.raises(ConsentError):
        await agent.on_user_turn_completed(None, _StubMessage("How much does it cost?"))
    assert agent._pending_user_text is None
    # Direct handle_user_turn still fail-closes too (the gate is the source of truth).
    with pytest.raises(ConsentError):
        await agent.voice_session.handle_user_turn("How much does it cost?", "pre-consent")
    assert agent.voice_session.state.pending == {}
    assert agent.voice_session.recorded is False


# =============================== SIP SPOKEN-CONSENT: the classifier =============================
# A phone caller has NO browser to click consent, so the worker runs an IVR-style spoken consent
# before the brain. The classifier is the deterministic core: map a free-text consent reply to
# yes / no / unclear (keyword/affirmation-negation based, robust to filler/punctuation).


def test_classify_consent_reply_affirmatives():
    """Affirmations -> "yes": plain yes, "sure", "I consent", "go ahead", "yeah that's fine"."""
    from src.voice import worker as w

    for text in ("yes", "Yes.", "sure", "I consent", "go ahead",
                 "yeah, that's fine", "yep absolutely", "okay yes please", "of course"):
        assert w._classify_consent_reply(text) == "yes", text


def test_classify_consent_reply_negatives():
    """Negations -> "no": plain no, "I don't", "stop", "no thanks", "I do not consent"."""
    from src.voice import worker as w

    for text in ("no", "No.", "I don't", "stop", "no thanks",
                 "I do not consent", "nope", "please don't record", "I'd rather not"):
        assert w._classify_consent_reply(text) == "no", text


def test_classify_consent_reply_unclear():
    """Ambiguous / non-answers -> "unclear": "uh, what?", empty, a question, pure filler."""
    from src.voice import worker as w

    for text in ("uh, what?", "", "   ", "can you repeat that?", "hmm", "who is this"):
        assert w._classify_consent_reply(text) == "unclear", text


# =============================== SIP PATH DETECTION (participant kind) ===========================
# The SIP path is a call where _seed_gate_from_metadata returned False AND a remote participant is a
# SIP caller (ParticipantKind.PARTICIPANT_KIND_SIP == 3). The browser/web path is NOT a SIP call.


class _StubKindParticipant:
    """A remote participant exposing a numeric `.kind` (3 == SIP) like livekit.rtc.RemoteParticipant."""

    def __init__(self, kind: int, attributes: Optional[dict[str, str]] = None) -> None:
        self.kind = kind
        self.attributes = attributes or {}


def test_is_sip_call_true_for_sip_participant():
    """_is_sip_call is True when a remote participant's kind == PARTICIPANT_KIND_SIP (3)."""
    from src.voice import worker as w

    room = _StubRoom(remote_participants={"caller": _StubKindParticipant(3)})
    assert w._is_sip_call(_StubCtx(room)) is True


def test_is_sip_call_false_for_standard_participant():
    """_is_sip_call is False for a standard (browser) participant (kind 0) or no participants."""
    from src.voice import worker as w

    web_room = _StubRoom(remote_participants={"web": _StubKindParticipant(0)})
    assert w._is_sip_call(_StubCtx(web_room)) is False
    assert w._is_sip_call(_StubCtx(_StubRoom(remote_participants={}))) is False


# =============================== SIP SPOKEN-CONSENT SUB-FLOW (the IVR loop) ======================
# The load-bearing new behavior: on a SIP call (no browser-captured consent) the worker arms an
# _awaiting_consent state. The FIRST user turn(s) are intercepted by handle_consent_turn (NOT run
# through the brain): "yes" -> grant (ready + recorded), "no" -> refuse (unrecorded/ended per
# policy), a minor reply -> flag_minor (need_parental, blocked), unclear -> re-ask once then end.
# Once the gate reaches can_converse, on_user_turn_completed routes subsequent turns to the brain.


def _sip_agent(refusal_policy: RefusalPolicy = RefusalPolicy.PROCEED_UNRECORDED,
               *, llm: Optional[MockLLMClient] = None):
    """A worker agent armed for the SIP spoken-consent flow: a fresh (pending) gate + _awaiting_consent
    set, exactly as entrypoint arms it when _seed_gate_from_metadata returned False on a SIP call."""
    gate = ConsentGate(jurisdiction="ca", refusal_policy=refusal_policy)
    agent = _make_worker_agent(consent_gate=gate, llm=llm)
    agent.arm_spoken_consent()
    return agent, gate


async def test_sip_consent_yes_grants_and_records():
    """"yes" on the intercepted first turn -> gate reaches can_converse AND recorded; the brain never
    ran on the consent turn (no pending turn / no transcript captured for it)."""
    agent, gate = _sip_agent()
    assert agent._awaiting_consent is True

    prompt = await agent.handle_consent_turn("Yes, that's fine.")

    assert gate.can_converse is True
    assert gate.can_record is True
    assert agent.voice_session.recorded is True
    assert agent._awaiting_consent is False
    # The consent turn was NOT run through the brain: nothing buffered, nothing captured.
    assert agent.voice_session.state.pending == {}
    assert agent.voice_session.state.turns == []
    assert prompt is not None  # a spoken hand-off line is returned


async def test_sip_consent_no_proceeds_unrecorded():
    """"no" with PROCEED_UNRECORDED -> can_converse True but recorded False (the call continues
    unrecorded); the consent turn never hit the brain."""
    agent, gate = _sip_agent(RefusalPolicy.PROCEED_UNRECORDED)

    await agent.handle_consent_turn("No, please don't record.")

    assert gate.can_converse is True
    assert agent.voice_session.recorded is False
    assert agent._awaiting_consent is False
    assert agent.voice_session.state.pending == {}


async def test_sip_consent_no_ends_under_end_policy():
    """"no" with the END refusal policy -> the gate ends (NOT conversable); the call is over."""
    agent, gate = _sip_agent(RefusalPolicy.END)

    await agent.handle_consent_turn("No.")

    assert gate.state == "ended"
    assert gate.can_converse is False
    assert agent._awaiting_consent is False


async def test_sip_consent_minor_blocks_need_parental():
    """A minor-indicating consent/early reply -> flag_minor -> need_parental (BLOCKED): can_converse
    stays False (a suspected minor may not proceed unsupervised), even if they said "yes"."""
    agent, gate = _sip_agent()

    await agent.handle_consent_turn("Yes, I'm in 9th grade and calling for myself.")

    assert gate.state == "need_parental"
    assert gate.can_converse is False
    assert agent._awaiting_consent is False


async def test_sip_consent_unclear_reasks_once_then_ends():
    """Unclear reply -> re-ask ONCE (still awaiting, gate not advanced); a second unclear reply ->
    politely END (no infinite loop, no deadlock)."""
    agent, gate = _sip_agent()

    first = await agent.handle_consent_turn("uh, what?")
    assert agent._awaiting_consent is True  # still capturing consent after one unclear reply
    assert gate.can_converse is False
    assert first is not None  # a re-ask prompt

    await agent.handle_consent_turn("hmm")
    assert gate.state == "ended"
    assert agent._awaiting_consent is False


async def test_sip_first_turn_intercepted_then_brain_runs():
    """END-TO-END (SIP path) through on_user_turn_completed: the FIRST user turn is intercepted as the
    consent answer (NOT run through the brain), and AFTER "yes" a subsequent turn DOES run the brain."""
    agent, gate = _sip_agent()

    # First turn = the consent answer. Intercepted: no pending turn, no transcript.
    await agent.on_user_turn_completed(None, _StubMessage("Yes, go ahead."))
    assert gate.can_converse is True
    assert agent._awaiting_consent is False
    assert agent.voice_session.state.pending == {}
    assert agent._pending_speech_id is None  # no speculative brain turn for the consent answer

    # Next turn flows to the brain (consent resolved): on_user_turn_completed stashes, llm_node runs
    # the brain + commits.
    await agent.on_user_turn_completed(None, _StubMessage("How much does it cost per month?"))
    sid = agent._pending_speech_id
    assert sid is not None
    await _drain_llm_node(agent)
    assert agent.voice_session.turn_for_speech_id(sid) is not None


async def test_sip_consent_does_not_arm_for_web_path():
    """REGRESSION: a metadata-seeded (browser) gate is NOT armed for spoken consent — a web call
    proceeds to the brain on the first turn with no IVR detour (the web-voice path is unchanged)."""
    from src.voice import worker as w

    gate = ConsentGate(jurisdiction="ca", refusal_policy=RefusalPolicy.PROCEED_UNRECORDED)
    w._seed_gate_from_metadata(gate, _StubCtx(_StubRoom(metadata=_consent_meta())))
    agent = _make_worker_agent(consent_gate=gate)
    # The web path NEVER calls arm_spoken_consent(), so _awaiting_consent stays False.
    assert agent._awaiting_consent is False

    await agent.on_user_turn_completed(None, _StubMessage("How much does it cost per month?"))
    sid = agent._pending_speech_id
    assert sid is not None  # stashed (no IVR detour); brain runs in llm_node
    await _drain_llm_node(agent)
    assert agent.voice_session.turn_for_speech_id(sid) is not None  # brain ran
    assert agent.voice_session.recorded is True


# =============================== LIVE REPLY-PIPELINE WIRING (the dead-air fix) ===================
# These require livekit-agents installed (the voice extra); they SKIP in the offline suite/CI where
# livekit is absent. They pin THE live-call "consent works, then dead silence on every answer" bug:
# livekit-agents skips reply generation entirely when AgentSession.llm is None (agent_activity:
# `elif self.llm is None: return`), so the brain's buffered reply (surfaced by llm_node) was never
# spoken. The 322 offline tests missed it precisely because they never exercise livekit's pipeline.


def test_passthrough_llm_is_a_valid_livekit_llm():
    """_passthrough_llm() must be an instance of livekit's llm.LLM so `AgentSession.llm is None` is
    False — that is the SOLE reason it exists (the brain authors every reply; this llm is inert)."""
    pytest.importorskip("livekit.agents")
    from livekit.agents import llm as lkllm

    w = _worker()
    passthrough = w._passthrough_llm()
    assert isinstance(passthrough, lkllm.LLM)  # satisfies the `self.llm is None` reply-pipeline gate


async def test_make_session_wires_non_none_llm_so_replies_are_spoken():
    """THE regression guard: _make_session must give the AgentSession a non-None llm. A bare
    AgentSession() leaves .llm None — the exact condition under which livekit SILENTLY drops every
    brain reply. If someone removes the llm wiring again, this fails instead of going live-silent.
    (Async so a running event loop exists — AgentSession.__init__ calls asyncio.get_event_loop.)"""
    pytest.importorskip("livekit.agents")
    from livekit.agents import AgentSession

    w = _worker()
    # The bug shape: no llm -> livekit skips reply generation (llm_node never called).
    assert AgentSession().llm is None
    # The fix: the worker's session-builder always wires the inert pass-through llm.
    session = w._make_session(None, None, None, version="champion_v0", kb_version="kb_v0")
    assert session.llm is not None


async def test_passthrough_llm_chat_is_inert_well_formed_stream():
    """Defensive: although llm_node fully overrides generation (so chat() is never reached in prod),
    the stub's chat() must still return a well-formed, EMPTY livekit LLMStream — if some edge path
    ever invokes it, the turn degrades to no extra tokens rather than crashing."""
    pytest.importorskip("livekit.agents")
    from livekit.agents import llm as lkllm

    w = _worker()
    stream = w._passthrough_llm().chat(chat_ctx=lkllm.ChatContext.empty())
    assert isinstance(stream, lkllm.LLMStream)
    chunks = [c async for c in stream]
    assert chunks == []  # inert: emits nothing


def test_make_session_disables_preemptive_generation(monkeypatch):
    """Regression for the first real-phone-call bug: preemptive_generation MUST be False. With it ON,
    livekit creates the reply SpeechHandle BEFORE on_user_turn_completed sets _pending_speech_id, so
    _wire_commit_on_speech sees None and never attaches the done-callback -> commit_turn never fires ->
    NO voice turns persist (a real call saved an empty in_progress episode that never reached Calls,
    and every brain turn logged turn=1 because the committed belief never advanced)."""
    pytest.importorskip("livekit.agents")
    import src.voice.worker as w

    captured: dict = {}

    class _CapSession:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(w, "AgentSession", _CapSession)
    w._make_session(None, None, None, version="v", kb_version="k")
    assert captured.get("preemptive_generation") is False
    assert captured.get("llm") is not None  # the pass-through llm (dead-air fix) must remain

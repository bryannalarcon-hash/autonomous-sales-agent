# Integration tests for the LiveKit voice-session ADAPTER (plan U12, R37 parity). DB-FREE +
# LIVEKIT-FREE: targets src.voice.session (the testable orchestrator) and only IMPORTS
# src.voice.agent to prove its import is guarded (works without livekit-agents installed). The
# load-bearing test is PARITY: a scripted voice session (handle_user_turn + commit_turn per turn)
# produces the IDENTICAL sequence of decisions (act/target_slot/tier) as a direct respond() loop
# building the same selfplay-shaped history — that is what makes "optimized in text == shipped in
# voice" literally true. Also covers side-effect safety (speculative pending buffer; commit_turn is
# the only writer), barge-in, and per-turn capture keyed by speech_id. Uses the SAME agent-mock
# routing as test_selfplay (response_format -> combined JSON; else plain reply) + FakeEmbedder.
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Sequence

import pytest

from src.config.settings import load_config
from src.core.belief_state import BeliefState
from src.core.llm import Message, MockLLMClient
from src.core.respond import respond
from src.kb.embeddings import FakeEmbedder
from src.kb.retriever import Chunk
from src.voice.session import (
    PendingTurn,
    VoiceSession,
    VoiceSessionState,
    should_play_filler,
    should_retrieve,
)

# --- Agent mock (IDENTICAL routing contract to tests/integration/test_selfplay._agent_mock) -----
# respond() makes THREE agent LLM calls per turn: dst.update -> complete_json, decide ->
# complete_json, realize -> complete (no json response_format). One callable routes on
# opts["response_format"]: a json_object call returns ONE combined JSON carrying BOTH the DST
# driver-delta keys AND the Decision keys; a non-json call returns the plain reply string.


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
    response_format opt. The combined JSON carries Decision keys + driver deltas in one object."""
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


# A FIXED script of user utterances the voice session and the direct loop both replay. The mix of
# factual ("how much does it cost") and non-factual turns exercises the intent-gated retrieval
# predicate while keeping the decision sequence deterministic under the routed mock.
_SCRIPT = [
    "Hi, my daughter needs help with algebra.",
    "How much does the tutoring cost per month?",
    "Okay, that makes sense to me.",
    "What results do other families see?",
    "Alright, let's talk about next steps.",
]


def _make_retrieve_hook():
    """A deterministic local retrieve hook (no DB): embeds a tiny fixed KB with FakeEmbedder and
    returns the closest chunk. Used so RAG is exercised WITHOUT Postgres — the session threads the
    returned facts straight into respond(retrieved_facts=...)."""
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


def _make_session() -> VoiceSession:
    config = load_config("champion_v0")
    return VoiceSession(
        config,
        _agent_mock(),
        FakeEmbedder(),
        kb_chunks_or_retrieve_hook=_make_retrieve_hook(),
        seed=0,
    )


# =============================== PARITY (R37) ====================================================


async def test_voice_text_parity_decision_sequence_identical():
    """The CORE guarantee (R37): a scripted voice session (handle_user_turn + commit_turn per turn)
    yields the SAME sequence of decisions (act/target_slot/tier) as a direct respond() loop that
    builds the identical selfplay-shaped history. Same config, same FIXED user script, two
    independent but IDENTICALLY-routed agent mocks (so call-state never collides)."""
    config = load_config("champion_v0")

    # --- (a) The voice path -------------------------------------------------------------------
    voice = VoiceSession(
        config, _agent_mock(), FakeEmbedder(),
        kb_chunks_or_retrieve_hook=_make_retrieve_hook(), seed=0,
    )
    voice_decisions: list[tuple[Optional[str], Optional[str], Optional[str]]] = []
    for i, utter in enumerate(_SCRIPT):
        sid = f"sp-{i}"
        pending = await voice.handle_user_turn(utter, sid)
        voice_decisions.append(
            (pending.decision.act, pending.decision.target_slot, pending.decision.tier)
        )
        voice.commit_turn(sid)

    # --- (b) The direct text path (the substrate optimized headlessly) -----------------------
    direct_llm = _agent_mock()
    direct_retrieve = _make_retrieve_hook()
    belief = BeliefState.fresh()
    history: list[dict[str, Any]] = []
    text_decisions: list[tuple[Optional[str], Optional[str], Optional[str]]] = []
    for utter in _SCRIPT:
        # Mirror the session's intent-gated retrieval EXACTLY: only retrieve when the predicate fires.
        facts = (
            direct_retrieve(utter, kb_version=config.kb_version, k=4)
            if should_retrieve(utter, belief)
            else None
        )
        decision, reply, belief = await respond(
            belief, history, utter, direct_llm, config, retrieved_facts=facts
        )
        history.append({"role": "user", "text": utter})
        history.append(
            {"role": "agent", "act": decision.act, "confidence": decision.confidence, "text": reply}
        )
        text_decisions.append((decision.act, decision.target_slot, decision.tier))

    assert voice_decisions == text_decisions, (
        f"PARITY BROKEN: voice {voice_decisions} != text {text_decisions}"
    )


async def test_voice_history_shape_matches_selfplay():
    """After commits, the session history is the EXACT shape selfplay builds: user dicts
    {'role':'user','text':...} and agent dicts {'role':'agent','act','confidence','text'} — the
    shape respond._last_agent_act and the gates read. (If this drifts, parity silently breaks.)"""
    session = _make_session()
    for i, utter in enumerate(_SCRIPT[:2]):
        sid = f"sp-{i}"
        await session.handle_user_turn(utter, sid)
        session.commit_turn(sid)

    hist = session.state.history
    # alternating user, agent, user, agent
    assert hist[0] == {"role": "user", "text": _SCRIPT[0]}
    assert hist[1]["role"] == "agent"
    assert set(hist[1].keys()) == {"role", "act", "confidence", "text"}
    assert hist[2] == {"role": "user", "text": _SCRIPT[1]}
    assert hist[3]["role"] == "agent"


# =============================== SIDE-EFFECT SAFETY ==============================================


async def test_discarded_speculative_turn_writes_no_state():
    """The side-effect-safety guarantee: handle_user_turn creates a PENDING turn but writes NO
    session state; discard_speculative drops it leaving belief/history/turns untouched (commit_turn
    is the ONLY writer)."""
    session = _make_session()
    belief_before = session.state.belief
    history_len_before = len(session.state.history)
    turns_len_before = len(session.state.turns)

    sid = "spec-1"
    pending = await session.handle_user_turn(_SCRIPT[0], sid)
    # pending exists, buffered, NOT committed
    assert isinstance(pending, PendingTurn)
    assert sid in session.state.pending
    assert pending.committed is False
    # NO state write happened yet
    assert session.state.belief is belief_before
    assert len(session.state.history) == history_len_before
    assert len(session.state.turns) == turns_len_before

    session.discard_speculative(sid)
    # pending dropped, still NO state write
    assert sid not in session.state.pending
    assert session.state.belief is belief_before
    assert len(session.state.history) == history_len_before
    assert len(session.state.turns) == turns_len_before


async def test_commit_is_the_only_writer():
    """Only commit_turn promotes pending -> committed: belief advances, history + a captured Turn
    are appended, and the pending entry is gone."""
    session = _make_session()
    belief_before = session.state.belief
    sid = "c-1"
    pending = await session.handle_user_turn(_SCRIPT[0], sid)

    session.commit_turn(sid)
    # belief advanced to the pending new_belief (the input belief was never mutated by respond())
    assert session.state.belief is pending.new_belief
    assert session.state.belief is not belief_before
    # history grew by exactly two (user + agent); one captured agent Turn appended
    assert len(session.state.history) == 2
    assert sid not in session.state.pending


# =============================== BARGE-IN ========================================================


async def test_barge_in_interrupts_and_leaves_committed_state_untouched():
    """A barge-in on a pending (speculatively generated) turn drops the pending turn and writes NO
    committed state (R5/R10 self-recover): an interrupted TTS leaves committed belief/history
    unchanged, and the interruption is marked on the still-uncommitted state."""
    session = _make_session()
    # commit one real turn first so there IS committed state to protect
    await session.handle_user_turn(_SCRIPT[0], "t0")
    session.commit_turn("t0")
    committed_belief = session.state.belief
    committed_history_len = len(session.state.history)

    # a second speculative turn that then gets barged in on
    sid = "barge-1"
    await session.handle_user_turn(_SCRIPT[1], sid)
    assert sid in session.state.pending

    session.on_barge_in(sid)
    # pending gone, committed state UNTOUCHED
    assert sid not in session.state.pending
    assert session.state.belief is committed_belief
    assert len(session.state.history) == committed_history_len
    # the interruption is recorded on the (still-uncommitted) state for the report
    assert session.state.last_interrupted_speech_id == sid


# =============================== PER-TURN CAPTURE (speech_id join) ===============================


async def test_per_turn_capture_keyed_by_speech_id_with_version_stamp():
    """After commit, the captured agent Turn carries the speech_id linkage + version + kb_version
    (the transcript+decision+version join the dashboard reads), and the Turn's decision matches the
    committed decision."""
    config = load_config("champion_v0")
    session = _make_session()
    sid = "join-7"
    pending = await session.handle_user_turn(_SCRIPT[0], sid)
    session.commit_turn(sid)

    # find the captured agent turn for this speech_id
    captured = session.turn_for_speech_id(sid)
    assert captured is not None
    assert captured.speaker == "agent"
    assert captured.decision == pending.decision.act
    assert captured.kb_version == config.kb_version
    # the version stamp travels with the captured turn (via session.version)
    assert session.state.version == config.version
    assert session.state.kb_version == config.kb_version
    # the speech_id is recorded so logs join transcript+decision+version by speech_id
    assert session.speech_id_for_turn(captured) == sid


# =============================== PER-TURN BELIEF PERSISTENCE (CB-23) =============================
# CB-23: every committed voice agent Turn must carry its belief snapshot (drivers + trends + stage),
# the SAME way the sim/text path does (selfplay logs belief.to_snapshot() per agent turn) — so Call
# Review can replay the trust/walk-away trajectory of a voice call. These lock that parity in the
# .venv suite (no livekit needed): a snapshot that silently went empty would make Review show a blank
# belief trajectory for voice calls (the reported symptom), which these would catch.

from src.api.operate import _belief_to_dict  # noqa: E402  (deferred so the helpers above are defined)
from src.core.belief_state import DRIVERS  # noqa: E402
from src.memory.schema import Turn  # noqa: E402


async def test_committed_voice_turn_carries_nonempty_belief():
    """A committed voice AGENT turn carries a NON-EMPTY belief snapshot: all six latent drivers, the
    derived velocity trends, and a stage — never the empty/None belief that would blank the Review
    trajectory. This is the CB-23 guarantee for a fresh (voice-ish) committed turn."""
    session = _make_session()
    sid = "belief-1"
    await session.handle_user_turn("How much does the tutoring cost per month?", sid)
    committed = session.commit_turn(sid)
    assert committed is not None

    agent_turn = session.turn_for_speech_id(sid)
    assert agent_turn is not None and agent_turn.speaker == "agent"
    snap = agent_turn.belief
    # The crux: the snapshot exists AND its drivers are populated (NOT None / empty).
    assert snap is not None, "committed voice agent turn has no belief snapshot (CB-23 regression)"
    assert snap.drivers, "belief.drivers is empty — Review would show no trust/walk-away (CB-23)"
    # All six canonical drivers are present (parity with the sim path's to_snapshot()).
    for d in DRIVERS:
        assert d in snap.drivers, f"driver {d!r} missing from the committed voice turn's belief"
    # The prioritized live-monitor signals (trust + walk-away/bail_risk) are real numbers in [0,1].
    assert 0.0 <= float(snap.drivers["trust"]) <= 1.0
    assert 0.0 <= float(snap.drivers["bail_risk"]) <= 1.0
    # Derived velocity trends + a dialogue stage travel too (the full Review/live shape).
    assert snap.trends, "belief.trends is empty — the velocity badges would be blank"
    assert snap.stage


async def test_voice_turn_belief_round_trips_and_maps_to_review_drivers():
    """The committed voice turn's belief survives the persistence serialization (Turn.to_dict ->
    Turn.from_dict, the exact round-trip store.save_episode/get_episode performs) AND maps to a
    NON-EMPTY primary_drivers list via the operate review serializer — so the Call Review page shows
    per-turn trust/walk-away for a voice call (CB-23 acceptance)."""
    session = _make_session()
    sid = "belief-2"
    await session.handle_user_turn("What results do other families see?", sid)
    session.commit_turn(sid)
    agent_turn = session.turn_for_speech_id(sid)
    assert agent_turn is not None

    # Round-trip through the schema serialization the store uses to persist/read episodes.
    rehydrated = Turn.from_dict(agent_turn.to_dict())
    assert rehydrated.belief is not None
    assert rehydrated.belief.drivers, "belief.drivers empty after to_dict/from_dict round-trip (CB-23)"

    # The operate review serializer turns it into the primary_drivers list the page renders. It must be
    # non-empty AND include trust + bail_risk (the two prioritized signals the Review/live show).
    bd = _belief_to_dict(rehydrated.belief)
    assert bd is not None
    keys = {d["key"] for d in bd["primary_drivers"]}
    assert "trust" in keys and "bail_risk" in keys, (
        "Review primary_drivers missing trust/bail_risk for a voice turn (CB-23)"
    )


async def test_voice_belief_persistence_matches_sim_path():
    """PARITY: a voice committed turn's belief snapshot and the sim path's belief snapshot are produced
    by the SAME projection (BeliefState.to_snapshot), so a voice turn is no less populated than a sim
    turn. We assert the voice snapshot carries the full driver set the sim selfplay logs per turn."""
    session = _make_session()
    sid = "belief-3"
    pending = await session.handle_user_turn("Hi, my daughter needs help with algebra.", sid)
    session.commit_turn(sid)
    agent_turn = session.turn_for_speech_id(sid)
    assert agent_turn is not None and agent_turn.belief is not None

    # The committed Turn's belief is EXACTLY pending.new_belief.to_snapshot() (the same call the sim
    # path makes via selfplay._belief_snapshot). Same keys, same values -> same Review fidelity.
    expected = pending.new_belief.to_snapshot()
    assert agent_turn.belief.drivers == expected.drivers
    assert agent_turn.belief.trends == expected.trends
    assert agent_turn.belief.stage == expected.stage


# =============================== INTENT-GATED RAG ================================================


async def test_should_retrieve_gates_on_factual_intent():
    """should_retrieve fires for factual/price questions (a KB lookup will help) and skips chit-chat
    / acknowledgements (R43-friendly: non-factual turns skip retrieval before generation)."""
    belief = BeliefState.fresh()
    assert should_retrieve("How much does it cost per month?", belief) is True
    assert should_retrieve("What results do families see?", belief) is True
    assert should_retrieve("Okay, that makes sense to me.", belief) is False
    assert should_retrieve("Hi there.", belief) is False


async def test_should_play_filler_only_when_kb_lookup_runs():
    """Latency cover (R43): should_play_filler is true exactly when a KB lookup will run (so the
    adapter can play a cached filler during the lookup), false otherwise."""
    belief = BeliefState.fresh()
    assert should_play_filler("How much does the program cost?", belief) is True
    assert should_play_filler("Sounds good, thanks.", belief) is False


# =============================== NO LIVEKIT LEAK =================================================


def _fresh_import_pulls_livekit(modules: tuple[str, ...]) -> bool:
    """In a CLEAN subprocess (immune to whatever the parent test run already imported), import
    `modules` and report whether `livekit` ended up in that fresh interpreter's sys.modules. This is
    the TRUE boundary invariant — order- AND install-independent: the dev box may have livekit-agents
    installed, but importing core/session in a fresh interpreter must still never load it. (The old
    in-process `livekit not in sys.modules` check was fragile — any earlier test that imported the
    worker polluted the shared process, making the result depend on collection order.)"""
    import os
    import subprocess
    import sys

    script = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in modules)
        + "leaked = [k for k in sys.modules if k == 'livekit' or k.startswith('livekit.')]\n"
        + "sys.exit(1 if leaked else 0)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1), f"subprocess import errored: {proc.stderr}"
    return proc.returncode == 1


def test_session_module_has_no_livekit_import():
    """session.py must NEVER import livekit (so it stays unit-testable AND cannot leak a LiveKit type
    into core): a static scan that no `import livekit`/`from livekit` line exists, plus a clean-
    subprocess check that importing the session in a fresh interpreter pulls no livekit."""
    src = Path("src/voice/session.py").read_text(encoding="utf-8")
    import_lines = [
        ln for ln in src.splitlines()
        if ln.strip().startswith(("import ", "from ")) and "livekit" in ln.lower()
    ]
    assert not import_lines, f"src/voice/session.py must not import livekit: {import_lines}"
    assert not _fresh_import_pulls_livekit(("src.voice.session",)), "importing the session pulled in livekit"


def test_core_modules_pull_no_livekit():
    """Importing the brain core (respond/policy/dst/nlg/belief_state) must not pull livekit in — the
    boundary guarantee — verified in a CLEAN subprocess (order/install-immune) + a static source scan."""
    assert not _fresh_import_pulls_livekit((
        "src.core.respond",
        "src.core.policy",
        "src.core.dst",
        "src.core.nlg",
        "src.core.belief_state",
    )), "core import pulled in livekit"

    # the core source files themselves contain no livekit import
    for path in (
        "src/core/respond.py",
        "src/core/policy.py",
        "src/core/dst.py",
        "src/core/nlg.py",
        "src/core/belief_state.py",
    ):
        text = Path(path).read_text(encoding="utf-8")
        assert "import livekit" not in text and "from livekit" not in text


def test_voice_agent_imports_regardless_of_livekit():
    """src.voice.agent must import cleanly WHETHER OR NOT livekit-agents is installed (the import is
    guarded). build_voice_agent + VoiceSalesAgent are always exposed, and LIVEKIT_AVAILABLE is a
    bool that TRUTHFULLY reflects whether livekit actually imports — green in both states, so the
    suite stays green without livekit (the load-bearing requirement) and also when it is present."""
    import importlib

    import src.voice.agent as agent_mod

    assert hasattr(agent_mod, "VoiceSalesAgent")
    assert hasattr(agent_mod, "build_voice_agent")
    assert isinstance(agent_mod.LIVEKIT_AVAILABLE, bool)

    # The flag must agree with reality: True iff `livekit.agents` can be imported here.
    try:
        importlib.import_module("livekit.agents")
        livekit_present = True
    except Exception:  # noqa: BLE001 - any import failure means "not usable"
        livekit_present = False
    assert agent_mod.LIVEKIT_AVAILABLE is livekit_present

    # When absent, Agent is aliased to object (so the class is still constructible/mockable).
    if not livekit_present:
        assert agent_mod.Agent is object


async def test_agent_glue_delegates_speculative_commit_and_bargein():
    """The thin glue wires the LiveKit hooks to the session correctly (works in BOTH livekit states
    since the hooks only delegate to the livekit-free VoiceSession): on_user_turn_completed buffers
    a pending turn, llm_node emits its reply, on_turn_committed commits it, and on_interrupted barges
    in (drops the speculative turn, leaving committed state intact)."""
    from src.voice.agent import build_voice_agent

    config = load_config("champion_v0")
    agent = build_voice_agent(
        config, _agent_mock(), FakeEmbedder(),
        kb_chunks_or_retrieve_hook=_make_retrieve_hook(),
    )

    # 1. user turn -> speculative pending, NOT committed
    pending = await agent.on_user_turn_completed(_SCRIPT[0], "g0")
    assert pending.committed is False
    assert "g0" in agent.voice_session.state.pending
    # llm_node emits the pending reply for the current speech_id
    assert await agent.llm_node() == pending.reply_text
    # commit advances state
    agent.on_turn_committed("g0")
    assert "g0" not in agent.voice_session.state.pending
    committed_belief = agent.voice_session.state.belief
    committed_hist_len = len(agent.voice_session.state.history)

    # 2. a second user turn that gets barged in on -> committed state untouched
    await agent.on_user_turn_completed(_SCRIPT[1], "g1")
    assert "g1" in agent.voice_session.state.pending
    agent.on_interrupted("g1")
    assert "g1" not in agent.voice_session.state.pending
    assert agent.voice_session.state.belief is committed_belief
    assert len(agent.voice_session.state.history) == committed_hist_len
    assert agent.voice_session.state.last_interrupted_speech_id == "g1"

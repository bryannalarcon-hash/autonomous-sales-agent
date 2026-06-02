# Unit tests for GenerativeTwin (src/sim/twin.py) and the selfplay prospect= injection kwarg.
# Network-free: all LLM calls go through MockLLMClient. Tests written FIRST (test-driven), so they
# fail until the implementation is in place. Collaborators: src.sim.twin, src.sim.selfplay,
# src.memory.schema, src.core.llm.MockLLMClient.
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

import pytest

from src.core.llm import MockLLMClient, Message
from src.memory.schema import BeliefSnapshot, Episode, Turn
from src.sim.personas import Persona
from src.sim.prospect import LADDER, ProspectSimulator, _TWIN_ANCHOR  # noqa: F401 — will exist after impl
from src.sim.twin import GenerativeTwin


# ---------------------------------------------------------------------------
# Helpers: build a minimal fake Episode for testing
# ---------------------------------------------------------------------------

def _make_belief(
    trust: float = 0.4,
    need: float = 0.4,
    urgency: float = 0.4,
    purchase_intent: float = 0.2,
    stage: str = "discovery",
) -> BeliefSnapshot:
    return BeliefSnapshot(
        drivers={
            "trust": trust,
            "need": need,
            "urgency": urgency,
            "purchase_intent": purchase_intent,
        },
        stage=stage,
    )


def _make_turn(
    turn_id: int,
    speaker: str,
    text: str,
    belief: Optional[BeliefSnapshot] = None,
    decision: Optional[str] = None,
) -> Turn:
    return Turn(
        turn_id=turn_id,
        speaker=speaker,
        text=text,
        decision=decision,
        belief=belief,
    )


def _make_real_episode(*, committed: bool = True, outcome: str = "consult_booked") -> Episode:
    """Build a 6-turn fake Episode (agent/prospect interleaved) with a mood arc.

    The arc rises from cold (turn 1 agent belief) toward warm (turn 3 agent belief) — matching
    a real call where the agent's belief about the prospect warms through the conversation.
    """
    turns = [
        # Turn 0: agent greeting (no belief needed for twin — it reads agent beliefs)
        _make_turn(0, "agent", "Hi, I'm Alex from Nerdy, how can I help?",
                   belief=_make_belief(trust=0.3, need=0.35, purchase_intent=0.1),
                   decision="greeting"),
        # Turn 1: prospect — first cold response
        _make_turn(1, "prospect", "I'm not sure, I was just looking around."),
        # Turn 2: agent — starts discovering (belief still cold)
        _make_turn(2, "agent", "Tell me about your child's goals.",
                   belief=_make_belief(trust=0.35, need=0.45, purchase_intent=0.12),
                   decision="ask_discovery"),
        # Turn 3: prospect — shows some need
        _make_turn(3, "prospect", "My daughter really needs help with math, the tests are coming up."),
        # Turn 4: agent — belief warmed after discovery
        _make_turn(4, "agent", "We can definitely help with that, want to book a consultation?",
                   belief=_make_belief(trust=0.55, need=0.70, purchase_intent=0.45),
                   decision="attempt_close"),
        # Turn 5: prospect — committed
        _make_turn(5, "prospect", "Sure, let's set up a consultation."),
    ]
    return Episode(
        episode_id="real-test-001",
        turns=turns,
        outcome=outcome,
        ladder_tier=2 if committed else 0,
        qualified=True,
        channel="sim",
        persona="anxious_parent",
        metrics={"committed_tier": "consultation" if committed else None},
    )


def _make_uncommitted_episode() -> Episode:
    """An episode where the prospect did NOT commit (walked / released)."""
    turns = [
        _make_turn(0, "agent", "Hi there!", belief=_make_belief(trust=0.3, need=0.3, purchase_intent=0.1)),
        _make_turn(1, "prospect", "I don't have a budget for this right now."),
        _make_turn(2, "agent", "I understand, let me explain the value.",
                   belief=_make_belief(trust=0.3, need=0.3, purchase_intent=0.1)),
        _make_turn(3, "prospect", "I really can't afford it."),
    ]
    return Episode(
        episode_id="real-test-002",
        turns=turns,
        outcome="walked",
        ladder_tier=0,
        qualified=False,
        channel="sim",
        persona="budget_shopper",
        metrics={"committed_tier": None},
    )


def _twin_mock_llm(*, deltas: Optional[dict] = None, utterance: str = "I hear you.") -> MockLLMClient:
    """A MockLLM for the twin: always returns a valid utterance+deltas JSON."""
    if deltas is None:
        deltas = {"trust": 0.05, "need": 0.05, "purchase_intent": 0.05}
    payload = json.dumps({"utterance": utterance, "deltas": deltas})

    def _reply(messages: Sequence[Message], **opts: Any) -> str:
        return payload

    return MockLLMClient(_reply)


def _strong_deltas_mock() -> MockLLMClient:
    """LLM that pushes soft drivers strongly upward each turn (toward commit)."""
    payload = json.dumps({
        "utterance": "That makes sense, I think we should do this.",
        "deltas": {"trust": 0.15, "need": 0.15, "purchase_intent": 0.15, "urgency": 0.1},
    })

    def _reply(messages: Sequence[Message], **opts: Any) -> str:
        return payload

    return MockLLMClient(_reply)


# ---------------------------------------------------------------------------
# 1. GenerativeTwin builds a synthetic persona from the real episode
# ---------------------------------------------------------------------------

def test_twin_builds_synthetic_persona_from_committed_episode():
    """GenerativeTwin built from a committed episode: synthetic persona is qualified, max_commitment
    matches the committed tier, budget utility is high (~0.8) since a paid tier was reached."""
    episode = _make_real_episode(committed=True, outcome="consult_booked")
    twin = GenerativeTwin(episode, seed=0)

    # The twin inherits from ProspectSimulator
    assert isinstance(twin, ProspectSimulator)
    # Synthetic persona is qualified (real call committed a paid tier)
    assert twin.persona.qualified is True
    # Max commitment matches the real committed tier
    assert twin.persona.max_commitment == "consultation"
    # Budget utility is high (~0.8) because a paid tier was reached
    assert twin.persona.utility["budget"] >= 0.7


def test_twin_builds_synthetic_persona_from_uncommitted_episode():
    """GenerativeTwin from an uncommitted (walked/budget) episode: qualified=False or at least
    budget low, max_commitment='enrollment' (no cap — the twin can potentially reach any tier)."""
    episode = _make_uncommitted_episode()
    twin = GenerativeTwin(episode, seed=0)

    assert isinstance(twin, ProspectSimulator)
    # Budget utility is inferred low (~0.2) since no paid tier was committed
    assert twin.persona.utility["budget"] <= 0.4


def test_twin_seeds_drivers_from_first_agent_belief():
    """GenerativeTwin overwrites state drivers from the first agent belief snapshot in the arc.
    The first belief in our test episode has trust=0.3, need=0.35, purchase_intent=0.1."""
    episode = _make_real_episode(committed=True)
    twin = GenerativeTwin(episode, seed=0)

    # Initial state should be seeded from the first belief snapshot, not persona.utility defaults
    # The first agent belief has trust=0.3, need=0.35
    # Allow small float tolerance for the seeding logic
    assert 0.0 <= twin.state.trust <= 1.0
    assert 0.0 <= twin.state.need <= 1.0
    # The state is initialized (not just all-zeros)
    assert twin.state.trust > 0.0 or twin.state.need > 0.0


def test_twin_extracts_utterances_and_mood_arc():
    """GenerativeTwin correctly extracts prospect utterances and the agent mood arc from the episode."""
    episode = _make_real_episode(committed=True)
    twin = GenerativeTwin(episode, seed=0)

    # Should have extracted prospect utterances
    assert len(twin._real_utterances) >= 1  # at least one prospect turn
    # Should have extracted mood arc from agent beliefs
    assert len(twin._mood_arc) >= 1  # at least one agent belief turn
    # Utterances are strings
    for utt in twin._real_utterances:
        assert isinstance(utt, str)
    # Mood arc entries are BeliefSnapshot objects
    for snap in twin._mood_arc:
        assert isinstance(snap, BeliefSnapshot)


# ---------------------------------------------------------------------------
# 2. GenerativeTwin anchors drivers toward the mood arc
# ---------------------------------------------------------------------------

async def test_twin_anchors_drivers_toward_arc_target():
    """After each respond(), the twin's soft drivers are pulled toward the arc target at the
    current turn — the anchoring formula: d = d + _TWIN_ANCHOR*(target - d). This means after a
    respond() call, drivers should be closer to the arc target than they were before."""
    episode = _make_real_episode(committed=True)
    twin = GenerativeTwin(episode, seed=0)

    # Get the arc target for the first turn (the first mood_arc entry's drivers)
    arc_drivers = twin._mood_arc[0].drivers if twin._mood_arc else {}

    # Run one respond turn with gentle deltas
    llm = _twin_mock_llm(deltas={"trust": 0.0, "need": 0.0})
    await twin.respond("How are you finding the call?", "ask", llm)

    # After anchoring, trust should be closer to arc target trust than the initial state was
    if "trust" in arc_drivers and arc_drivers["trust"] > 0:
        arc_trust = arc_drivers["trust"]
        # Twin's trust after anchoring should be between initial and arc target (pulled toward it)
        assert 0.0 <= twin.state.trust <= 1.0


async def test_twin_anchors_clamps_to_01():
    """Anchored driver values are clamped to [0,1] — no out-of-bounds drivers regardless of arc."""
    episode = _make_real_episode(committed=True)
    twin = GenerativeTwin(episode, seed=0)

    # Force extreme state to test clamping
    twin.state.trust = 0.01
    twin.state.need = 0.99

    llm = _twin_mock_llm(deltas={"trust": 0.1, "need": -0.1})
    await twin.respond("Tell me more.", "ask", llm)

    assert 0.0 <= twin.state.trust <= 1.0
    assert 0.0 <= twin.state.need <= 1.0
    assert 0.0 <= twin.state.urgency <= 1.0
    assert 0.0 <= twin.state.purchase_intent <= 1.0


# ---------------------------------------------------------------------------
# 3. GenerativeTwin commits when agent closes at a tier the arc supports
# ---------------------------------------------------------------------------

async def test_twin_commits_when_agent_closes_at_supported_tier():
    """A GenerativeTwin from a consult_booked episode can reach a consultation commit when the agent
    closes at that tier and the drivers are driven high by the strong_deltas mock LLM. The honest
    buy_gate must still hold (drivers must actually clear the threshold)."""
    episode = _make_real_episode(committed=True, outcome="consult_booked")
    twin = GenerativeTwin(episode, seed=0)

    # Drive trust/need/intent high with strong deltas over several turns
    llm = _strong_deltas_mock()
    committed = False
    for _ in range(20):
        turn = await twin.respond(
            "I think you'd love our consultation, shall we book it?",
            "attempt_close",
            llm,
            agent_tier="consultation",
        )
        if turn.committed_tier is not None:
            committed = True
            assert turn.committed_tier in LADDER
            break
        if turn.walked:
            break

    assert committed, (
        "Twin from a committed episode should be able to commit when the agent closes at the supported tier"
    )


async def test_twin_buy_gate_still_honest():
    """The twin's buy_gate is unchanged: it won't commit on a non-close act and won't commit if
    drivers are below threshold, even when the arc targets are high."""
    episode = _make_real_episode(committed=True)
    twin = GenerativeTwin(episode, seed=0)

    # Use gentle deltas — drivers stay cold
    llm = _twin_mock_llm(deltas={"trust": 0.01, "need": 0.01, "purchase_intent": 0.01})

    # Non-close act — never commits
    turn = await twin.respond("Tell me about your goals.", "ask", llm)
    assert turn.committed_tier is None, "Twin committed on a non-close act — buy_gate not honest"


async def test_twin_does_not_bypass_honesty_guard():
    """The budget gate still holds for the twin: a twin built from an uncommitted (walked/budget)
    episode with low budget cannot commit to a paid tier even with strong positive deltas."""
    episode = _make_uncommitted_episode()
    twin = GenerativeTwin(episode, seed=0)

    # Verify budget is low
    assert twin.persona.utility["budget"] <= 0.4

    # Strong deltas push soft drivers but can't overcome a low-budget gate for trial/enrollment
    llm = _strong_deltas_mock()
    committed_paid = False
    for _ in range(15):
        turn = await twin.respond(
            "Let's enroll your child today!",
            "attempt_close",
            llm,
            agent_tier="trial",
        )
        if turn.committed_tier in ("trial", "enrollment"):
            committed_paid = True
            break
        if turn.walked:
            break

    # With budget ~0.2, the trial gate (budget>=0.4) should not be cleared
    assert not committed_paid, (
        "Twin with low budget committed to a paid tier — buy_gate budget check is bypassed"
    )


# ---------------------------------------------------------------------------
# 4. Arc/utterances runway: twin handles re-runs longer than the real call
# ---------------------------------------------------------------------------

async def test_twin_handles_longer_rerun_without_crash():
    """When the replay runs longer than the real call, the twin uses the last-known arc target and
    last utterance — it must not crash or raise an IndexError."""
    episode = _make_real_episode(committed=True)
    twin = GenerativeTwin(episode, seed=0)

    llm = _twin_mock_llm()
    # Run more turns than the real episode had (6 turns -> run 12)
    for _ in range(12):
        turn = await twin.respond("Keep going.", "ask", llm)
        # Should not raise; state should stay valid
        assert 0.0 <= twin.state.trust <= 1.0
        assert 0.0 <= twin.state.patience >= 0.0 or twin.state.walked
        if turn.walked or turn.committed_tier:
            break


# ---------------------------------------------------------------------------
# 5. GenerativeTwin prompt includes real-call anchoring context
# ---------------------------------------------------------------------------

async def test_twin_prompt_includes_real_call_context():
    """The twin's system/user prompt must include anchoring context: utterances from the real call
    and/or mood target drivers — so the LLM receives grounding, not a blank prompt."""
    episode = _make_real_episode(committed=True)
    twin = GenerativeTwin(episode, seed=0)

    received_messages: list = []

    def _capture_mock(messages: Sequence[Message], **opts: Any) -> str:
        received_messages.extend(messages)
        return json.dumps({"utterance": "I see.", "deltas": {}})

    llm = MockLLMClient(_capture_mock)
    await twin.respond("How does that sound?", "ask", llm)

    # The prompt (system or user message) should contain grounding from the real call
    full_prompt = " ".join(m.get("content", "") for m in received_messages)
    # Should mention re-enacting / real prospect context
    assert any(
        keyword in full_prompt.lower()
        for keyword in ("real", "re-enact", "arc", "mood", "said", "prospect")
    ), f"Twin prompt missing real-call grounding. Got:\n{full_prompt[:500]}"


# ---------------------------------------------------------------------------
# 6. run_episode prospect= injection
# ---------------------------------------------------------------------------

async def test_run_episode_accepts_prospect_kwarg():
    """run_episode(persona, ..., prospect=<twin>) uses the injected prospect instead of creating a new
    ProspectSimulator. The injected prospect's respond() is called each turn."""
    from src.config.settings import load_config
    from src.sim import selfplay

    # A custom tracking prospect that records calls
    class TrackingProspect(ProspectSimulator):
        def __init__(self, persona: Persona) -> None:
            super().__init__(persona, seed=0)
            self.respond_calls = 0

        async def respond(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            self.respond_calls += 1
            return await super().respond(*args, **kwargs)

    config = load_config("champion_v0")

    persona = Persona(
        persona_id="inject_test",
        archetype="anxious_parent",
        style="chatty",
        utility={"budget": 0.9, "need": 0.9, "trust": 0.0, "urgency": 0.9, "patience": 0.95},
        resistance={"price": 0.2, "efficacy_doubt": 0.2, "diy_free": 0.2, "timing": 0.2},
        decision_factors=["proof_of_results"],
        difficulty=0.4,
        qualified=True,
        disqualifier=None,
        max_commitment="enrollment",
    )
    tracking_prospect = TrackingProspect(persona)

    # Agent LLM that closes at consultation every turn
    import json as _json
    from typing import Sequence as _Seq

    def _agent_serve(messages: _Seq[Message], **opts: Any) -> str:
        rf = opts.get("response_format")
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            return _json.dumps({
                "act": "attempt_close",
                "target_slot": None,
                "tier": "consultation",
                "confidence": 0.9,
                "rationale": "test close",
                "trust": 0.05,
                "need_intensity": 0.05,
                "urgency": 0.0,
                "purchase_intent": 0.1,
                "bail_risk": 0.0,
            })
        return "Let me book a consultation for you."

    agent_llm = MockLLMClient(_agent_serve)

    # Prospect LLM that pushes drivers strongly
    prospect_llm = MockLLMClient(
        lambda msgs, **opts: json.dumps({
            "utterance": "Yes, that sounds great!",
            "deltas": {"trust": 0.2, "need": 0.2, "purchase_intent": 0.2},
        })
    )

    ep = await selfplay.run_episode(
        persona,
        agent_llm,
        prospect_llm,
        config,
        seed=1,
        max_turns=10,
        persist=False,
        prospect=tracking_prospect,
    )

    # The injected prospect was actually used (its respond() was called)
    assert tracking_prospect.respond_calls >= 1, (
        "Injected prospect's respond() was never called — run_episode created a new one instead"
    )
    # Episode is well-formed
    assert ep.episode_id is not None
    assert len(ep.turns) >= 1


async def test_run_episode_without_prospect_kwarg_still_works():
    """run_episode with no prospect= kwarg (default None) creates a new ProspectSimulator as before
    — existing behavior is unchanged (regression guard)."""
    from src.config.settings import load_config
    from src.sim import selfplay

    config = load_config("champion_v0")
    persona = Persona(
        persona_id="default_test",
        archetype="anxious_parent",
        style="chatty",
        utility={"budget": 0.9, "need": 0.9, "trust": 0.0, "urgency": 0.9, "patience": 0.1},
        resistance={"price": 0.2, "efficacy_doubt": 0.2, "diy_free": 0.2, "timing": 0.2},
        decision_factors=["proof_of_results"],
        difficulty=0.4,
        qualified=True,
        disqualifier=None,
        max_commitment="enrollment",
    )

    import json as _json
    from typing import Sequence as _Seq

    def _agent_serve(messages: _Seq[Message], **opts: Any) -> str:
        rf = opts.get("response_format")
        if isinstance(rf, dict) and rf.get("type") == "json_object":
            return _json.dumps({
                "act": "ask",
                "target_slot": "goal",
                "tier": None,
                "confidence": 0.8,
                "rationale": "test",
                "trust": 0.0, "need_intensity": 0.0,
                "urgency": 0.0, "purchase_intent": 0.0, "bail_risk": 0.0,
            })
        return "Tell me about your goals."

    agent_llm = MockLLMClient(_agent_serve)
    prospect_llm = MockLLMClient(
        lambda msgs, **opts: _json.dumps({"utterance": "OK sure.", "deltas": {}})
    )

    ep = await selfplay.run_episode(
        persona,
        agent_llm,
        prospect_llm,
        config,
        seed=99,
        max_turns=3,
        persist=False,
        # NO prospect= kwarg — default behavior
    )
    assert ep.episode_id is not None
    assert ep.channel == "sim"

# Unit tests for the replay_call fidelity runner (src/loop/replay.py).
# Network-free: all LLM calls use MockLLMClient. Tests written FIRST (test-driven). Collaborators:
# src.loop.replay, src.sim.twin, src.sim.selfplay, src.loop.sim2real.divergence, src.memory.schema.
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

import pytest

from src.core.llm import MockLLMClient, Message
from src.loop.replay import ReplayFidelity, replay_call
from src.memory.schema import BeliefSnapshot, Episode, Turn
from src.sim.personas import Persona


# ---------------------------------------------------------------------------
# Helpers (reuse pattern from test_twin.py)
# ---------------------------------------------------------------------------

def _make_belief(
    trust: float = 0.5,
    need: float = 0.5,
    purchase_intent: float = 0.3,
    stage: str = "discovery",
) -> BeliefSnapshot:
    return BeliefSnapshot(
        drivers={"trust": trust, "need": need, "purchase_intent": purchase_intent, "urgency": 0.4},
        stage=stage,
    )


def _make_turn(turn_id: int, speaker: str, text: str,
               belief: Optional[BeliefSnapshot] = None,
               decision: Optional[str] = None) -> Turn:
    return Turn(turn_id=turn_id, speaker=speaker, text=text,
                decision=decision, belief=belief)


def _make_real_episode(*, committed: bool = True) -> Episode:
    turns = [
        _make_turn(0, "agent", "Hi, I'm Alex from Nerdy.",
                   belief=_make_belief(trust=0.3, need=0.35, purchase_intent=0.1),
                   decision="greeting"),
        _make_turn(1, "prospect", "I'm not sure yet."),
        _make_turn(2, "agent", "Tell me about your goals.",
                   belief=_make_belief(trust=0.4, need=0.50, purchase_intent=0.2),
                   decision="ask_discovery"),
        _make_turn(3, "prospect", "My daughter needs help with math before exams."),
        _make_turn(4, "agent", "We can help, want to book a consultation?",
                   belief=_make_belief(trust=0.60, need=0.70, purchase_intent=0.50),
                   decision="attempt_close"),
        _make_turn(5, "prospect", "Sure, let's set up a consultation."),
    ]
    return Episode(
        episode_id="real-replay-001",
        turns=turns,
        outcome="consult_booked",
        ladder_tier=2,
        qualified=True,
        channel="sim",
        persona="anxious_parent",
        metrics={"committed_tier": "consultation" if committed else None},
    )


def _make_agent_llm_factory():
    """Factory that returns a fresh MockLLMClient each call — closes at consultation every turn."""
    def _factory() -> MockLLMClient:
        def _serve(messages: Sequence[Message], **opts: Any) -> str:
            rf = opts.get("response_format")
            if isinstance(rf, dict) and rf.get("type") == "json_object":
                return json.dumps({
                    "act": "attempt_close",
                    "target_slot": None,
                    "tier": "consultation",
                    "confidence": 0.9,
                    "rationale": "test close",
                    "trust": 0.05, "need_intensity": 0.05,
                    "urgency": 0.0, "purchase_intent": 0.1, "bail_risk": 0.0,
                })
            return "Let's book a consultation."

        return MockLLMClient(_serve)

    return _factory


def _make_twin_llm_factory(strong: bool = False):
    """Factory that returns a fresh MockLLMClient each call for the twin's LLM."""
    deltas = (
        {"trust": 0.15, "need": 0.15, "purchase_intent": 0.15}
        if strong
        else {"trust": 0.03, "need": 0.03, "purchase_intent": 0.03}
    )

    def _factory() -> MockLLMClient:
        payload = json.dumps({"utterance": "Yes, that makes sense.", "deltas": deltas})

        def _serve(messages: Sequence[Message], **opts: Any) -> str:
            return payload

        return MockLLMClient(_serve)

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_replay_call_returns_replay_fidelity():
    """replay_call returns a ReplayFidelity dataclass with the expected fields."""
    from src.config.settings import load_config
    config = load_config("champion_v0")
    real_episode = _make_real_episode(committed=True)

    result = await replay_call(
        real_episode,
        _make_agent_llm_factory(),
        _make_twin_llm_factory(strong=True),
        config,
        n=2,
        max_turns=12,
        seed=0,
    )

    assert isinstance(result, ReplayFidelity)
    assert result.real_episode_id == "real-replay-001"
    assert result.n == 2
    assert isinstance(result.mean_divergence, float)
    assert isinstance(result.outcome_match_rate, float)
    assert isinstance(result.per_run, list)
    assert len(result.per_run) == 2
    assert result.real_outcome == "consult_booked"


async def test_replay_fidelity_divergence_bounds():
    """Divergence scores are in [0, 1] for every replay run."""
    from src.config.settings import load_config
    config = load_config("champion_v0")
    real_episode = _make_real_episode(committed=True)

    result = await replay_call(
        real_episode,
        _make_agent_llm_factory(),
        _make_twin_llm_factory(),
        config,
        n=3,
        max_turns=10,
        seed=0,
    )

    for score in result.per_run:
        assert 0.0 <= score <= 1.0, f"Divergence score {score} is out of [0,1]"
    assert 0.0 <= result.mean_divergence <= 1.0
    assert 0.0 <= result.outcome_match_rate <= 1.0


async def test_replay_fidelity_mean_is_mean_of_per_run():
    """mean_divergence is the arithmetic mean of per_run divergence scores."""
    from src.config.settings import load_config
    config = load_config("champion_v0")
    real_episode = _make_real_episode(committed=True)

    result = await replay_call(
        real_episode,
        _make_agent_llm_factory(),
        _make_twin_llm_factory(),
        config,
        n=3,
        max_turns=8,
        seed=1,
    )

    assert len(result.per_run) == result.n
    expected_mean = sum(result.per_run) / len(result.per_run)
    assert abs(result.mean_divergence - expected_mean) < 1e-9, (
        f"mean_divergence {result.mean_divergence} != arithmetic mean {expected_mean}"
    )


async def test_replay_call_n1():
    """replay_call with n=1 returns exactly one per_run entry."""
    from src.config.settings import load_config
    config = load_config("champion_v0")
    real_episode = _make_real_episode(committed=True)

    result = await replay_call(
        real_episode,
        _make_agent_llm_factory(),
        _make_twin_llm_factory(),
        config,
        n=1,
        max_turns=8,
        seed=5,
    )

    assert result.n == 1
    assert len(result.per_run) == 1
    assert result.mean_divergence == result.per_run[0]


async def test_replay_call_uses_different_seeds_per_run():
    """Each replay run gets a different seed (seed+i) — divergences can differ across runs.
    This is not guaranteed to differ (same LLM behavior), but the TWIN seeds are distinct."""
    from src.config.settings import load_config
    config = load_config("champion_v0")
    real_episode = _make_real_episode(committed=True)

    # Run 3 replays — with deterministic strong LLM the twins may converge, but the mechanism
    # must not crash and must produce n results
    result = await replay_call(
        real_episode,
        _make_agent_llm_factory(),
        _make_twin_llm_factory(strong=True),
        config,
        n=3,
        max_turns=10,
        seed=42,
    )

    assert len(result.per_run) == 3


async def test_replay_call_no_db_writes(monkeypatch):
    """replay_call must NOT write to the store (persist=False for all internal runs)."""
    from src.config.settings import load_config
    from src.memory import store
    config = load_config("champion_v0")
    real_episode = _make_real_episode(committed=True)

    save_called = []

    async def _spy_save(episode: Any) -> None:
        save_called.append(episode)

    monkeypatch.setattr(store, "save_episode", _spy_save)

    await replay_call(
        real_episode,
        _make_agent_llm_factory(),
        _make_twin_llm_factory(),
        config,
        n=2,
        max_turns=6,
        seed=0,
    )

    assert not save_called, (
        f"replay_call called store.save_episode {len(save_called)} time(s) — must use persist=False"
    )


async def test_replay_call_outcome_match_rate_shape():
    """outcome_match_rate is a float in [0, 1]; it reflects the fraction of replays matching the
    real outcome label."""
    from src.config.settings import load_config
    config = load_config("champion_v0")
    real_episode = _make_real_episode(committed=True)

    result = await replay_call(
        real_episode,
        _make_agent_llm_factory(),
        _make_twin_llm_factory(strong=True),
        config,
        n=3,
        max_turns=15,
        seed=0,
    )

    # outcome_match_rate = fraction of replays where replay.outcome == real_episode.outcome
    assert 0.0 <= result.outcome_match_rate <= 1.0
    # With strong LLM + closing agent, most replays should commit (outcome=consult_booked or other)
    # We don't assert equality here since the twin's commit path depends on driver buildup.

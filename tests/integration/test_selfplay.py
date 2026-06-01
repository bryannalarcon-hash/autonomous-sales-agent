# Integration tests for the headless text self-play harness (plan U8). DB-DEPENDENT: the whole
# module skips when DATABASE_URL is unset (mirrors tests/integration/test_store.py) so the
# orchestrator — which brings Postgres+pgvector up and applies the migration — runs these. Drives
# src.sim.selfplay.run_episode / run_batch with two MOCK LLM clients (agent vs prospect, KTD5) to
# prove: a run reaches a terminal ladder outcome and persists ONE queryable channel="sim" episode
# with version attribution + ground-truth qualified; ASR-noise corrupts prospect text without
# crashing the robust agent; walked/escalated outcomes map correctly; and a batch persists N
# episodes whose outcome_distribution sums to N. Mirrors test_store.py's _schema fixture (rebind the
# store pool per loop + apply migrations/001_init.sql).
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Sequence

import pytest

# Skip collection entirely if the DB driver isn't installed (DB-dependent module; keeps a DB-free
# unit run from failing collection) — same guard as test_store.py.
pytest.importorskip("asyncpg", reason="asyncpg not installed — DB-dependent integration test")

from src.config.settings import load_config
from src.core.llm import MockLLMClient, Message
from src.memory import store
from src.sim import selfplay
from src.sim.personas import Persona

# Skip the entire module unless a database is configured (store.load_dotenv() at import resolves
# DATABASE_URL the same way save_episode/get_pool will).
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — DB-dependent integration test (run with Postgres+pgvector up)",
)

_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"


@pytest.fixture(autouse=True)
async def _schema():
    """Per-test: rebind the store pool to THIS test's event loop + apply the idempotent migration.

    Identical pattern to tests/integration/test_store.py: pytest-asyncio (auto mode) runs each test
    on its own loop, the store keeps a single module-level pool, so we drop any pool bound to a prior
    loop and recreate it here, then run the CREATE ... IF NOT EXISTS migration (safe to repeat).
    """
    await store.close_pool()
    pool = await store.get_pool()
    sql = _MIGRATION.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
    yield
    await store.close_pool()


# --- Mock LLM builders ------------------------------------------------------------------------
# respond() makes exactly THREE agent LLM calls per turn:
#   1. dst.update  -> complete_json  (DST driver-delta JSON; response_format == json_object)
#   2. decide      -> complete_json  (policy proposal JSON;  response_format == json_object)
#   3. realize     -> complete       (NLG plain reply;       NO json response_format)
# So the agent mock is a single callable that ROUTES on opts["response_format"]: when it is a
# json_object, return ONE combined JSON carrying BOTH the DST driver-delta keys AND the Decision
# keys (DST ignores act/target_slot/tier; the policy ignores trust/need/... driver keys); when no
# json response_format, return a plain reply string. One callable serves every turn.


def _is_json_call(opts: dict[str, Any]) -> bool:
    rf = opts.get("response_format")
    return isinstance(rf, dict) and rf.get("type") == "json_object"


def _agent_mock(
    *,
    act: str = "ask",
    target_slot: Optional[str] = "goal",
    tier: Optional[str] = None,
    confidence: float = 0.8,
    concession: Optional[float] = None,
    reply: str = "Sure — tell me a bit about what your child is working on.",
) -> MockLLMClient:
    """An agent LLMClient whose single callable serves every respond() call by routing on the
    response_format opt. The combined JSON carries Decision keys + driver deltas in one object."""
    import json

    decision: dict[str, Any] = {
        "act": act,
        "target_slot": target_slot,
        "tier": tier,
        "confidence": confidence,
        "rationale": "test-routed combined proposal",
        # DST driver deltas (ignored by the policy; consumed by dst._update_drivers).
        "trust": 0.05,
        "need_intensity": 0.05,
        "urgency": 0.0,
        "purchase_intent": 0.03,
        "bail_risk": -0.02,
    }
    if concession is not None:
        decision["concession"] = concession
    decision_json = json.dumps(decision)

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        if _is_json_call(opts):
            return decision_json
        return reply

    return MockLLMClient(serve)


def _prospect_mock(*, big_push: bool = False, walk_hint: bool = False) -> MockLLMClient:
    """A prospect LLMClient. Returns the prospect JSON {"utterance", "deltas"}. With big_push the
    deltas slam the soft drivers up by the clamp each turn so a qualified persona crosses the buy
    gate quickly; otherwise neutral talk that never moves the gate."""
    import json

    if big_push:
        deltas = {"trust": 0.2, "need": 0.2, "urgency": 0.2, "purchase_intent": 0.2}
    else:
        deltas = {"trust": 0.0, "need": 0.0, "urgency": 0.0, "purchase_intent": 0.0}
    utterance = (
        "I'm not sure this is right for us and I have to run soon."
        if walk_hint
        else "That makes sense, my daughter does need help with this."
    )
    payload = json.dumps({"utterance": utterance, "deltas": deltas})

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        return payload

    return MockLLMClient(serve)


# --- Personas (explicit so outcomes are deterministic, independent of population sampling) ------


# Ladder outcomes a committing prospect can terminate at (the honest buy-gate fires at the LOWEST
# rung it reaches as drivers climb — so a cold prospect commits at callback/consultation, not
# necessarily enrollment). Used by the commit assertions below.
_COMMIT_OUTCOMES = {"enrolled", "trial_booked", "consult_booked", "callback_scheduled"}


def _hot_qualified() -> Persona:
    """A qualified persona with high budget/need + low starting trust so big_push prospect turns
    climb the soft drivers across the buy gate within a few turns. The honest gate commits at the
    LOWEST reachable rung as drivers climb (purchase_intent always starts at 0.1 in the engine), so
    this terminates at a real ladder commit (callback/consultation/trial) — a terminal ladder
    outcome, which is exactly the U8 verification."""
    return Persona(
        persona_id="hot_0001",
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


def _impatient() -> Persona:
    """Patience so low it is exhausted within a couple of turns -> deterministic walk."""
    return Persona(
        persona_id="walk_0001",
        archetype="deadline_crunch",
        style="terse",
        utility={"budget": 0.8, "need": 0.7, "trust": 0.3, "urgency": 0.9, "patience": 0.1},
        resistance={"price": 0.3, "efficacy_doubt": 0.3, "diy_free": 0.2, "timing": 0.5},
        decision_factors=["fast_start"],
        difficulty=0.5,
        qualified=True,
        disqualifier=None,
        max_commitment="enrollment",
    )


# --- Tests ------------------------------------------------------------------------------------


async def test_selfplay_run_terminates_and_logs_one_episode():
    """A run reaches a terminal ladder outcome and persists exactly one queryable channel='sim'
    episode with version tag set and qualified == persona ground truth. Round-trip via get_episode."""
    config = load_config("champion_v0")
    persona = _hot_qualified()
    agent_llm = _agent_mock()
    prospect_llm = _prospect_mock(big_push=True)

    ep = await selfplay.run_episode(
        persona, agent_llm, prospect_llm, config, seed=1, cohort="training", max_turns=24
    )

    # Terminal at a real ladder commit outcome (the honest gate fires at the lowest reachable rung).
    assert ep.outcome in _COMMIT_OUTCOMES
    assert ep.ladder_tier >= 1  # a genuine commitment rung (not 0 = none/walked/escalated/released)
    assert ep.channel == "sim"
    assert ep.version == config.version == "champion_v0"
    assert ep.kb_version == config.kb_version
    assert ep.qualified is True  # GROUND TRUTH from the persona
    assert ep.persona == "anxious_parent"
    assert ep.cohort == "training"
    assert ep.metrics.get("turn_count", 0) >= 1
    # The committed tier metric maps consistently to the logged outcome + ladder_tier.
    committed = ep.metrics.get("committed_tier")
    assert committed in ("enrollment", "trial", "consultation", "callback")
    _expected = {
        "enrollment": ("enrolled", 4), "trial": ("trial_booked", 3),
        "consultation": ("consult_booked", 2), "callback": ("callback_scheduled", 1),
    }[committed]
    assert (ep.outcome, ep.ladder_tier) == _expected

    # The agent opened with a canned greeting (turn 0, decision="greeting") — NOT via respond().
    assert ep.turns[0].speaker == "agent"
    assert ep.turns[0].decision == "greeting"
    assert ep.turns[0].belief is not None

    # Persisted exactly once and queryable by id.
    got = await store.get_episode(ep.episode_id)
    assert got is not None
    assert got.outcome in _COMMIT_OUTCOMES
    assert got.outcome == ep.outcome
    assert got.version == "champion_v0"
    assert got.qualified is True
    assert got.channel == "sim"


async def test_asr_noise_does_not_crash_agent():
    """With noise>0 the prospect text is corrupted before the agent sees it, yet the episode still
    completes and logs — the robust DST never raises on garbled input. At least one logged prospect
    turn text differs from the clean text (noise actually fired)."""
    config = load_config("champion_v0")
    persona = _hot_qualified()
    agent_llm = _agent_mock()
    prospect_llm = _prospect_mock(big_push=False)  # no commit; run goes several turns -> noise fires

    clean_utterance = "That makes sense, my daughter does need help with this."

    ep = await selfplay.run_episode(
        persona, agent_llm, prospect_llm, config, seed=7, cohort="training", max_turns=8, noise=0.6
    )

    # The episode terminated and was logged (max_turns -> released, since big_push is off here).
    assert ep.outcome in ("released", "enrolled", "trial_booked", "consult_booked",
                           "callback_scheduled", "walked", "escalated")
    assert ep.channel == "sim"
    got = await store.get_episode(ep.episode_id)
    assert got is not None

    # At least one prospect turn's logged text was actually corrupted away from the clean source.
    prospect_texts = [t.text for t in ep.turns if t.speaker == "prospect" and t.text]
    assert prospect_texts, "expected at least one prospect turn"
    assert any(t != clean_utterance for t in prospect_texts), "noise did not corrupt any prospect text"


async def test_walked_and_escalated_outcomes_map_correctly():
    """A low-patience persona yields outcome 'walked' with ladder_tier 0; an explicit human request
    (last_user_act forced via the harness) drives the escalate path -> 'escalated', escalated=True."""
    config = load_config("champion_v0")

    # --- walked ---
    walker = _impatient()
    ep_walk = await selfplay.run_episode(
        _impatient_clone(walker), _agent_mock(), _prospect_mock(walk_hint=True), config,
        seed=3, cohort="training", max_turns=24,
    )
    assert ep_walk.outcome == "walked"
    assert ep_walk.ladder_tier == 0
    assert ep_walk.escalated is False
    got_walk = await store.get_episode(ep_walk.episode_id)
    assert got_walk is not None and got_walk.outcome == "walked"

    # --- escalated --- the harness can feed a last_user_act per turn; "human_request" trips the
    # escalation gate inside respond()/gates regardless of the proposed act.
    ep_esc = await selfplay.run_episode(
        _hot_qualified(), _agent_mock(), _prospect_mock(big_push=False), config,
        seed=5, cohort="training", max_turns=24, last_user_act="human_request",
    )
    assert ep_esc.outcome == "escalated"
    assert ep_esc.escalated is True
    assert ep_esc.ladder_tier == 0
    got_esc = await store.get_episode(ep_esc.episode_id)
    assert got_esc is not None and got_esc.escalated is True


def _impatient_clone(p: Persona) -> Persona:
    """Fresh persona instance (run_episode should not depend on persona mutation, but be safe)."""
    return Persona(
        persona_id=p.persona_id,
        archetype=p.archetype,
        style=p.style,
        utility=dict(p.utility),
        resistance=dict(p.resistance),
        decision_factors=list(p.decision_factors),
        difficulty=p.difficulty,
        qualified=p.qualified,
        disqualifier=p.disqualifier,
        max_commitment=p.max_commitment,
    )


async def test_batch_runs_persist_and_outcome_distribution():
    """run_batch over N personas completes, persists N episodes, and outcome_distribution sums to N;
    every episode carries version attribution."""
    config = load_config("champion_v0")
    personas = [_hot_qualified() for _ in range(3)] + [_impatient_clone(_impatient()) for _ in range(2)]
    n = len(personas)

    # Factories: a FRESH LLM client per episode so MockLLMClient call-state never collides across
    # concurrent runs. Hot personas push, impatient ones walk; both terminate deterministically.
    def agent_factory() -> MockLLMClient:
        return _agent_mock()

    def prospect_factory() -> MockLLMClient:
        return _prospect_mock(big_push=True)

    episodes = await selfplay.run_batch(
        personas, agent_factory, prospect_factory, config,
        base_seed=100, cohort="training", max_turns=24, concurrency=4,
    )

    assert len(episodes) == n
    for ep in episodes:
        assert ep.version == "champion_v0"
        assert ep.kb_version == config.kb_version
        assert ep.channel == "sim"
        got = await store.get_episode(ep.episode_id)
        assert got is not None  # persisted

    dist = selfplay.outcome_distribution(episodes)
    assert sum(dist.values()) == n
    # The hot personas should reach a commit outcome; the impatient ones should walk.
    commits = sum(dist.get(o, 0) for o in _COMMIT_OUTCOMES)
    assert commits >= 1, f"expected >=1 ladder commit, got distribution {dist}"
    assert dist.get("walked", 0) >= 1, f"expected >=1 walked, got distribution {dist}"

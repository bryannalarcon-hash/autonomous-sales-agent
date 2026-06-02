# Integration tests for the headless text self-play harness (plan U8). DB-DEPENDENT: the whole
# module skips when DATABASE_URL is unset (mirrors tests/integration/test_store.py) so the
# orchestrator — which brings Postgres+pgvector up and applies the migration — runs these. Drives
# src.sim.selfplay.run_episode / run_batch with two MOCK LLM clients (agent vs prospect, KTD5) to
# prove: a run reaches a terminal ladder outcome and persists ONE queryable channel="sim" episode
# with version attribution + ground-truth qualified; ASR-noise corrupts prospect text without
# crashing the robust agent; walked/escalated outcomes map correctly; and a batch persists N
# episodes whose outcome_distribution sums to N. Mirrors test_store.py's _schema fixture (rebind the
# store pool per loop + apply migrations/001_init.sql). CB-06: also verifies that
# episode.metrics["prospect_trajectory"] is persisted on sim episodes with one entry per prospect
# turn carrying the 6 driver keys (trust/need/urgency/purchase_intent/budget/patience), each rounded
# to 3 dp, and that the GenerativeTwin injected-prospect path also populates the trajectory.
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


# Ladder outcomes a committing prospect can terminate at. The honest buy-gate is OFFER-gated: a
# prospect commits ONLY when the agent attempts a close, at the tier offered, and only if its true
# drivers clear it. The commit-path tests therefore drive the agent mock with act="attempt_close".
_COMMIT_OUTCOMES = {"enrolled", "trial_booked", "consult_booked", "callback_scheduled"}


def _hot_qualified() -> Persona:
    """A qualified persona with high budget/need + low starting trust so big_push prospect turns
    climb the soft drivers across a consultation close within a couple of turns. Commitment is
    offer-gated, so the agent mock must CLOSE (attempt_close at a tier) for this to terminate at a
    real ladder commit — which is exactly the U8 verification."""
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
    # The agent CLOSES at a consultation each turn; big_push climbs the prospect's drivers so the
    # offered consultation gate clears within a couple of turns -> a deterministic ladder commit.
    agent_llm = _agent_mock(act="attempt_close", target_slot=None, tier="consultation")
    prospect_llm = _prospect_mock(big_push=True)

    ep = await selfplay.run_episode(
        persona, agent_llm, prospect_llm, config, seed=1, cohort="training", max_turns=24
    )

    # Terminal at a real ladder commit outcome (offer-gated: the agent closed and the drivers cleared).
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
    # concurrent runs. The agent CLOSES at a consultation; hot personas clear the offered gate and
    # commit, impatient ones exhaust patience and walk first — both terminate deterministically.
    def agent_factory() -> MockLLMClient:
        return _agent_mock(act="attempt_close", target_slot=None, tier="consultation")

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


# CB-06: prospect true-driver trajectory persistence tests -------------------------------------------

# The 6 driver keys the frozen contract mandates in each trajectory entry's "drivers" dict.
_TRAJECTORY_DRIVER_KEYS = frozenset({"trust", "need", "urgency", "purchase_intent", "budget", "patience"})


async def test_prospect_trajectory_persisted_on_sim_episode():
    """CB-06: episode.metrics["prospect_trajectory"] has one entry per prospect turn with the 6
    frozen driver keys (trust/need/urgency/purchase_intent/budget/patience), each rounded to 3 dp,
    and 1-based turn indices. The trajectory length equals the number of prospect turns (not total
    turns — the agent's turns do not add entries). Verified via both the in-process Episode object
    and the round-tripped store.get_episode() to confirm the trajectory survives persistence."""
    config = load_config("champion_v0")
    persona = _hot_qualified()
    # Agent closes each turn; prospect pushes drivers hard so the episode ends after a few turns.
    agent_llm = _agent_mock(act="attempt_close", target_slot=None, tier="consultation")
    prospect_llm = _prospect_mock(big_push=True)

    ep = await selfplay.run_episode(
        persona, agent_llm, prospect_llm, config, seed=42, cohort="training", max_turns=24
    )

    # The trajectory must be present in metrics.
    traj = ep.metrics.get("prospect_trajectory")
    assert traj is not None, "expected episode.metrics['prospect_trajectory'] to be set on a sim episode"
    assert isinstance(traj, list), f"expected list, got {type(traj)}"
    assert len(traj) > 0, "expected at least one prospect turn"

    # Count prospect turns in the Turn list (excluding agent and the greeting) to check the length.
    prospect_turn_count = sum(1 for t in ep.turns if t.speaker == "prospect")
    assert len(traj) == prospect_turn_count, (
        f"trajectory length {len(traj)} != prospect turn count {prospect_turn_count}"
    )

    # Each entry must be {"turn": <1-based int>, "drivers": {6 float keys, each rounded to 3 dp}}.
    for i, entry in enumerate(traj):
        assert "turn" in entry, f"entry {i} missing 'turn' key"
        assert "drivers" in entry, f"entry {i} missing 'drivers' key"
        assert entry["turn"] == i + 1, f"entry {i} has turn={entry['turn']}, expected {i + 1}"
        drivers = entry["drivers"]
        assert isinstance(drivers, dict), f"entry {i} drivers is not a dict"
        # All 6 frozen driver keys must be present.
        assert _TRAJECTORY_DRIVER_KEYS == set(drivers.keys()), (
            f"entry {i} driver keys mismatch: got {set(drivers.keys())}"
        )
        for key, val in drivers.items():
            assert isinstance(val, float), f"entry {i} driver '{key}' is not a float (got {type(val)})"
            # Confirm 3 dp rounding: value must equal itself rounded to 3 places.
            assert val == round(val, 3), (
                f"entry {i} driver '{key}' = {val!r} is not rounded to 3 dp"
            )
            assert 0.0 <= val <= 1.0, f"entry {i} driver '{key}' = {val!r} out of [0, 1]"

    # Round-trip: trajectory must survive serialization to Postgres and back.
    got = await store.get_episode(ep.episode_id)
    assert got is not None
    rt_traj = (got.metrics or {}).get("prospect_trajectory")
    assert rt_traj is not None, "trajectory not found after round-trip through store"
    assert len(rt_traj) == len(traj)
    for i, (orig, rt) in enumerate(zip(traj, rt_traj)):
        assert orig["turn"] == rt["turn"], f"turn index mismatch at {i}"
        assert set(orig["drivers"].keys()) == set(rt["drivers"].keys()), f"driver key mismatch at {i}"


async def test_prospect_trajectory_via_injected_prospect():
    """CB-06: the trajectory is also populated when an external (injected) prospect is passed as the
    `prospect=` kwarg to run_episode, which is the GenerativeTwin code path (U15). The presence of
    .state.snapshot() on the injected object is what matters — the same accumulation logic applies."""
    config = load_config("champion_v0")
    persona = _hot_qualified()
    agent_llm = _agent_mock(act="attempt_close", target_slot=None, tier="consultation")
    prospect_llm = _prospect_mock(big_push=True)

    from src.sim.prospect import ProspectSimulator
    injected = ProspectSimulator(persona, seed=7)

    ep = await selfplay.run_episode(
        persona, agent_llm, prospect_llm, config,
        seed=7, cohort="training", max_turns=24,
        prospect=injected,
    )

    traj = ep.metrics.get("prospect_trajectory")
    assert traj is not None, "trajectory absent on injected-prospect run"
    assert len(traj) > 0
    for entry in traj:
        assert set(entry["drivers"].keys()) == _TRAJECTORY_DRIVER_KEYS

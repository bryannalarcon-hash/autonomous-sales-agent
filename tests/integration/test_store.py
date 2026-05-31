# Integration tests for the unified memory store (plan U2). DB-DEPENDENT: the whole module skips
# when DATABASE_URL is unset, so the orchestrator (which brings up Postgres + applies the migration)
# is the one that actually runs these. Covers: episode round-trip preserves belief trajectory +
# version tags; lead upsert merges slots by phone-hash; real (voice) and sim episodes share ONE
# schema (plan R26); and the privacy invariant — the raw phone string NEVER appears in a stored
# episode or lead row (plan R42). Uses pytest-asyncio (asyncio_mode=auto in pyproject).
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

# Skip collection entirely if the DB driver isn't installed — this is a DB-dependent module the
# orchestrator runs with Postgres+pgvector up; without asyncpg there is nothing to test here, and
# guarding at import keeps a DB-free unit run (pytest tests/unit) from failing collection.
pytest.importorskip("asyncpg", reason="asyncpg not installed — DB-dependent integration test")

from src.memory.schema import (
    BeliefSnapshot,
    Episode,
    Lead,
    Turn,
    VersionLineage,
    phone_hash,
)
from src.memory import store

# Skip the entire module unless a database is configured. `store` runs load_dotenv() at import
# (line above), so DATABASE_URL here reflects the resolved value (shell or .env) the store will use
# — keeping the skip guard consistent with what save_episode/get_pool actually connect to.
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — DB-dependent integration test (run with Postgres+pgvector up)",
)

_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"

# A raw phone string that must never leak into any stored row.
RAW_PHONE = "+1 (555) 010-7788"


@pytest.fixture(autouse=True)
async def _schema():
    """Per-test: bind the store's pool to THIS test's event loop and apply the migration.

    pytest-asyncio (auto mode) runs each test on its own event loop; the store keeps a single
    module-level pool, so we must drop any pool bound to a previous loop and recreate it here,
    or asyncpg raises "another operation is in progress" across loops. The migration is
    idempotent (CREATE ... IF NOT EXISTS), so re-running it per test is safe and cheap.
    """
    await store.close_pool()  # discard any pool bound to a prior test's loop
    pool = await store.get_pool()
    sql = _MIGRATION.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
    yield
    await store.close_pool()


def _belief(turn_no: int, trust: float, bail: float, stage: str) -> BeliefSnapshot:
    return BeliefSnapshot(
        slots={"grade_level": {"value": "11", "confidence": 0.9}},
        drivers={"trust": trust, "bail_risk": bail, "purchase_intent": 0.1 * turn_no},
        trends={"trust_velocity": 0.05 * turn_no},
        stage=stage,
        decision_confidence=0.8,
        escalation_imminent=bail > 0.8,
    )


def _make_episode(channel: str, phash: str | None) -> Episode:
    turns = [
        Turn(
            turn_id=0,
            speaker="agent",
            text="Hi, this is Alex from Nerdy. How can I help?",
            decision="greeting",
            rationale="open warmly per playbook",
            belief=_belief(0, trust=0.3, bail=0.1, stage="greeting"),
            latency_ms=240,
        ),
        Turn(
            turn_id=1,
            speaker="prospect",
            text="My son needs help with SAT math.",
            belief=_belief(1, trust=0.4, bail=0.15, stage="discovery"),
        ),
        Turn(
            turn_id=2,
            speaker="agent",
            text="Got it — what score is he aiming for?",
            decision="ask_goal",
            rationale="SPIN: surface the desired outcome before pitching",
            belief=_belief(2, trust=0.55, bail=0.2, stage="discovery"),
            latency_ms=310,
        ),
    ]
    return Episode(
        episode_id=f"ep-{uuid.uuid4().hex}",
        turns=turns,
        outcome="consult_booked",
        ladder_tier=2,
        qualified=True,
        version="champion_v0",
        kb_version="kb_v0",
        channel=channel,
        lead_phone_hash=phash,
        persona="anxious_parent",
        cohort="held_out",
        metrics={"turn_count": 3, "talk_listen": 0.6, "duration_ms": 41000},
    )


async def test_episode_round_trip_preserves_trajectory_and_versions():
    """Write+read an episode preserves the belief trajectory + version/kb_version tags."""
    ep = _make_episode("sim", phash=None)
    await store.save_episode(ep)
    got = await store.get_episode(ep.episode_id)

    assert got is not None
    assert got.version == "champion_v0"
    assert got.kb_version == "kb_v0"
    assert got.ladder_tier == 2
    assert got.outcome == "consult_booked"
    assert got.qualified is True

    # Belief trajectory preserved turn-by-turn (the contract the round-trip test guards).
    assert len(got.turns) == 3
    traj = got.belief_trajectory
    assert traj[0].drivers["trust"] == pytest.approx(0.3)
    assert traj[2].drivers["trust"] == pytest.approx(0.55)
    assert traj[2].trends["trust_velocity"] == pytest.approx(0.1)
    assert traj[2].stage == "discovery"

    # Per-turn decision + rationale survive (the Call Review decision trace).
    assert got.turns[2].decision == "ask_goal"
    assert got.turns[2].rationale.startswith("SPIN")
    assert got.turns[0].latency_ms == 240


async def test_lead_upsert_merges_slots_by_phone_hash():
    """Lead upsert by phone-hash MERGES slots across calls; objections union; voice sticks."""
    phash = phone_hash(RAW_PHONE)

    await store.upsert_lead_by_phone(
        Lead(
            phone_hash=phash,
            slots={"grade_level": {"value": "11", "confidence": 0.9}},
            prior_objections=["price"],
            assigned_voice="voice_a",
            last_outcome="trial_booked",
            persona_read="anxious_parent",
        )
    )
    # Second call fills a NEW slot and adds an objection; assigned_voice omitted (must persist).
    merged = await store.upsert_lead_by_phone(
        Lead(
            phone_hash=phash,
            slots={"subject": {"value": "SAT math", "confidence": 0.95}},
            prior_objections=["price", "timing"],
        )
    )

    assert set(merged.slots.keys()) == {"grade_level", "subject"}
    assert merged.slots["grade_level"]["value"] == "11"
    assert merged.slots["subject"]["value"] == "SAT math"
    assert merged.prior_objections == ["price", "timing"]  # union, de-duped, ordered
    assert merged.assigned_voice == "voice_a"  # sticky across the second (voice-less) upsert
    assert merged.last_outcome == "trial_booked"

    fetched = await store.get_lead_by_phone(phash)
    assert fetched is not None
    assert fetched.assigned_voice == "voice_a"


async def test_real_and_sim_episodes_share_one_schema():
    """A sim episode and a (stub) voice episode are queryable through one interface (plan R26)."""
    phash = phone_hash(RAW_PHONE)
    # Ensure the FK target exists for the voice (known-caller) episode.
    await store.upsert_lead_by_phone(Lead(phone_hash=phash))

    sim_ep = _make_episode("sim", phash=None)
    voice_ep = _make_episode("voice", phash=phash)
    await store.save_episode(sim_ep)
    await store.save_episode(voice_ep)

    got_sim = await store.get_episode(sim_ep.episode_id)
    got_voice = await store.get_episode(voice_ep.episode_id)

    assert got_sim.channel == "sim"
    assert got_voice.channel == "voice"
    # Same shape, same version attribution — one interface, both channels.
    assert type(got_sim) is type(got_voice)
    assert got_sim.version == got_voice.version == "champion_v0"
    assert got_voice.lead_phone_hash == phash


async def test_raw_phone_never_stored_in_episode_or_lead():
    """The raw phone string must NEVER appear in a stored episode/lead row (plan R42)."""
    phash = phone_hash(RAW_PHONE)
    assert RAW_PHONE not in phash  # the hash itself does not leak the raw digits

    await store.upsert_lead_by_phone(
        Lead(phone_hash=phash, slots={"subject": {"value": "SAT math"}})
    )
    voice_ep = _make_episode("voice", phash=phash)
    await store.save_episode(voice_ep)

    pool = await store.get_pool()
    digits = "5550107788"  # normalized raw digits
    async with pool.acquire() as conn:
        lead_blob = await conn.fetchval(
            "SELECT lead::text FROM lead WHERE phone_hash = $1", phash
        )
        ep_blob = await conn.fetchval(
            "SELECT episode::text FROM episode WHERE episode_id = $1", voice_ep.episode_id
        )

    for blob in (lead_blob, ep_blob):
        assert blob is not None
        assert RAW_PHONE not in blob
        assert digits not in blob
    # The phone-hash IS present as the key — that is the only phone-derived value allowed.
    assert phash in lead_blob


async def test_version_lineage_and_champion():
    """record_version builds lineage and enforces a single champion for rollback (P6/P9)."""
    base = f"v0-{uuid.uuid4().hex[:8]}"
    chal = f"v1-{uuid.uuid4().hex[:8]}"

    await store.record_version(
        VersionLineage(version=base, kb_version="kb_v0", kpi={"ladder": 0.4}, is_champion=True)
    )
    await store.record_version(
        VersionLineage(
            version=chal,
            parent_version=base,
            kb_version="kb_v0",
            kpi={"ladder": 0.51},
            is_champion=True,  # promoting the challenger demotes the base
        )
    )

    champ = await store.get_champion()
    assert champ is not None
    assert champ.version == chal

    base_node = await store.get_lineage(base)
    assert base_node.is_champion is False  # demoted on challenger promotion
    chal_node = await store.get_lineage(chal)
    assert chal_node.parent_version == base
    assert chal_node.kpi["ladder"] == pytest.approx(0.51)

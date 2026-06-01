# Integration tests for the demo-call persistence bridge (src.api.persistence.persist_call_end)
# against a REAL Postgres (plan U2/U14/U15). DB-DEPENDENT: the module skips when DATABASE_URL is
# unset, reusing the per-test `_schema` fixture pattern from tests/integration/test_store.py (drop the
# pool bound to a prior loop, recreate it, apply the idempotent migration). The contract under test is
# the FK-safe lead<-episode invariant: a call that binds a phone-hash but ends with NO raw_phone must
# NOT raise a ForeignKeyViolation (the episode's lead_phone_hash must reference an existing lead OR be
# None); a call that DOES pass raw_phone keys the episode by the SERVER-derived hash == the lead PK;
# an anonymous call persists the episode with no lead. Uses a tiny fake VoiceSession (no brain) so the
# test exercises the mapping + store calls, not the orchestrator.
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

# Skip collection entirely if the DB driver isn't installed (DB-free unit runs must not fail here).
pytest.importorskip("asyncpg", reason="asyncpg not installed — DB-dependent integration test")

from src.api.persistence import persist_call_end
from src.config.settings import load_config
from src.core.belief_state import BeliefState
from src.memory import store
from src.memory.schema import Turn, phone_hash

# Skip the whole module unless a database is configured (store ran load_dotenv() at import).
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — DB-dependent integration test (run with Postgres+pgvector up)",
)

_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"

# A raw phone the server hashes itself — the client must NEVER be trusted to supply the lead key.
RAW_PHONE = "+1 (555) 010-4242"


@pytest.fixture(autouse=True)
async def _schema():
    """Per-test: bind the store's pool to THIS test's event loop and apply the idempotent migration
    (mirrors tests/integration/test_store.py — pytest-asyncio runs each test on its own loop, so a
    pool bound to a prior loop must be dropped and recreated or asyncpg raises across loops)."""
    await store.close_pool()
    pool = await store.get_pool()
    sql = _MIGRATION.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
    yield
    await store.close_pool()


class _FakeState:
    """The minimal committed state persist_call_end reads: a final belief + the captured turns."""

    def __init__(self, belief: BeliefState, turns: list[Turn]) -> None:
        self.belief = belief
        self.turns = turns


class _FakeSession:
    """A brain-free stand-in for VoiceSession exposing exactly what persist_call_end touches:
    `.state.belief`, `.state.turns`, `.recorded`, `.assigned_voice`. Lets the test drive the mapping
    + the FK-safe store order without building a real per-call orchestrator."""

    def __init__(self, *, turns: Optional[list[Turn]] = None, recorded: bool = False) -> None:
        belief = BeliefState.fresh()
        belief.set_slot("subject", "SAT math", 0.9)
        self.state = _FakeState(belief, turns or [Turn(turn_id=0, speaker="agent", text="Hi.")])
        self.recorded = recorded
        self.assigned_voice = "voice_a"


def _cfg() -> Any:
    return load_config("champion_v0")


async def test_bound_hash_no_raw_phone_at_end_no_fk_error():
    """A call that bound a phone-hash but ends with NO raw_phone must persist WITHOUT a
    ForeignKeyViolation: the server upserts a minimal lead by that hash before saving the episode (or
    nulls the episode key), so episode.lead_phone_hash always references an existing lead row."""
    h = phone_hash(RAW_PHONE)
    session = _FakeSession()
    # phone_hash bound (client-side hash), raw_phone absent at /end — the historical FK trap.
    episode = await persist_call_end(
        session, config=_cfg(), channel="text", phone_hash=h, raw_phone=None
    )

    # No FK error was raised; the episode persisted and round-trips.
    got = await store.get_episode(episode.episode_id)
    assert got is not None
    # Its lead_phone_hash either references an existing lead OR is None — never a dangling FK.
    if got.lead_phone_hash is not None:
        assert got.lead_phone_hash == h
        lead = await store.get_lead_by_phone(h)
        assert lead is not None, "episode keyed to a hash must have a backing lead row (FK-safe)"


async def test_bound_raw_phone_episode_keyed_by_server_hash():
    """When raw_phone is passed at end, the episode's lead_phone_hash is the SERVER-derived hash and
    equals the lead PK the per-lead memory was written under (single-sourced key, not a client hash)."""
    server_hash = phone_hash(RAW_PHONE)
    session = _FakeSession()
    # The client supplies a DIFFERENT (wrong/stale) hash; the server must ignore it as the lead key.
    client_hash = phone_hash("999-999-9999")
    episode = await persist_call_end(
        session, config=_cfg(), channel="text", phone_hash=client_hash, raw_phone=RAW_PHONE
    )

    # The lead exists under the server-derived hash, and the episode references THAT (the lead PK).
    lead = await store.get_lead_by_phone(server_hash)
    assert lead is not None
    assert episode.lead_phone_hash == server_hash

    got = await store.get_episode(episode.episode_id)
    assert got is not None
    assert got.lead_phone_hash == server_hash
    # The raw phone never lands in the stored episode body.
    import json

    assert RAW_PHONE not in json.dumps(got.to_dict(), default=str)


async def test_anonymous_call_persists_episode_with_no_lead():
    """An anonymous call (no phone-hash, no raw_phone) persists the episode with lead_phone_hash=None
    and writes no lead row."""
    session = _FakeSession()
    episode = await persist_call_end(
        session, config=_cfg(), channel="text", phone_hash=None, raw_phone=None
    )
    got = await store.get_episode(episode.episode_id)
    assert got is not None
    assert got.lead_phone_hash is None

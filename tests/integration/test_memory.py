# Integration tests for the cross-call memory hydration bridge (plan U6; R2/R7/R11/R42). DB-DEPENDENT:
# the whole module skips when DATABASE_URL is unset (the orchestrator runs it with Postgres up).
# Covers AE1: a returning caller with PARTIAL prior info hydrates a BeliefState whose known slots
# read at >= the lock confidence (0.8) and src.core.gates.skip_known flips an `ask` for a hydrated
# slot to `confirm_known`; a NEW caller (no record) hydrates a fresh/empty state (full discovery);
# a partial caller leaves only the GAPS unknown; and persist_lead_after_call round-trips learned
# slots back to the store so a LATER hydrate sees them. Uses pytest-asyncio (asyncio_mode=auto).
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

# Skip collection if the DB driver isn't installed — this is a DB-dependent module; without asyncpg
# there is nothing to test, and guarding here keeps a DB-free unit run from failing collection.
pytest.importorskip("asyncpg", reason="asyncpg not installed — DB-dependent integration test")

from src.config.settings import load_config
from src.core.belief_state import DRIVERS, BeliefState
from src.core.gates import skip_known
from src.core.policy import Decision
from src.memory import store
from src.memory.hydrate import (
    HYDRATED_SLOT_CONFIDENCE,
    hydrate_belief,
    persist_lead_after_call,
    seed_slots,
)
from src.memory.schema import Lead, phone_hash

# Skip the entire module unless a database is configured (store runs load_dotenv() at import, so this
# reflects the resolved value the store will actually connect to — consistent with test_store.py).
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — DB-dependent integration test (run with Postgres+pgvector up)",
)

_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"


@pytest.fixture(autouse=True)
async def _schema():
    """Per-test: rebind the store's pool to THIS test's event loop and apply the (idempotent) migration.

    Mirrors test_store.py: pytest-asyncio (auto mode) runs each test on its own loop, but the store
    keeps one module-level pool, so we drop any pool bound to a prior loop and recreate it here.
    """
    await store.close_pool()
    pool = await store.get_pool()
    sql = _MIGRATION.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
    yield
    await store.close_pool()


def _unique_phone() -> str:
    """A fresh raw phone per test so runs don't collide on a previously-stored lead row."""
    return f"+1 (555) {uuid.uuid4().int % 900 + 100:03d}-{uuid.uuid4().int % 9000 + 1000:04d}"


@pytest.fixture()
def config():
    return load_config("champion_v0")


# ---------------------------------------------------------------------------
# AE1 — returning caller with PARTIAL prior info hydrates known slots at >= 0.8,
# and skip_known overrides an `ask` for a hydrated slot to confirm_known.
# ---------------------------------------------------------------------------
async def test_returning_caller_hydrates_known_slots_and_skip_known_confirms(config):
    """AE1: a returning caller's prior slots hydrate at >= lock confidence; skip_known confirms them."""
    raw = _unique_phone()
    # Seed a lead with PARTIAL prior info (grade + subject known; goal/timeline/budget never captured).
    await store.upsert_lead_by_phone(
        Lead(
            phone_hash=phone_hash(raw),
            slots={"grade_level": "11", "subject": "SAT math"},  # value-only (persist shape)
            prior_objections=["price"],
            assigned_voice="voice_a",
            persona_read="anxious_parent",
            last_outcome="consult_booked",
        )
    )

    belief = await hydrate_belief(raw, config)

    # Known slots are present at >= the lock confidence, so the gate treats them as known.
    assert belief.slot_value("grade_level") == "11"
    assert belief.slot_value("subject") == "SAT math"
    assert belief.slot_confidence("grade_level") >= HYDRATED_SLOT_CONFIDENCE
    assert belief.slot_confidence("subject") >= HYDRATED_SLOT_CONFIDENCE
    assert HYDRATED_SLOT_CONFIDENCE >= 0.8  # the gate's lock threshold

    # Cross-call CONTEXT carried on meta — voice/persona/prior objections, NOT active_objection.
    assert belief.meta["assigned_voice"] == "voice_a"
    assert belief.meta["persona_read"] == "anxious_parent"
    assert belief.meta["prior_objections"] == ["price"]
    assert belief.meta["returning_caller"] is True
    # A prior objection must NOT be force-loaded as a live, open objection.
    assert belief.active_objection is None

    # skip_known: an `ask` for a HYDRATED slot is overridden to confirm_known (don't re-ask).
    asked = Decision(act="ask", target_slot="grade_level", rationale="propose to ask grade")
    gated = skip_known(asked, belief, config)
    assert gated.act == "confirm_known"
    assert gated.target_slot == "grade_level"
    assert "skip_known" in gated.rationale

    # A GAP slot (never captured) is NOT known -> skip_known leaves the ask untouched (full discovery).
    ask_gap = Decision(act="ask", target_slot="budget", rationale="propose to ask budget")
    gated_gap = skip_known(ask_gap, belief, config)
    assert gated_gap.act == "ask"
    assert gated_gap.target_slot == "budget"


# ---------------------------------------------------------------------------
# NEW caller (no record) -> fresh/empty state (full discovery).
# ---------------------------------------------------------------------------
async def test_new_caller_hydrates_fresh_full_discovery(config):
    """A caller with NO stored lead hydrates a fresh state: no known slots, neutral drivers, greeting."""
    raw = _unique_phone()  # never stored
    assert await store.get_lead_by_phone(phone_hash(raw)) is None

    belief = await hydrate_belief(raw, config)

    assert belief.slots == {}  # nothing known -> full discovery
    assert belief.stage == "greeting"
    assert set(belief.drivers) == set(DRIVERS)
    assert all(v == 0.5 for v in belief.drivers.values())  # neutral cold-start prior
    assert belief.meta.get("returning_caller") is None  # not marked as returning

    # Every discovery slot is open -> skip_known overrides nothing (each ask survives).
    for slot in config.playbooks["discovery_sequence"]:
        ask = Decision(act="ask", target_slot=slot)
        assert skip_known(ask, belief, config).act == "ask"


async def test_anonymous_caller_hydrates_fresh(config):
    """No phone (anonymous / blocked caller id) -> fresh state; no lead lookup, no record created."""
    belief = await hydrate_belief(None, config)
    assert belief.slots == {}
    assert belief.stage == "greeting"
    assert belief.meta == {}


# ---------------------------------------------------------------------------
# PARTIAL caller leaves only the GAPS unknown.
# ---------------------------------------------------------------------------
async def test_partial_caller_leaves_only_gaps_unknown(config):
    """A partial caller hydrates the captured slots as known and leaves the rest open for discovery."""
    raw = _unique_phone()
    await store.upsert_lead_by_phone(
        Lead(
            phone_hash=phone_hash(raw),
            slots={"grade_level": "9", "goal": "raise GPA"},  # 2 of 6 captured
        )
    )

    belief = await hydrate_belief(raw, config)

    sequence = config.playbooks["discovery_sequence"]
    known = {s for s in sequence if belief.slot_confidence(s) >= HYDRATED_SLOT_CONFIDENCE}
    gaps = {s for s in sequence if belief.slot_confidence(s) < HYDRATED_SLOT_CONFIDENCE}

    assert known == {"grade_level", "goal"}
    # The remaining sequence slots are gaps -> still asked.
    assert gaps == {"subject", "timeline", "prior_tutoring", "budget"}
    for slot in gaps:
        assert skip_known(Decision(act="ask", target_slot=slot), belief, config).act == "ask"
    for slot in known:
        assert skip_known(Decision(act="ask", target_slot=slot), belief, config).act == "confirm_known"


# ---------------------------------------------------------------------------
# persist_lead_after_call round-trips learned slots; a later hydrate sees them.
# ---------------------------------------------------------------------------
async def test_persist_after_call_round_trips_to_later_hydrate(config):
    """persist_lead_after_call writes the final belief's slots; a SUBSEQUENT hydrate reads them back."""
    raw = _unique_phone()

    # --- Call 1: new caller, learns grade + subject during the call. ---
    belief1 = await hydrate_belief(raw, config)
    assert belief1.slots == {}  # nothing known on the first call
    belief1.set_slot("grade_level", "12", 0.95)
    belief1.set_slot("subject", "calculus", 0.9)
    belief1.set_slot("uncaptured", None, 0.0)  # None-valued slot must NOT be persisted

    saved = await persist_lead_after_call(
        raw, belief1, outcome="trial_booked", assigned_voice="voice_b"
    )
    assert saved is not None
    # Lead stores VALUE-ONLY (no per-call confidence noise).
    assert saved.slots == {"grade_level": "12", "subject": "calculus"}
    assert "uncaptured" not in saved.slots
    assert saved.last_outcome == "trial_booked"
    assert saved.assigned_voice == "voice_b"

    # --- Call 2: SAME phone, hydrate must surface the learned slots as known. ---
    belief2 = await hydrate_belief(raw, config)
    assert belief2.slot_value("grade_level") == "12"
    assert belief2.slot_value("subject") == "calculus"
    assert belief2.slot_confidence("grade_level") >= HYDRATED_SLOT_CONFIDENCE
    assert belief2.slot_confidence("subject") >= HYDRATED_SLOT_CONFIDENCE
    assert belief2.meta["returning_caller"] is True
    assert belief2.meta["assigned_voice"] == "voice_b"  # sticky voice carried across the call
    assert belief2.meta["last_outcome"] == "trial_booked"

    # skip_known now confirms the previously-learned slots instead of re-asking them (AE1 round-trip).
    gated = skip_known(Decision(act="ask", target_slot="subject"), belief2, config)
    assert gated.act == "confirm_known"

    # --- Call 2 learns one MORE slot; persist MERGES (slots accumulate, voice stays sticky). ---
    belief2.set_slot("goal", "ace the AP exam", 0.92)
    merged = await persist_lead_after_call(raw, belief2, outcome="enrolled")
    assert merged.slots == {
        "grade_level": "12",
        "subject": "calculus",
        "goal": "ace the AP exam",
    }
    assert merged.assigned_voice == "voice_b"  # not wiped by the voice-less second persist
    assert merged.last_outcome == "enrolled"


async def test_persist_anonymous_call_is_noop(config):
    """An anonymous (no-phone) call has nothing to key memory on -> persist returns None, no row."""
    belief = BeliefState.fresh()
    belief.set_slot("grade_level", "10", 0.9)
    result = await persist_lead_after_call(None, belief, outcome="walked")
    assert result is None


# ---------------------------------------------------------------------------
# seed_slots helper — shape tolerance (bare value AND {value, confidence}).
# ---------------------------------------------------------------------------
async def test_seed_slots_accepts_both_shapes(config):
    """seed_slots takes a {name: value} map; tolerates a {value, confidence} dict and never down-grades."""
    belief = BeliefState.fresh()
    seed_slots(
        belief,
        {
            "grade_level": "11",  # bare value -> floored to the hydrate confidence
            "subject": {"value": "physics", "confidence": 0.99},  # higher stored conf wins
            "goal": {"value": "scholarship", "confidence": 0.3},  # lower conf -> floored up to lock
            "empty": None,  # None value -> skipped entirely
        },
    )
    assert belief.slot_value("grade_level") == "11"
    assert belief.slot_confidence("grade_level") == pytest.approx(HYDRATED_SLOT_CONFIDENCE)
    assert belief.slot_value("subject") == "physics"
    assert belief.slot_confidence("subject") == pytest.approx(0.99)
    assert belief.slot_value("goal") == "scholarship"
    assert belief.slot_confidence("goal") == pytest.approx(HYDRATED_SLOT_CONFIDENCE)  # never down-graded
    assert "empty" not in belief.slots

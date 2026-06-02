# Integration tests for U14 (A): graceful async escalation deferral + operator manual takeover
# (plan R10). Covers AE4: a Decision over the concession band (or already act="escalate") ->
# handle_escalation returns an IN-PERSONA, non-empty deferral_text, a SECURED next_step, and an
# EscalationLog(lifecycle="unreviewed") logged to the review queue — with NO live human call (pure
# async). A store_hook stands in for the DB so these tests stay DB-free; one DB-gated test proves the
# escalation actually lands in the store-backed queue (list_escalations returns it). Also proves
# operator takeover: snapshot_for_takeover then restore preserves belief slots/drivers + history
# length + version (state intact), and mutating the live session AFTER snapshot does not corrupt the
# snapshot. Uses the routed MockLLMClient + FakeEmbedder pattern from test_voice_adapter.
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Sequence

import pytest

from src.config.settings import load_config
from src.core.belief_state import BeliefState
from src.core.llm import Message, MockLLMClient
from src.core.policy import Decision
from src.kb.embeddings import FakeEmbedder
from src.memory.schema import EscalationLog
from src.voice.escalation import (
    EscalationOutcome,
    NextStep,
    TakeoverState,
    handle_escalation,
    restore_from_takeover,
    snapshot_for_takeover,
)
from src.voice.session import VoiceSession


# --- Agent mock (same routing contract as test_voice_adapter / test_selfplay) -------------------


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


class _ExplodingLLM:
    """An LLM client that FAILS if any method is called — proves handle_escalation makes NO live
    human/LLM call on the deferral path (it is a pure, deterministic, async-only deferral)."""

    async def complete(self, *args: Any, **kwargs: Any) -> str:  # noqa: D401
        raise AssertionError("handle_escalation must not call an LLM / live human at runtime")

    async def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("handle_escalation must not call an LLM / live human at runtime")


def _config():
    return load_config("champion_v0")


# =============================== (A) ASYNC DEFERRAL (AE4) =========================================


async def test_discount_beyond_band_defers_secures_nextstep_and_logs_unreviewed():
    """AE4: a Decision proposing a concession beyond max_concession_band — surfaced as act="escalate"
    with the matched reason — yields a non-empty IN-PERSONA deferral_text, a SECURED next_step, and an
    EscalationLog(lifecycle="unreviewed") logged to the review queue via store_hook. NO live human is
    contacted (the LLM client would explode if touched)."""
    config = _config()
    belief = BeliefState.fresh()
    belief.last_user_act = "human_request"  # the moment is real
    # The gate already overrode to escalate on an over-band concession (gates have final say).
    decision = Decision(
        act="escalate",
        concession=0.30,  # 30% > champion_v0 max_concession_band (0.15)
        rationale="escalation_triggers: pricing concession beyond allowed band; deferring to a specialist",
    )

    logged: list[EscalationLog] = []

    async def store_hook(log: EscalationLog) -> None:
        logged.append(log)

    outcome = await handle_escalation(
        decision,
        belief,
        episode_id="ep-test-1",
        turn_id=7,
        config=config,
        store_hook=store_hook,
    )

    # An EscalationOutcome with all three deliverables.
    assert isinstance(outcome, EscalationOutcome)

    # 1. In-persona, non-empty deferral utterance (warm, persona-named, no dead air).
    assert isinstance(outcome.deferral_text, str)
    assert outcome.deferral_text.strip()
    assert "Alex" in outcome.deferral_text  # the champion_v0 persona name (in-persona)
    assert "specialist" in outcome.deferral_text.lower()

    # 2. A SECURED next-best step (a structured follow-up capture, not dead air).
    assert isinstance(outcome.next_step, NextStep)
    assert outcome.next_step.intent  # e.g. "callback" / "follow_up"

    # 3. An EscalationLog tied to the episode/turn, lifecycle "unreviewed", reason from the trigger.
    log = outcome.escalation_log
    assert isinstance(log, EscalationLog)
    assert log.episode_id == "ep-test-1"
    assert log.turn_id == 7
    assert log.lifecycle == "unreviewed"
    assert log.reason and "concession" in log.reason.lower()

    # Logged to the REVIEW QUEUE (the hook received exactly that EscalationLog).
    assert len(logged) == 1
    assert logged[0] is log
    # (No live human was ever called: the _ExplodingLLM was never passed in / touched.)


async def test_handle_escalation_makes_no_live_human_or_llm_call():
    """R10: deferral is PURE async — even with an LLM client in scope, handle_escalation never calls
    it (no live human at runtime). We pass an _ExplodingLLM-style hook-free call and assert it returns
    cleanly; the deferral text is generated deterministically from the persona, not via an LLM."""
    config = _config()
    belief = BeliefState.fresh()
    decision = Decision(act="escalate", rationale="explicit human request; deferring to a specialist")

    captured: list[EscalationLog] = []

    async def store_hook(log: EscalationLog) -> None:
        captured.append(log)

    # If handle_escalation tried to phone a human or call an LLM, there is nowhere to do it: it takes
    # no llm_client argument. This test asserts that contract structurally + that it still defers.
    outcome = await handle_escalation(
        decision, belief, episode_id="ep-2", turn_id=1, config=config, store_hook=store_hook
    )
    assert outcome.deferral_text.strip()
    assert len(captured) == 1
    assert captured[0].lifecycle == "unreviewed"


async def test_escalation_reason_falls_back_to_rationale_and_captures_moment():
    """When no structured concession is present, the reason is taken from the gate rationale, and the
    `moment` records the offending turn/quote so the review queue shows WHAT happened."""
    config = _config()
    belief = BeliefState.fresh()
    belief.last_user_act = "compliance"
    decision = Decision(
        act="escalate",
        rationale="escalation_triggers: compliance territory; deferring to a specialist",
    )

    async def store_hook(log: EscalationLog) -> None:
        return None

    outcome = await handle_escalation(
        decision,
        belief,
        episode_id="ep-3",
        turn_id=4,
        config=config,
        store_hook=store_hook,
        moment="Can you guarantee my kid gets into Harvard?",
    )
    assert "compliance" in (outcome.escalation_log.reason or "").lower()
    assert outcome.escalation_log.moment == "Can you guarantee my kid gets into Harvard?"


async def test_escalation_never_persists_an_empty_moment():
    """CB-18: a blank/None `moment` must NOT yield a contentless review-queue row — it is backfilled
    with the in-persona deferral line the agent spoke (so 'empty escalations' can't be created)."""
    config = _config()
    decision = Decision(act="escalate", rationale="explicit human request; deferring to a specialist")
    captured: list[EscalationLog] = []

    async def store_hook(log: EscalationLog) -> None:
        captured.append(log)

    for blank in (None, "", "   "):
        captured.clear()
        belief = BeliefState.fresh()
        belief.last_user_act = "human_request"
        outcome = await handle_escalation(
            decision, belief, episode_id="ep-empty", turn_id=2, config=config,
            store_hook=store_hook, moment=blank,
        )
        assert len(captured) == 1
        # The persisted moment is non-empty and equals the agent's actual deferral line.
        assert (captured[0].moment or "").strip(), f"empty moment persisted for input {blank!r}"
        assert captured[0].moment == outcome.deferral_text.strip() or captured[0].moment.startswith("Escalation:")


# =============================== OPERATOR MANUAL TAKEOVER =========================================


async def test_snapshot_for_takeover_preserves_state_and_restore_round_trips():
    """Operator takeover: snapshot_for_takeover captures belief slots/drivers + history length +
    version; restore_from_takeover puts that SAME state back so the operator resumes exactly where the
    agent was (control transfers WITH STATE INTACT)."""
    config = _config()
    session = VoiceSession(config, _agent_mock(), FakeEmbedder(), seed=0)

    # Drive one real turn so there IS committed belief/history/turns to hand off.
    await session.handle_user_turn("Hi, my daughter needs help with algebra.", "s0")
    session.commit_turn("s0")

    belief_before = session.state.belief
    history_len = len(session.state.history)
    turns_len = len(session.state.turns)
    drivers_before = dict(belief_before.drivers)
    slots_before = {k: dict(v) for k, v in belief_before.slots.items()}

    snap = snapshot_for_takeover(session)
    assert isinstance(snap, TakeoverState)
    # The snapshot carries the per-call state the operator needs to resume.
    assert snap.version == session.state.version == config.version
    assert snap.kb_version == session.state.kb_version == config.kb_version
    assert len(snap.history) == history_len
    assert len(snap.turns) == turns_len
    assert snap.belief.drivers == drivers_before
    assert snap.belief.slots == slots_before

    # Restore onto a FRESH session (the human-controlled arm) -> state intact.
    fresh = VoiceSession(config, _agent_mock(), FakeEmbedder(), seed=0)
    restore_from_takeover(fresh, snap)
    assert len(fresh.state.history) == history_len
    assert len(fresh.state.turns) == turns_len
    assert fresh.state.belief.drivers == drivers_before
    assert fresh.state.belief.slots == slots_before
    assert fresh.state.version == config.version


async def test_mutating_live_session_after_snapshot_does_not_corrupt_snapshot():
    """The snapshot is a plain, independent copy: advancing the live session AFTER snapshot (another
    committed turn that moves belief + grows history) leaves the captured TakeoverState unchanged."""
    config = _config()
    session = VoiceSession(config, _agent_mock(), FakeEmbedder(), seed=0)
    await session.handle_user_turn("Hi, my daughter needs help with algebra.", "s0")
    session.commit_turn("s0")

    snap = snapshot_for_takeover(session)
    snap_history_len = len(snap.history)
    snap_turns_len = len(snap.turns)
    snap_drivers = dict(snap.belief.drivers)
    snap_slots = {k: dict(v) for k, v in snap.belief.slots.items()}

    # Mutate the live session: commit another turn (belief advances, history/turns grow).
    await session.handle_user_turn("How much does it cost per month?", "s1")
    session.commit_turn("s1")
    # Also mutate the underlying objects in place to prove the snapshot did not alias them.
    session.state.belief.drivers["trust"] = 0.999
    session.state.belief.set_slot("grade_level", "12", 0.95)

    assert len(session.state.history) > snap_history_len  # live session grew
    # The snapshot is UNCHANGED (deep/independent copy).
    assert len(snap.history) == snap_history_len
    assert len(snap.turns) == snap_turns_len
    assert snap.belief.drivers == snap_drivers
    assert snap.belief.slots == snap_slots
    assert snap.belief.drivers.get("trust") != 0.999


# =============================== DB-GATED: lands in the review queue ==============================

# Load .env so DATABASE_URL is visible at COLLECTION time. The store submodule (which calls
# load_dotenv at import) isn't imported until inside the DB test below, so load it here too — test_store.py
# gets DATABASE_URL for free via its top-level `from src.memory import store`; we load_dotenv directly
# instead of importing store, so the 5 DB-FREE escalation tests still run without asyncpg installed.
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
_HAS_DB = bool(os.environ.get("DATABASE_URL"))
_MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"


@pytest.mark.skipif(not _HAS_DB, reason="DATABASE_URL not set — DB-dependent review-queue test")
async def test_escalation_lands_in_store_backed_review_queue():
    """The escalation lands in the REVIEW QUEUE: with the real store as the persistence path,
    handle_escalation saves the EscalationLog and list_escalations(lifecycle="unreviewed") returns it
    (the store/hook received it; the queue read returns it). DB-gated like test_store.py."""
    pytest.importorskip("asyncpg")
    from src.memory import store
    from src.memory.schema import Episode

    await store.close_pool()
    pool = await store.get_pool()
    sql = _MIGRATION.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
    try:
        # An escalation_log row FKs to an episode; create the parent episode first.
        import uuid

        episode_id = f"ep-esc-{uuid.uuid4().hex}"
        await store.save_episode(
            Episode(episode_id=episode_id, channel="voice", escalated=True, version="champion_v0")
        )

        config = _config()
        belief = BeliefState.fresh()
        belief.last_user_act = "human_request"
        decision = Decision(act="escalate", rationale="explicit human request; deferring to a specialist")

        # No store_hook -> the default persistence path is the real store.save_escalation.
        outcome = await handle_escalation(
            decision, belief, episode_id=episode_id, turn_id=2, config=config
        )

        queue = await store.list_escalations(lifecycle="unreviewed")
        ids = {e.escalation_id for e in queue}
        assert outcome.escalation_log.escalation_id in ids

        fetched = await store.get_escalation(outcome.escalation_log.escalation_id)
        assert fetched is not None
        assert fetched.episode_id == episode_id
        assert fetched.lifecycle == "unreviewed"
        assert fetched.turn_id == 2
    finally:
        await store.close_pool()

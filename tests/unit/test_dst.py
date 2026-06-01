# U3 tests (TEST-FIRST) for the hybrid Dialogue State Tracker (src/core/dst.py) and the working
# BeliefState object (src/core/belief_state.py). Covers: one utterance fills 3 slots with
# confidences (deterministic extraction); a value-acknowledgment raises trust while a price-shock
# raises price_sensitivity + lowers trust (LLM delta-update driven by a MockLLMClient with scripted
# deltas); trend/velocity features are DERIVED deterministically from a fixed trajectory (NOT
# LLM-emitted, Markov-preserving); garbled/contradictory input does not corrupt high-confidence
# slots; and the BeliefState <-> BeliefSnapshot round-trip stays consistent with the stored schema.
from __future__ import annotations

import json

import httpx
import pytest

from src.core.belief_state import DRIVERS, BeliefState
from src.core.dst import update
from src.core.llm import MockLLMClient
from src.memory.schema import BeliefSnapshot


class _RaisingLLMClient:
    """An LLMClient stub whose complete()/complete_json() ALWAYS raise a transient httpx error
    (an OpenRouter 429/5xx/timeout that outlived the retry budget). Proves the DST driver-update
    degrades to 'no driver change' instead of crashing the turn (FINDING 2)."""

    def __init__(self, exc: Exception | None = None) -> None:
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(429, request=request)
        self._exc = exc or httpx.HTTPStatusError(
            "rate limited", request=request, response=response
        )

    async def complete(self, messages, *, model=None, response_format=None, **opts):
        raise self._exc

    async def complete_json(self, messages, *, model=None, **opts):
        raise self._exc


# --- BeliefState shape + snapshot interop -----------------------------------------------------

def test_fresh_belief_has_all_six_drivers_and_neutral_priors():
    """A new BeliefState carries exactly the six named drivers at neutral priors, empty slots."""
    b = BeliefState.fresh()
    assert set(b.drivers.keys()) == set(DRIVERS)
    assert set(DRIVERS) == {
        "trust",
        "need_intensity",
        "price_sensitivity",
        "urgency",
        "purchase_intent",
        "bail_risk",
    }
    assert b.slots == {}
    assert b.stage == "greeting"
    assert b.turn_count == 0


def test_round_trips_through_belief_snapshot():
    """to_snapshot()/from_snapshot keeps the stored trajectory consistent with src/memory.schema."""
    b = BeliefState.fresh()
    b.set_slot("grade_level", "11", 0.9)
    b.drivers["trust"] = 0.6
    b.trends["trust_velocity"] = 0.1
    b.stage = "discovery"
    b.active_objection = "price"
    b.decision_confidence = 0.8
    b.escalation_imminent = True

    snap = b.to_snapshot()
    assert isinstance(snap, BeliefSnapshot)
    assert snap.slots["grade_level"] == {"value": "11", "confidence": 0.9}
    assert snap.drivers["trust"] == 0.6
    assert snap.trends["trust_velocity"] == 0.1
    assert snap.stage == "discovery"
    assert snap.active_objection == "price"
    assert snap.decision_confidence == 0.8
    assert snap.escalation_imminent is True

    # Snapshot -> working object preserves the persisted layers.
    b2 = BeliefState.from_snapshot(snap)
    assert b2.slots == b.slots
    assert b2.drivers["trust"] == 0.6
    assert b2.trends["trust_velocity"] == 0.1
    assert b2.stage == "discovery"
    assert b2.active_objection == "price"


# --- Deterministic slot extraction ------------------------------------------------------------

async def test_one_utterance_fills_three_slots_with_confidence():
    """A single information-dense utterance deterministically fills >=3 slots, each with a
    confidence, with NO dependence on the LLM (drivers held flat by a zero-delta mock)."""
    flat = MockLLMClient([json.dumps({d: 0.0 for d in DRIVERS})])
    belief = BeliefState.fresh()

    utterance = "My daughter is in 11th grade and needs SAT math help; our budget is about $300 a month."
    new = await update(belief, "ask_discovery", utterance, flat)

    assert new.slots["grade_level"]["value"] == "11"
    assert new.slots["subject"]["value"] in ("SAT math", "SAT", "math")
    assert "budget" in new.slots
    assert new.slots["budget"]["value"] == 300
    # At least three slots filled in this one turn.
    assert len([s for s in new.slots.values() if s.get("value") is not None]) >= 3
    # Every filled slot carries a confidence in [0, 1].
    for slot in new.slots.values():
        assert 0.0 <= slot["confidence"] <= 1.0


async def test_garbled_input_does_not_corrupt_high_confidence_slots():
    """A high-confidence slot survives a later garbled/contradictory low-signal utterance
    (confidence arbitration — near-monotonic accumulation, per the schema)."""
    flat = MockLLMClient([json.dumps({d: 0.0 for d in DRIVERS}), json.dumps({d: 0.0 for d in DRIVERS})])
    belief = BeliefState.fresh()

    clear = "She is in 11th grade."
    belief = await update(belief, "ask_grade", clear, flat)
    assert belief.slots["grade_level"]["value"] == "11"
    high_conf = belief.slots["grade_level"]["confidence"]
    assert high_conf >= 0.8

    # Garbled ASR-ish noise that *weakly* suggests a different grade must not overwrite it.
    garbled = "uh er ninth grade no wait [inaudible] mmm"
    belief = await update(belief, "confirm_grade", garbled, flat)
    assert belief.slots["grade_level"]["value"] == "11"  # unchanged, high-confidence preserved
    assert belief.slots["grade_level"]["confidence"] >= high_conf - 1e-9


# --- LLM latent-driver delta-update -----------------------------------------------------------

async def test_value_ack_raises_trust():
    """A value-acknowledgment turn (positive trust delta scripted) raises trust toward 1."""
    scripted = MockLLMClient([json.dumps({"trust": 0.3, "need_intensity": 0.1})])
    belief = BeliefState.fresh()
    belief.drivers["trust"] = 0.4

    new = await update(belief, "share_value_story", "That actually makes a lot of sense, thank you.", scripted)
    assert new.drivers["trust"] == pytest.approx(0.7)
    assert new.drivers["need_intensity"] == pytest.approx(0.6)  # 0.5 prior + 0.1


async def test_price_shock_raises_price_sensitivity_and_lowers_trust():
    """A price-shock turn: scripted deltas raise price_sensitivity and lower trust together."""
    scripted = MockLLMClient(
        [json.dumps({"price_sensitivity": 0.4, "trust": -0.25, "bail_risk": 0.2})]
    )
    belief = BeliefState.fresh()
    belief.drivers["trust"] = 0.6
    belief.drivers["price_sensitivity"] = 0.3

    new = await update(belief, "quote_price", "Whoa, that's way more than I expected to pay.", scripted)
    assert new.drivers["price_sensitivity"] == pytest.approx(0.7)
    assert new.drivers["trust"] == pytest.approx(0.35)
    assert new.drivers["bail_risk"] == pytest.approx(0.7)  # 0.5 prior + 0.2
    # Driver levels stay clamped to [0, 1].
    for v in new.drivers.values():
        assert 0.0 <= v <= 1.0


async def test_driver_deltas_clamp_at_bounds():
    """An extreme scripted delta cannot push a driver past [0, 1]."""
    scripted = MockLLMClient([json.dumps({"trust": 5.0, "bail_risk": -5.0})])
    belief = BeliefState.fresh()
    new = await update(belief, "act", "anything", scripted)
    assert new.drivers["trust"] == 1.0
    assert new.drivers["bail_risk"] == 0.0


async def test_unknown_driver_keys_ignored():
    """A hallucinated driver key in the LLM JSON is ignored, not written into state."""
    scripted = MockLLMClient([json.dumps({"trust": 0.1, "made_up_driver": 0.9})])
    belief = BeliefState.fresh()
    new = await update(belief, "act", "hello", scripted)
    assert "made_up_driver" not in new.drivers
    assert set(new.drivers.keys()) == set(DRIVERS)


async def test_malformed_llm_json_leaves_drivers_unchanged():
    """If the LLM returns non-JSON, drivers are left at their priors (no corruption)."""
    bad = MockLLMClient(["sorry I can't do that"])
    belief = BeliefState.fresh()
    belief.drivers["trust"] = 0.55
    new = await update(belief, "act", "hello", bad)
    assert new.drivers["trust"] == pytest.approx(0.55)


async def test_httpx_error_leaves_drivers_unchanged_and_does_not_crash():
    """FINDING 2: a real OpenRouter failure (httpx.HTTPStatusError, NOT ValueError) during the
    driver delta-update must degrade exactly like the bad-JSON path — drivers stay at priors, the
    deterministic slot extraction + meta bookkeeping still happen, and no exception propagates."""
    belief = BeliefState.fresh()
    belief.drivers["trust"] = 0.55
    belief.drivers["price_sensitivity"] = 0.42

    # Slots should still extract deterministically (LLM-independent) despite the driver-call failure.
    new = await update(belief, "ask_grade", "She is in 11th grade.", _RaisingLLMClient())

    # Drivers untouched (no corruption) — same contract as malformed JSON.
    assert new.drivers["trust"] == pytest.approx(0.55)
    assert new.drivers["price_sensitivity"] == pytest.approx(0.42)
    # Deterministic layers still ran: slot filled + turn advanced.
    assert new.slots["grade_level"]["value"] == "11"
    assert new.turn_count == 1


async def test_request_error_leaves_drivers_unchanged():
    """A transport-level RequestError (timeout/connection) likewise leaves drivers at priors."""
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    belief = BeliefState.fresh()
    belief.drivers["trust"] = 0.6
    new = await update(
        belief, "act", "hello", _RaisingLLMClient(exc=httpx.ConnectTimeout("t", request=request))
    )
    assert new.drivers["trust"] == pytest.approx(0.6)


# --- Deterministically-derived trends (NOT LLM-emitted) ---------------------------------------

async def test_trends_derived_deterministically_from_trajectory():
    """Velocity/trend features are computed from the LOGGED trajectory, not emitted by the LLM.
    Driving with a flat (zero-delta) mock keeps levels fixed, but velocity reflects the prior
    trajectory deltas the DST records turn over turn."""
    # Scripted: turn 1 raises trust by +0.2, turn 2 raises trust by +0.1.
    scripted = MockLLMClient(
        [
            json.dumps({"trust": 0.2}),
            json.dumps({"trust": 0.1}),
        ]
    )
    belief = BeliefState.fresh()
    belief.drivers["trust"] = 0.3

    belief = await update(belief, "act1", "ok", scripted)  # trust -> 0.5
    assert belief.drivers["trust"] == pytest.approx(0.5)
    # Velocity after the first observed delta is the change from the prior level.
    assert belief.trends["trust_velocity"] == pytest.approx(0.2)

    belief = await update(belief, "act2", "ok again", scripted)  # trust -> 0.6
    assert belief.drivers["trust"] == pytest.approx(0.6)
    assert belief.trends["trust_velocity"] == pytest.approx(0.1)

    # Trends are present for every driver and are deterministic functions of the trajectory.
    for d in DRIVERS:
        assert f"{d}_velocity" in belief.trends


async def test_trends_from_fixed_trajectory_match_expected_levels_and_velocity():
    """Given a fixed scripted trajectory, the belief trajectory matches expected level + trend
    values (the U3 verification: scripted transcript -> expected level + trend)."""
    scripted = MockLLMClient(
        [
            json.dumps({"need_intensity": 0.2, "trust": 0.1}),
            json.dumps({"need_intensity": 0.2, "trust": 0.1}),
            json.dumps({"need_intensity": -0.1, "trust": 0.0}),
        ]
    )
    b = BeliefState.fresh()  # need_intensity prior 0.5, trust prior 0.5
    b = await update(b, "a", "u1", scripted)  # need 0.7, trust 0.6
    b = await update(b, "a", "u2", scripted)  # need 0.9, trust 0.7
    b = await update(b, "a", "u3", scripted)  # need 0.8, trust 0.7

    assert b.drivers["need_intensity"] == pytest.approx(0.8)
    assert b.drivers["trust"] == pytest.approx(0.7)
    # Last-step velocity = level(t) - level(t-1).
    assert b.trends["need_intensity_velocity"] == pytest.approx(-0.1)
    assert b.trends["trust_velocity"] == pytest.approx(0.0)
    assert b.turn_count == 3


async def test_turn_count_and_last_user_act_meta_update():
    """The DST advances turn_count and records last_user_act bookkeeping each user turn."""
    flat = MockLLMClient([json.dumps({d: 0.0 for d in DRIVERS})])
    b = BeliefState.fresh()
    assert b.turn_count == 0
    b = await update(b, "ask_grade", "She's in 11th grade.", flat, last_user_act="inform")
    assert b.turn_count == 1
    assert b.last_user_act == "inform"

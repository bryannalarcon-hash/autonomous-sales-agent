# TEST-FIRST for per-turn timing instrumentation in src/core/respond.py. respond() must measure each
# of its three sequential stages (DST belief update, gated policy decision, NLG realization) — each
# dominated by exactly one model call — and expose the breakdown on decision.meta["timings"] so per-turn
# AND per-model-call latency is measurable (and a structured line is logged). Network-free via MockLLMClient.
from __future__ import annotations

import json

from src.config.settings import AgentConfig, Persona
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import MockLLMClient
from src.core.respond import respond


def _cfg() -> AgentConfig:
    return AgentConfig(
        version="test_v0",
        kb_version="kb_test",
        persona=Persona(name="Alex", role="learning advisor", style="warm-consultative"),
        prompts={"policy": "Propose the next act as JSON.", "nlg": "Reply concisely."},
        playbooks={"discovery_sequence": ["grade_level", "subject"]},
        thresholds={
            "trust_gate_open_price": 0.5,
            "pushiness_cap": 0.7,
            "pushiness_pressure_count_cap": 2,
            "escalate_low_confidence_turns": 2,
            "low_confidence_level": 0.4,
            "max_concession_band": 0.15,
            "discovery_slots_required": 0,
        },
    )


def _llm() -> MockLLMClient:
    # The 3 calls one respond() turn makes, in order: DST deltas, policy proposal, NLG text.
    return MockLLMClient(
        [
            json.dumps({d: 0.0 for d in DRIVERS}),
            json.dumps({"act": "pitch", "rationale": "value", "confidence": 0.6}),
            "Here is how we help.",
        ]
    )


async def test_respond_records_per_stage_timings():
    """respond() attaches a timing breakdown to decision.meta['timings'] with the four keys, each a
    non-negative number — so a caller/log can report per-turn AND per-model-call latency."""
    decision, _reply, _belief = await respond(BeliefState.fresh(), [], "Hi", _llm(), _cfg())

    timings = decision.meta.get("timings")
    assert timings is not None, "respond() must record decision.meta['timings']"
    for key in ("dst_ms", "policy_ms", "nlg_ms", "total_ms"):
        assert key in timings, f"missing timing key {key!r}"
        assert isinstance(timings[key], (int, float)) and timings[key] >= 0.0


async def test_respond_total_covers_the_three_stages():
    """total_ms is the wall-clock for the whole turn — at least the sum of the three sequential stages
    (they run one after another), allowing a tiny float-rounding slack."""
    decision, _reply, _belief = await respond(BeliefState.fresh(), [], "Hi", _llm(), _cfg())
    t = decision.meta["timings"]
    assert t["total_ms"] >= (t["dst_ms"] + t["policy_ms"] + t["nlg_ms"]) - 1.0

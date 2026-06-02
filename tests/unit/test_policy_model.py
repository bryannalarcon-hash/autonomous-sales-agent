# TEST-FIRST for the per-call POLICY model override (mixed-model setup). The policy proposal is small
# fixed-vocabulary strict-JSON, so it can ride a cheaper/faster model than NLG. decide() must send the
# POLICY_MODEL override (+ a minimal reasoning effort for GPT-5 reasoning models) to its JSON call when
# set, and nothing when unset. Network-free: a MockLLMClient callable captures the call opts.
from __future__ import annotations

import json

from src.config.settings import AgentConfig, Persona
from src.core.belief_state import BeliefState
from src.core.llm import MockLLMClient
from src.core.policy import decide


def _cfg() -> AgentConfig:
    return AgentConfig(
        version="test_v0",
        kb_version="kb_test",
        persona=Persona(name="Alex", role="learning advisor", style="warm"),
        prompts={"policy": "Propose the next act as JSON."},
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


def _capturing_llm(captured: dict) -> MockLLMClient:
    def cb(messages, **opts):
        captured["model"] = opts.get("model", "UNSET")
        captured["reasoning"] = opts.get("reasoning", "UNSET")
        return json.dumps({"act": "pitch", "rationale": "value", "confidence": 0.6})

    return MockLLMClient(cb)


async def test_policy_uses_policy_model_env_when_set(monkeypatch):
    """With POLICY_MODEL set, the policy JSON call carries that model + a minimal reasoning effort."""
    monkeypatch.setenv("POLICY_MODEL", "openai/gpt-5-mini")
    monkeypatch.delenv("POLICY_REASONING_EFFORT", raising=False)
    captured: dict = {}
    await decide(BeliefState.fresh(), [], _cfg(), _capturing_llm(captured))
    assert captured["model"] == "openai/gpt-5-mini"
    assert captured["reasoning"] == {"effort": "minimal"}


async def test_policy_no_override_when_unset(monkeypatch):
    """With POLICY_MODEL unset, the policy call passes no model + no reasoning — default unchanged."""
    monkeypatch.delenv("POLICY_MODEL", raising=False)
    captured: dict = {}
    await decide(BeliefState.fresh(), [], _cfg(), _capturing_llm(captured))
    assert captured["model"] in (None, "UNSET")
    assert captured["reasoning"] == "UNSET"

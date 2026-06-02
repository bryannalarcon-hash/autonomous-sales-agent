# TEST-FIRST for the per-call DST model override (mixed-model setup). The belief-update (DST) call is
# mechanical strict-JSON that runs every turn, so it can ride a cheaper/faster model than the policy &
# NLG calls. dst.update() must send the DST_MODEL env override to its JSON call when set, and use no
# override (the client's default model) when unset. Network-free: a MockLLMClient callable captures opts.
from __future__ import annotations

import json

from src.core import dst
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import MockLLMClient


def _capturing_llm(captured: dict) -> MockLLMClient:
    def cb(messages, **opts):
        captured["model"] = opts.get("model", "UNSET")
        captured["reasoning"] = opts.get("reasoning", "UNSET")
        return json.dumps({d: 0.0 for d in DRIVERS})

    return MockLLMClient(cb)


async def test_dst_uses_dst_model_env_when_set(monkeypatch):
    """With DST_MODEL set, the DST JSON call carries that per-call model override AND a minimal
    reasoning effort — GPT-5-family models are reasoning models that, left at default, 'think' for
    ~18s on this rubric (vs ~1.6s at minimal); the override is only a win with reasoning pinned low."""
    monkeypatch.setenv("DST_MODEL", "openai/gpt-5-nano")
    monkeypatch.delenv("DST_REASONING_EFFORT", raising=False)
    captured: dict = {}
    await dst.update(BeliefState.fresh(), None, "Hi there", _capturing_llm(captured))
    assert captured["model"] == "openai/gpt-5-nano"
    assert captured["reasoning"] == {"effort": "minimal"}


async def test_dst_no_override_when_unset(monkeypatch):
    """With DST_MODEL unset, the DST call passes NO model override and NO reasoning param — the
    client's default model + its default behavior are used unchanged."""
    monkeypatch.delenv("DST_MODEL", raising=False)
    captured: dict = {}
    await dst.update(BeliefState.fresh(), None, "Hi there", _capturing_llm(captured))
    assert captured["model"] in (None, "UNSET")
    assert captured["reasoning"] == "UNSET"


async def test_dst_reasoning_effort_is_overridable(monkeypatch):
    """DST_REASONING_EFFORT tunes the pinned effort; 'none' disables the param entirely."""
    monkeypatch.setenv("DST_MODEL", "openai/gpt-5-nano")
    monkeypatch.setenv("DST_REASONING_EFFORT", "none")
    captured: dict = {}
    await dst.update(BeliefState.fresh(), None, "Hi there", _capturing_llm(captured))
    assert captured["model"] == "openai/gpt-5-nano"
    assert captured["reasoning"] == "UNSET"

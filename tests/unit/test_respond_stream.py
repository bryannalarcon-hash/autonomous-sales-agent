# TEST-FIRST for CB-41's streaming brain seam in src/core (respond_stream + realize_stream). Proves the
# STREAMING path is byte-for-byte equivalent to the blocking path at the brain level: the gated decision
# + new belief are IDENTICAL to respond()'s, the yielded NLG tokens rejoin to the EXACT reply realize()
# would produce (R37 parity), the StreamResult holder is finalized after the stream, and a blank/failed
# stream degrades to the SAME safe filler realize() falls back to. Network-free via MockLLMClient.
from __future__ import annotations

import json

import pytest

from src.config.settings import AgentConfig, Persona
from src.core.belief_state import DRIVERS, BeliefState
from src.core.llm import LLMClient, MockLLMClient
from src.core.nlg import _SAFE_FILLER, realize, realize_stream
from src.core.policy import Decision
from src.core.respond import StreamResult, respond, respond_stream


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


_REPLY = "Here is exactly how we help your learner make progress."


def _llm() -> MockLLMClient:
    """The 3 calls one turn makes, in order: DST deltas, policy proposal, NLG text. The SAME script is
    consumed identically by respond() (NLG via complete) and respond_stream() (NLG via complete_stream),
    because complete_stream advances the list one entry just like complete()."""
    return MockLLMClient(
        [
            json.dumps({d: 0.0 for d in DRIVERS}),
            json.dumps({"act": "pitch", "rationale": "value", "confidence": 0.6}),
            _REPLY,
        ]
    )


async def test_respond_stream_tokens_rejoin_to_reply_parity():
    """CB-41 R37 parity: the tokens respond_stream yields rejoin BYTE-FOR-BYTE to the reply respond()
    produces for the same script, and the StreamResult.reply equals that concatenation."""
    # Blocking path reply.
    _d, blocking_reply, _b = await respond(BeliefState.fresh(), [], "Hi", _llm(), _cfg())

    # Streaming path: collect tokens + the finalized holder.
    result = StreamResult()
    tokens = [
        t
        async for t in respond_stream(
            BeliefState.fresh(), [], "Hi", _llm(), _cfg(), result=result
        )
    ]
    assert len(tokens) > 1  # genuinely streamed (not one whole string)
    assert "".join(tokens) == blocking_reply  # streamed bytes == the blocking reply (parity)
    assert result.ready is True
    assert result.reply == "".join(tokens)
    assert result.tokens == tokens
    assert result.reply == blocking_reply


async def test_respond_stream_decision_and_belief_identical_to_respond():
    """The gated decision + new belief from respond_stream are IDENTICAL to respond()'s (the gate logic
    is shared via _decide_turn, never duplicated): same act, target_slot, tier, confidence, and the same
    advanced belief drivers/turn_count."""
    d_block, _reply, b_block = await respond(BeliefState.fresh(), [], "Hi", _llm(), _cfg())

    result = StreamResult()
    async for _ in respond_stream(BeliefState.fresh(), [], "Hi", _llm(), _cfg(), result=result):
        pass
    d_stream: Decision = result.decision  # type: ignore[assignment]
    b_stream: BeliefState = result.new_belief  # type: ignore[assignment]

    assert (d_stream.act, d_stream.target_slot, d_stream.tier, d_stream.confidence) == (
        d_block.act, d_block.target_slot, d_block.tier, d_block.confidence,
    )
    assert b_stream.turn_count == b_block.turn_count
    assert b_stream.drivers == b_block.drivers


async def test_respond_stream_stamps_timings_like_respond():
    """respond_stream attaches the SAME decision.meta['timings'] breakdown respond() does (so voice
    turns keep the per-stage latency telemetry; nlg_ms now spans generation start -> last token)."""
    result = StreamResult()
    async for _ in respond_stream(BeliefState.fresh(), [], "Hi", _llm(), _cfg(), result=result):
        pass
    timings = result.decision.meta.get("timings")  # type: ignore[union-attr]
    assert timings is not None
    for key in ("dst_ms", "policy_ms", "nlg_ms", "total_ms"):
        assert key in timings and timings[key] >= 0.0


# --- realize_stream focused parity + robustness -----------------------------------------------


def _decision(act: str = "pitch") -> Decision:
    return Decision(act=act, rationale="r", confidence=0.6)


async def test_realize_stream_matches_realize_for_same_reply():
    """realize_stream yields the SAME reply realize() returns for an identical script + decision
    (both build _build_messages), proving the streaming twin doesn't drift the surface text."""
    belief = BeliefState.fresh()
    cfg = _cfg()
    blocking = await realize(_decision(), belief, cfg, MockLLMClient([_REPLY]))
    tokens = [
        t async for t in realize_stream(_decision(), belief, cfg, MockLLMClient([_REPLY]))
    ]
    assert "".join(tokens) == blocking
    assert len(tokens) > 1


class _BlankStreamLLM(MockLLMClient):
    """An LLMClient whose complete_stream yields NOTHING (a degenerate/blank stream)."""

    def __init__(self) -> None:
        super().__init__(["unused"])

    async def complete_stream(self, messages, *, model=None, response_format=None, **opts):
        if False:  # pragma: no cover - make this an async generator that yields nothing
            yield ""
        return


class _FailingStreamLLM(MockLLMClient):
    """An LLMClient whose complete_stream raises BEFORE any token (a transient failure that outlived
    retries) — realize_stream must degrade to the safe filler, never crash the turn."""

    def __init__(self) -> None:
        super().__init__(["unused"])

    async def complete_stream(self, messages, *, model=None, response_format=None, **opts):
        raise RuntimeError("stream blew up before first token")
        yield ""  # pragma: no cover - unreachable; marks this an async generator


async def test_realize_stream_blank_stream_degrades_to_safe_filler():
    """A blank stream (no tokens) yields the SAME safe filler realize() falls back to — the turn is
    never empty (FINDING 2 parity)."""
    tokens = [
        t async for t in realize_stream(_decision(), BeliefState.fresh(), _cfg(), _BlankStreamLLM())
    ]
    assert "".join(tokens) == _SAFE_FILLER


async def test_realize_stream_failure_before_first_token_degrades_to_filler():
    """A stream that errors BEFORE any token degrades to the safe filler (parity with realize()'s
    except-branch fallback)."""
    tokens = [
        t async for t in realize_stream(_decision(), BeliefState.fresh(), _cfg(), _FailingStreamLLM())
    ]
    assert "".join(tokens) == _SAFE_FILLER

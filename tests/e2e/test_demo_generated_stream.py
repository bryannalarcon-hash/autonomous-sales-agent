# Regression tests for CB-12: the SERVER-SIDE demo is now a REAL self-play vs an LLM-GENERATED
# prospect (src.sim.ProspectSimulator) — NOT the old fixed _AUTO_DEMO_SCRIPT — and it exposes an SSE
# stream route for real-time per-turn nudges. These tests are DB-free (capture hooks injected) and
# drive the background demo task through fastapi's TestClient with MOCK LLMs (no network), asserting:
#   - the scripted-prospect constant is gone (the prospect is generated now);
#   - a demo whose agent CLOSES lands a terminal commitment outcome (e.g. consult_booked), driven by
#     the deterministic buy_gate on the generated prospect — never lingering in_progress;
#   - a demo whose agent NEVER closes still lands TERMINAL ("released" at the turn budget), proving the
#     CB-12 demo_terminal_override guarantee (no perpetual in_progress shell);
#   - the SSE stream endpoint (/api/demo/auto/stream) is registered on the demo router.
# The SSE stream is NOT consumed here (TestClient's single-loop portal deadlocks if a blocking stream
# read runs concurrently with the in-loop background demo task) — the live SSE is integration-tested
# against a real running server. We assert the route exists + the per-turn/terminal persistence, which
# is the load-bearing behavior.
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional, Sequence

import pytest
from fastapi.testclient import TestClient

from src.api.server import create_app
from src.core.llm import Message, MockLLMClient
from src.sim.prospect import SIM_MODEL

# Shrink the demo so the background task finishes fast under the test (module-level constants the
# runner reads each turn). Patched per test via monkeypatch so we never mutate global state durably.
_FAST_MAX_TURNS = 6


def _is_json_call(opts: dict[str, Any]) -> bool:
    rf = opts.get("response_format")
    return isinstance(rf, dict) and rf.get("type") == "json_object"


def _is_prospect_call(messages: Sequence[Message], opts: dict[str, Any]) -> bool:
    """The prospect side is routed to SIM_MODEL and its system prompt starts with "You ARE a prospect".
    Either signal identifies a prospect-LLM call (vs an agent DST/policy/NLG call)."""
    if opts.get("model") == SIM_MODEL:
        return True
    sys = messages[0]["content"] if messages else ""
    return isinstance(sys, str) and sys.startswith("You ARE a prospect")


def _make_serve(*, agent_closes: bool):
    """One mock `serve` for BOTH the agent and the generated prospect (create_app shares the LLM
    factory). The prospect warms hard so the buy_gate can fire; the agent either closes at
    consultation (agent_closes=True) or always asks (agent_closes=False -> the demo runs to budget)."""
    agent_calls = {"n": 0}

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        if _is_prospect_call(messages, opts):
            # Generated prospect: an in-character continue line + strong (clamped) warming so a
            # qualified persona crosses the consultation buy-gate when the agent closes.
            return json.dumps(
                {
                    "utterance": "That sounds genuinely helpful — tell me more.",
                    "deltas": {"trust": 0.14, "need": 0.14, "urgency": 0.14, "purchase_intent": 0.14},
                }
            )
        if _is_json_call(opts):
            agent_calls["n"] += 1
            if agent_closes and agent_calls["n"] >= 2:
                return json.dumps(
                    {
                        "act": "attempt_close", "target_slot": None, "tier": "consultation",
                        "confidence": 0.9, "rationale": "r", "trust": 0.1, "need_intensity": 0.1,
                        "urgency": 0.1, "purchase_intent": 0.1, "bail_risk": -0.05,
                    }
                )
            return json.dumps(
                {
                    "act": "ask", "target_slot": "goal", "tier": None, "confidence": 0.8,
                    "rationale": "r", "trust": 0.05, "need_intensity": 0.05, "urgency": 0.0,
                    "purchase_intent": 0.0, "bail_risk": 0.0,
                }
            )
        return "Let's get your child set up — how does a consultation sound?"

    return serve


def _app_with_capture(*, agent_closes: bool):
    """Build a DB-free app that captures the demo's final episode via call_end_persist_hook and
    no-ops the live upsert. Returns (TestClient, captured-list)."""
    captured: list[Any] = []

    async def call_end_hook(session: Any, **kw: Any):
        from src.api.persistence import episode_from_session

        ep = episode_from_session(
            session, config=kw["config"], channel=kw["channel"],
            last_tier=kw.get("last_tier"), episode_id=kw.get("episode_id"),
            outcome_override=kw.get("outcome_override"), created_at=kw.get("created_at"),
        )
        captured.append(ep)
        return ep

    async def live_hook(session: Any, **kw: Any):
        return None  # DB-free; the live upsert is exercised elsewhere

    app = create_app(
        llm_client_factory=lambda: MockLLMClient(_make_serve(agent_closes=agent_closes)),
        live_rag=False,
        call_end_persist_hook=call_end_hook,
        live_upsert_hook=live_hook,
    )
    return TestClient(app), captured


def _drain_demo(captured: list[Any], *, timeout_s: float = 12.0) -> Any:
    """Poll until the background demo task finalizes (captures its episode) or time out."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if captured:
            return captured[0]
        time.sleep(0.05)
    raise AssertionError("demo background task did not finalize within the timeout")


@pytest.fixture(autouse=True)
def _fast_demo_and_key(monkeypatch):
    """Each demo test needs the LLM key (else 503) and a short turn budget + zero gap so the
    background task finishes promptly."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr("src.api.demo_routes._AUTO_DEMO_MAX_TURNS", _FAST_MAX_TURNS)
    monkeypatch.setattr("src.api.demo_routes._AUTO_DEMO_TURN_GAP_S", 0.0)


# --- the scripted prospect is gone (CB-12) ---------------------------------------------------------

def test_scripted_prospect_constant_removed():
    """CB-12 swapped the fixed _AUTO_DEMO_SCRIPT for a generated ProspectSimulator — the constant
    must no longer exist (its presence would mean the prospect is still scripted)."""
    import src.api.demo_routes as dr

    assert not hasattr(dr, "_AUTO_DEMO_SCRIPT"), "the scripted prospect must be replaced by generation"


# --- the demo is a real self-play that always lands TERMINAL ---------------------------------------

def test_generated_demo_closes_lands_terminal_commitment():
    """A demo whose agent closes a warmed, qualified GENERATED prospect lands a terminal commitment
    outcome (the buy_gate fires) — never in_progress. Proves the prospect is real self-play (the
    commitment depends on the prospect's true drivers + the agent's offered tier)."""
    client, captured = _app_with_capture(agent_closes=True)
    with client:
        r = client.post("/api/demo/auto/start")
        assert r.status_code == 200, r.text
        ep = _drain_demo(captured)

    # consultation close on a qualified prospect -> consult_booked (terminal, in the Calls buckets).
    assert ep.outcome == "consult_booked"
    assert ep.outcome != "in_progress"
    assert len(ep.turns) > 0


# Terminal outcomes a finalized demo may legitimately reach — the one thing it must NEVER be is the
# in_progress sentinel (that would be a dangling shell, exactly what CB-12 forbids).
_TERMINAL_OUTCOMES = frozenset(
    {"consult_booked", "trial_booked", "enrolled", "callback_booked", "released", "walked",
     "escalated", "disqualified", "abandoned"}
)


def test_generated_demo_always_lands_terminal_never_in_progress():
    """The demo's prospect is generated and the agent is the REAL gated brain, so the exact outcome
    varies — but the demo MUST ALWAYS finalize to a TERMINAL outcome, never the in_progress sentinel.
    This is the CB-12 (and CB-08) guarantee: a non-closing run falls back to the demo_terminal_override
    ('released'/'walked') so no in_progress shell ever dangles. We drive the agent with a mock that
    prefers `ask` (the brain may still gate-close a warmed prospect — that's fine, it's still
    terminal)."""
    client, captured = _app_with_capture(agent_closes=False)
    with client:
        r = client.post("/api/demo/auto/start")
        assert r.status_code == 200, r.text
        ep = _drain_demo(captured)

    assert ep.outcome != "in_progress", "a demo must never finalize as in_progress (dangling shell)"
    assert ep.outcome in _TERMINAL_OUTCOMES, f"unexpected non-terminal outcome {ep.outcome!r}"


# --- the SSE stream route exists (CB-12 real-time) -------------------------------------------------

def test_sse_stream_route_registered():
    """The per-turn SSE stream endpoint is registered on the demo router (the live page subscribes to
    it for the selected call). We assert registration, not consumption (the live stream is
    integration-tested against a real server)."""
    client, _ = _app_with_capture(agent_closes=True)
    paths = {getattr(rt, "path", None) for rt in client.app.routes}
    assert "/api/demo/auto/stream" in paths


def test_demo_auto_start_503_without_llm_key(monkeypatch):
    """No LLM key -> demo_auto_start returns a clear 503 (unchanged CB-08 guard, still holds)."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client, _ = _app_with_capture(agent_closes=True)
    with client:
        r = client.post("/api/demo/auto/start")
    assert r.status_code == 503, r.text

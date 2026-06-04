# CB-43 unit tests (TEST-FIRST) for the 3 deeply-prompted, NAMED eval personas (src/sim/personas.py:
# Dana / Marcus / Pat), the seeded non-sequitur wiring in the prospect prompt (src/sim/prospect.py),
# and the eval runner (src/sim/eval_personas.py). These pin the HONESTY-relevant invariants the
# orchestrator locked: each named persona instantiates with the intended qualified/disqualifier/
# ceiling (Dana qualified→enrollment, Marcus qualified→enrollment, Pat UNqualified/no_authority→
# callback), each carries a non-empty behavioral prompt + non-sequitur curveballs, and — the backbone
# — Pat's pure buy_gate can NEVER return a tier above callback no matter how high the true drivers run
# or what tier the agent offers. They also assert the non-sequitur injection is SEEDED + OCCASIONAL (a
# fixed seed reproduces it; it is not on every turn), and that the eval runner tags every episode with
# an experiment/eval cohort (NEVER 'live') while running a named persona end-to-end through the brain.
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional, Sequence

import pytest

from src.config.settings import load_config
from src.core.llm import Message, MockLLMClient
from src.sim.eval_personas import (
    EVAL_COHORT_PREFIX,
    build_eval_cohort,
    run_named_eval,
)
from src.sim.personas import (
    LADDER,
    NAMED_PERSONAS,
    Persona,
    build_dana,
    build_marcus,
    build_pat,
    named_persona,
)
from src.sim.prospect import (
    ProspectSimulator,
    ProspectState,
    buy_gate,
)


# --- per-persona ground-truth labels (the orchestrator-locked spec) ---------------------------

def test_dana_qualified_enrollment_ceiling():
    """Dana — anxious over-researcher, a QUALIFIED warm lead: no disqualifier, enrollment ceiling."""
    dana = build_dana()
    assert isinstance(dana, Persona)
    assert dana.qualified is True
    assert dana.disqualifier is None
    assert dana.max_commitment == "enrollment"
    # has real budget + need (a closable lead)
    assert dana.utility["budget"] >= 0.6
    assert dana.utility["need"] >= 0.6


def test_marcus_qualified_enrollment_ceiling():
    """Marcus — terse deadline-crunch, QUALIFIED + impatient: enrollment ceiling, high urgency."""
    marcus = build_marcus()
    assert marcus.qualified is True
    assert marcus.disqualifier is None
    assert marcus.max_commitment == "enrollment"
    assert marcus.utility["budget"] >= 0.6
    assert marcus.utility["urgency"] >= 0.8        # SAT in 5 wks — wants results now
    assert marcus.utility["patience"] <= 0.45      # low patience / prickly


def test_pat_unqualified_no_authority_callback_ceiling():
    """Pat — distracted gatekeeper, UNQUALIFIED: no_authority disqualifier, ceiling capped at callback."""
    pat = build_pat()
    assert pat.qualified is False
    assert pat.disqualifier == "no_authority"
    assert pat.max_commitment == "callback"


# --- THE HONESTY BACKBONE: Pat can NEVER close above callback ---------------------------------

@pytest.mark.parametrize("offered_tier", ["enrollment", "trial", "consultation"])
def test_pat_buy_gate_never_exceeds_callback(offered_tier):
    """Even with ALL true drivers pinned to 1.0 (maximally warm) and the agent offering a higher
    tier, Pat's pure buy_gate must refuse every rung above callback — the no_authority ceiling is
    immutable to talk. This is the locked invariant: Pat is unclosable above a callback."""
    pat = build_pat()
    state = ProspectState.initial(pat)
    # Drive every gated driver to the ceiling — fluency that could never lift a real gatekeeper.
    state.trust = 1.0
    state.need = 1.0
    state.urgency = 1.0
    state.purchase_intent = 1.0
    state.budget = 1.0
    assert buy_gate(state, pat, offered_tier=offered_tier) is None


def test_pat_buy_gate_allows_callback_when_warm():
    """The flip side: a callback OFFER to a warm-enough Pat IS allowed (the correct outcome is the
    agent discovering no-authority and booking a callback) — so the ceiling caps, it doesn't forbid."""
    pat = build_pat()
    state = ProspectState.initial(pat)
    state.trust = 0.8
    state.purchase_intent = 0.8
    assert buy_gate(state, pat, offered_tier="callback") == "callback"


# --- deeply-prompted: each named persona carries a rich behavioral script + curveballs --------

@pytest.mark.parametrize("builder", [build_dana, build_marcus, build_pat])
def test_named_personas_have_behavioral_prompt(builder):
    """Each named persona is DEEPLY prompted, not numeric jitter: a non-trivial behavior_prompt
    (backstory + objection/timing + walk/convert triggers) and at least one seeded non-sequitur."""
    p = builder()
    assert isinstance(p.behavior_prompt, str)
    assert len(p.behavior_prompt) >= 200            # a real script, not a one-liner
    assert isinstance(p.non_sequiturs, tuple)
    assert len(p.non_sequiturs) >= 2                # occasional curveballs to inject


def test_named_persona_registry_and_lookup():
    """NAMED_PERSONAS holds exactly the 3 locked personas; named_persona() resolves by short name."""
    assert set(NAMED_PERSONAS) == {"dana", "marcus", "pat"}
    assert named_persona("dana").qualified is True
    assert named_persona("pat").disqualifier == "no_authority"
    with pytest.raises(KeyError):
        named_persona("nobody")


def test_named_persona_ids_are_stable_and_distinct():
    """Stable, distinct persona_ids so an eval episode is attributable to the exact personality."""
    ids = {build_dana().persona_id, build_marcus().persona_id, build_pat().persona_id}
    assert len(ids) == 3


# --- seeded, OCCASIONAL non-sequitur injection (not constant noise) ----------------------------

def _ack_reply():
    """A MockLLM callable: bland in-character ack with no objection theme + tiny warming delta."""
    payload = json.dumps({"utterance": "Okay, that makes sense.", "deltas": {"trust": 0.02}})

    def _reply(messages, **opts):
        return payload

    return _reply


def test_non_sequitur_injection_is_seeded_and_reproducible():
    """The non-sequitur hint is injected into the sim prompt via the simulator's SEEDED rng, so the
    same seed reproduces the EXACT set of turns that get a curveball — never the global random."""
    dana = build_dana()

    def _capture_seed(seed: int) -> list[bool]:
        sim = ProspectSimulator(dana, seed=seed)
        injected: list[bool] = []
        # Patch the message builder seam to record whether a curveball hint was present this turn.
        orig = sim._sim_messages

        def _spy(agent_utterance, agent_act):
            msgs = orig(agent_utterance, agent_act)
            sys_and_user = " ".join(m["content"] for m in msgs)
            injected.append("CURVEBALL" in sys_and_user)
            return msgs

        sim._sim_messages = _spy  # type: ignore[assignment]
        client = MockLLMClient(_ack_reply())
        import asyncio

        async def _run():
            for _ in range(12):
                await sim.respond("How can I help?", "discovery", client)

        asyncio.run(_run())
        return injected

    a = _capture_seed(7)
    b = _capture_seed(7)
    assert a == b                       # deterministic given the seed
    assert any(a)                       # at least one curveball fired over 12 turns
    assert not all(a)                   # but NOT on every turn — occasional, not constant noise


def test_non_sequitur_frequency_differs_by_seed():
    """Different seeds yield different curveball schedules — proof the schedule is seed-driven, not
    a fixed every-Nth-turn pattern independent of the rng."""
    dana = build_dana()

    def _schedule(seed: int) -> tuple[bool, ...]:
        sim = ProspectSimulator(dana, seed=seed)
        flags: list[bool] = []
        orig = sim._sim_messages

        def _spy(agent_utterance, agent_act):
            msgs = orig(agent_utterance, agent_act)
            flags.append("CURVEBALL" in " ".join(m["content"] for m in msgs))
            return msgs

        sim._sim_messages = _spy  # type: ignore[assignment]
        client = MockLLMClient(_ack_reply())
        import asyncio

        async def _run():
            for _ in range(16):
                await sim.respond("ok", "discovery", client)

        asyncio.run(_run())
        return tuple(flags)

    assert _schedule(1) != _schedule(2)


# --- the eval runner: experiment/eval cohort (never 'live') + end-to-end through the brain -----

def _is_json_call(opts: dict[str, Any]) -> bool:
    rf = opts.get("response_format")
    return isinstance(rf, dict) and rf.get("type") == "json_object"


def _agent_mock(*, act: str, tier: Optional[str], reply: str) -> MockLLMClient:
    """Agent brain mock: routes on response_format. respond() makes 3 calls/turn (DST json, policy
    json, NLG plain). One combined JSON serves both json calls; the plain reply serves NLG. Mirrors
    tests/integration/test_selfplay.py's _agent_mock so the brain runs end-to-end with no network."""
    decision: dict[str, Any] = {
        "act": act,
        "target_slot": None,
        "tier": tier,
        "confidence": 0.85,
        "rationale": "eval-runner test combined proposal",
        "trust": 0.05,
        "need_intensity": 0.05,
        "urgency": 0.0,
        "purchase_intent": 0.03,
        "bail_risk": -0.02,
    }
    decision_json = json.dumps(decision)

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        return decision_json if _is_json_call(opts) else reply

    return MockLLMClient(serve)


def _prospect_mock(*, push: bool) -> MockLLMClient:
    """Prospect mock: {"utterance","deltas"}. push slams the soft drivers up so a qualified persona
    can cross an offered gate; otherwise neutral talk that never moves the gate."""
    deltas = (
        {"trust": 0.2, "need": 0.2, "urgency": 0.2, "purchase_intent": 0.2}
        if push else {"trust": 0.0, "need": 0.0, "urgency": 0.0, "purchase_intent": 0.0}
    )
    payload = json.dumps({"utterance": "That makes sense, tell me more.", "deltas": deltas})

    def serve(messages: Sequence[Message], **opts: Any) -> str:
        return payload

    return MockLLMClient(serve)


def test_build_eval_cohort_is_experiment_scoped_never_live():
    """Every eval cohort tag is experiment-scoped (prefix + persona) and is NEVER 'live' — so the
    operator live Calls log (which filters to cohort='live') can never be flooded by an eval run."""
    for name in NAMED_PERSONAS:
        tag = build_eval_cohort(name)
        assert tag == f"{EVAL_COHORT_PREFIX}_{name}"
        assert tag.startswith("experiment_")
        assert tag != "live"
    with pytest.raises(KeyError):
        build_eval_cohort("nobody")


def test_run_named_eval_runs_persona_through_brain_emits_transcript_outcome_turns():
    """The runner drives ONE named persona end-to-end through the agent brain (respond()) and returns
    the transcript + outcome + turn_count, with the episode tagged under the eval cohort (not 'live')
    and persist=False so no DB is touched."""
    config = load_config("champion_v0")
    result = asyncio.run(
        run_named_eval(
            "dana",
            _agent_mock(act="ask", tier=None, reply="Tell me a bit about what your child is working on."),
            _prospect_mock(push=False),
            config,
            seed=3,
            max_turns=6,
            persist=False,
        )
    )
    assert result["cohort"] == build_eval_cohort("dana")
    assert result["cohort"] != "live"
    assert result["persona"] == "Dana"
    assert result["qualified"] is True
    assert result["disqualifier"] is None
    # End-to-end: a real transcript (canned greeting + alternating turns) and a sane turn count.
    assert result["turn_count"] >= 1
    assert len(result["transcript"]) >= 2
    assert result["transcript"][0].startswith("agent:")
    # The persisted Episode carries the eval cohort + channel='sim' (never a live call).
    ep = result["episode"]
    assert ep.cohort == build_eval_cohort("dana")
    assert ep.channel == "sim"


def test_run_named_eval_pat_never_enrolls():
    """Even with the agent aggressively closing at enrollment AND the prospect pushed maximally warm,
    Pat (no_authority, ceiling=callback) can NEVER terminate at an enrollment — the pure buy_gate caps
    her. The eval outcome is anything BUT 'enrolled'/'trial_booked'/'consult_booked'."""
    config = load_config("champion_v0")
    result = asyncio.run(
        run_named_eval(
            "pat",
            _agent_mock(act="attempt_close", tier="enrollment", reply="Let's get you enrolled today."),
            _prospect_mock(push=True),
            config,
            seed=5,
            max_turns=12,
            persist=False,
        )
    )
    assert result["outcome"] not in {"enrolled", "trial_booked", "consult_booked"}

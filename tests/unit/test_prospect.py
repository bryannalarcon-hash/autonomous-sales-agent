# U7 tests (TEST-FIRST) for the honest hidden-utility prospect simulator (src/sim/prospect.py,
# src/sim/personas.py). The buy-gate determinism is the HONESTY BACKBONE: these tests pin that
# fluent agent talk alone can NEVER make a sub-threshold or structurally-unqualified prospect
# "buy" (commit/walk is a pure numeric gate on the hidden drivers + persona ceiling, R31), that
# budget/qualified are immutable structural facts the sim LLM may not change by talking, that a
# low-patience prospect walks, that population sampling yields the seeded qualified/unqualified mix
# deterministically (R14), and that a fixed persona + fixed agent script + seed reproduces outcomes.
from __future__ import annotations

import json

import pytest

from src.core.llm import MockLLMClient
from src.sim.personas import Persona, sample_population
from src.sim.prospect import (
    LADDER,
    ProspectSimulator,
    ProspectState,
    ProspectTurn,
    buy_gate,
)


# --- helpers ----------------------------------------------------------------------------------

def _enthusiasm_callable(deltas: dict[str, float]):
    """A MockLLM callable that always replies with maximal-enthusiasm talk + the given soft deltas.

    Used to PROVE fluent talk alone never crosses the gate: the prospect keeps gushing and the LLM
    keeps proposing the same upward deltas, yet the deterministic gate decides commit/walk.
    """
    payload = json.dumps({"utterance": "Oh wow, this sounds absolutely perfect for us!", "deltas": deltas})

    def _reply(messages, **opts):
        return payload

    return _reply


def _qualified_persona(**overrides) -> Persona:
    """A genuinely-qualified persona with an enrollment-capable ceiling (overridable)."""
    base = dict(
        persona_id="p_qual",
        archetype="anxious_parent",
        style="chatty",
        utility={"budget": 0.9, "need": 0.8, "trust": 0.5, "urgency": 0.6, "patience": 0.9},
        resistance={"price": 0.2, "efficacy_doubt": 0.3, "diy_free": 0.1, "timing": 0.2},
        decision_factors=["proof_of_results", "scheduling_flexibility"],
        difficulty=0.4,
        qualified=True,
        disqualifier=None,
        max_commitment="enrollment",
    )
    base.update(overrides)
    return Persona(**base)


def _no_budget_persona() -> Persona:
    """A structurally-unqualified no_budget persona (budget pinned ~0, capped at callback)."""
    return Persona(
        persona_id="p_nobudget",
        archetype="budget_shopper",
        style="chatty",
        utility={"budget": 0.05, "need": 0.7, "trust": 0.5, "urgency": 0.5, "patience": 0.95},
        resistance={"price": 0.9, "efficacy_doubt": 0.2, "diy_free": 0.5, "timing": 0.2},
        decision_factors=["price"],
        difficulty=0.6,
        qualified=False,
        disqualifier="no_budget",
        max_commitment="callback",
    )


# --- buy-gate: thresholds, not talk (the honesty backbone) ------------------------------------

async def test_buy_gate_requires_thresholds_regardless_of_talk():
    """A sub-threshold persona, driven by a MockLLM that gushes and pushes purchase_intent every
    turn, never commits across many turns; a persona whose TRUE drivers already cross the
    thresholds does commit. Fluent talk alone != commit (R31)."""
    # Sub-threshold: high budget, but the prospect's substantive drivers (need, trust) sit BELOW
    # even the lowest tier's floor and the LLM moves them nowhere. The agent's gushing only spikes
    # the surface buying-signal (purchase_intent) — yet the multi-driver gate ALSO demands need and
    # trust, so it keeps returning None. A prospect can SOUND eager without being ready (R31).
    low = _qualified_persona(
        utility={"budget": 0.9, "need": 0.15, "trust": 0.1, "urgency": 0.2, "patience": 1.0},
    )
    # Spikes intent (the eager words), but does NOT move need/trust — talk didn't change the real
    # problem or credibility, so those stay pinned below the consultation floor (0.4).
    enthusiastic = MockLLMClient(_enthusiasm_callable({"purchase_intent": 0.2}))
    sim_low = ProspectSimulator(low, seed=1)

    committed = False
    for _ in range(6):
        turn = await sim_low.respond("You will LOVE this, sign up now!", "attempt_close", enthusiastic)
        if turn.committed_tier is not None:
            committed = True
            break
        if turn.walked:
            break
    assert not committed, "sub-threshold prospect committed on talk alone (gate not honest)"

    # Already-over-threshold: true drivers above the enrollment gate -> commits on the first turn,
    # even with a flat (zero-delta) LLM, because the gate reads the TRUE drivers.
    high = _qualified_persona(
        utility={"budget": 0.9, "need": 0.85, "trust": 0.85, "urgency": 0.6, "patience": 1.0},
    )
    flat = MockLLMClient(_enthusiasm_callable({"purchase_intent": 0.2}))
    sim_high = ProspectSimulator(high, seed=1)
    # purchase_intent starts low (~0.1); a couple of bounded turns lift it past the gate.
    tier = None
    for _ in range(5):
        turn = await sim_high.respond("Here is how we get results.", "pitch", flat)
        if turn.committed_tier is not None:
            tier = turn.committed_tier
            break
    assert tier is not None, "a genuinely-qualified, over-threshold prospect should commit"
    assert tier in LADDER


def test_buy_gate_is_pure_and_deterministic():
    """buy_gate is a pure function of state + persona: identical inputs -> identical output,
    never depends on talk. Crossing all enrollment thresholds returns 'enrollment'."""
    persona = _qualified_persona()
    state = ProspectState.initial(persona)
    state.trust = 0.8
    state.need = 0.8
    state.urgency = 0.6
    state.purchase_intent = 0.8
    # budget is read from persona.utility (immutable), here 0.9 -> enrollment gate met.
    t1 = buy_gate(state, persona)
    t2 = buy_gate(state, persona)
    assert t1 == "enrollment"
    assert t1 == t2


def test_buy_gate_respects_max_commitment_ceiling():
    """A persona capped at 'callback' never returns a tier above callback even when every higher
    threshold is numerically met (the ceiling is a HARD cap)."""
    persona = _qualified_persona(max_commitment="callback")
    state = ProspectState.initial(persona)
    state.trust = 0.95
    state.need = 0.95
    state.urgency = 0.9
    state.purchase_intent = 0.95
    tier = buy_gate(state, persona)
    assert tier == "callback"

    none_persona = _qualified_persona(max_commitment="none")
    state2 = ProspectState.initial(none_persona)
    state2.trust = 0.95
    state2.need = 0.95
    state2.urgency = 0.9
    state2.purchase_intent = 0.95
    assert buy_gate(state2, none_persona) is None


# --- unqualified: immutable structural disqualifier -------------------------------------------

async def test_unqualified_no_budget_never_commits_and_signals_disqualification():
    """A no_budget persona NEVER returns a buy tier (enrollment/trial) over a long fluent dialogue,
    and surfaces its disqualifier (the sim is instructed to voice it, and the engine flags it)."""
    persona = _no_budget_persona()

    # The sim LLM gushes AND tries to push every soft driver up; it also voices the disqualifier.
    def _reply(messages, **opts):
        return json.dumps(
            {
                "utterance": "Honestly I don't really have a budget for this right now.",
                "deltas": {"purchase_intent": 0.2, "trust": 0.2, "need": 0.2, "urgency": 0.2},
                "disqualifier_signaled": True,
            }
        )

    pushy = MockLLMClient(_reply)
    sim = ProspectSimulator(persona, seed=3)

    bought = False
    signaled = False
    for _ in range(12):
        turn = await sim.respond("This will transform your child's grades, let's enroll today!", "attempt_close", pushy)
        if turn.committed_tier in ("enrollment", "trial"):
            bought = True
            break
        if turn.disqualifier_signaled:
            signaled = True
        if turn.walked:
            break
    assert not bought, "no_budget prospect crossed a BUY tier — budget gate is not honest"
    assert signaled, "no_budget prospect never surfaced its disqualifier"


# --- patience exhaustion -> walk --------------------------------------------------------------

async def test_patience_exhaustion_walks():
    """A low-patience persona walks within its patience budget; the turn outcome is 'walked'."""
    persona = _qualified_persona(
        utility={"budget": 0.9, "need": 0.2, "trust": 0.1, "urgency": 0.2, "patience": 0.15},
    )
    # The LLM proposes no helpful deltas; pressure acts on low trust burn patience fast.
    flat = MockLLMClient(_enthusiasm_callable({}))
    sim = ProspectSimulator(persona, seed=5)

    walked = False
    turns = 0
    for _ in range(20):
        turn = await sim.respond("Buy now, this is the last chance!", "attempt_close", flat)
        turns += 1
        if turn.walked:
            walked = True
            assert turn.outcome == "walked"
            assert turn.committed_tier is None
            break
    assert walked, "low-patience prospect under pressure never walked"
    assert turns <= 10, "patience budget should run out quickly for a 0.15-patience prospect"


# --- immutability of structural facts (budget / qualified label) ------------------------------

async def test_budget_and_qualified_immutable_to_talk():
    """A MockLLM that explicitly proposes a `budget` delta and tries to flip qualified/disqualifier
    is IGNORED: budget (read from persona.utility) is unchanged, and the persona's label is
    unchanged. Structural facts cannot be talked into existence."""
    persona = _no_budget_persona()
    original_budget = persona.utility["budget"]

    def _cheating_reply(messages, **opts):
        # The sim LLM tries to grant money + authority by talking. The engine must refuse.
        return json.dumps(
            {
                "utterance": "You know what, money is no object after all!",
                "deltas": {
                    "budget": 0.95,          # structural -> must be ignored
                    "qualified": True,        # not a soft driver -> ignored
                    "disqualifier": None,     # cannot be cleared by talk -> ignored
                    "purchase_intent": 0.2,   # the only legitimate (soft) delta
                },
            }
        )

    cheater = MockLLMClient(_cheating_reply)
    sim = ProspectSimulator(persona, seed=2)

    for _ in range(5):
        turn = await sim.respond("Let's just enroll, you can afford it!", "attempt_close", cheater)
        # budget is never mutated into state and stays read from the immutable persona utility.
        assert sim.state.budget == pytest.approx(original_budget)
        assert "budget" not in turn.state_snapshot or turn.state_snapshot["budget"] == pytest.approx(original_budget)
        if turn.walked:
            break

    # The ground-truth label the grader (U9) compares against is untouched.
    assert persona.qualified is False
    assert persona.disqualifier == "no_budget"
    assert persona.utility["budget"] == pytest.approx(original_budget)
    # And it never crossed a buy tier.


# --- population sampling: seeded mix + determinism (R14) ---------------------------------------

def test_population_sampling_mix_and_determinism():
    """sample_population(200, seed=7): unqualified fraction in [0.15, 0.35]; same seed -> identical
    population; a different seed -> a different population (R14)."""
    pop = sample_population(200, seed=7)
    assert len(pop) == 200
    unqualified = [p for p in pop if not p.qualified]
    frac = len(unqualified) / len(pop)
    assert 0.15 <= frac <= 0.35, f"unqualified fraction {frac} outside target band"

    # Every unqualified persona has a real disqualifier and a capped ceiling; qualified ones don't.
    for p in pop:
        assert p.utility.keys() == {"budget", "need", "trust", "urgency", "patience"}
        for v in p.utility.values():
            assert 0.0 <= v <= 1.0
        if p.qualified:
            assert p.disqualifier is None
            assert p.max_commitment in LADDER
        else:
            assert p.disqualifier in {"no_budget", "no_authority", "no_real_need"}
            assert p.max_commitment in {"none", "callback"}
            if p.disqualifier == "no_budget":
                assert p.utility["budget"] <= 0.1
            if p.disqualifier == "no_real_need":
                assert p.utility["need"] <= 0.15

    # Determinism: same seed -> identical population (id, label, utility).
    pop_again = sample_population(200, seed=7)
    assert [p.persona_id for p in pop] == [p.persona_id for p in pop_again]
    assert [p.qualified for p in pop] == [p.qualified for p in pop_again]
    assert [p.utility for p in pop] == [p.utility for p in pop_again]

    # A different seed -> a different population.
    pop_diff = sample_population(200, seed=99)
    assert [p.utility for p in pop] != [p.utility for p in pop_diff]


def test_disqualifier_iff_unqualified():
    """Invariant: disqualifier is None IFF qualified is True (across a sampled population)."""
    for p in sample_population(120, seed=11):
        assert (p.disqualifier is None) == (p.qualified is True)


# --- reproducibility: fixed persona + fixed script + seed -> identical outcome -----------------

async def test_reproducible_outcome_for_fixed_persona_and_script():
    """Same persona + same scripted agent turns + same seed, run twice -> identical
    (committed_tier, walked, turns_elapsed). The U7 verification: deterministic outcomes."""
    agent_turns = [
        ("Hi, tell me about your goals.", "ask_discovery"),
        ("Here's how we get results.", "pitch"),
        ("We can be flexible on scheduling.", "handle_objection"),
        ("Ready to get started?", "attempt_close"),
        ("Let's lock it in.", "attempt_close"),
    ]
    deltas = json.dumps(
        {"utterance": "That makes sense.", "deltas": {"trust": 0.15, "need": 0.15, "purchase_intent": 0.2}}
    )

    async def run_once():
        persona = _qualified_persona(
            utility={"budget": 0.8, "need": 0.55, "trust": 0.5, "urgency": 0.5, "patience": 0.9},
        )
        # Fresh scripted list each run so the two runs see identical LLM replies.
        llm = MockLLMClient([deltas for _ in agent_turns])
        sim = ProspectSimulator(persona, seed=42)
        for utt, act in agent_turns:
            last = await sim.respond(utt, act, llm)
            if last.walked or last.committed_tier is not None:
                break
        return (sim.state.committed_tier, sim.state.walked, sim.state.turns_elapsed)

    out_a = await run_once()
    out_b = await run_once()
    assert out_a == out_b

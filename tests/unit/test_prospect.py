# U7 tests (TEST-FIRST) for the honest hidden-utility prospect simulator (src/sim/prospect.py,
# src/sim/personas.py). The buy-gate determinism is the HONESTY BACKBONE: these tests pin that
# fluent agent talk alone can NEVER make a sub-threshold or structurally-unqualified prospect
# "buy" (commit/walk is a pure numeric gate on the hidden drivers + persona ceiling, R31), that
# budget/qualified are immutable structural facts the sim LLM may not change by talking, that a
# low-patience prospect walks, that population sampling yields the seeded qualified/unqualified mix
# deterministically (R14), and that a fixed persona + fixed agent script + seed reproduces outcomes.
# CB-03 adds a DEPTH test: under the slowed (asymmetric) warming, a COLD prospect does NOT become
# enrollment-commit-ready in a couple of turns — it takes many handled-objection + value turns of
# gradual trust/intent gain — while the pure buy_gate is unchanged (still threshold-honest).
# CB-03 ITERATION 2 adds two REALISM tests (depth must not be a loop): (1) a rubric-following
# prospect rotates through DISTINCT objection themes and never repeats one (no degenerate verbatim
# loop), and (2) an unqualified no_budget prospect surfaces its disqualifier EARLY as a firm release
# signal and never warms purchase_intent toward a yes it cannot give — so the call resolves in
# ~6-10 turns instead of looping 30+. The honesty backbone + iteration-1 warming stay unchanged.
from __future__ import annotations

import json
import re

import pytest

from src.core.llm import MockLLMClient
from src.sim.personas import Persona, sample_population
from src.sim.prospect import (
    LADDER,
    _MAX_WARMING_DELTA,
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
        # The agent repeatedly CLOSES (offers consultation), yet the sub-threshold drivers never
        # clear it — proving the offer-gated buy_gate honors thresholds, not eagerness.
        turn = await sim_low.respond(
            "You will LOVE this, sign up now!", "attempt_close", enthusiastic, agent_tier="consultation"
        )
        if turn.committed_tier is not None:
            committed = True
            break
        if turn.walked:
            break
    assert not committed, "sub-threshold prospect committed on talk alone (gate not honest)"

    # Already-over-threshold: true drivers clear the offered gate -> when the agent CLOSES, it
    # commits, because the gate reads the TRUE drivers (a well-timed close on a ready prospect).
    high = _qualified_persona(
        utility={"budget": 0.9, "need": 0.85, "trust": 0.85, "urgency": 0.6, "patience": 1.0},
    )
    flat = MockLLMClient(_enthusiasm_callable({"purchase_intent": 0.2}))
    sim_high = ProspectSimulator(high, seed=1)
    # purchase_intent starts low (~0.1); a couple of bounded turns lift it past the consultation gate.
    tier = None
    for _ in range(5):
        turn = await sim_high.respond(
            "Ready to book a consultation?", "attempt_close", flat, agent_tier="consultation"
        )
        if turn.committed_tier is not None:
            tier = turn.committed_tier
            break
    assert tier is not None, "a genuinely-qualified, over-threshold prospect should commit when closed"
    assert tier in LADDER


def test_buy_gate_is_pure_and_deterministic():
    """buy_gate is a pure function of state + persona + offered tier: identical inputs -> identical
    output, never depends on talk. With NO offer it returns None (no spontaneous commit); offered
    'enrollment' with every enrollment threshold met returns 'enrollment'."""
    persona = _qualified_persona()
    state = ProspectState.initial(persona)
    state.trust = 0.8
    state.need = 0.8
    state.urgency = 0.6
    state.purchase_intent = 0.8
    # No close offer this turn -> the prospect never commits spontaneously.
    assert buy_gate(state, persona, offered_tier=None) is None
    # budget is read from persona.utility (immutable), here 0.9 -> enrollment gate met when OFFERED.
    t1 = buy_gate(state, persona, offered_tier="enrollment")
    t2 = buy_gate(state, persona, offered_tier="enrollment")
    assert t1 == "enrollment"
    assert t1 == t2


def test_buy_gate_respects_max_commitment_ceiling():
    """A persona capped at 'callback' declines any offer above callback even when every higher
    threshold is numerically met (the ceiling is a HARD cap); it accepts a callback offer."""
    persona = _qualified_persona(max_commitment="callback")
    state = ProspectState.initial(persona)
    state.trust = 0.95
    state.need = 0.95
    state.urgency = 0.9
    state.purchase_intent = 0.95
    # Offered enrollment -> above the callback ceiling -> declined; offered callback -> accepted.
    assert buy_gate(state, persona, offered_tier="enrollment") is None
    assert buy_gate(state, persona, offered_tier="consultation") is None
    assert buy_gate(state, persona, offered_tier="callback") == "callback"

    none_persona = _qualified_persona(max_commitment="none")
    state2 = ProspectState.initial(none_persona)
    state2.trust = 0.95
    state2.need = 0.95
    state2.urgency = 0.9
    state2.purchase_intent = 0.95
    # A "none" persona declines EVERY offer, even the lowest rung.
    assert buy_gate(state2, none_persona, offered_tier="callback") is None
    assert buy_gate(state2, none_persona, offered_tier="enrollment") is None


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
        # The agent keeps trying to close the SALE (enrollment); the no_budget gate refuses it.
        turn = await sim.respond(
            "This will transform your child's grades, let's enroll today!",
            "attempt_close",
            pushy,
            agent_tier="enrollment",
        )
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
        turn = await sim.respond(
            "Let's just enroll, you can afford it!", "attempt_close", cheater, agent_tier="enrollment"
        )
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
    # (utterance, act, offered_tier) — tier is set only on close attempts (mirrors the harness).
    agent_turns = [
        ("Hi, tell me about your goals.", "ask_discovery", None),
        ("Here's how we get results.", "pitch", None),
        ("We can be flexible on scheduling.", "handle_objection", None),
        ("Ready to get started?", "attempt_close", "consultation"),
        ("Let's lock it in.", "attempt_close", "consultation"),
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
        for utt, act, tier in agent_turns:
            last = await sim.respond(utt, act, llm, agent_tier=tier)
            if last.walked or last.committed_tier is not None:
                break
        return (sim.state.committed_tier, sim.state.walked, sim.state.turns_elapsed)

    out_a = await run_once()
    out_b = await run_once()
    assert out_a == out_b


# --- commitment requires a close OFFER (never spontaneous) ------------------------------------

async def test_no_commit_without_a_close_offer():
    """A hot, fully-qualified prospect driven through NON-close acts (ask/pitch) never commits — a
    commitment fires ONLY when the agent actually attempts a close. This pins the offer-gated model:
    discovery/pitch/talk, however effective, advance drivers but never themselves close the deal."""
    persona = _qualified_persona(
        utility={"budget": 0.9, "need": 0.85, "trust": 0.85, "urgency": 0.6, "patience": 1.0},
    )
    # Talk that drives every soft driver up to the ceiling — yet without a close offer, no commit.
    pusher = MockLLMClient(_enthusiasm_callable({"trust": 0.2, "need": 0.2, "purchase_intent": 0.2}))
    sim = ProspectSimulator(persona, seed=1)

    for act in ("ask", "pitch", "ask", "pitch", "ask"):
        turn = await sim.respond("Tell me more / here's how we help.", act, pusher)
        assert turn.committed_tier is None, f"committed on a non-close act ({act}) — gate not offer-driven"

    # Drivers are now hot; the moment the agent CLOSES at a tier they clear, it commits.
    closed = await sim.respond("Shall we book a consultation?", "attempt_close", pusher, agent_tier="consultation")
    assert closed.committed_tier == "consultation"


# --- CB-03: gradual warming -> longer, objection-first calls (NOT a 2-3-turn commit) ----------

def _objection_then_gradual_reply(objection_turns: int):
    """A deterministic MockLLM callable modeling the NEW realistic behavior: for the first
    `objection_turns` turns the prospect RAISES an objection and warms ZERO (it is pushing back,
    not cooperating); after that it warms only GRADUALLY (+_MAX_WARMING_DELTA per turn on each soft
    driver) as concerns get handled. No turn ever exceeds the warming cap, so reaching enrollment's
    high thresholds (0.7) from a cold start MUST take many turns — never a couple."""
    calls = {"n": 0}
    objections = [
        "Honestly, this sounds pricey — I'm not sure we can justify that cost.",
        "And does this actually move the needle? I've seen tutoring promise a lot before.",
        "There's also a ton of free stuff out there — Khan Academy, YouTube. Why pay?",
    ]

    def _reply(messages, **opts):
        i = calls["n"]
        calls["n"] += 1
        if i < objection_turns:
            # Pushing back: surface a real concern, warm nothing yet (a cold, skeptical buyer).
            obj = objections[i % len(objections)]
            return json.dumps({"utterance": obj, "deltas": {}})
        # Concern handled -> warm GRADUALLY (the cap throttles each climb).
        return json.dumps(
            {
                "utterance": "Okay, that helps — I'm following you.",
                "deltas": {
                    "trust": _MAX_WARMING_DELTA,
                    "need": _MAX_WARMING_DELTA,
                    "purchase_intent": _MAX_WARMING_DELTA,
                },
            }
        )

    return _reply


async def test_gradual_warming_means_cold_prospect_is_not_commit_ready_in_a_couple_turns():
    """CB-03 DEPTH: a COLD, genuinely-qualified prospect that first raises objections and then warms
    only at the per-turn warming cap does NOT become enrollment-commit-ready in a couple of turns —
    it requires MANY turns of gradual trust/intent gain. This proves self-play calls run deep (not
    the old 3-5 turns) WITHOUT touching the honest gate: the buy_gate still refuses every close until
    the TRUE drivers genuinely clear the threshold."""
    # Cold start: purchase_intent ~0.1 (seeded), trust mid, need a touch below the enrollment floor.
    persona = _qualified_persona(
        utility={"budget": 0.9, "need": 0.6, "trust": 0.5, "urgency": 0.6, "patience": 1.0},
        max_commitment="enrollment",
    )
    objection_turns = 3
    llm = MockLLMClient(_objection_then_gradual_reply(objection_turns))
    sim = ProspectSimulator(persona, seed=7)

    # 1) The agent CLOSES at enrollment every turn (impatient). For the first several turns the
    #    prospect is below threshold AND raising objections, so the gate refuses — no early commit.
    EARLY = 4  # "a couple" — generous upper bound on what counts as too-soon
    objections_seen = 0
    for t in range(EARLY):
        turn = await sim.respond(
            "Let's just enroll today — it'll be great!", "attempt_close", llm, agent_tier="enrollment"
        )
        assert turn.committed_tier is None, (
            f"cold prospect commit-ready after only {t + 1} turns — warming is too fast (CB-03)"
        )
        if t < objection_turns:
            # During the objection phase the prospect voices a real concern and warms nothing.
            assert any(w in turn.text.lower() for w in ("pricey", "needle", "free", "khan")), (
                "prospect should be surfacing objections before warming"
            )
            objections_seen += 1
    assert objections_seen >= 2, "the objection-raising path was not exercised"

    # Sanity on the binding driver: purchase_intent has only crept up by the small warming cap,
    # nowhere near the enrollment floor (0.7) yet — it must EARN its way up over many more turns.
    assert sim.state.purchase_intent < 0.5, (
        "purchase_intent climbed too fast — warming cap not slowing it (CB-03)"
    )

    # 2) Keep handling concerns + warming gradually; count how many MORE turns the gradual climb
    #    needs before the TRUE drivers clear enrollment and a close finally lands.
    commit_turn = None
    for extra in range(1, 40):
        turn = await sim.respond(
            "Here's how we'd make this work for your family.", "attempt_close", llm, agent_tier="enrollment"
        )
        if turn.committed_tier is not None:
            commit_turn = EARLY + extra
            assert turn.committed_tier == "enrollment"
            break
        assert not turn.walked, "high-patience prospect should not walk before earning the commit"

    assert commit_turn is not None, "a genuinely-qualified prospect should eventually commit when warmed + closed"
    # The whole point: it took MANY turns of gradual warming (deep call), not 2-3. (The conviction
    # coupling — intent tracking mean(trust,need), capped at the consultation bar — shifted enrollment
    # readiness from ~8 to ~7 turns; the top-tier intent past the cap is still LLM-earned, so the call
    # stays deep. The honest gate + no-early-commit guarantees below are unchanged.)
    assert commit_turn >= 6, (
        f"enrollment-ready after only {commit_turn} turns — calls are still too short (CB-03 wants depth)"
    )
    # And honesty held throughout: at the commit turn the TRUE drivers actually clear the threshold.
    assert sim.state.trust >= 0.7 and sim.state.need >= 0.7 and sim.state.purchase_intent >= 0.7


# --- CB-03 iteration 2: REALISTIC objection dynamics (no degenerate verbatim loop) -------------

def _rubric_aware_objection_reply():
    """A MockLLM callable that behaves like a rubric-following LLM: it reads the ALREADY_RAISED list
    the engine injects into the prompt and voices a NEW distinct objection each turn (never repeating
    one), then warms once it has aired enough concerns. This proves the rubric + ProspectState
    variety machinery actually rotate the prospect through DISTINCT concerns instead of looping.

    The phrasings are chosen so each maps to a different theme via _classify_objection_theme:
    price -> efficacy -> diy_free -> timing -> decision_maker.
    """
    # theme -> a line that classifies to exactly that theme (and not an already-raised one).
    lines = {
        "price": "Honestly this sounds pretty expensive — I'm not sure we can justify that cost.",
        "efficacy": "And does this actually work? I'm skeptical it'll really move the needle.",
        "diy_free": "There's a ton of free stuff too — Khan Academy, YouTube. Why pay?",
        "timing": "The timing's also rough, we're pretty swamped right now.",
        "decision_maker": "I'd also need to check with my spouse before deciding anything.",
    }
    order = ["price", "efficacy", "diy_free", "timing", "decision_maker"]

    def _reply(messages, **opts):
        # Inspect ONLY the per-turn USER message (the engine puts the dynamic RAISED_ALREADY list
        # there; the fixed system rubric also says the words "RAISED_ALREADY" in prose, so scanning
        # the whole prompt would mis-match). The list is a Python repr of a sorted list; parse it
        # precisely, then voice the FIRST not-yet-raised theme in our escalation order — mirroring a
        # rubric-obedient LLM honoring the anti-repeat instruction.
        user_msg = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        already: set[str] = set()
        match = re.search(r"RAISED_ALREADY[^\[]*\[([^\]]*)\]", user_msg)
        if match:
            already = {tok.strip().strip("'\"") for tok in match.group(1).split(",") if tok.strip()}
        for theme in order:
            if theme not in already:
                return json.dumps({"utterance": lines[theme], "deltas": {}})
        # All distinct concerns aired -> stop objecting, warm gradually toward a decision.
        return json.dumps(
            {"utterance": "Okay, that actually helps — I'm following you now.", "deltas": {"trust": 0.05}}
        )

    return _reply


async def test_prospect_does_not_repeat_the_same_objection_theme():
    """CB-03 iter2 VARIETY: a rubric-following prospect raises each objection theme AT MOST ONCE and
    NEVER the same theme twice in a row — it rotates through DISTINCT concerns (the degenerate
    ~8x-verbatim loop is gone). The engine tracks raised themes in ProspectState and feeds an
    ALREADY_RAISED hint into the prompt; this asserts the resulting utterances are varied."""
    from src.sim.prospect import _classify_objection_theme

    persona = _qualified_persona(
        utility={"budget": 0.9, "need": 0.6, "trust": 0.4, "urgency": 0.6, "patience": 1.0},
    )
    llm = MockLLMClient(_rubric_aware_objection_reply())
    sim = ProspectSimulator(persona, seed=11)

    themes_in_order: list[str] = []
    for _ in range(6):
        # Agent keeps acknowledging (a NON-close act, so no commit interferes) — exactly the loop
        # scenario from the bad batch, except now the prospect must vary its objection.
        turn = await sim.respond(
            "I hear you — let me address that concern.", "handle_objection", llm
        )
        theme = _classify_objection_theme(turn.text)
        if theme is not None:
            themes_in_order.append(theme)

    # 1) No theme is ever raised twice IN A ROW (the verbatim-loop symptom).
    for prev, cur in zip(themes_in_order, themes_in_order[1:]):
        assert prev != cur, f"prospect repeated the '{cur}' objection back-to-back (degenerate loop)"

    # 2) No theme is raised repeatedly across the whole call (each distinct concern voiced once).
    assert len(themes_in_order) == len(set(themes_in_order)), (
        f"an objection theme was raised more than once over the call: {themes_in_order}"
    )

    # 3) Several DISTINCT concerns were actually exercised (real back-and-forth, not a single line).
    assert len(set(themes_in_order)) >= 2, (
        f"prospect did not rotate through distinct concerns: {themes_in_order}"
    )

    # 4) The engine recorded the variety in state (the non-gating bookkeeping that drives the rubric).
    assert sim.state.objections_raised == set(themes_in_order)


async def test_unqualified_no_budget_releases_rather_than_loops():
    """CB-03 iter2 RESOLUTION: a no_budget prospect that honestly airs its disqualifier does NOT loop
    the same line for 30+ turns nor warm toward a commit. After a couple of honest exchanges the
    engine pushes a firm RELEASE nudge into the rubric; the prospect keeps signaling its
    disqualifier and never crosses a buy tier — so a well-behaved agent can let it go in ~6-10 turns.

    This is the deterministic counterpart to the real bad-batch failure (8x verbatim 'no budget' +
    8x acknowledgment over 38 turns): here the disqualifier signal fires early and the prospect's
    purchase_intent is NOT being warmed toward a yes it cannot honestly give."""
    persona = _no_budget_persona()

    # A rubric-following sim: it airs the budget disqualifier honestly. Once the engine's RELEASE
    # nudge appears in the prompt (after _DISQUALIFIER_RELEASE_AFTER honest exchanges), it gives the
    # FIRM final line. It NEVER proposes positive purchase_intent deltas (it can't honestly buy).
    def _reply(messages, **opts):
        prompt = " ".join(m.get("content", "") for m in messages)
        if "RESOLUTION:" in prompt:
            return json.dumps(
                {
                    "utterance": "Look, I really can't afford this — I have to be honest, it's a no.",
                    "deltas": {},
                    "disqualifier_signaled": True,
                }
            )
        return json.dumps(
            {
                "utterance": "I just don't have a budget for tutoring right now.",
                "deltas": {},
                "disqualifier_signaled": True,
            }
        )

    llm = MockLLMClient(_reply)
    sim = ProspectSimulator(persona, seed=7)

    signaled_early = False
    first_signal_turn = None
    for t in range(10):
        # Agent keeps trying to close the enrollment — the no_budget gate refuses it every time.
        turn = await sim.respond(
            "I really think this is worth it — let's enroll today!",
            "attempt_close",
            llm,
            agent_tier="enrollment",
        )
        # Never crosses a BUY tier (the immutable budget gate holds).
        assert turn.committed_tier not in ("enrollment", "trial"), (
            "no_budget prospect crossed a BUY tier — budget gate is not honest"
        )
        if turn.disqualifier_signaled and first_signal_turn is None:
            first_signal_turn = t + 1
            if first_signal_turn <= 3:
                signaled_early = True
        if turn.walked:
            break

    # 1) The disqualifier (the RELEASE signal) surfaces EARLY (~first couple of turns), not after 30+.
    assert signaled_early, (
        f"no_budget prospect did not surface its disqualifier early (first signal turn={first_signal_turn})"
    )

    # 2) The engine moved the prospect into the firm-release path (RESOLUTION nudge active), proving
    #    the exchange resolves rather than loops the identical opening line forever.
    assert sim.state.disqualifier_exchanges >= 2, (
        "honest disqualifier exchanges were not counted — the release nudge can't fire"
    )

    # 3) It NEVER warmed purchase_intent toward a fake yes — it stayed at the cold-start floor.
    assert sim.state.purchase_intent <= 0.1, (
        f"unqualified prospect warmed purchase_intent toward a commit ({sim.state.purchase_intent})"
    )


def test_conviction_couples_purchase_intent_for_a_budgeted_prospect():
    """purchase_intent tracks CONVICTION (trust + need) for a prospect who can actually buy: when both
    are high, even an empty LLM delta pulls intent UP toward the commit bar — fixing the crawl that
    left intent ~3x behind trust/need, so warm, qualified prospects walked before intent reached the
    threshold. Low conviction -> no free intent. (No-budget prospects are budget-gated OUT of this
    coupling — see the no_budget honesty test above, which must still hold.)"""
    sim = ProspectSimulator(_qualified_persona(), seed=1)  # budget 0.9 -> coupling active
    sim.state.trust = 0.9
    sim.state.need = 0.85
    sim.state.purchase_intent = 0.1
    sim._apply_soft_deltas({})  # no LLM movement -> only the conviction coupling acts
    assert sim.state.purchase_intent > 0.2  # pulled up toward conviction (was crawling)

    cold = ProspectSimulator(_qualified_persona(), seed=1)
    cold.state.trust = 0.3
    cold.state.need = 0.3
    cold.state.purchase_intent = 0.1
    cold._apply_soft_deltas({})
    assert cold.state.purchase_intent == pytest.approx(0.1)  # unconvinced -> no intent gain

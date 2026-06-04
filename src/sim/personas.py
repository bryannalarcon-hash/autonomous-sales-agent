# The hidden ground-truth Persona schema for the U7 prospect simulator + a deterministic, seeded
# population sampler (R14). A Persona is the COUNTERPARTY's true latent state the AGENT never sees:
# hidden utility dims (budget/need/trust/urgency/patience), RESPER-style objection resistances, the
# ground-truth qualified/disqualifier label the grader (U9) checks the agent's verdict against, and
# a hard commitment ceiling (max_commitment). sample_population() draws a reproducible population
# from a handful of tutoring-grounded archetype templates using a SEEDED random.Random (never the
# global random module), with a configurable genuinely-unqualified fraction.
# CB-43 adds 3 DEEPLY-PROMPTED, NAMED eval personas (build_dana/build_marcus/build_pat, registry
# NAMED_PERSONAS + named_persona()): each carries a rich `behavior_prompt` (backstory + objection
# script & timing + patience/walk/convert triggers) and a tuple of `non_sequiturs` (occasional
# realistic curveballs), which src/sim/prospect.py weaves into the sim-LLM prompt — NOT mere numeric
# template jitter. Their hidden-utility/ceiling numbers stay consistent with the honest buy-gate
# (Dana/Marcus qualified→enrollment; Pat UNqualified/no_authority→callback, unclosable above callback).
# Pure stdlib; consumed by src/sim/prospect.py (the engine) and U8 self-play / U9 grading. No LLM, no
# I/O here.
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

# Commitment ladder, strongest -> weakest, mirroring the agent's policy LADDER_TIERS plus a "none"
# sentinel for personas who can never commit. Kept in sync with src/core/policy.LADDER_TIERS so the
# grader (U9) compares the agent's tier verdict against the same vocabulary. "none" is the floor.
LADDER: tuple[str, ...] = ("enrollment", "trial", "consultation", "callback")
LADDER_WITH_NONE: tuple[str, ...] = LADDER + ("none",)

# The five hidden utility dimensions, each in [0, 1]. `budget` is a STRUCTURAL fact (immutable by
# talk); the rest seed the prospect's initial soft drivers in the engine.
UTILITY_DIMS: tuple[str, ...] = ("budget", "need", "trust", "urgency", "patience")

# The three structural disqualifiers (R14). disqualifier is None IFF qualified is True.
DISQUALIFIERS: tuple[str, ...] = ("no_budget", "no_authority", "no_real_need")


@dataclass
class Persona:
    """The hidden ground-truth a synthetic prospect carries; the AGENT only ESTIMATES this via DST.

    `utility` and `resistance` are dicts of [0,1] floats. `qualified` + `disqualifier` are the
    ground-truth labels the grader (U9) compares against (disqualifier is None iff qualified).
    `max_commitment` is the HARD ladder ceiling this persona can ever reach — the engine's buy-gate
    never returns a tier above it. Unqualified personas cap at "callback" or "none".
    """

    persona_id: str
    archetype: str
    style: str
    utility: dict[str, float]
    resistance: dict[str, float]
    decision_factors: list[str] = field(default_factory=list)
    difficulty: float = 0.5
    qualified: bool = True
    disqualifier: Optional[str] = None
    max_commitment: str = "enrollment"
    # CB-43 deeply-prompted fields (empty for the template-jitter archetypes; rich for the 3 named
    # eval personas). `behavior_prompt` is free-text the prospect prompt prepends so THIS specific
    # personality speaks (backstory + objection script/timing + walk/convert triggers). `non_sequiturs`
    # are realistic curveball lines the engine injects OCCASIONALLY (seeded, ~1-in-N turns) so the
    # agent is stressed on wrong-phase / repeat-question / prescriptiveness handling — never constant.
    name: str = ""
    behavior_prompt: str = ""
    non_sequiturs: tuple[str, ...] = ()


# --- Archetype templates ----------------------------------------------------------------------
# Each template gives a base utility/resistance + the soft-driver ranges the seeded rng varies
# within. Numbers are tutoring-grounded cold-start priors; the sampler jitters them per-persona so
# a population has spread while staying deterministic for a given seed.

# Qualified archetypes: genuine need + real (varying) budget; ceiling = enrollment (the full ladder
# is reachable in principle, but only if the agent earns the drivers).
_QUALIFIED_TEMPLATES = (
    {
        "archetype": "anxious_parent",
        "style": "chatty",
        "decision_factors": ["proof_of_results", "scheduling_flexibility"],
        "budget": (0.55, 0.9),
        "need": (0.6, 0.9),
        "trust": (0.35, 0.6),
        "urgency": (0.5, 0.85),
        "patience": (0.55, 0.85),
        "resistance": {"price": 0.4, "efficacy_doubt": 0.5, "diy_free": 0.2, "timing": 0.3},
        "difficulty": (0.4, 0.7),
    },
    {
        "archetype": "skeptical_researcher",
        "style": "skeptical",
        "decision_factors": ["proof_of_results", "tutor_credentials"],
        "budget": (0.5, 0.85),
        "need": (0.45, 0.75),
        "trust": (0.2, 0.45),
        "urgency": (0.3, 0.6),
        "patience": (0.6, 0.9),
        "resistance": {"price": 0.3, "efficacy_doubt": 0.8, "diy_free": 0.5, "timing": 0.2},
        "difficulty": (0.6, 0.85),
    },
    {
        "archetype": "deadline_crunch",
        "style": "terse",
        "decision_factors": ["scheduling_flexibility", "fast_start"],
        "budget": (0.5, 0.85),
        "need": (0.6, 0.9),
        "trust": (0.4, 0.65),
        "urgency": (0.8, 0.98),
        "patience": (0.3, 0.55),
        "resistance": {"price": 0.4, "efficacy_doubt": 0.3, "diy_free": 0.2, "timing": 0.6},
        "difficulty": (0.3, 0.6),
    },
    {
        "archetype": "budget_shopper",
        "style": "chatty",
        "decision_factors": ["price", "proof_of_results"],
        "budget": (0.45, 0.7),
        "need": (0.5, 0.8),
        "trust": (0.35, 0.6),
        "urgency": (0.4, 0.7),
        "patience": (0.5, 0.8),
        "resistance": {"price": 0.8, "efficacy_doubt": 0.4, "diy_free": 0.6, "timing": 0.3},
        "difficulty": (0.5, 0.75),
    },
    {
        "archetype": "tire_kicker",
        "style": "distracted",
        "decision_factors": ["proof_of_results"],
        "budget": (0.5, 0.85),
        "need": (0.4, 0.65),
        "trust": (0.3, 0.55),
        "urgency": (0.2, 0.45),
        "patience": (0.4, 0.7),
        "resistance": {"price": 0.5, "efficacy_doubt": 0.5, "diy_free": 0.5, "timing": 0.4},
        "difficulty": (0.55, 0.8),
    },
)

# Unqualified archetypes: each maps to one structural disqualifier with a structurally-low utility
# that matches it, and a capped ceiling ("none" or "callback"). The agent can never close these no
# matter how fluent — budget is immutable, authority/need are structural facts.
_UNQUALIFIED_TEMPLATES = (
    {
        "archetype": "budget_shopper",
        "disqualifier": "no_budget",
        "style": "chatty",
        "decision_factors": ["price"],
        # budget pinned structurally low so the buy gate's budget>=0.4/0.6 can NEVER be met.
        "budget": (0.0, 0.1),
        "need": (0.5, 0.85),
        "trust": (0.35, 0.6),
        "urgency": (0.4, 0.7),
        "patience": (0.6, 0.95),
        "resistance": {"price": 0.9, "efficacy_doubt": 0.3, "diy_free": 0.6, "timing": 0.2},
        "difficulty": (0.6, 0.85),
        "max_commitment": ("none", "callback"),
    },
    {
        "archetype": "gatekeeper_no_authority",
        "disqualifier": "no_authority",
        "style": "terse",
        "decision_factors": ["defer_to_decision_maker"],
        "budget": (0.4, 0.8),
        "need": (0.4, 0.75),
        "trust": (0.4, 0.65),
        "urgency": (0.3, 0.6),
        "patience": (0.5, 0.85),
        "resistance": {"price": 0.3, "efficacy_doubt": 0.3, "diy_free": 0.2, "timing": 0.4},
        "difficulty": (0.5, 0.8),
        # Can at most book a callback for the real decision-maker; never buys.
        "max_commitment": ("none", "callback"),
    },
    {
        "archetype": "tire_kicker",
        "disqualifier": "no_real_need",
        "style": "distracted",
        "decision_factors": ["curiosity"],
        "budget": (0.4, 0.85),
        # need pinned structurally low — there is no genuine problem to solve.
        "need": (0.0, 0.15),
        "trust": (0.35, 0.6),
        "urgency": (0.1, 0.35),
        "patience": (0.5, 0.85),
        "resistance": {"price": 0.4, "efficacy_doubt": 0.5, "diy_free": 0.8, "timing": 0.3},
        "difficulty": (0.5, 0.8),
        "max_commitment": ("none", "callback"),
    },
)


def _u(rng: random.Random, lo: float, hi: float) -> float:
    """Draw a value in [lo, hi] from the seeded rng, rounded to 3 dp for stable equality/logging."""
    return round(rng.uniform(lo, hi), 3)


def _build_qualified(rng: random.Random, idx: int) -> Persona:
    """Build one genuinely-qualified persona from a seeded template choice (ceiling = enrollment)."""
    tpl = rng.choice(_QUALIFIED_TEMPLATES)
    utility = {dim: _u(rng, *tpl[dim]) for dim in UTILITY_DIMS}
    return Persona(
        persona_id=f"qual_{idx:04d}",
        archetype=tpl["archetype"],
        style=tpl["style"],
        utility=utility,
        resistance=dict(tpl["resistance"]),
        decision_factors=list(tpl["decision_factors"]),
        difficulty=_u(rng, *tpl["difficulty"]),
        qualified=True,
        disqualifier=None,
        max_commitment="enrollment",
    )


def _build_unqualified(rng: random.Random, idx: int) -> Persona:
    """Build one structurally-unqualified persona: a real disqualifier + matching low utility +
    a capped ceiling ("none"/"callback"). The structural-low dim makes the buy gate unreachable."""
    tpl = rng.choice(_UNQUALIFIED_TEMPLATES)
    utility = {dim: _u(rng, *tpl[dim]) for dim in UTILITY_DIMS}
    ceiling = rng.choice(tpl["max_commitment"])
    return Persona(
        persona_id=f"unqual_{idx:04d}",
        archetype=tpl["archetype"],
        style=tpl["style"],
        utility=utility,
        resistance=dict(tpl["resistance"]),
        decision_factors=list(tpl["decision_factors"]),
        difficulty=_u(rng, *tpl["difficulty"]),
        qualified=False,
        disqualifier=tpl["disqualifier"],
        max_commitment=ceiling,
    )


def sample_population(
    n: int,
    *,
    seed: int,
    unqualified_fraction: float = 0.25,
) -> list[Persona]:
    """Draw a deterministic population of `n` personas: ~unqualified_fraction genuinely unqualified
    (R14), the rest qualified, all varied by the SEEDED rng so the same seed reproduces the EXACT
    population. Uses random.Random(seed) — NEVER the global random module.

    The unqualified personas are placed at deterministic positions (the first `k`) and then the whole
    list is shuffled with the same rng, so order is reproducible yet mixed. Each unqualified persona
    gets a real disqualifier + a structurally-low utility dim matching it + a capped ceiling.
    """
    if n < 0:
        raise ValueError("population size must be non-negative")
    if not 0.0 <= unqualified_fraction <= 1.0:
        raise ValueError("unqualified_fraction must be in [0, 1]")

    rng = random.Random(seed)
    k_unqualified = round(n * unqualified_fraction)
    k_unqualified = max(0, min(k_unqualified, n))

    population: list[Persona] = []
    for i in range(k_unqualified):
        population.append(_build_unqualified(rng, i))
    for i in range(n - k_unqualified):
        population.append(_build_qualified(rng, i))

    rng.shuffle(population)
    return population


# --- CB-43: the 3 DEEPLY-PROMPTED, NAMED eval personas ----------------------------------------
# Quality-over-quantity eval set: a SMALL number of distinct personalities, each with a rich
# behavioral script + seeded curveballs, beats hundreds of shallow stubs (the CB-42 "8-8 tie" was
# untrustworthy precisely because the stubs were too shallow). These are LOCKED by the orchestrator
# in docs/change-board.md (CB-43) — implement exactly these three, do not invent new ones. Each
# builder returns a FRESH Persona (stable persona_id) whose hidden-utility/ceiling numbers stay
# consistent with the honest buy-gate. The behavior_prompt / non_sequiturs are consumed by
# src/sim/prospect.py's prompt seam; they NEVER touch the pure buy_gate.

def build_dana() -> Persona:
    """P-A "Dana" — anxious over-researcher, a QUALIFIED but hard-to-close warm lead (ceiling=
    enrollment). 8th-grader struggling in Algebra 1, state test ~10 weeks out; has budget but is
    terrified of wasting money after a free app + a school tutor both flopped. Trust climbs slowly;
    rotates efficacy_doubt -> price -> "maybe I should ask other parents." High patience. Converts on
    concrete proof + scheduling flexibility + a low-commitment trial; walks if pushed to close before
    efficacy is addressed."""
    return Persona(
        persona_id="eval_dana_pa",
        name="Dana",
        archetype="anxious_parent",
        style="chatty, warm-but-worried, over-explains",
        # Real budget + genuine need (closable). Trust starts LOW (the burned, skeptical part);
        # urgency moderate (10 wks); patience HIGH (will stay on the call a long time).
        utility={"budget": 0.8, "need": 0.78, "trust": 0.3, "urgency": 0.6, "patience": 0.9},
        # Efficacy doubt is the dominant, recurring concern; price is secondary; little DIY/timing pull.
        resistance={"price": 0.55, "efficacy_doubt": 0.85, "diy_free": 0.3, "timing": 0.2},
        decision_factors=["proof_of_results", "scheduling_flexibility", "low_commitment_trial"],
        difficulty=0.7,
        qualified=True,
        disqualifier=None,
        max_commitment="enrollment",
        behavior_prompt=(
            "You are DANA, a worried parent of an 8th grader who is struggling in Algebra 1, with a "
            "state test in about 10 weeks. You DO have money set aside for this, but you are TERRIFIED "
            "of wasting it: you already paid for a tutoring app your kid never opened, and a school "
            "tutor who didn't help. So you are warm and friendly but your trust climbs SLOWLY — you "
            "need to be convinced this is different. OBJECTION SCRIPT & TIMING: early on, your first "
            "and biggest worry is whether this ACTUALLY WORKS (efficacy) — press for concrete proof, "
            "results, how they'd help with Algebra specifically. Only after that's addressed do you "
            "raise PRICE (is it worth it, can you justify it). Later you may waver with 'maybe I "
            "should ask other parents what they used' — a stalling worry, not a hard no. You over-"
            "explain and ramble a bit. CONVERT when the agent gives you concrete proof it works, "
            "shows scheduling flexibility, AND offers a LOW-COMMITMENT way in (a trial) so you're not "
            "betting everything up front — then you can warm to a real enrollment. WALK (get cold, "
            "lose patience) if the agent tries to CLOSE you before your efficacy worry has genuinely "
            "been answered — being pushed to commit while still scared feels exactly like the last "
            "time you got burned. You are patient otherwise and will stay on a long, thorough call."
        ),
        non_sequiturs=(
            "Oh sorry, hang on — I'm trying to sort out my younger one's soccer schedule, it's chaos. "
            "Anyway, what were you saying?",
            "Actually, totally off topic, but do you all also do SAT prep? My older kid is starting "
            "college apps and I'm drowning.",
            # answers a DIFFERENT question than asked (price when asked about grade):
            "Well honestly the price is my big question — is this going to be hundreds a month?",
            "My partner keeps asking how much this costs, so that's on my mind too.",
        ),
    )


def build_marcus() -> Persona:
    """P-B "Marcus" — terse, skeptical deadline-crunch, QUALIFIED but fast and prickly (ceiling=
    enrollment). Son is a junior, SAT in 5 weeks, wants results NOW; budget is fine; distrusts
    scripts. Terse one-liners; pushes back on discovery ("why does that matter, just tell me the
    price?"). LOW patience, VERY HIGH urgency. Converts FAST on a direct, competent fast-start +
    trial; walks fast on over-discovery / wishy-washy answers."""
    return Persona(
        persona_id="eval_marcus_pb",
        name="Marcus",
        archetype="deadline_crunch",
        style="terse, blunt one-liners, impatient",
        # Budget fine + real need; trust low (distrusts scripts) but moves on COMPETENCE; urgency very
        # high (SAT in 5 wks); patience LOW (walks fast on fluff).
        utility={"budget": 0.85, "need": 0.7, "trust": 0.35, "urgency": 0.92, "patience": 0.35},
        # Timing/urgency pressure dominates; efficacy doubt present (distrusts scripts); low price/DIY.
        resistance={"price": 0.2, "efficacy_doubt": 0.6, "diy_free": 0.25, "timing": 0.7},
        decision_factors=["fast_start", "direct_competence", "low_commitment_trial"],
        difficulty=0.55,
        qualified=True,
        disqualifier=None,
        max_commitment="enrollment",
        behavior_prompt=(
            "You are MARCUS, blunt and busy. Your son is a junior with the SAT in 5 WEEKS and you want "
            "RESULTS NOW. Money is not the issue — you can pay. But you DISTRUST scripted salespeople "
            "and you have NO patience for filler. SPEAK IN TERSE ONE-LINERS. OBJECTION SCRIPT & "
            "TIMING: when the agent tries to run discovery questions, PUSH BACK fast — 'why does that "
            "matter?', 'just tell me the price', 'can you actually move his score in 5 weeks or not?'. "
            "Your real concern is whether they can deliver a FAST, competent start; efficacy/can-they-"
            "actually-do-it under your deadline. CONVERT FAST — within a few turns — if the agent is "
            "DIRECT, competent, and proposes a concrete fast-start (a trial session this week, a clear "
            "plan for the 5 weeks); you'll move straight toward enrollment, no hand-holding. WALK FAST "
            "(curt, done) if the agent over-discovers (keeps asking soft questions), gives wishy-washy "
            "non-answers, or sounds like a script. Low patience: you do not stay on a meandering call."
        ),
        non_sequiturs=(
            # hard pivot:
            "Forget the SAT for a sec — can you also do his AP Chem? He's failing that too.",
            # curveball skepticism:
            "My neighbor said these tutoring outfits are basically a scam. Convince me they're not.",
            # non-answer the agent must convert to a next step:
            "Look, just send me something. I'll look at it later.",
            "I don't have time for twenty questions. Bottom line it for me.",
        ),
    )


def build_pat() -> Persona:
    """P-C "Pat" — distracted gatekeeper, UNQUALIFIED (no_authority, ceiling=callback). Calling for
    their SISTER's kid; not the decision-maker, not paying, vaguely curious. Gives fuzzy answers
    ("she's in like 7th? maybe 8th"); CANNOT commit budget or authority. The CORRECT outcome is the
    agent discovering the no-authority and booking a CALLBACK — never attempting enrollment. The pure
    buy_gate enforces this: Pat can NEVER close above callback no matter how warm or what's offered."""
    return Persona(
        persona_id="eval_pat_pc",
        name="Pat",
        archetype="gatekeeper_no_authority",
        style="distracted, vague, easily sidetracked",
        # Mid budget-FEELING but it's not THEIRS (authority is the structural fact); low real need (it's
        # not their kid); low urgency; decent patience but distracted.
        utility={"budget": 0.5, "need": 0.4, "trust": 0.45, "urgency": 0.25, "patience": 0.6},
        resistance={"price": 0.3, "efficacy_doubt": 0.3, "diy_free": 0.3, "timing": 0.3},
        decision_factors=["defer_to_decision_maker"],
        difficulty=0.65,
        qualified=False,
        disqualifier="no_authority",
        # HARD ceiling: at most a callback for the real decision-maker (the sister). Never buys.
        max_commitment="callback",
        behavior_prompt=(
            "You are PAT, calling on behalf of your SISTER about HER kid — you are NOT the decision-"
            "maker and you are NOT paying. You're only vaguely curious / doing your sister a favor, and "
            "you're DISTRACTED (a dog, another room, half-listening). You give FUZZY answers about the "
            "kid because it isn't yours — 'she's in like 7th grade? maybe 8th', 'I think she's "
            "struggling in math, or maybe it's science'. OBJECTION / STRUCTURAL TRUTH: you genuinely "
            "cannot commit budget OR sign anything up — that's your sister's call. When the agent asks "
            "qualifying questions, make it increasingly clear you'd have to check with your sister / "
            "it's not your decision. Do NOT pretend you can enroll, pay, or decide. The RIGHT ending "
            "is the agent realizing you're not the decision-maker and offering to schedule a CALLBACK "
            "(or to follow up with your sister) — accept THAT. Never agree to enroll, pay, or book a "
            "trial as if it were yours. You're friendly enough and reasonably patient, just scattered."
        ),
        non_sequiturs=(
            # distraction:
            "Sorry — my dog just knocked something over. What did you ask?",
            # answers for the wrong child:
            "Wait, are we talking about my nephew or my niece? I keep mixing them up.",
            # contradictory budget-then-deferral:
            "I mean, we could probably spend a couple hundred — well, actually, that's up to my "
            "sister, not me.",
            "Hang on, someone's at the door — keep going, I'm listening.",
        ),
    )


# The locked CB-43 eval set, keyed by short name. named_persona() resolves a name to a FRESH Persona
# (calling the builder each time, so a caller can't mutate a shared instance). Exactly these three.
_NAMED_BUILDERS: dict[str, "callable"] = {  # noqa: F821 (callable hint; runtime values are functions)
    "dana": build_dana,
    "marcus": build_marcus,
    "pat": build_pat,
}
# Public tuple of the registered short names (for runners/tests to enumerate the locked set).
NAMED_PERSONAS: tuple[str, ...] = ("dana", "marcus", "pat")


def named_persona(name: str) -> Persona:
    """Return a FRESH instance of a named CB-43 eval persona by short name (case-insensitive).
    Raises KeyError for an unknown name (loud — never silently substitutes a default)."""
    key = (name or "").strip().lower()
    if key not in _NAMED_BUILDERS:
        raise KeyError(f"unknown named persona {name!r}; known: {NAMED_PERSONAS}")
    return _NAMED_BUILDERS[key]()

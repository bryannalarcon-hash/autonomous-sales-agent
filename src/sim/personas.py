# The hidden ground-truth Persona schema for the U7 prospect simulator + a deterministic, seeded
# population sampler (R14). A Persona is the COUNTERPARTY's true latent state the AGENT never sees:
# hidden utility dims (budget/need/trust/urgency/patience), RESPER-style objection resistances, the
# ground-truth qualified/disqualifier label the grader (U9) checks the agent's verdict against, and
# a hard commitment ceiling (max_commitment). sample_population() draws a reproducible population
# from a handful of tutoring-grounded archetype templates using a SEEDED random.Random (never the
# global random module), with a configurable genuinely-unqualified fraction. Pure stdlib; consumed
# by src/sim/prospect.py (the engine) and, later, U8 self-play / U9 grading. No LLM, no I/O here.
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

# The headless prospect-simulation package (U7) — the honest synthetic counterparty the agent
# practices against in self-play. Re-exports the Persona schema + seeded population sampler
# (personas.py) and the ProspectSimulator engine with its PURE deterministic buy_gate (prospect.py),
# the honesty backbone (R31): talk moves only soft drivers, structural facts (budget/qualification)
# are immutable. Pure stdlib + the src.core.llm seam; NO LiveKit / DB / src.memory imports.
from __future__ import annotations

from src.sim.personas import (
    DISQUALIFIERS,
    LADDER,
    LADDER_WITH_NONE,
    UTILITY_DIMS,
    Persona,
    sample_population,
)
from src.sim.prospect import (
    SIM_MODEL,
    SOFT_DRIVERS,
    ProspectSimulator,
    ProspectState,
    ProspectTurn,
    buy_gate,
)

__all__ = [
    "Persona",
    "sample_population",
    "LADDER",
    "LADDER_WITH_NONE",
    "UTILITY_DIMS",
    "DISQUALIFIERS",
    "ProspectSimulator",
    "ProspectState",
    "ProspectTurn",
    "buy_gate",
    "SOFT_DRIVERS",
    "SIM_MODEL",
]

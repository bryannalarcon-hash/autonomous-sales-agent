# Transport-agnostic brain core (plan KTD2 / Phase 1). Imports NO LiveKit/voice types: both the
# headless text self-play and the live voice adapter call into this package, so what is optimized
# in text is exactly what ships in voice. Re-exports the shared LLM seam, the belief-state / DST
# primitives, and the U4 brain: the gated policy (Decision/decide), the deterministic gates
# (apply_gates), NLG (realize), and the orchestrating respond() — the one entry point adapters call.
from __future__ import annotations

from src.core.belief_state import DRIVERS, BeliefState
from src.core.dst import update as dst_update
from src.core.gates import apply_gates
from src.core.llm import LLMClient, MockLLMClient, OpenRouterClient
from src.core.nlg import realize
from src.core.policy import ACTS, LADDER_TIERS, Decision, decide
from src.core.respond import respond

__all__ = [
    "DRIVERS",
    "BeliefState",
    "dst_update",
    "LLMClient",
    "MockLLMClient",
    "OpenRouterClient",
    "Decision",
    "decide",
    "ACTS",
    "LADDER_TIERS",
    "apply_gates",
    "realize",
    "respond",
]

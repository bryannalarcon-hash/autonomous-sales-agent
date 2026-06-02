# GenerativeTwin — a ProspectSimulator subclass rebuilt from a real Episode (plan U15). Seeds
# its synthetic Persona and initial state from the real call's per-turn agent-belief mood arc and
# the prospect's utterances, then responds GENERATIVELY (via LLM) while anchoring each turn's soft
# drivers a fraction (_TWIN_ANCHOR) toward the real arc target. The honest buy_gate, patience decay,
# and structural immutability from the base class are preserved unchanged. Collaborators:
# src.sim.prospect (ProspectSimulator, buy_gate, _TWIN_ANCHOR, SOFT_DRIVERS, _clamp01),
# src.sim.personas (Persona), src.memory.schema (Episode, BeliefSnapshot), src.core.llm (LLMClient).
from __future__ import annotations

from typing import Optional

from src.core.llm import LLMClient
from src.memory.schema import BeliefSnapshot, Episode
from src.sim.personas import Persona
from src.sim.prospect import (
    SOFT_DRIVERS,
    ProspectSimulator,
    ProspectTurn,
    _TWIN_ANCHOR,
    _clamp01,
    _build_sim_messages,
)

# Budget utility assigned to the twin's synthetic persona when the real call committed a paid tier
# (the prospect clearly had real budget) vs. when it did not.
_TWIN_BUDGET_COMMITTED = 0.8
_TWIN_BUDGET_COLD = 0.2

# Paid tiers: used to decide whether the real call committed to something that indicates real budget.
_PAID_TIERS = frozenset({"consultation", "trial", "enrollment"})

# Fallback max_commitment for a twin built from an uncommitted episode — no cap, twin can reach any
# tier if the drivers get there (we don't want to artificially prevent a partial-commit replays).
_FALLBACK_MAX_COMMITMENT = "enrollment"


def _extract_committed_tier(episode: Episode) -> Optional[str]:
    """Return the tier the real call committed to (from metrics or outcome label), or None."""
    # Prefer the explicit committed_tier metric if present
    tier = (episode.metrics or {}).get("committed_tier")
    if tier and tier in ("consultation", "trial", "enrollment", "callback"):
        return tier
    # Fall back to parsing the outcome label
    outcome = episode.outcome or ""
    if outcome == "consult_booked":
        return "consultation"
    if outcome == "trial_booked":
        return "trial"
    if outcome == "enrolled":
        return "enrollment"
    if outcome == "callback_scheduled":
        return "callback"
    return None


def _extract_mood_arc(episode: Episode) -> list[BeliefSnapshot]:
    """Extract the agent's per-turn belief snapshots in turn order (the mood arc)."""
    arc: list[BeliefSnapshot] = []
    for t in episode.turns:
        if t.speaker == "agent" and isinstance(t.belief, BeliefSnapshot):
            arc.append(t.belief)
    return arc


def _extract_prospect_utterances(episode: Episode) -> list[str]:
    """Extract prospect turn texts in order."""
    return [t.text for t in episode.turns if t.speaker == "prospect" and t.text]


def _arc_target_at(arc: list[BeliefSnapshot], turn_index: int) -> Optional[dict[str, float]]:
    """Return the driver dict from the arc snapshot nearest to `turn_index`, or None if arc is empty.
    When the replay goes longer than the real call, returns the last arc entry's drivers."""
    if not arc:
        return None
    idx = min(turn_index, len(arc) - 1)
    snap = arc[idx]
    return {k: float(v) for k, v in (snap.drivers or {}).items() if k in SOFT_DRIVERS}


def _utterance_near(utterances: list[str], turn_index: int, window: int = 2) -> list[str]:
    """Return up to `window` real utterances centred around `turn_index` for prompt grounding."""
    if not utterances:
        return []
    lo = max(0, turn_index - window // 2)
    hi = min(len(utterances), lo + window)
    return utterances[lo:hi]


class GenerativeTwin(ProspectSimulator):
    """A generative digital-twin prospect rebuilt from a real Episode (plan U15).

    Subclasses ProspectSimulator to inherit the honest buy_gate, patience decay, soft-driver
    clamping, and structural immutability. Overrides the LLM prompt to anchor each response to the
    real call's mood arc + utterances, while still reacting naturally to the live agent. After each
    LLM turn, soft drivers are anchored _TWIN_ANCHOR of the way toward the arc target for that
    turn — keeping the twin tracking reality while remaining generative.
    """

    def __init__(self, real_episode: Episode, *, seed: int = 0) -> None:
        # --- Extract artefacts from the real episode ----------------------------------------
        self._mood_arc: list[BeliefSnapshot] = _extract_mood_arc(real_episode)
        self._real_utterances: list[str] = _extract_prospect_utterances(real_episode)
        self._turn_index: int = 0  # tracks which arc position we are at each respond()

        committed_tier = _extract_committed_tier(real_episode)

        # --- Build synthetic Persona --------------------------------------------------------
        # max_commitment: the twin CAN reach the tier the real call reached (no artificial cap);
        # for uncommitted episodes, use the enrollment ceiling so we don't prevent any tier.
        max_commitment = committed_tier if committed_tier else _FALLBACK_MAX_COMMITMENT

        # budget utility: high (~0.8) if the real call committed a paid tier; inferred low (~0.2)
        # otherwise (a budget-constrained or walkaway prospect is unlikely to have high budget).
        budget_utility = (
            _TWIN_BUDGET_COMMITTED if committed_tier in _PAID_TIERS else _TWIN_BUDGET_COLD
        )

        # Seed trust/need/urgency from the FIRST agent belief snapshot's drivers (if available).
        first_drivers: dict[str, float] = {}
        if self._mood_arc:
            first_drivers = {
                k: float(v)
                for k, v in (self._mood_arc[0].drivers or {}).items()
                if k in SOFT_DRIVERS
            }

        synthetic_persona = Persona(
            persona_id=f"twin-{real_episode.episode_id}",
            archetype=real_episode.persona or "unknown",
            style="natural",
            utility={
                "budget": budget_utility,
                "need": _clamp01(first_drivers.get("need", 0.5)),
                "trust": _clamp01(first_drivers.get("trust", 0.3)),
                "urgency": _clamp01(first_drivers.get("urgency", 0.4)),
                "patience": 0.7,  # default patience; the twin runs the full arc
            },
            resistance={
                "price": 0.4,
                "efficacy_doubt": 0.4,
                "diy_free": 0.3,
                "timing": 0.3,
            },
            decision_factors=["proof_of_results"],
            difficulty=0.5,
            qualified=committed_tier in _PAID_TIERS,
            disqualifier=None,
            max_commitment=max_commitment,
        )

        super().__init__(synthetic_persona, seed=seed)

        # Overwrite initial state drivers from the first belief snapshot (more accurate than the
        # persona.utility seeding done by ProspectState.initial).
        if first_drivers:
            for key in SOFT_DRIVERS:
                if key in first_drivers:
                    setattr(self.state, key, _clamp01(first_drivers[key]))

    # --- Prompt override: generative but anchored to the real call --------------------------

    def _build_twin_messages(
        self,
        agent_utterance: str,
        agent_act: Optional[str],
    ) -> list[dict]:
        """Build the twin's LLM prompt. Inherits the base prospect prompt (same JSON contract) but
        PREPENDS a grounding paragraph with nearby real utterances and the arc target drivers, so
        the LLM responds in the spirit of the real call while reacting to the live agent."""
        # Nearby real utterances for grounding
        nearby = _utterance_near(self._real_utterances, self._turn_index)
        nearby_text = (
            "Around this point in the real call, the prospect said things like: "
            + "; ".join(f'"{u}"' for u in nearby)
            if nearby
            else ""
        )

        # Arc target drivers at this turn
        arc_drivers = _arc_target_at(self._mood_arc, self._turn_index) or {}
        arc_text = (
            "Their emotional arc at this turn was roughly: "
            + ", ".join(f"{k}={round(v, 2)}" for k, v in arc_drivers.items())
            if arc_drivers
            else ""
        )

        twin_preamble = (
            "You are re-enacting a real prospect from a real tutoring sales call. "
            f"{nearby_text}. {arc_text}. "
            "The agent just said: "
            f"{agent_utterance!r}. "
            "Respond in character, reacting naturally to the agent, generally following that "
            "emotional arc. Emit soft-driver deltas as JSON."
        ).strip()

        # Get the base messages from the parent class prompt builder
        base_messages = _build_sim_messages(self.persona, self.state, agent_utterance, agent_act)

        # Inject the twin preamble into the system message
        if base_messages and base_messages[0].get("role") == "system":
            original_system = base_messages[0]["content"]
            base_messages[0] = {
                "role": "system",
                "content": twin_preamble + "\n\n" + original_system,
            }

        return base_messages

    def _sim_messages(self, agent_utterance: str, agent_act: Optional[str]) -> list:
        """Override the base prospect prompt with the twin's arc/utterance-grounded prompt. The base
        respond() calls self._sim_messages, so this slots in with NO module-global monkey-patching —
        concurrent twins never race on a shared builder reference."""
        return self._build_twin_messages(agent_utterance, agent_act)

    # --- respond() override: anchor soft drivers toward the real arc after each LLM turn ----------

    async def respond(
        self,
        agent_utterance: str,
        agent_act: Optional[str],
        llm_client: LLMClient,
        *,
        agent_tier: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ProspectTurn:
        """One twin turn. The base respond() runs UNCHANGED (patience decay, the LLM call through our
        overridden _sim_messages prompt, buy_gate, delta application); then we ANCHOR each soft driver
        _TWIN_ANCHOR of the way toward the real-call arc target for this turn — so the twin keeps
        tracking reality while reacting to the live agent. The buy_gate has already fired for THIS
        turn (on the pre-anchor drivers); the anchor shapes the trajectory going into the next turn."""
        turn = await super().respond(
            agent_utterance, agent_act, llm_client, agent_tier=agent_tier, model=model
        )
        arc_drivers = _arc_target_at(self._mood_arc, self._turn_index) or {}
        for key in SOFT_DRIVERS:
            if key in arc_drivers:
                current = float(getattr(self.state, key))
                target = arc_drivers[key]
                setattr(self.state, key, _clamp01(current + _TWIN_ANCHOR * (target - current)))
        self._turn_index += 1
        return turn

# The honest hidden-utility ProspectSimulator (U7) — the synthetic counterparty the agent practices
# against in self-play. Its INTEGRITY is the point: the sim LLM (a different model family than the
# agent; mocked in tests) drives only the prospect's TALK and proposes BOUNDED deltas to the SOFT
# drivers (trust/need/urgency/purchase_intent); commit/walk is decided by buy_gate(), a PURE,
# DETERMINISTIC numeric gate on the TRUE hidden drivers + the persona's hard ceiling (R31). budget
# and qualified/disqualifier are STRUCTURAL FACTS the LLM may NEVER mutate by talking — so a
# no_budget prospect can never cross the budget gate no matter how fluent the agent is. Robust like
# the DST: malformed/missing JSON -> no delta; unknown/structural keys ignored. Headless + pure:
# NO LiveKit, NO DB, NO src.memory, NO src.core.respond/policy imports (the prospect is the
# independent counterparty; episodes are logged later by U8). Collaborators: src.sim.personas,
# the src.core.llm LLMClient seam.
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Optional

from src.core.llm import LLMClient, Message
from src.sim.personas import LADDER, Persona

# The SOFT drivers the sim LLM is permitted to nudge (clamped). budget is intentionally NOT here:
# it lives only on the immutable persona.utility and can never be moved by talk.
SOFT_DRIVERS: tuple[str, ...] = ("trust", "need", "urgency", "purchase_intent")

# Default sim model id (overridable per call). A DIFFERENT family than the agent (KTD5); mocked in
# tests so it is never actually contacted there.
SIM_MODEL = "openai/gpt-4o-mini"

# Per-turn delta clamp for the soft drivers (mirrors the DST's bounded-update discipline) and the
# level clamp to [0, 1].
_MAX_DELTA = 0.2
_PATIENCE_BASE_DECAY = 0.12          # patience lost every turn regardless of agent behavior
_PATIENCE_PRESSURE_PENALTY = 0.18    # extra loss when a pressure act lands on low trust
_LOW_TRUST_THRESHOLD = 0.5           # below this, pressure acts are felt as pushiness
_PRESSURE_ACTS = frozenset({"attempt_close", "pitch"})


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _clamp_delta(d: float) -> float:
    if d < -_MAX_DELTA:
        return -_MAX_DELTA
    if d > _MAX_DELTA:
        return _MAX_DELTA
    return d


@dataclass
class ProspectState:
    """The MUTABLE per-turn state of a prospect. Soft drivers (trust/need/urgency/purchase_intent)
    + patience evolve via clamped LLM deltas / deterministic decay; `budget` is read once from the
    immutable persona.utility and NEVER mutated (it is a structural fact, not a soft driver). The
    buy-gate reads these true drivers to decide commit/walk — talk only moves the SOFT ones.
    """

    trust: float
    need: float
    urgency: float
    purchase_intent: float
    patience: float
    budget: float  # IMMUTABLE mirror of persona.utility["budget"]; here only so the gate can read it
    turns_elapsed: int = 0
    walked: bool = False
    committed_tier: Optional[str] = None

    @classmethod
    def initial(cls, persona: Persona) -> "ProspectState":
        """Seed soft drivers from persona.utility; purchase_intent starts low (~0.1, a cold lead)."""
        u = persona.utility
        return cls(
            trust=_clamp01(float(u.get("trust", 0.5))),
            need=_clamp01(float(u.get("need", 0.5))),
            urgency=_clamp01(float(u.get("urgency", 0.5))),
            purchase_intent=0.1,
            patience=_clamp01(float(u.get("patience", 0.5))),
            budget=_clamp01(float(u.get("budget", 0.0))),
        )

    def snapshot(self) -> dict[str, float]:
        """A plain-dict view for the ProspectTurn (logging-friendly; never carries hidden labels)."""
        return asdict(self)


@dataclass
class ProspectTurn:
    """The result of one prospect response: the spoken `text`, the `outcome`
    ("continue"|"walked"|"committed:<tier>"), the committed tier (if any), whether the prospect
    walked, whether it surfaced its structural disqualifier this turn, and a state snapshot for
    the harness/grader. Deterministic given the same inputs + seed + mocked LLM."""

    text: str
    outcome: str
    committed_tier: Optional[str]
    walked: bool
    disqualifier_signaled: bool
    state_snapshot: dict[str, float] = field(default_factory=dict)


# --- THE DETERMINISTIC BUY-GATE (the honesty backbone, R31) -----------------------------------
# Per-tier thresholds on the TRUE hidden drivers. budget is a HARD gate per tier (an immutable
# structural fact), so no_budget personas can NEVER cross enrollment/trial. The agent's fluency
# cannot change these — only the true drivers + the persona's ceiling decide. Tiers strongest ->
# weakest; we return the HIGHEST tier whose thresholds are ALL met AND <= the persona ceiling.
_TIER_THRESHOLDS: dict[str, dict[str, float]] = {
    # tier:          need   trust  intent budget  (+urgency/patience where it matters)
    "enrollment": {"need": 0.7, "trust": 0.7, "purchase_intent": 0.7, "budget": 0.6},
    "trial":      {"need": 0.55, "trust": 0.55, "purchase_intent": 0.55, "budget": 0.4},
    "consultation": {"need": 0.4, "trust": 0.4, "purchase_intent": 0.4, "budget": 0.2},
    # callback is the lowest rung: a soft yes to keep talking later — mostly intent + a little trust,
    # NO budget gate (you can agree to a callback with no money).
    "callback":   {"purchase_intent": 0.3, "trust": 0.25},
}

# Rank for ceiling comparison: higher index = stronger tier.
_TIER_RANK: dict[str, int] = {tier: i for i, tier in enumerate(("callback", "consultation", "trial", "enrollment"))}


def _ceiling_rank(max_commitment: str) -> int:
    """Rank of the persona's hard ceiling. "none" -> -1 (no tier is ever allowed)."""
    if max_commitment == "none":
        return -1
    return _TIER_RANK.get(max_commitment, -1)


def _meets(state: ProspectState, thresholds: dict[str, float]) -> bool:
    """True iff every threshold (read off the TRUE drivers / immutable budget) is met."""
    for field_name, floor in thresholds.items():
        if float(getattr(state, field_name)) < floor:
            return False
    return True


def buy_gate(state: ProspectState, persona: Persona) -> Optional[str]:
    """PURE, DETERMINISTIC commit decision: the highest ladder tier whose driver+budget thresholds
    are ALL met AND whose strength is <= the persona's hard ceiling, else None. Depends ONLY on the
    true hidden drivers + the immutable budget + the persona ceiling — NEVER on how fluent the agent
    was. This is the honesty invariant: talk moves soft drivers, but the GATE is the arbiter.
    """
    ceiling = _ceiling_rank(persona.max_commitment)
    if ceiling < 0:
        return None  # a "none" persona can never commit
    # Walk the ladder strongest -> weakest; return the first (highest) tier that both clears its
    # thresholds AND is allowed by the ceiling.
    for tier in LADDER:  # ("enrollment","trial","consultation","callback")
        if _TIER_RANK[tier] > ceiling:
            continue  # above the persona's hard cap — not reachable
        if _meets(state, _TIER_THRESHOLDS[tier]):
            return tier
    return None


# --- The sim-LLM prompt (talk + SOFT-driver delta proposals only) -----------------------------

_SIM_SYSTEM = (
    "You ARE a prospect a tutoring salesperson just called — not an assistant. Stay fully in "
    "character. You have a hidden disposition; respond the way THIS person would. Output ONLY a "
    "JSON object: {\"utterance\": <what you say, 1-2 sentences in your style>, \"deltas\": {<how "
    "this exchange moved your feelings, signed numbers in [-0.2, 0.2]>}}. You may ONLY propose "
    "deltas for these soft feelings: trust, need, urgency, purchase_intent. You CANNOT change your "
    "budget, and you CANNOT decide to buy here — your real decision is made elsewhere. Never invent "
    "money or authority you don't have."
)


def _build_sim_messages(
    persona: Persona,
    state: ProspectState,
    agent_utterance: str,
    agent_act: Optional[str],
) -> list[Message]:
    """Assemble the in-character sim prompt. Unqualified personas get an explicit instruction to
    NATURALLY surface their structural disqualifier (e.g. 'I don't really have a budget for this'),
    so the agent has an honest chance to detect it — but voicing it never changes the structural
    fact itself."""
    disq_hint = ""
    if not persona.qualified and persona.disqualifier:
        human = {
            "no_budget": "you genuinely cannot afford this; mention you don't have a budget for it",
            "no_authority": "you are not the decision-maker; you'd have to check with someone else",
            "no_real_need": "you don't actually have a pressing need; you're mostly just curious",
        }.get(persona.disqualifier, "you are not a fit and should say so naturally")
        disq_hint = f"\nIMPORTANT: {human}. Surface this naturally when it fits."

    profile = (
        f"ARCHETYPE: {persona.archetype}\n"
        f"STYLE: {persona.style} (talk this way)\n"
        f"OBJECTION_PROPENSITIES: {persona.resistance}\n"
        f"WHAT_MOVES_YOU: {persona.decision_factors}\n"
        f"CURRENT_FEELINGS: trust={round(state.trust,2)} need={round(state.need,2)} "
        f"urgency={round(state.urgency,2)} purchase_intent={round(state.purchase_intent,2)} "
        f"patience={round(state.patience,2)}"
        f"{disq_hint}"
    )
    user = (
        f"{profile}\n\n"
        f"AGENT_ACT: {agent_act or 'none'}\n"
        f"AGENT_SAID: {agent_utterance!r}\n\n"
        "Reply now as this prospect (JSON only)."
    )
    return [
        {"role": "system", "content": _SIM_SYSTEM},
        {"role": "user", "content": user},
    ]


# Acceptance lines per tier (in-character enough; the harness/judge cares about the outcome, not
# the exact words — kept deterministic so reproducibility holds).
_ACCEPTANCE = {
    "enrollment": "Okay, let's go ahead and enroll.",
    "trial": "Alright, I'll try a trial session.",
    "consultation": "Sure, let's set up a consultation.",
    "callback": "Fine, you can call me back about this.",
}
_WALK_LINE = "I think I'm done here — I've got to go."


class ProspectSimulator:
    """Drives one synthetic prospect through a dialogue. `respond()` is async (it calls the sim LLM
    for talk + soft-driver deltas), but commit/walk is decided by the PURE buy_gate. State is held
    on `self.state`; budget is mirrored immutably from the persona. A seed (or injected rng) keeps
    any tie-breaking deterministic so identical inputs reproduce identical outcomes.
    """

    def __init__(
        self,
        persona: Persona,
        *,
        seed: int = 0,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.persona = persona
        self.rng = rng if rng is not None else random.Random(seed)
        self.state = ProspectState.initial(persona)

    # --- soft-driver delta application (the structural-fact guard lives here) ------------------

    def _apply_soft_deltas(self, deltas: object) -> None:
        """Apply ONLY clamped soft-driver deltas. THE HONESTY GUARD: budget and the qualification
        label are structural facts — any attempt to delta `budget`, `qualified`, or `disqualifier`
        (or any non-soft key) is IGNORED. Malformed (non-dict) input leaves state unchanged; a
        non-numeric delta for a real driver is skipped (no corruption), mirroring the DST.
        """
        if not isinstance(deltas, dict):
            return
        for key, raw in deltas.items():
            # Structural facts / unknown keys can never be moved by talk — silently ignore them.
            if key not in SOFT_DRIVERS:
                continue
            try:
                d = _clamp_delta(float(raw))
            except (TypeError, ValueError):
                continue  # skip a non-numeric delta rather than corrupt the level
            current = float(getattr(self.state, key))
            setattr(self.state, key, _clamp01(current + d))

    def _deplete_patience(self, agent_act: Optional[str]) -> None:
        """Base patience decay every turn; an EXTRA penalty when a pressure act (attempt_close/pitch)
        lands while trust is still low — pushiness costs patience, modeling a prospect who tires of
        being sold to before rapport exists."""
        decay = _PATIENCE_BASE_DECAY
        if agent_act in _PRESSURE_ACTS and self.state.trust < _LOW_TRUST_THRESHOLD:
            decay += _PATIENCE_PRESSURE_PENALTY
        self.state.patience = _clamp01(self.state.patience - decay)

    async def respond(
        self,
        agent_utterance: str,
        agent_act: Optional[str],
        llm_client: LLMClient,
        *,
        model: Optional[str] = None,
    ) -> ProspectTurn:
        """One prospect turn. Order: advance turn + deplete patience -> (walk if exhausted) ->
        sim-LLM talk + clamped SOFT deltas -> deterministic buy_gate -> outcome. Budget/qualified
        are never mutated. Returns a ProspectTurn; also records the terminal state on self.state."""
        self.state.turns_elapsed += 1
        self._deplete_patience(agent_act)

        # Patience exhausted and not yet committed -> walk. (A committed prospect doesn't walk.)
        if self.state.patience <= 0.0 and self.state.committed_tier is None:
            self.state.walked = True
            return ProspectTurn(
                text=_WALK_LINE,
                outcome="walked",
                committed_tier=None,
                walked=True,
                disqualifier_signaled=False,
                state_snapshot=self.state.snapshot(),
            )

        # Ask the sim LLM for in-character talk + proposed soft-driver deltas (robust to bad JSON).
        utterance = ""
        disqualifier_signaled = False
        messages = _build_sim_messages(self.persona, self.state, agent_utterance, agent_act)
        try:
            reply = await llm_client.complete_json(messages, model=model or SIM_MODEL)
        except ValueError:
            reply = None  # malformed/no JSON -> no talk text, no deltas (state unchanged)

        if isinstance(reply, dict):
            utterance = str(reply.get("utterance", ""))
            self._apply_soft_deltas(reply.get("deltas"))
            disqualifier_signaled = bool(reply.get("disqualifier_signaled", False))

        # Belt-and-suspenders: for an unqualified persona, also treat an explicit mention of the
        # disqualifier in the spoken text as a signal, so detection doesn't hinge on an optional key.
        if not self.persona.qualified and self.persona.disqualifier and not disqualifier_signaled:
            disqualifier_signaled = _mentions_disqualifier(utterance, self.persona.disqualifier)

        # THE GATE: a PURE function of the true drivers + ceiling. Talk never decides this.
        tier = buy_gate(self.state, self.persona)
        if tier is not None:
            self.state.committed_tier = tier
            return ProspectTurn(
                text=_ACCEPTANCE.get(tier, "Okay, let's do that."),
                outcome=f"committed:{tier}",
                committed_tier=tier,
                walked=False,
                disqualifier_signaled=disqualifier_signaled,
                state_snapshot=self.state.snapshot(),
            )

        return ProspectTurn(
            text=utterance,
            outcome="continue",
            committed_tier=None,
            walked=False,
            disqualifier_signaled=disqualifier_signaled,
            state_snapshot=self.state.snapshot(),
        )


# Heuristic disqualifier-mention detector (sim text is in-character; this catches the natural phrasings
# the prompt asks for). Only consulted for unqualified personas as a fallback signal.
_DISQ_PHRASES = {
    "no_budget": ("budget", "afford", "can't pay", "cannot pay", "too expensive", "no money", "money"),
    "no_authority": ("decision-maker", "decision maker", "check with", "not my call", "my spouse",
                     "my partner", "talk to", "ask my", "not up to me"),
    "no_real_need": ("don't really need", "do not really need", "just curious", "just looking",
                     "no real need", "not sure i need", "browsing"),
}


def _mentions_disqualifier(text: str, disqualifier: str) -> bool:
    low = (text or "").lower()
    return any(phrase in low for phrase in _DISQ_PHRASES.get(disqualifier, ()))

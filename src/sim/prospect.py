# The honest hidden-utility ProspectSimulator (U7) — the synthetic counterparty the agent practices
# against in self-play. Its INTEGRITY is the point: the sim LLM (a different model family than the
# agent; mocked in tests) drives only the prospect's TALK and proposes BOUNDED deltas to the SOFT
# drivers (trust/need/urgency/purchase_intent). A commitment is NEVER spontaneous: it happens ONLY
# when the agent attempts a close, and only at the tier the agent OFFERS — decided by buy_gate(), a
# PURE, DETERMINISTIC numeric gate on the TRUE hidden drivers + the offered tier + the persona's hard
# ceiling (R31). budget and qualified/disqualifier are STRUCTURAL FACTS the LLM may NEVER mutate by
# talking — so a no_budget prospect can never cross the budget gate no matter how fluent the agent is.
# CB-43: when a DEEPLY-PROMPTED named persona (Dana/Marcus/Pat from personas.py) is driven, its rich
# `behavior_prompt` is prepended to the in-character prompt so THIS specific personality speaks, and
# its `non_sequiturs` are injected OCCASIONALLY — the simulator rolls its OWN SEEDED rng (~1-in-
# _NON_SEQUITUR_EVERY_N turns, never the global random) and, on a hit, tells the LLM to weave in one
# chosen curveball line. These curveballs stress wrong-phase / repeat-question / prescriptiveness
# handling without becoming constant noise; they touch only the TALK prompt, never the pure buy_gate.
# CB-03 depth tuning makes the call REALISTICALLY LONG (15-40 turns) WITHOUT faking: the prompt/
# rubric makes the prospect surface a FEW DISTINCT objections before it warms, warming deltas are
# throttled (asymmetric clamp: gradual to earn, sharp to lose — _MAX_WARMING_DELTA vs _MAX_DELTA),
# and patience decays slowly while the pushiness penalty stays sharp. CB-03 ITERATION 2 makes the
# depth REALISTIC, not loopy: the rubric forbids restating an objection already raised — the prospect
# rotates through DISTINCT concerns (price -> efficacy -> diy_free -> timing -> decision_maker),
# caps total objections (~2-3) then progresses to a decision, and a genuinely unqualified/no-budget
# prospect FIRMLY surfaces its structural disqualifier (a release signal) after 1-2 honest exchanges
# instead of looping. To make variety robust against a verbatim-repeating LLM, ProspectState tracks
# the OBJECTION THEMES already raised (a small set; classified from the utterance) + how many honest
# disqualifier exchanges have happened, and feeds both into the rubric. None of this touches the
# gate: the buy_gate is still pure/deterministic and budget/qualified stay immutable to talk. Robust
# like the DST: malformed/missing JSON -> no delta; unknown/structural keys ignored. Headless + pure:
# NO LiveKit, NO DB, NO src.memory, NO src.core.respond/policy imports (the prospect is the
# independent counterparty; episodes are logged later by U8). Collaborators: src.sim.personas,
# the src.core.llm LLMClient seam.
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Optional

from src.core.llm import LLMClient, Message
# LADDER is re-exported (not used in this module's body): callers/tests do
# `from src.sim.prospect import LADDER` to reach the commitment-tier vocabulary alongside the engine.
from src.sim.personas import LADDER, Persona  # noqa: F401  (intentional public re-export)

# The SOFT drivers the sim LLM is permitted to nudge (clamped). budget is intentionally NOT here:
# it lives only on the immutable persona.utility and can never be moved by talk.
SOFT_DRIVERS: tuple[str, ...] = ("trust", "need", "urgency", "purchase_intent")

# Default sim model id (overridable per call). A DIFFERENT family than the agent (KTD5); mocked in
# tests so it is never actually contacted there.
SIM_MODEL = "openai/gpt-4o-mini"

# Per-turn delta clamp for the soft drivers (mirrors the DST's bounded-update discipline) and the
# level clamp to [0, 1].
#
# CB-03 depth tuning: real tutoring sales calls run 15-40 turns with surfaced objections, so the
# prospect must WARM GRADUALLY and stay engaged LONGER — it earns its way up over many handled-
# objection + value turns instead of spiking to a commit in 2-3. Two asymmetric clamps do this
# honestly (no faking — the buy_gate stays pure):
#   * POSITIVE soft-driver deltas (warming) are capped tightly (_MAX_WARMING_DELTA) so trust/need/
#     urgency/purchase_intent climb slowly; reaching a tier's commit thresholds takes more turns.
#   * NEGATIVE soft-driver deltas (cooling after a bad/pushy turn) keep the wider _MAX_DELTA range,
#     so a misstep can still drop a driver sharply — warming is slow, but losing ground is not.
# Patience decay is likewise slowed (the prospect doesn't walk in 2-3 turns) WHILE the pushiness
# penalty is kept intact, so a PUSHY agent still bleeds patience faster than a patient one.
_MAX_DELTA = 0.2                     # widest single-turn move (still the hard clamp for COOLING)
_MAX_WARMING_DELTA = 0.14            # cap on POSITIVE soft-driver moves: gradual early, decisive once
                                     # earned (still < _MAX_DELTA cooling) so a qualified buyer whose
                                     # concerns are handled can reach a commit within its patience
_PATIENCE_BASE_DECAY = 0.03          # patience lost every turn regardless of agent behavior. At ~0.5
                                     # starting patience this gives a ~16-turn budget — long enough
                                     # for a good agent to warm + close a QUALIFIED prospect (they
                                     # were walking at ~turn 9 under 0.05). Pushy turns still bleed
                                     # patience fast via _PATIENCE_PRESSURE_PENALTY, so bad agents
                                     # still get walked early.
_PATIENCE_PRESSURE_PENALTY = 0.18    # extra loss when a pressure act lands on low trust (kept sharp)

# GenerativeTwin anchoring fraction (src/sim/twin.py): after each LLM turn the twin's soft drivers
# are pulled this fraction of the way toward the real-call arc target at that turn. 0.5 = halfway.
# Exported here (next to the other sim constants) so twin.py imports it cleanly and tests can
# assert on it via `from src.sim.prospect import _TWIN_ANCHOR`.
_TWIN_ANCHOR: float = 0.5

# Conviction coupling (CB-03): purchase_intent realistically TRACKS conviction = mean(trust, need).
# The sim-LLM left intent crawling ~3x behind trust/need (trust would hit 1.0 while intent was still
# 0.3), so warm, qualified prospects WALKED before intent ever reached the commit bar — the binding
# constraint behind near-zero conversion. After the LLM deltas, intent is pulled UP toward a
# conviction-derived target (never down — cooling stays with the deltas) at the warming rate. BUDGET-
# GATED: a prospect who can't afford it does NOT develop paid-buying intent (honesty), so this never
# helps an unqualified-by-budget prospect cross a tier the budget gate would block.
_CONVICTION_FLOOR = 0.45             # mean(trust,need) below this exerts no pull (an unconvinced lead)
_CONVICTION_SPAN = 0.5               # conviction 0.45 -> target 0.0 ; 0.95 -> target 1.0
_CONVICTION_BUDGET_FLOOR = 0.2       # only a prospect with real budget develops paid-buying intent
# Conviction gets a prospect to "willing to consult" (intent ~consultation bar); intent BEYOND this —
# trial (0.55) / enrollment (0.7) — must be EARNED via the LLM's own deltas over a deeper call, so the
# coupling never makes the top tiers trivially fast (preserves CB-03 depth for high-commitment closes).
_CONVICTION_INTENT_CAP = 0.4
_LOW_TRUST_THRESHOLD = 0.5           # below this, pressure acts are felt as pushiness
_PRESSURE_ACTS = frozenset({"attempt_close", "pitch"})

# CB-03 iteration 2 — REALISTIC objection dynamics (no degenerate verbatim loop).
# The distinct objection themes a prospect may rotate through (in a natural escalation order). The
# prospect raises each AT MOST ONCE: once a theme is in ProspectState.objections_raised it must move
# to the NEXT distinct concern or warm — never restate the same one. price/efficacy/diy_free/timing
# mirror the persona.resistance keys; decision_maker is the "check with my spouse/partner" concern.
_OBJECTION_THEMES: tuple[str, ...] = ("price", "efficacy", "diy_free", "timing", "decision_maker")

# After this many DISTINCT concerns have been raised + engaged, the prospect should STOP objecting
# and progress toward a decision (warm to a close if qualified, or release if not) — not stonewall.
_MAX_DISTINCT_OBJECTIONS = 3
# After this many honest budget/disqualifier exchanges, an UNQUALIFIED prospect should firmly signal
# it can't proceed (the release signal) so the agent can let it go in ~6-10 turns, not loop 30+.
_DISQUALIFIER_RELEASE_AFTER = 2

# CB-43 — seeded, OCCASIONAL non-sequitur injection for the deeply-prompted named personas. On each
# turn the simulator rolls its OWN seeded rng; with probability ~1/_NON_SEQUITUR_EVERY_N it picks one
# of the persona's `non_sequiturs` and tells the sim LLM to weave it in this turn. This is a CURVEBALL,
# not constant noise — most turns get none. The prompt seam labels an injected turn with the marker
# _CURVEBALL_MARKER so the message construction (and tests) can tell a curveball turn apart. Touches
# only the TALK prompt; the pure buy_gate never sees it.
_NON_SEQUITUR_EVERY_N = 4            # ~1-in-4 turns gets a curveball (occasional, seed-driven)
_CURVEBALL_MARKER = "CURVEBALL"     # appears in the prompt ONLY on an injected turn (test seam)

# Heuristic theme classifier (the sim text is in-character; this catches the natural phrasings the
# rubric asks for, so state-tracking of "which themes were raised" works even with a mocked LLM).
# Mirrors _DISQ_PHRASES' shape. Used ONLY to track variety + nudge the rubric — never to gate.
_OBJECTION_PHRASES: dict[str, tuple[str, ...]] = {
    "price": ("price", "pricey", "expensive", "cost", "afford", "budget", "justify that", "too much"),
    "efficacy": ("actually work", "move the needle", "really help", "does it work", "prove",
                 "results", "guarantee", "skeptical it"),
    "diy_free": ("free", "khan", "youtube", "on our own", "diy", "do it ourselves", "online stuff"),
    "timing": ("timing", "too busy", "right now", "bad time", "later", "swamped", "next semester"),
    "decision_maker": ("spouse", "partner", "check with", "talk to my", "ask my", "not my call",
                       "decision-maker", "decision maker", "run it by"),
}


def _classify_objection_theme(text: str) -> Optional[str]:
    """Best-effort map an utterance to the objection theme it voices, else None. First match wins in
    a fixed order so classification is deterministic. Used only to track raised themes for variety."""
    low = (text or "").lower()
    for theme in _OBJECTION_THEMES:
        if any(phrase in low for phrase in _OBJECTION_PHRASES[theme]):
            return theme
    return None


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _clamp_delta(d: float) -> float:
    """Asymmetric per-turn clamp (CB-03): WARMING (positive) is throttled to the tight
    _MAX_WARMING_DELTA so soft drivers climb gradually over many turns; COOLING (negative) keeps
    the wider _MAX_DELTA range so a bad/pushy turn can still drop a driver sharply. Slow to earn,
    fast to lose — this is what makes the prospect take 15-40 turns to reach a commit honestly,
    without touching the pure buy_gate."""
    if d < -_MAX_DELTA:
        return -_MAX_DELTA
    if d > _MAX_WARMING_DELTA:
        return _MAX_WARMING_DELTA
    return d


@dataclass
class ProspectState:
    """The MUTABLE per-turn state of a prospect. Soft drivers (trust/need/urgency/purchase_intent)
    + patience evolve via clamped LLM deltas / deterministic decay; `budget` is read once from the
    immutable persona.utility and NEVER mutated (it is a structural fact, not a soft driver). The
    buy-gate reads these true drivers to decide commit/walk — talk only moves the SOFT ones.

    CB-03 iteration 2 adds NON-gating bookkeeping for REALISTIC objection dynamics: the set of
    objection THEMES already raised (so the prospect never restates one), and a count of honest
    disqualifier exchanges (so an unqualified prospect resolves with a firm release instead of
    looping). These feed the rubric only — the pure buy_gate never reads them.
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
    # CB-03 iter2: which distinct objection themes have already been voiced (variety guard). NOT read
    # by the gate. A set so re-raising the same theme is a no-op; len() caps total distinct objections.
    objections_raised: set[str] = field(default_factory=set)
    # CB-03 iter2: how many times an UNQUALIFIED prospect has honestly aired its disqualifier; once it
    # passes _DISQUALIFIER_RELEASE_AFTER the rubric pushes a firm, final "I can't proceed" release.
    disqualifier_exchanges: int = 0

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
        """A plain-dict view for the ProspectTurn (logging-friendly; never carries hidden labels).
        `objections_raised` is serialized to a SORTED list so the snapshot is JSON-friendly and
        deterministic for logging/equality."""
        snap = asdict(self)
        snap["objections_raised"] = sorted(self.objections_raised)
        return snap


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
# cannot change these — only the true drivers + the OFFERED tier + the persona's ceiling decide. A
# prospect commits ONLY to a tier the agent OFFERS (closes at): the gate checks that one tier's
# thresholds (and the ceiling), never auto-selecting a rung the agent didn't ask for.
_TIER_THRESHOLDS: dict[str, dict[str, float]] = {
    # tier:          need   trust  intent budget  (+urgency/patience where it matters)
    "enrollment": {"need": 0.7, "trust": 0.7, "purchase_intent": 0.7, "budget": 0.6},
    "trial":      {"need": 0.55, "trust": 0.55, "purchase_intent": 0.55, "budget": 0.4},
    "consultation": {"need": 0.4, "trust": 0.4, "purchase_intent": 0.4, "budget": 0.2},
    # callback is the lowest rung: a soft yes to keep talking later — mostly intent + a little trust,
    # NO budget gate (you can agree to a callback with no money).
    # callback is the FREE, lowest rung — a future follow-up needs little conviction, so its bar is
    # low: an unqualified/budget-constrained prospect the agent offers a callback to (offer_low_
    # commitment_on_budget) can actually ACCEPT it (get help + a future meeting) instead of walking.
    "callback":   {"purchase_intent": 0.15, "trust": 0.2},
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


def buy_gate(
    state: ProspectState,
    persona: Persona,
    *,
    offered_tier: Optional[str] = None,
) -> Optional[str]:
    """PURE, DETERMINISTIC commit decision. A prospect commits ONLY to a tier the agent OFFERS (the
    rung it attempts to close at) — commitment is NEVER spontaneous. Returns `offered_tier` iff:
    (a) the agent is actually offering a known tier, (b) that tier is within the persona's hard
    ceiling, and (c) the TRUE hidden drivers + immutable budget clear that tier's thresholds; else
    None (no offer, an over-ceiling offer, or a declined offer the drivers don't support).

    Depends ONLY on the true drivers + immutable budget + persona ceiling + the OFFERED tier — NEVER
    on how fluent the agent was (R31). offered_tier=None (the agent did not attempt to close this
    turn) always yields None: discovery, pitching, and talk never trigger a commitment. Over-offering
    (closing at a rung the drivers don't support, e.g. enrollment when only consultation is earned)
    is declined — so the agent's close-TIMING and TIER choice genuinely matter.
    """
    if offered_tier is None or offered_tier not in _TIER_THRESHOLDS:
        return None  # no close attempt (or an unknown tier) -> no commit
    ceiling = _ceiling_rank(persona.max_commitment)
    if ceiling < 0 or _TIER_RANK[offered_tier] > ceiling:
        return None  # a "none" persona, or an offer above the persona's hard cap -> can't commit
    if _meets(state, _TIER_THRESHOLDS[offered_tier]):
        return offered_tier
    return None


# --- The sim-LLM prompt (talk + SOFT-driver delta proposals only) -----------------------------

_SIM_SYSTEM = (
    "You ARE a prospect a tutoring salesperson just called — not an assistant. Stay fully in "
    "character. You have a hidden disposition; respond the way THIS person would. Output ONLY a "
    "JSON object: {\"utterance\": <what you say, 1-2 sentences in your style>, \"deltas\": {<how "
    "this exchange moved your feelings, signed numbers in [-0.2, 0.2]>}}. You may ONLY propose "
    "deltas for these soft feelings: trust, need, urgency, purchase_intent. You CANNOT change your "
    "budget, and you CANNOT decide to buy here — your real decision is made elsewhere. Never invent "
    "money or authority you don't have.\n"
    # CB-03 realism rubric: real tutoring buyers do NOT warm up immediately — they push back first
    # and warm only as concerns are actually answered. This forces a deeper, 15-40 turn call.
    "BE A REALISTIC, SKEPTICAL BUYER — do NOT cooperate or get excited right away:\n"
    "- EARLY in the call (while trust is still low, roughly <0.5), SURFACE your real concerns "
    "before warming. Raise a FEW genuine objections OVER SEVERAL TURNS — pick the ones that fit "
    "your OBJECTION_PROPENSITIES, e.g.: price / can-we-afford-it, doubt it actually works "
    "(efficacy), 'there are free options like Khan Academy / YouTube', bad timing / too busy, or "
    "'I'd need to check with my spouse/partner first'. Voice ONE objection per turn, naturally and "
    "in your style — don't dump them all at once.\n"
    # CB-03 ITERATION 2 — the anti-loop rule. A real buyer says a concern ONCE; repeating the exact
    # same objection verbatim while the agent loops an acknowledgment is unrealistic and degenerate.
    "- NEVER restate an objection you already raised. The RAISED_ALREADY line below tells you which "
    "concerns you have voiced — do NOT repeat any of them, even reworded. When the agent has "
    "addressed a concern, you MUST either (a) accept it, warm slightly, and MOVE ON, or (b) raise a "
    "DIFFERENT, not-yet-raised concern (see NOT_YET_RAISED). Progress through DISTINCT concerns; do "
    "not re-litigate the same one.\n"
    "- After about 2-3 DISTINCT concerns have been raised and genuinely engaged, STOP objecting and "
    "MOVE TOWARD A DECISION: if the agent has earned it, warm enough that a well-run close can land; "
    "do not stonewall forever once your real concerns have been heard.\n"
    "- WARM ONLY WHEN EARNED, in two phases. EARLY (still skeptical, objections unaddressed): keep "
    "positive deltas SMALL (around +0.02 to +0.06). LATER, ONCE the agent has genuinely answered "
    "your real concerns and the value clearly lands for YOU: warm MORE DECISIVELY (around +0.08 to "
    "+0.14 per good turn) so that, if the agent runs a solid close, you actually become ready to say "
    "yes — a qualified buyer whose concerns are handled SHOULD reach a commit, not stall forever. "
    "Give warming ONLY for real understanding/value; a polished pitch that ignores your objection "
    "earns little or nothing, and a pushy or premature close should LOWER trust/purchase_intent "
    "(negative deltas down to -0.2). purchase_intent starts cold — it earns its way up over the "
    "call, slowly at first then faster once you're genuinely won over; it should not jump on turn 1.\n"
    # CB-03 ITERATION 2 — unqualified resolution. A prospect that structurally CANNOT proceed must not
    # warm toward a fake yes nor loop the same disqualifier; after a couple of honest exchanges it
    # firmly and finally says it can't go ahead, so the agent can gracefully release the call.
    "- If you have a STRUCTURAL disqualifier (you genuinely can't afford it, aren't the decision-"
    "maker, or have no real need): air it honestly ONCE or twice, and if the agent keeps pushing, "
    "state FIRMLY and FINALLY that you cannot proceed (set \"disqualifier_signaled\": true). Do NOT "
    "keep repeating the same line turn after turn, and do NOT warm purchase_intent toward a yes you "
    "cannot honestly give — make it clear the call should end.\n"
    "- Stay engaged and keep talking through your DISTINCT concerns rather than agreeing fast; a "
    "real call has back-and-forth before any soft yes — but it does not loop the same objection."
)


def _build_sim_messages(
    persona: Persona,
    state: ProspectState,
    agent_utterance: str,
    agent_act: Optional[str],
    non_sequitur: Optional[str] = None,
) -> list[Message]:
    """Assemble the in-character sim prompt. Unqualified personas get an explicit instruction to
    NATURALLY surface their structural disqualifier (e.g. 'I don't really have a budget for this'),
    so the agent has an honest chance to detect it — but voicing it never changes the structural
    fact itself. CB-03 iter2: also surfaces the ALREADY_RAISED objection themes (so the LLM rotates
    to a NEW concern instead of repeating one) and, once enough honest disqualifier exchanges have
    happened, an explicit RELEASE nudge so an unqualified prospect resolves rather than loops.

    CB-43: for a deeply-prompted NAMED persona, `persona.behavior_prompt` (the rich backstory +
    objection script/timing + walk/convert triggers) is prepended to the profile so THIS personality
    speaks. `non_sequitur` (chosen by the SEEDED caller on ~1-in-N turns; None otherwise) is woven in
    as a one-off CURVEBALL line — a realistic tangent/off-topic/wrong-answer the agent must handle —
    marked with _CURVEBALL_MARKER. Neither field touches the pure buy_gate."""
    disq_hint = ""
    if not persona.qualified and persona.disqualifier:
        human = {
            "no_budget": "you genuinely cannot afford this; mention you don't have a budget for it",
            "no_authority": "you are not the decision-maker; you'd have to check with someone else",
            "no_real_need": "you don't actually have a pressing need; you're mostly just curious",
        }.get(persona.disqualifier, "you are not a fit and should say so naturally")
        disq_hint = f"\nIMPORTANT: {human}. Surface this naturally when it fits."
        # Once the disqualifier has been honestly aired a couple of times, push a FIRM, FINAL release
        # so the call resolves in ~6-10 turns (no 30+ turn repeat loop).
        if state.disqualifier_exchanges >= _DISQUALIFIER_RELEASE_AFTER:
            disq_hint += (
                "\nRESOLUTION: you have already aired this honestly. Now state FIRMLY and FINALLY "
                "that you cannot proceed and the call should end — set \"disqualifier_signaled\": "
                "true. Do NOT repeat your earlier line again; do NOT warm toward a yes."
            )

    # Tell the LLM which objection themes it has ALREADY voiced (variety guard) and which distinct
    # ones remain, plus whether it should stop objecting and progress toward a decision.
    raised = sorted(state.objections_raised)
    remaining = [t for t in _OBJECTION_THEMES if t not in state.objections_raised]
    if raised:
        objection_hint = (
            f"\nRAISED_ALREADY (do NOT repeat any of these — pick a NEW concern or warm/move on): "
            f"{raised}"
        )
        if remaining and len(state.objections_raised) < _MAX_DISTINCT_OBJECTIONS:
            objection_hint += f"\nNOT_YET_RAISED (you may raise ONE of these instead): {remaining}"
        else:
            objection_hint += (
                "\nYou have raised enough distinct concerns — STOP objecting and move toward a "
                "decision (warm if the agent earned it)."
            )
    else:
        objection_hint = ""

    # CB-43: a deeply-prompted named persona leads with its rich behavioral script (who you are +
    # objection script/timing + walk/convert triggers), so the generic archetype line below just
    # backs it up. Template-jitter personas have an empty behavior_prompt -> unchanged behavior.
    backstory = f"YOU ARE: {persona.behavior_prompt}\n\n" if persona.behavior_prompt else ""

    # CB-43: a one-off CURVEBALL for THIS turn (the caller injects it on ~1-in-N seeded turns). It is
    # a realistic tangent/off-topic/wrong-answer to weave in naturally — a stress test for the agent's
    # wrong-phase / repeat-question handling, NOT a change to your underlying feelings or the gate.
    curveball_hint = ""
    if non_sequitur:
        curveball_hint = (
            f"\n{_CURVEBALL_MARKER}: This turn, naturally weave in this off-script aside in your own "
            f"voice (a realistic tangent — do NOT drop your concerns or warm because of it): "
            f"{non_sequitur!r}"
        )

    profile = (
        f"{backstory}"
        f"ARCHETYPE: {persona.archetype}\n"
        f"STYLE: {persona.style} (talk this way)\n"
        f"OBJECTION_PROPENSITIES: {persona.resistance}\n"
        f"WHAT_MOVES_YOU: {persona.decision_factors}\n"
        f"CURRENT_FEELINGS: trust={round(state.trust,2)} need={round(state.need,2)} "
        f"urgency={round(state.urgency,2)} purchase_intent={round(state.purchase_intent,2)} "
        f"patience={round(state.patience,2)}"
        f"{objection_hint}"
        f"{disq_hint}"
        f"{curveball_hint}"
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

        # Conviction coupling: pull purchase_intent UP toward mean(trust, need) once the prospect is
        # convinced — but only for a prospect with real budget (a no-budget lead never develops
        # paid-buying intent). Pull-up only; the LLM deltas above own any cooling. This is what lets a
        # warm, qualified prospect's intent reach the commit bar within its patience budget.
        if float(self.state.budget) >= _CONVICTION_BUDGET_FLOOR:
            conviction = (float(self.state.trust) + float(self.state.need)) / 2.0
            target = min(_CONVICTION_INTENT_CAP, _clamp01((conviction - _CONVICTION_FLOOR) / _CONVICTION_SPAN))
            if target > self.state.purchase_intent:
                step = min(_MAX_WARMING_DELTA, target - self.state.purchase_intent)
                self.state.purchase_intent = _clamp01(self.state.purchase_intent + step)

    def _deplete_patience(self, agent_act: Optional[str]) -> None:
        """Base patience decay every turn; an EXTRA penalty when a pressure act (attempt_close/pitch)
        lands while trust is still low — pushiness costs patience, modeling a prospect who tires of
        being sold to before rapport exists."""
        decay = _PATIENCE_BASE_DECAY
        if agent_act in _PRESSURE_ACTS and self.state.trust < _LOW_TRUST_THRESHOLD:
            decay += _PATIENCE_PRESSURE_PENALTY
        self.state.patience = _clamp01(self.state.patience - decay)

    def _maybe_non_sequitur(self) -> Optional[str]:
        """CB-43: with probability ~1/_NON_SEQUITUR_EVERY_N, pick one of the persona's curveball lines
        for THIS turn (else None). Uses the simulator's OWN seeded rng (never the global random), so a
        fixed seed reproduces the exact schedule of curveball turns AND which line each one uses. A
        persona with no non_sequiturs (the template-jitter archetypes) always returns None — unchanged
        behavior. Occasional by design: most turns get None, so curveballs are stress, not noise."""
        if not self.persona.non_sequiturs:
            return None
        if self.rng.random() >= (1.0 / _NON_SEQUITUR_EVERY_N):
            return None
        return self.rng.choice(self.persona.non_sequiturs)

    def _sim_messages(self, agent_utterance: str, agent_act: Optional[str]) -> list[Message]:
        """The sim-LLM chat messages for this turn. A thin instance seam over the module-level
        builder so a subclass (GenerativeTwin) can override the prompt cleanly — no monkey-patching.
        CB-43: rolls the seeded rng for an occasional non-sequitur and threads it into the builder."""
        return _build_sim_messages(
            self.persona,
            self.state,
            agent_utterance,
            agent_act,
            non_sequitur=self._maybe_non_sequitur(),
        )

    async def respond(
        self,
        agent_utterance: str,
        agent_act: Optional[str],
        llm_client: LLMClient,
        *,
        agent_tier: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ProspectTurn:
        """One prospect turn. Order: advance turn + deplete patience -> (walk if exhausted) ->
        sim-LLM talk + clamped SOFT deltas -> deterministic buy_gate -> outcome. Budget/qualified
        are never mutated. Returns a ProspectTurn; also records the terminal state on self.state.

        `agent_act`/`agent_tier` describe the agent's move this turn: a commitment fires ONLY when
        `agent_act == "attempt_close"`, against the offered `agent_tier` (defaulting to the lowest
        substantive rung, "consultation", if the close carried no explicit tier — mirroring the
        policy's own default). Any non-close act offers nothing, so the buy_gate returns None."""
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
        # Called via self (not the bare module fn) so a subclass (GenerativeTwin) can override the
        # prompt by overriding _sim_messages — NO module-global monkey-patching, so concurrent
        # prospects/twins never race on a shared builder reference.
        messages = self._sim_messages(agent_utterance, agent_act)
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

        # CB-03 iter2 — NON-gating bookkeeping that keeps the next turn's rubric anti-loop. Record any
        # objection theme this utterance voiced (a set, so repeats are no-ops and len() caps variety),
        # and count honest disqualifier exchanges so an unqualified prospect gets a firm release nudge
        # next turn instead of looping. Neither field is ever read by the pure buy_gate.
        theme = _classify_objection_theme(utterance)
        if theme is not None:
            self.state.objections_raised.add(theme)
        if disqualifier_signaled:
            self.state.disqualifier_exchanges += 1

        # THE GATE: a prospect commits ONLY in response to a close attempt, to the OFFERED tier, and
        # only if its TRUE drivers clear it. Discovery/pitch/talk offer nothing -> buy_gate -> None.
        offered = (agent_tier or "consultation") if agent_act == "attempt_close" else None
        tier = buy_gate(self.state, self.persona, offered_tier=offered)
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

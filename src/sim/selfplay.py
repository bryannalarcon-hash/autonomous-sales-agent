# Headless text self-play harness (plan U8, R37/R26): drives the agent BRAIN (src.core.respond,
# the single transport-agnostic entry point) against the honest ProspectSimulator at high N with NO
# LiveKit, logging each run to the unified Episode schema (src.memory.store). This is what makes the
# agent trainable — U10's improvement loop runs batches through run_batch(). Two SEPARATE LLM
# clients per episode (agent vs prospect are different model families, KTD5). Realism corruption
# (inject_noise) is applied to the prospect text BEFORE the agent sees it — deterministic, seeded —
# so the agent's robust DST is exercised on garbled (voice-shaped) input. Collaborators:
# src.core.respond, src.core.belief_state, src.sim.prospect/personas, src.config.settings,
# src.core.llm, src.memory.{schema,store}. This file MAY import the store (it is the harness, not
# core/) but MUST NOT import LiveKit (R37: text substrate) and MUST NOT touch src/core/.
from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any, Callable, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient
from src.core.respond import respond
from src.memory import store
from src.memory.schema import BeliefSnapshot, Episode, Turn
from src.sim.personas import Persona
from src.sim.prospect import ProspectSimulator, ProspectTurn

# --- The outcome -> ladder_tier mapping (documented in the header) ----------------------------
# Higher int = stronger commitment, matching the schema's "0 none .. N enrollment" intent and the
# simulator's tier strength order (callback < consultation < trial < enrollment). Non-commit
# terminals (walked/escalated/released) are tier 0.
#   committed:enrollment   -> "enrolled"             tier 4
#   committed:trial        -> "trial_booked"         tier 3
#   committed:consultation -> "consult_booked"       tier 2
#   committed:callback     -> "callback_scheduled"   tier 1
#   walked                 -> "walked"               tier 0
#   escalated              -> "escalated"            tier 0
#   max_turns reached      -> "released"             tier 0
_TIER_TO_OUTCOME: dict[str, tuple[str, int]] = {
    "enrollment": ("enrolled", 4),
    "trial": ("trial_booked", 3),
    "consultation": ("consult_booked", 2),
    "callback": ("callback_scheduled", 1),
}
_WALKED = ("walked", 0)
_ESCALATED = ("escalated", 0)
_RELEASED = ("released", 0)

# The agent's canned opener (greeting) — turn 0 is NOT routed through respond() (there is no user
# utterance yet). Persona-flavored from the config when available.
_DEFAULT_OPENER = "Hi, this is {name} from Nerdy. How can I help you today?"

# inject_noise tuning: bounded so the text is degraded (ASR-shaped) but not destroyed. All
# randomness flows through the passed seeded random.Random so a seed reproduces the corruption.
_SWAP_NEIGHBORS = "abcdefghijklmnopqrstuvwxyz"


def _opener(config: AgentConfig) -> str:
    """The canned greeting for turn 0, persona-named from the config (no respond() call yet)."""
    name = getattr(getattr(config, "persona", None), "name", None) or "Alex"
    return _DEFAULT_OPENER.format(name=name)


# --- Realism injection (R37) — deterministic, seeded ------------------------------------------

def inject_noise(text: str, *, rng: random.Random, level: float) -> str:
    """Apply voice-shaped, bounded corruption to prospect `text` so the agent's robust DST is
    exercised on garbled input (ASR errors + terseness). Deterministic: all randomness comes from
    the passed seeded `rng`, so the same seed reproduces the exact corruption. level<=0 is identity.

    Two bounded effects, each gated by `level` per token/char:
      - char swaps/drops (ASR substitution/deletion) on letters, at a per-char probability ~ level.
      - occasional dropping of a SHORT word (terseness), at a per-word probability ~ level/3.
    The corruption is capped so the text stays mostly recoverable (we never empty it).
    """
    if not text or level <= 0.0:
        return text

    # Per-word terseness: drop the occasional short (<=3 char) word.
    words = text.split(" ")
    kept: list[str] = []
    drop_p = min(0.5, level / 3.0)
    for w in words:
        if len(w) <= 3 and rng.random() < drop_p and len(kept) < len(words):
            continue  # dropped (terseness)
        kept.append(w)
    if not kept:  # never destroy the whole utterance
        kept = words
    terse = " ".join(kept)

    # Per-char ASR corruption: swap a letter to a neighbor or drop it, bounded by level.
    char_p = min(0.4, level * 0.5)
    out_chars: list[str] = []
    for ch in terse:
        if ch.isalpha() and rng.random() < char_p:
            roll = rng.random()
            if roll < 0.5:
                out_chars.append(rng.choice(_SWAP_NEIGHBORS))  # substitution
            # else: deletion (append nothing)
        else:
            out_chars.append(ch)
    corrupted = "".join(out_chars).strip()
    return corrupted if corrupted else terse  # guard against an all-deleted result


# --- The dialogue loop (one episode) ----------------------------------------------------------

def _terminal_from_prospect(pt: ProspectTurn) -> Optional[tuple[str, int]]:
    """Map a terminal ProspectTurn (walked / committed:<tier>) to (outcome_label, ladder_tier).
    Returns None when the prospect turn is non-terminal ("continue")."""
    if pt.walked:
        return _WALKED
    if pt.committed_tier:
        return _TIER_TO_OUTCOME.get(pt.committed_tier, (f"committed:{pt.committed_tier}", 0))
    return None


def _belief_snapshot(belief: BeliefState) -> BeliefSnapshot:
    """Project a working BeliefState to the stored snapshot (the persisted, lossy projection)."""
    return belief.to_snapshot()


def _build_episode(
    *,
    persona: Persona,
    config: AgentConfig,
    cohort: str,
    turns: list[Turn],
    outcome: str,
    ladder_tier: int,
    committed_tier: Optional[str],
    walked: bool,
    escalated: bool,
    turn_count: int,
) -> Episode:
    """Assemble the channel='sim' Episode from a finished run. version/kb_version come from
    config.stamp(); qualified/disqualifier_reason/persona are GROUND TRUTH from the persona."""
    stamp = config.stamp()
    return Episode(
        episode_id=f"sp-{uuid.uuid4().hex}",
        turns=turns,
        outcome=outcome,
        ladder_tier=ladder_tier,
        qualified=persona.qualified,
        disqualifier_reason=persona.disqualifier,
        version=stamp.get("version", ""),
        kb_version=stamp.get("kb_version", ""),
        channel="sim",
        persona=persona.archetype,
        cohort=cohort,
        escalated=escalated,
        metrics={
            "turn_count": turn_count,
            "committed_tier": committed_tier,
            "prospect_walked": walked,
        },
    )


async def run_episode(
    persona: Persona,
    agent_llm: LLMClient,
    prospect_llm: LLMClient,
    config: AgentConfig,
    *,
    seed: int = 0,
    cohort: str = "training",
    max_turns: int = 24,
    noise: float = 0.0,
    retrieve_facts: Optional[Callable[[BeliefState, str], Sequence[Any]]] = None,
    persist: bool = True,
    last_user_act: Optional[str] = None,
    on_turn: Optional[Callable[[dict[str, Any]], None]] = None,
) -> Episode:
    """Run ONE self-play episode: the agent brain (respond()) alternates with the ProspectSimulator
    to a terminal state, logging every turn (decision + rationale + belief snapshot) to a unified
    channel='sim' Episode.

    The two LLM clients are SEPARATE on purpose (agent vs prospect are different model families).
    Turn 0 is the agent's CANNED greeting (no respond() call — there is no user utterance yet).
    Then each loop: prospect.respond() -> inject_noise() on the prospect text BEFORE the agent sees
    it -> terminal check (walk/commit) -> respond() -> terminal check (escalate) -> max_turns guard.

    `retrieve_facts(belief, corrupted_text) -> facts` is an optional KB-grounding hook threaded to
    respond() (U5). `last_user_act` (e.g. "human_request") is passed to respond() each agent turn so
    the escalation gate can see it. If `persist`, the Episode is saved via store.save_episode().
    """
    rng = random.Random(seed)
    prospect = ProspectSimulator(persona, seed=seed)
    belief = BeliefState.fresh()

    # `history` is the list of dicts the gates + respond() read. Key contract (must match exactly):
    #   agent turn:    {"role": "agent", "act": <decision.act>, "confidence": <float>, "text": <reply>}
    #   prospect turn: {"role": "user",  "text": <prospect text>}
    # respond._last_agent_act() scans for role in ("assistant","agent") + reads "act";
    # gates._consecutive_pressure_count() reads role + "act"; gates._low_confidence_streak() reads
    # role + "confidence". These keys are exactly those.
    history: list[dict[str, Any]] = []
    turns: list[Turn] = []
    turn_id = 0

    # --- Turn 0: canned greeting (not via respond()) ---
    greeting = _opener(config)
    turns.append(
        Turn(
            turn_id=turn_id,
            speaker="agent",
            text=greeting,
            decision="greeting",
            rationale="canned opener (no user utterance yet)",
            belief=_belief_snapshot(BeliefState.fresh()),
        )
    )
    history.append({"role": "agent", "act": "greeting", "confidence": None, "text": greeting})
    turn_id += 1

    last_agent_text = greeting
    last_agent_act: Optional[str] = "greeting"
    # The tier the agent last offered (only set when it attempts a close) — the prospect commits
    # ONLY in response to a close offer, at this tier (U7 close-triggered buy-gate).
    last_agent_tier: Optional[str] = None

    outcome: str = _RELEASED[0]
    ladder_tier: int = _RELEASED[1]
    committed_tier: Optional[str] = None
    walked = False
    escalated = False
    turn_count = 0

    while True:
        # 1. Prospect responds to the agent's last utterance + act + offered tier (commit/walk
        #    decided inside; a commitment fires ONLY when last_agent_act was a close, at that tier).
        pt: ProspectTurn = await prospect.respond(
            last_agent_text, last_agent_act, prospect_llm, agent_tier=last_agent_tier
        )
        clean_text = pt.text

        # 2. REALISM corruption applied to the prospect text BEFORE the agent ever sees it.
        corrupted_text = inject_noise(clean_text, rng=rng, level=noise)

        # Log the prospect turn with the (possibly corrupted) text the agent actually receives.
        turns.append(Turn(turn_id=turn_id, speaker="prospect", text=corrupted_text))
        history.append({"role": "user", "text": corrupted_text})
        turn_id += 1
        turn_count += 1

        # Optional calibration observation hook (default None -> no-op, no behavior change): exposes
        # the prospect's TRUE hidden drivers + the tier the agent just offered + the buy-gate verdict,
        # so calibration can see WHY a close was accepted/rejected (the episode logs only the agent's
        # belief, not the prospect's true state).
        if on_turn is not None:
            on_turn(
                {
                    "turn": turn_count,
                    "offered_tier": last_agent_tier,
                    "true": prospect.state.snapshot(),
                    "committed_tier": pt.committed_tier,
                    "walked": pt.walked,
                }
            )

        # 3. Terminal: the prospect walked or committed (the deterministic buy-gate decided).
        terminal = _terminal_from_prospect(pt)
        if terminal is not None:
            outcome, ladder_tier = terminal
            walked = pt.walked
            committed_tier = pt.committed_tier
            break

        # 4. Agent turn through the SINGLE brain entry point (DST -> gated policy -> NLG).
        facts = retrieve_facts(belief, corrupted_text) if retrieve_facts is not None else None
        decision, reply, belief = await respond(
            belief,
            history,
            corrupted_text,
            agent_llm,
            config,
            retrieved_facts=facts,
            last_user_act=last_user_act,
        )

        _timings = decision.meta.get("timings") or {}
        turns.append(
            Turn(
                turn_id=turn_id,
                speaker="agent",
                text=reply,
                decision=decision.act,
                rationale=decision.rationale,
                belief=_belief_snapshot(belief),
                # Real measured turn latency (respond() stage timing) — was previously unset.
                latency_ms=int(round(_timings["total_ms"])) if "total_ms" in _timings else None,
            )
        )
        history.append(
            {
                "role": "agent",
                "act": decision.act,
                "confidence": decision.confidence,
                "text": reply,
            }
        )
        turn_id += 1
        last_agent_text = reply
        last_agent_act = decision.act
        last_agent_tier = decision.tier  # the offered rung (set by the policy for attempt_close)

        # 5. Terminal: the agent escalated (deferred to a specialist).
        if decision.act == "escalate":
            outcome, ladder_tier = _ESCALATED
            escalated = True
            break

        # 6. Turn-count guard: reached the budget without a terminal -> released.
        if turn_count >= max_turns:
            outcome, ladder_tier = _RELEASED
            break

    episode = _build_episode(
        persona=persona,
        config=config,
        cohort=cohort,
        turns=turns,
        outcome=outcome,
        ladder_tier=ladder_tier,
        committed_tier=committed_tier,
        walked=walked,
        escalated=escalated,
        turn_count=turn_count,
    )
    if persist:
        await store.save_episode(episode)
    return episode


# --- Batch ------------------------------------------------------------------------------------

async def run_batch(
    personas: Sequence[Persona],
    agent_llm_factory: Callable[[], LLMClient],
    prospect_llm_factory: Callable[[], LLMClient],
    config: AgentConfig,
    *,
    base_seed: int = 0,
    cohort: str = "training",
    max_turns: int = 24,
    noise: float = 0.0,
    concurrency: int = 8,
    retrieve_facts: Optional[Callable[[BeliefState, str], Sequence[Any]]] = None,
    persist: bool = True,
    last_user_act: Optional[str] = None,
) -> list[Episode]:
    """Run N episodes CONCURRENTLY (asyncio.gather, bounded by a Semaphore=concurrency). Each
    episode gets its own seed (base_seed + i) and its OWN agent/prospect LLM clients (the factories
    are called per episode so MockLLMClient call-state never collides across concurrent runs).
    Returns the episodes in input order."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(i: int, persona: Persona) -> Episode:
        async with sem:
            return await run_episode(
                persona,
                agent_llm_factory(),
                prospect_llm_factory(),
                config,
                seed=base_seed + i,
                cohort=cohort,
                max_turns=max_turns,
                noise=noise,
                retrieve_facts=retrieve_facts,
                persist=persist,
                last_user_act=last_user_act,
            )

    tasks = [_one(i, p) for i, p in enumerate(personas)]
    return list(await asyncio.gather(*tasks))


def outcome_distribution(episodes: Sequence[Episode]) -> dict[str, int]:
    """Count episodes by outcome label (for batch verification / KPI rollups)."""
    dist: dict[str, int] = {}
    for ep in episodes:
        key = ep.outcome or "unknown"
        dist[key] = dist.get(key, 0) + 1
    return dist

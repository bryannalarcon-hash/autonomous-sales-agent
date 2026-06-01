# U14 (B) — the SIM-TO-REAL divergence harness (plan R36/R37). This is the central-risk measurement:
# an agent that wins in headless sim but FAILS with a real human. It runs the SAME matched scenario
# through BOTH arms with matched inputs and computes the divergence scalar U10's promotion gate pauses
# on (R36):
#   run_matched(scenario, ...) -> (1) the SIM arm: text self-play via selfplay.run_episode(persist=False),
#     which deterministically generates the prospect's turn script at CLEAN text; (2) the VOICE arm:
#     the SAME brain (a VoiceSession) driven on the SAME scripted prospect turns, so at voice_noise=0
#     the perceived inputs are identical and the decisions match (parity, R37). `voice_noise` injects
#     ASR-style corruption (reuse selfplay.inject_noise) into the voice arm's PERCEIVED user text to
#     model the real gap.
#   divergence(sim_ep, voice_ep) -> over matched turns: decision-disagreement rate + outcome/ladder-tier
#     gap + a belief-trajectory distance (mean abs driver delta), combined into divergence_score in [0,1]
#     (0 == identical behavior).
#   sim_to_real_report(scenarios, ...) -> aggregate (per_scenario, mean_divergence, max_divergence); the
#     mean (or max) is the scalar fed to promotion.evaluate_promotion(divergence=...) (R36).
# DB-FREE (persist=False) + LIVEKIT-FREE (drives the livekit-free VoiceSession). SEEDED random only;
# NO src/core mutation. Collaborators: src.sim.selfplay, src.voice.session, src.memory.schema,
# src.config.settings, src.core.llm.
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.belief_state import BeliefState
from src.core.llm import LLMClient
from src.memory.schema import BeliefSnapshot, Episode, Turn
from src.sim import selfplay
from src.sim.personas import Persona
from src.voice.session import VoiceSession

# A FakeEmbedder is only needed to construct a VoiceSession (the voice arm does no retrieval here —
# kb_chunks=None means the session's retrieve hook is absent, matching the sim arm's no-RAG default).
from src.kb.embeddings import FakeEmbedder

# A factory builds a FRESH LLMClient per arm/episode so MockLLMClient call-state never collides
# across the two arms (the sim arm and the voice arm must each get their own clients).
LLMFactory = Callable[[], LLMClient]


@dataclass
class MatchedScenario:
    """One scenario runnable in BOTH arms with matched inputs (plan R36). `prospect_script_or_seed`
    seeds the prospect simulator so the sim arm deterministically generates the SAME prospect turns
    both arms replay; `label` names it for the report."""

    persona: Persona
    prospect_script_or_seed: int
    label: str


@dataclass
class MatchedRun:
    """The paired result of running ONE scenario through both arms: the sim Episode (text self-play)
    and the voice Episode (the same brain on the same scripted prospect turns). divergence() consumes
    these two episodes."""

    scenario: MatchedScenario
    sim_episode: Episode
    voice_episode: Episode


@dataclass
class DivergenceReport:
    """The sim-vs-voice gap for one matched run (plan R36). `decision_disagreement` is the fraction of
    matched agent turns whose act/tier differ; `ladder_gap` is the normalized outcome/ladder-tier gap;
    `belief_distance` is the mean absolute driver delta over matched turns. `divergence_score` ∈ [0,1]
    combines them (0 = identical behavior) — the scalar U10's promotion gate pauses on."""

    decision_disagreement: float
    ladder_gap: float
    belief_distance: float
    divergence_score: float


@dataclass
class Sim2RealReport:
    """Aggregate divergence across scenarios (plan R36). `mean_divergence` (or `max_divergence`) is the
    scalar fed to promotion.evaluate_promotion(divergence=...) so the gate consumes the gap."""

    per_scenario: list[DivergenceReport] = field(default_factory=list)
    mean_divergence: float = 0.0
    max_divergence: float = 0.0


# --- The voice arm: replay scripted prospect turns through a VoiceSession ----------------------


def _responded_prospect_texts(episode: Episode) -> list[str]:
    """The prospect turn texts the SIM arm's agent ACTUALLY RESPONDED TO, in order.

    The sim episode interleaves turn 0 (canned agent greeting), then prospect/agent pairs — BUT the
    final prospect turn is the TERMINAL one (the walk/commit line) that the agent never answered (the
    loop breaks before responding). We must replay ONLY the prospect turns that were followed by an
    agent turn; replaying the unanswered terminal turn would make the voice arm take one extra agent
    turn and falsely register a length divergence. So: a prospect turn counts iff the NEXT turn in the
    transcript is an agent turn (i.e. the agent responded to it).
    """
    turns = episode.turns
    out: list[str] = []
    for i, t in enumerate(turns):
        if t.speaker != "prospect" or not t.text:
            continue
        nxt = turns[i + 1] if i + 1 < len(turns) else None
        if nxt is not None and nxt.speaker == "agent":
            out.append(t.text)
    return out


async def _run_voice_arm(
    scenario: MatchedScenario,
    config: AgentConfig,
    agent_llm_factory: LLMFactory,
    prospect_texts: Sequence[str],
    *,
    seed: int,
    voice_noise: float,
) -> Episode:
    """Drive the SAME brain (a VoiceSession) on the SAME scripted prospect turns the sim arm produced.

    At voice_noise=0 the perceived user text is IDENTICAL to the sim arm's, so the decisions match
    (parity, R37). voice_noise>0 injects ASR-style corruption (selfplay.inject_noise, seeded) into the
    perceived user text BEFORE the brain sees it — modeling the real gap. The voice arm is committed
    turn-by-turn (commit_turn is the only writer) so its belief trajectory + decisions are captured,
    then projected into a channel="voice" Episode for divergence().
    """
    rng = random.Random(seed)
    session = VoiceSession(config, agent_llm_factory(), FakeEmbedder(), seed=seed)

    voice_turns: list[Turn] = []
    # Turn 0: mirror the sim arm's canned greeting (no respond() call) so the trajectories align.
    greeting = selfplay._opener(config)
    voice_turns.append(
        Turn(
            turn_id=0,
            speaker="agent",
            text=greeting,
            decision="greeting",
            rationale="canned opener (no user utterance yet)",
            belief=BeliefState.fresh().to_snapshot(),
        )
    )
    next_id = 1

    for i, clean_text in enumerate(prospect_texts):
        perceived = selfplay.inject_noise(clean_text, rng=rng, level=voice_noise)
        sid = f"v-{i}"
        pending = await session.handle_user_turn(perceived, sid)
        # Capture the prospect turn (the perceived text the brain saw) + the committed agent turn.
        voice_turns.append(Turn(turn_id=next_id, speaker="prospect", text=perceived))
        next_id += 1
        session.commit_turn(sid)
        voice_turns.append(
            Turn(
                turn_id=next_id,
                speaker="agent",
                text=pending.reply_text,
                decision=pending.decision.act,
                rationale=pending.decision.rationale,
                belief=pending.new_belief.to_snapshot(),
            )
        )
        next_id += 1

    stamp = config.stamp()
    return Episode(
        episode_id=f"v2r-voice-{scenario.label}-{seed}",
        turns=voice_turns,
        outcome=None,  # the voice arm replays a fixed script; outcome parity is judged on decisions
        ladder_tier=0,
        qualified=scenario.persona.qualified,
        version=stamp.get("version", ""),
        kb_version=stamp.get("kb_version", ""),
        channel="voice",
        persona=scenario.persona.archetype,
        cohort="sim2real",
    )


async def run_matched(
    scenario: MatchedScenario,
    config: AgentConfig,
    agent_llm_factory: LLMFactory,
    prospect_llm_factory: LLMFactory,
    *,
    seed: int,
    voice_noise: float = 0.0,
    kb_chunks: Optional[Sequence[Any]] = None,
) -> MatchedRun:
    """Run ONE matched scenario through both arms with matched inputs (plan R36/R37).

    SIM arm: selfplay.run_episode at CLEAN text (noise=0, persist=False) — it deterministically
    generates the prospect's turn script (the matched inputs). VOICE arm: the SAME brain driven on the
    SAME scripted prospect turns (extracted from the sim episode), with `voice_noise` applied to the
    PERCEIVED user text to model the ASR gap. Returns a MatchedRun(sim_episode, voice_episode).

    `kb_chunks` is accepted for API symmetry with the rest of the loop; the matched run does no RAG so
    both arms see the same (no-grounding) inputs — the divergence is the channel gap, not a KB gap.
    """
    _ = kb_chunks  # no RAG in the matched run — both arms ungrounded so the gap is the channel gap.
    # SIM arm — CLEAN text so the prospect script is the matched, noise-free input both arms replay.
    sim_episode = await selfplay.run_episode(
        scenario.persona,
        agent_llm_factory(),
        prospect_llm_factory(),
        config,
        seed=scenario.prospect_script_or_seed,
        cohort="sim2real",
        noise=0.0,
        persist=False,
    )

    # VOICE arm — same brain on the SAME scripted prospect turns; voice_noise models the real gap.
    prospect_texts = _responded_prospect_texts(sim_episode)
    voice_episode = await _run_voice_arm(
        scenario,
        config,
        agent_llm_factory,
        prospect_texts,
        seed=seed,
        voice_noise=voice_noise,
    )
    return MatchedRun(scenario=scenario, sim_episode=sim_episode, voice_episode=voice_episode)


# --- Divergence ---------------------------------------------------------------------------------


def _agent_decisions(episode: Episode) -> list[tuple[Optional[str], Optional[str]]]:
    """The ordered (act, tier-ish) of each NON-greeting agent turn — what the policy decided. tier is
    folded into the decision string (the schema Turn stores only decision act), so we compare acts."""
    out: list[tuple[Optional[str], Optional[str]]] = []
    for t in episode.turns:
        if t.speaker != "agent":
            continue
        if t.decision == "greeting":
            continue
        out.append((t.decision, None))
    return out


def _agent_belief_snapshots(episode: Episode) -> list[Optional[BeliefSnapshot]]:
    """The ordered belief snapshots of each non-greeting agent turn (the trajectory to diff)."""
    out: list[Optional[BeliefSnapshot]] = []
    for t in episode.turns:
        if t.speaker != "agent" or t.decision == "greeting":
            continue
        out.append(t.belief if isinstance(t.belief, BeliefSnapshot) else None)
    return out


def _driver_delta(a: BeliefSnapshot, b: BeliefSnapshot) -> float:
    """Mean absolute driver delta over the shared driver keys (0 == identical drivers)."""
    da = {k: float(v) for k, v in (a.drivers or {}).items()}
    db = {k: float(v) for k, v in (b.drivers or {}).items()}
    keys = set(da) & set(db)
    if not keys:
        return 0.0
    return sum(abs(da[k] - db[k]) for k in keys) / len(keys)


def _slot_mismatch(a: BeliefSnapshot, b: BeliefSnapshot) -> float:
    """Fraction of slots that DISAGREE across the two beliefs (0 == identical slot extraction).

    Over the union of slot names, a slot disagrees if it is present in one belief but absent in the
    other, or its extracted value differs. This is the DETERMINISTIC, text-sensitive component: the
    DST's regex slot extraction yields different slots on ASR-garbled vs clean text, so this term
    captures the sim-to-real gap even when an arm's LLM-emitted drivers are identical.
    """
    sa = a.slots or {}
    sb = b.slots or {}
    keys = set(sa) | set(sb)
    if not keys:
        return 0.0
    mismatches = 0
    for k in keys:
        va = (sa.get(k) or {}).get("value") if isinstance(sa.get(k), dict) else None
        vb = (sb.get(k) or {}).get("value") if isinstance(sb.get(k), dict) else None
        if va != vb:
            mismatches += 1
    return mismatches / len(keys)


def _decision_disagreement(sim: Episode, voice: Episode) -> float:
    """Fraction of MATCHED agent turns whose act differs (0 = all matched turns agree).

    Compared over the overlap (min length); if the arms produced different turn COUNTS, the unmatched
    tail is counted as disagreement so a structural divergence (one arm escalated early) still scores.
    """
    sd, vd = _agent_decisions(sim), _agent_decisions(voice)
    total = max(len(sd), len(vd))
    if total == 0:
        return 0.0
    overlap = min(len(sd), len(vd))
    mismatches = sum(1 for i in range(overlap) if sd[i][0] != vd[i][0])
    mismatches += abs(len(sd) - len(vd))  # length mismatch = structural disagreement
    return mismatches / total


def _ladder_gap(sim: Episode, voice: Episode) -> float:
    """Normalized outcome/ladder-tier gap between the arms (0 = same tier).

    ladder_tier ranges 0..4 (none..enrollment); the gap is the absolute tier difference / 4 so it is
    in [0,1]. The voice arm replays a fixed script (no terminal commit), so when its tier is 0 this
    measures how far the sim arm climbed beyond what the matched voice run reached.
    """
    return abs(int(sim.ladder_tier) - int(voice.ladder_tier)) / 4.0


# Within the belief-trajectory distance, weight the LLM-emitted driver delta and the deterministic
# slot-extraction mismatch. Slots carry the text-sensitive DST signal (the realistic ASR gap),
# drivers carry the latent read; both are belief layers, so the trajectory distance blends them.
_W_DRIVER = 0.5
_W_SLOT = 0.5


def _belief_distance(sim: Episode, voice: Episode) -> float:
    """Belief-trajectory distance over matched agent turns (plan R36): a blend of the mean absolute
    driver delta and the slot-extraction mismatch, both in [0,1], so the result is in [0,1].

    For each matched turn we combine |sim_driver - voice_driver| (the latent read) with the fraction of
    DISAGREEING slots (the DST's deterministic, text-sensitive extraction — the layer ASR noise most
    directly corrupts), then average across matched turns. 0 == identical belief trajectories (the
    parity floor at voice_noise=0). Unmatched turns are skipped here (decision-disagreement penalizes a
    length mismatch separately).
    """
    sb, vb = _agent_belief_snapshots(sim), _agent_belief_snapshots(voice)
    overlap = min(len(sb), len(vb))
    if overlap == 0:
        return 0.0
    per_turn: list[float] = []
    for i in range(overlap):
        a, b = sb[i], vb[i]
        if a is None or b is None:
            continue
        per_turn.append(_W_DRIVER * _driver_delta(a, b) + _W_SLOT * _slot_mismatch(a, b))
    if not per_turn:
        return 0.0
    return sum(per_turn) / len(per_turn)


# Weights combining the three components into the single divergence_score. Decision-disagreement
# dominates (behavior is what matters for the gate); ladder gap + belief distance refine it. They sum
# to 1 so the score stays in [0,1].
_W_DECISION = 0.5
_W_LADDER = 0.2
_W_BELIEF = 0.3


def divergence(sim_episode: Episode, voice_episode: Episode) -> DivergenceReport:
    """Compute the sim-vs-voice gap (plan R36). Over matched turns: decision-disagreement rate +
    outcome/ladder-tier gap + belief-trajectory distance (mean abs driver delta), combined into a
    single divergence_score ∈ [0,1] (0 = identical behavior — the parity floor at voice_noise=0).

    divergence_score = 0.5*decision_disagreement + 0.2*ladder_gap + 0.3*belief_distance, each term
    already normalized to [0,1], so the weighted sum is in [0,1].
    """
    decision_disagreement = _decision_disagreement(sim_episode, voice_episode)
    ladder_gap = _ladder_gap(sim_episode, voice_episode)
    belief_distance = _belief_distance(sim_episode, voice_episode)
    score = (
        _W_DECISION * decision_disagreement
        + _W_LADDER * ladder_gap
        + _W_BELIEF * belief_distance
    )
    # Clamp defensively so accumulated float error can never push the reported score out of [0,1].
    score = max(0.0, min(1.0, score))
    return DivergenceReport(
        decision_disagreement=decision_disagreement,
        ladder_gap=ladder_gap,
        belief_distance=belief_distance,
        divergence_score=score,
    )


async def sim_to_real_report(
    scenarios: Sequence[MatchedScenario],
    config: AgentConfig,
    agent_llm_factory: LLMFactory,
    prospect_llm_factory: LLMFactory,
    *,
    seed: int,
    voice_noise: float = 0.0,
) -> Sim2RealReport:
    """Aggregate divergence across scenarios (plan R36). Runs each scenario through both arms, computes
    its DivergenceReport, and returns the per-scenario list plus mean_divergence + max_divergence —
    the scalar(s) fed to promotion.evaluate_promotion(divergence=...) so the gate consumes the gap.

    Each scenario gets a per-scenario seed (seed + i) so the voice arm's noise injection is reproducible
    yet distinct per scenario.
    """
    per_scenario: list[DivergenceReport] = []
    for i, scenario in enumerate(scenarios):
        run = await run_matched(
            scenario,
            config,
            agent_llm_factory,
            prospect_llm_factory,
            seed=seed + i,
            voice_noise=voice_noise,
        )
        per_scenario.append(divergence(run.sim_episode, run.voice_episode))

    if per_scenario:
        scores = [r.divergence_score for r in per_scenario]
        mean_divergence = sum(scores) / len(scores)
        max_divergence = max(scores)
    else:
        mean_divergence = 0.0
        max_divergence = 0.0

    return Sim2RealReport(
        per_scenario=per_scenario,
        mean_divergence=mean_divergence,
        max_divergence=max_divergence,
    )

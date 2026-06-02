# Real-call replay fidelity runner (plan U15): builds a GenerativeTwin from a real Episode and
# runs the current agent against it N times, measuring sim-to-real fidelity using the existing
# divergence() metric from src.loop.sim2real. Returns a ReplayFidelity dataclass summarising
# mean_divergence, outcome_match_rate, and per-run divergence scores. Pure async, no DB writes
# (persist=False throughout), no API endpoints, no UI. Collaborators: src.sim.twin.GenerativeTwin,
# src.sim.selfplay.run_episode, src.loop.sim2real.divergence, src.memory.schema.Episode,
# src.config.settings.AgentConfig, src.core.llm.LLMClient.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from src.config.settings import AgentConfig
from src.core.llm import LLMClient
from src.loop.sim2real import divergence
from src.memory.schema import Episode
from src.sim import selfplay
from src.sim.twin import GenerativeTwin

# Type alias for an LLM client factory (called once per replay run to get a fresh client).
LLMFactory = Callable[[], LLMClient]


@dataclass
class ReplayFidelity:
    """Aggregated fidelity metrics from running the agent against a GenerativeTwin N times.

    `mean_divergence` and `per_run` consume divergence() (src.loop.sim2real): 0 = identical
    behavior. `outcome_match_rate` = fraction of replays whose .outcome matches the real episode's
    outcome. `real_outcome` echoes the real call's outcome label for context.
    """

    real_episode_id: str
    n: int
    mean_divergence: float
    outcome_match_rate: float
    per_run: list[float] = field(default_factory=list)
    real_outcome: str = ""


async def replay_call(
    real_episode: Episode,
    agent_llm_factory: LLMFactory,
    twin_llm_factory: LLMFactory,
    config: AgentConfig,
    *,
    n: int = 3,
    max_turns: int = 40,
    seed: int = 0,
) -> ReplayFidelity:
    """Run the current agent against a GenerativeTwin rebuilt from `real_episode` N times and
    measure sim-to-real fidelity.

    For each run i in range(n):
      1. Build a fresh GenerativeTwin(real_episode, seed=seed+i).
      2. Call run_episode(twin._persona, agent_llm_factory(), twin_llm_factory(), config,
                          seed=seed+i, max_turns=max_turns, persist=False, prospect=twin).
      3. Compute divergence(real_episode, replay_episode).divergence_score.

    Aggregates into ReplayFidelity:
      - mean_divergence: arithmetic mean of per-run divergence scores.
      - outcome_match_rate: fraction of replays where replay.outcome == real_episode.outcome.
      - per_run: list of per-run divergence scores (length == n).

    No DB writes: persist=False is always passed to run_episode. This is a pure measurement
    function; it does not modify the real episode or any stored state.
    """
    per_run_scores: list[float] = []
    outcome_matches: int = 0
    real_outcome = real_episode.outcome or ""

    for i in range(n):
        run_seed = seed + i

        # Build a fresh GenerativeTwin for this run (different seed = different stochasticity).
        twin = GenerativeTwin(real_episode, seed=run_seed)

        # Run the agent against the twin (persist=False: no DB writes).
        replay_episode = await selfplay.run_episode(
            twin.persona,
            agent_llm_factory(),
            twin_llm_factory(),
            config,
            seed=run_seed,
            max_turns=max_turns,
            persist=False,
            prospect=twin,
        )

        # Measure divergence from the real episode.
        report = divergence(real_episode, replay_episode)
        per_run_scores.append(report.divergence_score)

        # Check outcome match.
        if replay_episode.outcome == real_outcome:
            outcome_matches += 1

    mean_div = sum(per_run_scores) / len(per_run_scores) if per_run_scores else 0.0
    match_rate = outcome_matches / n if n > 0 else 0.0

    return ReplayFidelity(
        real_episode_id=real_episode.episode_id,
        n=n,
        mean_divergence=mean_div,
        outcome_match_rate=match_rate,
        per_run=per_run_scores,
        real_outcome=real_outcome,
    )

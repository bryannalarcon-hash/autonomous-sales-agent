# CB-43 eval runner — run ONE deeply-prompted NAMED persona (Dana/Marcus/Pat from src/sim/personas.py)
# end-to-end through the agent BRAIN via src/sim/selfplay.run_episode, and emit its transcript +
# outcome + turn count. Every episode it persists is cohort-tagged with an EXPERIMENT/eval cohort
# (EVAL_COHORT_PREFIX + "_" + persona name, e.g. "experiment_eval_personas_dana") — NEVER "live" — so
# it can never flood the operator live Calls log (which scopes to cohort='live'; operate._is_active
# only treats fresh-heartbeat in_progress rows as active, and a finished self-play episode is neither).
# Two helpers: build_eval_cohort(name) (the tag) and run_named_eval(...) (the programmatic entry, LLM
# clients injected so unit code/tests need no network); plus a thin CLI main() mirroring scripts/
# run_one_sim.py (reads AGENT_MODEL/SIM_MODEL from .env, never prints the API key). Collaborators:
# src.sim.personas (named_persona/NAMED_PERSONAS), src.sim.selfplay (run_episode), src.config.settings
# (load_config), src.core.llm (OpenRouterClient). Headless: NO LiveKit; the brain runs on text.
from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any, Optional

from src.config.settings import AgentConfig, load_config
from src.core.llm import LLMClient
from src.sim.personas import NAMED_PERSONAS, named_persona
from src.sim.selfplay import run_episode

# The experiment/eval cohort prefix. Reuses the established "experiment_" convention (src/api/improve.py
# tags arm episodes "experiment_<arm>"); the operator Calls log filters to cohort='live', so anything
# under this prefix is excluded from the live view by construction. NEVER "live".
EVAL_COHORT_PREFIX = "experiment_eval_personas"


def build_eval_cohort(name: str) -> str:
    """The cohort tag for a named-persona eval episode: '<EVAL_COHORT_PREFIX>_<name>'. Validates the
    name against the locked CB-43 set (raises KeyError otherwise) so a typo can't silently mis-tag."""
    key = (name or "").strip().lower()
    if key not in NAMED_PERSONAS:
        raise KeyError(f"unknown named persona {name!r}; known: {NAMED_PERSONAS}")
    return f"{EVAL_COHORT_PREFIX}_{key}"


def transcript_lines(episode: Any) -> list[str]:
    """Render an episode's turns as 'speaker: text' lines — a readable transcript for the runner's
    stdout / a caller's report. Reads only the public Turn fields (speaker/text)."""
    lines: list[str] = []
    for t in episode.turns:
        lines.append(f"{t.speaker}: {t.text}")
    return lines


async def run_named_eval(
    name: str,
    agent_llm: LLMClient,
    prospect_llm: LLMClient,
    config: AgentConfig,
    *,
    seed: int = 0,
    max_turns: int = 24,
    noise: float = 0.0,
    persist: bool = True,
) -> dict[str, Any]:
    """Run ONE named eval persona through the agent brain (self-play) and return a summary dict:
    {episode, episode_id, persona, cohort, outcome, turn_count, qualified, disqualifier, transcript}.

    The persona is resolved FRESH from named_persona(name) (its rich behavior_prompt + seeded non-
    sequiturs drive the prospect prompt). The episode is persisted (unless persist=False) under the
    eval cohort from build_eval_cohort(name) — never 'live' — so it stays out of the operator live
    Calls log. The two LLM clients are SEPARATE on purpose (agent vs prospect are different model
    families); inject MockLLMClients in tests so no network is touched."""
    persona = named_persona(name)
    cohort = build_eval_cohort(name)
    episode = await run_episode(
        persona,
        agent_llm,
        prospect_llm,
        config,
        seed=seed,
        cohort=cohort,
        max_turns=max_turns,
        noise=noise,
        persist=persist,
    )
    turn_count = int((episode.metrics or {}).get("turn_count", 0))
    return {
        "episode": episode,
        "episode_id": episode.episode_id,
        "persona": persona.name or name,
        "cohort": cohort,
        "outcome": episode.outcome,
        "turn_count": turn_count,
        "qualified": persona.qualified,
        "disqualifier": persona.disqualifier,
        "transcript": transcript_lines(episode),
    }


# --- CLI (real-LLM) — mirrors scripts/run_one_sim.py; never prints the API key --------------------

async def _main(name: str, seed: int, max_turns: int, noise: float, persist: bool) -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ABORT: OPENROUTER_API_KEY not set.")
        return 2
    # Imported here so importing the module (for the programmatic helpers/tests) needs no network env.
    from dotenv import load_dotenv

    from src.core.llm import OpenRouterClient

    load_dotenv()
    agent_model = os.environ.get("AGENT_MODEL", "anthropic/claude-sonnet-4.5")
    sim_model = os.environ.get("SIM_MODEL", "openai/gpt-4o")
    cohort = build_eval_cohort(name)
    print(
        f"eval_personas — persona={name} cohort={cohort} agent NLG={agent_model} prospect={sim_model} "
        f"seed={seed} max_turns={max_turns} noise={noise} persist={persist}"
    )
    result = await run_named_eval(
        name,
        OpenRouterClient(model=agent_model),
        OpenRouterClient(model=sim_model),
        load_config("champion_v0"),
        seed=seed,
        max_turns=max_turns,
        noise=noise,
        persist=persist,
    )
    print("---- TRANSCRIPT ----")
    for line in result["transcript"]:
        print(line)
    print("---- OUTCOME ----")
    print(
        f"episode_id={result['episode_id']} outcome={result['outcome']} "
        f"turns={result['turn_count']} qualified={result['qualified']} "
        f"disqualifier={result['disqualifier']} cohort={result['cohort']}"
    )
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Run one CB-43 named eval persona through the brain.")
    p.add_argument("persona", choices=list(NAMED_PERSONAS), help="named eval persona to run")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--max-turns", type=int, default=24)
    p.add_argument("--noise", type=float, default=0.0)
    p.add_argument("--no-persist", action="store_true", help="run but do NOT save the episode")
    a = p.parse_args(argv)
    return asyncio.run(_main(a.persona, a.seed, a.max_turns, a.noise, not a.no_persist))


if __name__ == "__main__":
    raise SystemExit(main())

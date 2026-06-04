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
    retrieve_facts: Optional[Any] = None,
) -> dict[str, Any]:
    """Run ONE named eval persona through the agent brain (self-play) and return a summary dict:
    {episode, episode_id, persona, cohort, outcome, turn_count, qualified, disqualifier, transcript}.

    The persona is resolved FRESH from named_persona(name) (its rich behavior_prompt + seeded non-
    sequiturs drive the prospect prompt). The episode is persisted (unless persist=False) under the
    eval cohort from build_eval_cohort(name) — never 'live' — so it stays out of the operator live
    Calls log. The two LLM clients are SEPARATE on purpose (agent vs prospect are different model
    families); inject MockLLMClients in tests so no network is touched.

    retrieve_facts (optional) is the KB grounding hook threaded into the brain so the agent can
    answer with REAL Nerdy facts (the CLI wires the live pgvector hook; tests pass None to stay
    offline). It may be sync or async — selfplay awaits it when needed."""
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
        retrieve_facts=retrieve_facts,
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

    from src.core import cost
    from src.core.llm import OpenRouterClient

    load_dotenv()
    # CB-52: per-run cost tracking. reset() zeroes the in-process accumulator so the printed total is
    # THIS run's spend; LLM_COST_LOG (default the gitignored eval log) makes every LLM call append a
    # per-call line to a durable cross-process ledger (feeds docs/eval-budget-ledger.md).
    cost.reset()
    os.environ.setdefault("LLM_COST_LOG", "docs/eval-cost-log.jsonl")
    agent_model = os.environ.get("AGENT_MODEL", "anthropic/claude-sonnet-4.5")
    sim_model = os.environ.get("SIM_MODEL", "openai/gpt-4o")
    config = load_config("champion_v0")
    cohort = build_eval_cohort(name)

    # Wire the REAL KB grounding hook (the SAME build_live_retrieve_hook the demo API + voice worker
    # use), so the agent answers with real Nerdy facts from pgvector instead of running KB-blind.
    # Needs the embedder (sentence-transformers) + asyncpg — run this CLI under /usr/bin/python3.
    # Set EVAL_NO_KB=1 to disable (offline / no-DB sanity run). Lazy import: the heavy ML deps load
    # only on the real CLI path, never on a plain module import.
    retrieve_facts = None
    if os.environ.get("EVAL_NO_KB", "").strip().lower() not in ("1", "true", "yes", "on"):
        from src.kb.embeddings import SentenceTransformerEmbedder
        from src.kb.live import build_live_retrieve_hook

        _live_hook = build_live_retrieve_hook(SentenceTransformerEmbedder(), kb_version=config.kb_version, k=4)

        async def retrieve_facts(belief: Any, text: str) -> Any:  # noqa: ANN001 (selfplay hook shape)
            return await _live_hook(text, kb_version=config.kb_version, k=4)

    print(
        f"eval_personas — persona={name} cohort={cohort} agent NLG={agent_model} prospect={sim_model} "
        f"kb={config.kb_version if retrieve_facts else 'OFF'} "
        f"seed={seed} max_turns={max_turns} noise={noise} persist={persist}"
    )
    result = await run_named_eval(
        name,
        OpenRouterClient(model=agent_model),
        OpenRouterClient(model=sim_model),
        config,
        seed=seed,
        max_turns=max_turns,
        noise=noise,
        persist=persist,
        retrieve_facts=retrieve_facts,
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
    print(cost.format_snapshot())  # CB-52: this run's measured OpenRouter spend, per model
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

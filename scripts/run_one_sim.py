#!/usr/bin/env python3
# scripts/run_one_sim.py — run ONE real-LLM self-play episode against the champion config and PERSIST
# it (channel="sim") so the operator Review page has a fresh episode carrying CB-06 prospect_trajectory.
# Uses the locked mixed-model setup read from .env (agent NLG=AGENT_MODEL, prospect=SIM_MODEL, DST/policy
# overridden per-call inside dst.py/policy.py via DST_MODEL/POLICY_MODEL). Prints the episode_id. Never
# prints the API key. One qualified persona by default so the arc climbs + closes (good belief-gap demo).
from __future__ import annotations

import argparse
import asyncio
import os
import random

from dotenv import load_dotenv

from src.config.settings import load_config
from src.core.llm import OpenRouterClient
from src.sim.personas import _build_qualified, _build_unqualified
from src.sim.selfplay import run_episode

load_dotenv()


async def main(seed: int, max_turns: int, unqualified: bool) -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ABORT: OPENROUTER_API_KEY not set.")
        return 2
    agent_model = os.environ.get("AGENT_MODEL", "anthropic/claude-sonnet-4.5")
    sim_model = os.environ.get("SIM_MODEL", "openai/gpt-4o")
    print(
        f"run_one_sim — agent NLG={agent_model} prospect={sim_model} "
        f"DST={os.environ.get('DST_MODEL','(default)')} POLICY={os.environ.get('POLICY_MODEL','(default)')} "
        f"seed={seed} max_turns={max_turns} unqualified={unqualified}"
    )
    rng = random.Random(seed)
    persona = _build_unqualified(rng, 0) if unqualified else _build_qualified(rng, 0)
    config = load_config("champion_v0")
    agent_llm = OpenRouterClient(model=agent_model)
    prospect_llm = OpenRouterClient(model=sim_model)

    ep = await run_episode(
        persona,
        agent_llm,
        prospect_llm,
        config,
        seed=seed,
        max_turns=max_turns,
        persist=True,
    )
    traj = (ep.metrics or {}).get("prospect_trajectory") or []
    print(
        f"DONE episode_id={ep.episode_id} outcome={ep.outcome} turns={len(ep.turns)} "
        f"prospect_trajectory_len={len(traj)} qualified={persona.qualified}"
    )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--max-turns", type=int, default=20)
    p.add_argument("--unqualified", action="store_true")
    a = p.parse_args()
    raise SystemExit(asyncio.run(main(a.seed, a.max_turns, a.unqualified)))

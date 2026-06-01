#!/usr/bin/env python3
# scripts/smoke_real_llm.py — a TINY real-LLM end-to-end smoke test of the self-improvement pipeline.
# Proves the agent brain (respond) + honest prospect simulator + self-play harness + grading actually
# EXECUTE against live models via OpenRouter (agent=AGENT_MODEL, prospect=SIM_MODEL) — NOT a statistical
# experiment (n is tiny by design to keep spend minimal). Runs a few synthetic callers against the
# champion config and a discovery-sequencing challenger, then grades the pair, printing an HONEST
# summary (outcomes + KPIs + real LLM call count). No DB needed (persist=False). Never prints the key.
from __future__ import annotations

import argparse
import asyncio
import os
import time
from typing import Any, Optional, Sequence

from dotenv import load_dotenv

from src.config.settings import load_config
from src.core.llm import Message, OpenRouterClient
from src.loop.generator import reorder_discovery
from src.loop.grading import compare_versions, kpi_score
from src.sim.personas import sample_population
from src.sim.selfplay import outcome_distribution, run_batch

load_dotenv()


class _CountingClient:
    """Wraps an LLMClient and counts real calls so the smoke test can report the spend footprint."""

    def __init__(self, inner: Any, label: str) -> None:
        self.inner = inner
        self.label = label
        self.calls = 0

    async def complete(self, messages: Sequence[Message], **kw: Any) -> str:
        self.calls += 1
        return await self.inner.complete(messages, **kw)

    async def complete_json(self, messages: Sequence[Message], **kw: Any) -> Any:
        self.calls += 1
        return await self.inner.complete_json(messages, **kw)


def _summarize(label: str, episodes: Sequence[Any]) -> str:
    dist = outcome_distribution(episodes)
    return (
        f"  {label}: n={len(episodes)}  kpi(weighted-ladder mean)={kpi_score(episodes):.3f}  "
        f"outcomes={dist}"
    )


async def main(n_callers: int, max_turns: int) -> int:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("ABORT: OPENROUTER_API_KEY not set in the environment / .env — cannot run a real smoke test.")
        return 2
    agent_model = os.environ.get("AGENT_MODEL", "anthropic/claude-sonnet-4.5")
    sim_model = os.environ.get("SIM_MODEL", "openai/gpt-4o")
    print(
        f"Real-LLM smoke test — agent={agent_model}  prospect={sim_model}  "
        f"callers={n_callers}  max_turns={max_turns}\n"
        "(tiny by design — proves end-to-end execution on live models; NOT a statistical experiment)\n"
    )

    # Shared counting clients (OpenRouterClient opens a fresh httpx client per call, so sharing is safe).
    agent_inner = OpenRouterClient(model=agent_model)
    prospect_inner = OpenRouterClient(model=sim_model)
    agent_client = _CountingClient(agent_inner, "agent")
    prospect_client = _CountingClient(prospect_inner, "prospect")

    def agent_factory() -> Any:
        return agent_client

    def prospect_factory() -> Any:
        return prospect_client

    champion = load_config("champion_v0")
    # The challenger differs by exactly ONE dimension: the discovery question order (the demo dim).
    seq = list(champion.playbooks.get("discovery_sequence", []))
    challenger_seq = ([seq[1], seq[0]] + seq[2:]) if len(seq) >= 2 else seq
    challenger = reorder_discovery(champion, challenger_seq)

    personas = sample_population(n_callers, seed=20260531)

    t0 = time.monotonic()
    errors: list[str] = []
    champ_eps: list[Any] = []
    chal_eps: list[Any] = []
    try:
        champ_eps = await run_batch(
            personas, agent_factory, prospect_factory, champion,
            base_seed=1, cohort="smoke", max_turns=max_turns, concurrency=2, persist=False,
        )
        chal_eps = await run_batch(
            personas, agent_factory, prospect_factory, challenger,
            base_seed=1, cohort="smoke", max_turns=max_turns, concurrency=2, persist=False,
        )
    except Exception as exc:  # noqa: BLE001 — a smoke test reports failures, it does not raise
        errors.append(f"{type(exc).__name__}: {exc}")

    elapsed = time.monotonic() - t0
    print("--- RESULT ---")
    if errors:
        print("PIPELINE ERROR (the real-LLM path did NOT complete cleanly):")
        for e in errors:
            print(f"  {e}")
    if champ_eps:
        print(_summarize("champion ", champ_eps))
    if chal_eps:
        print(_summarize("challenger", chal_eps))

    cmp_line = ""
    if champ_eps and chal_eps:
        cmp = compare_versions(champ_eps, chal_eps, seed=7)
        cmp_line = (
            f"  delta(challenger-champion)={cmp.delta:+.3f}  challenger_better={cmp.challenger_better} "
            f"(n={len(champ_eps)} — NOT statistically meaningful at this n; smoke only)"
        )
        print(cmp_line)

    print(
        f"\nreal LLM calls: agent={agent_client.calls}  prospect={prospect_client.calls}  "
        f"total={agent_client.calls + prospect_client.calls}   wall={elapsed:.1f}s"
    )
    ok = bool(champ_eps and chal_eps and not errors)
    print(f"\nSMOKE TEST {'PASSED — real-LLM pipeline executed end-to-end' if ok else 'FAILED'}.")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Tiny real-LLM end-to-end smoke test of the loop.")
    ap.add_argument("--callers", type=int, default=3, help="synthetic callers per arm (keep small)")
    ap.add_argument("--max-turns", type=int, default=10, help="max turns per call")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.callers, args.max_turns)))

#!/usr/bin/env python3
# scripts/time_turn.py — per-stage latency profiler for ONE live agent turn (diagnoses the live-call
# "dead air" — R43). Times dst.update / policy.decide / nlg.realize separately against the REAL
# OpenRouter model the voice worker uses, so we know which stage(s) cost the seconds a phone caller
# hears as silence. Prints stage seconds + total; never prints the API key. Tiny spend (3 calls).
from __future__ import annotations

import asyncio
import os
import time

from dotenv import load_dotenv

from src.config.settings import load_config
from src.core import dst
from src.core.belief_state import BeliefState
from src.core.llm import OpenRouterClient
from src.core.nlg import realize
from src.core.policy import decide

load_dotenv()


async def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("ABORT: OPENROUTER_API_KEY not set")
        return 2
    model = os.environ.get("AGENT_MODEL", "anthropic/claude-sonnet-4.5")
    client = OpenRouterClient(model=model)
    config = load_config("champion_v0")
    belief = BeliefState.fresh()
    history = [
        {"role": "assistant", "act": "greet", "text": "Hi! Thanks for calling Nerdy. How can I help?"},
    ]
    user = "What services do you guys offer?"

    print(f"model={model}\nuser={user!r}\n--- per-stage timing ---")

    # Time the LIVE RAG path the voice worker actually runs (embedder + pgvector query) — the live
    # call's ~30s of silence happened AFTER the query embedding logged, so the DB lookup is suspect.
    facts = None
    try:
        from src.kb.live import build_live_retrieve_hook
        from src.kb.embeddings import SentenceTransformerEmbedder

        te0 = time.monotonic()
        emb = SentenceTransformerEmbedder()
        emb.embed(["warm up"])  # mirror prewarm so we time the query, not the model load
        te1 = time.monotonic()
        hook = build_live_retrieve_hook(emb, kb_version=config.kb_version)
        facts = await hook(user, kb_version=config.kb_version, k=4)
        te2 = time.monotonic()
        print(f"  embedder load : {te1 - te0:6.2f}s  (prewarmed at process start in prod)")
        print(f"  RAG retrieve  : {te2 - te1:6.2f}s  ({len(facts or [])} chunks)")
    except Exception as exc:  # noqa: BLE001
        print(f"  RAG retrieve  : FAILED {type(exc).__name__}: {exc}")

    t0 = time.monotonic()
    nb = await dst.update(belief, "greet", user, client)
    t1 = time.monotonic()
    decision = await decide(nb, history=history, config=config, llm_client=client)
    t2 = time.monotonic()
    reply = await realize(decision, nb, config, client, retrieved_facts=facts)
    t3 = time.monotonic()

    print(f"  dst.update    : {t1 - t0:6.2f}s")
    print(f"  policy.decide : {t2 - t1:6.2f}s")
    print(f"  nlg.realize   : {t3 - t2:6.2f}s")
    print(f"  TOTAL         : {t3 - t0:6.2f}s  (caller hears this much silence per turn)")
    print(f"\n  decision.act={decision.act!r}\n  reply={reply!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

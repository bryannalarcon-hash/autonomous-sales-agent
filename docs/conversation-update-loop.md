# Autonomous Sales Agent — Per-Conversation Update Loop

> How the agent processes **one caller turn** — update belief, decide an action, speak — repeated
> every turn until the call reaches a terminal outcome. The brain is transport-agnostic
> (`src/core/respond.py`): the **same** code runs the live voice worker and text self-play
> (**R37 parity** — no LiveKit type crosses into `src/core`).
>
> Neuro-symbolic contract: **the LLM proposes, deterministic gates decide.** Three LLM calls per turn
> — ① DST, ② Policy proposer, ③ NLG — and only ③ generates words.

## The loop (core)

```mermaid
flowchart TD
  IN["Caller turn<br/>STT / text in"]:::io
  GATE{"Consent<br/>cleared?"}:::dec
  DISC["Speak AI-disclosure"]:::io

  subgraph TURN["respond() &nbsp;·&nbsp; one turn &nbsp;↻ repeats"]
    direction TB
    DST["① DST · update belief<br/>regex + gpt-5-nano"]:::llm
    BELIEF[("Belief · 6 drivers<br/>+ trends + stage")]:::state
    POL["② Policy · propose act<br/>gpt-5-mini"]:::llm
    GATES{{"Deterministic gates<br/>safety decides"}}:::safety
    NLG["③ NLG · reply, streamed<br/>claude-sonnet-4.5"]:::llm
    DST --> BELIEF --> POL --> GATES --> NLG
  end

  COMMIT["Speak + commit turn"]:::io
  OUTCOME(["Outcome · booked / walked /<br/>escalated / released"]):::done

  IN ==> GATE
  GATE -- no --> DISC
  DISC -. re-ask .-> GATE
  GATE == yes ==> DST
  NLG ==> COMMIT
  COMMIT == terminal ==> OUTCOME
  COMMIT -. next caller turn .-> IN

  subgraph LEGEND[" "]
    direction LR
    L1["I/O"]:::io
    L2["① ② ③ LLM call"]:::llm
    L3["belief"]:::state
    L4["gate"]:::safety
    L5["decision"]:::dec
    L6["outcome"]:::done
    L1 ~~~ L2 ~~~ L3 ~~~ L4 ~~~ L5 ~~~ L6
  end
  OUTCOME ~~~ LEGEND

  classDef io fill:#1e3a8a,stroke:#7cb1ff,color:#eaf3ff;
  classDef dec fill:#a5610f,stroke:#fcd34d,color:#fffbeb;
  classDef llm fill:#5b21b6,stroke:#c9b6ff,color:#f7f4ff;
  classDef state fill:#0f5e57,stroke:#5eead4,color:#d7fff8;
  classDef safety fill:#991b1b,stroke:#fca5a5,color:#fff0f0;
  classDef done fill:#166534,stroke:#86efac,color:#e7ffe9;
```

## Inside the gates (detail / zoom-in)

`apply_gates()` runs the LLM's proposed act through an ordered chain of pure, deterministic gates
that have **final say** — the live transcript proved the LLM will not reliably self-police, so
safety/correctness lives here, not in the prompt.

```mermaid
flowchart LR
  PROP(["LLM proposed act"]):::llm --> G1
  G1["escalation_triggers"]:::safety --> G2["de_escalate<br/>hostility / bail≥0.9"]:::safety --> G3["skip_known"]:::safety --> G4["no_repeat_discovery"]:::safety --> G5["establish_who_first"]:::safety --> G6["address_direct_input"]:::safety --> G7["must_clear_objection"]:::safety --> G8["price_gate"]:::safety --> G9["advance_to_close /<br/>close_floor"]:::safety --> G10["pushiness_cap"]:::safety --> G11["buy_gate<br/>(honest, pure)"]:::safety --> GATED(["gated decision → NLG"]):::done
  classDef safety fill:#7f1d1d,stroke:#fca5a5,color:#fee2e2;
  classDef llm fill:#5b21b6,stroke:#c9b6ff,color:#f7f4ff;
  classDef done fill:#166534,stroke:#86efac,color:#e7ffe9;
```

## Notes

- **Three sequential LLM calls per turn** (the latency budget): DST `gpt-5-nano` → Policy proposer
  `gpt-5-mini` → NLG `claude-sonnet-4.5` (all via OpenRouter). NLG is the only generative call and is
  **streamed to TTS** (CB-41) so the first audible word lands at NLG first-token.
- **Belief is Markov + frame-stacked:** the 6 driver *levels* (trust, urgency, bail_risk,
  need_intensity, purchase_intent, price_sensitivity) plus their *velocities* (deterministic deltas)
  let the Policy see direction-of-travel without unbounded history.
- **Buy-gate purity:** a commitment is immutable to talk — it fires only when the agent runs
  `attempt_close` at a tier the prospect's budget + qualification actually support. The agent cannot
  "talk its way" to a sale.
- **Returning caller:** phone → sha256 hash (raw phone never stored) → prior lead hydrates slots so
  `skip_known` won't re-ask, with sticky prior objections.
- **R37 parity:** this exact loop is what `tests/integration/test_voice_sim.py` (roomless voice) and
  text self-play both exercise — voice changes only *delivery* (STT/TTS/streaming), never decisions.
- **Out of scope here:** the separate *self-improvement* loop (generator → experiment →
  bootstrap-CI grading → sim-to-real divergence gate → promotion) runs across many conversations.
- **[Experimental, CB-42]** a distilled-ML variant replaces the DST `gpt-5-nano` call with a local
  MLP driver-delta regressor and the Policy `gpt-5-mini` proposer with a confidence-gated ML
  classifier (same gate chain, NLG stays the streamed LLM) — ~3→1.1 LLM calls/turn. Lives on the
  `cb42-ml-distill` branch, not this (production) path.

<!-- Rendered reference: experiments/cb42 render pipeline produced /tmp/mmd/loop_v3.png during design. -->
```

# Recursive Self-Improvement: How the Agent Gets Better

*Purpose: describe, grounded in code, the closed loop by which the agent improves itself — and show the discovery-sequencing experiment as the worked before/after example, with every number labeled HONESTLY (illustrative harness output vs. measured model run).*

---

## The one-paragraph version

The agent improves by running a **batch champion-vs-challenger tournament against honest synthetic prospects**, and promoting a challenger only when a deterministic, over-claim-resistant gate says it genuinely won. A *challenger* is the current champion config plus exactly one declared change. It is run head-to-head against the champion on a frozen, adversarial prospect set; both arms are graded; and the challenger is promoted only if it shows a statistically significant lift **and** clean guardrails **and** held qualification accuracy **and** the sim-to-real divergence is within threshold. Clean, low-stakes wins auto-promote (human-on-the-loop, reviewed after the fact); high-stakes wins (pricing, persona) are blocked for human sign-off. Every promotion is recorded in a version lineage so any change is attributable and reversible.

---

## The closed loop, stage by stage (all code-grounded)

```
   mined episodes
        │
        ▼
 ┌──────────────┐   generate     ┌────────────────────┐  experiment   ┌──────────────┐
 │  GENERATOR   │ ─────────────▶ │ champion vs         │ ────────────▶ │   GRADING    │
 │ minimal-diff │  one declared  │ challenger on a     │  frozen       │ groundedness │
 │  challenger  │  dimension     │ FROZEN held-out set │  held-out set │ + pairwise   │
 └──────────────┘                └────────────────────┘               │ judge + CIs  │
                                                                       └──────┬───────┘
                                                                              │
                       ┌──────────────────────────────────────────────────────┘
                       ▼
              ┌──────────────────┐    auto-promote (clean, non-extreme)    ┌─────────────┐
              │  PROMOTION GATE  │ ──────────────────────────────────────▶ │   VERSION   │
              │ divergence pause │    pending_approval (extreme)            │   LINEAGE   │
              │ ∧ lift ∧ guardr. │ ──────────────────────────────────────▶ │ (rollback)  │
              │ ∧ qualification  │    rejected / paused                     └─────────────┘
              └──────────────────┘
```

### 1. Generate — a minimal-diff challenger (`src/loop/generator.py`)
The generator is **failure-conditioned** (it clusters losing episodes — `walked` or `ladder_tier < 2`) and **tactic-mined** (it looks at winning episodes, `ladder_tier ≥ 3`). It then proposes a candidate by either:
- a **deterministic reorder** of `playbooks.discovery_sequence` (the demo text dimension — no LLM needed), or
- a **parametric perturbation** of one safe numeric threshold (seeded direction).

The mutation surface is **prompts + playbooks + thresholds + persona only** (R21 — never code). Every returned challenger satisfies `is_minimal_diff` (exactly one changed dimension) and carries a deterministic `challenger_version`.

### 2. Experiment — both arms on the SAME frozen held-out set (`src/loop/experiment.py`)
`run_experiment` runs champion and challenger on the **same** frozen, adversarial persona set with the **same** base seed (a paired comparison — the only difference is the config). The held-out set is reproducible (`frozen_held_out(seed, n, rotation_index)`) and rotates while excluding any R22-mined personas (so mined failures never leak into the eval set).

### 3. Grade — deterministic groundedness + pairwise judge + bootstrap CIs (`src/loop/grading.py`)
Three layers, in priority of trust:
- **Deterministic groundedness** (`episode_groundedness`) — reuses the same KB-claim check the live guardrail uses.
- **Deterministic guardrail metrics** (`guardrail_report`) — pushiness rate + false-promise flags, computed from the logged trajectory.
- **Pairwise LLM-judge** (`judge_pairwise`) — Arena-style, cross-family, position-bias-mitigated (asks both orders; disagreement → tie).
- **Bootstrap CIs** (`compare_versions`) — the KPI is the weighted commitment-ladder mean (`callback=1 < consultation=2 < trial=3 < enrollment=4`); `challenger_better` is True only if the delta > 0 **and** its bootstrap CI excludes 0.

### 4. Promotion gate — the integrity-critical conjunction (`src/loop/promotion.py::evaluate_promotion`)
A **pure function** (testable with no self-play). In order:
1. **Sim-to-real divergence pause (R36)** — if `divergence > threshold` (default `0.2`) → `paused`; nothing promotes while sim and real disagree. `divergence=None` ⇒ not yet measured ⇒ skip.
2. **The bar** — `challenger_better` (significant lift) **AND** no guardrail regression **AND** qualification accuracy held (within `QUAL_TOLERANCE = 0.02`).
3. Bar failed → `rejected` (with a reason per failed criterion).
4. Bar passed **and** extreme → `pending_approval` (`requires_human=True`).
5. Bar passed **and** non-extreme → `promoted` (auto, with HOTL post-hoc review).

### 5. Promote + lineage (`src/loop/promotion.py::promote`)
A promoted challenger is recorded as the new champion via `store.record_version`, which demotes the prior champion (single-champion invariant) and writes a `VersionLineage` node (parent link + KPI snapshot). Rollback is supported (dashboard P9 / `POST /api/versions/{version}/rollback`).

---

## Verified gate behavior (this run, against the actual `evaluate_promotion`)

The four promotion branches were exercised against the **real** `src/loop/promotion.py::evaluate_promotion` function with stacked inputs (no self-play, no network — the same way `tests/integration/test_loop.py` tests it). Observed:

| Scenario (stacked input)                                              | Returned status     | promote / requires_human |
|-----------------------------------------------------------------------|---------------------|--------------------------|
| Significant lift, guardrails clean, qual held, **non-extreme**        | `promoted`          | promote=True             |
| Same, but **extreme** diff (pricing/persona)                          | `pending_approval`  | requires_human=True      |
| Divergence `0.35` > threshold `0.20`                                  | `paused`            | (no promotion)           |
| Delta CI includes 0 (no significant lift)                             | `rejected`          | —                        |

> These are **mechanism verifications** of the gate logic on hand-constructed inputs — not a measured model run. They prove the gate routes correctly; they do not assert any real KPI.

---

## The worked example: the discovery-sequencing experiment (before/after)

The brainstorm's first improvement dimension is **discovery sequencing** — *when* budget/price is allowed to enter the conversation, relative to the trust gate. The champion's `discovery_sequence` in `champion_v0.yaml` is:

```
[grade_level, subject, goal, timeline, prior_tutoring, budget]
```

with `budget` intentionally **last** (trust-gated). A discovery-sequencing challenger reorders this sequence (`src/loop/generator.py::reorder_discovery`) and is graded on the weighted-ladder KPI against the frozen set.

### Honesty label on the numbers

**A tiny real-LLM end-to-end SMOKE run has been executed; no large statistical batch has.** The pipeline that produces a before/after is fully **built and unit-tested** (U8 self-play, U9 grading, U10 loop — see `tests/integration/test_selfplay.py`, `test_grading.py`, `test_loop.py`, all green within the 224-test suite). Beyond the mock-LLM tests, `scripts/smoke_real_llm.py` was run against **live models** (agent `anthropic/claude-sonnet-4.5`, prospect `openai/gpt-4o`) over a deliberately tiny `n=2` callers / `max_turns=8` champion-vs-challenger pair: **72 real LLM calls, ~96s, pipeline executed end-to-end and graded successfully.** At that size BOTH short calls ended in `walked` (weighted-ladder KPI `0.0` on both arms) — which is **not a statistical result and not evidence about conversion**: an 8-turn budget is far too short to complete the champion's multi-slot discovery → pitch → close (the real default is 24 turns), so the synthetic prospects' patience simply runs out. The smoke run proves the live-model path *executes + grades*; it asserts nothing about lift. **No large statistical batch has been run** — doing so is a single command (see the demo runbook).

Therefore the before/after below is presented as an **illustrative, representative shape of the harness output** — the columns and the decision logic are exactly what the code computes, but the specific numbers are **not measured results**. They are placeholders showing *what the artifact looks like and how it would read*, not evidence of a lift.

### Representative before/after (ILLUSTRATIVE — not a measured run)

| Metric (weighted-ladder KPI, frozen held-out set)   | Champion (v0)     | Challenger (reordered) | Δ          | Significant? (CI excludes 0) |
|------------------------------------------------------|-------------------|------------------------|------------|------------------------------|
| Weighted commitment-ladder mean                      | *(illustrative)*  | *(illustrative)*       | *(illustr.)* | decided by `delta_ci[0] > 0` |
| Pushiness rate (guardrail)                           | *(illustrative)*  | *(illustrative)*       | must not regress | block on regression      |
| False-promise flags (guardrail)                      | *(illustrative)*  | *(illustrative)*       | must not regress | block on regression      |
| Qualification accuracy (v0 proxy)                    | *(illustrative)*  | *(illustrative)*       | held within ±0.02 | block on drop            |
| Sim-to-real divergence                               | n/a               | *(measured separately)*| —          | **pause if > 0.2**           |

**How to read a real run when it exists:** the dashboard's Experiment Lab (P6, `web/app/improve/lab`) renders this exact before/after — KPI delta, 95% CI, significance, guardrail pass/trip, and the declared one-dimension diff — from version-tagged episodes. The `compare_versions` result (`champion_kpi`, `challenger_kpi`, `delta`, `delta_ci`, `challenger_better`) is the data behind that view.

### What a promotion of this challenger would mean
If the reordered sequence produced a real, CI-separated lift on the weighted-ladder KPI with clean guardrails, held qualification accuracy, and sim-to-real divergence ≤ 0.2, and the diff is non-extreme (a discovery reorder is **not** a pricing/persona change), then per `evaluate_promotion` it would **auto-promote** with post-hoc review (covers AE5). The mechanism-visible story — "we let value land before price, and the qualified-ladder score rose, without pushing harder" — is exactly why discovery sequencing was chosen as the first dimension.

---

## Why this loop resists self-deception

- It optimizes against prospects whose buy decision is a **deterministic gate on hidden drivers**, not on fluent talk (FM-9).
- It requires the lift to **survive a bootstrap CI** (FM-10), with a **cross-family, order-robust judge** (FM-11).
- It **blocks any guardrail regression** even when the headline KPI rises (FM-13).
- It **pauses entirely** when sim and real diverge (FM-16) — the central-risk gate.
- It keeps every change to a **single attributable dimension** (FM-14) and every promotion in a **reversible lineage**.

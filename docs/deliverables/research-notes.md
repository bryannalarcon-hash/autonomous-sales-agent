# Research Notes: Precedents Behind the Design

*Purpose: document the published precedents the design draws on, so the architecture reads as grounded-in-prior-work rather than invented. Citations follow the brainstorm and `docs/belief-state-schema.md`; anything not directly verifiable from those sources is marked **[UNVERIFIED]**.*

> These notes summarize the precedent the project's own design documents cite (`docs/belief-state-schema.md` carries the full per-driver provenance scorecard and a reusable-artifacts table). They are not an independent literature review; where a claim rests on a citation we could not verify from within the repo, it is flagged.

---

## 1. The brain pattern — modular task-oriented dialogue (TOD) with a POMDP/belief-state framing

The agent is a **modular task-oriented dialogue system**: NLU/DST → policy → NLG, with a grounding (RAG) module and deterministic gates (`docs/belief-state-schema.md`, "Where this fits in the architecture"). The agent's "position" is its **belief state**, framed as a POMDP / Information-State-Update (ISU) information state — a *sufficient statistic* for the conversation so the agent carries structured state instead of re-dumping the transcript each turn. The belief is **factored** (each tracked variable is a state factor), maintained by Dialogue State Tracking as an approximate Bayes filter.

- **Slot-schema discipline:** the design adopts the **Schema-Guided Dialogue (SGD)** JSON slot format and **MultiWOZ 2.2** ontology discipline (no unbounded free-text slots). *(cited in belief-state-schema; SGD = github.com/google-research-datasets/dstc8-schema-guided-dialogue, MultiWOZ 2.2 = arXiv:2007.12720.)* **[UNVERIFIED]** — cited by the design doc, not independently re-checked here.

## 2. Hybrid DST — deterministic slots + LLM latent-driver deltas + derived velocity

The belief update **factorizes by layer** (`docs/belief-state-schema.md`, "The belief update"):
- **Slots:** near-monotonic accumulation via deterministic extraction (NLU) + confidence arbitration.
- **Latent drivers:** a graded **delta-update** from the valence of the user act (rubric-defined at cold-start, loop-tuned later).
- **Trends/velocity:** computed **deterministically from the logged trajectory** (a frame-stacking pattern) so the state stays Markov while the policy can see momentum — *no extra LLM latency, fully auditable.*

This hybrid (deterministic slot extraction + LLM for latent drivers + derived trends) is the design's chosen mechanism over a single all-LLM state call, and is implemented in `src/core/dst.py` / `src/core/belief_state.py`.

## 3. The six latent drivers (and their honest provenance)

The latent layer tracks four **drivers** the agent moves, one **outcome** it measures, one **process constraint** (`docs/belief-state-schema.md`, Layer B):

| Driver | Role | Provenance verdict (from the schema's scorecard) |
|---|---|---|
| `engagement` / `bail_risk` | process (walk-away signal) | **Grounded** — HERALD (arXiv:2106.00162), SalesRLAgent (arXiv:2503.23303) **[UNVERIFIED]** |
| `price_sensitivity` | driver (packaging/concession) | **Grounded as economics** — CraigslistBargain reservation price (arXiv:1808.09637), Deal-or-No-Deal (arXiv:1706.05125) **[UNVERIFIED]** |
| `purchase_intent` | **outcome, measured directly** | **Grounded as direct measure** — SalesLLM ordinal, SalesRLAgent conversion-prob **[UNVERIFIED]** |
| `need_intensity` | driver (urgency/value framing) | **Partial / by-analogy** — MIND willingness 1–10, SPIN **[UNVERIFIED]** |
| `trust` | driver (gates value + price) | **Partial (counseling, not sales)** — MENTAL-TRUST, rapport SEM **[UNVERIFIED]** |
| `urgency` (felt) | provisional driver | **Novel / weak** — only discrete proxies (BANT-Timing, TTM-Preparation) **[UNVERIFIED]** |

**Two precedent-driven corrections the design enforces (and the code respects):**
1. **`purchase_intent` is MEASURED DIRECTLY, not computed as a composite of the drivers** — all precedent measures it from text; the drivers are tracked separately and their predictive value is to be learned empirically.
2. **`urgency (felt)` is provisional** (weakest grounding) and may collapse into `need_intensity` + the `timeline` slot under factor analysis.

The schema's own "what we are inventing" list is explicit that the **joint six-driver factored belief** is the project's contribution (precedent tracks one hidden quantity, not six interacting), to be validated against the transcript corpus — and the U11 driver-validation gate (`src/loop/validation.py`) exists to do exactly that.

## 4. Sales methodology — SPIN + BANT

v0 discovery and rebuttal playbooks are grounded in **SPIN** (Situation → Problem → Implication → Need-payoff), supplemented by **BANT**-style qualification discipline (brainstorm "Key Decisions" / R35). SPIN's Implication→Need-payoff ordering is the substrate for the discovery-sequencing experiment; SPIN was chosen over Sandler/Challenger as a better fit for anxious-parent buyers and the anti-pushiness guardrail. **[UNVERIFIED]** as published efficacy; used here as a structuring heuristic, not an empirical claim. The discovery sequence is encoded in `champion_v0.yaml::playbooks.discovery_sequence`.

## 5. Gated neuro-symbolic policy + decoupled act interface

- **Gated policy:** an LLM proposal constrained by deterministic gates (`src/core/policy.py` + `src/core/gates.py`) — the "hand-authored EFSM/gates for safety vs. learnable policy for sequencing/phrasing" fork noted in the schema's open design forks.
- **Decoupled policy↔NLG act interface:** the design adopts the **CraigslistBargain** coarse dialogue-act layer pattern (decouple strategy from generation). *(cited: github.com/stanfordnlp/cocoa.)* **[UNVERIFIED]**
- **Resistance / persuasion taxonomies:** **RESPER's 7 resistance strategies** for `active_objection` / prospect-side acts, and **Persuasion-for-Good's 10 strategies** for agent-side persuasion acts — *adopt released taxonomies instead of inventing.* *(cited: github.com/americast/resper; aclanthology.org/P19-1566.)* **[UNVERIFIED]**

## 6. Evaluation — pairwise Arena-style LLM-judge + bootstrap CIs

The grading layer (`src/loop/grading.py`) uses **pairwise (Arena-style) LLM-judging** rather than absolute scoring, on the design's stated rationale that pairwise champion-vs-challenger judging is more stable than absolute scoring. The judge is a **different model family** than the agent (KTD5) and is **position-bias-mitigated** (both orders judged; disagreement → tie). Statistical separation uses a **percentile bootstrap CI** of the KPI delta; a lift counts only if the CI excludes 0. Precedent cited: **DeepEval Arena G-Eval (pairwise)** + **Promptfoo** for batch gating; bootstrap CIs + pinned judge config. **[UNVERIFIED]** as to specific tool APIs (the code implements the *pattern* with a stdlib bootstrap in `src/loop/_stats.py`, not the named libraries verbatim).

## 7. Honest hidden-utility simulators + self-play

- **Hidden-utility prospects:** each persona carries a hidden numeric utility (budget/need/trust/urgency/patience) that **deterministically gates purchase** (`src/sim/prospect.py`), after **CraigslistBargain's hidden reservation price** and **Deal-or-No-Deal**'s self-play negotiation. The agent cannot be persuaded past the gate by ungrounded talk (R31). **[UNVERIFIED]** as to the precedent papers; the *mechanism* is implemented and tested.
- **Persona schema:** parameterized after **SalesLLM**'s persona schema (buy-propensity / cooperative↔adversarial style / decision factors / difficulty tiers) — `src/sim/personas.py`. **[UNVERIFIED]**
- **Self-play loop scaffold:** **Deal-or-No-Deal** (RL self-play) and **ICL-AIF** (self-play + AI critic, arXiv:2305.10142) are the cited scaffolds for the batch loop. **[UNVERIFIED]**

## 8. Versioning / observability

Config-as-code in git defines an agent version as `{prompts, playbooks, thresholds}` + pinned `{model, architecture, kb_version}` (R12), implemented as a typed `AgentConfig` loaded from `src/config/versions/*.yaml`. The plan names **Langfuse (OSS, native LiveKit OTel)** + git for observability/lineage (KTD8); the **lineage** is implemented in the datastore (`VersionLineage`, `migrations/001_init.sql`). Langfuse wiring itself is named in the plan but is **[UNVERIFIED]** as integrated in the current build (no Langfuse client appears in `src/`).

---

## Provenance honesty (carried from the belief-state schema)

The schema is explicit that the project is **inventing**: the joint six-driver factored belief; `urgency (felt)` as a distinct continuous latent; `price_sensitivity` as a trait in a subscription/EdTech context; an EdTech-specific trust rubric + `student_buy_in`; and a persuasion POMDP whose policy acts on the full six-driver belief. Each is listed as "must validate against the transcript corpus." The research posture is therefore: **lift released artifacts where they exist, and flag the novel combinations as hypotheses, not facts.**

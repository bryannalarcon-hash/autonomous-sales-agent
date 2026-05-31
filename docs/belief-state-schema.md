# Agent Belief-State Schema

**Version:** v0.2 — *precedent-grounded; latent set still pending data validation*
**Project:** Autonomous AI Sales Agent (Nerdy / Varsity Tutors)
**Date:** 2026-05-31
**Scope:** Defines the variables the agent tracks to decide what to ask / say / do each turn.

---

## Where this fits in the architecture

- **Brain pattern:** modular **Task-Oriented Dialogue (TOD)** system — NLU/DST → policy → NLG, with a grounding (RAG) module and deterministic gates.
- **The agent's "position"** is its **belief state** (POMDP framing) ≡ **information state** (Information-State-Update / ISU framing).
- **The variables below are the `state variables`** (each a *factor*) of a **factored belief state**. Slots are *observed* state variables once filled; the prospect-psychology variables are *latent (hidden)* state variables. As fed to the policy they form the **state feature vector**.
- Maintained by **Dialogue State Tracking (DST)** via a per-turn **belief update** (a Bayes filter, approximated by an LLM/hybrid tracker). The belief state is a **sufficient statistic** for the conversation history — so we carry structured state instead of re-dumping the transcript each turn.

---

## Design principle (the inclusion test)

> **Track a variable only if you can name the policy decision it gates or ranks.**

The state splits into **three layers that update and gate differently**.

---

## Layer A — Discovery slots (facts the agent *gathers*)

Belief over the **user's goal**. Grounded in **BANT/MEDDIC** + tutoring specifics. **Well-precedented** (standard DST). Each carries `value` + `confidence`.

**Adopt:** the **Schema-Guided Dialogue (SGD)** JSON format verbatim — each slot = `{name, NL description, is_categorical, possible_values}`; per-turn state = `{active_intent, requested_slots, slot_values}`. MultiWOZ 2.2 for ontology discipline (no unbounded free-text slots).

| Slot | Type | Why / decision it drives | Init |
|---|---|---|---|
| `grade_level` | required | Curriculum, tutor match, pricing tier | CRM/form, else ask |
| `subject(s)` | required | Tutor/program match | form, else ask |
| `goal/outcome` | required | The "why" — sell value, define success | ask early |
| `timeline` (test/term date) | required | Program length + urgency close | ask |
| `budget` | required | Qualify + package selection; high-stakes slot | ask late (gated) |
| `decision_maker` | required | Can't close with someone who can't say yes | infer/ask |
| `pain_point` | leading | Emotional driver for objections + close | infer |
| `prior_tutoring` | leading | Shapes efficacy objections + differentiation | infer/ask |
| `student_buy_in` | leading | Predicts success + how to pitch *(domain-novel — no precedent found)* | infer |

---

## Layer B — Latent prospect state (the agent *infers* these)

A small causal model of "will they buy." Four **drivers** the agent moves, one **outcome** it measures, one **process constraint**.

| Variable | Role | Why / decision it drives | Inferred from |
|---|---|---|---|
| `trust` | driver | Credibility/rapport — **gates value & price talk** | tone, pushback, acknowledgments |
| `need_intensity` | driver | Felt problem severity — fuels urgency & value framing | pain language, stakes |
| `price_sensitivity` | driver | **Packaging/discount strategy; concession-escalation trigger** | reaction to cost, comparison shopping |
| `urgency` (felt) | driver *(provisional)* | Close tempo + scarcity framing | deadline pressure cues |
| `purchase_intent` / conversion-prob | **outcome (measured directly)** | **Gates pivot-to-close and qualify/disqualify**; the KPI proxy | buying signals (measured, not computed) |
| `engagement` / `bail_risk` | process | **Re-hook vs wrap-up**; pacing; the *walk-away* signal | turn length, latency, disengagement cues |

**Policy framing:** **move the lowest driver currently gating `purchase_intent`, before `bail_risk` runs out.**

**Two corrections from the precedent review (do NOT regress):**
1. **`purchase_intent` is MEASURED DIRECTLY, not computed as a composite of the drivers.** All precedent (SalesLLM ordinal classifier, SalesRLAgent continuous conversion-prob) measures it from text. Track the drivers separately and **learn empirically** whether they predict intent (regression/factor analysis) — do not hardcode `intent = f(drivers)`.
2. **`urgency (felt)` is provisional — weakest grounding.** May collapse into `need_intensity` + the `timeline` slot under factor analysis. Validate or fold; do not treat as load-bearing.

**Symmetry with the simulator:** the sim prospect carries the *true* versions as hidden ground truth; the agent holds beliefs over them → clean threshold calibration. **Parameterize the sim prospect with SalesLLM's persona schema:** buy-propensity (0.8 easy → 0.05 adversarial), cooperative↔adversarial style, 10 decision factors, difficulty tiers.

---

## Layer C — Dialogue-control meta (bookkeeping)

**Adopt released act/objection taxonomies instead of inventing:**
- **RESPER's 7 resistance strategies** (Source Derogation, Counter Argument, Personal Choice, Information Inquiry, Self Pity, Hesitance, Self Assertion) → `active_objection` / prospect-side `last_user_act`. *Released corpus + code, annotated across persuasion AND negotiation.*
- **Persuasion-for-Good's 10 strategies** → agent-side persuasion acts (system dialogue acts).
- **CraigslistBargain coarse dialogue-act layer** (`propose(price=…)` + rule parser) → the **policy↔NLG interface** (decouple strategy from generation — already our design).

| Variable | Why / decision it drives |
|---|---|
| `stage` | EFSM skeleton: greeting → discovery → objection → pitch → close → escalate |
| `active_objection` (RESPER type + count) | Gate: must clear before advancing to close |
| `last_user_act` (NLU label) | Routes inform / question / objection / disqualify / non-answer |
| `decision_confidence` | **Triggers escalation on low-confidence turns** |
| `open_question` / `unanswered_by_us` | Detect dodges; answer their question before pivoting |
| `turn_count` | Pairs with `bail_risk` for pacing |
| *(derived)* `*_velocity` | Trend/momentum factors computed from the logged belief trajectory; fed to the policy. Keeps state Markov while letting policy see direction-of-travel |

---

## The belief update (`belief_state₁ → belief_state₂`)

Formal name: the **belief update** (recursive Bayes filter). Approximated by the DST. **Factorizes by layer:**
- **Slots:** near-monotonic accumulation — deterministic extraction (NLU) → merge with confidence arbitration.
- **Latent drivers:** graded **delta-update** from the valence of the user act (rubric-defined at cold-start; loop-tuned later).
- **Meta:** counters + flag set/clear.

**Trends:** the LLM tracker emits *levels* only; **velocity/trend factors are computed deterministically from the logged trajectory** and added to the state vector (frame-stacking pattern) so the state stays Markov and the policy can condition on momentum. No extra LLM latency; fully auditable.

**Implementation:** one LLM call (or hybrid: deterministic slot extraction + LLM for latent drivers) taking `(prior state JSON, last agent act, new utterance)` → new state JSON, **with a one-line rationale per changed driver** (also feeds the decision-log/dashboard).

---

## Provenance scorecard (what's grounded vs invented)

| Driver | Verdict | Precedent |
|---|---|---|
| `engagement`/`bail_risk` | **Grounded** | HERALD (arXiv:2106.00162); SalesRLAgent (arXiv:2503.23303) |
| `price_sensitivity` | **Grounded as economics / partial as trait** | CraigslistBargain reservation price (arXiv:1808.09637); Deal-or-No-Deal (arXiv:1706.05125); WTP estimation |
| `purchase_intent` | **Grounded as direct measure / novel as composite** | SalesLLM ordinal (arXiv:2604.07054); SalesRLAgent conversion-prob (arXiv:2503.23303) |
| `need_intensity` | **Partial / by-analogy** | MIND willingness 1–10 (arXiv:2603.21696); TTM decisional-balance; SPIN |
| `trust` | **Partial (counseling, not sales)** | MENTAL-TRUST (arXiv:2501.03064); rapport SEM (arXiv:1703.00549) |
| `urgency` (felt) | **Novel / weak** | only discrete proxies (BANT-Timing, TTM-Preparation) |

**What we are inventing (must validate against the transcript corpus):**
1. The **joint six-driver factored belief** — precedent tracks one hidden quantity, not six interacting. *Our contribution; validate separability.*
2. `purchase_intent` as a **composite** (resolved above: measure directly instead).
3. `urgency (felt)` as a **distinct continuous latent**.
4. `price_sensitivity` as a **trait** in a subscription/EdTech context (vs. a reservation price in haggling).
5. **EdTech-specific trust rubric** + `student_buy_in` (learner-as-distinct-stakeholder).
6. A **persuasion/sales POMDP whose policy acts on the full six-driver belief** (closest exemplar SalesRLAgent uses a single conversion value).

---

## Reusable artifacts (lift, don't reinvent)

| To initialize… | Artifact | Link |
|---|---|---|
| Slot schema format | SGD JSON | github.com/google-research-datasets/dstc8-schema-guided-dialogue |
| Belief-update / ontology discipline | MultiWOZ 2.2 | arXiv:2007.12720 |
| Intent & need rubrics | MIND 1–10 bands; SalesLLM 5-level ordinal | arXiv:2603.21696; arXiv:2604.07054 |
| Simulated-prospect personas | SalesLLM persona schema (propensity/style/10-factors/difficulty) | arXiv:2604.07054 |
| Engagement/bail signals | SalesRLAgent features; HERALD | arXiv:2503.23303; arXiv:2106.00162 |
| Resistance / objection taxonomy | RESPER (7 strategies, corpus+code) | github.com/americast/resper |
| Agent persuasion acts | Persuasion-for-Good (10 strategies) | aclanthology.org/P19-1566 + ConvoKit |
| Policy↔NLG act interface | CraigslistBargain dialogue-act parser | github.com/stanfordnlp/cocoa |
| Self-play loop scaffold | Deal-or-No-Deal (RL self-play); ICL-AIF (self-play + AI critic) | github.com/facebookresearch/end-to-end-negotiator; arXiv:2305.10142 |

---

## Validation plan (before locking the schema)

1. **Pilot-annotate** a slice of the provided PII-substituted transcripts with the six drivers, using MIND 1–10 bands + SalesLLM ordinal intent as starting scales.
2. **Inter-annotator agreement** — are these drivers reliably ratable by humans/LLMs?
3. **Factor analysis** — which drivers are separable latents vs. collinear/collapsible (resolves urgency + composite-intent + trust-transfer).
4. **Threshold calibration** against the simulator (operating point that maximizes sim close-rate).
5. Keep only the drivers that carry signal; expect 1–2 to drop.

---

## Open design forks (next decisions)

- **Belief-update mechanism:** single LLM call for the whole factored state vs. hybrid (deterministic slot extraction + LLM for latent drivers). *Leaning hybrid.*
- **Gate boundary:** hand-authored EFSM/gates (escalation, must-clear-objection, safety) vs. learnable policy (sequencing, phrasing, rebuttal choice).
- **Next-question selection:** LLM-judgment vs. information-gain (EVI) ranking.
- **`purchase_intent` measurement:** ordinal classifier (SalesLLM-style) vs. continuous conversion-probability (SalesRLAgent-style).

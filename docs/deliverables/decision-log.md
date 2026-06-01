# Decision Log

*Purpose: record the key design decisions, the reasoning behind each, and — per the brainstorm's honesty rule — flag every cold-start prior and gate threshold as a GUESS pending sim calibration, not a measured optimum.*

---

## 1. Architectural decisions

### D-1. Transport-agnostic `respond()` core (the load-bearing decision)
**Decision:** The brain is a pure async `respond(belief, history, user_msg, llm, config) -> (decision, reply, new_belief)` in `src/core/` with **no LiveKit types**. Voice (`src/voice/`) and headless text self-play (`src/sim/selfplay.py`) are thin adapters that both call the same `respond()`.
**Why:** This is what makes the project honest end-to-end — *what is optimized in text is exactly what ships in voice*. It is verified by a parity test (a scripted voice session produces the same decisions as the equivalent text session — `tests/integration/test_voice_adapter.py`, and the sim2real harness's `voice_noise=0` parity floor).

### D-2. Neuro-symbolic policy: LLM proposes, deterministic gates decide
**Decision:** The LLM proposes a next act; deterministic gates (`src/core/gates.py`) have **final say**, including escalation checked first AND last in `apply_gates()`.
**Why:** Safety- and integrity-critical behavior (don't push a walker, don't quote price before trust, don't improvise a concession) must not depend on an LLM's mood. Gates are pure functions, unit-tested.

### D-3. Close-triggered commitment in the simulator (integrity backbone)
**Decision:** A synthetic prospect commits **only** in response to an agent `attempt_close`, at the offered tier, and only if its true hidden drivers clear that tier — never spontaneously.
**Why / provenance:** This was a deliberate correction. Git commit `c2ceb5b` — *"fix: make prospect commitment close-triggered, not spontaneous (U7/U8)"* — changed the sim so fluent talk alone can never produce a sale. It makes the agent's close *timing* and *tier choice* genuinely matter and prevents the loop from rewarding a glib agent. Implemented in `src/sim/prospect.py::buy_gate`.

### D-4. One Postgres store (JSONB + pgvector) for everything
**Decision:** Episodes, per-lead memory, version lineage, **and** KB vectors all live in one Postgres+pgvector instance (`migrations/001_init.sql`, `002_kb.sql`, `003_experiments.sql`).
**Why:** Single store, transactional version lineage, one ops surface for a solo operator. (See stack comparison D-9.)

### D-5. Unified episode schema for real + sim calls
**Decision:** Real and simulated calls normalize to one `Episode` shape (`src/memory/schema.py`), tagged with `version`, `kb_version`, `channel ∈ {sim, text, voice}`.
**Why:** The dashboard and the grading loop read one contract; a sim episode and a voice episode are queryable through one interface (R26).

---

## 2. Cold-start priors and gate thresholds — **ALL FLAGGED AS GUESSES**

> **Honesty rule (from the brainstorm):** v0 thresholds are cold-start *priors*, logged as **guesses pending sim calibration**. None of the numbers below is an empirically-optimized value. They are documented in code as such (`src/core/gates.py` `_DEFAULTS` comment: *"these are cold-start priors pending sim calibration"*; `champion_v0.yaml` comment: *"Cold-start priors (logged as guesses pending sim calibration)"*).

### D-6. Gate thresholds (the live values)

| Threshold | Value in `champion_v0.yaml` | Fallback in `gates._DEFAULTS` | What it gates | Status |
|---|---|---|---|---|
| `trust_gate_open_price` | `0.5` | `0.5` | `trust` level that opens price/budget talk (`price_gate`) | **GUESS** |
| `pushiness_cap` | `0.7` | `0.8` | `bail_risk` level above which further pressure is forbidden | **GUESS** |
| `pushiness_pressure_count_cap` | `2` | `2` | consecutive pressure acts before backing off | **GUESS** |
| `escalate_low_confidence_turns` | `2` | `2` | consecutive low-confidence turns → escalate | **GUESS** |
| `low_confidence_level` | `0.4` | `0.4` | `decision_confidence` below this counts as a low-confidence turn | **GUESS** |
| `max_concession_band` | `0.15` | `0.15` | discount fraction beyond which a concession is EXTREME → escalate | **GUESS** |
| `discovery_slots_required` | `4` | `4` | non-budget discovery slots filled that also opens the price gate | **GUESS** |

> **Note on `pushiness_cap`:** the shipped config value (`0.7`) is *stricter* than the code default (`0.8`). The config wins (gates read config first, fall back to `_DEFAULTS` only for omitted keys), so the live agent backs off pressure at `bail_risk ≥ 0.7`. Both are guesses; the discrepancy is intentional headroom, not a measured tuning. This is the kind of value the improvement loop (or sim calibration) is expected to set properly.

### D-7. Persona priors (`champion_v0.yaml`)
- **Persona:** Alex, "learning advisor", style `warm-consultative` (empathetic, educational, low-pressure — R3). The prompt/playbook bodies in `champion_v0.yaml` are placeholders at U1; real prompt content is authored in the brain/KB units.
- **Discovery sequence (SPIN-grounded, R35):** `[grade_level, subject, goal, timeline, prior_tutoring, budget]` — budget intentionally last (trust-gated). This ordering is the substrate for the discovery-sequencing experiment. **GUESS** pending the experiment.

### D-8. Simulator buy-gate thresholds (`src/sim/prospect.py::_TIER_THRESHOLDS`)
Per-tier thresholds on the true hidden drivers (e.g. enrollment requires `need ≥ 0.7, trust ≥ 0.7, purchase_intent ≥ 0.7, budget ≥ 0.6`; callback is the lowest rung with `purchase_intent ≥ 0.3, trust ≥ 0.25` and no budget gate). **GUESS** — these are the sim's cold-start calibration; per R36 they must be validated against real commit/walk outcomes before promotions are trusted. The unqualified fraction defaults to **~25%** (`sample_population(unqualified_fraction=0.25)`), within the brainstorm's stated 20–30% band.

---

## 3. Tech-stack decisions (what was picked, and the closest alternative)

The full competitive comparisons live in the plan's "Key Technical Decisions" section. Summary of what was **locked and built**:

| Layer | Picked | Closest alternative considered | Deciding tradeoff |
|---|---|---|---|
| Language/runtime | **Python** | (no real peer given the LiveKit lock) | LiveKit Agents is Python-first; chart skipped per policy |
| LLM access | **OpenRouter (single key → many families)** | direct Anthropic/OpenAI keys | one key routes agent (Claude) + sim/judge (a different family, KTD5) without per-vendor wiring (`src/core/llm.py::OpenRouterClient`) |
| Datastore | **Postgres + pgvector** | SQLite+Chroma; Mongo+Atlas | single store, transactional lineage, one ops surface (D-4) |
| Vector index | **pgvector** (folds into the DB) | Chroma (simplest standalone); Qdrant (scales hardest) | avoid a second datastore at v0 scale |
| Embeddings | **local sentence-transformers** (`BAAI/bge-small-en-v1.5`, 384-dim) | hosted embedding APIs | no key, no network, deterministic test fake (`FakeEmbedder`); 384-dim matches `migrations/002_kb.sql` |
| Voice transport | **LiveKit** (self-hosted, locked) | Pipecat; managed speech-to-speech | control of the reasoning seam; same brain in text + voice |
| STT + TTS | **ElevenLabs (Scribe v2 Realtime STT + TTS)** | Deepgram (STT); Cartesia (TTS) | see D-10 below — note this **diverges from the plan's KTD6** |
| Eval/judge | **pairwise LLM-judge + bootstrap CIs** (cross-family) | absolute G-Eval scoring | pairwise is more stable than absolute scoring; CIs resist over-claiming |
| API backend | **FastAPI** | Flask; Django | async-native (matches the async brain/loop), injectable deps for offline tests (`src/api/server.py`) |
| Front-end | **Next.js 14 (App Router) + React 18 + TypeScript + Tailwind** | Streamlit; Plotly Dash | a live-call monitor needs WebRTC + websocket; Streamlit/Dash are poll-based and have no voice path |

---

## 4. Decisions that DIVERGE from the plan/brainstorm (flagged for accuracy)

### D-9. STT vendor: built on ElevenLabs Scribe, not Deepgram
**What the plan said (KTD6):** Deepgram Nova-3 for STT + ElevenLabs Turbo for TTS.
**What was built:** `src/voice/agent.py` sets `STT_MODEL_ID = "scribe_v2_realtime"` (ElevenLabs Scribe) and `.env.example` documents STT as *"handled by ElevenLabs Scribe v2 Realtime ... Deepgram is an OPTIONAL drop-in fallback only if live turn-taking lags."* So the implemented default consolidates STT+TTS on ElevenLabs, with Deepgram demoted to an optional fallback.
**Why this is fine / why it matters:** consolidating on one speech vendor reduces integration surface for the demo; the plan's chart noted these latency numbers disagree 3–4× across sources and must be re-benchmarked from the egress region before locking anyway, so the vendor choice was never load-bearing for any *claim*. The README still says "Deepgram (STT)" — that line is **stale** relative to the build (see the limitations memo).

### D-10. Agent model id pinned in voice glue
`src/voice/agent.py` hard-codes `AGENT_MODEL = "anthropic/claude-3.5-sonnet"` as the live-wiring default, while `.env.example` sets `AGENT_MODEL=anthropic/claude-sonnet-4.5`. The env value wins for the actual `OpenRouterClient` (`src/core/llm.py` reads `AGENT_MODEL` from env); the constant in the voice glue is only a fallback for the STT/TTS persona surface. Worth noting so the two don't read as a contradiction. The judge model (`JUDGE_MODEL=openai/gpt-4o`) is a different family, satisfying KTD5.

---

## 5. Open decisions deferred to a real run

These were explicitly left for implementation-against-data and are **not yet resolved** (they need a real model batch and/or the transcript corpus):
- The concrete statistical test parameters (the gate uses a percentile bootstrap CI; resample count and alpha are caller-set, default `n_resamples=1000, alpha=0.05`).
- Frozen-set size for adequate power vs. single-diff effect size.
- The R39 `min_sample` / `min_agreement` concretes for headline reporting.
- The sim buy-gate calibration against real commit/walk outcomes (R36).
- STT turn-detection choice (ElevenLabs Scribe vs. a local turn detector) on real sales-call audio.

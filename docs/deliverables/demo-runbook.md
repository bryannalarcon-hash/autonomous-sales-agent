# Demo Runbook

*Purpose: exact, ordered commands to bring the whole system up locally — tests, API, a self-play batch + one improvement round, and the web console — plus the demo walkthrough. Every command here was checked against the repo; verified facts are called out inline.*

> **Conventions.** All backend commands run from the repo root (`/home/bryann/gauntlet/auto-sales-agent` in this environment) with `PYTHONPATH=.` so `src` is importable. Python 3.10+. The web app runs from `web/`. The shell's working directory resets between steps in some harnesses — prefer absolute paths or re-`cd` as shown.

---

## 0. Prerequisites

- **Python 3.10+** (the suite runs on 3.10 here).
- **Docker** (for the Postgres+pgvector container) — *only needed for the DB-backed paths; the test suite, the API demo, and the self-play loop all run DB-free with the injected fakes shown below.*
- **Node 18+ / npm** (for the web console).

---

## 1. Environment

```bash
cp .env.example .env
```

Then edit `.env`. For the **text** demo and the test suite you need **nothing real** (the API injects mocks offline). For a **real model run** set `OPENROUTER_API_KEY` (+ optionally `AGENT_MODEL` / `SIM_MODEL` / `JUDGE_MODEL`, which already default to a cross-family split: agent = `anthropic/claude-sonnet-4.5`, sim/judge = `openai/gpt-4o`). For **live voice** also set `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, and `ELEVENLABS_API_KEY`.

> `.env` is gitignored (verified) — never commit it.

---

## 2. Start Postgres + pgvector (optional for the text demo)

```bash
docker compose up -d db
```

This launches `pgvector/pgvector:pg16` exposing Postgres on host port **5434** (container 5432), DB `sales_agent`, user/pass `sales`/`sales` (`docker-compose.yml`). The default `DATABASE_URL` in `.env.example` already points at `postgresql://sales:sales@localhost:5434/sales_agent`.

> Skip this step entirely if you only want the test suite, the text demo, or the self-play loop — those run with in-memory / mock stores.

---

## 3. Apply migrations (only if using the DB)

Apply the three migrations **in order** (`migrations/001_init.sql`, `002_kb.sql`, `003_experiments.sql` — verified present and idempotent / `IF NOT EXISTS`):

```bash
for f in 001_init.sql 002_kb.sql 003_experiments.sql; do
  docker compose exec -T db psql -U sales -d sales_agent < "migrations/$f"
done
```

- `001_init.sql` — pgvector extension + `episode`, `lead`, `version_lineage`, `escalation_log`.
- `002_kb.sql` — `kb_chunk` (vector(384), kb_version-scoped).
- `003_experiments.sql` — experiment records for the Improve dashboard.

---

## 4. Install Python deps

```bash
pip install -e .
```

Installs the backend incl. the FastAPI/uvicorn API service (pydantic, pyyaml, asyncpg, pgvector, python-dotenv, anthropic, httpx, fastapi, uvicorn — see `pyproject.toml`). Add extras as needed: `pip install -e '.[dev]'` (pytest/ruff), `'.[voice]'` (LiveKit + ElevenLabs/silero/openai plugins for the live voice worker), `'.[rag]'` (the sentence-transformers embedder for live grounded RAG — the test suite uses a fake embedder and does **not** need it), `'.[sim]'` (the OpenAI client). For the full live product: `pip install -e '.[voice,rag]'`.

---

## 5. Run the test suite (verified: 224 passed)

```bash
PYTHONPATH=. python3 -m pytest tests -q
```

**Verified output:** `224 passed in ~5s`. Breakdown (verified by collection): **unit 93** (`tests/unit/`: config, consent, dst, llm, policy, prospect), **integration 93** (`tests/integration/`: store, respond, kb, memory, selfplay, grading, loop, validation, voice_adapter, escalation, sim2real), **e2e 38** (`tests/e2e/`: demo_call, operate, improve). The suite is fully offline (mock LLMs + fake embedder + in-memory stores).

---

## 6. Start the API (verified boots + serves /api/health)

The FastAPI app exposes a module-level `app = create_app()` at the bottom of `src/api/server.py`, so the uvicorn factory invocation is:

```bash
PYTHONPATH=. python3 -m uvicorn src.api.server:app --host 0.0.0.0 --port 8000
```

**Verified:** the server boots and `GET /api/health` returns `{"status":"ok"}` (200). Verified registered routes include: `/api/consent/start`, `/api/consent/respond`, `/api/chat`, `/api/livekit/token`, `/api/session/{id}`, `/api/health`, and the dashboard read API (`/api/episodes`, `/api/episodes/{id}`, `/api/kpis`, `/api/escalations`, `/api/live`, `/api/experiments`, `/api/approvals` (+approve/reject), `/api/kb`, `/api/playbook`, `/api/versions` (+rollback)), plus `/docs`.

> Quick boot check (optional):
> ```bash
> curl -s http://localhost:8000/api/health      # -> {"status":"ok"}
> ```

Notes:
- The text demo works with **no** LLM key only if you inject a mock factory; in normal operation `create_app()` builds an `OpenRouterClient` lazily on first chat turn (so it needs `OPENROUTER_API_KEY`). For an offline demo, pass `llm_client_factory=` to `create_app()`.
- `/api/chat` returns **409** until consent is satisfied; `/api/livekit/token` returns **503** (never a crash) when LiveKit creds are absent.

---

## 7. Run a self-play batch + one improvement round (verified, DB-free)

There is **no packaged CLI** in this build — self-play and the improvement loop are driven through their Python entrypoints. The real entrypoints are `src/sim/selfplay.py::run_batch` / `run_episode` and `src/loop/promotion.py::run_improvement_round` / `evaluate_promotion`. Drive them with a short script. The snippet below was **run successfully in this environment** with mock LLMs (no network, no DB):

```bash
PYTHONPATH=. python3 - <<'PY'
import asyncio
from src.core.llm import MockLLMClient
from src.config.settings import load_config
from src.sim import selfplay
from src.sim.personas import sample_population

# Mock LLMs keep this offline (swap for OpenRouterClient() factories for a real model batch).
agent_factory    = lambda: MockLLMClient(lambda m, **o: '{"drivers": {}, "slots": {}}')
prospect_factory = lambda: MockLLMClient(lambda m, **o: '{"utterance": "Tell me more.", "deltas": {"trust": 0.1}}')

cfg = load_config("champion_v0")
pop = sample_population(8, seed=1)                      # ~25% seeded unqualified
eps = asyncio.run(selfplay.run_batch(
    pop, agent_factory, prospect_factory, cfg,
    base_seed=10, cohort="demo", noise=0.3, max_turns=8, persist=False))
print("episodes:", len(eps), "outcomes:", selfplay.outcome_distribution(eps))
PY
```

**Verified:** runs 8 episodes and prints an outcome distribution. *(With trivial mock LLMs the prospects walk — the harness mechanics are what this verifies; a real model run is needed for a sales outcome.)*

For a **real model batch**, set `OPENROUTER_API_KEY` in `.env` and replace the two factories with `from src.core.llm import OpenRouterClient` and `agent_factory = OpenRouterClient` (the sim/judge default to a different family per KTD5). Then run `run_improvement_round(...)` from `src/loop/promotion.py` to generate a challenger, run the frozen-held-out experiment, and gate promotion.

A packaged **real-LLM smoke test** is provided — it wires real `OpenRouterClient`s (agent `AGENT_MODEL`, prospect `SIM_MODEL`), runs a tiny champion-vs-challenger pair, grades it, and prints an honest summary + the live call count:

```bash
PYTHONPATH=. python3 scripts/smoke_real_llm.py --callers 2 --max-turns 8
```

**Verified (this environment):** it executed end-to-end on `anthropic/claude-sonnet-4.5` (agent) + `openai/gpt-4o` (prospect) — **72 live calls, ~96s, both arms graded.** At that intentionally tiny size both short calls ended in `walked` (KPI `0.0`) — proof the live-model pipeline *runs + grades*, **not** a conversion/lift result (raise `--callers` and `--max-turns 24` for a real batch; the default budget completes discovery→pitch→close).

The **promotion gate** itself is a pure function and was verified across all four branches in this environment (no self-play needed):

```bash
PYTHONPATH=. python3 - <<'PY'
from src.loop.grading import ComparisonResult, GuardrailReport
from src.loop.experiment import ExperimentResult
from src.loop.promotion import evaluate_promotion
g = lambda: GuardrailReport(0.0, [], 0, True, False, 10)
cmp = ComparisonResult(2.0, 2.6, 0.6, (1.8,2.2), (2.4,2.8), (0.3,0.9), True, 20, 20)
exp = ExperimentResult("playbooks.discovery_sequence", cmp, g(), g(), 0.90, 0.91, [], [])
print("non-extreme:", evaluate_promotion(exp, is_extreme=False, divergence=0.05).status)  # promoted
print("extreme:    ", evaluate_promotion(exp, is_extreme=True,  divergence=0.05).status)  # pending_approval
print("divergent:  ", evaluate_promotion(exp, is_extreme=False, divergence=0.35).status)  # paused
PY
```

**Verified output:** `promoted`, `pending_approval`, `paused` (and a no-significant-lift input returns `rejected`).

---

## 8. Start the web console

```bash
cd web
npm install
cp .env.local.example .env.local      # NEXT_PUBLIC_API_BASE defaults to http://localhost:8000
npm run dev                           # http://localhost:3000  (root redirects to /demo)
```

Stack (verified `web/package.json`): **Next.js 14 App Router + React 18 + TypeScript + Tailwind**, LiveKit JS SDK (`livekit-client`, `@livekit/components-react`). The backend base URL is `NEXT_PUBLIC_API_BASE` (default `http://localhost:8000`) — only `NEXT_PUBLIC_*` vars reach the browser; the LiveKit token is minted server-side.

> Verified route files exist for every page below under `web/app/`.

---

## 9. The demo walkthrough

### `/demo` — prospect-facing call (consent → text/voice)
1. On load, the page calls `POST /api/consent/start` and shows the **AI disclosure + recording notice**.
2. The prospect **acknowledges AI**, **allows/declines recording**, and flags **whether a minor is involved**; `POST /api/consent/respond` resolves a state:
   - `ready` → call enabled (recorded); `unrecorded` → enabled with a "not recorded" banner; `need_parental` → parental-consent step; `ended` → polite end. Text/voice stay **disabled** until `ready`/`unrecorded`.
3. **Text console** — `POST /api/chat {session_id, text}` renders the reply. The internal `decision_act` label is hidden behind a debug toggle (never shown to a prospect). **Works without any voice creds.**
4. **Voice** — "Start voice call" fetches `POST /api/livekit/token` and connects. If creds are absent the endpoint returns **503** and the UI shows a "voice unavailable — use text console" fallback (no crash). **Live voice requires LiveKit creds (`LIVEKIT_*`) + `ELEVENLABS_API_KEY`.**

### `/operate` — live monitor, KPIs, escalations (operator)
- **Live** (`/operate/live`) — live-call monitor with the prioritized belief signals (trust + bail-risk gauges, stage, current decision + rationale + confidence, escalation strip), talk/listen meter, and a **Take over** control.
- **Calls** (`/operate/calls`) + **Call Review** (`/operate/review/[id]`) — browse/filter calls; replay how the belief state evolved turn-by-turn (scrubber over the recorded per-turn belief log).
- **KPI Views** (`/operate/kpi`) — weighted-ladder score, **same-call enrollment shown as a distinct metric**, qualification accuracy, guardrail status, ladder distribution, objection recovery, per-archetype conversion, per-stage dwell; filter by version + cohort.
- **Escalations** (`/operate/escalations`) — the deferred-items review queue with lifecycle states (unreviewed/reviewed/resolved).

Read endpoints are injectable (`read_store=`); for a DB-free dashboard demo, pass a seeded in-memory store to `create_app()`.

### `/improve` — experiment lab, approvals, KB editor, rollback (operator)
- **Experiment Lab** (`/improve/lab`) — champion-vs-challenger **before/after** (KPI delta + 95% CI + significance + guardrail status + the declared one-dimension diff) with running/passed/blocked/promoted states. This is where the **discovery-sequencing before/after** is read.
- **Approvals** (`/improve/approvals`) — the human-approval queue for **extreme** promotions (pricing/persona); approve-with-override or reject (logged to version history).
- **KB / Playbook editor** (`/improve/kb`) — edits create **draft challengers**, never a direct champion mutation.
- **Versions** (`/improve/versions`) — version lineage + **one-click rollback** (`POST /api/versions/{version}/rollback`).

---

## 10. Live VOICE — running the worker (and where it differs from text)

- **Text** (`/demo` text console, `/api/chat`) works against the running API with **no voice creds**.
- **Live voice** additionally requires **LiveKit credentials** on the backend (`LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`) and `ELEVENLABS_API_KEY` for STT (Scribe v2 Realtime) + TTS. Without them, `/api/livekit/token` returns 503 and the UI falls back to text. Real turn-taking / barge-in / ASR recovery are verified **manually at demo time** (not in CI).

### 10.1 Install the voice plugins (one-time)

The runnable worker needs the LiveKit plugin set (NOT pulled in by `pip install -e .`):

```bash
pip install --user livekit-plugins-elevenlabs livekit-plugins-silero livekit-plugins-openai
python3 -c "from livekit.plugins import elevenlabs, silero, openai; print('plugins OK')"
```

**Verified (this environment):** installed `livekit-plugins-{elevenlabs,silero,openai}` **1.5.15** (matching `livekit-agents` 1.5.15) and they import cleanly. These plugins are isolated to the worker — `PYTHONPATH=. python3 -m pytest tests -q` still reports **241 passed** (the worker's heavy imports never leak into `src/core` or the suite).

### 10.2 Start the voice worker

The worker (`src/voice/worker.py`) registers with LiveKit Cloud and is **auto-dispatched** into the caller's `/demo` room. Run it from the repo root in its own shell:

```bash
PYTHONPATH=. python3 -m src.voice.worker dev
```

What it does on boot: loads `.env`, loads config `champion_v0` (override with `WORKER_CONFIG_VERSION`), builds **our brain** via `build_voice_agent(...)` — a real `OpenRouterClient(model=AGENT_MODEL)` + a real `SentenceTransformerEmbedder` + `build_live_retrieve_hook(...)` so voice answers are **grounded in the ingested KB** — wires ElevenLabs Scribe v2 realtime STT + ElevenLabs TTS + Silero VAD, and registers with LiveKit Cloud.

**Verified (this environment) — it registers with LiveKit Cloud:**

```
INFO  livekit.agents  starting worker  {"version": "1.5.15", "rtc-version": "1.1.8"}
INFO  livekit.agents  registered worker  {"agent_name": "", "id": "AW_********", "url": "wss://sales-agent-********.livekit.cloud", "region": "US Central", "protocol": 17}
```

(Worker URL/id masked above.) **The actual audio loop — STT → brain → TTS → audible reply + live transcript — is a MANUAL check; it needs a real caller in `/demo` and cannot be CI-tested.**

### 10.3 End-to-end demo flow (worker + web)

1. Backend API up (§6) + the **voice worker** running (§10.2), web console up (§8).
2. `/demo` → satisfy consent (`ready`/`unrecorded`) → **Start voice call** → browser calls `POST /api/livekit/token` (room `demo-<session_id>`) and joins it.
3. The running worker auto-dispatches into that room: STT transcribes the caller → the `VoiceSession`/`respond()` decides + grounds (same brain as text) → ElevenLabs TTS speaks → `VoiceRoom.tsx` shows the live transcript and plays the agent audio.
4. On hang-up / room close the worker persists the voice **Episode** (channel `"voice"`) via the same `src/api/persistence.persist_call_end` path, so the call appears in `/operate`.

> **Cost note:** every live voice call **spends ElevenLabs credit** (STT + TTS) **and LiveKit Cloud minutes**, plus OpenRouter tokens for each agent turn. Stop the worker (`Ctrl-C`) when not demoing.

---

## Command checklist (copy/paste order)

```bash
# backend
cp .env.example .env                                            # edit keys as needed
docker compose up -d db                                         # optional (DB paths only)
for f in 001_init.sql 002_kb.sql 003_experiments.sql; do \
  docker compose exec -T db psql -U sales -d sales_agent < "migrations/$f"; done   # optional
pip install -e '.[dev]'
PYTHONPATH=. python3 -m pytest tests -q                         # -> 224 passed
PYTHONPATH=. python3 -m uvicorn src.api.server:app --port 8000  # -> /api/health 200

# web (new shell)
cd web && npm install && cp .env.local.example .env.local && npm run dev   # -> :3000 /demo
```

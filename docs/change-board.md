# Change Board

> Living kanban for changes after the first build. The planner owns **To Do**;
> the implementing agent owns **In Progress** and **Done**. Items are appended
> here as they're discussed. **Implementer prompts are produced on request** — an
> item sitting in To Do is scoped, not yet authorized to build.

---

## Workflow (read this first)

**What this file is.** The single source of truth for *what changes* and *who is doing what right now*. It sits on top of the project's binding docs — it does not replace them:

- `docs/deliverables/demo-runbook.md` — how to run the stack (API :8000, dashboard :3000, voice worker, Postgres :5434) + the demo/phone steps; what "runnable" means.
- `src/core/respond.py` — the load-bearing contract: the transport-agnostic brain `respond(belief, history, utterance, llm, config)` shared by text self-play AND the live voice worker. **R37 parity** — voice must call the SAME brain as text; nothing LiveKit may cross this boundary.
- The test suite (`.venv/bin/python -m pytest -q`, currently 333 passing / 3 skipped) — the green bar each item stays at; livekit-guarded voice tests run under `/usr/bin/python3`.
- `web/components/cadence/` + `web/app/cadence.css` (the "Cadence" dark design system) — what "matches the design" means for any UI item.

**The discipline (do this every time):**

1. **Pull only what you're told.** Don't start a To Do item until you've been handed its prompt. The board defines scope; the prompt authorizes execution.
2. **Before you touch code:** move every unit you're *about to* work on from **To Do → In Progress** and fill the In Progress template (owner, plan, files). Split one item into `CB-NN.a/.b` if it's several work units.
3. **While working:** keep the In Progress checklist current. Record blockers inline rather than going silent.
4. **When finished AND verified:** move the item **In Progress → Done** and fill the Done template (actual files, verification result). "Finished" is *tests green + matches the design + no regression*, not "code written."
5. **Never delete history.** Items only move forward. Edit in place; don't rewrite IDs.

### Default workflows

- **Bug-fix flow:** reproduce → write/extend the failing test (it should fail for the stated reason) → fix → test green → visual/behavior diff vs the design → move to Done.
- **Feature flow:** confirm it fits the spec/contract (fix the signature first if not) → write the acceptance test → build behind the agreed seam → test green → diff vs design → move to Done.

**IDs.** `CB-NN`, stable and never reused. These are **internal indices** — never render them in any viewer-facing surface (UI, decks, dashboards, copy). Code/comments/`data-*` only.

**Project hard constraints (every item must uphold):**
- **R37 brain parity** — the live voice worker and text path both go through `src/core/respond.py`; no LiveKit type crosses into `src/core`.
- **No internal indices in observable output** — `CB-NN`, version/cohort/episode ids, persona/driver slugs, outcome keys: translate to a human label or omit; never render raw.
- **PII** — phone numbers stored ONLY as a sha256 hash (R42); raw phone never persisted; consent gating (AI disclosure + recording + minor/COPPA) is compliance-critical.
- **Design** — UI changes use the Cadence dark tokens/components, no new visual language; verify the rendered result (Playwright/screenshot), not just that code compiles.
- **Green bar** — the change keeps the suite green and adds a regression test for the behavior it changes.

**Field legend (used by all three templates):**
- **Type:** `bug` (diverges from spec/design) · `feature` (new capability) · `change` (intentional spec revision).
- **Surface:** `/demo` · `/operate/*` · `/improve/*` · `api` (FastAPI src/api) · `voice-worker` (src/voice) · `core` (brain src/core) · `data` (Postgres) · `design` (Cadence).
- **Size:** `S` (≤1 file / localized) · `M` (a component + its wiring) · `L` (multi-file / new subsystem).
- **Prereqs:** other `CB-NN` or a doc/signature that must land first; `—` if none.

---

## To Do

> **Template — copy this block per item.**
> ```
> ### CB-NN — <short title>
> - **Type / Surface / Size:** <bug|feature|change> · <surface> · <S|M|L>
> - **Prereqs:** <CB-NN, …, or —>
> - **Important files (candidates):** <best-guess paths; verify against the contract>
> - **Current:** <what it does today>
> - **Desired:** <what it should do; cite the design/spec it must match>
> - **Acceptance:** <how we'll know it's done — visible behavior + which test>
> - **Refs:** <spec docs / sections / design files>
> ```

### CB-01 — Active-calls queue with per-call take-over
- **Type / Surface / Size:** feature · `/operate/live` · `api` · `voice-worker` · L
- **Prereqs:** — *(no other CB blocks it; internally it has 3 work units that order themselves: the list endpoint → the queue UI → the take-over mechanism. Split into `CB-01.a/.b/.c` when pulled.)*
- **Important files (candidates):** `src/api/operate.py` (new "list active calls" endpoint alongside `/api/live`; today `live_ep` does `list_episodes(limit=1)`), `web/app/operate/live/page.tsx` + `web/lib/operate-api.ts` (queue UI + fetcher), `src/voice/worker.py` + `src/api/demo_routes.py` (the real take-over / operator barge-in into the call's LiveKit room).
- **Current:** `/api/live` returns only the single most-recent episode, so `/operate/live` shows ONE call. The backend already runs concurrent calls (each phone call = its own LiveKit job subprocess; each `/demo` session = its own `session_id` + `VoiceSession`), but there's no way to enumerate them. The "Take over" button on `/operate/live` is honestly **disabled** because operator barge-in isn't wired (see Done CB-history / commit `0bf1f93`).
- **Desired:** an operator sees a **queue/list of ALL active calls** (each in-progress, non-stale call — phone + web), can select any to watch its live transcript + belief, and can **take over any one** of them — a real mechanism that connects the operator into that call's LiveKit room (or transfers the call to a human), with the agent gracefully yielding. Must respect R37 (don't fork the brain), consent/recording state, and the stale-call guard already in `live_snapshot` (a 0-turn in-progress call older than `LIVE_STALE_SECONDS` is not "active").
- **Acceptance:** with N concurrent active calls, the live page lists N entries (newest-first, each with caller/persona + stage + trust/bail signal); selecting one streams its live transcript; "Take over" on an entry connects the operator audio into that LiveKit room (verified with a real or stubbed second participant) and the AI yields; an integration test covers the multi-active list endpoint (≥2 active calls → ≥2 entries, stale ones excluded) and the take-over hand-off. No raw episode/room id rendered as a primary label.
- **Refs:** `src/api/operate.py::live_snapshot` + `live_ep`; LiveKit Agents room/participant model + SIP dispatch (`scripts/setup_sip_dispatch.py`, rule `SDR_hKYQYAwz96uA`); the disabled take-over control (`web/app/operate/live/page.tsx`); R37 in `src/core/respond.py`.

### CB-02 — Show the real change dimension on the version lineage (kill "CHANGE —")
- **Type / Surface / Size:** change · `/improve/versions` · `api` · `data` · M
- **Prereqs:** — *(deferred during the QA loop as a data-model limitation; tracked here so it isn't lost.)*
- **Important files (candidates):** `src/api/improve.py` (`/api/versions` serialization), `src/memory/store.py` + the `version_lineage` table/schema, `web/app/improve/versions/page.tsx`.
- **Current:** the lineage renders "CHANGE —" for promoted versions because `version_lineage` rows store only `version / parent_version / config_ref / kb_version / kpi / is_champion` — there is **no change-dimension field**. The human change ("Pricing concession band", "Discovery sequencing") lives on the `experiment` table, which these seed lineage rows aren't cleanly linked to.
- **Desired:** each promoted version shows its human change dimension (via `dimensionLabel`) instead of "—" — either by joining lineage → experiment, or by stamping the dimension onto the lineage row at promotion time (the cleaner fix; the loop's `promote` path is where it's known).
- **Acceptance:** a promoted version on `/improve/versions` shows its change dimension label (not "—"); a genesis version still shows "—"; no raw `__dimension__` slug leaks.
- **Refs:** `src/loop/promotion.py` (where the dimension is known at promote time); `version_lineage` schema; `web/lib/labels.ts::dimensionLabel`.

### CB-04 — Humanize three remaining raw-slug leaks (KPI cohort + Approvals diff + Review rationale)
- **Type / Surface / Size:** bug · `/operate/kpi` · `/improve/approvals` · `/operate/review` · S
- **Prereqs:** —
- **Important files (candidates):** `web/app/operate/kpi/page.tsx` (the `COHORTS` selector ~line 39 + the empty-state line), `web/app/improve/approvals/page.tsx` (`diff_description` render ~line 96/232); `web/app/operate/review/[id]/page.tsx` (the `rv-rat` rationale span ~line 536); possibly the backend `diff_description` text in `src/api/improve.py` / `src/loop`, and the gate rationale strings in `src/core/gates.py` (e.g. `pushiness_cap: bail_risk over cap…`).
- **Current:** (a) the KPI **cohort** dropdown renders the raw slug `held_out`; (b) the Approvals "diff_description" sentence embeds the raw threshold key, e.g. *"Raise max_concession_band 0.15 → 0.22"*; (c) the Review page renders each turn's gate rationale verbatim in `.rv-rat`, and gate rationales embed raw enum slugs + gate names, e.g. *"pushiness_cap: bail_risk over cap; backing off pressure…"* — confirmed leaking on a live sim episode during the CB-06 screenshot audit (2026-06-02).
- **Desired:** humanize all three via `web/lib/labels.ts` (`held_out` → "Held-out"; `max_concession_band` → "Pricing concession band"; `bail_risk` → "walk-away risk", `pushiness_cap` → a human gate label) — no raw snake_case slug or gate-name in operator-facing text (the no-internal-index rule). For the Review rationale, prefer scrubbing/labeling at the render boundary so internal logs keep the raw slug.
- **Acceptance:** a Playwright text scan of `/operate/kpi` (cohort dropdown open), `/improve/approvals`, and `/operate/review/<sim-id>` finds no `held_out` / `max_concession_band` / `bail_risk` / `pushiness_cap` / other snake_case slug rendered.
- **Refs:** QA round 7 findings; CB-06 screenshot audit (`tests/e2e/verify_cb05_cb06_screens.py` flagged `bail_risk` in `rv-rat`); `web/lib/labels.ts` (`populationLabel`/`dimensionLabel` already exist).

### CB-10 — Incoherent seed/sim episodes (terminal outcome ≠ transcript)
- **Type / Surface / Size:** bug · `data` (seed/sim episodes) · `/operate/calls` · `/operate/review` · M
- **Prereqs:** —
- **Important files (candidates):** the seeded sim batch + `scripts/seed_demo.py` (escalation-coherence precedent), `src/sim/selfplay.py::run_episode` (produces coherent arcs), `src/memory/store.py` (query/purge).
- **Current:** legacy 3-turn sim seed episodes carry a generic discovery transcript (greeting → "My son needs help with SAT math." → "What score is he aiming for?") but a MISMATCHED terminal outcome. Confirmed on `ep-98b95465e9ec456d9dab1ffa80eff9c5` (channel=sim, cohort `coh-cda860a7`, **outcome=enrolled, 3 turns, no close in the transcript**). The user: "says it hit enrollment but the transcript shows nothing." New live/demo calls (CB-08) are already coherent — this is only the old seeded stubs.
- **Desired:** every episode shown in Calls/Review has transcript↔outcome coherence (a terminal outcome's transcript actually reaches that close). Either REGENERATE the demo dataset via real self-play (`run_episode`, which now reliably closes — CB-03) and replace the 3-turn stubs, or PURGE the incoherent stubs. (DB DELETE/regenerate needs explicit user authorization.)
- **Acceptance:** no episode in the Calls list shows a terminal outcome (enrolled/consult_booked/…) whose transcript never reaches a close; spot-check ≥10 seeded episodes.
- **Refs:** `ep-98b95465…` (the reported case); `scripts/seed_demo.py`; `src/sim/selfplay.run_episode`; CB-03 (self-play now reaches realistic closes).

### CB-11 — Remove the redundant per-call "explore/improve" button
- **Type / Surface / Size:** change · `/operate/review` (TBC) · S
- **Prereqs:** — *(blocked on confirming the exact element — see Notes)*
- **Important files (candidates):** `web/app/operate/review/[id]/page.tsx` (the **"Use in experiment"** button, ~line 496–503, flask icon → `/improve/lab`) — the leading candidate; confirm against the actual screen the user means.
- **Current:** a per-call button opens the Experiment Lab ("explore/improve" the call). The user reports it's useless because "we see the entire list on the right hand side" — i.e. the call list / belief panel already provides the affordance.
- **Desired:** remove the redundant button (and any now-dead handler/import); keep the layout balanced.
- **Acceptance:** the button is gone; no orphaned import/handler; tsc clean; the page layout still reads cleanly (screenshot check).
- **Refs:** review page buttons row (`Flag` / `Export` / `Use in experiment`). NOTE: confirm WHICH button/screen with the user before removing (the "list on the right" detail doesn't perfectly match the Review layout).

### CB-12 — Demo as a real-time, generated back-and-forth (not poll-batched scripted lines)
- **Type / Surface / Size:** feature · `/operate/live` · `api` · M/L
- **Prereqs:** builds on CB-08 (server-side demo).
- **Important files (candidates):** `src/api/demo_routes.py::_run_auto_demo` (swap the scripted `_AUTO_DEMO_SCRIPT` for the `ProspectSimulator` so the prospect is LLM-GENERATED), `src/sim/prospect.py` (the generated prospect side), the live transport — today `web/app/operate/live/page.tsx` polls `/api/live/active` every `POLL_MS=5000`, so turns appear in batches; real-time needs SSE/WebSocket OR sub-second polling of the in-progress turns.
- **Current:** the demo plays FIXED prospect lines against the real brain, and the UI refreshes on the 5 s poll — so turns show up in clumps "every few seconds," not as a live exchange.
- **Desired:** (1) the PROSPECT generates its turn via the brain/sim (varied run-to-run, a true self-play), then the AGENT generates its reply; (2) each turn STREAMS to the monitor as it's produced (prospect bubble appears, then agent bubble), in near-real-time — not batched on a 5 s poll.
- **Acceptance:** watching a demo, each turn appears individually as generated (prospect → agent → prospect …), with sub-second latency from server commit to on-screen; prospect lines differ across two runs (proving generation, not a script).
- **Refs:** `src/sim/selfplay.run_episode` (the self-play loop), `persist_call_live` heartbeat, the live poll cadence; an SSE endpoint pattern.

### CB-13 — Demo end: clear "call ended" marker + the monitor must NOT auto-close
- **Type / Surface / Size:** feature · `/operate/live` · S
- **Prereqs:** builds on CB-08.
- **Important files (candidates):** `web/app/operate/live/page.tsx` (the active-call → empty-state transition + the queue auto-select), the demo finalizer in `src/api/demo_routes.py` (terminal outcome).
- **Current:** when the demo ends it goes terminal → drops out of `/api/live/active` → the monitor auto-clears (empty state / auto-selects another call). There's no explicit end-of-call signal in the transcript, and the just-watched call vanishes.
- **Desired:** at end, render a clear **"Call ended"** marker in the transcript (with the outcome), and the monitor must NOT auto-close/clear — the finished call stays on screen until the operator explicitly dismisses it / returns to the live list.
- **Acceptance:** after a demo completes, the transcript shows an unmistakable "Call ended — <outcome>" marker and the call remains displayed (no auto-clear); a manual control returns to the live queue.
- **Refs:** `web/app/operate/live/page.tsx` empty-state + `pollQueue` auto-select; CB-08 demo finalizer.

### CB-14 — Site-wide scroll/click jank from pervasive backdrop-filter blur
- **Type / Surface / Size:** bug · `perf` · all surfaces (`cadence.css` global) · S
- **Prereqs:** —
- **Important files (candidates):** `web/app/cadence.css` (`--glass-blur: saturate(150%) blur(22px)` on `.card`+`.kpi`; `backdrop-filter: blur(8px)` on `.nav`+`.topbar`); minor: the CB-08 `useNow` 1 s timer (`web/app/operate/live/page.tsx`).
- **Current:** the console feels slow on every scroll and click. Root cause: `backdrop-filter: blur(22px)` is applied to EVERY `.card` and `.kpi` (the KPI page alone renders ~15) plus the always-visible `.nav`/`.topbar` — the browser re-composites + re-blurs all those layers on every paint/scroll frame (a well-known scroll-jank source, amplified by `next dev`). Secondary: the live-page `useNow` interval re-renders `CallMonitor` every second even when idle.
- **Desired:** smooth scroll/click. Drop `backdrop-filter` from the high-count, always-on surfaces (cards/kpi/nav/topbar) while KEEPING the translucent `--glass` gradient + sheen/shadow so the look is preserved (the blur is barely visible over the solid dark page bg). Keep blur only on transient overlays (`.drawer`/`.modal`/`.scrim`). Optionally gate the `useNow` tick to active calls only.
- **Acceptance:** scrolling a card-heavy page (`/operate/kpi`) is smooth; before/after screenshots show the visual look preserved (translucent cards, sheen, borders intact); no `backdrop-filter` on `.card`/`.kpi`/`.nav`/`.topbar`; tsc/build unaffected.
- **Refs:** `web/app/cadence.css` lines ~26–28 (`--glass`/`--glass-blur`), 112 (`.nav`), 144 (`.topbar`), 175 (`.card`), 256 (`.kpi`).

### CB-15 — "Run experiment" freezes the page (synchronous minutes-long endpoint)
- **Type / Surface / Size:** bug · `/improve/lab` · `api` · M
- **Prereqs:** —
- **Important files (candidates):** `src/api/improve.py::run_experiment_ep` (~line 519 — runs the full A/B inline under `asyncio.wait_for` + `run_semaphore`), `web/app/improve/lab/page.tsx` (`RunDrawer.submit` ~299 awaits the single fetch; the poll at ~494 already exists), `web/lib/improve-api.ts::runExperiment`.
- **Current:** `POST /api/experiments/run` executes the ENTIRE A/B (`n×2` REAL model calls, minutes long in prod) SYNCHRONOUSLY before responding. The drawer `await`s that one request with the button stuck on "Running…", so the page looks frozen for the whole run; a second run returns 503 busy. No progress/cancel/escape.
- **Desired:** make the run ASYNCHRONOUS — persist a `running` ExperimentRecord and return IMMEDIATELY (202) with it, spawn the A/B as a background task that updates the record to passed/blocked/etc. on completion (mirrors the CB-08 server-side task pattern). The drawer closes at once, the new "running" card appears, and the existing `/api/experiments` poll settles it. Page stays interactive; keep the concurrency guard (busy → clear message, not a hang). Bonus: a cancel.
- **Acceptance:** clicking Run closes the drawer immediately + shows a "running" card; the page is interactive throughout; the card transitions to its result via poll; a contended run shows a clear "in progress" message, never a frozen button.
- **Refs:** `src/api/improve.py` run_experiment_ep + `run_semaphore`/`wait_for`; `web/app/improve/lab/page.tsx` RunDrawer + poll; CB-08 background task.

### CB-16 — Dashboard → "Talk to the agent" (demo call console) link
- **Type / Surface / Size:** feature · dashboard shell (all `/operate`+`/improve`) · S
- **Prereqs:** —
- **Important files (candidates):** `web/components/cadence/DashboardShell.tsx` (topbar/nav — add the control), `web/app/demo/*` (the existing operator-dashboard link to mirror), `web/app/page.tsx`.
- **Current:** the landing page links to both `/demo` and `/operate`, and `/demo` has an operator-dashboard link (commit `e78d0dc`), but the dashboard chrome has NO link back to the speaking/demo interface (`/demo`) — once in the dash you can't get to the talk-to-the-agent console without going home.
- **Desired:** a clear control in the dashboard shell (topbar preferred) — "Talk to the agent" / "Call console" → `/demo` — mirroring the dashboard link that already lives on `/demo`, so the round-trip works both ways.
- **Acceptance:** a visible Cadence-styled control in the dashboard shell navigates to `/demo`; it round-trips with `/demo`'s existing dashboard link; present on every dashboard page (it's in the shared shell).
- **Refs:** `web/components/cadence/DashboardShell.tsx` topbar; the `/demo` dashboard-link precedent (`e78d0dc`).

---

## In Progress

> **Template — copy this block when you pull an item.**
> ```
> ### CB-NN[.x] — <short title>
> - **Type / Surface / Size:** <…>
> - **Owner:** <agent / model tier>
> - **Started:** <YYYY-MM-DD>
> - **Prereqs met?:** <yes / blocked on CB-NN>
> - **Plan (checklist):**
>   - [ ] <step>
>   - [ ] <step>
>   - [ ] test written/updated (which test)
>   - [ ] diff vs design
> - **Files being touched:** <actual paths>
> - **Notes / blockers:** <inline; don't go silent>
> ```

### CB-03 — Deepen self-play conversations (sim raises objections; agent does fuller discovery)
- **Type / Surface / Size:** change · `core` (`src/sim`) · `data` · L
- **Owner:** coder-sim-depth (implementation) + orchestrator (design + self-play verification)
- **Started:** 2026-06-01
- **Prereqs met?:** yes
- **Why:** the synthetic transcripts are unrealistically short (most 3–5 turns; `max_turns`=24 is not the cap). The prospect warms its soft drivers + depletes patience too fast, so calls reach commit/walk in a few turns — a sim-fidelity gap (the sim-to-real concern). Real tutoring sales calls run 15–40 turns with surfaced objections.
- **Plan (checklist):**
  - [ ] Slow the prospect's soft-driver warming + patience depletion so reaching commit thresholds takes more turns (gradual trust/intent gain).
  - [ ] Have the prospect SURFACE 1–3 objections (price / efficacy / DIY-free / timing) before warming — realistic friction the agent must handle.
  - [ ] Deepen agent discovery (config: require more discovery slots before pitch/close) so it earns the close over more turns.
  - [ ] Keep `test_prospect.py` green; adjust expected dynamics ONLY where the tuning legitimately changes them (not to paper over a broken invariant).
  - [ ] Orchestrator: run a SMALL real self-play batch → confirm avg turn count rises (target ~10–20) + outcomes stay sane + the buy-gate invariants hold; then regenerate demo transcripts.
- **Files being touched:** `src/sim/prospect.py` (patience + soft-driver warming + the sim-LLM objection prompt), `src/sim/selfplay.py` (termination, if needed), `src/config/versions/champion_v0.yaml` (`thresholds.discovery_slots_required`), `tests/unit/test_prospect.py`.
- **Notes / blockers:** HARD INVARIANT — the honest buy-gate must stay PURE/DETERMINISTIC: budget + qualified + per-tier ceiling immutable to talk; a commitment fires ONLY on the agent's `attempt_close` at the offered tier. Do NOT let "more turns" come from breaking that (e.g., never auto-commit / never move budget). Regenerating transcripts is paid LLM self-play — bounded batch first.
- **Notes:** DONE + committed (1d9cdec): conviction coupling fixed the binding constraint; N=10 verify = 6/7 qualified close, 0/3 unqualified, avg ~19 turns, buy-gate intact. (Board kept here for history.)

---

## Done

> **Template — copy this block when an item is finished AND verified.**
> ```
> ### CB-NN[.x] — <short title>
> - **Type / Surface / Size:** <…>
> - **Completed:** <YYYY-MM-DD>
> - **Files changed (actual):** <paths>
> - **What changed:** <1–3 lines — the real diff, not the intent>
> - **Verification:** <tests run + pass/fail; diff vs which design>
> - **Constraints checked:** <project invariants verified, or N/A>
> - **Follow-ups / known gaps:** <or none>
> ```

### CB-05 — Per-turn belief DELTA in Call Review
- **Type / Surface / Size:** feature · `/operate/review` · S
- **Completed:** 2026-06-02
- **Files changed (actual):** `web/app/operate/review/[id]/page.tsx` (added `driverVelocity()` + `VelocityBadge`), `web/lib/operate-types.ts` (no new type needed — `trends` already typed).
- **What changed:** each gauge header (Trust, Walk-away risk) now renders an inline `VelocityBadge` reading `trends["<key>_velocity"]` — `↑ +0.12` green (rising), `↓ −0.08` amber (falling), `~` faint (|v|<0.01). Raw velocity slugs never render (badge shows arrow + signed magnitude only).
- **Verification:** tsc exit 0; `.venv` suite 412 passed / 5 skipped; screenshot-verified on a live sim episode (`sp-a0494cd6…`) — Trust `~`, Walk-away risk `↓ −0.10` render correctly, no overflow, 0 JS errors (`tests/e2e/verify_cb05_cb06_screens.py`).
- **Constraints checked:** no raw `*_velocity` slug leaks into visible text (verified); Cadence tokens only, no new visual language.
- **Follow-ups / known gaps:** none for CB-05. (Separate pre-existing leak found during the audit: the gate **rationale** string in `.rv-rat` leaks `bail_risk`/`pushiness_cap` — folded into CB-04, not introduced here.)

### CB-06 — Agent-estimate vs prospect-TRUTH belief panel (self-play / twin only)
- **Type / Surface / Size:** feature · `core` (`src/sim`) · `api` · `/operate/review` · M
- **Completed:** 2026-06-02
- **Files changed (actual):** `src/sim/selfplay.py` + `src/api/operate.py` + `tests/integration/test_selfplay.py` + `tests/e2e/test_operate.py` (backend, committed `e86df9e`); `web/app/operate/review/[id]/page.tsx` (`ProspectTruthPanel`, `findProspectState`, `OVERLAP_KEYS`, `PROSPECT_ONLY_KEYS`, `gapColor`) + `web/lib/operate-types.ts` (`ProspectTurnState` + `prospect_trajectory` field) (frontend).
- **What changed:** `run_episode` accumulates `prospect.state.snapshot()` (filtered to the 6 frozen driver keys, rounded 3dp) into `episode.metrics["prospect_trajectory"] = [{turn, drivers}]`; `episode_detail` exposes top-level `prospect_trajectory` (`[]` for real calls). Frontend renders a panel — only when `prospect_trajectory.length > 0` — comparing agent-estimate vs prospect-truth for the 3 overlapping drivers (trust/urgency/purchase_intent) with a signed, color-coded gap, plus prospect-only context (need/budget/patience).
- **Verification:** backend — 412 passed / 5 skipped incl. 5 new CB-06 tests (persisted-on-sim, empty-for-voice, empty-when-metrics-None); frontend — tsc exit 0. Screenshot-verified (`tests/e2e/verify_cb05_cb06_screens.py`): sim episode `sp-a0494cd6…` shows the panel (Trust 0.95/0.74/+0.21 amber, Urgency 0.00/0.42/−0.42 red, Purchase intent 0.00/0.40/−0.40 red; context Need 0.59 / Budget 0.71 / Patience 0.20), voice episode `ep-94aabde4…` correctly OMITS the panel with no layout hole, 0 JS errors on both.
- **Constraints checked:** pure-additive backend (buy-gate / outcomes / existing metrics untouched); driver-key mismatch respected (only overlapping keys compared); no prospect-side slug rendered raw; R37 parity unaffected (sim-only path).
- **Follow-ups / known gaps:** the agent's belief shows urgency=0.00 / purchase_intent=0.00 at the close turn while the prospect's true urgency/intent are ~0.4 — a real DST-calibration signal (agent under-tracks urgency/intent), now VISIBLE thanks to this panel; candidate for a future calibration item, not a UI defect.

### CB-07 — "Run demo call" button on the Live monitor (self-driving scripted call, no phone)
- **Type / Surface / Size:** feature · `/operate/live` · `api-client` · S
- **Completed:** 2026-06-02
- **Why:** a real inbound phone call only feeds the Live monitor once a turn COMMITS (per-turn heartbeat upsert). The operator's test call hung up during the agent's opening (0 turns) so the monitor stayed empty — making it look broken. Operators (and demos) need a phone-free way to watch the live pipeline populate end-to-end.
- **Files changed (actual):** `web/lib/demo-call.ts` (NEW — `runDemoCall` orchestrator + `DEMO_PROSPECT_SCRIPT`), `web/lib/api.ts` (+`sessionEnd`), `web/lib/api-types.ts` (+`SessionEndRequest/Response`, +`done?` on `ChatResponse`), `web/app/operate/live/page.tsx` (empty-state "Run demo call" button + status + cleanup), `tests/e2e/verify_demo_button.py` (NEW — e2e verification).
- **What changed:** the button drives a scripted self-playing call through the REAL `/api/consent/start`+`/respond` then `/api/chat` per turn (each commit fires the same live-heartbeat upsert the phone path uses) then `/api/session/{id}/end`. The existing 5 s queue poll auto-selects the new call; the operator watches it stream. Aborts + best-effort-ends the session on unmount (no dangling shell).
- **Verification:** tsc exit 0; e2e (`tests/e2e/verify_demo_button.py`) — button present on empty state, click → call LIVE in monitor at ~8 s → streamed to 10 turns → reported complete, 0 JS errors; screenshots (`/tmp/cb0506/demo_empty.png`, `demo_live.png`) reviewed: clean empty-state button + populated live monitor (LIVE pill, transcript, belief gauges). Demo ended terminal ("Consultation booked", 12 turns) — also lands in Calls + Review; live queue back to 0 (no phantom).
- **Constraints checked:** real brain replies (no mock); consent flow honored (AI-disclosure + recording accepted, is_minor=false); no internal phase slugs rendered (humanized status text); Cadence tokens only; N3 invariant intact (LIVE pill only when genuinely active).
- **Follow-ups / known gaps:** demo is a TEXT-channel call (monitor is channel-agnostic). Separately, the REAL phone hang-up leaves the call `in_progress` forever (invisible in Calls); mapping mid-call participant-disconnect → a terminal outcome ("abandoned"/"released") remains an open item (noted in the handoff).

### CB-08 — Server-side demo call + live ticking timer + working auto-scroll
- **Type / Surface / Size:** feature+bug · `api` (`demo_routes`/`persistence`) · `/operate/live` · M
- **Completed:** 2026-06-02
- **Files changed (actual):** `src/api/demo_routes.py` (POST /api/demo/auto/start + `_run_auto_demo` background task + `_finalize_auto_demo` + `_AUTO_DEMO_SCRIPT`; session_end passes created_at), `src/api/persistence.py` (`episode_from_session` gains `created_at`→episode.created_at+`duration_ms` and `outcome_override`; `persist_call_live` records `duration_ms`; `persist_call_end` threads both), `web/app/operate/live/page.tsx` (button→`startAutoDemo`, removed client orchestrator + abort-on-unmount, `useNow` ticking timer, real auto-scroll toggle), `web/lib/api.ts` + `web/lib/api-types.ts` (`startAutoDemo`/`AutoDemoResponse`), deleted `web/lib/demo-call.ts`, `tests/e2e/verify_demo_button.py` + `tests/e2e/test_demo_duration_outcome.py`.
- **What changed:** the demo now runs SERVER-SIDE — one POST spawns a background task that streams the full scripted arc through the real brain + the same per-turn live-upsert path /api/chat uses, so it populates the Live monitor AND survives the operator navigating away (it is no longer browser-bound). The live monitor shows a per-second ticking duration (from created_at); duration_ms is persisted; auto-scroll is a working toggle. The demo forces a terminal outcome (`outcome_override` from whether the agent closed at any point) so it lands in Calls as "Consultation booked", never lingering in_progress.
- **Verification:** `.venv` 427 passed / 5 skipped (8 new pure tests in test_demo_duration_outcome.py); tsc exit 0; e2e (`verify_demo_button.py`) PASS — click → live at ~8 s, timer ticked 0:09→0:12, **survived navigation to Calls and back (turns 2→6 while away)**, completed terminal "Consultation booked" (16 turns, duration 67809 ms), 0 JS errors. Screenshots (`/tmp/cb0506/demo_live.png`, `demo_after_nav.png`) reviewed: ticking timer (0:07→0:27) + streaming transcript + belief gauges + auto-scroll, no layout break.
- **Constraints checked:** real brain (no mock); consent pre-captured server-side (AI disclosure + recording, non-minor); background task wrapped in try/except + held in a ref (no GC) + finalizes in `finally` (no dangling in_progress); Cadence tokens only; no internal slug rendered; 503 when no LLM key / at capacity.
- **Follow-ups / known gaps:** the script keeps talking ~2 turns past a mid-call close (harmless for a demo). `worker.py` remains 996 lines (pre-existing; flagged, not split here).

### CB-09 — Mid-call hang-up maps to a terminal outcome (not stuck in_progress)
- **Type / Surface / Size:** bug · `voice` (`src/voice/worker`) · `api/operate` · S
- **Completed:** 2026-06-02
- **Files changed (actual):** `src/voice/worker.py` (`_terminal_outcome_on_disconnect` pure mapping + wired into the shutdown persist: re-saves with a terminal outcome when the call ended with no terminal act), `src/api/labels.py` (+`"abandoned": "Abandoned"` human label), `src/api/operate.py` (`_is_completed` now surfaces a 0-turn `abandoned` hang-up in the Calls list while still excluding in_progress / other 0-turn rows), `tests/unit/test_disconnect_outcome.py` (NEW, test-first) + the `_is_completed` cases in `tests/e2e/test_demo_duration_outcome.py`.
- **What changed:** a participant-disconnect that reached no terminal act now persists as a terminal outcome — `abandoned` (≈0 substantive turns, the user's hang-up case) or `released` (real conversation, no close) — never in_progress, never a fabricated positive. The Calls-list filter was relaxed so a 0-turn `abandoned` call (the exact reported episode shape) is visible (a real missed lead), while in_progress shells stay hidden.
- **Verification:** test-first RED→GREEN (`test_disconnect_outcome.py` 7 cases: ImportError→7 passed); `.venv` 427 passed / 5 skipped; the voice worker was restarted by explicit PID (pid 96812) and **re-registered with LiveKit** so real calls now get the fix.
- **Constraints checked:** never maps a hang-up to enrolled/booked (returns None when a real close/escalate/disqualify fired → derive_outcome wins); consent/PII invariants untouched (reads only already-committed scrubbed turns); the `abandoned` slug renders via its label, never raw (no-internal-index rule).
- **Follow-ups / known gaps:** the user's ORIGINAL episode `ep-e8a4f96b…` was persisted BEFORE the fix so it stays in_progress (historical) — only calls from now on get the terminal mapping.

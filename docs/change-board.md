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
- **Take-over spec (user, 2026-06-02) — refines CB-01.c:** on "Take over", the AI must (1) be INTERRUPTED mid-conversation and (2) speak a DEFAULT TTS hand-off line (e.g. "Let me bring in a specialist to help — one moment. They may type some of their responses, so there might be a brief pause between answers."). Then the OPERATOR talks into their own device (browser mic / WebRTC into the call's LiveKit room) and is heard by the caller through the phone — operator audio is published as a room participant, NOT a dial-in (presume the operator is NOT calling from their own phone number, so no SIP bridge / caller-ID concerns). The agent stops auto-generating; the operator may ALSO TYPE responses that are TTS'd to the caller ("the agent might be typing their responses"). So take-over = interrupt + disclosure TTS + operator mic→room audio + optional operator-typed→TTS, with the AI yielded. Consent/recording state must carry over; respect R37 (don't fork the brain — the brain just stops).

### CB-30 — (future) Golden-set capture of real conversations
- **Type / Surface / Size:** feature · `data` · M
- **Prereqs:** CB-22 + CB-23 (call logging + belief persistence must work first)
- **Current:** the user wants to record a few real conversations as a GOLDEN SET to calibrate/work from — but only once call logging (CB-22) + per-turn belief (CB-23) are verified, so the captured calls are complete + coherent.
- **Desired:** a way to mark/flag a real call as "golden" and export the set (transcript + belief trajectory + outcome) for use as a calibration reference / replay seed.
- **Acceptance:** an operator can tag a real call as golden; the golden set is exportable + replayable.
- **Refs:** user "record a couple of conversations down the line as a golden set"; depends on CB-22/CB-23; the replay/twin harness (CB-06 era).

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

### CB-02/11/12/13/14/15/16/17/19 — Wave 1 batch (3 parallel coders + orchestrator)
- **Completed:** 2026-06-02
- **Verification (shared):** `.venv` suite **438 passed / 5 skipped**; `web` `tsc --noEmit` exit 0; integration screenshots (`tests/e2e/verify_wave1.py`, `/tmp/cb_wave1/*`) + endpoint checks; API + worker restarted. 0 JS errors on the verified pages.
- **CB-11** (remove Operate/Improve mode toggle) — `DashboardShell.tsx`/`cadence.css`; toggle + unused `mode`/`router` removed; nav groups intact. Verified: `.mode` count = 0.
- **CB-14** (scroll/click jank) — removed `backdrop-filter` from `.card`/`.kpi`/`.nav`/`.topbar` (kept translucent `--glass` bg + sheen/shadow; blur only on `.drawer`/`.modal`/`.scrim`). Verified: glass look preserved in screenshot.
- **CB-16** (dash→demo link) — topbar `.gctl` "Talk to the agent" → `/demo`. Verified: `a[href="/demo"]` present.
- **CB-17** (stale nav badges) — `useNavBadges` now polls (15 s) + refetches on focus/visibilitychange; self-contained in the shell.
- **CB-12** (real-time generated demo) — `_run_auto_demo` now drives an LLM-generated `ProspectSimulator` (varied per run) vs the real brain; new SSE `GET /api/demo/auto/stream` streams per-turn (5 s poll fallback); terminal-guarantee extended (`walked`/`released`). Verified: generated skeptical prospect streamed live.
- **CB-13** (call-ended marker + no auto-close) — ended call freezes its last snapshot + "Call ended — <outcome>" marker + "Back to live"; no auto-clear. Frontend-only.
- **CB-15** (experiment Run freeze) — `/api/experiments/run` returns immediately with a `running` record; background asyncio task settles it. Verified: run returned instantly; `verify-async` settled to `rejected` (no hang).
- **CB-19** (Use-in-experiment scaffold) — new `POST /api/experiments/scaffold` from an episode; review-page button scaffolds + routes to `/improve/lab?episode=<id>` (auto-opens a pre-seeded review), not a blank lab.
- **CB-02** (version lineage dimension) — `/api/versions` backfills `dimension_label` from the experiment table (non-migration JOIN); `promotion.py` already stamps it for future. Verified: champion v1 renders "Discovery sequencing" (was "CHANGE —"); genesis "—".
- **Files:** `DashboardShell.tsx`, `cadence.css`, `operate/live/page.tsx`, `demo_routes.py`, `improve.py`, `improve/lab/page.tsx`, `improve/versions/page.tsx`, `operate/review/[id]/page.tsx` (button only), `improve-api.ts`, `improve-types.ts` + tests (`test_demo_generated_stream.py`, `test_improve.py`).
- **Follow-ups:** `demo_routes.py` (879) + `live/page.tsx` (968) exceed 500 lines (pre-existing; flagged). SSE *streaming* path is orchestrator-verified (not unit-tested — TestClient single-loop limitation).

### CB-04 — Humanize three raw-slug leaks (KPI cohort + Approvals diff + Review rationale)
- **Completed:** 2026-06-02
- **Files changed:** `web/lib/labels.ts` (+`THRESHOLD_LABELS`/`thresholdLabel`, `DRIVER_LABELS`, `GATE_LABELS`, `humanizeDiffDescription`, `humanizeRationale`), `web/app/operate/kpi/page.tsx` (cohort `{value,label}` + `populationLabel`), `web/app/improve/approvals/page.tsx` (both diff renders → `humanizeDiffDescription`), `web/app/operate/review/[id]/page.tsx` (`.rv-rat` → `humanizeRationale`).
- **What changed:** all three leaks humanized at the RENDER boundary (internal logs/`diff_description`/`rationale` keep raw slugs): `held_out`→"Held-out"; `max_concession_band`→"Pricing concession band" (e.g. "Pricing concession band 0.15 → 0.22"); `"pushiness_cap: bail_risk over cap…"`→"Pushiness cap: walk-away risk over cap…".
- **Verification:** tsc exit 0; re-ran `tests/e2e/verify_cb05_cb06_screens.py` on a sim episode — review-page `leaked_slugs=[]` (was `['bail_risk']`), panel present (sim) / absent (voice), 0 JS errors; agent's deterministic humanizer check over all 8 gate rationales + both diff shapes leaked nothing.
- **Constraints checked:** render-boundary only (logs keep raw); Cadence tokens; no layout change; longest-first regex so `bail_risk_velocity` beats `bail_risk`.
- **Follow-ups:** none.

### CB-18 — Empty escalations purged + guard (no more contentless rows)
- **Completed:** 2026-06-02
- **Code (committed 8e98da5):** `handle_escalation` backfills a blank `moment` (deferral line → reason) so no contentless escalation can be persisted again; regression test in `tests/integration/test_escalation.py` (7 pass). Lifecycle review already persisted (`store.update_escalation_lifecycle`).
- **Data (purged, authorized 2026-06-02):** `DELETE FROM escalation_log WHERE episode_id LIKE 'ep-esc-%' OR moment IS NULL OR btrim(moment)=''` → **30 deleted**; **48 real escalations remain**, **0 empty/orphan left**.
- **Verification:** dry-run before delete (30 matched, 48 real untouched); post-delete recount = 0 orphans. seed_demo's `DELETE FROM escalation_log` already covers all on re-seed.

### CB-20 — Stuck experiments purged + terminal-state guard
- **Completed:** 2026-06-02
- **Code (committed 8e98da5, via CB-15):** a run can no longer linger `running` — the background task settles every run to terminal (`_settle_failed_run` → `rejected` on timeout/crash). Verified `verify-async` → `rejected`.
- **Data (purged, authorized 2026-06-02):** `DELETE FROM experiment WHERE experiment_id LIKE 'DRAFT-%' AND state='running'` → **2 deleted** (the Jun-1 "KB edit · Competitor comparisons" + Jun-2 rebuttals draft). **0 running experiments left** (3 total: rejected/promoted/the settled verify-async).

### CB-10 — Incoherent seed/sim episodes — purged short mock stubs (option A)
- **Completed:** 2026-06-02
- **Decision:** full regeneration (~3,098 paid self-play episodes) DECLINED by user; chose option A — purge the short mock-LLM stubs (no LLM spend).
- **Root cause (for the record):** ~2,625 of 3,206 sim episodes were ≤4-turn MOCK-LLM self-play stubs (e.g. greeting → "My son needs help with SAT math." → "What score…", outcome `enrolled`; or greeting → "I'm done here — I've got to go.", outcome `walked` on a qualified persona). The transcript came from a mock LLM (degenerate), but the OUTCOME fired honestly from the deterministic buy-gate on the persona's true drivers → outcome ≠ transcript. Aggregate KPIs stayed honest (computed from outcomes); only individual short-call review was incoherent.
- **Data (purged, authorized 2026-06-02):** `DELETE FROM episode WHERE channel='sim' AND jsonb_array_length(turns)<=4` → **2,625 deleted** (FK `escalation_log → episode ON DELETE CASCADE`; 0 of the 48 real escalations referenced them, so the queue is untouched). Episodes 4,334 → **1,709** (656 voice · 581 sim · 472 text); 0 incoherent ≤4-turn sims remain.
- **Verification:** post-delete recount confirms 0 sim episodes with ≤4 turns; escalations intact (48); remaining sims are the coherent ≥5-turn (mostly 6–19) real/post-CB-03 runs.
- **Follow-ups:** new sims (CB-03/CB-12 real-brain self-play) are coherent by construction, so this won't recur. If the KPI sample feels thin later, a SMALL coherent regen (option C) is the no-mass-spend path.

### CB-21/22/23/24/25/26/27/28/29 — Wave 2 batch (3 parallel coders + orchestrator)
- **Completed:** 2026-06-03
- **Verification (shared):** `.venv` suite **474 passed / 5 skipped** (≈36 new tests); `web` tsc exit 0; API + worker restarted; integration screenshots (`tests/e2e/verify_wave2.py`, `/tmp/cb_wave2/*`) all PASS, 0 JS errors; a fresh real-LLM sim + a fresh experiment exercised the new paths.
- **CB-22** (voice call not logged) — `worker.py`: `_terminal_outcome_on_disconnect` rewritten + `_agent_acts()` scans EVERY committed act; a disconnect now always settles terminal (escalated→`escalated`, else `released`/`abandoned`; a real close keeps its booking) — never in_progress. Tests: `test_disconnect_outcome.py` (14 cases) + `test_worker.py` (escalate-then-continue). *(Legacy row `ep-eba52675` left in_progress — classifier blocked the one-off repair; all future calls log.)*
- **CB-23** (voice belief) — found ALREADY correct (voice turns persist belief; the call was just unreachable while in_progress — CB-22 restores it); hardened `session.py` + 3 round-trip tests.
- **CB-21** (spinner + streaming) — live monitor shows a "Generating…" spinner during compose + word-by-word reveal; demo SSE emits a `generating` event. Real-voice token streaming DEFERRED (needs a streaming `respond()` in R37-locked `src/core` + LiveKit TTS chunking).
- **CB-24** (failure reason) — `improve.py` background run captures a specific reason (timeout vs exception class); lab card + drawer surface `guardrail_reason`. Verified: `w2-detail` → `rejected` "no significant lift…" shown.
- **CB-25** (experiment detail) — new `GET /api/experiments/{id}` persists per-arm episodes (`{id}::{arm}::{i}`); new `/improve/lab/[id]` page with a "See experiment" button shows both arms' calls (verified 5 champion + 5 challenger), each deep-linking to Review.
- **CB-26** (metric explainer) — a "?" beside each metric expands a value→behavior band chart (human text, no slug).
- **CB-27** (multi-metric) — RunDrawer CSV-like grid (+/− rows); `generator.build_multi_challenger`; `improve.py` `changes` list; R19 single-dimension relaxed for MANUAL experiments only (default preserved).
- **CB-28** (tool-use inspection) — `Turn.retrieved` (schema) populated in `respond()` on `answer_via_kb`, wired through `selfplay.py` + `session.py` Turn construction, exposed in `episode_detail`, rendered as a clickable "N sources" panel in Review (verified `retrieved` key present on all serialized turns, null on non-tool).
- **CB-29** (agent quality) — `nlg.py` hard no-hallucinated-slots rule (never invents a daughter/student/grade not in confirmed slots); `gates.py` `close_floor` backs off a blind/pushy `attempt_close` (paid tier, no discovery, intent at prior); `address_direct_input` for the ignored-question case. Tests incl. an `ep-eba52675`-shaped replay (no "daughter", question acknowledged, no early close). Buy-gate + R37 intact. Verified: fresh sim showed NO invented child.
- **Files:** `worker.py`, `session.py`, `demo_routes.py`, `operate/live/page.tsx`, `improve.py`, `improve/lab/page.tsx` + new `improve/lab/[id]/page.tsx`, `improve-api.ts`, `improve-types.ts`, `generator.py`, `schema.py`, `respond.py`, `gates.py`, `nlg.py`, `operate.py`, `operate/review/[id]/page.tsx`, `operate-types.ts`, `selfplay.py` + tests.
- **Follow-ups:** CB-21(b) real-voice token streaming; legacy `ep-eba52675` row repair (optional); `experiment_record_from` collapses a settled multi-metric `declared_diff` to `["multi"]` (cosmetic; `diff_description` still lists all).

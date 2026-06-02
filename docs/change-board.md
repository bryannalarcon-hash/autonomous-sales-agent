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

### CB-04 — Humanize two remaining raw-slug leaks (KPI cohort + Approvals diff)
- **Type / Surface / Size:** bug · `/operate/kpi` · `/improve/approvals` · S
- **Prereqs:** —
- **Important files (candidates):** `web/app/operate/kpi/page.tsx` (the `COHORTS` selector ~line 39 + the empty-state line), `web/app/improve/approvals/page.tsx` (`diff_description` render ~line 96/232); possibly the backend `diff_description` text in `src/api/improve.py` / `src/loop`.
- **Current:** the KPI **cohort** dropdown renders the raw slug `held_out` (the *version* selector is already humanized; the cohort one isn't); the Approvals "diff_description" sentence embeds the raw threshold key, e.g. *"Raise max_concession_band 0.15 → 0.22"*.
- **Desired:** humanize both via `web/lib/labels.ts` (`held_out` → "Held-out"; `max_concession_band` → "Pricing concession band") — no raw snake_case slug in operator-facing text (the no-internal-index rule).
- **Acceptance:** a Playwright text scan of `/operate/kpi` (cohort dropdown open) and `/improve/approvals` finds no `held_out` / `max_concession_band` / other snake_case slug rendered.
- **Refs:** QA round 7 findings; `web/lib/labels.ts` (`populationLabel`/`dimensionLabel` already exist).

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

### CB-05 — Per-turn belief DELTA in Call Review
- **Type / Surface / Size:** feature · `/operate/review` · S
- **Owner:** coder-belief-ui (frontend)
- **Started:** 2026-06-02
- **Prereqs met?:** yes (the data already ships)
- **Why:** the DST computes `<driver>_velocity = driver(t) − driver(t−1)` and `_belief_to_dict` already ships it as `trends`, and `operate-types.ts` types it — but the Review belief panel renders only VALUES, not how the read MOVED each turn. Operators want to see momentum ("Trust ↑ +0.12 since last turn").
- **Plan (checklist):**
  - [ ] Render each driver's `trends` velocity next to its value on the Review belief panel (↑/↓ signed, colored; ~0 shown as flat).
  - [ ] tsc clean; Cadence tokens only, no new visual language.
  - [ ] Screenshot-verify the Review page (open a real episode) — layout intact, no overflow/overlap.
- **Files being touched:** `web/app/operate/review/[id]/page.tsx`, `web/lib/operate-types.ts` (trends already typed), `web/app/cadence.css` if needed.
- **Notes / blockers:** data is already in the payload — pure frontend.

### CB-06 — Agent-estimate vs prospect-TRUTH belief panel (self-play / twin only)
- **Type / Surface / Size:** feature · `core` (`src/sim`) · `api` · `/operate/review` · M
- **Owner:** coder-prospect-truth (backend) + coder-belief-ui (frontend)
- **Started:** 2026-06-02
- **Prereqs met?:** yes
- **Why:** the UI shows only the agent's BELIEF (its estimate of the prospect); for self-play / digital-twin episodes we also have the prospect's TRUE hidden drivers (the `on_turn` hook captures them) but never persist/surface them. Showing agent-estimate vs prospect-truth per turn + the gap is the "is the agent reading the room correctly?" / sim-to-real belief-accuracy view. Real calls have NO prospect truth → panel hidden there.
- **Plan (checklist):**
  - [ ] Backend: persist the prospect's true-driver trajectory per turn on sim/twin episodes (e.g. `episode.metrics["prospect_trajectory"] = [{turn, drivers}]`); expose it in the episode-detail API.
  - [ ] Frontend: a Review panel (only when prospect_trajectory present) showing, per turn, agent-estimate vs prospect-truth for the OVERLAPPING drivers (trust / urgency / purchase_intent) + the signed gap; prospect-only drivers (need / budget / patience) shown as context.
  - [ ] Tests: backend persistence test; tsc clean.
  - [ ] Screenshot-verify the Review page with a sim episode — panel renders, no UI break; and with a real (voice) episode — panel correctly ABSENT.
- **Files being touched:** `src/sim/selfplay.py` (capture prospect.state per turn), `src/api/operate.py` (expose), `web/app/operate/review/[id]/page.tsx`, `web/lib/operate-types.ts`.
- **Notes / blockers:** driver-key MISMATCH — prospect true drivers are trust/need/urgency/purchase_intent/budget/patience; agent belief drivers are trust/need_intensity/price_sensitivity/urgency/purchase_intent/bail_risk. Compare only the overlapping keys; never render a raw slug. CB-05 + CB-06 BOTH touch the Review page — same frontend owner builds both to avoid a collision.

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

_(empty — fill as items finish)_

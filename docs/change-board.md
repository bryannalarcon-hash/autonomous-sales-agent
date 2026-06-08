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


### CB-85 — Non-sequitur false-pivot + price persistence for the combative persona (brain quality)
- **Type / Surface / Size:** bug · `core` (`gates`/`nlg`) · M · MEDIUM
- **Prereqs:** —
- **Current (QA10-chat):** (a) "you guys see the storm coming tonight?" → "Sounds good — let me get you scheduled" (ignored + misread as scheduling consent — no brief human ack then pivot); (b) the COMBATIVE dad's terse price re-asks ("Price. Just the number.") still got deflection + a FALSE "I don't have access to the exact pricing tiers right now" (the cooperative mom got "$300–600" on first ask) — CB-77's terse-recurrence didn't fire for this persona / the agent claimed no access to data it has.
- **Desired:** a non-sequitur gets a one-clause ack before the pivot, never misread as consent; CB-77 terse-price forcing fires regardless of adversarial tone; the agent never claims it lacks pricing it can ground.
- **Acceptance:** scripted replay of the storm turn + the 3× terse dad asks; price range surfaces by ask 2; no "I don't have access" when KB carries the range.
- **Refs:** QA10-chat SEV-2 (non-sequitur), SEV-3 (price determinism); builds on CB-62/77.



### CB-86 — Checkpoint-persist the lead at contact-capture (not only at /end)
- **Type / Surface / Size:** change · `api` (`demo_routes` live_upsert + persistence) · M
- **Prereqs:** CB-82 (done — booked closes now finalize via /end)
- **Current:** CB-82 made any committed close fire /end → the lead persists then. But a chat that captures a phone and is ABANDONED before any terminal close (user closes the tab) still loses the lead — `_live_upsert` writes `lead_phone_hash=None` every turn. Deferred from CB-82 as invasive (raw_phone into the live_upsert hook signature + a mid-call Lead FK write before the episode row exists).
- **Desired:** when contact is captured mid-call, write/upsert a minimal Lead (phone as sha256 only) so an abandoned-after-booking chat keeps it; FK ordering handled (lead before episode ref).
- **Acceptance:** a chat that gives a phone then abandons (no /end) → the lead exists with the sha256 hash; no raw phone anywhere; live-call path unaffected.
- **Refs:** CB-82 follow-up.

### CB-69 — Text-channel escalate: don't ask a question the session can't hear answered
- **Type / Surface / Size:** design/bug · `core` (`nlg` escalate guidance) · `api` (`demo_routes` done semantics) · S–M
- **Prereqs:** — (user decision wanted on the desired semantics)
- **Important files (candidates):** `src/core/nlg.py` (`_ACT_GUIDANCE["escalate"]` — the realized line collected contact info), `src/api/demo_routes.py` (escalate ⇒ done ⇒ input disabled on the SAME turn).
- **Current (CB-61 replay):** on a terminal escalate, NLG spoke "all I need is a good phone number and email so our specialist can reach you Thursday…— what's the best number?" and the demo session ENDED on that turn — the caller could never answer. The handoff sounded great and then hung up on its own question.
- **Desired (pick one):** (a) escalate's realized line must be self-contained on text (no trailing question; offer the callback using already-known contact info or none), or (b) the demo keeps the session open exactly one more turn after an escalate that asked for contact, committing the answer to the lead record before ending. Voice path unaffected either way (the worker keeps the room open).
- **Acceptance:** replay turn-6 shape: either no trailing question on the terminal line, or the caller's number is captured into the lead before the session closes; no session ends on an unanswered direct question from the agent.
- **Refs:** CB-61 replay transcript /tmp/qa7/cb61_replay_transcript.txt (turn 6); CB-68 Done entry.


### CB-79 — One-time reset of the dev DB's polluted champion (needs user authorization — DB write)
- **Type / Surface / Size:** chore · `db` (dev only) · S
- **Prereqs:** CB-73 (done — stops further pollution)
- **Current:** the dev DB's champion is a TEST CHALLENGER `champion_v0__playbooks_discovery_sequence__7` (left by DB-gated tests BEFORE CB-73's teardown landed); ~758 lineage rows, ~4900 episodes accumulated. CB-73 froze the counts going forward but a fix may not mutate existing rows without authorization. CB-73's drawer mismatch line currently fires on 9 real experiments because of this.
- **Desired (USER DECISION):** authorize a one-time dev-DB cleanup — reset is_champion to a real version (or re-promote champion_v0 cleanly) and optionally prune the test lineage rows + orphan episodes. Dry-run first; destructive → explicit go required.
- **Acceptance:** dev champion is a real version; the CB-73 mismatch line stops firing on legitimate records; counts sane.
- **Refs:** CB-73 Done entry; orchestrator DB query 2026-06-08.

### CB-01 — Active-calls queue with per-call take-over
- **Type / Surface / Size:** feature · `/operate/live` · `api` · `voice-worker` · L
- **Prereqs:** — *(no other CB blocks it; internally it has 3 work units that order themselves: the list endpoint → the queue UI → the take-over mechanism. Split into `CB-01.a/.b/.c` when pulled.)*
- **Important files (candidates):** `src/api/operate.py` (new "list active calls" endpoint alongside `/api/live`; today `live_ep` does `list_episodes(limit=1)`), `web/app/operate/live/page.tsx` + `web/lib/operate-api.ts` (queue UI + fetcher), `src/voice/worker.py` + `src/api/demo_routes.py` (the real take-over / operator barge-in into the call's LiveKit room).
- **Current:** `/api/live` returns only the single most-recent episode, so `/operate/live` shows ONE call. The backend already runs concurrent calls (each phone call = its own LiveKit job subprocess; each `/demo` session = its own `session_id` + `VoiceSession`), but there's no way to enumerate them. The "Take over" button on `/operate/live` is honestly **disabled** because operator barge-in isn't wired (see Done CB-history / commit `0bf1f93`).
- **Desired:** an operator sees a **queue/list of ALL active calls** (each in-progress, non-stale call — phone + web), can select any to watch its live transcript + belief, and can **take over any one** of them — a real mechanism that connects the operator into that call's LiveKit room (or transfers the call to a human), with the agent gracefully yielding. Must respect R37 (don't fork the brain), consent/recording state, and the stale-call guard already in `live_snapshot` (a 0-turn in-progress call older than `LIVE_STALE_SECONDS` is not "active").
- **Acceptance:** with N concurrent active calls, the live page lists N entries (newest-first, each with caller/persona + stage + trust/bail signal); selecting one streams its live transcript; "Take over" on an entry connects the operator audio into that LiveKit room (verified with a real or stubbed second participant) and the AI yields; an integration test covers the multi-active list endpoint (≥2 active calls → ≥2 entries, stale ones excluded) and the take-over hand-off. No raw episode/room id rendered as a primary label.
- **Refs:** `src/api/operate.py::live_snapshot` + `live_ep`; LiveKit Agents room/participant model + SIP dispatch (`scripts/setup_sip_dispatch.py`, rule `SDR_hKYQYAwz96uA`); the disabled take-over control (`web/app/operate/live/page.tsx`); R37 in `src/core/respond.py`.
- **Take-over spec (user, 2026-06-02) — refines CB-01.c:** on "Take over", the AI must (1) be INTERRUPTED mid-conversation and (2) speak a DEFAULT TTS hand-off line (e.g. "Let me bring in a specialist to help — one moment. They may type some of their responses, so there might be a brief pause between answers."). Then the OPERATOR talks into their own device (browser mic / WebRTC into the call's LiveKit room) and is heard by the caller through the phone — operator audio is published as a room participant, NOT a dial-in (presume the operator is NOT calling from their own phone number, so no SIP bridge / caller-ID concerns). The agent stops auto-generating; the operator may ALSO TYPE responses that are TTS'd to the caller ("the agent might be typing their responses"). So take-over = interrupt + disclosure TTS + operator mic→room audio + optional operator-typed→TTS, with the AI yielded. Consent/recording state must carry over; respect R37 (don't fork the brain — the brain just stops).

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

### CB-82 — Conversion/lead loss: only enrollment closes finalize (callback/consult/trial vanish)
- **Type / Surface / Size:** bug · `api` (`demo_routes` done-condition + persistence) · M · HIGH
- **Completed:** 2026-06-08
- **Files changed (actual):** `src/api/demo_routes.py` (done-condition at ~770: expanded from `attempt_close@enrollment` to ALL `attempt_close` tiers; file-top header updated), `tests/e2e/test_demo_call.py` (parametrize corrected + 11 new CB-82 regression tests: done flag × 4 tiers, episode outcome × 4 tiers, phone sha256 compliance, non-close purity, consent gate unchanged), `tests/e2e/test_cb63_chat_stream.py` (parametrize updated with CB-82 tiers).
- **What changed:** one-line done-condition fix: `committed.decision.act in ("disqualify", "escalate", "attempt_close")` — dropped the `and tier == "enrollment"` guard so any committed close (callback/consultation/trial/enrollment) sets done=True, causing the client to fire /end which calls the persist hook and saves the episode + lead. Phone stored as sha256 only (the persist path was already correct; the capture hook test confirms it). Checkpoint-persist (lead at contact-capture) deferred as CB-86 (would require wiring raw_phone into the live_upsert signature, new lead FK write mid-call — invasive to the per-turn path).
- **Verification:** 962 passed / 5 skipped (was 946). 16 new tests all green; zero regressions. Compliance assertion: `test_cb82_phone_stored_as_sha256_only_on_booked_close` serializes the full episode to JSON and asserts the raw phone never appears; `lead_phone_hash` is the sha256.
- **Constraints checked:** buy-gate purity untouched (done only gates finalization; the commit decision is buy_gate in src/sim/prospect.py — unmodified); consent gate unchanged (test confirms 409 before consent regardless of close tier); R42 phone-hash-only compliance confirmed by test assertion on the episode JSON. `_finalize_auto_demo` already handled all tiers (derive_outcome call) — no change needed there.
- **Follow-ups / known gaps:** CB-86 (checkpoint persist — lead/phone saved at contact-capture moment, not just at /end, so an abandoned-after-booking chat keeps the lead even if the user closes the tab before /end fires). CB-69 (escalate + trailing question still open).

### CB-83 — CRITICAL: contact-name extractor matched "it's free" (CB-76 follow-up)
- **Type / Surface / Size:** bug · `core` (`dst`) · S · CRITICAL (regression from the shipped CB-76)
- **Completed:** 2026-06-08
- **Files changed (actual):** `src/core/dst.py` (`_CONTACT_NAME_RE` rebuilt: case-sensitive name group `(?-i:([A-Z][a-z]+))`, dropped the `it's <X>` intro form, `_NAME_STOPWORDS` negative-lookahead; `update()` gate only sets `contact_just_captured` on an explicit-intro name OR a co-occurring phone/email), `tests/unit/test_cb7677_adversarial.py` (+21 tests).
- **What changed:** the shipped CB-76 contact-ack matched any lowercase word after "it's"/"I'm" as a name — live, "Guarantee me a B or it's free" → "Got it, Free — I've noted that down", swallowing the guarantee objection and addressing the prospect as "Free". Now only a capitalized proper noun via an explicit intro (or contact alongside a phone/email) captures.
- **Verification (orchestrator-spot-checked):** exact QA utterance + "it's free/fine", "I'm worried/looking" → no name; "I'm Dana Reyes 555-…", "My name is Karen", "I'm Dana" → captured. Adversarial 43; full repo 983p/5s (.venv) + 997p (system); zero regressions.
- **Constraints checked:** Karen replay + qa3chat conv-2 contact ack preserved; R37/grounding/CB-61 invariants green.
- **Follow-ups / known gaps:** none.

### CB-76 — Listening v2: disjunctive/plural windows, deadline forms, never-deny, contact ack
- **Type / Surface / Size:** bug · `core` (`dst`, `nlg`) · M
- **Completed:** 2026-06-08 (2 rounds + adversarial verifier)
- **Files changed (actual):** `src/core/dst.py` (plural/disjunctive `_CALLBACK_WINDOW_RE` + `_normalize_plural_day`; plural branch GATED on `_SCHEDULING_CUES_RE` so venting/past-habit days don't lock — round-2 blocking fix; `_DEADLINE_CONTEXT_RE` widened with past-tense activity words; `_TIMELINE_RE` "end of the month/semester", "before finals", etc.; `_MEMORY_CHECK_RE` requiring a recall referent; contact name/phone/email detection → `contact_just_captured`), `src/core/nlg.py` (`_NEVER_DENY_NOTE`, `_CONTACT_ACK_NOTE`), `tests/unit/test_cb76_cb77.py` (65) + `tests/unit/test_cb7677_adversarial.py` (22).
- **What changed:** the CRITICAL gaslighting bug fixed at BOTH layers — extraction now captures "Wednesdays or Fridays after 4"; and even when extraction misses, NLG is forbidden to deny the caller said something (hedges honestly instead — verified it does NOT force a false claim of having info it lacks). Deadlines extracted + acknowledged; captured contact gets a one-line ack on any act. Round-1 over-triggered the plural-day extractor (would fabricate "I have Friday noted" from "Fridays are crazy") — caught by the verifier, fixed with the scheduling-cue gate.
- **Verification:** 87 item+adversarial tests; invariant set 167; full suites 946p/5s (.venv) + 960p (system); genuine disjunctive/singular cases regression-checked.
- **Constraints checked:** R37; no LiveKit in src/core; never-deny vs honest-lacking tension resolved correctly.
- **Follow-ups / known gaps:** real-model echo/ack/hedge phrasing → paid replay (QA round 3).

### CB-77 — Price persistence: terse re-asks trigger the answer; range carries numbers
- **Type / Surface / Size:** bug · `core` (`gates`, `nlg`) · S–M
- **Completed:** 2026-06-08
- **Files changed (actual):** `src/core/gates.py` (`_TERSE_PRICE_REASK_RE` + `_has_prior_price_inquiry`; `_question_recurs` short-circuits true on an intent-keyed terse price re-ask — no content-word overlap needed — feeding the existing `address_direct_input`→`answer_via_kb` path), `src/core/nlg.py` (`_BUDGET_CONCERN_NOTE` demands the KB floor–ceiling range, forbids "a few hundred" when facts carry numbers).
- **What changed:** terse re-asks ("just give me the number") after a prior price inquiry now force the KB-grounded answer (the verbose Karen asks already worked; the terse skeptic-dad asks didn't). Grounding guard (CB-53) stays the enforcement — no figures in instructions.
- **Verification:** 18 tests; terse-non-price re-ask confirmed NOT hijacked (verifier); suites green as above.
- **Follow-ups / known gaps:** real-model compliance with the explicit range → paid replay.

### CB-84 — CB-NN internal index leaks into operator call-review rationale
- **Type / Surface / Size:** bug · `web` (review) · S
- **Completed:** 2026-06-08
- **Files changed (actual):** `web/lib/labels.ts` (`humanizeRationale` strips `(CB-NN …)` parentheticals + inline `(CB|D|W|S|P|R)-?\d+` tokens; raw rationale untouched on record/logs).
- **What changed:** "...before any close attempt (CB-48 directness)" no longer leaks the CB-48 index into the observable operator review.
- **Verification:** leak sweep asserts no CB-NN in operator-review text + unit test; 28 e2e green live.

### CB-81 — Operate label leaks: channel slug + stub rationale in review
- **Type / Surface / Size:** bug · `web` (operate) · S
- **Completed:** 2026-06-08
- **Files changed (actual):** `web/lib/labels.ts` (`channelLabel()` text→"Web chat"/voice→"Web voice"/phone→"Phone"; `humanizeRationale` "sim-harness decision"→"Simulated training call"), `web/app/operate/calls/page.tsx` (both channel renders use channelLabel).
- **What changed:** CHANNEL no longer shows raw `text`; seeded-stub rationale reads "Simulated training call".
- **Verification:** leak sweep extended (raw_channel_text + sim_harness patterns); 28 e2e green; tsc clean.
- **WAIVED (refuted vs code+API by orchestrator):** "Open full call review empty href" (button onClick=router.push); "Reviewed/Resolved render 0" (API 8/8; Resolved rendered 8 — no refetch await); "54-vs-41 cap" (API 54, no slice); "real-calls label disappears" (always renders); "no row-cap disclosure" (fires when capped); "no escalation→call link" (drawer has View-call).

### CB-80 — Lab display residue (no-result guardrail chip, zombie-draft drawer, reject-why, footer)
- **Type / Surface / Size:** bug · `web` (lab) · S
- **Completed:** 2026-06-08
- **Files changed (actual):** `web/app/improve/lab/page.tsx` (L1 guardrail chip gated `&& !noRes`; L2 drawer header uses `effectiveState`; L3 rejected/blocked always renders a reason; L4 baseline note → "the live champion's saved settings aren't available here — see Versions"; L5 sticky action footer).
- **What changed:** no false guardrail chip on no-result cards; zombie drafts read Draft everywhere; no silent rejection; plain baseline note; Run button always visible.
- **Verification:** qa9fix_lab.py 14/14 live; full suite green.
- **WAIVED (refuted by orchestrator):** "Run button z-index intercept" (elementFromPoint returns the button when scrolled); "See experiment → lab root / no transcripts" (CB-55+QA9 prove it for SETTLED runs; QA10 clicked a RUNNING card); "Ladder/CI no tooltips" (5 title= tooltips exist).

### CB-75 — Operate polish round 2 (label dup, hint→affordance, counts, sort, leaks)
- **Type / Surface / Size:** bug · `web` (operate) · M
- **Completed:** 2026-06-08
- **Files changed (actual):** `web/components/cadence/DashboardShell.tsx` (drop the literal "Champion " prefix that doubled `versionLabel`; badge listens for an escalation-count event so it can't disagree with the queue), `web/app/operate/escalations/page.tsx` (cohort hint → `<Link href="/operate/calls?cohort=all">`; dispatches the count event), `web/app/operate/calls/page.tsx` (reads `?cohort=all`; `StaticTh` for non-sortable cols; sort glyph contrast; `shortId()` + copy-full-id affordance for CALL ID), `web/app/operate/live/page.tsx` (`humanizeRationale` on the belief panel), `web/lib/labels.ts` (`establish_who_first`/`no_repeat_discovery` gate labels), `tests/e2e/qa9fix_ops.py` (NEW).
- **What changed:** the eight QA9-ops items — two verified as already-correct (75-vs-76 count is one array; empty-filter already shows the no-match card) with tests pinning them, six fixed.
- **WAIVED with evidence:** "review pages 404 on direct URL" — disproven live by the orchestrator (200 + render for real and bogus ids; agent artifact).
- **Verification:** 44 passed / 1 self-skip (fallback path needs an empty champion); qa7 regressions 31 green.
- **Follow-ups / known gaps:** none.

### CB-74 — KPI page never defaults into an empty population
- **Type / Surface / Size:** bug · `web` (KPI) · S–M
- **Completed:** 2026-06-08
- **Files changed (actual):** `web/app/operate/kpi/page.tsx` (auto-select the live champion only when `count>0`; else fall back to the most-populated version + render "The live champion (X) has no recorded calls yet — showing Y."; explicit empty selections gain a "try another version/cohort" hint).
- **What changed:** the CRITICAL — a fresh /operate/kpi load no longer shows an unexplained "No episodes match v1" empty page when the live champion has zero matching episodes (the CB-73 hash-churn symptom).
- **Verification:** qa9fix_ops.py (the empty-champion hint test self-skips when the live champion has episodes — exercised via a seeded fake in unit coverage); suite green.
- **Follow-ups / known gaps:** once CB-79 cleans the polluted champion, the live champion will have episodes and the happy path renders directly.

### CB-73 — Integration tests stop polluting the dev DB (lineage/episodes frozen)
- **Type / Surface / Size:** bug · `tests` (conftest) · `api`+`web` (baseline-mismatch copy) · M
- **Completed:** 2026-06-08
- **Files changed (actual):** `tests/conftest.py` (snapshot/restore teardown via `_force_fresh_pool()` replacing the four `close_pool()` calls — guaranteed cleanup of rows DB-gated tests create, chosen over schema isolation to avoid touching production store code), `src/api/improve.py` (`store_champion_version` on GET /api/experiments), `web/lib/improve-types.ts` + `web/app/improve/lab/page.tsx` (drawer renders a one-line baseline-mismatch note when an experiment's champion ≠ the store champion).
- **What changed:** DB-gated integration tests no longer accrete lineage/episode rows into shared dev Postgres — PROVEN: two back-to-back full integration runs left version_lineage at 758 and the champion row unchanged (orchestrator-verified). The lab drawer now explains a baseline mismatch in plain English.
- **Verification:** integration 159p/5s twice with identical before/after DB counts; full suite 680p/5s.
- **Constraints checked:** fix introduces NO destructive mutation of existing rows (only prevents new pollution; test_persistence's +3 live rows are intentional and isolated).
- **Follow-ups / known gaps:** the EXISTING polluted champion (a test challenger) predates this fix → CB-79 (needs user auth for the DB write).

### CB-72 — Lab UX clarity batch (cost estimate, disabled-button hint, toast, tooltips, small-n)
- **Type / Surface / Size:** change · `web` (lab) · S–M
- **Completed:** 2026-06-08
- **Files changed (actual):** `web/app/improve/lab/page.tsx` ($0.025/call estimate constant → "≈ $X.XX (approximate)"; disabled-button helper text; completion toast via the fidelity notice idiom; Ladder/Significance/CI `title=` tooltips; small-n caution chip at n<10; `data-kind="exp-card"` to dedupe the render).
- **What changed:** the QA9 Sev-3 friction items resolved; the "double render" was a test-selector artifact (`.card` + `.card-pad`), now disambiguated with a data attribute.
- **Verification:** qa9fix_lab.py (10) + leak sweep (12) green live; full suite 855p/5s.
- **Follow-ups / known gaps:** $ estimate uses a hardcoded per-call constant (matches measured ~$0.025); drift if model mix changes.

### CB-71 — One truthful display state machine for experiment records (incl. legacy rows)
- **Type / Surface / Size:** bug · `web` (lab) · S–M
- **Completed:** 2026-06-08
- **Files changed (actual):** `web/app/improve/lab/page.tsx` + `[id]/page.tsx` (`isNoResult`/`effectiveState` helpers; guardrail-regression chip on card + drawer; `humanizeGuardrailReason` on every reason surface; no-result records suppress metrics; zombie draft renders as Draft).
- **What changed:** one state machine for all records (table in the coder report): guardrail regression now visible on the card, legacy `challenger_better is False` translated everywhere, the timeout card no longer shows fabricated metrics, zombie "Running+Draft" renders Draft only.
- **Verification:** qa9fix_lab.py asserts each legacy-card shape; full suite 855p/5s.
- **Follow-ups / known gaps:** no-result detection matches two known legacy reason strings explicitly (commented) — a stored-state flag would be cleaner if the schema gains one.

### CB-70 — Blank/raw experiment name falls back to the humanized description
- **Type / Surface / Size:** bug · `web` (lab list/detail) · S
- **Completed:** 2026-06-08
- **Files changed (actual):** `web/app/improve/lab/page.tsx` (`titleOf`) + `[id]/page.tsx` (`titleOfDetail`): `RAW_MUTATION_RE` detects a raw mutation string persisted as the name and falls back to `humanizeDiffDescription` → `changeLabel`; `tests/e2e/qa7_cb65_leaksweep.py` extended with a `-> [` title assertion.
- **What changed:** the Sev-1 — the `name` field actually held the raw `reorder discovery_sequence -> [...]` string (not blank); now detected and humanized on card/Past/detail.
- **Verification:** leak sweep 12/12 incl. the known QA9 record; full suite 855p/5s.
- **Follow-ups / known gaps:** the raw string is still STORED as the name (display-layer fix); a future run-creation fix could store the humanized name (low priority).

### CB-65 — Viewer-facing leak sweep (slugs, IDs, debug artifacts)
- **Type / Surface / Size:** bug · `web` (all surfaces) · M
- **Completed:** 2026-06-08
- **Files changed (actual):** `web/lib/labels.ts` (`kbVersionLabel`, `ladderTierLabel`, `humanizeGuardrailReason`; extended `humanizeDiffDescription`/`humanizeRationale`), applied across demo header (debug toggle REMOVED), DashboardShell chip, calls/review/live/versions/kb pages, lab list + detail; `tests/e2e/qa7_cb65_leaksweep.py` (NEW — 11-test playwright sweep over every surface, the standing regression), `tests/unit/test_cb65_labels.py`.
- **What changed:** 11 leak classes found live and all translated at the render layer: `kb_v0` → "Knowledge base v0" everywhere; `tier 0/1` → ladder labels; `challenger_better is False` → plain English; config dumps → "New sequence: Grade level, Goal, …"; Python kwargs stripped from tactic notes; consumer debug toggle gone. Zero allowlist exceptions needed.
- **Verification:** sweep AFTER = zero hits (re-run by orchestrator: 11/11); full suite 869 passed; every prior qa7 regression script re-run green.
- **Constraints checked:** no DB writes; src/core untouched; raw values kept in data fields for automation.
- **Follow-ups / known gaps:** none — the sweep runs as a permanent regression test.

### CB-68 — Ground the LLM's concession proposal (phantom discount terminally escalated a healthy close)
- **Type / Surface / Size:** bug · `core` (`gates`) · S
- **Completed:** 2026-06-08 (found by the CB-61 replay, fixed by the orchestrator)
- **Files changed (actual):** `src/core/gates.py` (`_price_context_live` + strip step at `apply_gates` entry), `tests/unit/test_cb68_concession_grounding.py` (NEW, 6), `tests/unit/test_policy.py` (band test re-spec'd: concession now requires live price context — the no-context arm is CB-68's strip).
- **What changed:** replay turn 6 ("Thursday works — what do you need?") saw the policy LLM hallucinate `concession>band`; `escalation_triggers` honored it ungrounded → terminal escalate mid-close. Now a concession is acted on ONLY inside live price talk (price_inquiry / open price-affordability objection / recorded refusals); phantom fractions are stripped. The R10 over-band→defer design is unchanged inside genuine price context.
- **Verification:** 6 new tests incl. the live failure shape; full suites 611p/5s + 625p.
- **Constraints checked:** R10 band rule intact in price context; same grounding discipline as CB-36/CB-67 (all LLM labels with destructive power are now grounded).
- **Follow-ups / known gaps:** the *separate* lifecycle question (escalate handoff line asked for contact info, then the text session terminated before the answer) is logged as CB-69 in To Do — a design decision, not auto-fixed.

### CB-67 — Ground the LLM's human_request label (hallucinated terminal escalate on turn 1)
- **Type / Surface / Size:** bug · `core` (`dst`) · S
- **Completed:** 2026-06-08 (found and fixed by the orchestrator during the CB-61 paid replay)
- **Files changed (actual):** `src/core/dst.py` (`_HUMAN_REQUEST_CUES_RE` + `_ground_human_request` — the human_request analogue of CB-36's objection grounding; applied at the LLM-intent fusion seam), `tests/unit/test_cb67_human_request_grounding.py` (NEW, 6).
- **What changed:** live replay caught gpt-5-nano labeling "Can you tell me how this works?" as `human_request`; the fusion took the LLM act UNGROUNDED and `escalation_triggers` (correctly) treats an explicit human request as terminal → a brand-new call ended on turn 1 with a specialist-callback. Pre-existing probabilistic landmine (not a tonight regression — CB-36 grounded objections but never human_request, the one label that can END a call). Now an LLM human_request needs a lexical person/speak cue; ungrounded ones degrade to question/statement (recoverable). Genuine phrasings ("talk to a real person", "someone I could speak with", "representative") still pass.
- **Verification:** 6 new tests incl. the exact live utterance; DST + CB-61 adversarial/listening + CB-50 escalate-guard suites green (81); full suite 605p/5s.
- **Constraints checked:** legitimate escalation paths intact (cue-bearing requests escalate; CB-50 tests green).
- **Follow-ups / known gaps:** none — same grounding discipline now covers both LLM labels with destructive downstream power.

### CB-59 — One champion: experiments + KPI target the live champion honestly
- **Type / Surface / Size:** bug/change · `api` (`improve`, `server`) · `loop` (`promotion`) · `config` · `web` (KPI) · M
- **Completed:** 2026-06-07 (2 rounds — round 1 had 2 orchestrator-caught defects)
- **Files changed (actual):** `src/api/improve.py` (`resolve_champion_config(store)` — THE one championship authority: store champion's materialized content when loadable; HONEST fallback to bootstrap under its TRUE label, never relabeling content), `src/api/server.py` (provider injected only for explicit test configs — round 1 was inert in prod), `src/config/settings.py` (`save_config()`), `src/loop/promotion.py` (promotion MATERIALIZES the new champion's yaml + sets `config_ref` — root fix; best-effort, never blocks promotion), `web/app/operate/kpi/page.tsx` + `web/lib/operate-api.ts` (selector lists store versions incl. the live champion), `tests/e2e/test_cb59_champion.py` (17), `tests/integration/test_loop.py` (tmp-dir fixture so DB-gated promote tests stop writing yamls into the repo).
- **What changed:** round-1 defects: (1) server.py always injected static v0 → fix dead in prod; (2) fallback stamped v0 CONTENT with the store champion's label — a substance lie (verified: `config_ref` is a pointer, set None at promotion; live v1 unmaterializable). Round 2: honest semantics — with the legacy v1 champion, new records truthfully say `champion_v0` until a champion is promoted through the fixed path (which now persists content).
- **Verification:** 17 item tests (incl. prod-wiring regression + substance assertion on a mutated threshold value); full suite 806p/5s; stray test yamls cleaned + prevented.
- **Constraints checked:** run route never crashes on store failure; historical records render their stored versions unchanged.
- **Follow-ups / known gaps:** approval-queue promotions (`approve_ep`) still can't materialize (record lacks the config body — how v1 got stranded); lineage seed rows churn hashes per suite run (pre-existing). Both noted for a future item if the lab's human-approval path gets exercised.

### CB-66 — Operate polish batch (sorting, filters, durations, labels)
- **Type / Surface / Size:** bug · `web` (`/operate`) · `api` (labels/serialization) · M
- **Completed:** 2026-06-07
- **Files changed (actual):** `src/api/labels.py` (`canonical_outcome_key()` + `OUTCOME_KEY_ALIAS` — callback_scheduled/callback_booked unified; `FILTERABLE_OUTCOME_KEYS`), `src/api/operate.py` (`_compute_duration_ms()` fallback from turn latencies — display-only, no DB writes; canonical outcome in summaries; alias-aware filter), `web/app/operate/calls/page.tsx` (SortTh clickable sort + aria-sort; 13 complete outcome filters; pluralization; ARCHETYPE dup column removed), `web/app/operate/review/[id]/page.tsx` (avatar badge labeled; proactive-escalation annotation), `web/app/operate/escalations/page.tsx` (timestamps), `tests/e2e/test_cb66_operate_polish.py` (NEW, 20), `tests/e2e/qa7_cb66.py` (NEW, 15 live).
- **What changed:** all 10 QA6 polish items addressed; 2 judged no-fix with evidence ("Show sample call" was already wired — stale QA observation; the 0:09 "Consultation booked" is seeded stub data already badged via CB-60, and `derive_outcome` is sound for real calls). Escalation chip timing is genuinely proactive architecture (escalation_imminent fires before the explicit ask) — annotated honestly at display layer rather than touching src/core.
- **Verification:** 20 + 15 new tests; operate suites 69 passed; full suite 820 passed.
- **Constraints checked:** no DB writes; src/core untouched; one human name per outcome concept.
- **Follow-ups / known gaps:** duration fallback activates for live rows after the API restart (queued).

### CB-63 — Stream the demo chat (SSE) + typing indicator
- **Type / Surface / Size:** change · `api` (`demo_routes`) · `web/components/demo` · M
- **Completed:** 2026-06-07
- **Files changed (actual):** `src/api/demo_routes.py` (dual-mode `/api/chat`: SSE via the existing `demo_auto_stream` StreamingResponse idiom + unchanged buffered JSON fallback; shared `_post_turn_hooks` closure for commit/escalation/live-upsert), `web/components/demo/TextConsole.tsx` (progressive token render + immediate typing indicator; CB-54 `/end`-on-done + 409 re-consent preserved), `tests/e2e/test_cb63_chat_stream.py` (NEW, 16).
- **What changed:** chat now streams the brain's tokens (respond_stream reused — no brain fork, R37 intact). Consent 409 + minor gate fire BEFORE any token; timing stamps/turn persistence/escalation+PII scrub/CB-52 cost capture all proven preserved on the SSE path.
- **Verification:** 16 new contract tests incl. concat(chunks)==buffered parity + 409-before-first-token; CB-54's 17 tests stay green; full suite 562p/5s.
- **Constraints checked:** R37 parity; consent/PII compliance on the streamed path; cost accounting fires via the existing `_sse_usage` path.
- **Follow-ups / known gaps:** live progressive-render + typing-indicator eyeball needs the orchestrator's API restart (queued before the blind QA round).

### CB-64 — Repetition residual in live chat: verbatim pitch + triple callback-pitch
- **Type / Surface / Size:** bug · `core` (`nlg`, `respond`) · S–M
- **Completed:** 2026-06-07
- **Files changed (actual):** `src/core/nlg.py` (`_near_verbatim_repetition` — deterministic >8-shared-content-word check vs own last lines; `_REPETITION_RETRY_RULE`; `realize(repetition_retry=)`), `src/core/respond.py` (`_derepetition_blocking_reply` after the grounding guard — regenerate once then accept; stream path flag-only, mirroring CB-53 discipline), `tests/unit/test_cb64_repetition_guard.py` (NEW, 13).
- **What changed:** post-NLG deterministic near-verbatim guard (chosen over meta topic-tracking — objective count, no LLM judgment). Orchestrator probes: the QA6 reply-1/3 pitch pair AND the triple callback-pitch shape both flag; a legitimate confirm-echo and short acks do NOT (calibration explicitly checked against the CB-53 over-fire lesson).
- **Verification:** 13 new tests; invariant files 128 passed; full suites 562p/5s (.venv) + 576p (system).
- **Constraints checked:** R37 (stream flag-only, can't un-speak); one-retry cap (no regenerate loop); reuses CB-53's retry machinery, not duplicated.
- **Follow-ups / known gaps:** whether the real model obeys the harder retry instruction → paid replay; eval-judge repetition axis re-check rides the QA round.

### CB-62 — Price-question policy: offer the KB-grounded ballpark instead of pure deflection
- **Type / Surface / Size:** change · `core` (`nlg` only — gates already routed correctly) · S
- **Completed:** 2026-06-07
- **Files changed (actual):** `src/core/nlg.py` (`_BUDGET_CONCERN_NOTE` injected when a price inquiry meets `answer_via_kb`/`handle_objection`: empathetic ack first — explicit ban on "Perfect!" after a budget concern — then the KB-grounded ballpark from the FACTS block, then the exact-quote callback bridge; `is_price_inquiry` from DST label OR question text), `tests/unit/test_cb62_price_policy.py` (NEW, 14).
- **What changed:** investigation showed `address_direct_input` + `price_gate` already route a direct price ask to `answer_via_kb` — the defect was NLG having no instruction to use the retrieved range or acknowledge budget anxiety. No hardcoded prices anywhere (CB-53 grounding guard remains the enforcement: numbers must come from retrieved KB facts).
- **Verification:** 14 new tests (instruction present in prompt under the right conditions; decision path unchanged); full suites green as above.
- **Constraints checked:** compliance — no invented prices possible via this path (prompt carries no figures; grounding guard still polices output).
- **Follow-ups / known gaps:** real-model phrasing quality (ack + ballpark actually spoken) → paid replay + blind QA.

### CB-60 — Cohort/count coherence across Operate (one disease, many symptoms)
- **Type / Surface / Size:** bug · `api` (`operate`) · `web` (calls/escalations) · L
- **Completed:** 2026-06-07 (resumed after mid-flight stop; 2 agent sessions)
- **Files changed (actual):** `src/api/operate.py` (`_is_completed` filter shared by Calls list AND `/api/kpis` — one denominator; `total` on episodes response; `is_stub` on summaries via `channel=='sim'`; `episode_cohort` on escalations), `web/app/operate/calls/page.tsx` ("Seeded sample" badge; "showing N of total" disclosure), `web/app/operate/escalations/page.tsx` (cohort hint for non-live escalations), `web/lib/operate-types.ts`, `tests/e2e/test_cb60_coherence.py` (NEW, 12), `tests/e2e/qa7_cb60.py` (NEW, live sweep), `tests/e2e/test_operate.py` (2 KPI tests re-spec'd to the completed-only denominator).
- **What changed:** the 67%-vs-0% KPI self-contradiction came from `/api/kpis` computing over ALL episodes (incl. in_progress orphans) while the list filtered — both now use `_is_completed`; the ~150 no-cohort orphans were those in_progress/null-outcome rows, now excluded from both. Escalation rows carry their cohort (badge == reachable list count); seeded stubs badged and excluded from "Real calls" by default (NOT deleted).
- **Verification:** 49 passed (coherence 12 + operate suite incl. re-spec'd KPI tests); full suite 584p/11s; query/display-layer only — zero DB writes.
- **Constraints checked:** no DB UPDATE/DELETE/migrations; stubs preserved; src/core untouched.
- **Follow-ups / known gaps:** live-dashboard verification needs the API restart (running process predates these changes); KPI version-selector contents are CB-59's scope.

### CB-54 — Demo chat dies after the first conversation (consent 409 wall + stuck sessions)
- **Type / Surface / Size:** bug · `web/app/demo` + `web/components/demo` · M (server unchanged)
- **Completed:** 2026-06-07
- **Files changed (actual):** `web/components/demo/ConsentFlow.tsx` (StrictMode dedup `startedRef` + stale-async `effectTokenRef` — the consented session is deterministically the one bound), `web/components/demo/TextConsole.tsx` (calls `/api/session/{id}/end` on done → episode finalizes + reaches Calls list; honest 409 copy + "Re-open consent" recovery), `web/app/demo/page.tsx` (re-gate callback), `tests/e2e/test_cb54_consent_session.py` (NEW, 17 server-contract tests), `tests/e2e/qa7_cb54.py` (NEW, live browser regression).
- **What changed:** root cause was React StrictMode double-firing the consent-start effect → two sessions; the UNCONSENTED one could win the parent-state race → chat bound to a `pending` session → 409 for every subsequent visitor. The effect-token guard discards the stale session; finalization + 409 recovery added.
- **Verification:** 17 server tests green (cross-session regression, double-start race, finalization, compliance invariants: no-consent 409, minor→need_parental, in-chat minor detection, sha256-only phone, AI disclosure). Live browser (qa7_cb54.py, 2 paid turns ≈$0.03): conv A then FRESH conv B both chat — the QA6 blocker is dead.
- **Constraints checked:** consent/minor/PII gates proven unweakened; server untouched.
- **Follow-ups / known gaps:** in dev, StrictMode still fires consent/start 2× by design (one orphan `pending` session, dev-only, never used, still 409-gated; single-fire in prod builds). Calls-list appearance of a *naturally finished* conversation rides on `/end` — verified at the contract level; blind QA round will confirm end-to-end.

### CB-61 — The agent doesn't LISTEN: DST drops caller-stated slots (deadline, callback time)
- **Type / Surface / Size:** bug · `core` (`dst`, `gates`, `nlg`) · M–L
- **Completed:** 2026-06-07 (2 coder rounds + adversarial verifier)
- **Files changed (actual):** `src/core/dst.py` (widened `_TIMELINE_RE` + hedge softening; NEW `callback_window` slot/extractor with `_DEADLINE_CONTEXT_RE` guard + `_CALLBACK_HEDGE_RE`; tightened agent-directed `_CONFIRM_REQUEST_RE` with "confirm with <person>" exclusion; `confirm_request` intent), `src/core/gates.py` (`address_direct_input`: live objection wins over confirm-routing; confirm_request → `confirm_known` targeting callback_window), `src/core/nlg.py` (`confirm_known` guidance echoes the exact booked day/time), `tests/unit/test_cb61_listening.py` (NEW, 32), `tests/unit/test_cb61_adversarial.py` (NEW, 17 — verifier's spec).
- **What changed:** volunteered facts ("test in about three weeks", "Thursday after 3pm") now extract as slots → `skip_known`/`no_repeat_discovery` block re-asks; explicit confirm requests echo the time. Round-1 cut had 3 BLOCKING defects caught by the verifier (deadline-day captured as callback window at 0.9; "confirm with my wife" hijacking the decision_maker objection past the CB-50 guard; confirm-routing pre-empting live objections) — all fixed in round 2 without editing the spec.
- **Verification:** adversarial 17/17, listening 32/32; loop-breaker 14, grounding 8, CB-50 guard 7, CB-48 directness 13, R37 parity 37; FULL suites 535p/5s (.venv) + 549p (system python), zero regressions; no LiveKit imports in src/core.
- **Constraints checked:** R37 parity, buy-gate purity untouched, CB-50 third-party logic proven unmasked.
- **Follow-ups / known gaps:** real-model behaviors deferred to the paid replay (NLG actually echoing the time; DST-LLM agreement on unusual phrasings; "can you confirm a human will call" reaches confirm_request not human_request — documented tolerable gap).

### CB-58 — Untangle rejection vs guardrail messaging on experiment cards
- **Type / Surface / Size:** bug · `api` (`improve`) · `loop` · `web` · S–M
- **Completed:** 2026-06-07
- **Files changed (actual):** `src/api/improve.py` (`_settle_failed_run` no longer sets `guardrail="trip"` for incomplete runs; plain-English fallback reason), `src/loop/promotion.py` (all 3 rejection reasons rewritten plain-English — no `challenger_better is False` flag text), `web/app/improve/lab/page.tsx` (approval CTA only when `state=='blocked'`; no fabricated metrics/Sample bar for n=0 runs; reason no longer duplicated into the guardrail strip).
- **What changed:** the three conflated endings now tell distinct truths: rejected-insignificant (no guardrail language), guardrail-regressed (regression strip, approval CTA only if an approvals entry exists), failed/timed-out (no metrics, honest copy).
- **Verification:** 6 new tests (terminal-path state/guardrail/reason triples + `"challenger_better"` absent from every reason); full suite 680/0 with all Wave-1 changes in tree.
- **Constraints checked:** no internal flag names render; historical records keep old text (display now keyed off state, so they render correctly).
- **Follow-ups / known gaps:** old stored records retain `guardrail="trip"` from the legacy failure path (historical data, display-mitigated).

### CB-57 — Run-experiment honesty: quote the floored n, honest progress
- **Type / Surface / Size:** bug · `web` (`/improve/lab`) · `api` · S–M
- **Completed:** 2026-06-07
- **Files changed (actual):** `src/api/improve.py` (202 body now carries `effective_n`/`n_min`/`n_max`), `web/lib/improve-types.ts`, `web/app/improve/lab/page.tsx` (dialog quotes the FLOORED n with a floor note; running card shows "Running — results land in one batch" instead of a fake full Sample bar), `tests/e2e/qa7_cb57.py` (NEW — pre-launch probe, no paid submit).
- **What changed:** n=1 now honestly quotes 10 calls (5/arm minimum) before launch; no fake instant progress.
- **Verification:** 3 new API tests (floor/cap/no-floor in 202); qa7_cb57.py PASS live (n=1 → "10 calls" quoted, drawer closed without submitting).
- **Constraints checked:** no paid run launched during verification.
- **Follow-ups / known gaps:** UI mirrors N_MIN=5/N_MAX=24 as local constants for the pre-launch quote (matches backend today) — drift risk if `_MIN_RUN_N`/`_MAX_RUN_N` change; wire a config GET if they ever move. No completion toast yet (card transition only).

### CB-56 — Experiment rerun silently overwrites prior run's record (ID collision)
- **Type / Surface / Size:** bug · `api` (`improve`) · S–M
- **Completed:** 2026-06-07
- **Files changed (actual):** `src/api/improve.py` (`unique_run_id()` — ms-timestamp suffix on `RUN-{challenger_version}`; used by `run_experiment_ep`).
- **What changed:** every launch now creates a unique record; re-running the same dimension can no longer clobber prior history (the QA6 run had destroyed the goal-first@n30 record this way). Old-format ids still fetch (back-compat) and arm episode namespaces are scoped per run.
- **Verification:** 4 new tests (distinct ids, first record untouched by rerun, arm-id scoping, legacy id fetch); improve suite 52/52.
- **Constraints checked:** concurrency guard makes same-ms collision impossible (one run at a time).
- **Follow-ups / known gaps:** reruns are separate cards (no grouping UI) — acceptable per CB entry ("may be grouped, never overwritten").

### CB-55 — Lab→Review handoff 404s: composite episode IDs double-encoded
- **Type / Surface / Size:** bug · `web` (`/improve/lab/[id]` → `/operate/review/[id]`) · S
- **Completed:** 2026-06-07
- **Files changed (actual):** `web/app/operate/review/[id]/page.tsx` (decode once + clean error copy), `tests/e2e/qa7_cb55.py` (NEW — assertion-based playwright regression).
- **What changed:** Next.js 14 does NOT auto-decode dynamic route params; the lab page's `encodeURIComponent('RUN-…::champion::0')` arrived at the review page still encoded, and `fetchEpisode` encoded it AGAIN (`%253A%253A`) → API saw a nonexistent id. Fix: `const id = decodeURIComponent(params.id)` at the consumer (idempotent for plain `ep-`/`sp-` ids). Error page now shows a stable human message ("This call record does not exist…") — never the raw encoded ID blob.
- **Verification:** qa7_cb55.py before-fix FAIL (both arms "Call not found" with `%3A%3A` blob) → after-fix PASS (champion + challenger transcripts render, 10 turns each; bogus-id error shows clean copy). Re-run by orchestrator against the live dash: PASS.
- **Constraints checked:** no raw ID/slug in viewer-facing error text; normal call routes unaffected (idempotent decode).
- **Follow-ups / known gaps:** none.

### CB-45 — Surface call timing on the dashboard (live + review + calls)
- **Type / Surface / Size:** feature · `/operate/live` · `/operate/review` · `/operate/calls` · `design` · M
- **Completed:** 2026-06-04
- **Files changed (actual):** `web/lib/operate-types.ts` (`TurnTiming`, `Turn.timing?`, `LiveTiming`, `LiveSnapshot.live_timing?`, `EpisodeSummary.avg_first_token_ms?`/`avg_stream_ms?`), `web/lib/operate-api.ts` (`fmtMsShort`/`fmtFirstToken`/`fmtSpoke`), `web/app/operate/live/page.tsx`, `web/app/operate/review/[id]/page.tsx`, `web/app/operate/calls/page.tsx`.
- **What changed:** timing surfaces everywhere a call is reviewed — Live monitor active turn shows "⚡ 0.8s to first word · speaking 1.2s" from `snap.live_timing`; Review shows per-turn chips ("0.7s to first word · spoke for 1.4s") + a call-level "Voice timing" stat from the avg fields; Calls drawer shows "Time to first word". Built against CB-44's locked contract; all fields nullable → omit/"—" on text/legacy turns, never crash.
- **Verification:** `cd web && npx tsc --noEmit` clean (exit 0). Cadence dark tokens reused (no new visual language); human phrases only, no raw key/slug renders. Screenshot via a temp mock harness (backend wasn't up) confirmed all four phrases render — `tests/e2e/shots/cb45_timing.png`; harness deleted after capture.
- **Constraints checked:** no internal-index/slug leak in rendered timing; null-tolerant so it shipped independent of CB-44 landing. Contract field names match the backend exactly (verified against operate.py).
- **Follow-ups / known gaps:** real-data render (vs mock harness) to be eyeballed once a live/stored call carries timing.

### CB-53 — Consolidate the repetition + fabrication safeguards into two coherent mechanisms
- **Type / Surface / Size:** change · `core` (`src/core`) · L
- **Completed:** 2026-06-05
- **Files changed (actual):** `src/core/gates.py` (new `break_no_progress_loop` + sticky terminal; deleted the `_recent_objection_handles`/`_BREAKTHROUGH_OBJECTION_HANDLES` special case), `src/core/respond.py` (recent-acts/progress + loop_break/loop_terminal meta; runtime grounding guard `_is_fabrication` + regenerate/fallback), `src/core/nlg.py` (one consolidated `_GROUNDING_RULE` + hardened retry), `tests/unit/test_loop_breaker.py` (NEW, 14), `tests/unit/test_grounding_guard.py` (NEW, 8), `tests/unit/test_policy.py` (objection-breakthrough test relocated to the unified gate).
- **What changed:** the scattered repetition guards (CB-48/51 NLG phrasing + `no_repeat_discovery` 7-slot cap + objection-deadlock counter) and fabrication guards (soft NLG instruction + offline-only `grounded()`) were each partial with gaps. Consolidated into TWO: (1) ONE progress-based loop-breaker — K=3 reactive turns with no progress (no slot lock / objection clear / purchase-intent rise) forces a commit; a SECOND stall after a declined commit escalates terminally (sticky), so no relapse-loop; (2) ONE runtime grounding guard on claim turns — `grounded()` now runs on the live blocking path and a genuine fabrication (unsupported number, claim-token, or invented grade/score-jump outcome story) is regenerated once then falls back to an honest line. Streaming flags-only (can't un-speak).
- **Verification (batch, not just suite):** suite 635 passed/5 skipped; voice 10; R37 parity 8. 15-run batch + a 6-run regression re-batch judged by a blind QA subagent across both rounds: **Pat turn-1 escalation eliminated** (light discovery → callback, no hard close, 5/5); **dangerous student-outcome fabrication eliminated** (0 recurrences; the new outcome-pattern detector catches the D→B+ story). First consolidation OVER-FIRED the grounding fallback (60×/15 — a regression); the fix dropped the broad ratio arm so it fires only on genuine fabricated specifics → **fallback over-fire 60 → ~0** (5/6 re-batch transcripts now 0; no fabrication in any). dana_11 (one adversarial proof-demander seed) still runs long but now ESCALATES (sticky terminal) instead of looping to a walk.
- **Constraints checked:** R37 (respond/respond_stream lock-step, reply==concat(tokens)); buy-gate PURE (forced commits only attempt_close at an offered tier, blind paid → free callback); never raises into the call path.
- **Follow-ups / known gaps:** dana_11-style relentless proof-demand still produces a longer-than-ideal call before the terminal escalate (KB has no concrete Algebra proof — fundamental). Streaming-path grounding is flag-only (can't retract a spoken token).

### CB-52 — Per-call LLM cost accounting (automatic spend ledger)
- **Type / Surface / Size:** feature · `core` (`src/core`) · `sim` · M
- **Completed:** 2026-06-04
- **Files changed (actual):** `src/core/cost.py` (NEW — in-process accumulator + optional JSONL ledger + `python -m src.core.cost` rollup), `src/core/llm.py` (request `usage:{include:true}`; record `usage.cost` from `complete` + the stream's final usage chunk via new `_sse_usage`), `src/sim/eval_personas.py` (reset + set `LLM_COST_LOG` + print `RUN COST` per run), `tests/unit/test_cost.py` (NEW, 6), `.gitignore` (eval-cost-log.jsonl + eval-budget-ledger.md). Feeds `docs/eval-budget-ledger.md`.
- **What changed:** replaced hand-estimated spend (which was ~10–17× off) with REAL per-call cost. OpenRouter returns `usage.cost` when asked; the client now records it per call into a thread-safe accumulator (grand total + per-model + tokens) and, when `LLM_COST_LOG` is set, appends one JSONL line per call. Each eval run prints `RUN COST: $X over N calls — <by model>`. Best-effort — `record_usage` NEVER raises into the call path; the mock client + tests stay at $0 with no file writes.
- **Verification:** test_cost 6 passed; llm + streaming-parity 30 passed; full non-voice suite **613 passed/5 skipped**. Real Marcus run printed `RUN COST: $0.0397 over 24 LLM calls` (sonnet NLG $0.0358 = ~90%, gpt-5-mini $0.0024, gpt-4o-mini $0.0011, gpt-5-nano $0.0005); JSONL rollup matched exactly. Token-stream PARITY preserved (the usage-only final chunk has empty choices → never yielded; `complete()`==concat(stream) tests green). R37: cost is metadata only, brain decision untouched.
- **Constraints checked:** R37 (no decision change; reply==concat(tokens)); never raises into the call path; API key never logged; ledger files gitignored.
- **Follow-ups / known gaps:** capture is wired in the OpenRouterClient, so it's automatic for ANY entry point that sets `LLM_COST_LOG` (eval CLI does by default); the API server / voice worker could opt in too. `usage_daily` lags the per-call sum — trust RUN COST / the JSONL rollup.

### CB-51 — Widen anti-repetition window + honest-pivot rule (Dana stat-cycling)
- **Type / Surface / Size:** change · `core` (`src/core`) · S
- **Completed:** 2026-06-04
- **Files changed (actual):** `src/core/respond.py` (`_own_last_lines` n 2→4), `src/core/nlg.py` (`_NO_RESTATE_INSTRUCTION` strengthened).
- **What changed:** the CB-48 anti-repetition window was the agent's own last 2 lines, which missed a stat restated a few turns apart (Dana cycled "97% satisfaction" 3×). Widened to 4 lines and rewrote the no-restate instruction to explicitly forbid re-citing the same statistic/offer and to add: "if the prospect keeps pressing for something you honestly cannot give, say so plainly once and pivot to the next step — never loop."
- **Verification:** full non-voice suite 607 passed/5 skipped; R37 parity + CB-48 directness 19 passed; voice 10 passed. Real-model eval — Dana seed 7 went from escalated t5 (97% repeated 3×) to **callback_scheduled t10** (stat dropped after 2 mentions; agent acknowledged "I've shared every angle I can" then closed); Marcus seed 7 unchanged (walked t6, honest). Actual cost of the 3 verify runs: $0.11 (OpenRouter usage_daily delta).
- **Constraints checked:** R37 (own-lines only, no caller transcript; reply == concat(tokens)); distillation preserved.
- **Follow-ups / known gaps:** Dana's MIDDLE turns still loop on "a specialist can show you the algebra data" because she demands concrete Algebra case studies that genuinely don't exist publicly — a KB-content ceiling (the honest agent won't fabricate), not a code defect. A close-sooner policy nudge could shorten it but risks pushiness; left as-is.

### CB-50 — No-authority gatekeeper → callback, and no triggerless terminal escalation
- **Type / Surface / Size:** change · `core` (`src/core` gates) · M
- **Completed:** 2026-06-04
- **Files changed (actual):** `src/core/gates.py` (`no_authority_prefers_callback` + tightened `escalation_triggers`/`de_escalate` short-circuits + new `escalate_only_if_warranted`); `src/core/dst.py` (hedged/disjunctive grade extracted at reduced confidence so an uncertain "7th or 8th" isn't locked + asserted back; broadened `_HUMAN_REQUEST_RE`); `tests/unit/test_policy.py` (+8), `tests/unit/test_cb50_escalate_guard.py` (NEW, 7), `tests/e2e/test_demo_call.py` (escalate param uses a genuine human request).
- **What changed:** RCA — the no-authority gatekeeper surfaces as `active_objection="decision_maker"`, the LLM proposed `escalate`, and `escalation_triggers` short-circuited on an already-`escalate` input even with no genuine trigger. Two-part fix: (1) a triggerless `escalate` is NEVER terminal — `escalate_only_if_warranted` redirects it to `attempt_close@callback` on a no-authority/gatekeeper read (incl. a tight "for my <relative>'s kid" third-party detector) or otherwise to a non-terminal selling act (`answer_via_kb` if there's an open question, else `pitch`); (2) only a GENUINE reason (explicit human request, compliance, low-confidence streak, over-band concession, hostility/extreme-bail) escalates.
- **Verification:** full non-voice suite 607 passed/5 skipped (TDD red→green); escalation+policy+personas 84 passed; buy-gate stays pure (Pat caps at callback). Real-model eval (before→after): Pat went from turn-1 escalate/no-discovery to **discovery first → callback offer** on the no-authority reveal (both seeds); Dana reached **callback_scheduled at t12** (was a premature t2 escalate); Marcus still escalates only for a genuine unanswerable scheduling ask.
- **Constraints checked:** R37 (gates copy+mutate the Decision); genuine escalation preserved; buy-gate untouched.
- **Follow-ups / known gaps:** a vague gatekeeper can still hit the low-confidence-streak genuine-escalation trigger (labelled `escalated`) before the prospect commits to a callback — behaviorally it's a callback offer after discovery, but the outcome label isn't always `callback_scheduled`. Acceptable; revisit if a cleaner gatekeeper→callback-commit path is wanted.

### CB-49 — Replace the fake demo KB with a real, sourced 175-chunk Nerdy corpus + fix retrieval
- **Type / Surface / Size:** feature · `data` (KB) · `core` (retrieval) · `sim` (eval wiring) · L
- **Completed:** 2026-06-04
- **Files changed (actual):** `src/kb/content/{pricing,policies,programs,objections,competitors}.json` (rewritten with real data) + `src/kb/content/{results,company}.json` (NEW); `src/kb/embeddings.py` + `src/kb/retriever.py` (`embed_query`); `src/sim/selfplay.py` (await async retrieve hook) + `src/sim/eval_personas.py` (wire the real KB hook into the eval CLI); `tests/integration/test_kb.py` (count derived from corpus, not hardcoded). Re-ingested kb_v0 → 175 chunks (was 27).
- **What changed:** 6 parallel research agents scraped Nerdy/Varsity Tutors and authored 175 sourced chunks across 7 files — every fact tagged VERIFIED/REPORTED/ESTIMATED with contradictions resolved in `_meta` (Nerdy publishes NO fixed rate card, so pricing is anchored on their filed ARPM ~$335→$374/mo + framed "confirmed in consultation"; results carry only real aggregate proof — 97% satisfaction, 2× math proficiency growth/ESSA III, 4.9★ — and OMIT a fabricated SAT point-gain that doesn't exist). Added the missing 4 R30 objection slugs + the `objection_taxonomy` key.
- **TWO retrieval bugs fixed (the KB was useless without these):** (1) the first ingest wrote CORRUPTED vectors (cold model load during a concurrent HF download — stored embeddings were ~orthogonal to their own text, cosine≈0); caught by a stored-vs-fresh check, fixed by re-ingest (cosine=1.0). (2) the embedder ignored BGE-v1.5's required asymmetric QUERY instruction; added `embed_query` (query-only prefix; passages unchanged → no re-ingest needed; FakeEmbedder/tests unaffected). The self-play eval also never wired KB retrieval at all — now it does (async hook awaited).
- **Verification:** KB integration 9 passed; full non-voice suite 607 passed/5 skipped. Retrieval now surfaces the right chunks for every persona's hardest question. Real-model eval: Marcus answers "5 weeks?" with honest no-guarantee + diagnostic + Pass Guarantee + concrete $300-600/mo; Dana engages the proof demand (97% satisfaction, guarantee, consult) — both grounded, no fabrication.
- **Constraints checked:** compliance — sourced/aggregate claims only, no invented prices/results; exact price always "confirmed in consultation." No internal slug leaks (chunks are human prose). Demo `kb_v0` overwritten in place (auditable in git).
- **Follow-ups / known gaps:** Dana seed 7 still cycles the one satisfaction stat + over-defers — because she demands concrete Algebra-specific proof that genuinely does not exist publicly; the honest agent can't satisfy it without fabricating. A longer anti-repetition window / steer-to-2×-growth-then-close would help (future brain pass). NOTE: a cold-model-load ingest can silently write garbage vectors — re-ingest with a warm/cached model; consider a post-ingest self-retrieval sanity check.

### CB-48 — Production-brain directness + anti-repetition (verified against the 3 eval personas)
- **Type / Surface / Size:** change · `core` (`src/core`) · M
- **Completed:** 2026-06-04
- **Files changed (actual):** `src/core/nlg.py` (`_build_messages`/`realize`/`realize_stream` accept `own_last_lines` → `YOUR_LAST_LINES` block + no-restate instruction), `src/core/respond.py` (`_own_last_lines(history)` threaded into BOTH `respond` and `respond_stream`), `src/core/gates.py` (`_question_recurs` content-word recurrence; `address_direct_input` answers a recurring question even over pitch/close + suppresses discovery `ask`/`confirm_known` at high bail_risk; `advance_to_close` recurring-question guard), `tests/unit/test_cb48_directness.py` (NEW, 13 cases).
- **What changed:** (1) ANTI-REPETITION — NLG now sees the agent's OWN last 1-2 lines (not the caller transcript → distillation preserved, no new inventable facts) and is told not to restate; threaded identically to `realize` + `realize_stream`. (2) DIRECTNESS — a RECURRING unanswered question (content-word overlap with an earlier turn) forces `answer_via_kb` before any pitch/close; a discovery ask under high bail_risk (≥ pushiness cap) is redirected to answer/advance. A genuinely NEW question keeps the reactive-loop escape (so the agent isn't trapped answering forever).
- **Verification:** full non-voice suite **586 passed / 5 skipped** (573 baseline + 13 new, zero existing tests broken/adjusted); R37 parity `test_respond_stream`+`test_respond_timing` 8 passed; voice integration 10 passed. TDD red-first (8 failed → green). Orchestrator re-verified all suites + read the gates/respond/nlg diffs.
- **Eval re-runs (real models, before → after):** **Marcus** — repetition gone (every turn a distinct framing), ZERO discovery questions (was asking "his score?" under pressure), answered the core "5 weeks?" with a concrete "50–100 points"; still walks t6 (he demands a yes/no guarantee an honest agent won't give → CB-49 proof gap). **Dana** — the verbatim "free consultation ×5" is gone; agent varies + directly engages the proof demand (honestly reframes to trial + progress tracking); the remaining "no concrete Algebra proof" is the CB-49 KB gap. **Pat** — disqualify path INTACT (escalated t2, qualified=False/no_authority, no false close).
- **Constraints checked:** R37 parity (both paths thread the same own-lines — asserted by test; reply == concat(tokens)); buy-gate pure + untouched; own-lines only (not caller transcript). Headers updated. One assertion in coder's OWN new test scoped to the YOUR_LAST_LINES block (caller question legitimately still appears under brain-derived PROSPECT_ASKED) — no existing test modified.
- **Follow-ups / known gaps:** the residual on Marcus/Dana is the **concrete-proof KB gap → CB-49** (data, not brain). Pat over-escalation → CB-50 still open.

### CB-47 — Perceived-latency filler masks the DST+Policy prelude (no more dead air on ring)
- **Type / Surface / Size:** feature · `voice-worker` · S
- **Completed:** 2026-06-04
- **Files changed (actual):** `src/voice/worker.py` (env consts `VOICE_FILLER_ENABLED`/`VOICE_FILLER_DELAY_MS` + 6-line rotation; `_filler_line_for_turn`/`_maybe_speak_filler`/`_cancel_filler`; wired in `llm_node`), `tests/integration/test_voice_sim.py` (4 new tests).
- **What changed:** at brain-start `llm_node` schedules a delayed backchannel that speaks via `session.say(allow_interruptions=True)` ONLY if the first NLG token hasn't arrived within `VOICE_FILLER_DELAY_MS` (default 700ms); the first-token branch marks the sid + cancels the task (belt-and-suspenders, before any await) so a fast turn gets NO filler and there's no double-speak. Line rotates deterministically by turn counter ("Sure—", "Got it,", …). So the caller hears a natural ack within <1s instead of ~3-5s of dead air.
- **Verification:** `/usr/bin/python3 -m pytest tests/integration/test_voice_sim.py -q` → 10 passed (6 original + 4 new: slow→fires, fast→skips, llm_node-cancel→no double-speak, full-sim→committed reply/transcript byte-for-byte unaltered). TDD red-first verified.
- **Constraints checked:** R37 — brain UNCHANGED; filler is worker-only TTS, never a committed Turn, never touches the streamed reply or CB-44 timing stamps. NOT parallelizing DST+Policy (data dependency — correctness risk), per the measured RCA.
- **Follow-ups / known gaps:** prompt-caching bonus SKIPPED (implicit OpenAI caching needs ≥1024-token prefixes the short DST/Policy prompts don't reach; explicit Anthropic caching discounts prefill, not the round-trip latency that dominates) — correct call, not half-implemented. Real (not just perceived) prelude latency is still ~3s → revisit only if needed.

### CB-46 — Fix: call visible on connect (BEFORE the disclosure) + streaming verdict
- **Type / Surface / Size:** bug · `voice-worker` · `/operate/live` · S
- **Completed:** 2026-06-04
- **Files changed (actual):** `src/voice/worker.py` (new `_disclose_and_mark_connected` helper; `entrypoint` calls it), `tests/integration/test_voice_sim.py` (ordering regression test).
- **What changed:** RCA confirmed — the connect-time in_progress upsert ran AFTER `await session.say(disclosure)`, whose await blocks for the whole (multi-second) disclosure TTS, so the row the live monitor polls for wasn't written until the disclosure finished → call invisible until ~the first committed turn. Fix: `_disclose_and_mark_connected` fires the connect upsert FIRST as fire-and-forget (`asyncio.ensure_future(_upsert_live_voice)`), THEN speaks the disclosure. Single-sourced so the ordering is regression-tested via one code path.
- **Verification:** `/usr/bin/python3 -m pytest tests/integration/test_voice_sim.py -q` → 6 passed incl. new `test_voice_sim_connect_upsert_fires_before_disclosure_say` (written red first). Consent/disclosure semantics UNCHANGED (R33 — disclosure still spoken before the brain answers); R37 untouched.
- **STREAMING VERDICT (CB-44 data):** word-streaming is **GENUINE** — NLG streamed 32 tokens over ~1.77s (`stream_duration_ms ≫ 0`), not a single chunk. The perceived "dead air on ring / not streaming" is NOT the NLG: `first_token_ms ≈ 4.8s` is dominated by the **~3s blocking DST + Policy prelude** (two JSON LLM round-trips) that runs BEFORE NLG streaming starts (brain.timing: dst≈1412ms, policy≈1664ms, nlg≈3493ms).
- **Follow-ups / known gaps:** **CB-47 candidate** — mask/shrink the DST+Policy prelude (filler line and/or parallelize/cache the two prelude round-trips) so time-to-first-word drops from ~4.8s. Audio-in→first-token on a REAL phone/web call still to be captured (the eval run had no STT).

### CB-44 — Voice-call timing observability (audio-in → first-token → stream-end), every call
- **Type / Surface / Size:** feature · `voice-worker` · `api` · `data` · M
- **Completed:** 2026-06-04
- **Files changed (actual):** `src/voice/worker.py` (stamp `t_audio_in` in `on_user_turn_completed`; `t_brain_start`/`t_first_token`/`t_stream_end` in `llm_node`; `_record_turn_timing` + `_live_timing_snapshot`; carried through `_upsert_live_voice`/`_upsert_live_partial`/`_register_persistence`), `src/api/persistence.py` (`turn_timings`/`live_timing` → `metrics`, turn_id stringified for jsonb), `src/api/operate.py` (`_coerce_timing`/`_coerce_live_timing`/`_timing_averages`; serializes the 3 LOCKED shapes), `tests/unit/test_cb44_timing.py` (NEW, 5 cases), `tests/integration/test_voice_sim.py` (streaming-timing test).
- **What changed:** every agent voice turn now records audio→first-token→stream-end and exposes per-turn `timing` (4 ms numbers, null on text/legacy), live `live_timing` (in-flight first-token + stream-elapsed, resolved to absolute ms so the API process needs no foreign clock), and summary `avg_first_token_ms`/`avg_stream_ms`. One info log line per turn.
- **Verification:** `tests/unit/test_cb44_timing.py` 5 passed; `tests/integration/test_voice_sim.py` 6 passed; full non-voice suite 573 passed/5 skipped; operate e2e 37 passed; DB-gated store/persistence 19 passed (real Postgres :5434). R37 parity intact (timing metadata only; reply == concat(tokens)). Fire-and-forget — every timing path wrapped/swallowed, never blocks the speak loop.
- **Captured numbers (RUN-1):** real production NLG via `handle_user_turn_stream` — `first_token_ms≈4803, stream_duration_ms≈1766, tokens=32` (model claude-sonnet-4.5); mock path (with STT in the harness) populated all 4 incl. `audio_to_first_token_ms`. See the CB-46 streaming verdict.
- **Constraints checked:** PII — timings are numbers only, no transcript leakage. Contract matches the frontend `TurnTiming`/`LiveTiming` types exactly (verified).

### CB-43 — Deeply-prompted eval personas (Dana / Marcus / Pat) + seeded non-sequiturs
- **Type / Surface / Size:** feature · `core` (`src/sim`) · M
- **Completed:** 2026-06-04
- **Files changed (actual):** `src/sim/personas.py` (3 named builders `build_dana/marcus/pat` + `NAMED_PERSONAS` registry + `named_persona()`; `Persona` gains `name`/`behavior_prompt`/`non_sequiturs`), `src/sim/prospect.py` (per-persona behavior prompt wired into `_build_sim_messages`; seeded `_maybe_non_sequitur()` ~1-in-4 from the simulator's own `random.Random`, never global), `src/sim/eval_personas.py` (NEW runner + CLI: `build_eval_cohort`/`run_named_eval`/`transcript_lines`), `tests/unit/test_personas.py` (NEW, 17 cases).
- **What changed:** 3 orchestrator-locked personas — Dana (qualified hard-to-close), Marcus (qualified fast/prickly), Pat (UNqualified no_authority/callback) — each with a rich behavioral prompt + in-character curveball lines injected occasionally on a seeded schedule (measured 12 curveballs / 40 turns). Curveballs touch only the TALK prompt; the pure buy-gate never sees them.
- **Verification:** `.venv/bin/python -m pytest tests/unit/test_personas.py -q` → 17 passed; sim units (prospect+twin+personas) → 44 passed; full non-voice suite → 573 passed / 5 skipped. TDD held (red on missing `NAMED_PERSONAS` → green).
- **Constraints checked:** buy-gate PURITY — Pat's `buy_gate` returns None for enrollment/trial/consultation even with all true drivers pinned to 1.0, caps at callback; buy-gate code untouched. Cohort = `experiment_eval_personas_<name>` (never `live`, so it can't flood the operator Calls log). Stubs NOT deleted. File headers updated.
- **Follow-ups / known gaps:** a real-model bounded self-play batch was NOT spent by the agent (mock client only); the orchestrator runs one bounded real eval (P-B Marcus) at integration. The deeply-prompted set begins at 3; expand later if the eval proves trustworthy.

### CB-42 — [EXPERIMENT, contained on `cb42-ml-distill`] Distill DST + Policy LLMs to local ML — EVALUATED, NOT MERGED
- **Completed (evaluated):** 2026-06-03 · on the isolated `cb42-ml-distill` worktree/branch (commits `78e5820`, `806f8ba`); **NOT merged to main** (did not clear the "better" gate).
- **What was built/measured:** data export (5,919 DST samples, 1,876 CB-06-grounded; 900 teacher-distilled policy acts) → **DST→ML regressor** (MLP, held-out MAE 0.0033, ~92% over naive, 98.6% directional agreement, tracks CB-06 truth as well as the LLM DST, autoregressive trajectory MAE 0.0235) → **Policy→ML** (HistGB 0.931 agreement, confidence-gated hybrid: ~89% local on easy turns, 64% LLM-fallback on adversarial turns) → wired `respond_ml` (gates + buy-gate UNCHANGED, NLG the sole streamed LLM) → **blind-QA panel** (3 judges × 6 adversarial call scripts).
- **Result:** cost/latency WIN — **3 → ~1.1 LLM calls/turn (≈45–63% fewer)**, DST replaceable cleanly. Behaviorally a **TIE**: blind panel 8–8 on better-overall; ML slightly cleaner on nonsensical (7 vs 10) but slightly worse on wrong-phase (9 vs 7) and prescriptiveness (9 vs 7) — the lenses the owner prioritized. Belief-only policy myopia drives the wrong-phase slips.
- **Decision:** gate was "merge only if BETTER"; a tie doesn't clear it → kept contained. Promote-DST / keep-policy-hybrid is the recommendation; **next step before reconsidering merge: add recent-utterance features to the policy act-classifier**, then re-run blind-QA + a larger self-play A/B. Full writeup: `experiments/cb42/RESULTS.md` (on the branch).
- **Bonus finding (for a future CB on the PRODUCTION brain):** the blind QA flagged residual issues in BOTH brains — invented "timing/busy-schedule" framing in objection handling + canned objection scripts that don't acknowledge a disclosure; and some who-for repetition. Worth a follow-up CB.

### CB-30 — Golden-set capture of real conversations (tag + enumerable/exportable set)
- **Completed:** 2026-06-03
- **Verification:** non-voice suite **402 passed / 5 skipped** (+3 golden integration tests) + `tests/e2e/test_operate.py` +6 golden tests; `tsc --noEmit` exit 0; adversarial verifier confirmed all functional checks (the lone `concerns` was the pending API restart, now done). **Live-verified after API restart:** POST `/api/episodes/{id}/golden {true}` → 200 + `detail.golden=true` (persists), GET `/api/episodes/golden` (count 19, contains it), toggle `{false}` → 200, unknown id → 404.
- **What changed:** reused existing seams (single-call export + twin replay already existed; NO migration). `store.set_episode_golden(id, bool)` does a TARGETED `jsonb_set(metrics,'{golden}',…)` (preserves turns + CB-06 prospect_trajectory — verified identical before/after) + `store.list_golden_episodes(limit)` (newest-first, full Episode shape); exported via `src/memory/__init__.py`. `operate.episode_summary/detail` surface a top-level bool `golden`; new routes POST `/api/episodes/{id}/golden` (404 on unknown) + GET `/api/episodes/golden` (full episode_detail payloads = transcript+belief_trajectory+outcome, so the set is enumerable/exportable/replayable); literal `/golden` declared before the dynamic `/{episode_id}` (regression-tested). Review page: a persisted "Mark golden"/"Golden ✓" toggle beside Export (re-hydrated from `ep.golden`, survives reload) + a "Golden" pill (human labels, no slug leak).
- **Files:** `src/memory/store.py`, `src/memory/__init__.py`, `src/api/operate.py`, `web/lib/operate-types.ts`, `web/lib/operate-api.ts`, `web/app/operate/review/[id]/page.tsx`, `tests/integration/test_store.py`, `tests/e2e/test_operate.py`.
- **Constraints checked:** no migration; targeted update (no clobber); no internal-index leak (human labels); replay unchanged (golden = a flagged normal episode the twin harness replays identically).
- **Follow-ups / known gaps:** the shared dev DB already carries ~19 golden tags from dev/test runs — dev cruft the operator can curate (golden is a non-destructive flag, set by the operator per the spec); not an ongoing leak (committed tests use a fake store + test-created episodes).

### CB-41/CB-38 — Wave 4: stream NLG generation → TTS, surfaced on the live monitor (R37-preserving)
- **Completed:** 2026-06-02
- **Verification:** voice suite (`/usr/bin/python3`) **61 passed** (was 60); non-voice (`.venv`) **399 passed / 5 skipped** (was 386/5, +13 streaming tests); `src/core` livekit-CLEAN; adversarial verifier `pass` + `parity_confirmed`. API+worker restarted by explicit PID (worker re-registered with LiveKit). Real-phone audio fidelity is the only unverifiable-in-CI piece (noted).
- **What changed:** added `OpenRouterClient.complete_stream` (httpx SSE `stream:true`), `nlg.realize_stream`, `respond.respond_stream` + a `StreamResult` holder (DST→gated-Policy extracted into a shared `_decide_turn()` so the streamed gated decision is IDENTICAL to `respond()` — gates never forked), and `VoiceSession.handle_user_turn_stream`. The worker's `llm_node` now runs the WHOLE brain STREAMING and yields each NLG token to TTS at generation pace (CB-41), throttle-upserting the growing `live_partial` (CB-38) and committing once at the end (CB-22 preserved). `on_user_turn_completed` now only fail-fast-checks consent + stashes user text (the brain moved into `llm_node` because LiveKit awaits `on_user_turn_completed` fully before `llm_node`). Blocking `complete`/`realize`/`respond` kept for text self-play + DST/policy JSON.
- **Parity (R37):** byte-for-byte `concat(streamed tokens) == committed Turn.text == respond() reply`, verified end-to-end through the real roomless AgentSession AND with an adversarial multibyte/emoji/leading+trailing-whitespace reply; `test_voice_sim_first_token_forwarded_before_full_reply` proves the first token reaches TTS before the last is produced (true streaming, not buffer-then-chunk).
- **Files:** `src/core/llm.py`, `src/core/nlg.py`, `src/core/respond.py`, `src/voice/session.py`, `src/voice/worker.py`, `tests/unit/test_llm.py`, `tests/unit/test_respond_stream.py` (new), `tests/integration/test_voice_sim.py`, `tests/integration/test_worker.py`.
- **Constraints checked:** R37 (no livekit in `src/core`; shared `_decide_turn`); buy-gate purity intact; consent fail-fast unchanged (a not-yet-`can_converse` turn raises `ConsentError`, nothing recorded); commit-once (idempotent); file-top headers updated.
- **Follow-ups / known gaps:** `realize_stream` preserves trailing whitespace while `realize` strips it — a rare cosmetic voice-vs-text transcript divergence (NOT an R37 break; fixing it would risk the parity invariant). Co-locating inference in the LiveKit region (the other latency lever) is a deploy-time change, not code. Real-phone first-audible-word timing needs a live call to confirm.

### CB-33/34/35/36/37/39/40 — Wave 3 batch (4 parallel coders + 5 adversarial verifiers + orchestrator repairs)
- **Completed:** 2026-06-02
- **Verification (shared):** `.venv` suite **386 passed / 5 skipped**; voice suite (`/usr/bin/python3`) **60 passed**; `tsc --noEmit` exit 0; API+worker restarted by explicit PID (worker re-registered with LiveKit). CB-39 browser-verified (Playwright, 2 live demo calls + screenshots `/tmp/cb39_*.png`); CB-33 live-verified (`/api/live/active` unchanged across a polluter run); CB-34/35/36/37 real-LLM-replay-verified (the `ep-2b8ad5bc` scenario now clean — no presumed learner, no re-ask, disclosure acknowledged, hostility → graceful hand-off, **zero hallucinated context**).
- **CB-33** (test-DB isolation) — `operate._is_active` excludes a `cohort` in `_TEST_LIVE_COHORTS` (default `{test}`, env-wideable); `persist_call_live` reads `LIVE_PERSIST_COHORT` (default `live`); new `tests/conftest.py` tags test upserts `cohort=test` + an autouse teardown deletes only `cohort=test`/`ep-esc-*` artifacts. **Orchestrator repair:** removed a data-loss footgun (the bare `OR outcome='in_progress'` delete arm could purge a real live call overlapping a pytest run; the *original* version had already mass-purged ~576 in_progress zombie shells during Wave-1 test runs when its pre-run snapshot failed → purge — no real *completed* call lost). Files: `src/api/operate.py`, `src/api/persistence.py`, `tests/conftest.py`, `tests/unit/test_cb33_test_cohort_isolation.py`.
- **CB-34** (don't presume a learner exists) — DST `_LEARNER_DENIAL_RE`/`_LEARNER_ESTABLISH_RE` set `meta.learner_denied`/`learner_established` (+ `who_for`); `gates.establish_who_first` redirects a learner-specific ask to a who-is-this-for step, and (orchestrator repair) a **pitch/close right after an explicit denial** to who_for too (with a who_for ask-cap to avoid a loop) — fixes the broken context-less pitch reply; NLG no-presumption rule extended to a learner's EXISTENCE. Files: `src/core/dst.py`, `src/core/gates.py`, `src/core/nlg.py`.
- **CB-35** (no re-ask) — DST extracts grade from a bare ordinal/number ONLY on the answer turn (orchestrator repair: gated on `asked_grade` + age-phrase guard, so "1st time calling"/"the 3rd of June"/"7 years old" no longer lock a grade); `gates.skip_known` + new `no_repeat_discovery` (filled/declined/over-asked → hard drop). Files: `src/core/dst.py`, `src/core/gates.py`, `src/core/respond.py` (asked-slot tracking).
- **CB-36** (no hallucinated context) — DST flags an emotional disclosure → `active_objection='concern'` (acknowledge-first via `address_direct_input`); NLG no-invented-context rule. **Orchestrator repair (the verifier's headline real-LLM failure):** the LLM intent classifier hallucinated objections ('timing' on "I said third", 'diy_free' on "feels like a scam") that latched and invented prose — added deterministic **objection grounding** (`_ground_objection`: keep an LLM objection only with a lexical cue; broad cue sets preserve paraphrase-catching) + **disclosure precedence** (a mistreatment disclosure overrides an LLM objection mislabel to `concern`). Files: `src/core/dst.py`, `src/core/nlg.py`, `src/core/gates.py`.
- **CB-37** (de-escalate on hostility) — DST `_HOSTILITY_RE` sets a sticky `meta.hostility`; new `gates.de_escalate` (runs first) overrides any pitch/close/re-ask to `escalate` at hostility OR extreme bail (≥0.9); `pushiness_cap` enforced. Files: `src/core/dst.py`, `src/core/gates.py`.
- **CB-39** (scroll the live transcript) — `web/app/operate/live/page.tsx` `stuckToBottomRef` + `handleStreamScroll` (auto-follow only within 40px of bottom); `web/app/cadence.css` `.lv-stream` `overflow-y:auto` + `margin-top:auto` (replacing `justify-content:flex-end` so the top is reachable). Browser-verified.
- **CB-40** (experiment timeout) — `src/api/improve.py` `compute_run_timeout_s` (scales `n×2×max_turns×3s`, floor 300s, ceiling 1800s); `_settle_failed_run` intact; `tests/unit/test_improve_timeout.py`.
- **New tests:** `tests/unit/test_cb34_37.py` (40 incl. orchestrator-added grounding/age/pitch-gate/disclosure-override regressions), `test_cb33_test_cohort_isolation.py` (4), `test_improve_timeout.py` (11).
- **Constraints checked:** R37 (no livekit in `src/core`); buy-gate purity intact (`de_escalate`/grounding never commit); no internal slug in UI; file-top headers updated on every touched file.
- **Follow-ups / known gaps:** the NLG occasionally adds light unsolicited framing on a `concern` turn (acknowledges but mentions "third grade is a fun year"); minor stage oscillation (close↔discovery) in the adversarial replay — cosmetic, not nonsensical. Broader objection-grounding (beyond the 5 canonical keys) is a candidate if new hallucination contexts surface.

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

### CB-31/CB-32 — Real-voice word streaming + call-visible-on-connect
- **Completed:** 2026-06-03
- **Verification:** `.venv` 477 passed / 5 skipped; voice suite `PYTHONPATH=… /usr/bin/python3 -m pytest test_voice_sim.py test_worker.py test_voice_adapter.py` = 60 passed; tsc exit 0; worker + API restarted. New `test_voice_sim.py::test_voice_sim_connect_upsert_and_word_streaming` drives the REAL roomless AgentSession and asserts: in_progress upsert BEFORE the first commit (CB-32); `llm_node` streams >1 chunk rejoining to the exact reply + a non-empty `live_partial` written DURING the turn + still commits once (CB-31).
- **CB-32:** `worker.py` entrypoint upserts a 0-turn in_progress episode (fresh heartbeat) right after `session.start` + disclosure → the monitor shows the call "Connecting…" the instant it's received, before any turn. Stale-guard still drops a never-engaged call.
- **CB-31 (the deferred CB-21(b)):** `llm_node` is now an async generator yielding ~3-word chunks of the SAME brain-decided reply (livekit-agents 1.5.15 supports `AsyncIterator[str]`) → TTS speaks progressively; throttled (0.4s) partial-text upserts via `persist_call_live(live_partial=…)` → `metrics["live_partial"]`; `operate.live_snapshot` exposes `live_partial` only while active; the live page polls the detail at 1s while a call is active and renders the words filling in on the active turn. Commit-once-at-end preserved (CB-22 + dead-silence fix intact); R37 held (worker only chunks the reply, never re-decides).
- **Files:** `src/voice/worker.py`, `src/api/persistence.py`, `src/api/operate.py`, `web/app/operate/live/page.tsx`, `web/lib/operate-types.ts` + tests.
- **Real-phone-only limits:** ElevenLabs synthesizing the chunked text naturally vs. as one utterance, and the true wall-clock monitor lag, can only be confirmed on a live call (the sim uses silence-frame TTS — it proves the chunk plumbing + partial writes + commit, not audio timing).

# Failure-Mode Report

*Purpose: enumerate the ways an autonomous sales agent (and its self-improvement loop) can fail, and name the concrete, code-grounded guard that defends against each — plus the residual failure modes that are NOT yet eliminated.*

This report is grounded in the actual repository as built (units U1–U16; 224 tests passing). Every guard below cites the file that implements it. Where a defense is partial or unproven, it is labeled honestly in the **Residual** section.

---

## How to read this

The system fails in two arenas:

1. **In-call** — the live agent does something harmful or stupid to a prospect (pushes too hard, quotes a price it shouldn't, hallucinates a fact, mishandles a minor).
2. **In-loop** — the self-improvement engine promotes a *worse* agent because it was fooled (over-claims a lift from noise, optimizes against obedient bots, regresses a guardrail to win the headline KPI).

Each failure mode below is paired with the deterministic guard that defends against it. The design bias is **neuro-symbolic**: the LLM proposes, but deterministic code has the final say wherever safety or integrity is at stake.

---

## Part 1 — In-call failure modes (the live agent)

### FM-1. Pushing a prospect who is signaling they want to leave
**Guard: `pushiness_cap` gate — `src/core/gates.py`.**
A pressure act (`attempt_close` or `pitch` — the `_PRESSURE_ACTS` set) is backed off to a low-pressure act when either trigger fires: a **signal** trigger (`bail_risk` ≥ the `pushiness_cap` threshold) or a **count** trigger (consecutive pressure acts ≥ `pushiness_pressure_count_cap`). When tripped, the act is rewritten to `answer_via_kb` (if the prospect has an open question) or `confirm_known`. The thresholds are read from config (`champion_v0.yaml`: `pushiness_cap: 0.7`, `pushiness_pressure_count_cap: 2`), falling back to `gates._DEFAULTS` (`0.8` / `2`) for any omitted key.

### FM-2. Quoting price / opening budget before trust is established
**Guard: `price_gate` — `src/core/gates.py` (covers acceptance example AE2).**
Any act that would open price/budget talk (`ask` / `answer_via_kb` / `confirm_known` on the `budget` slot) is overridden to a value `pitch` *unless* the trust event is satisfied: the prospect asked about price unprompted, OR `trust` ≥ `trust_gate_open_price` (default `0.5`), OR the required non-budget discovery slots are filled (`discovery_slots_required`, default `4`). The agent re-anchors on value rather than quoting prematurely.

### FM-3. Re-asking for information the agent already has (within or across calls)
**Guard: `skip_known` gate — `src/core/gates.py`, plus cross-call hydrate.**
An `ask` for a slot already filled at/above lock confidence (`_LOCK_CONFIDENCE = 0.8`) is rewritten to `confirm_known`. Across calls, per-lead memory hydration (U6) seeds known slots from the prior call (keyed by phone-hash) so the gate treats them as known on the next call.

### FM-4. Advancing to close while an objection is still open
**Guard: `must_clear_objection` gate — `src/core/gates.py`.**
An `attempt_close` proposed while `belief.active_objection` is set is overridden to `handle_objection`. The agent cannot pivot to the close with an unresolved objection on the table.

### FM-5. Improvising on an EXTREME moment (illegal/over-band concession, human request, compliance territory, repeated low confidence)
**Guard: `escalation_triggers` gate (final-say) — `src/core/gates.py`; runtime handling — `src/voice/escalation.py`.**
The escalation gate is checked **first AND last** in `apply_gates()`: first so an extreme moment short-circuits everything, and again after the other gates so a gate-introduced concession can't slip past. Triggers: a proposed concession beyond `max_concession_band` (default `0.15`), an explicit `human_request`, `compliance` territory, or `escalate_low_confidence_turns` (default `2`) consecutive low-confidence turns. When tripped, the decision becomes `escalate` and `belief.escalation_imminent` is set. `src/voice/escalation.py::handle_escalation` then produces an **in-persona, deterministic deferral** ("a specialist will follow up", persona-named, NO LLM on this path so it can never stall), secures a structured next step, and writes an `EscalationLog(lifecycle="unreviewed")` to the review queue. No live human is required (covers AE4).

### FM-6. Hallucinating a fact (price, policy, competitor claim) not supported by the KB
**Guard: deterministic groundedness check — `src/kb/retriever.py::grounded`, enforced in grading via `src/loop/grading.py::episode_groundedness`.**
Factual agent acts (`answer_via_kb`, `pitch`, `handle_objection`, `confirm_known`) are checked against the retrieved KB chunks; an unsupported claim becomes a `GroundednessViolation` carrying the offending tokens. The KB version queried is pinned per turn (`kb_version`). The authored KB lives in `src/kb/content/` (`programs.json`, `pricing.json`, `policies.json`, `competitors.json`, `objections.json`). *Note: groundedness here means consistency-to-KB; the KB is domain-plausible demo data, not real-world-verified — see the limitations memo.*

### FM-7. Selling to (or recording) a minor without parental consent; recording without consent; logging raw PII
**Guard: the compliance core — `src/voice/consent.py` (R33/R40/R41/R42).**
- **Consent state machine (`ConsentGate`)**: a call may not be recorded until `state == ready` with recording granted (`can_record`); conversation is blocked until `ready` or `unrecorded` (`can_converse`). The API 409s on `/api/chat` until consent is satisfied (`src/api/server.py`).
- **Jurisdiction awareness**: `consent_mode()` returns `all_party` for two-party-consent states; recording is gated regardless of mode.
- **Refusal path**: `RefusalPolicy` decides proceed-unrecorded (default) vs end on refusal.
- **Minor detection (`detect_minor`)**: a suspected minor forces `need_parental`, which **blocks** conversation until parental consent is granted or denied (denied → call ends).
- **PII scrub (`scrub_pii`)**: emails / phones / addresses / SSNs / cards / full names are redacted to placeholders **before** any transcript body is stored. The lead key is the phone-**hash**, never the raw phone.

### FM-8. A barge-in or discarded speculative turn corrupting committed state
**Guard: turn-commit side-effect safety — `src/voice/session.py`.**
Under preemptive/speculative generation, each turn is buffered in `pending[speech_id]`; `commit_turn()` is the **only** writer of session state. `on_barge_in()` / `discard_speculative()` drop the pending turn with **no** state write, so an interrupted TTS or a discarded speculative turn leaves committed belief/history/turns intact.

---

## Part 2 — In-loop failure modes (the self-improvement engine)

### FM-9. Getting "great at selling" to obedient bots that buy when talked at
**Guard: the honest, deterministic buy-gate — `src/sim/prospect.py::buy_gate`.**
The prospect LLM drives only *talk* and proposes bounded deltas to the SOFT drivers (`trust`/`need`/`urgency`/`purchase_intent`, clamped to ±0.2). Commit/walk is a **pure, deterministic numeric gate** on the TRUE hidden drivers + the persona's hard ceiling + the tier the agent OFFERS. Key honesty properties:
- A commitment is **never spontaneous** — it fires only on an `attempt_close`, at the offered tier (made close-triggered in commit `c2ceb5b`, "fix: make prospect commitment close-triggered, not spontaneous").
- `budget` and the qualified/disqualifier label are **structural facts** the sim LLM can never move by talking (`_apply_soft_deltas` ignores any non-soft key). A `no_budget` persona can never cross the budget gate no matter how fluent the agent is.
- Over-offering (closing at a rung the drivers don't support) is declined, so close-*timing* and *tier* genuinely matter.

### FM-10. Over-claiming a KPI lift that is really sampling noise
**Guard: bootstrap-CI significance rule — `src/loop/grading.py::compare_versions`.**
`challenger_better` is True **only if** the delta > 0 **and** the delta's bootstrap CI excludes 0 (`delta_ci[0] > 0`). A lift inside the noise band fails the gate. The CIs are computed with a seeded `random.Random` (pure stdlib, reproducible — `src/loop/_stats.py`).

### FM-11. A judge that grades its own house style, or flips on prompt order
**Guard: cross-family + position-bias-mitigated pairwise judge — `src/loop/grading.py`.**
`JUDGE_MODEL` defaults to a non-Anthropic family (`openai/gpt-4o`) so a model never judges its own style (KTD5). `judge_pairwise` asks both orders (a,b) and (b,a); if they disagree, the verdict is a conservative **tie** (an order-sensitive judge never casts a deciding vote). Unparseable JSON → tie.

### FM-12. Reporting a headline number before the automated judge is calibrated to humans
**Guard: the R39 headline gate — `src/loop/grading.py::can_report_headline`.**
A headline KPI may be reported **only** when the human-calibration sample is large enough (`n ≥ min_sample`) **and** judge-vs-human agreement clears `min_agreement`. Otherwise the number is withheld. (The concrete `min_sample` / `min_agreement` are caller-supplied per R39; the gate enforces them, but no calibrated human sample exists yet — see the limitations memo.)

### FM-13. A challenger winning the headline KPI while quietly regressing a guardrail
**Guard: guardrail-regression block in the promotion gate — `src/loop/grading.py::guardrails_regressed` + `src/loop/promotion.py::evaluate_promotion`.**
A challenger regresses if it pushes harder (higher `pushiness_rate`) OR makes more false promises (more groundedness violations) than the champion — either one blocks promotion. The bar is a conjunction: `challenger_better AND (not guardrails_regressed) AND (qualification accuracy held within tolerance)`.

### FM-14. A multi-change challenger that's impossible to attribute
**Guard: minimal-diff containment — `src/loop/generator.py::is_minimal_diff`.**
Every generated challenger declares exactly **one** changed dimension (prompts / playbooks / thresholds / persona), enforced at construction in `_build_challenger` (raises if >1). A multi-dimension diff is rejected by the gate, so any measured lift is attributable to a single mechanism.

### FM-15. Auto-shipping a high-stakes change (pricing concession, persona overhaul) without a human
**Guard: extreme-diff human-approval block — `src/loop/generator.py` + `src/loop/promotion.py` (covers AE5/AE6).**
A diff touching a pricing-concession threshold (`max_concession_band`, `trust_gate_open_price`) or the persona is flagged `is_extreme`. An extreme challenger that clears the bar is held as `pending_approval` (`requires_human=True`) instead of auto-promoting; a clean non-extreme pass auto-promotes with post-hoc review.

### FM-16. **The central risk** — an agent that wins in sim and fails with a real human (sim-to-real gap)
**Guard: the sim-to-real divergence PAUSE — `src/loop/sim2real.py` → `src/loop/promotion.py::evaluate_promotion`.**
The sim-to-real harness runs matched scenarios through both the text-sim arm and the (LiveKit-free) voice arm on the same scripted prospect turns, and computes a `divergence_score ∈ [0,1]` (decision-disagreement + ladder gap + belief-trajectory distance, with ASR-style noise injected into the voice arm's perceived text). `evaluate_promotion` checks this **first**: if `divergence > divergence_threshold` (default `0.2`), the decision is `paused` and *nothing* promotes — divergence is a **gate, not a report** (R36). `divergence=None` means "not yet measured" and the pause check is skipped.

### FM-17. Mined real-call failures leaking into the eval set (training on the test)
**Guard: frozen held-out set + R22 exclusion on rotation — `src/loop/experiment.py`.**
The held-out adversarial set is frozen and deterministically reproducible (`frozen_held_out(seed, n, rotation_index)`). `rotate()` advances the rotation while **excluding** any R22-mined persona ids, re-keying personas by a content hash so exclusion is meaningful (mined failures feed the training population only).

---

## Part 3 — Known residual failure modes (honest)

These are failure modes the system **does not** fully eliminate. They are stated plainly because honesty is the project's core value; the limitations memo expands on each.

- **R-A. The sim-to-real gap is mitigated, not eliminated.** The divergence pause (FM-16) only triggers once a divergence has been *measured*; if no matched voice runs are performed, `divergence=None` and the pause is skipped. The gap measurement is built and unit-tested, but no large real-human voice batch has been run.
- **R-B. The synthetic-distribution caveat.** Persona calibration runs on a synthetic seed because the promised PII-substituted transcript corpus has not arrived. Until it does, any "calibrated to a real distribution" claim is **synthetic-distribution lift**, not "realistic" (per the brainstorm's Dependencies).
- **R-C. Qualification accuracy is a v0 behavioral proxy.** `qualification_accuracy` (`src/loop/experiment.py`) infers the agent's verdict from the ladder tier it reached (≥ consultation ⇒ "judged qualified") because there is **no explicit `disqualify` act yet**. This conflates "judged unqualified" with "tried and failed to close." The code documents this limitation in its own docstring.
- **R-D. The six latent drivers are not yet empirically validated at scale.** The driver-validation gate exists (`src/loop/validation.py`, U11) but the pilot annotation / factor analysis has not been run on a real corpus; the belief-state schema itself flags 1–2 drivers (notably `urgency`) as likely to drop.
- **R-E. Live voice behavior is not CI-tested.** The voice **parity** (a scripted voice session produces the same decisions as text) and the LiveKit **wiring** are tested, but real turn-taking, barge-in timing, and ASR recovery are verified manually at demo time, not in CI (`web/README.md`, `src/voice/agent.py` import guard).
- **R-F. No real CRM / PSTN.** Bookings are mock; the phone channel (PSTN/SIP) is out of scope; web voice is the demo channel.
- **R-G. Compliance is built but legally unreviewed.** The minor/consent/PII gates are implemented and unit-tested, but legal review (COPPA/FERPA, all-party recording statutes) is **required before any real-human trial**.

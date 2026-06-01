# Limitations Memo

*Purpose: state honestly what this system does NOT prove, where the risks live, and what must happen before any claim of real-world performance. Honesty is the project's core value; this memo is deliberately unflattering.*

---

## The headline limitation: the sim-to-real gap is mitigated, not eliminated

The central risk the whole project is engineered around is the **sim-to-real gap** — an agent can become excellent at selling to synthetic prospects and still fail with a real human. The system treats this as a *gate*, not a report:

- `src/loop/sim2real.py` measures a `divergence_score` between matched sim and (LiveKit-free) voice runs.
- `src/loop/promotion.py::evaluate_promotion` **pauses** the loop (no promotion) when `divergence > 0.2` (R36).

**But the mitigation only fires once a divergence has actually been measured.** When `divergence=None` (not yet measured), the pause check is *skipped* — promotions proceed on sim evidence alone. As of now, **no large real-human voice batch has been run**, so the gap is bounded by design but not yet quantified by data. The pause is a real safety mechanism; it is not yet a *demonstration* that the gap is small.

---

## The synthetic-distribution caveat (the fallback we are currently in)

The brainstorm's Dependencies section is explicit: the PRD promised a small database of **PII-substituted sales transcripts** for persona calibration and tactic-mining. **That corpus has not arrived.** Until it does:

- Persona calibration and tactic-mining run on a **synthetic seed** (`src/sim/personas.py`, hand-authored archetypes + a seeded ~25% unqualified fraction).
- Any "calibrated to a real distribution" claim is, honestly, a **synthetic-distribution lift** — not "realistic." A lift the loop measures is a lift *against our own synthetic population*, which shares the agent's blind spots far less than real callers would but is still our construction.
- In this fallback, **the real-human trial becomes the only non-synthetic evidence**, and must be sized accordingly. Swap the seed for the corpus when it lands; re-label results as "realistic" only then.

---

## Qualification accuracy is a v0 behavioral proxy

`src/loop/experiment.py::qualification_accuracy` does **not** read an explicit agent judgment. It infers the agent's verdict from the **ladder tier it reached**: tier ≥ consultation ⇒ "judged qualified", else "judged unqualified", and scores agreement with the persona's ground-truth label.

The limitation is documented in the code's own docstring: **there is no explicit `disqualify` act yet**, so this proxy **conflates "judged unqualified" with "tried but failed to close."** An agent that mishandles a genuinely-qualified prospect and ends low-tier is scored *as if it correctly judged them unqualified* — which flatters the metric. A later version must add an explicit disqualify decision and read that instead. Treat the current qualification-accuracy number as directional, not precise.

---

## The six latent drivers await empirical validation

The factored six-driver belief is the design's novel contribution (see research notes), and the belief-state schema itself expects **1–2 drivers to drop** under factor analysis (`urgency (felt)` is flagged "novel / weak"). The validation machinery exists — `src/loop/validation.py` (U11) gates the policy/experiment on validated drivers and degrades gracefully if one drops, with a `price_sensitivity`-grounded fallback experiment named for the discovery-sequencing experiment. **But the validation has not been run at scale** (it needs the transcript corpus + a human-annotated slice). So the drivers are a *hypothesis the gate is built to test*, not a validated feature set.

---

## Live voice behavior is NOT CI-tested

What **is** tested (within the 224 passing tests):
- **Parity** — a scripted voice session produces the same decisions as the equivalent text session (`tests/integration/test_voice_adapter.py`; the sim2real `voice_noise=0` parity floor).
- **Wiring** — the LiveKit hooks delegate correctly; the module imports cleanly with `LIVEKIT_AVAILABLE=False` when livekit-agents is absent (`src/voice/agent.py` import guard).
- **Side-effect safety** — barge-in / discarded speculative turns write no committed state (`src/voice/session.py`).

What is **NOT** tested in CI:
- Real-time turn-taking, barge-in *timing*, ASR-noise recovery on live audio, and end-to-end latency. These are verified **manually at demo time** (`web/README.md`: "browser interactivity and a live LiveKit room cannot be exercised in CI"). The latency budget (R43) is *designed for* (concurrent DST+RAG, off-critical-path writes, cached filler) but **not benchmarked from a real egress region** — the plan's own STT/TTS numbers disagree 3–4× and require local benchmarking before any latency claim.

---

## No real CRM, no PSTN, mock bookings

- **No CRM integration** — bookings capture details and "confirm (mock calendar)"; per-lead memory is a mocked store (brainstorm "Out of scope").
- **No phone (PSTN/SIP) channel** — web voice is the demo channel; PSTN is deferred.
- **Single-tenant, no production hardening** — multi-tenant operation and always-on (continuous) self-play are out of scope; the loop is batch.

---

## Compliance is built but legally unreviewed — BLOCKING for any real-human trial

`src/voice/consent.py` implements jurisdiction-aware recording consent (all-party detection), a refusal path, suspected-minor detection forcing a parental-consent gate (COPPA/FERPA intent), and deterministic PII scrubbing (phone-hash key, raw phone never stored). It is unit-tested (`tests/unit/test_consent.py`).

**However:**
- The minor detector and the all-party-state list are **heuristic** (regex cues, a hard-coded state list) — they will have false negatives/positives on real callers.
- PII scrubbing is regex-based and will miss adversarial or unusual formats.
- **No legal review has been performed.** Recording statutes, COPPA, and FERPA obligations must be reviewed by qualified counsel **before any real human is recorded or any minor's data is touched.** Treat the compliance core as a *good-faith engineering scaffold*, not legal sufficiency.

---

## Documentation drift to be aware of

- `README.md` lists "Deepgram (STT)" in the stack line, but the build defaults STT to **ElevenLabs Scribe v2 Realtime** (`src/voice/agent.py`, `.env.example`); Deepgram is an optional fallback. The README line is stale (see the decision log, D-9).
- The plan's KTD6 names Deepgram Nova-3 for STT; the build diverged to ElevenLabs Scribe. This is a vendor swap, not a capability change, and was never load-bearing for any claim — but it is a real plan/build divergence and is recorded honestly in the decision log.

---

## What this system HONESTLY demonstrates today

To be precise about the claim surface:
- A **complete, transport-agnostic belief-state brain** with deterministic safety gates, grounded RAG, and cross-call memory — built and unit-tested.
- A **complete, honest self-improvement loop** — generator → frozen-set experiment → hybrid grading (deterministic groundedness + cross-family pairwise judge + bootstrap CIs) → an over-claim-resistant promotion gate with a sim-to-real pause and human-approval for extreme changes — built and unit-tested (224 tests passing).
- A **runnable demo + operator dashboard** (consent-gated text chat works without any voice creds; live voice needs LiveKit creds).

What it does **not** yet demonstrate: a *measured* KPI lift from a real model batch, a *measured* sim-to-real gap, validated drivers, or any real-human outcome. Those are one real run and one trial away — and that gap between "the machine is built and correct" and "the machine has been run against reality" is stated here so it is never mistaken for proven performance.

---
date: 2026-05-31
topic: autonomous-voice-sales-agent
---

# Autonomous AI Voice Sales Agent — Requirements

## Summary

A fully-autonomous web-voice AI sales agent for tutoring (Nerdy / Varsity Tutors) that runs discovery-to-appointment calls as a *warm consultative advisor*, remembers callers across calls, answers from a grounded knowledge base, and decides in real time what to ask, when to pivot to close, and when to gracefully defer. A batch self-improvement loop mines real and simulated calls into minimal-diff variants, tests them against honest hidden-utility synthetic prospects on a frozen adversarial set, and promotes improvements with light human oversight — all observable and operable from a single Operate / Improve dashboard.

## Problem Frame

Nerdy relies on live phone agents to gather information and close individual tutoring sales. The hypothesis is that an AI agent could meet or exceed human sales benchmarks, which would yield consistency, 24/7/365 coverage, and the ability to experiment on sales tactics at scale. The hard part is not the voice plumbing — it is building an agent that *actually sells* to prospects who hesitate, push back on price, and disqualify themselves, and proving it improves against a KPI that is meaningful rather than flattering. Submissions fail when they optimize against obedient synthetic leads and report a rising win rate that collapses in front of a real human. The central risk this project must manage is the **sim-to-real gap**.

## Key Decisions

- **Optimize in text, deliver in voice.** The agent brain is channel-agnostic; the improvement loop runs at high N as text self-play (cheap, statistical), and the winning version is served over voice. The *same brain* runs in both — but only for channel-agnostic behaviors; voice-only behaviors (turn-taking, barge-in, ASR recovery) are validated on the voice path before a text-winner is trusted (see R37).
- **Belief-state dialogue brain, precedent-grounded.** A modular task-oriented dialogue system (DST → gated policy → NLG) with a factored belief state, grounded in published precedent rather than invented (see `docs/belief-state-schema.md`).
- **Human on the loop, not in it (everywhere).** Promotions auto-ship and live calls run fully autonomous; a human reviews after the fact and is pulled in only for extreme cases. Maximizes the 24/7-autonomy claim.
- **The KPI is hardened against flattery.** Success is measured on a *frozen held-out adversarial* prospect population, scored *with qualification accuracy* (correctly releasing an unqualified lead is a win; booking a junk lead is a penalty) — not raw conversion against agreeable bots.
- **Honest synthetic prospects.** Each simulated prospect has a hidden numeric utility (budget/need/trust/urgency/patience) that gates purchase, plus a hidden ground-truth qualification label; a deliberate fraction are genuinely unqualified.
- **Batch improvement cadence.** Experiments run in batches (scheduled + failure-accumulation triggered), which keeps the frozen-set statistically honest (few enough promotion decisions to avoid multiple-comparisons drift).
- **First improvement dimension: discovery sequencing** (when price/budget enters the conversation) — a clean, mechanism-visible before/after tied to the trust gate.
- **The agent discloses it is an AI assistant.** Sounding human ≠ pretending to be human; transparency is the honest bar and increasingly a legal one.
- **Success is relative, not absolute.** A statistically significant lift from a documented v0 baseline + an honestly-reported sim-to-real gap + a small real-human trial — no pre-committed arbitrary numbers.
- **Voice stack: LiveKit self-hosted (locked).** Full control of the reasoning seam — grounding, decisioning, escalation, version-tagging — and it lets the same brain run in text and voice; lowest cost at scale. Chosen over Pipecat (closest alternative) and the managed / speech-to-speech stacks.
- **Membership / subscription pricing.** The agent sells toward a monthly tiered tutoring membership (Nerdy's actual model); price talk is about a recurring commitment and value-per-month.
- **The win is a weighted commitment ladder.** Same-call enrollment > trial session > consultation > callback > none. The agent secures the best tier the prospect supports, scored by weighted commitment and gated by qualification — honest prospects + the pushiness guardrail prevent over-pushing.
- **Honest buy decision via deterministic thresholds.** The prospect LLM drives talk and resistance, but commit/walk is a hard numeric gate on the hidden drivers — the agent cannot be persuaded past it by ungrounded talk.
- **SPIN-grounded playbooks.** v0 discovery + rebuttals are grounded in SPIN (Situation → Problem → Implication → Need-payoff) plus BANT qualification discipline — low-pressure (fits the warm advisor + need_intensity driver), and its Implication→Need-payoff ordering is the substrate for the discovery-sequencing experiment. Sandler/Challenger rejected as misfit for anxious-parent buyers + the anti-pushiness guardrail.

## Actors

- A1. **Prospect** — a parent or student. A real human in the demo / real-call surface, or a simulated hidden-utility persona during training. Talks to the agent over web voice or text.
- A2. **Sales Agent** — the versioned brain + consistent advisor persona that runs the call.
- A3. **Operator** — the human running the system via the dashboard: reviews calls and escalations, approves extreme promotions, can manually take over a live call, drives experiments.
- A4. **Prospect Simulator** — generates the calibrated population of honest hidden-utility personas the agent practices against.
- A5. **Improvement Loop** — the batch engine that mines episodes, generates challengers, runs self-play experiments, grades, and promotes.

## Key Flows

- F1. **Inbound discovery-to-appointment call.** **Trigger:** prospect initiates (warm). Agent hydrates per-lead memory, confirms known info (skip-when-known), runs prioritized discovery, answers from KB, pivots to close once trust + required slots are satisfied, books a qualified appointment (or correctly disqualifies). Full transcript + decisions + belief trajectory + version tag logged.
- F2. **Outbound call.** **Trigger:** agent initiates (cold-ish). Distinct opening playbook that earns the right to the conversation before discovery; otherwise as F1.
- F3. **Per-turn decision cycle.** Utterance → hybrid DST updates belief state (deterministic slots + LLM latent drivers + derived trends) → gated policy selects the next system act → NLG realizes it in persona → state + decision logged.
- F4. **Escalation.** **Trigger:** extreme moment (concession beyond band / explicit human request / compliance). Agent stays in-persona, gracefully defers ("a specialist will follow up"), secures the next-best step, and logs the escalation for post-hoc review. Low-confidence and ordinary objections are self-recovered, not escalated. Operator may optionally take over live.
- F5. **Batch improvement cycle.** Mine scored/tagged losing + winning episodes → generator proposes minimal-diff challengers → text self-play vs the calibrated population → grade on the frozen adversarial set + guardrails → HOTL-promote (auto, or block if extreme) → promoted challenger becomes the new version-tagged champion.

## Requirements

**Conversation & persona**
- R1. Real-time bidirectional web voice (WebRTC) with latency low enough for natural turn-taking and barge-in. A text console is a second channel into the identical brain (used for dogfooding and as the human-driven analog of the sim harness).
- R2. Handle leads with full, partial, or no prior info, hydrating any existing state from per-lead memory at call start.
- R3. A single consistent agent persona: a warm consultative advisor (a "learning/enrollment advisor") — empathetic, educational, low-pressure. Two TTS voices, **sticky per caller**: a returning caller always hears their assigned voice; a new caller is assigned one at first contact that then sticks. The agent discloses it is an AI assistant.
- R4. Support both inbound (prospect initiates) and outbound (agent initiates) with distinct opening playbooks; the rest of the call converges.

**Discovery, decisioning & knowledge (the brain)**
- R5. A belief-state dialogue brain: per-turn hybrid Dialogue State Tracking (deterministic slot extraction + LLM latent-driver updates + deterministically-derived trend/velocity features) → a gated neuro-symbolic policy (LLM proposal + deterministic gates) → NLG.
- R6. The belief state is the factored three-layer schema in `docs/belief-state-schema.md`: discovery slots (BANT + tutoring, value + confidence), latent drivers (trust, need_intensity, price_sensitivity, urgency [provisional], purchase_intent [measured directly], engagement/bail_risk), and dialogue-control meta. The latent driver set is a hypothesis pending validation against the transcript corpus.
- R7. A prioritized script of required and leading questions with dynamic ordering and skip-when-already-known, driven by slot confidence — including skipping info already known from prior calls.
- R8. Knowledge-base lookup for policy, objection, and competitive answers, with grounded responses: the agent states no fact not supported by the KB.
- R9. Real-time decisioning: select the next act (ask / answer-via-KB / pitch / handle-objection / attempt-close / escalate); pivot-to-close gated on trust + required-slots; correctly qualify and disqualify.
- R10. Live calls are fully autonomous. The agent self-recovers low-confidence turns and ordinary objections. Extreme cases (a pricing concession beyond the allowed band, an explicit request for a human, compliance-risky territory) trigger an in-persona graceful async deferral plus securing the next-best step; the operator may optionally take over manually. Escalations are reviewed post-hoc, never block the live call on a human.

**Memory, identity & versioning**
- R11. Cross-call per-lead memory persists slots, prior objections, outcome, assigned voice, and persona read; the agent "remembers conversation history across calls."
- R12. An agent version is defined as `{prompts, playbooks, policy thresholds}` plus pinned references to `{model, architecture, KB version}`. The live champion is version-controlled; past versions are saved and rollback is supported. The KB is versioned independently and pinned by reference (each version records which KB it ran against). Model weights are frozen.

**Simulation & honest prospects**
- R13. The prospect simulator generates prompted hidden-utility personas using a different model than the agent. Each persona has a hidden utility (budget ceiling, need, trust, urgency, patience) that gates purchase, plus a resistance repertoire; population distribution is calibrated to the transcript corpus.
- R14. Each persona carries a hidden ground-truth qualified/unqualified label + disqualifier reason (no-budget / no-authority / no-need / no-fit / no-urgency). A deliberate fraction of the population is genuinely unqualified and behaves accordingly (resists, walks if pushed).
- R15. One calibrated population is shared across experiments; each per-dimension experiment measures on the relevant slice rather than a hand-built per-dimension cohort (no cherry-picking surface).

**Improvement loop**
- R16. A batch self-improvement loop runs champion vs challenger(s), triggered on a schedule and by failure accumulation.
- R17. A challenger is the champion plus a single declared minimal diff (containment — it may only edit its declared dimension). Generation is failure-conditioned (cluster losing episodes → targeted diffs) + tactic-mined (winning episodes → playbook candidates), using LLM proposal for text dimensions and parametric search for numeric dimensions.
- R18. Every call (real and simulated) is annotated by an LLM note-generator with a plausibility/usefulness score, type tags, and notes. Episodes are curated by score; the score/category gates whether a cluster auto-triggers an experiment or routes to a human-review queue.
- R19. Promotion is human-on-the-loop by default: a challenger that clears the bar auto-promotes immediately with post-hoc review and rollback. A blocking human approval is required only for extreme changes (a guardrail tripwire fires, or the diff touches a pricing concession or persona overhaul).
- R20. The promotion bar is a statistically significant lift on the headline KPI measured on a frozen held-out adversarial set (with periodic light rotation), with zero guardrail regression and qualification accuracy maintained.
- R21. The mutation surface is prompts + playbooks + policy thresholds only. Model fine-tuning is out of scope for v0.
- R22. Real-call failure modes the simulator never produced are mined back into the simulator as new prospect behaviors (the sim-to-real tightening loop).

**Measurement & integrity**
- R23. Grading is hybrid: deterministic KB-groundedness checks plus LLM-judge rubrics (a different model than the agent, multiple judges on high-stakes calls) gate the loop at scale; a periodic human-rated sample calibrates the judges (and the note-generator scorer) and produces the headline "sounds human" score and the sim-to-real gap measurement.
- R24. Guardrail metrics are defined and enforced at promotion: groundedness/hallucination (every factual claim supported by the KB), pushiness (judge-scored pressure/aggression), and false-promise (claims beyond KB/policy). Promotion requires guardrails clean.

**Observability & dashboard**
- R25. A single web app with two modes. **Operate:** live-call monitor with real-time belief state + decisions, call review, KPI views, an escalation review queue, and an optional manual takeover control. **Improve:** experiment lab (launch/watch experiments, before/after evidence, promote/retire), the human-approval queue for extreme promotions, a KB/playbook editor, and version history + rollback.
- R26. Full transcript, outcome, per-turn decisions (next-question / pivot / escalate), and belief trajectory are captured per call and tagged with the serving version. Real and simulated calls are normalized to one episode schema.
- R27. Dashboards track the chosen KPIs per agent version and cohort, so performance is attributable to the version that produced it.

**Sale, objections & robustness**
- R28. The agent sells toward a monthly tiered tutoring membership; KB price content and objection handling are framed around a recurring commitment and value-per-month.
- R29. The win is a weighted commitment ladder — same-call enrollment > trial session > consultation > callback > none. The agent secures the highest tier the prospect supports; "qualified" = required slots filled + genuine need + decision-maker reachable + a budget signal in range + adequate fit + sufficient urgency (covering all five R14 disqualifier dimensions). Booking = capture the required details (need, decision-maker, scheduled time, contact) and confirm (mock calendar).
- R30. The agent handles the full nine-objection tutoring taxonomy with KB-grounded rebuttals: price/affordability, efficacy doubt, DIY/free alternatives, timing, decision-maker, scheduling, online-vs-in-person skepticism, student resistance, and trust/legitimacy.
- R31. Sim buy decision: the prospect LLM drives conversation and resistance, but commit/walk is a deterministic numeric gate on the hidden drivers (commit to a tier when its thresholds are crossed; walk on patience-zero or trust-below-floor). The agent cannot be persuaded past the gate by ungrounded talk.
- R32. Comprehensive in-call edge handling: silence (timeout → prompt → graceful wrap + callback), disconnect (save state + log partial outcome), ASR-uncertainty (clarify/repeat, never act on garbled input), hard-no / abusive / off-topic (in-persona redirect then graceful exit), plus outbound retry + voicemail detection and abuse filtering.
- R33. Recording and consent on real calls: the agent discloses it is an AI assistant and that the call is recorded for quality/training, and proceeds only on consent; real-trial transcripts are PII-scrubbed before entering storage or the loop.
- R34. The dashboard tracks a comprehensive sales-KPI set per version and cohort, alongside the headline weighted-commitment-ladder score and the integrity guardrails: ladder-tier breakdown (enrollment / trial / consult / callback / none rates), objection-recovery rate (overall and per objection type), escalation rate, disqualification rate, talk-listen ratio, average turns + duration, slot/discovery completeness, per-archetype conversion, time-to-pivot, abandon timing, and per-stage dwell.
- R35. The v0 discovery and rebuttal playbooks are grounded in SPIN (Situation → Problem → Implication → Need-payoff), supplemented by BANT-style qualification discipline. The Implication→Need-payoff ordering is the substrate for the discovery-sequencing experiment.

**Review hardening (round 1)**
- R36. The simulator's buy-gate calibration must be validated against real commit/walk outcomes before promotions are trusted, and a sim-to-real divergence threshold must *pause* the loop (not merely report the gap) — turning the central sim-to-real risk into a gate rather than a post-hoc report.
- R37. The "optimize in text" approach is scoped to channel-agnostic behaviors (discovery sequencing, objection content, policy thresholds). Voice-only behaviors (turn-taking, barge-in, ASR-noise recovery) are validated on the real-voice path, and a text-optimized challenger must hold its lift on a small real-voice batch before promotion.
- R38. The six latent drivers (R6) are validated as a gating milestone before the policy gates, sim thresholds, and improvement experiment depend on them; gates degrade gracefully if a driver is dropped. A price-sensitivity-grounded experiment is the named fallback for the discovery-sequencing experiment if the trust driver (counseling-precedent-only) fails validation.
- R39. Headline "sounds human" and sim-to-real numbers require a stated minimum human-rated sample size and a judge-vs-human agreement threshold (the automated judges must agree with human ratings above that bar) before being reported.
- R40. Minors' data: detect when a caller is or represents a minor (student), require verifiable parent/guardian consent before recording or storing a minor's data, and state what is retained for a minor. COPPA and FERPA are the governing constraints.
- R41. Recording consent (extends R33) is jurisdiction-aware with all-party consent as the baseline, and defines the refusal path (proceed without recording, or end gracefully) when consent is declined.
- R42. Cross-call recognition uses the phone number as the stable lead key, retained for recognition but stored securely (hashed for matching; raw retained only where operationally needed, e.g., callbacks). The PII-scrub (extends R33) applies to transcript content across all real-call transcripts, not only the trial subset.
- R43. The per-turn decision cycle (F3) must fit the real-time turn-taking latency budget — e.g., fuse the state-update and policy decision into one model call, or run the latent-driver update asynchronously off the critical path — rather than deferring the whole question to tuning.
- R44. The dashboard's per-screen flows, navigation model, and screen states are resolved in a dedicated dashboard UI/IA spec (pages, features per page, transition states), required as an input for the dashboard implementation.

## Acceptance Examples

- AE1. **Covers R2, R3, R7, R11.** A returning caller with partial prior info reaches the agent; the agent uses that caller's sticky voice, opens with familiarity ("welcome back — last time we looked at SAT math"), and skips already-known slots rather than re-asking.
- AE2. **Covers R9.** A prospect raises price before trust is established; the agent re-anchors value and does not quote a discount, deferring price until engagement is signaled (the discovery-sequencing behavior).
- AE3. **Covers R9, R14, R20.** A genuinely unqualified prospect (no budget) is correctly identified and gracefully released; this counts as a *correct* outcome for qualification accuracy, not a lost sale.
- AE4. **Covers R10.** A prospect demands a discount beyond the allowed band; the agent does not improvise a concession — it defers ("a specialist will follow up on pricing"), secures the next-best step, and logs an escalation. No live human is required.
- AE5. **Covers R17, R19, R20.** A discovery-sequencing challenger improves qualified-appointment rate on the frozen set with guardrails clean and is non-extreme → it auto-promotes with post-hoc review available.
- AE6. **Covers R19.** A challenger that edits a pricing rebuttal clears the bar but is extreme → it is blocked pending human approval rather than auto-shipped.
- AE7. **Covers R8, R24.** A prospect asks how Nerdy compares to a named competitor; the agent answers using only KB-supported differentiators and invents no facts.
- AE8. **Covers R29.** A hot, qualified prospect (high trust + need) is guided to same-call enrollment; a warm-but-not-ready prospect is secured to a trial or consultation instead of being pushed to enroll — pushing the not-ready prospect would drop trust and risk a walk.
- AE9. **Covers R33.** On a real call the agent opens by disclosing it is an AI assistant and that the call is recorded for quality, and proceeds only after the prospect consents.

## Success Criteria

- The improvement loop moves the weighted commitment-ladder score (qualified) by a statistically significant margin from the documented v0 baseline, measured on the frozen held-out adversarial set, with guardrails clean and qualification accuracy held.
- Same-call enrollment rate is tracked and reported as a *distinct* primary metric (not blended into the ladder), so cheap mid-ladder commitments cannot mask a weak close rate; the qualification labels and ladder weights are validated/recalibrated against the real-human trial.
- The sim-to-real gap is measured and honestly reported (matched scenarios run in sim and over real voice; KPI and quality compared), not hidden.
- A small real-human trial is conducted, and the agent reads as a real consultative call (human-rated "sounds human"), not a script reader.
- Demo: a live web-voice discovery-to-appointment call, plus the dashboard surfacing transcripts, decisions, and KPIs, plus the documented before/after on the discovery-sequencing experiment.

## Scope Boundaries

**In scope**
- Web-voice + text-console agent, inbound + outbound, with the full belief-state brain, grounded KB, cross-call memory, the batch improvement loop on one dimension (discovery sequencing), honest hidden-utility prospects, and the Operate/Improve dashboard.

**Deferred for later**
- Model fine-tuning (prove the pattern with prompt/playbook/threshold variants first).
- Outbound-vs-inbound tactics as a second formal experiment dimension.
- Phone (PSTN/SIP) channel; swap the synthetic transcript seed for the provided corpus when it arrives.

**Out of scope**
- Real CRM integration and real-PII handling (the per-lead memory store is mocked).
- The WhatsApp channel.
- Production hardening / multi-tenant operation.
- Continuous (always-on) self-play — the loop is batch.
- A separately-documented monolithic architecture baseline (the loop's documented baseline is the hand-authored structured champion).
- Pre-committed absolute KPI targets.

## Dependencies / Assumptions

- The PRD promises a small database of PII-substituted sales transcripts. Until it arrives, persona calibration and tactic-mining run on a **synthetic seed**; if the corpus is late or absent, the "calibrated to a real distribution" claim remains synthetic — in that case results are reported as a *synthetic-distribution lift* (not "realistic"), and the real-human trial becomes the only non-synthetic evidence (sized accordingly). Swap in when available.
- **LiveKit self-hosted** is the locked voice stack (control over the reasoning seam).
- The authored knowledge base is domain-plausible demo data, not real-world-true; groundedness is defined as consistency-to-KB, so this is sufficient for the grounding requirement.
- The agent's headline KPI is a *weighted commitment-ladder score* (enrollment > trial > consultation > callback > none), gated by qualification; same-call enrollment is the ladder's top tier.

## Outstanding Questions

**Deferred to planning**
- Statistical test method for the promotion gate (significance test vs. Bayesian vs. sequential).
- Decision-log / episode schema concrete shape.
- Experiment concurrency within a batch.
- Voice latency budget tuning and barge-in handling specifics.
- KB ingestion / retrieval pipeline.
- New-caller voice-assignment rule (default vs. segment-based) before stickiness applies.

## Draft: Knowledge Base Content (for review — illustrative demo data, replace/refine)

- **Programs:** 1:1 online tutoring; small-group sessions; test prep (SAT/ACT/AP); subject tutoring K-12 and college; ongoing membership.
- **Pricing (illustrative):** monthly membership tiers bundling tutoring hours; per-hour ranges by program; test-prep packages tied to a test date. *Marked as demo data.*
- **Policies:** scheduling/rescheduling windows; cancellation terms; satisfaction guarantee with tutor re-match; refund window.
- **Competitive differentiators:** vetted 1:1 tutor matching + weekly progress tracking vs. marketplace sites (e.g., Wyzant); structured 1:1 vs. content libraries (e.g., Khan, Chegg); flexibility/cost vs. in-person centers.
- **Objection rebuttals (KB-grounded):** price → ROI / score-lift framing + tier options; efficacy doubt → vetted match + weekly tracking + re-match guarantee; DIY/free resources → structure + accountability + personalization; timing → urgency tied to the test/term date.

## Draft: Prospect Archetypes (for review)

A calibrated population weighted toward qualified-but-resistant, with a deliberate genuinely-unqualified fraction (~20–30%) so the agent regularly practices correct disqualification.

- **Anxious parent** — high need, mid budget, near-term test date. *Qualified.*
- **Budget-conscious shopper** — price-sensitive, comparison-shopping. *Qualified if value is anchored before price.*
- **Skeptic** — prior bad tutoring experience, efficacy doubt. *Qualified if trust is built.*
- **High-intent warm** — referral, ready to move. *Qualified, easy.*
- **Tire-kicker / researcher** — low intent, "just looking," no real timeline. *Often unqualified (no-urgency).*
- **Not-the-decision-maker** — student calling without a parent, or one parent without the other. *Unqualified until the decision-maker is reached.*
- **No-budget** — real need but cannot afford. *Genuinely unqualified (no-budget).*

## Sources / Research

- `docs/belief-state-schema.md` — the precedent-grounded factored belief-state schema (v0.2), per-driver provenance scorecard, reusable artifacts, and validation plan.
- Negotiation / persuasion + self-play precedent: Deal-or-No-Deal (RL self-play), CraigslistBargain (hidden reservation price, decoupled dialogue acts), Persuasion-for-Good (persuasion strategies), RESPER (resistance taxonomy), ICL-AIF (LLM self-play + AI critic), SalesLLM (ordinal buying-intent + calibrated customer personas), MIND (continuous willingness scalar).
- Dialogue-state precedent: MultiWOZ, Schema-Guided Dialogue (slot schema format), DSTC; POMDP / Information-State-Update dialogue management.
- Voice stack comparison (LiveKit / Pipecat / Vapi / Retell / OpenAI Realtime / Gemini Live / Bland) — recommendation: LiveKit self-hosted for reasoning-seam control.

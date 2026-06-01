# Autonomous Voice AI Sales Agent

A fully-autonomous web-voice AI sales agent for tutoring (Nerdy / Varsity Tutors): a belief-state
dialogue brain that runs discovery-to-commitment calls, grounds answers in a knowledge base,
remembers callers across calls, and improves itself through a batch self-play loop against honest
hidden-utility synthetic prospects — observable and operable from an Operate/Improve dashboard.

The architectural spine is a **transport-agnostic `respond()` core** (`src/core/`) that both the
headless text self-play loop and the live LiveKit voice session call, so what is optimized in text
is exactly what ships in voice.

## Docs
- Requirements: `docs/brainstorms/2026-05-31-autonomous-voice-sales-agent-requirements.md`
- Belief-state schema: `docs/belief-state-schema.md`
- Build plan: `docs/plans/2026-05-31-001-feat-voice-sales-agent-plan.md`
- Dashboard UI/IA spec: `docs/design/dashboard-ia-spec.md`
- **Deliverables + how to run the demo: `docs/deliverables/` (start with `demo-runbook.md`)**

## Setup
1. `cp .env.example .env` and fill in keys (see `.env.example`).
2. `docker compose up -d db` to start Postgres + pgvector (host port 5434).
3. Install deps (`pip install -e .` or `pip install --user ...`), then run the suite:
   `PYTHONPATH=. python3 -m pytest tests -q`.
4. Full demo (API + dashboard + a real-LLM smoke run): see `docs/deliverables/demo-runbook.md`.

## Stack
Python 3.10 · LiveKit Agents (voice) · **ElevenLabs Scribe v2 Realtime (STT) + ElevenLabs (TTS)**
(Deepgram is an optional fallback only) · OpenRouter (one key → Claude for the agent, a different
family for the prospect sim/judges) · local BGE embeddings · Postgres + pgvector · FastAPI (demo API)
· Next.js + React (Cadence Operate/Improve dashboard + the prospect demo console).

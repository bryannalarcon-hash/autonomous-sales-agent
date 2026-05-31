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

## Setup
1. `cp .env.example .env` and fill in keys (see `.env.example`).
2. `docker compose up -d db` to start Postgres + pgvector.
3. `pip install -e .` (Python 3.10+).
4. `pytest` to run the test suite.

## Stack
Python · LiveKit Agents (voice) · Deepgram (STT) · ElevenLabs (TTS) · Postgres + pgvector ·
Claude (agent) + a different family for the prospect sim/judges · Next.js dashboard.

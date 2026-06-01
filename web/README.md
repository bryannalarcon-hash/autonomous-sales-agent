# Sales Agent Web Console

The web front-end for the autonomous voice sales agent. Today it ships the prospect-facing **demo
console** at `/demo` (plan unit U13). The same Next.js app reserves `/operate` and `/improve` for the
operator dashboard built later (U15/U16).

Stack: **Next.js 14 (App Router) + React 18 + TypeScript**, styled with **Tailwind CSS**, web voice
via the **LiveKit JS SDK** (`livekit-client` + `@livekit/components-react`).

## Run

```bash
cd web
npm install
cp .env.local.example .env.local   # then edit if your backend isn't on localhost:8000
npm run dev                        # http://localhost:3000 → redirects to /demo
```

Production build:

```bash
npm run build
npm start
```

## Configuration

| Env var                | Default                 | Purpose                                   |
| ---------------------- | ----------------------- | ----------------------------------------- |
| `NEXT_PUBLIC_API_BASE` | `http://localhost:8000` | Base URL of the FastAPI backend. The app calls `${NEXT_PUBLIC_API_BASE}/api/...`. |

Only `NEXT_PUBLIC_*` values reach the browser. **No secrets live in client code** — the LiveKit
access token is minted server-side by the backend (`POST /api/livekit/token`) and only held in
memory for the duration of the room connection.

## The `/demo` flow

1. **Consent gate (R33/R40/R41)** — on load, `POST /api/consent/start` returns the AI-disclosure +
   recording notice. The prospect acknowledges AI, allows/declines recording, and flags whether a
   minor is involved. `POST /api/consent/respond` resolves a state:
   - `ready` → call enabled (recorded).
   - `unrecorded` → call enabled with a visible "not being recorded" banner.
   - `need_parental` → parental-consent step; re-submits with `parental_consent: true`.
   - `ended` → polite end screen.
   The text and voice surfaces stay **disabled** until the state is `ready` or `unrecorded`.
2. **Text console (R1)** — `POST /api/chat {session_id, text}` renders `{reply}`. A 409 surfaces as
   "consent required". The internal `decision_act` policy label only appears behind the `debug`
   toggle; it is never shown to the prospect.
3. **Voice (R3)** — "Start voice call" fetches `POST /api/livekit/token` and connects to the room.
   If the token endpoint returns **503** (LiveKit creds absent), the UI shows a clear
   "voice unavailable — use text console" fallback instead of crashing.

## Note on live behavior (manual verification)

The text console and voice path require the **running FastAPI backend** to do anything real, and
live voice additionally needs **LiveKit credentials** configured on the backend. Because browser
interactivity and a live LiveKit room cannot be exercised in CI, voice/text are verified **manually
at demo time**. CI's correctness bar is a clean `npm run build` plus faithful wiring to the API
contract in `lib/api-types.ts`.

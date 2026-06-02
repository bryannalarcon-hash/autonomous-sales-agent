// The EXACT API contract shared with the Python FastAPI backend (U13). These types mirror the
// request/response shapes for /api/consent/start, /api/consent/respond, /api/chat, and
// /api/livekit/token. Keep this file in lockstep with the backend — it is the single source of
// truth the client (lib/api.ts) and the demo UI type against.

/** Consent-flow states the backend may return. The call UI is gated on these. */
export type ConsentState =
  | 'ready' // consent fully satisfied → enable the (recorded) call
  | 'unrecorded' // consent given but recording declined → enable call, show "not recorded" banner
  | 'need_parental' // minor flagged → must collect parental consent and re-submit
  | 'ended'; // policy ended the session (e.g. consent refused where recording is required)

/** A LiveKit recording / AI disclosure channel. Text vs voice can carry different disclosures. */
export type ConsentChannel = 'text' | 'voice';

// --- POST /api/consent/start --------------------------------------------------------------------

export interface ConsentStartRequest {
  jurisdiction: string; // e.g. "US-CA" — drives all-party vs one-party recording-consent wording
  channel: ConsentChannel;
}

export interface ConsentStartResponse {
  session_id: string;
  /** AI-disclosure + recording-notice text to display verbatim before any consent is collected. */
  disclosure_text: string;
}

// --- POST /api/consent/respond ------------------------------------------------------------------

export interface ConsentRespondRequest {
  session_id: string;
  ai_acknowledged: boolean;
  recording_consent: boolean;
  is_minor: boolean;
  /** Only meaningful on the parental re-submit (state was need_parental). */
  parental_consent?: boolean;
}

export interface ConsentRespondResponse {
  session_id: string;
  state: ConsentState;
  /** Optional message the backend may attach (e.g. the end-screen copy for `ended`). */
  message?: string;
}

// --- POST /api/chat -----------------------------------------------------------------------------

export interface ChatRequest {
  session_id: string;
  text: string;
}

export interface ChatResponse {
  reply: string;
  /** Internal policy act label (debug-only). MUST NOT render in the prospect-facing surface. */
  decision_act?: string;
  /** True when the agent reached a terminal act (enrollment close / disqualify / escalate). */
  done?: boolean;
}

// --- POST /api/session/{id}/end -----------------------------------------------------------------

export interface SessionEndRequest {
  session_id: string;
  /** Optional raw phone to remember the caller by — HASHED server-side, never stored raw. */
  raw_phone?: string;
}

export interface SessionEndResponse {
  episode_id: string;
  outcome: string;
  escalations: number;
  persisted: boolean;
}

// --- POST /api/demo/auto/start ------------------------------------------------------------------

/** Response from POST /api/demo/auto/start — kicks off a SERVER-SIDE demo call that streams turns
 *  into the live monitor over ~1-2 min, independent of the browser. 503 if the LLM key is missing
 *  or demo capacity is reached (the 503 `detail` surfaces via ApiError.message in lib/api.ts). */
export interface AutoDemoResponse {
  episode_id: string;
  started: boolean;
}

// --- POST /api/livekit/token --------------------------------------------------------------------

export interface LiveKitTokenRequest {
  session_id: string;
  identity: string;
}

export interface LiveKitTokenResponse {
  token: string;
  /** LiveKit server websocket URL (wss://...) the client connects to. */
  url: string;
  room: string;
}

// Typed fetch client for the FastAPI backend. One thin wrapper per endpoint (consent start/respond,
// chat, LiveKit token) that returns the typed contract from lib/api-types. Surfaces HTTP status via
// a typed ApiError so callers can branch on the meaningful codes the contract defines — 409 (chat:
// "consent required") and 503 (token: "voice unavailable / LiveKit creds absent") — without parsing
// strings. Base URL comes from NEXT_PUBLIC_API_BASE (browser-safe); never embeds secrets.
import type {
  AutoDemoResponse,
  ChatRequest,
  ChatResponse,
  ConsentRespondRequest,
  ConsentRespondResponse,
  ConsentStartRequest,
  ConsentStartResponse,
  LiveKitTokenRequest,
  LiveKitTokenResponse,
  SessionEndRequest,
  SessionEndResponse,
} from './api-types';

const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000').replace(/\/+$/, '');

/** Error carrying the HTTP status so callers can branch on 409 / 503 etc. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly body?: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function postJson<TReq, TRes>(path: string, body: TReq): Promise<TRes> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (cause) {
    // Network failure (backend down / CORS / DNS) — status 0 signals "could not reach backend".
    throw new ApiError(0, 'Could not reach the backend. Is the API running?', cause);
  }

  // Read the body once; tolerate empty / non-JSON responses (e.g. a bare 503).
  const raw = await res.text();
  let parsed: unknown = undefined;
  if (raw) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = raw;
    }
  }

  if (!res.ok) {
    const detail =
      (parsed && typeof parsed === 'object' && 'detail' in parsed
        ? String((parsed as { detail: unknown }).detail)
        : undefined) ?? `Request to ${path} failed (${res.status})`;
    throw new ApiError(res.status, detail, parsed);
  }

  return parsed as TRes;
}

export function consentStart(req: ConsentStartRequest): Promise<ConsentStartResponse> {
  return postJson<ConsentStartRequest, ConsentStartResponse>('/api/consent/start', req);
}

export function consentRespond(req: ConsentRespondRequest): Promise<ConsentRespondResponse> {
  return postJson<ConsentRespondRequest, ConsentRespondResponse>('/api/consent/respond', req);
}

export function sendChat(req: ChatRequest): Promise<ChatResponse> {
  return postJson<ChatRequest, ChatResponse>('/api/chat', req);
}

export function livekitToken(req: LiveKitTokenRequest): Promise<LiveKitTokenResponse> {
  return postJson<LiveKitTokenRequest, LiveKitTokenResponse>('/api/livekit/token', req);
}

export function sessionEnd(req: SessionEndRequest): Promise<SessionEndResponse> {
  return postJson<SessionEndRequest, SessionEndResponse>(`/api/session/${encodeURIComponent(req.session_id)}/end`, req);
}

/** Kick off a SERVER-SIDE demo call. The backend streams turns into the live monitor (~1-2 min) and
 *  the existing /api/live/active queue poll surfaces + auto-selects it — no body needed. Throws an
 *  ApiError(503) (LLM key missing / demo capacity reached) whose `.message` is the human-readable
 *  reason the caller can show. */
export function startAutoDemo(): Promise<AutoDemoResponse> {
  return postJson<Record<string, never>, AutoDemoResponse>('/api/demo/auto/start', {});
}

export { API_BASE };

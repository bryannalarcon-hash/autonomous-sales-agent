// replay-api.ts — Typed fetch client for the sim-to-real fidelity replay endpoints.
// Wraps POST /api/replay/build (trigger a batch replay job) and GET /api/replay/results
// (poll status + per-call fidelity scores). Reuses ApiError + API_BASE from lib/api.ts;
// mirrors the frozen contract from the backend replay router exactly.
import { ApiError, API_BASE } from './api';

// --- Types ---------------------------------------------------------------------------------------

/** One per-call replay result. persona_label and real_outcome arrive humanized from the backend. */
export interface ReplayResult {
  episode_id: string;
  real_outcome: string;       // humanized by backend — never re-map in the UI
  mean_divergence: number;    // 0..1; 0 = perfect mimic, 1 = totally different
  outcome_match_rate: number; // 0..1 fraction of replays that reproduced the real outcome
  n: number;                  // number of replay runs for this episode
  persona_label: string;      // humanized by backend
}

/** Aggregate fidelity statistics over all replayed calls. null = no results yet. */
export interface ReplayAggregate {
  mean_divergence: number | null;
  mean_outcome_match: number | null;
  calls: number;
}

/** Full response from GET /api/replay/results. */
export interface ReplayResultsResponse {
  status: 'idle' | 'running' | 'done';
  calls_total: number;
  calls_done: number;
  results: ReplayResult[];
  aggregate: ReplayAggregate;
}

/** Response from POST /api/replay/build (202 on success). */
export interface BuildReplayResponse {
  status: 'started' | 'empty';
  calls: number;
}

// --- Internal helpers ---------------------------------------------------------------------------

async function parse<T>(res: Response, path: string): Promise<T> {
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
      parsed && typeof parsed === 'object' && 'detail' in parsed
        ? String((parsed as { detail: unknown }).detail)
        : `Request to ${path} failed (${res.status})`;
    throw new ApiError(res.status, detail, parsed);
  }
  return parsed as T;
}

// --- Public API ---------------------------------------------------------------------------------

/**
 * Trigger a batch replay experiment across all eligible calls.
 * POST /api/replay/build
 * Returns 202 {status:"started", calls:N} on success.
 * Throws ApiError(409) if a job is already running — callers should catch that code specially.
 * Throws ApiError(0) if the backend is unreachable.
 */
export async function buildReplay(opts?: {
  n?: number;
  max_calls?: number;
}): Promise<BuildReplayResponse> {
  const body: Record<string, number> = {};
  if (opts?.n !== undefined) body.n = opts.n;
  if (opts?.max_calls !== undefined) body.max_calls = opts.max_calls;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/replay/build`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError(0, 'Could not reach the backend. Is the API running?', cause);
  }
  return parse<BuildReplayResponse>(res, '/api/replay/build');
}

/**
 * Poll replay job status + per-call fidelity results.
 * GET /api/replay/results
 * Returns the full ReplayResultsResponse including aggregate scores and per-call table.
 */
export async function fetchReplayResults(): Promise<ReplayResultsResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/api/replay/results`, {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    });
  } catch (cause) {
    throw new ApiError(0, 'Could not reach the backend. Is the API running?', cause);
  }
  return parse<ReplayResultsResponse>(res, '/api/replay/results');
}

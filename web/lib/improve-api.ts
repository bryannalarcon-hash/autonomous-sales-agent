// Typed fetch client for the Improve dashboard (U16). One thin wrapper per Improve endpoint
// (experiments list, approvals list + approve/reject, KB/playbook read + draft-save, versions list +
// rollback) returning the typed contract from lib/improve-types. Reuses the same ApiError + API_BASE
// convention as lib/api.ts (never embeds secrets); GETs are no-store, mutations POST JSON. All
// SEMANTIC labels arrive pre-translated from the backend, so nothing here re-derives a human label
// from an internal index.
import { ApiError, API_BASE } from './api';
import type {
  ApprovalListResponse,
  DecisionResponse,
  DraftResponse,
  ExperimentListResponse,
  KbResponse,
  PlaybookResponse,
  RollbackResponse,
  RunExperimentRequest,
  RunExperimentResponse,
  VersionListResponse,
} from './improve-types';

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

async function getJson<T>(path: string, params?: Record<string, string | undefined>): Promise<T> {
  const qs = new URLSearchParams();
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== '') qs.set(k, v);
    }
  }
  const url = `${API_BASE}${path}${qs.toString() ? `?${qs.toString()}` : ''}`;
  let res: Response;
  try {
    res = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' });
  } catch (cause) {
    throw new ApiError(0, 'Could not reach the backend. Is the API running?', cause);
  }
  return parse<T>(res, path);
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError(0, 'Could not reach the backend. Is the API running?', cause);
  }
  return parse<T>(res, path);
}

// --- P6 Experiment Lab ---------------------------------------------------------------------------
export function fetchExperiments(params?: { state?: string }): Promise<ExperimentListResponse> {
  return getJson<ExperimentListResponse>('/api/experiments', params);
}

/** RUN a champion-vs-value A/B and persist the result. COST: in prod this spends real model credit
 *  (n calls per arm) — the caller surfaces an explicit "this runs N real model calls" note. */
export function runExperiment(req: RunExperimentRequest): Promise<RunExperimentResponse> {
  return postJson<RunExperimentResponse>('/api/experiments/run', req);
}

// --- P7 Approval Queue ---------------------------------------------------------------------------
export function fetchApprovals(): Promise<ApprovalListResponse> {
  return getJson<ApprovalListResponse>('/api/approvals');
}

export function approveExperiment(id: string): Promise<DecisionResponse> {
  return postJson<DecisionResponse>(`/api/approvals/${encodeURIComponent(id)}/approve`);
}

export function rejectExperiment(id: string): Promise<DecisionResponse> {
  return postJson<DecisionResponse>(`/api/approvals/${encodeURIComponent(id)}/reject`);
}

// --- P8 KB / Playbook Editor ---------------------------------------------------------------------
export function fetchKb(): Promise<KbResponse> {
  return getJson<KbResponse>('/api/kb');
}

export function fetchPlaybook(): Promise<PlaybookResponse> {
  return getJson<PlaybookResponse>('/api/playbook');
}

/** SAVE a KB edit -> a DRAFT challenger (R20: never a live champion mutation). */
export function saveKbDraft(req: { name: string; body?: Record<string, unknown> }): Promise<DraftResponse> {
  return postJson<DraftResponse>('/api/kb', req);
}

/** SAVE a playbook edit -> a DRAFT challenger (R20: never a live champion mutation). */
export function savePlaybookDraft(req: {
  name: string;
  body?: Record<string, unknown>;
}): Promise<DraftResponse> {
  return postJson<DraftResponse>('/api/playbook', req);
}

// --- P9 Version History & Rollback ---------------------------------------------------------------
export function fetchVersions(): Promise<VersionListResponse> {
  return getJson<VersionListResponse>('/api/versions');
}

export function rollbackVersion(version: string): Promise<RollbackResponse> {
  return postJson<RollbackResponse>(`/api/versions/${encodeURIComponent(version)}/rollback`);
}

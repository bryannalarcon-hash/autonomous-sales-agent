// Typed fetch client for the Improve dashboard (U16). One thin wrapper per Improve endpoint
// (experiments list, run an A/B [ASYNC — 202 + a running record, CB-15; multi-metric via `changes`,
// CB-27], scaffold a draft from a reviewed call [CB-19], one experiment's detail + A/B mock calls
// [CB-25], approvals list + approve/reject, KB/playbook read + draft-save, versions list
// + rollback) returning the typed contract from lib/improve-types. Reuses the same ApiError + API_BASE
// convention as lib/api.ts (never embeds secrets); GETs are no-store, mutations POST JSON. All
// SEMANTIC labels arrive pre-translated from the backend, so nothing here re-derives a human label
// from an internal index.
import { ApiError, API_BASE } from './api';
import type {
  ApprovalListResponse,
  DecisionResponse,
  DraftResponse,
  ExperimentDetailResponse,
  ExperimentListResponse,
  KbResponse,
  PlaybookResponse,
  RollbackResponse,
  RunExperimentRequest,
  RunExperimentResponse,
  ScaffoldExperimentRequest,
  ScaffoldExperimentResponse,
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

/** KICK OFF a champion-vs-value A/B ASYNCHRONOUSLY (CB-15). The endpoint persists a `running` record
 *  and replies 202 Accepted IMMEDIATELY — it does NOT wait for the (minutes-long, paid) run. The
 *  caller shows the returned `running` card at once; the /api/experiments poll settles it to its
 *  terminal state. COST: in prod the background run spends real model credit (n calls per arm). */
export function runExperiment(req: RunExperimentRequest): Promise<RunExperimentResponse> {
  return postJson<RunExperimentResponse>('/api/experiments/run', req);
}

/** SCAFFOLD a draft experiment seeded from a reviewed call (CB-19) — launches nothing. Returns the
 *  persisted `draft` + the originating episode so the lab's per-experiment review view can open it
 *  pre-populated for the operator to confirm before running. */
export function scaffoldExperiment(
  req: ScaffoldExperimentRequest,
): Promise<ScaffoldExperimentResponse> {
  return postJson<ScaffoldExperimentResponse>('/api/experiments/scaffold', req);
}

/** FETCH one experiment's detail + its A/B mock calls (CB-25): the experiment DTO plus the champion-arm
 *  + challenger-arm self-play episodes the run produced, each openable in Review. Powers the
 *  /improve/lab/[id] detail page reached via the drawer's "See experiment". */
export function fetchExperimentDetail(id: string): Promise<ExperimentDetailResponse> {
  return getJson<ExperimentDetailResponse>(`/api/experiments/${encodeURIComponent(id)}`);
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

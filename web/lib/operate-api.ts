// Typed fetch client + small display helpers for the Operate dashboard (U15). One thin wrapper per
// read endpoint (episodes list/detail, KPIs, escalations, live snapshot) returning the typed
// contract from lib/operate-types. Reuses the same ApiError + NEXT_PUBLIC_API_BASE convention as
// lib/api.ts (never embeds secrets). The display helpers (duration / time-ago / initials) format
// raw numeric backend fields for the glass UI; all SEMANTIC labels come pre-translated from the
// backend, so nothing here re-derives a human label from an internal index. The lone WRITE here is
// updateEscalationLifecycle (POST /api/escalations/{id}/lifecycle) — the escalation triage action.
import { ApiError, API_BASE } from './api';
import type {
  Escalation,
  EpisodeDetail,
  EpisodeListResponse,
  EscalationListResponse,
  KpiResponse,
  LiveSnapshot,
} from './operate-types';

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

// POST a JSON body and parse the JSON response, mirroring getJson's ApiError + detail handling so a
// 400/404 surfaces the backend's `detail` message verbatim (the only mutating call in this module).
async function postJson<T>(path: string, body: unknown): Promise<T> {
  const url = `${API_BASE}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      cache: 'no-store',
    });
  } catch (cause) {
    throw new ApiError(0, 'Could not reach the backend. Is the API running?', cause);
  }
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

export function fetchEpisodes(params?: {
  version?: string;
  cohort?: string;
  outcome?: string;
  escalated?: string;
  limit?: string;
}): Promise<EpisodeListResponse> {
  return getJson<EpisodeListResponse>('/api/episodes', params);
}

export function fetchEpisode(episodeId: string): Promise<EpisodeDetail> {
  return getJson<EpisodeDetail>(`/api/episodes/${encodeURIComponent(episodeId)}`);
}

export function fetchKpis(params?: {
  version?: string;
  cohort?: string;
  compare_version?: string;
}): Promise<KpiResponse> {
  return getJson<KpiResponse>('/api/kpis', params);
}

export function fetchEscalations(params?: {
  lifecycle?: string;
  limit?: string;
}): Promise<EscalationListResponse> {
  return getJson<EscalationListResponse>('/api/escalations', params);
}

/** Triage action: move an escalation to a new lifecycle state. POSTs to
 *  /api/escalations/{id}/lifecycle and returns the backend's confirmed {escalation_id, lifecycle}.
 *  Throws ApiError on a 400 (invalid lifecycle) / 404 (unknown id) so the caller can show it. */
export function updateEscalationLifecycle(
  id: string,
  lifecycle: Escalation['lifecycle'],
): Promise<{ escalation_id: string; lifecycle: Escalation['lifecycle'] }> {
  return postJson<{ escalation_id: string; lifecycle: Escalation['lifecycle'] }>(
    `/api/escalations/${encodeURIComponent(id)}/lifecycle`,
    { lifecycle },
  );
}

export function fetchLive(): Promise<LiveSnapshot> {
  return getJson<LiveSnapshot>('/api/live');
}

// --- display helpers (formatting only — no semantic-label derivation) ---------------------------

/** ms -> "m:ss" (e.g. 348000 -> "5:48"). Null/0 -> "—". */
export function fmtDuration(ms: number | null | undefined): string {
  if (!ms || ms <= 0) return '—';
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

/** ISO timestamp -> a coarse "x min ago" relative string for the dense tables. */
export function fmtTimeAgo(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '—';
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return 'Just now';
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  const days = Math.round(hrs / 24);
  return days === 1 ? 'Yesterday' : `${days} days ago`;
}

/** Person/company name -> up-to-2-char initials for an avatar tile. */
export function initials(name: string | null | undefined): string {
  if (!name) return '—';
  const parts = name.trim().split(/\s+/).slice(0, 2);
  return parts.map((p) => p[0]?.toUpperCase() ?? '').join('') || '—';
}

/** A stable, friendly display name derived from an episode id (the backend has no caller name in
 *  this build — calls are keyed by id/phone-hash, never a stored name). Shows the call id itself as
 *  the heading and uses its trailing digits for a deterministic avatar, so nothing is fabricated. */
export function callDisplay(episodeId: string): { title: string; avatar: string } {
  return { title: episodeId, avatar: episodeId.replace(/[^0-9]/g, '').slice(-2) || 'CA' };
}

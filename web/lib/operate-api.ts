// Typed fetch client + small display helpers for the Operate dashboard (U15/U16). One thin wrapper
// per read endpoint (episodes list/detail, KPIs, escalations, live snapshot, active-calls queue,
// sample call) returning the typed contract from lib/operate-types. Reuses the same ApiError +
// NEXT_PUBLIC_API_BASE convention as lib/api.ts (never embeds secrets). Display helpers (duration /
// time-ago / initials) format raw numeric backend fields for the glass UI; all SEMANTIC labels come
// pre-translated from the backend, so nothing here re-derives a human label from an internal index.
// WRITES: updateEscalationLifecycle (POST /api/escalations/{id}/lifecycle) and CB-30's
// setEpisodeGolden (POST /api/episodes/{id}/golden — tag/untag the golden calibration set). U16 adds:
// fetchActiveCalls (/api/live/active), fetchLive now accepts an optional episodeId param, and
// fetchSampleCall (/api/live/sample) for the "Show sample call" toggle in the empty state. CB-30 also
// adds fetchGoldenEpisodes (GET /api/episodes/golden) to enumerate/export the golden set.
// CB-45 display helpers: fmtMsShort (raw ms -> "0.8s"/"120ms"), fmtFirstToken ("0.8s to first word")
// and fmtSpoke ("spoke for 1.4s") format the nullable CB-44 timing numbers into human phrases — they
// never render a raw ms key and return "" on null so callers can omit the chip entirely.
import { ApiError, API_BASE } from './api';
import type {
  ActiveCallsResponse,
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

/** Fetch the live snapshot. When episodeId is given, fetches that specific call via
 *  /api/live?episode_id=<id> (for the queue "watch this call" action); otherwise fetches the
 *  newest active call from /api/live. */
export function fetchLive(episodeId?: string): Promise<LiveSnapshot> {
  return getJson<LiveSnapshot>('/api/live', episodeId ? { episode_id: episodeId } : undefined);
}

/** Fetch the queue of all currently active (in-progress) calls, newest-first.
 *  Returns ActiveCallsResponse with count + calls[]. An empty calls array means no calls active. */
export function fetchActiveCalls(): Promise<ActiveCallsResponse> {
  return getJson<ActiveCallsResponse>('/api/live/active');
}

/** Fetch a sample call snapshot for the "Show sample call" preview toggle.
 *  The returned LiveSnapshot has sample:true and active:false — the monitor renders it with a
 *  SAMPLE badge and no live/recording affordances. */
export function fetchSampleCall(): Promise<LiveSnapshot> {
  return getJson<LiveSnapshot>('/api/live/sample');
}

/** Response from GET /api/episodes/golden — the golden calibration set as full Call Review payloads
 *  (each is the same EpisodeDetail shape Call Review renders / Export downloads), newest-first. */
export interface GoldenSetResponse {
  episodes: EpisodeDetail[];
  count: number;
}

/** CB-30: fetch the golden calibration set (transcript + belief trajectory + outcome per episode),
 *  newest-first. The set is exportable as-is and replayable by the twin harness — each entry is the
 *  full episode record, so callers can enumerate or download the whole set. */
export function fetchGoldenEpisodes(limit?: number): Promise<GoldenSetResponse> {
  return getJson<GoldenSetResponse>('/api/episodes/golden', limit ? { limit: String(limit) } : undefined);
}

/** CB-30 WRITE: tag/untag a real call as golden (persists in metrics['golden']). POSTs to
 *  /api/episodes/{id}/golden and returns the backend's confirmed {episode_id, golden}. Throws
 *  ApiError on a 404 (unknown id) so the caller can surface it. */
export function setEpisodeGolden(
  id: string,
  golden: boolean,
): Promise<{ episode_id: string; golden: boolean }> {
  return postJson<{ episode_id: string; golden: boolean }>(
    `/api/episodes/${encodeURIComponent(id)}/golden`,
    { golden },
  );
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

/** CB-44/CB-45 timing -> a compact human duration. Sub-second reads as ms ("420ms"); ≥1s reads as
 *  seconds with one decimal ("0.8s" is reserved for <1s already covered, so this is "1.4s"/"12s").
 *  Null/0/negative -> "" so a caller can OMIT the chip rather than print a placeholder. Never shows
 *  a raw ms integer with no unit, and never an internal key. */
export function fmtMsShort(ms: number | null | undefined): string {
  if (ms == null || ms <= 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const secs = ms / 1000;
  // ≥10s: no decimal (already coarse); otherwise one decimal so "0.8s"/"1.4s" read naturally.
  return secs >= 10 ? `${Math.round(secs)}s` : `${secs.toFixed(1)}s`;
}

/** Time-to-first-token as a sentence-fragment label, e.g. "0.8s to first word". Null -> "". The
 *  "first word" wording keeps it human (this is when the agent started speaking), not "first token". */
export function fmtFirstToken(ms: number | null | undefined): string {
  const t = fmtMsShort(ms);
  return t ? `${t} to first word` : '';
}

/** Stream duration as a human phrase, e.g. "spoke for 1.4s" (how long the agent's reply streamed).
 *  Null -> "". Used for both per-turn stream_duration_ms and a call's avg_stream_ms summary. */
export function fmtSpoke(ms: number | null | undefined): string {
  const t = fmtMsShort(ms);
  return t ? `spoke for ${t}` : '';
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

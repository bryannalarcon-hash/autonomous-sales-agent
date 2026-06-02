// Fidelity page — "Build replay experiments from all calls" operator panel (Improve tab).
// Shows a trigger button, live progress while a job runs (polls GET /api/replay/results every ~3s),
// an aggregate header card (mean divergence + outcome-match + call count), and a per-call table
// sorted best-first (lowest divergence). divergence_score 0..1: lower = closer to real call.
// Handles: idle (no results), running (progress bar), done (table), empty (no eligible calls),
// 409 conflict (job already running), and backend-unreachable (graceful error, no crash).
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { Icon } from '@/components/cadence/Icon';
import { buildReplay, fetchReplayResults } from '@/lib/replay-api';
import type { ReplayResult, ReplayResultsResponse } from '@/lib/replay-api';
import { ApiError } from '@/lib/api';

// --- helpers ------------------------------------------------------------------------------------

/** Format a 0..1 fraction as a percentage string, e.g. 0.732 → "73%" */
function pct(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return `${Math.round(v * 100)}%`;
}

/** Format a 0..1 divergence score to 2 decimal places, e.g. 0.312 → "0.31" */
function div2(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return v.toFixed(2);
}

/** Color token for a divergence value: low (≤0.3) is good/green, high (≥0.7) is bad/warn, mid is neutral. */
function divergenceColor(v: number): string {
  if (v <= 0.3) return 'var(--ok)';
  if (v >= 0.7) return 'var(--warn)';
  return 'var(--text-2)';
}

// --- sub-components -----------------------------------------------------------------------------

function AggregateCard({ agg, calls_total }: { agg: ReplayResultsResponse['aggregate']; calls_total: number }) {
  return (
    <div
      className="card solid card-pad"
      style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 0 }}
    >
      <div style={{ padding: '14px 18px', borderRight: '1px solid var(--border)' }}>
        <div
          className="faint"
          style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 }}
        >
          Avg divergence
        </div>
        <div
          className="mono"
          style={{ fontSize: 26, fontWeight: 700, color: agg.mean_divergence !== null ? divergenceColor(agg.mean_divergence) : 'var(--text-3)' }}
        >
          {div2(agg.mean_divergence)}
        </div>
        <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
          lower = closer to the real calls
        </div>
      </div>

      <div style={{ padding: '14px 18px', borderRight: '1px solid var(--border)' }}>
        <div
          className="faint"
          style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 }}
        >
          Outcome match rate
        </div>
        <div
          className="mono"
          style={{ fontSize: 26, fontWeight: 700, color: agg.mean_outcome_match !== null && agg.mean_outcome_match >= 0.7 ? 'var(--ok)' : 'var(--text)' }}
        >
          {pct(agg.mean_outcome_match)}
        </div>
        <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
          of replays reproduced the real outcome
        </div>
      </div>

      <div style={{ padding: '14px 18px' }}>
        <div
          className="faint"
          style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 }}
        >
          Calls replayed
        </div>
        <div className="mono" style={{ fontSize: 26, fontWeight: 700 }}>
          {agg.calls}
        </div>
        <div className="muted" style={{ fontSize: 11.5, marginTop: 4 }}>
          of {calls_total} eligible calls
        </div>
      </div>
    </div>
  );
}

function DivergenceBar({ value }: { value: number }) {
  const color = divergenceColor(value);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <div className="bar" style={{ flex: 1, height: 6 }}>
        <i style={{ width: `${Math.min(100, value * 100)}%`, background: color }} />
      </div>
      <span className="mono" style={{ fontSize: 12, width: 34, textAlign: 'right', color }}>
        {div2(value)}
      </span>
    </div>
  );
}

function ResultsTable({ results }: { results: ReplayResult[] }) {
  if (results.length === 0) return null;
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr 2fr 1fr',
          borderBottom: '1px solid var(--border)',
          padding: '9px 18px',
        }}
      >
        {(['Persona', 'Outcome', 'Divergence (lower = better)', 'Outcome match'] as const).map((h) => (
          <div
            key={h}
            className="faint"
            style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em' }}
          >
            {h}
          </div>
        ))}
      </div>

      {results.map((r, i) => (
        <div
          key={r.episode_id}
          style={{
            display: 'grid',
            gridTemplateColumns: '1fr 1fr 2fr 1fr',
            padding: '12px 18px',
            borderBottom: i < results.length - 1 ? '1px solid var(--border-soft)' : 'none',
            alignItems: 'center',
          }}
        >
          <div style={{ fontSize: 13, fontWeight: 600 }}>{r.persona_label}</div>
          <div className="muted" style={{ fontSize: 12.5 }}>{r.real_outcome}</div>
          <DivergenceBar value={r.mean_divergence} />
          <div
            className="mono"
            style={{
              fontSize: 13,
              fontWeight: 650,
              color: r.outcome_match_rate >= 0.7 ? 'var(--ok)' : r.outcome_match_rate <= 0.3 ? 'var(--warn)' : 'var(--text-2)',
            }}
          >
            {pct(r.outcome_match_rate)}
          </div>
        </div>
      ))}
    </div>
  );
}

// --- main page ----------------------------------------------------------------------------------

export default function FidelityPage() {
  const [data, setData] = useState<ReplayResultsResponse | null>(null);
  const [triggering, setTriggering] = useState(false);
  const [notice, setNotice] = useState<{ kind: 'error' | 'info'; msg: string } | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Fetch current results (used both on mount and during polling).
  const load = useCallback(async () => {
    try {
      const res = await fetchReplayResults();
      setData(res);
      // Stop polling once the job finishes.
      if (res.status !== 'running' && pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    } catch (e) {
      // Backend not mounted yet — set a graceful empty state, don't crash.
      if (e instanceof ApiError && (e.status === 0 || e.status === 404)) {
        setData(null); // treat as no results
      }
      // Other errors (500, etc.) are silently swallowed on polling; initial load surfaced below.
    }
  }, []);

  // Start polling every 3s.
  const startPolling = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = setInterval(() => { void load(); }, 3000);
  }, [load]);

  // Initial load — surface error in notice only if we have nothing to show.
  useEffect(() => {
    void load();
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [load]);

  // If data arrives showing a running job, make sure we're polling.
  useEffect(() => {
    if (data?.status === 'running') startPolling();
  }, [data?.status, startPolling]);

  async function handleBuild() {
    setTriggering(true);
    setNotice(null);
    try {
      const res = await buildReplay();
      if (res.status === 'empty') {
        setNotice({ kind: 'info', msg: 'No eligible calls found to replay. Run some real calls first.' });
      } else {
        // Job started — poll for updates.
        void load();
        startPolling();
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setNotice({ kind: 'info', msg: 'A replay job is already running. Progress is shown below.' });
        void load();
        startPolling();
      } else if (e instanceof ApiError && e.status === 0) {
        setNotice({ kind: 'error', msg: 'Cannot reach the backend. Start the API server and try again.' });
      } else {
        setNotice({ kind: 'error', msg: e instanceof Error ? e.message : 'Failed to start replay job.' });
      }
    } finally {
      setTriggering(false);
    }
  }

  const isRunning = data?.status === 'running';
  const isDone = data?.status === 'done';
  const hasResults = (data?.results?.length ?? 0) > 0;

  return (
    <div className="page">
      <div className="page-scroll scroll">
        <div className="pad" style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>

          {/* explainer */}
          <div
            className="row"
            style={{
              gap: 10,
              alignItems: 'flex-start',
              padding: '12px 16px',
              borderRadius: 12,
              background: 'var(--accent-soft)',
              border: '1px solid var(--accent-border)',
            }}
          >
            <Icon name="layers" size={16} style={{ color: 'var(--accent-strong)', flexShrink: 0, marginTop: 1 }} />
            <p style={{ fontSize: 13, color: 'var(--accent-strong)', margin: 0, lineHeight: 1.55 }}>
              Each real call is replayed against an LLM &ldquo;digital twin&rdquo; of the caller; divergence 0 = the replay behaved identically, 1 = totally different.
            </p>
          </div>

          {/* trigger card */}
          <div className="card card-pad" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div className="row" style={{ gap: 10, alignItems: 'flex-start' }}>
              <div style={{ flex: 1 }}>
                <div className="b" style={{ fontSize: 15, fontFamily: 'var(--font-display)', marginBottom: 5 }}>
                  Build replay experiments from all calls
                </div>
                <div className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>
                  Re-runs the agent against a digital-twin persona for every stored call and measures how closely the replay matches the real conversation.
                </div>
                <div
                  className="row"
                  style={{
                    gap: 7,
                    marginTop: 10,
                    padding: '8px 12px',
                    borderRadius: 9,
                    background: 'var(--warn-soft)',
                    border: '1px solid var(--warn-border)',
                    display: 'inline-flex',
                  }}
                >
                  <Icon name="bolt" size={14} style={{ color: 'var(--warn)', flexShrink: 0 }} />
                  <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--warn)' }}>
                    Running costs real LLM calls — one replay run per call in your dataset.
                  </span>
                </div>
              </div>

              <button
                className="btn btn-primary"
                onClick={handleBuild}
                disabled={triggering || isRunning}
                style={{ flexShrink: 0, minWidth: 220 }}
              >
                {isRunning ? (
                  <>
                    <Icon name="pivot" size={16} />
                    Running… {data.calls_done} / {data.calls_total}
                  </>
                ) : triggering ? (
                  <>
                    <Icon name="pivot" size={16} />
                    Starting…
                  </>
                ) : (
                  <>
                    <Icon name="play" size={16} />
                    Build replay experiments
                  </>
                )}
              </button>
            </div>

            {/* progress bar — visible while running */}
            {isRunning && data.calls_total > 0 ? (
              <div>
                <div className="row" style={{ justifyContent: 'space-between', marginBottom: 5 }}>
                  <span className="faint" style={{ fontSize: 11 }}>Progress</span>
                  <span className="mono" style={{ fontSize: 11.5 }}>
                    {data.calls_done} / {data.calls_total} calls
                  </span>
                </div>
                <div className="bar" style={{ height: 6 }}>
                  <i
                    style={{
                      width: `${data.calls_total > 0 ? Math.min(100, (data.calls_done / data.calls_total) * 100) : 0}%`,
                      background: 'var(--accent)',
                    }}
                  />
                </div>
              </div>
            ) : null}
          </div>

          {/* notice strip (info or error) */}
          {notice ? (
            <div
              className="row"
              style={{
                gap: 9,
                alignItems: 'center',
                padding: '10px 14px',
                borderRadius: 10,
                background: notice.kind === 'error' ? 'var(--danger-soft)' : 'var(--info-soft)',
                border: `1px solid ${notice.kind === 'error' ? 'var(--danger-border)' : 'var(--info-border)'}`,
              }}
            >
              <Icon
                name={notice.kind === 'error' ? 'alert' : 'note'}
                size={15}
                style={{ color: notice.kind === 'error' ? 'var(--danger)' : 'var(--info)', flexShrink: 0 }}
              />
              <span style={{ fontSize: 13, color: notice.kind === 'error' ? 'var(--danger)' : 'var(--info)', fontWeight: 550 }}>
                {notice.msg}
              </span>
            </div>
          ) : null}

          {/* aggregate header — shown once we have data */}
          {data && hasResults ? (
            <AggregateCard agg={data.aggregate} calls_total={data.calls_total} />
          ) : null}

          {/* results table — sorted best-first, as backend returns */}
          {isDone && hasResults ? (
            <div>
              <div className="sec-head" style={{ marginBottom: 12 }}>
                <h2>Per-call fidelity</h2>
                <span className="sub">{data.results.length} calls · sorted by lowest divergence</span>
              </div>
              <ResultsTable results={data.results} />
            </div>
          ) : null}

          {/* partial results while running */}
          {isRunning && hasResults ? (
            <div>
              <div className="sec-head" style={{ marginBottom: 12 }}>
                <h2>Results so far</h2>
                <span className="sub">Updating every few seconds…</span>
              </div>
              <ResultsTable results={data.results} />
            </div>
          ) : null}

          {/* empty state — job done but no results, or no results and never run */}
          {!isRunning && !hasResults && !triggering ? (
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="layers" size={28} />
                </div>
                <h3>No fidelity results yet</h3>
                <p>
                  Click &ldquo;Build replay experiments&rdquo; to replay all real calls against their
                  digital-twin personas and measure sim-to-real fidelity.
                </p>
              </div>
            </div>
          ) : null}

        </div>
      </div>
    </div>
  );
}

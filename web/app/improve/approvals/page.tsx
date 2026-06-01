// P7 — Approval Queue (U16). The human sign-off surface for EXTREME challengers (pricing-concession /
// persona) that improve outcomes but breach a guardrail — promotion is PAUSED until the operator
// decides (R19/AE6). Lists experiments in `blocked` state from /api/approvals. Each card shows WHY
// it's extreme (the guardrail reason) + the enroll/ladder lift, with Reject (danger) / Approve with
// override (ok) actions wired to POST /api/approvals/{id}/{approve,reject}: approve PROMOTES the
// challenger to champion, reject leaves the champion unchanged. A decided card leaves the queue and
// appends a logged-decision line. A drawer surfaces the full "why it needs sign-off" detail.
'use client';

import { useEffect, useState } from 'react';
import { Icon } from '@/components/cadence/Icon';
import { approveExperiment, fetchApprovals, rejectExperiment } from '@/lib/improve-api';
import type { Experiment } from '@/lib/improve-types';

function Box({ l, v, good, warn, last }: { l: string; v: string; good?: boolean; warn?: boolean; last?: boolean }) {
  const color = good ? 'var(--ok)' : warn ? 'var(--warn-strong)' : 'var(--text)';
  return (
    <div style={{ padding: '12px 16px', borderRight: last ? 0 : '1px solid var(--border)' }}>
      <div className="faint" style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em' }}>
        {l}
      </div>
      <div
        style={{
          fontSize: warn ? 13 : 17,
          fontWeight: 650,
          marginTop: 3,
          fontFamily: warn ? 'var(--font-mono)' : 'var(--font-display)',
          color,
        }}
      >
        {v}
      </div>
    </div>
  );
}

function AprDrawer({
  a,
  onClose,
  onDecide,
  busy,
}: {
  a: Experiment;
  onClose: () => void;
  onDecide: (id: string, v: 'approved' | 'rejected') => void;
  busy: boolean;
}) {
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="drawer">
        <div className="card-head">
          <div className="grow">
            <div className="row" style={{ gap: 8, marginBottom: 4 }}>
              <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{a.experiment_id}</span>
              <span className="tag warn dot">{a.dimension_label}</span>
            </div>
            <div className="b" style={{ fontSize: 15.5, fontFamily: 'var(--font-display)' }}>{a.name}</div>
          </div>
          <button className="gctl" onClick={onClose} style={{ width: 36, padding: 0, justifyContent: 'center' }}>
            <Icon name="x" size={16} />
          </button>
        </div>
        <div className="scroll" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14, overflow: 'auto' }}>
          <div className="card solid card-pad">
            <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 }}>
              Why it needs sign-off
            </div>
            <div style={{ fontSize: 13.5, lineHeight: 1.55 }}>
              {a.diff_description}. This change is extreme ({a.dimension_label.toLowerCase()}), so promotion is paused for a human.
            </div>
          </div>
          <div className="card card-pad" style={{ borderColor: 'var(--warn-border)', background: 'var(--warn-soft)' }}>
            <div className="row" style={{ gap: 9 }}>
              <Icon name="shield" size={17} style={{ color: 'var(--warn-strong)' }} />
              <div>
                <div className="b" style={{ fontSize: 13, color: 'var(--warn-strong)' }}>
                  Guardrail: {a.guardrail_reason ?? 'breached'}
                </div>
                <div style={{ fontSize: 12, color: 'var(--warn-strong)', opacity: 0.85, marginTop: 2 }}>
                  Approving records an override on your account and ships {a.challenger_version} as champion.
                </div>
              </div>
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
            <div className="card solid card-pad">
              <div className="faint" style={{ fontSize: 11, fontWeight: 600 }}>Enroll lift</div>
              <div style={{ fontSize: 19, fontWeight: 650, color: 'var(--ok)', fontFamily: 'var(--font-display)', marginTop: 3 }}>
                +{Math.round(a.enroll_delta * 100)} pts
              </div>
            </div>
            <div className="card solid card-pad">
              <div className="faint" style={{ fontSize: 11, fontWeight: 600 }}>Ladder lift</div>
              <div style={{ fontSize: 19, fontWeight: 650, color: 'var(--ok)', fontFamily: 'var(--font-display)', marginTop: 3 }}>
                +{a.delta.toFixed(2)}
              </div>
            </div>
          </div>
          <div className="row" style={{ gap: 10, marginTop: 4 }}>
            <button className="btn btn-danger grow" disabled={busy} onClick={() => onDecide(a.experiment_id, 'rejected')}>
              <Icon name="x" size={15} />
              Reject
            </button>
            <button className="btn btn-ok grow" disabled={busy} onClick={() => onDecide(a.experiment_id, 'approved')}>
              <Icon name="check" size={15} />
              Approve with override
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

export default function ApprovalsPage() {
  const [rows, setRows] = useState<Experiment[]>([]);
  const [sel, setSel] = useState<Experiment | null>(null);
  const [decided, setDecided] = useState<Record<string, 'approved' | 'rejected'>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchApprovals()
      .then((res) => {
        if (!cancelled) {
          setRows(res.approvals);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load approvals.');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function decide(id: string, v: 'approved' | 'rejected') {
    setBusy(true);
    try {
      if (v === 'approved') await approveExperiment(id);
      else await rejectExperiment(id);
      setRows((prev) => prev.filter((r) => r.experiment_id !== id));
      setDecided((d) => ({ ...d, [id]: v }));
      setSel(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Decision failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <div className="page-scroll scroll">
        <div className="pad">
          <div className="sec-head">
            <Icon name="badge" size={18} style={{ color: 'var(--warn)' }} />
            <h2>Awaiting your sign-off</h2>
            <span className="sub">
              Changes that lift outcomes but breach a guardrail — promotion is paused until you decide.
            </span>
            <span className="sp" />
          </div>

          {error ? (
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="alert" size={28} />
                </div>
                <h3>Couldn&apos;t load the queue</h3>
                <p>{error}</p>
              </div>
            </div>
          ) : rows.length === 0 ? (
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="check" size={28} />
                </div>
                <h3>Queue clear</h3>
                <p>No changes are waiting on approval. Guardrail-blocked experiments will surface here.</p>
              </div>
            </div>
          ) : (
            <div className="col" style={{ gap: 14 }}>
              {rows.map((a) => (
                <div key={a.experiment_id} className="card">
                  <div className="card-pad">
                    <div className="row" style={{ gap: 9, marginBottom: 9 }}>
                      <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{a.experiment_id}</span>
                      <span className="tag warn dot">{a.dimension_label}</span>
                      <span className="tag">{a.champion_version ?? '—'} · {a.challenger_version}</span>
                    </div>
                    <div className="b" style={{ fontSize: 16, fontFamily: 'var(--font-display)', letterSpacing: '-0.02em' }}>{a.name}</div>
                    <div className="muted" style={{ fontSize: 13, marginTop: 5, lineHeight: 1.5, maxWidth: 760 }}>
                      {a.diff_description}. This is an extreme change ({a.dimension_label.toLowerCase()}) — it needs your sign-off before it can ship.
                    </div>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1.4fr', borderTop: '1px solid var(--border)' }}>
                    <Box l="Enroll lift" v={`+${Math.round(a.enroll_delta * 100)} pts`} good />
                    <Box l="Ladder lift" v={`+${a.delta.toFixed(2)}`} good />
                    <Box l="Guardrail breached" v={a.guardrail_reason ?? 'breached'} warn last />
                  </div>
                  <div style={{ padding: '13px 16px', borderTop: '1px solid var(--border)', display: 'flex', gap: 10 }}>
                    <button className="btn btn-ghost btn-sm" onClick={() => setSel(a)}>
                      <Icon name="eye" size={14} />
                      Review detail
                    </button>
                    <div className="grow" />
                    <button className="btn btn-danger" disabled={busy} onClick={() => decide(a.experiment_id, 'rejected')}>
                      <Icon name="x" size={15} />
                      Reject
                    </button>
                    <button className="btn btn-ok" disabled={busy} onClick={() => decide(a.experiment_id, 'approved')}>
                      <Icon name="check" size={15} />
                      Approve with override
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          {Object.keys(decided).length > 0 ? (
            <div className="row" style={{ gap: 8, marginTop: 16, color: 'var(--text-3)', fontSize: 12.5 }}>
              <Icon name="check" size={14} />
              {Object.entries(decided)
                .map(([k, v]) => `${k} ${v}`)
                .join(' · ')}
              {' — logged to version history'}
            </div>
          ) : null}
        </div>
      </div>
      {sel ? <AprDrawer a={sel} onClose={() => setSel(null)} onDecide={decide} busy={busy} /> : null}
    </div>
  );
}

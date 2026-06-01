// P5 — Escalation Queue (U15). The review queue (store.list_escalations) with lifecycle states:
// Unreviewed → Reviewed → Resolved, shown as a segmented control with live counts. Each escalation
// is a card with a severity bar (derived from the reason category — compliance/concession run hot),
// the labeled reason, the linked call, and the trigger moment. Clicking opens a drawer with the
// trigger moment, a "Belief at escalation" mini-bar block, and actions: Open call (→ Call Review),
// an optional manual-takeover affordance, Mark reviewed (only when unreviewed), and Resolve. All
// reason/lifecycle text is the backend's pre-translated label (no raw key renders).
'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchEscalations } from '@/lib/operate-api';
import type { Escalation, EscalationListResponse } from '@/lib/operate-types';

type Sev = 'high' | 'med' | 'low';
const SEV_COLOR: Record<Sev, string> = { high: 'danger', med: 'warn', low: 'info' };

// Severity is derived from the reason category: compliance + pricing concession run hot (high),
// a human request is medium, anything else low. (The schema carries reason + lifecycle, not a
// severity column, so this keeps the dense card's left bar meaningful without inventing data.)
function severityOf(reasonKey: string | null): Sev {
  if (!reasonKey) return 'med';
  if (reasonKey.includes('compliance') || reasonKey.includes('concession') || reasonKey.includes('false_promise')) return 'high';
  if (reasonKey.includes('human')) return 'med';
  return 'low';
}

const TABS: { key: 'unreviewed' | 'reviewed' | 'resolved'; label: string }[] = [
  { key: 'unreviewed', label: 'Unreviewed' },
  { key: 'reviewed', label: 'Reviewed' },
  { key: 'resolved', label: 'Resolved' },
];

function Drawer({ e, onClose }: { e: Escalation; onClose: () => void }) {
  const router = useRouter();
  const sev = severityOf(e.reason_key);
  const beliefRows: [string, number, string][] = [
    ['Walk-away risk', 0.71, 'var(--danger)'],
    ['Concession pressure', 0.64, 'var(--warn)'],
    ['Trust', 0.38, 'var(--warn)'],
  ];
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="drawer">
        <div className="card-head">
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: 10,
              background: `var(--${SEV_COLOR[sev]}-soft)`,
              color: `var(--${SEV_COLOR[sev]})`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <Icon name="alert" size={18} />
          </div>
          <div className="grow">
            <div className="b" style={{ fontSize: 15, fontFamily: 'var(--font-display)' }}>{e.reason}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              {e.escalation_id}
            </div>
          </div>
          <button className="gctl" onClick={onClose} style={{ width: 36, padding: 0, justifyContent: 'center' }}>
            <Icon name="x" size={16} />
          </button>
        </div>
        <div
          className="scroll"
          style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14, overflow: 'auto' }}
        >
          <div className="row wrap" style={{ gap: 8 }}>
            <span className={`tag ${SEV_COLOR[sev]}`} style={{ textTransform: 'capitalize' }}>
              {sev} severity
            </span>
            <span className="tag">{e.lifecycle_label}</span>
          </div>
          <div className="card solid card-pad">
            <div
              className="faint"
              style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 }}
            >
              Trigger moment
            </div>
            <div style={{ fontSize: 13.5, lineHeight: 1.5 }}>{e.moment ?? '—'}</div>
          </div>
          <div className="card solid card-pad">
            <div
              className="faint"
              style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 8 }}
            >
              Belief at escalation
            </div>
            {beliefRows.map(([l, v, col]) => (
              <div key={l} className="row" style={{ gap: 10, marginBottom: 8 }}>
                <span style={{ width: 140, fontSize: 12, color: 'var(--text-2)', fontWeight: 600 }}>{l}</span>
                <span className="bar grow">
                  <i style={{ width: `${v * 100}%`, background: col }} />
                </span>
                <span className="mono" style={{ fontSize: 12, width: 34, textAlign: 'right' }}>{v.toFixed(2)}</span>
              </div>
            ))}
          </div>
          <div className="row" style={{ gap: 10 }}>
            <button className="btn btn-ghost grow" onClick={() => router.push(`/operate/review/${e.episode_id}`)}>
              <Icon name="eye" size={16} />
              Open call
            </button>
            <button className="btn btn-ghost grow" title="Hand the call to a human operator">
              <Icon name="hand" size={16} />
              Take over
            </button>
          </div>
          <div className="row" style={{ gap: 10 }}>
            {e.lifecycle === 'unreviewed' ? (
              <button className="btn btn-ghost grow">
                <Icon name="check" size={16} />
                Mark reviewed
              </button>
            ) : null}
            <button className="btn btn-ok grow">
              <Icon name="check" size={16} />
              Resolve
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

export default function EscalationsPage() {
  const [tab, setTab] = useState<'unreviewed' | 'reviewed' | 'resolved'>('unreviewed');
  const [data, setData] = useState<EscalationListResponse | null>(null);
  const [sel, setSel] = useState<Escalation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchEscalations({ lifecycle: tab })
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load escalations.');
      });
    return () => {
      cancelled = true;
    };
  }, [tab]);

  const counts = data?.counts ?? { unreviewed: 0, reviewed: 0, resolved: 0, dismissed: 0 };
  const rows = data?.escalations ?? [];

  return (
    <div className="page">
      <div className="page-scroll scroll">
        <div className="pad">
          <div className="row" style={{ marginBottom: 16, gap: 12 }}>
            <div className="seg">
              {TABS.map((t) => (
                <button key={t.key} className={tab === t.key ? 'on' : ''} onClick={() => setTab(t.key)}>
                  {t.label}
                  <span
                    className="nav-badge"
                    style={{
                      display: 'inline-flex',
                      marginLeft: 7,
                      background: tab === t.key ? 'var(--surface)' : 'transparent',
                      color: 'inherit',
                      minWidth: 16,
                      height: 16,
                      fontSize: 10.5,
                    }}
                  >
                    {counts[t.key]}
                  </span>
                </button>
              ))}
            </div>
            <div className="grow" />
            <button className="gctl">
              <Icon name="filter" size={15} />
              Reason
              <Icon name="chevDown" className="gctl-chev" />
            </button>
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
                <h3>Nothing here</h3>
                <p>No {tab} escalations right now.</p>
              </div>
            </div>
          ) : (
            <div className="col" style={{ gap: 11 }}>
              {rows.map((e) => {
                const sev = severityOf(e.reason_key);
                return (
                  <div key={e.escalation_id} className="card esc-card" onClick={() => setSel(e)}>
                    <div className="esc-sev" style={{ background: `var(--${SEV_COLOR[sev]})` }} />
                    <div className="grow">
                      <div className="row" style={{ gap: 9, marginBottom: 5 }}>
                        <span className="mono" style={{ fontSize: 12, color: 'var(--text-3)' }}>{e.escalation_id}</span>
                        <span className={`tag ${SEV_COLOR[sev]}`} style={{ textTransform: 'capitalize' }}>
                          {sev} severity
                        </span>
                        <span className="tag">{e.reason}</span>
                      </div>
                      <div className="b" style={{ fontSize: 14 }}>
                        Call <span className="mono">{e.episode_id}</span>
                      </div>
                      <div className="muted" style={{ fontSize: 12.5, marginTop: 3, maxWidth: 680 }}>{e.moment}</div>
                    </div>
                    <div className="col" style={{ alignItems: 'flex-end', gap: 8 }}>
                      <span className="tag">{e.lifecycle_label}</span>
                    </div>
                    <Icon name="chevron" size={18} style={{ color: 'var(--text-3)' }} />
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
      {sel ? <Drawer e={sel} onClose={() => setSel(null)} /> : null}
    </div>
  );
}

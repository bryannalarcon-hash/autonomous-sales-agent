// P5 — Escalation Queue (U15). The review queue (store.list_escalations) with lifecycle states:
// Unreviewed → Reviewed → Resolved, shown as a segmented control with live counts. Each escalation
// is a card with a severity bar (derived from the reason category — compliance/concession run hot),
// the labeled reason, the linked call, and the trigger moment. Clicking opens a drawer that fetches
// the referenced episode (fetchEpisode) and renders the REAL belief snapshot at the escalation's
// turn_id (nearest at/below, like the review page) — hidden if the turn/episode can't be resolved.
// Drawer actions: Open call (→ Call Review); Take over (honestly DISABLED — operator barge-in is
// not built, mirrors the Live page treatment); Mark reviewed (unreviewed only) → POST lifecycle
// `reviewed`; Resolve → POST lifecycle `resolved`. A successful POST optimistically updates the row
// + drawer and refetches the queue so the segmented-control counts move; a 4xx surfaces inline.
// All reason/lifecycle text is the backend's pre-translated label (no raw key renders).
'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchEpisode, fetchEscalations, updateEscalationLifecycle } from '@/lib/operate-api';
import type { BeliefSnapshot, Escalation, EscalationListResponse } from '@/lib/operate-types';

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

// A belief-driver row to render in the "Belief at escalation" block: the labeled signal, its 0..1
// value, and a value-tuned color (high = danger, mid = warn, low = info). Trust is INVERTED — low
// trust is the bad signal — so it greens at high values and reddens at low ones.
function driverColor(key: string, label: string, value: number): string {
  const isTrust = key === 'trust' || label.toLowerCase() === 'trust';
  const v = isTrust ? 1 - value : value;
  if (v >= 0.66) return 'var(--danger)';
  if (v >= 0.4) return 'var(--warn)';
  return 'var(--info)';
}

// The escalation's belief snapshot = the NEAREST recorded belief at/below the escalation's turn_id
// (the same fallback the call-review replay uses). turn_id indexes the turns list; a prospect turn
// often has no belief, so we walk backward to the last agent turn that carries one. Returns null if
// nothing resolves (the drawer then HIDES the bars rather than show fabricated values).
function beliefAtTurn(
  turns: { turn_id: number; belief: BeliefSnapshot | null }[],
  turnId: number | null,
): BeliefSnapshot | null {
  if (turns.length === 0) return null;
  const start =
    turnId != null && turnId >= 0 && turnId < turns.length ? turnId : turns.length - 1;
  for (let i = start; i >= 0; i--) {
    if (turns[i]?.belief) return turns[i].belief;
  }
  return null;
}

function Drawer({
  e,
  onClose,
  onLifecycleChange,
}: {
  e: Escalation;
  onClose: () => void;
  // Bubble a confirmed lifecycle transition up so the page optimistically updates the row + refetches
  // the queue (so the segmented-control counts move). Called only AFTER the POST returns 200.
  onLifecycleChange: (id: string, lifecycle: Escalation['lifecycle']) => void;
}) {
  const router = useRouter();
  const sev = severityOf(e.reason_key);

  // The drawer reflects the live lifecycle locally (optimistic) so the chip + which actions show
  // update the instant a POST succeeds, without waiting for the parent's refetch to round-trip.
  const [lifecycle, setLifecycle] = useState<Escalation['lifecycle']>(e.lifecycle);
  const [lifecycleLabel, setLifecycleLabel] = useState<string>(e.lifecycle_label);
  const [busy, setBusy] = useState<null | 'reviewed' | 'resolved'>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState<string | null>(null);

  // Real belief snapshot at the escalation moment: fetch the referenced episode, then read the
  // nearest belief at/below turn_id. null = couldn't resolve (episode missing / no belief) → bars
  // are hidden, never faked.
  const [belief, setBelief] = useState<BeliefSnapshot | null>(null);

  // Reset all per-escalation state when a DIFFERENT escalation is selected (the parent reuses one
  // Drawer instance across selections). Keyed on escalation_id ONLY — not lifecycle — because our
  // own successful POST optimistically flips the parent's e.lifecycle for THIS row, and re-running
  // this reset on that change would instantly clear the "Marked reviewed" success affordance.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    setLifecycle(e.lifecycle);
    setLifecycleLabel(e.lifecycle_label);
    setBusy(null);
    setActionError(null);
    setJustSaved(null);
    setBelief(null);
  }, [e.escalation_id]);

  useEffect(() => {
    let cancelled = false;
    if (!e.episode_id) return;
    fetchEpisode(e.episode_id)
      .then((ep) => {
        if (!cancelled) setBelief(beliefAtTurn(ep.turns, e.turn_id));
      })
      .catch(() => {
        // The episode lookup failing just means we hide the bars (no fabricated belief).
        if (!cancelled) setBelief(null);
      });
    return () => {
      cancelled = true;
    };
  }, [e.episode_id, e.turn_id]);

  // Drivers to render: the prioritized primary signals (Trust + Walk-away risk) plus the top few
  // secondary signals, capped so the block stays compact. Each is the REAL recorded 0..1 value.
  const driverRows = belief
    ? [...belief.primary_drivers, ...belief.secondary_drivers.slice(0, 2)]
    : [];

  async function setLifecycleAction(next: 'reviewed' | 'resolved') {
    if (busy) return;
    setBusy(next);
    setActionError(null);
    try {
      const res = await updateEscalationLifecycle(e.escalation_id, next);
      // Reflect the confirmed state in the drawer, then bubble up for the row + queue refetch.
      setLifecycle(res.lifecycle);
      setLifecycleLabel(res.lifecycle === 'resolved' ? 'Resolved' : 'Reviewed');
      setJustSaved(res.lifecycle === 'resolved' ? 'Resolved' : 'Marked reviewed');
      onLifecycleChange(e.escalation_id, res.lifecycle);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : 'Could not update this escalation.');
    } finally {
      setBusy(null);
    }
  }

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
            <span className="tag">{lifecycleLabel}</span>
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
          {/* REAL belief at the escalation's turn — rendered only when we resolved a snapshot; when
              the episode/turn can't be found we hide this block rather than show fake bars. */}
          {driverRows.length > 0 ? (
            <div className="card solid card-pad">
              <div
                className="faint"
                style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 8 }}
              >
                Belief at escalation
              </div>
              {driverRows.map((d) => (
                <div key={d.key} className="row" style={{ gap: 10, marginBottom: 8 }}>
                  <span style={{ width: 140, fontSize: 12, color: 'var(--text-2)', fontWeight: 600 }}>{d.label}</span>
                  <span className="bar grow">
                    <i style={{ width: `${d.value * 100}%`, background: driverColor(d.key, d.label, d.value) }} />
                  </span>
                  <span className="mono" style={{ fontSize: 12, width: 34, textAlign: 'right' }}>{d.value.toFixed(2)}</span>
                </div>
              ))}
            </div>
          ) : null}
          <div className="row" style={{ gap: 10 }}>
            <button className="btn btn-ghost grow" onClick={() => router.push(`/operate/review/${e.episode_id}`)}>
              <Icon name="eye" size={16} />
              Open call
            </button>
            {/* Operator barge-in into the live call isn't built (tracked as CB-01), so this is honestly
                DISABLED with the same tooltip as the Live page — never an enabled-but-inert click. */}
            <button
              className="btn btn-ghost grow"
              disabled
              title="Live takeover isn't available in this build — the monitor is view-only. Interact via the demo console or the phone line."
            >
              <Icon name="hand" size={16} />
              Take over
            </button>
          </div>
          {actionError ? (
            <div className="row" style={{ gap: 7, alignItems: 'center', color: 'var(--danger)', fontSize: 12.5 }}>
              <Icon name="alert" size={14} />
              {actionError}
            </div>
          ) : justSaved ? (
            <div className="row" style={{ gap: 7, alignItems: 'center', color: 'var(--ok)', fontSize: 12.5 }}>
              <Icon name="check" size={14} />
              {justSaved}
            </div>
          ) : null}
          <div className="row" style={{ gap: 10 }}>
            {lifecycle === 'unreviewed' ? (
              <button
                className="btn btn-ghost grow"
                disabled={busy !== null}
                onClick={() => setLifecycleAction('reviewed')}
              >
                <Icon name="check" size={16} />
                {busy === 'reviewed' ? 'Saving…' : 'Mark reviewed'}
              </button>
            ) : null}
            {lifecycle !== 'resolved' ? (
              <button
                className="btn btn-ok grow"
                disabled={busy !== null}
                onClick={() => setLifecycleAction('resolved')}
              >
                <Icon name="check" size={16} />
                {busy === 'resolved' ? 'Resolving…' : 'Resolve'}
              </button>
            ) : null}
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
  // Bumped after a successful lifecycle POST to re-run the queue fetch (so the segmented-control
  // counts unreviewed/reviewed/resolved reflect the move).
  const [refreshTick, setRefreshTick] = useState(0);

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
  }, [tab, refreshTick]);

  // A confirmed lifecycle transition (POST returned 200): optimistically rewrite the moved row in
  // the current list + the open drawer's selection, then trigger a queue refetch so the per-lifecycle
  // counts update. The row will drop out of the active tab on the refetch (e.g. an unreviewed item
  // marked reviewed leaves the Unreviewed tab), which is the expected triage behavior.
  function handleLifecycleChange(id: string, lifecycle: Escalation['lifecycle']) {
    const label = lifecycle === 'resolved' ? 'Resolved' : lifecycle === 'reviewed' ? 'Reviewed' : 'Unreviewed';
    setData((prev) =>
      prev
        ? {
            ...prev,
            escalations: prev.escalations.map((row) =>
              row.escalation_id === id ? { ...row, lifecycle, lifecycle_label: label } : row,
            ),
          }
        : prev,
    );
    setSel((prev) => (prev && prev.escalation_id === id ? { ...prev, lifecycle, lifecycle_label: label } : prev));
    setRefreshTick((t) => t + 1);
  }

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
      {sel ? <Drawer e={sel} onClose={() => setSel(null)} onLifecycleChange={handleLifecycleChange} /> : null}
    </div>
  );
}

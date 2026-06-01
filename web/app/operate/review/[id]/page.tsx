// Call Review (U15) — post-call forensics for one completed episode. Left: the full transcript with
// the per-turn DECISION TRACE (agent turns show the labeled decision + rationale + latency);
// clicking a turn selects it. Right: the BELIEF-TRAJECTORY replay — a scrubber over the turns with
// tick marks at key decision turns, and a belief snapshot (Trust + Walk-away risk gauges, stage +
// the selected turn's decision, slot mini-table) that reflects the NEAREST belief snapshot at/below
// the selected turn. Drives entirely off the recorded per-turn belief log (/api/episodes/{id}).
// Belief/stage/decision labels are pre-translated by the backend; the raw persona archetype + version
// slugs are humanized client-side via @/lib/labels (archetypeLabel/versionLabel) and the raw episode
// id is muted/secondary (never the headline) — no internal index renders as an operator-facing label.
// A 0-turn / still-connecting episode renders a graceful empty state (with a link to the live monitor)
// rather than a blank transcript + dash belief.
// The strip's three actions are all HONEST (no dead clicks): "Flag" is a local-only toggle that
// shows a "Flagged" pill (no backend), "Export" downloads the fetched episode as JSON client-side,
// and "Use in experiment" routes to /improve/lab (same destination as the KB page's lab button).
'use client';

import { useMemo, useState } from 'react';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { Ring } from '@/components/cadence/Spark';
import { fetchEpisode, fmtDuration } from '@/lib/operate-api';
import { archetypeLabel, versionLabel } from '@/lib/labels';
import type { BeliefSnapshot, EpisodeDetail } from '@/lib/operate-types';

function driverValue(b: BeliefSnapshot | null, key: string): number | null {
  return b?.primary_drivers.find((d) => d.key === key)?.value ?? null;
}

// Next.js 14 App Router: route `params` is a PLAIN object (the Promise+`use()` shape is Next 15+).
// Passing a plain object to React.use() throws "unsupported type" — so destructure params directly.
export default function ReviewPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const router = useRouter();
  const [ep, setEp] = useState<EpisodeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cur, setCur] = useState(0);
  // "Flag" has no backend in this demo, so it's an HONEST local-only marker: toggling it shows a
  // "Flagged" pill on the call strip (a clear state change), reset per visit. Not persisted.
  const [flagged, setFlagged] = useState(false);

  // "Export" — client-side download of the already-fetched episode as JSON. No silent no-op: it
  // produces a real file (the forensic record the operator is looking at) named by the call id.
  function exportEpisode() {
    if (!ep) return;
    const blob = new Blob([JSON.stringify(ep, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${ep.episode_id}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  useEffect(() => {
    let cancelled = false;
    fetchEpisode(id)
      .then((e) => {
        if (!cancelled) {
          setEp(e);
          setCur(Math.max(0, e.turns.length - 1));
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load the call.');
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  // nearest belief snapshot at/below the selected turn (the replay reflects that moment).
  const snap = useMemo<BeliefSnapshot | null>(() => {
    if (!ep) return null;
    for (let i = cur; i >= 0; i--) {
      if (ep.turns[i]?.belief) return ep.turns[i].belief;
    }
    return null;
  }, [ep, cur]);

  if (error) {
    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="ico">
            <Icon name="eye" size={28} />
          </div>
          <h3>Call not found</h3>
          <p>{error}</p>
        </div>
      </div>
    );
  }
  if (!ep) {
    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <p className="muted">Loading call…</p>
        </div>
      </div>
    );
  }

  // A still-connecting / never-started call has no turns to replay — show a graceful empty state
  // (with the call id + a link to the live monitor) instead of a blank transcript + dash belief.
  if (ep.turns.length === 0) {
    const live = ep.outcome_key == null || ep.outcome_key === '' || ep.outcome_key === 'in_progress';
    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="ico">
            <Icon name="eye" size={28} />
          </div>
          <h3>{live ? 'Call still in progress' : 'Nothing recorded yet'}</h3>
          <p>
            <span className="mono">{ep.episode_id}</span> has no turns to review yet.{' '}
            {live
              ? 'The transcript and decision trace will be here once the call wraps up.'
              : 'This call ended without a recorded transcript.'}
          </p>
          {live ? (
            <a className="btn btn-ghost btn-sm" href="/operate/live" style={{ marginTop: 12 }}>
              <Icon name="broadcast" size={14} />
              Watch it live
            </a>
          ) : null}
        </div>
      </div>
    );
  }

  const total = ep.turns.length;
  const denom = total - 1 || 1;
  const active = ep.turns[cur];
  const trust = driverValue(snap, 'trust');
  const bail = driverValue(snap, 'bail_risk');
  // key decision turns -> tick marks on the scrubber.
  const marks = ep.turns
    .map((t, i) => ({ t, i }))
    .filter(({ t }) => t.speaker === 'agent' && /pivot|close|de-?risk|reframe/i.test(t.decision_label ?? ''));

  const qualifiedRight = (ep.ladder_tier >= 2) === ep.qualified;

  return (
    <div className="page">
      <div className="rv">
        {/* outcome strip */}
        <div className="rv-strip">
          <div className="rv-av">{ep.episode_id.replace(/[^0-9]/g, '').slice(-2) || 'CA'}</div>
          <div>
            {/* Lead with the humanized archetype as the title; the raw episode id is an internal
                index, demoted to a muted mono secondary line. The cohort id (`coh-…`) has no human
                form so it's dropped — only the outcome accompanies the archetype here. */}
            <div className="rv-name">{ep.persona ? archetypeLabel(ep.persona) : 'Unknown archetype'} · {ep.outcome}</div>
            <div className="rv-co mono" style={{ opacity: 0.7 }}>
              #{ep.episode_id}
            </div>
          </div>
          <div className="rv-stat">
            <div className="l">Outcome</div>
            <div className="v" style={{ color: ep.ladder_tier >= 3 ? 'var(--ok)' : undefined }}>
              {ep.outcome} · {ep.ladder_label}
            </div>
          </div>
          <div className="rv-stat">
            <div className="l">Duration</div>
            <div className="v">{fmtDuration((ep.metrics?.duration_ms as number) ?? null)}</div>
          </div>
          <div className="rv-stat">
            <div className="l">Turns</div>
            <div className="v">{ep.turn_count}</div>
          </div>
          <div className="rv-stat">
            <div className="l">Version</div>
            <div className="v">
              {versionLabel(ep.version)} · {ep.kb_version}
            </div>
          </div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 9, alignItems: 'center' }}>
            {flagged ? (
              <span className="tag warn dot" title="Flagged for follow-up (local to this view)">
                Flagged
              </span>
            ) : null}
            <button
              className={`btn btn-sm ${flagged ? 'btn-ok' : 'btn-ghost'}`}
              onClick={() => setFlagged((f) => !f)}
              title={flagged ? 'Remove the flag from this call' : 'Flag this call for follow-up (local to this view)'}
            >
              <Icon name="flag" size={14} />
              {flagged ? 'Unflag' : 'Flag'}
            </button>
            <button className="btn btn-ghost btn-sm" onClick={exportEpisode} title="Download this call's full record as JSON">
              <Icon name="download" size={14} />
              Export
            </button>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => router.push('/improve/lab')}
              title="Open the Experiment Lab to trial a config change against calls like this"
            >
              <Icon name="flask" size={14} />
              Use in experiment
            </button>
          </div>
        </div>

        {/* transcript + decision trace */}
        <div className="rv-left scroll">
          {ep.turns.map((t, i) => {
            const isAgent = t.speaker === 'agent';
            const latency = t.latency_ms != null ? `${(t.latency_ms / 1000).toFixed(1)}s` : null;
            return (
              <div
                key={t.turn_id}
                className={`rv-turn ${isAgent ? 'a' : 'p'}${i === cur ? ' cur' : ''}`}
                onClick={() => setCur(i)}
              >
                <div className="rv-tnum">{i + 1}</div>
                <div className="rv-tc">
                  <div className="rv-twho">
                    {isAgent ? 'Alex (agent)' : 'Prospect'}
                    <span>·</span>
                    {t.belief?.stage ? (
                      <span className="tag" style={{ padding: '1px 7px' }}>
                        {t.belief.stage}
                      </span>
                    ) : null}
                  </div>
                  <div className="rv-ttext">{t.text}</div>
                  {isAgent && t.decision_label ? (
                    <div className="rv-trace">
                      <span className="rv-dchip">
                        <Icon name="spark" size={12} />
                        {t.decision_label}
                      </span>
                      {t.rationale ? <span className="rv-rat">{`“${t.rationale}”`}</span> : null}
                      {latency ? <span className="rv-lat">{latency}</span> : null}
                    </div>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>

        {/* belief replay */}
        <div className="rv-right scroll">
          <div className="card card-pad rv-replay">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <h3 style={{ fontSize: 14 }}>Belief replay</h3>
              <span className="tag accent">Turn {cur + 1}</span>
            </div>
            <div className="rv-scrub">
              <Icon name="play" size={16} style={{ color: 'var(--accent)' }} />
              <div
                className="rv-track"
                onClick={(e) => {
                  const r = e.currentTarget.getBoundingClientRect();
                  setCur(Math.round(((e.clientX - r.left) / r.width) * denom));
                }}
              >
                <div className="rv-trackfill" style={{ width: `${(cur / denom) * 100}%` }} />
                {marks.map(({ i, t }) => (
                  <div
                    key={i}
                    className="rv-mk"
                    style={{ left: `${(i / denom) * 100}%` }}
                    title={t.decision_label ?? ''}
                  />
                ))}
                <div className="rv-knob" style={{ left: `${(cur / denom) * 100}%` }} />
              </div>
            </div>
            <div className="faint" style={{ fontSize: 11 }}>
              Drag the scrubber or click a turn — the belief state below reflects that moment.
            </div>
          </div>

          <div className="card card-pad">
            <div className="rv-gg">
              <div className="rv-g">
                <div className="rv-gt">
                  <span>Trust</span>
                </div>
                <div className="rv-gv">{trust == null ? '—' : trust.toFixed(2)}</div>
                <div className="bar" style={{ marginTop: 8 }}>
                  <i style={{ width: `${trust == null ? 0 : Math.round(trust * 100)}%`, background: 'var(--ok)' }} />
                </div>
              </div>
              <div className="rv-g">
                <div className="rv-gt">
                  <span>Walk-away risk</span>
                </div>
                <div className="rv-gv">{bail == null ? '—' : bail.toFixed(2)}</div>
                <div className="bar" style={{ marginTop: 8 }}>
                  <i style={{ width: `${bail == null ? 0 : Math.round(bail * 100)}%`, background: 'var(--warn)' }} />
                </div>
              </div>
            </div>
            <div className="row" style={{ margin: '14px 0 12px', gap: 10 }}>
              {snap?.stage ? <span className="tag accent">Stage · {snap.stage}</span> : null}
              {active?.decision_label ? <span className="tag violet">{active.decision_label}</span> : null}
            </div>
            {(snap?.slots ?? []).map((s) => (
              <div className="rv-slot" key={s.key}>
                <span className="n">{s.label}</span>
                <span className="bar">
                  <i
                    style={{
                      width: `${Math.round(s.confidence * 100)}%`,
                      background: s.confidence < 0.6 ? 'var(--warn)' : 'var(--accent)',
                    }}
                  />
                </span>
                <span className="v">{s.confidence.toFixed(2)}</span>
              </div>
            ))}
          </div>

          <div className="rv-outcome">
            <Ring value={ep.qualified ? 0.93 : 0.4} size={46} color={qualifiedRight ? 'var(--ok)' : 'var(--warn)'} label={qualifiedRight ? '✓' : '!'} />
            <div>
              <div className="b" style={{ fontSize: 13.5 }}>
                {qualifiedRight ? 'Qualified correctly' : 'Qualification mismatch'}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {ep.outcome} · {ep.ladder_label}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

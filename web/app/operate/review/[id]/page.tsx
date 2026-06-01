// Call Review (U15) — post-call forensics for one completed episode. Left: the full transcript with
// the per-turn DECISION TRACE (agent turns show the labeled decision + rationale + latency);
// clicking a turn selects it. Right: the BELIEF-TRAJECTORY replay — a scrubber over the turns with
// tick marks at key decision turns, and a belief snapshot (Trust + Walk-away risk gauges, stage +
// the selected turn's decision, slot mini-table) that reflects the NEAREST belief snapshot at/below
// the selected turn. Drives entirely off the recorded per-turn belief log (/api/episodes/{id}); all
// labels are pre-translated by the backend (no driver slug / internal index renders).
'use client';

import { useMemo, useState } from 'react';
import { useEffect } from 'react';
import { Icon } from '@/components/cadence/Icon';
import { Ring } from '@/components/cadence/Spark';
import { fetchEpisode, fmtDuration } from '@/lib/operate-api';
import type { BeliefSnapshot, EpisodeDetail } from '@/lib/operate-types';

function driverValue(b: BeliefSnapshot | null, key: string): number | null {
  return b?.primary_drivers.find((d) => d.key === key)?.value ?? null;
}

// Next.js 14 App Router: route `params` is a PLAIN object (the Promise+`use()` shape is Next 15+).
// Passing a plain object to React.use() throws "unsupported type" — so destructure params directly.
export default function ReviewPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const [ep, setEp] = useState<EpisodeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cur, setCur] = useState(0);

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
            <div className="rv-name">{ep.episode_id}</div>
            <div className="rv-co">
              {(ep.persona ?? 'Unknown archetype')} · {ep.cohort ?? '—'}
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
              {ep.version} · {ep.kb_version}
            </div>
          </div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 9 }}>
            <button className="btn btn-ghost btn-sm">
              <Icon name="flag" size={14} />
              Flag
            </button>
            <button className="btn btn-ghost btn-sm">
              <Icon name="download" size={14} />
              Export
            </button>
            <button className="btn btn-ghost btn-sm">
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
                    {isAgent ? 'Ava (agent)' : 'Prospect'}
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

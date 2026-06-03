// CB-25 — Experiment detail page (/improve/lab/[id]). Reached via the lab drawer's "See experiment".
// Shows ONE experiment's before/after + per-arm outcomes/KPIs + the failure reason (CB-24), and lists
// the A/B's "mock calls" — the champion-arm + challenger-arm self-play episodes the run produced —
// each openable in Review (/operate/review/<id>). Data from GET /api/experiments/{id} (fetchExperimentDetail);
// all semantic labels (state, dimension, version, outcome) are humanized before render — no raw slug
// (versionLabel/dimensionLabel from lib/labels; outcomeLabel from lib/improve-types). A draft / legacy /
// timed-out run that persisted no arm episodes shows the summary + reason with an empty-calls note.
'use client';

import { useCallback, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchExperimentDetail } from '@/lib/improve-api';
import type { ArmCall, ExperimentDetailResponse } from '@/lib/improve-types';
import { outcomeLabel } from '@/lib/improve-types';
import { dimensionLabel, versionLabel } from '@/lib/labels';

function changeLabel(dimension: string, dimensionLabelText: string): string {
  return dimensionLabelText || dimensionLabel(dimension);
}

// outcome slug -> a chip color class (the LABEL text comes from outcomeLabel; this is color only).
function outcomeTag(outcome: string | null): string {
  if (outcome === 'enrolled' || outcome === 'trial_booked' || outcome === 'consult_booked') return 'ok';
  if (outcome === 'walked') return 'danger';
  if (outcome === 'escalated') return 'warn';
  return '';
}

function pts(v: number): string {
  return `${v > 0 ? '+' : ''}${Math.round(v * 100)} pts`;
}
function dec(v: number): string {
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}`;
}

// One A/B arm's call list — the per-arm self-play episodes, each a row linking to Review.
function ArmColumn({
  title,
  accent,
  versionText,
  kpi,
  calls,
  onOpen,
}: {
  title: string;
  accent: boolean;
  versionText: string;
  kpi: number;
  calls: ArmCall[];
  onOpen: (episodeId: string) => void;
}) {
  return (
    <div className="card card-pad grow" style={accent ? { borderColor: 'var(--accent-border)' } : undefined}>
      <div
        style={{
          fontSize: 10.5,
          fontWeight: 700,
          textTransform: 'uppercase',
          letterSpacing: '.06em',
          color: accent ? 'var(--accent-strong)' : 'var(--text-faint)',
        }}
      >
        {title}
      </div>
      <div className="b" style={{ fontSize: 16, fontFamily: 'var(--font-display)', margin: '4px 0', color: accent ? 'var(--accent-strong)' : undefined }}>
        {versionText}
      </div>
      <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>Ladder {kpi.toFixed(2)}</div>
      {calls.length === 0 ? (
        <div className="muted" style={{ fontSize: 12 }}>No mock calls recorded for this arm.</div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {calls.map((c, i) => (
            <button
              key={c.episode_id}
              className="row"
              onClick={() => onOpen(c.episode_id)}
              style={{
                gap: 9,
                alignItems: 'center',
                padding: '8px 10px',
                border: '1px solid var(--border)',
                borderRadius: 9,
                background: 'var(--surface)',
                cursor: 'pointer',
                textAlign: 'left',
                width: '100%',
              }}
              title="Open this call in Review"
            >
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', width: 22 }}>{i + 1}</span>
              <span className={`tag ${outcomeTag(c.outcome)} dot`}>{outcomeLabel(c.outcome)}</span>
              <div className="grow" />
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-2)' }}>tier {c.ladder_tier}</span>
              <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)' }}>· {c.turn_count} turns</span>
              <Icon name="arrowR" size={14} style={{ color: 'var(--accent)' }} />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function ExperimentDetailPage({ params }: { params: { id: string } }) {
  const { id } = params;
  const router = useRouter();
  const [data, setData] = useState<ExperimentDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchExperimentDetail(id);
      setData(res);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load this experiment.');
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  // Open one arm episode in Review (the existing /operate/review/<id> page reads it from the SAME store).
  const openInReview = useCallback(
    (episodeId: string) => router.push(`/operate/review/${encodeURIComponent(episodeId)}`),
    [router],
  );

  const e = data?.experiment ?? null;
  const tripped = e?.guardrail === 'trip';
  const isFailed = e?.state === 'rejected' || e?.state === 'blocked';
  const failureReason = e && (isFailed || tripped) ? e.guardrail_reason : null;

  return (
    <div className="page">
      <div className="page-scroll scroll">
        <div className="pad" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          <button className="btn btn-ghost" onClick={() => router.push('/improve/lab')} style={{ alignSelf: 'flex-start' }}>
            <Icon name="arrowR" size={15} style={{ transform: 'rotate(180deg)' }} />
            Back to the lab
          </button>

          {loading ? (
            <div className="card">
              <div className="empty">
                <div className="ico"><Icon name="flask" size={28} /></div>
                <h3>Loading the experiment…</h3>
              </div>
            </div>
          ) : error || !e ? (
            <div className="card">
              <div className="empty">
                <div className="ico"><Icon name="alert" size={28} /></div>
                <h3>Couldn&apos;t load this experiment</h3>
                <p>{error ?? 'It may have been removed.'}</p>
                <button className="btn btn-primary" style={{ marginTop: 12 }} onClick={() => void load()}>
                  Try again
                </button>
              </div>
            </div>
          ) : (
            <>
              {/* header */}
              <div className="card card-pad">
                <div className="row" style={{ gap: 8, marginBottom: 6 }}>
                  <span className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>
                    {changeLabel(e.dimension, e.dimension_label)}
                  </span>
                  <span className="tag dot">{e.state_label}</span>
                </div>
                <div className="b" style={{ fontSize: 19, fontFamily: 'var(--font-display)', letterSpacing: '-0.02em' }}>
                  {e.name || changeLabel(e.dimension, e.dimension_label)}
                </div>
                <div className="row" style={{ gap: 8, marginTop: 8 }}>
                  <span className="tag">
                    {versionLabel(e.champion_version)}
                    <Icon name="arrowR" size={11} />
                    {versionLabel(e.challenger_version)}
                  </span>
                  <span className="muted" style={{ fontSize: 12 }}>{e.diff_description}</span>
                </div>
              </div>

              {/* CB-24: the failure / rejection reason, surfaced prominently. */}
              {failureReason ? (
                <div
                  className="row"
                  style={{
                    gap: 9,
                    alignItems: 'flex-start',
                    padding: '12px 14px',
                    borderRadius: 11,
                    background: 'var(--danger-soft)',
                    border: '1px solid var(--danger-border)',
                  }}
                >
                  <Icon name="alert" size={16} style={{ color: 'var(--danger)', marginTop: 1 }} />
                  <div>
                    <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em', color: 'var(--danger)' }}>
                      Why {e.state === 'blocked' ? 'it needs approval' : 'it ended'}
                    </div>
                    <div style={{ fontSize: 13, color: 'var(--danger)', marginTop: 3, lineHeight: 1.5 }}>{failureReason}</div>
                  </div>
                </div>
              ) : null}

              {/* before/after KPIs */}
              <div className="card card-pad">
                <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 10 }}>
                  Outcome (challenger − champion)
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: 12 }}>
                  <div>
                    <div className="faint" style={{ fontSize: 11 }}>Same-call enroll</div>
                    <div className="mono" style={{ fontSize: 16, fontWeight: 650, marginTop: 3, color: e.enroll_delta > 0 ? 'var(--ok)' : 'var(--text)' }}>
                      {pts(e.enroll_delta)}
                    </div>
                  </div>
                  <div>
                    <div className="faint" style={{ fontSize: 11 }}>Ladder score</div>
                    <div className="mono" style={{ fontSize: 16, fontWeight: 650, marginTop: 3, color: e.delta > 0 ? 'var(--ok)' : e.delta < 0 ? 'var(--danger)' : 'var(--text)' }}>
                      {dec(e.delta)}
                    </div>
                  </div>
                  <div>
                    <div className="faint" style={{ fontSize: 11 }}>95% CI</div>
                    <div className="mono" style={{ fontSize: 13, fontWeight: 650, marginTop: 3 }}>
                      [{e.delta_ci[0].toFixed(2)}, {e.delta_ci[1].toFixed(2)}]
                    </div>
                  </div>
                  <div>
                    <div className="faint" style={{ fontSize: 11 }}>Significance</div>
                    <div className="mono" style={{ fontSize: 16, fontWeight: 650, marginTop: 3, color: e.challenger_better ? 'var(--ok)' : 'var(--text)' }}>
                      {Math.round(e.significance * 100)}%
                    </div>
                  </div>
                </div>
              </div>

              {/* CB-25: the A/B mock calls per arm — each openable in Review. */}
              <div>
                <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 8 }}>
                  A/B mock calls — open any in Review
                </div>
                {!data!.has_calls ? (
                  <div className="card card-pad">
                    <div className="muted" style={{ fontSize: 13, lineHeight: 1.55 }}>
                      This run didn&apos;t record per-call transcripts to open — it may be a draft, a run that
                      didn&apos;t complete, or an older experiment. The outcome and the reason above still apply.
                    </div>
                  </div>
                ) : (
                  <div className="row" style={{ gap: 14, alignItems: 'stretch' }}>
                    <ArmColumn
                      title="Champion arm"
                      accent={false}
                      versionText={versionLabel(e.champion_version)}
                      kpi={e.champion_kpi}
                      calls={data!.champion_calls}
                      onOpen={openInReview}
                    />
                    <ArmColumn
                      title="Challenger arm"
                      accent
                      versionText={versionLabel(e.challenger_version)}
                      kpi={e.challenger_kpi}
                      calls={data!.challenger_calls}
                      onOpen={openInReview}
                    />
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// P6 — Experiment Lab (U16). Champion-vs-challenger experiments rendered as cards: the BEFORE/AFTER
// KPI delta + significance (does the delta CI exclude 0?) + guardrail status + the declared
// one-dimension diff, with running/passed/blocked/promoted/rejected state chips. Active/Past tabs.
// A card opens a right-side drawer with the full champion→challenger before/after, the "what changed"
// diff line, the lift block (enroll, ladder, 95% CI, significance) + guardrail strip, and a
// state-dependent CTA (a blocked challenger links to the Approval Queue, per the handoff §183). Data
// from /api/experiments; the discovery-sequencing before/after is the headline demo artifact. All
// semantic labels (state, dimension) arrive pre-translated from the backend — no raw slug renders.
'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchExperiments } from '@/lib/improve-api';
import type { Experiment } from '@/lib/improve-types';

// Experiment state -> the tag color class for the chip (the LABEL text comes from the backend).
const STATE_TAG: Record<Experiment['state'], string> = {
  draft: 'info',
  running: 'info',
  passed: 'ok',
  blocked: 'danger',
  promoted: 'ok',
  rejected: '',
  paused: 'warn',
};

const ACTIVE_STATES: Experiment['state'][] = ['draft', 'running', 'passed', 'blocked'];

function pts(v: number): string {
  return `${v > 0 ? '+' : ''}${Math.round(v * 100)} pts`;
}
function dec(v: number): string {
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}`;
}

function Cell({ l, v, good, last }: { l: string; v: string; good?: boolean; last?: boolean }) {
  const color = good === true ? 'var(--ok)' : good === false ? 'var(--danger)' : 'var(--text)';
  return (
    <div style={{ padding: '11px 14px', borderRight: last ? 0 : '1px solid var(--border)' }}>
      <div className="faint" style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em' }}>
        {l}
      </div>
      <div className="mono" style={{ fontSize: 16, fontWeight: 650, marginTop: 3, color }}>
        {v}
      </div>
    </div>
  );
}

function Stat({ l, v, good, mono }: { l: string; v: string; good?: boolean | null; mono?: boolean }) {
  const color = good === true ? 'var(--ok)' : good === false ? 'var(--danger)' : 'var(--text)';
  return (
    <div>
      <div className="faint" style={{ fontSize: 11, fontWeight: 600 }}>
        {l}
      </div>
      <div
        className={mono ? 'mono' : ''}
        style={{
          fontSize: mono ? 13 : 18,
          fontWeight: 650,
          marginTop: 3,
          fontFamily: mono ? 'var(--font-mono)' : 'var(--font-display)',
          color,
        }}
      >
        {v}
      </div>
    </div>
  );
}

function ExpDrawer({ e, onClose }: { e: Experiment; onClose: () => void }) {
  const router = useRouter();
  const tripped = e.guardrail === 'trip';
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="drawer">
        <div className="card-head">
          <div className="grow">
            <div className="row" style={{ gap: 8, marginBottom: 4 }}>
              <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{e.experiment_id}</span>
              <span className={`tag ${STATE_TAG[e.state]} dot`}>{e.state_label}</span>
            </div>
            <div className="b" style={{ fontSize: 15.5, fontFamily: 'var(--font-display)' }}>{e.name}</div>
          </div>
          <button className="gctl" onClick={onClose} style={{ width: 36, padding: 0, justifyContent: 'center' }}>
            <Icon name="x" size={16} />
          </button>
        </div>
        <div className="scroll" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14, overflow: 'auto' }}>
          {/* before / after */}
          <div className="row" style={{ gap: 12, alignItems: 'stretch' }}>
            <div className="card solid card-pad grow">
              <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em' }}>
                Champion
              </div>
              <div className="b mono" style={{ fontSize: 17, fontFamily: 'var(--font-display)', margin: '4px 0' }}>
                {e.champion_version ?? '—'}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>Ladder {e.champion_kpi.toFixed(2)} · current production</div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center' }}>
              <Icon name="arrowR" size={20} style={{ color: 'var(--accent)' }} />
            </div>
            <div className="card card-pad grow" style={{ borderColor: 'var(--accent-border)' }}>
              <div style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', color: 'var(--accent-strong)' }}>
                Challenger
              </div>
              <div className="b mono" style={{ fontSize: 17, fontFamily: 'var(--font-display)', margin: '4px 0', color: 'var(--accent-strong)' }}>
                {e.challenger_version}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>Ladder {e.challenger_kpi.toFixed(2)} · {e.population}</div>
            </div>
          </div>

          <div className="card solid card-pad">
            <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 7 }}>
              What changed · {e.dimension_label}
            </div>
            <div className="mono" style={{ fontSize: 13, lineHeight: 1.6 }}>{e.diff_description}</div>
          </div>

          <div className="card solid card-pad">
            <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 10 }}>
              Lift (challenger − champion)
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <Stat l="Same-call enroll" v={pts(e.enroll_delta)} good={e.enroll_delta > 0} />
              <Stat l="Ladder score" v={dec(e.delta)} good={e.delta > 0} />
              <Stat l="95% CI" v={`[${e.delta_ci[0].toFixed(2)}, ${e.delta_ci[1].toFixed(2)}]`} mono />
              <Stat l="Significance" v={`${Math.round(e.significance * 100)}%`} good={e.challenger_better ? true : null} />
            </div>
            <div
              className="row"
              style={{
                marginTop: 12,
                gap: 8,
                alignItems: 'center',
                padding: '9px 11px',
                borderRadius: 10,
                background: tripped ? 'var(--danger-soft)' : 'var(--ok-soft)',
                border: `1px solid ${tripped ? 'var(--danger-border)' : 'var(--ok-border)'}`,
              }}
            >
              <Icon name="shield" size={15} style={{ color: tripped ? 'var(--danger)' : 'var(--ok)' }} />
              <span style={{ fontSize: 12.5, fontWeight: 600, color: tripped ? 'var(--danger)' : 'var(--ok)' }}>
                {tripped
                  ? `Guardrail tripped — needs approval${e.guardrail_reason ? ` · ${e.guardrail_reason}` : ''}`
                  : e.guardrail === 'warn'
                    ? 'Guardrail warning'
                    : 'All guardrails pass'}
              </span>
            </div>
          </div>

          {e.state === 'passed' ? (
            <button className="btn btn-primary">
              <Icon name="promote" size={16} />
              Promote {e.challenger_version} to champion
            </button>
          ) : e.state === 'blocked' ? (
            <button className="btn btn-primary" onClick={() => router.push('/improve/approvals')}>
              <Icon name="badge" size={16} />
              Send to approval queue
              <Icon name="arrowR" size={15} />
            </button>
          ) : e.state === 'running' || e.state === 'draft' ? (
            <div className="row" style={{ gap: 10 }}>
              <button className="btn btn-ghost grow">Pause</button>
              <button className="btn btn-danger grow">Stop experiment</button>
            </div>
          ) : (
            <button className="btn btn-ghost">
              <Icon name="rollback" size={16} />
              Clone &amp; re-run
            </button>
          )}
        </div>
      </div>
    </>
  );
}

export default function LabPage() {
  const [tab, setTab] = useState<'active' | 'past'>('active');
  const [rows, setRows] = useState<Experiment[]>([]);
  const [sel, setSel] = useState<Experiment | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchExperiments()
      .then((res) => {
        if (!cancelled) {
          setRows(res.experiments);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load experiments.');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const active = useMemo(() => rows.filter((e) => ACTIVE_STATES.includes(e.state)), [rows]);
  const past = useMemo(() => rows.filter((e) => !ACTIVE_STATES.includes(e.state)), [rows]);
  const visible = tab === 'active' ? active : past;

  return (
    <div className="page">
      <div className="page-scroll scroll">
        <div className="pad">
          <div className="row" style={{ marginBottom: 16, gap: 12 }}>
            <div className="seg">
              <button className={tab === 'active' ? 'on' : ''} onClick={() => setTab('active')}>
                Active · {active.length}
              </button>
              <button className={tab === 'past' ? 'on' : ''} onClick={() => setTab('past')}>
                Past · {past.length}
              </button>
            </div>
            <div className="grow" />
            <button className="btn btn-primary">
              <Icon name="plus" size={16} />
              New experiment
            </button>
          </div>

          {error ? (
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="alert" size={28} />
                </div>
                <h3>Couldn&apos;t load experiments</h3>
                <p>{error}</p>
              </div>
            </div>
          ) : visible.length === 0 ? (
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="flask" size={28} />
                </div>
                <h3>No {tab} experiments</h3>
                <p>Champion-vs-challenger runs appear here. Start one with &ldquo;New experiment&rdquo; or save a draft from the editor.</p>
              </div>
            </div>
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
              {visible.map((e) => (
                <div
                  key={e.experiment_id}
                  className="card"
                  style={{ cursor: 'pointer', overflow: 'hidden' }}
                  onClick={() => setSel(e)}
                >
                  <div className="card-pad" style={{ paddingBottom: 12 }}>
                    <div className="row" style={{ gap: 8, marginBottom: 9 }}>
                      <span className="mono" style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{e.experiment_id}</span>
                      <span className={`tag ${STATE_TAG[e.state]} dot`}>{e.state_label}</span>
                      {e.state === 'passed' && e.challenger_better && !e.is_extreme ? (
                        <span className="tag ok">
                          <Icon name="bolt" size={12} />
                          Auto-promote ready
                        </span>
                      ) : null}
                    </div>
                    <div className="b" style={{ fontSize: 15, fontFamily: 'var(--font-display)', letterSpacing: '-0.02em' }}>{e.name}</div>
                    <div className="row" style={{ gap: 8, marginTop: 6 }}>
                      <span className="tag">
                        {e.champion_version ?? '—'}
                        <Icon name="arrowR" size={11} />
                        {e.challenger_version}
                      </span>
                      <span className="muted" style={{ fontSize: 12 }}>{e.population}</span>
                    </div>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', borderTop: '1px solid var(--border)' }}>
                    <Cell l="Enroll Δ" v={pts(e.enroll_delta)} good={e.enroll_delta > 0} />
                    <Cell l="Ladder Δ" v={dec(e.delta)} good={e.delta > 0} />
                    <Cell l="Significance" v={`${Math.round(e.significance * 100)}%`} last />
                  </div>
                  {e.state === 'blocked' ? (
                    <div
                      style={{
                        padding: '9px 16px',
                        background: 'var(--danger-soft)',
                        borderTop: '1px solid var(--danger-border)',
                        fontSize: 11.5,
                        color: 'var(--danger)',
                        display: 'flex',
                        gap: 7,
                        alignItems: 'center',
                      }}
                    >
                      <Icon name="alert" size={13} />
                      {e.guardrail_reason ?? 'Guardrail blocked — needs approval'}
                    </div>
                  ) : null}
                  <div style={{ padding: '10px 16px', borderTop: '1px solid var(--border)' }}>
                    <div className="row" style={{ justifyContent: 'space-between', marginBottom: 5 }}>
                      <span className="faint" style={{ fontSize: 11 }}>Sample</span>
                      <span className="mono" style={{ fontSize: 11.5 }}>{e.n} / {e.target || '—'}</span>
                    </div>
                    <div className="bar">
                      <i
                        style={{
                          width: `${e.target ? Math.min(100, (e.n / e.target) * 100) : 0}%`,
                          background: e.target && e.n >= e.target ? 'var(--ok)' : 'var(--accent)',
                        }}
                      />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      {sel ? <ExpDrawer e={sel} onClose={() => setSel(null)} /> : null}
    </div>
  );
}

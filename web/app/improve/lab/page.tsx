// P6 — Experiment Lab (U16). Champion-vs-challenger experiments rendered as cards: the BEFORE/AFTER
// KPI delta + significance (does the delta CI exclude 0?) + guardrail status + the declared
// one-dimension diff, with running/passed/blocked/promoted/rejected state chips. Active/Past tabs.
// A card opens a right-side drawer with the full champion→challenger before/after, the "what changed"
// diff line, the lift block (enroll, ladder, 95% CI, significance) + guardrail strip, and a
// state-dependent CTA (a blocked challenger links to the Approval Queue, per the handoff §183). The
// "Run experiment" control opens a RunDrawer: pick a dimension (Discovery sequencing reorder, or a
// threshold + new value), see an explicit "this runs N real model calls" cost note, submit to
// /api/experiments/run, then watch the new card transition running -> result (we poll /api/experiments).
// Data from /api/experiments; the discovery-sequencing before/after is the headline demo artifact. All
// semantic labels (state, dimension) arrive pre-translated from the backend — no raw slug renders.
'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchExperiments, fetchPlaybook, runExperiment } from '@/lib/improve-api';
import type { Experiment, PlaybookResponse, RunExperimentRequest } from '@/lib/improve-types';

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

// The dimensions an operator can A/B from the lab. Each carries the backend slug (sent to the run
// endpoint) + a human label (what the operator reads — never the raw slug). Thresholds flagged
// `extreme` (a pricing concession) are offered but will land in the approval queue, not auto-promote.
type RunKind = 'discovery' | 'threshold';
interface ThresholdOption {
  key: string; // the thresholds.<key> tail (sent as `thresholds.${key}`)
  label: string; // operator-facing name
  extreme?: boolean;
}
const THRESHOLD_OPTIONS: ThresholdOption[] = [
  { key: 'pushiness_cap', label: 'Pushiness cap' },
  { key: 'pushiness_pressure_count_cap', label: 'Pushiness pressure count' },
  { key: 'discovery_slots_required', label: 'Discovery depth' },
  { key: 'low_confidence_level', label: 'Low-confidence threshold' },
  { key: 'max_concession_band', label: 'Pricing concession band', extreme: true },
  { key: 'trust_gate_open_price', label: 'Trust gate for pricing', extreme: true },
];

function moved<T>(arr: T[], from: number, to: number): T[] {
  if (to < 0 || to >= arr.length) return arr;
  const next = arr.slice();
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

// The Run-an-A/B drawer. Picks ONE dimension + value, shows the explicit cost note, and submits.
function RunDrawer({
  onClose,
  onStarted,
}: {
  onClose: () => void;
  onStarted: (exp: Experiment) => void;
}) {
  const [playbook, setPlaybook] = useState<PlaybookResponse | null>(null);
  const [kind, setKind] = useState<RunKind>('discovery');
  const [seq, setSeq] = useState<string[]>([]);
  const [thrKey, setThrKey] = useState<string>(THRESHOLD_OPTIONS[0].key);
  const [thrValue, setThrValue] = useState<string>('');
  const [n, setN] = useState<number>(12);
  const [name, setName] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchPlaybook()
      .then((pb) => {
        if (cancelled) return;
        setPlaybook(pb);
        setSeq(pb.discovery_sequence ?? []);
        const cur = pb.thresholds?.[THRESHOLD_OPTIONS[0].key];
        if (typeof cur === 'number') setThrValue(String(cur));
      })
      .catch((e) => !cancelled && setErr(e instanceof Error ? e.message : 'Could not load the champion config.'));
    return () => {
      cancelled = true;
    };
  }, []);

  // Keep the threshold value field showing the current champion value when the key changes.
  function selectThreshold(key: string) {
    setThrKey(key);
    const cur = playbook?.thresholds?.[key];
    setThrValue(typeof cur === 'number' ? String(cur) : '');
  }

  const selectedThr = THRESHOLD_OPTIONS.find((t) => t.key === thrKey);
  const willBeExtreme = kind === 'threshold' && !!selectedThr?.extreme;
  // A reorder that hasn't moved anything is a no-op the backend rejects — disable submit for it.
  const reorderUnchanged =
    kind === 'discovery' &&
    !!playbook &&
    JSON.stringify(seq) === JSON.stringify(playbook.discovery_sequence ?? []);
  const thrUnchanged =
    kind === 'threshold' &&
    !!playbook &&
    String(playbook.thresholds?.[thrKey]) === thrValue.trim();
  const canSubmit =
    !busy &&
    !!playbook &&
    (kind === 'discovery' ? !reorderUnchanged && seq.length >= 2 : thrValue.trim() !== '' && !thrUnchanged);

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setErr(null);
    const req: RunExperimentRequest =
      kind === 'discovery'
        ? { dimension: 'playbooks.discovery_sequence', value: seq, n, name: name || undefined }
        : { dimension: `thresholds.${thrKey}`, value: Number(thrValue), n, name: name || undefined };
    try {
      const res = await runExperiment(req);
      onStarted(res.experiment);
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Run failed.');
      setBusy(false);
    }
  }

  return (
    <>
      <div className="scrim" onClick={busy ? undefined : onClose} />
      <div className="drawer">
        <div className="card-head">
          <div className="grow">
            <div className="b" style={{ fontSize: 15.5, fontFamily: 'var(--font-display)' }}>Run an experiment</div>
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              A/B the live champion against one changed value on a held-out persona set.
            </div>
          </div>
          <button className="gctl" onClick={onClose} disabled={busy} style={{ width: 36, padding: 0, justifyContent: 'center' }}>
            <Icon name="x" size={16} />
          </button>
        </div>

        <div className="scroll" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 16, overflow: 'auto' }}>
          {/* what to change */}
          <div>
            <div className="faint" style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em', marginBottom: 8 }}>
              What to change
            </div>
            <div className="seg" style={{ display: 'flex' }}>
              <button className={kind === 'discovery' ? 'on' : ''} onClick={() => setKind('discovery')}>
                Discovery sequencing
              </button>
              <button className={kind === 'threshold' ? 'on' : ''} onClick={() => setKind('threshold')}>
                Policy threshold
              </button>
            </div>
          </div>

          {kind === 'discovery' ? (
            <div className="card solid card-pad">
              <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 8 }}>
                Reorder discovery questions
              </div>
              {seq.length === 0 ? (
                <div className="muted" style={{ fontSize: 12.5 }}>Loading the current sequence…</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {seq.map((slot, i) => (
                    <div
                      key={slot}
                      className="row"
                      style={{ gap: 8, alignItems: 'center', padding: '7px 10px', border: '1px solid var(--border)', borderRadius: 9, background: 'var(--surface)' }}
                    >
                      <span className="mono" style={{ fontSize: 11, color: 'var(--text-3)', width: 18 }}>{i + 1}</span>
                      <span style={{ fontSize: 13, fontWeight: 550, textTransform: 'capitalize' }}>{slot.replace(/_/g, ' ')}</span>
                      <div className="grow" />
                      <button className="gctl" disabled={i === 0} onClick={() => setSeq((s) => moved(s, i, i - 1))} style={{ width: 30, padding: 0, justifyContent: 'center' }} aria-label="Move up">
                        <Icon name="chevUp" size={14} />
                      </button>
                      <button className="gctl" disabled={i === seq.length - 1} onClick={() => setSeq((s) => moved(s, i, i + 1))} style={{ width: 30, padding: 0, justifyContent: 'center' }} aria-label="Move down">
                        <Icon name="chevDown" size={14} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div className="muted" style={{ fontSize: 11.5, marginTop: 8 }}>
                Move one question to change the order. This is a non-extreme change — a clear win auto-promotes.
              </div>
            </div>
          ) : (
            <div className="card solid card-pad" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div>
                <div className="faint" style={{ fontSize: 11, fontWeight: 600, marginBottom: 5 }}>Threshold</div>
                <select className="field" value={thrKey} onChange={(e) => selectThreshold(e.target.value)} style={{ width: '100%' }}>
                  {THRESHOLD_OPTIONS.map((t) => (
                    <option key={t.key} value={t.key}>
                      {t.label}
                      {t.extreme ? ' (needs approval)' : ''}
                    </option>
                  ))}
                  <Icon name="chevDown" size={14} />
                </select>
              </div>
              <div>
                <div className="faint" style={{ fontSize: 11, fontWeight: 600, marginBottom: 5 }}>
                  New value{playbook?.thresholds?.[thrKey] !== undefined ? ` · currently ${playbook.thresholds[thrKey]}` : ''}
                </div>
                <input
                  className="input"
                  style={{ width: '100%' }}
                  type="number"
                  step="0.05"
                  value={thrValue}
                  onChange={(e) => setThrValue(e.target.value)}
                  placeholder="e.g. 0.5"
                />
              </div>
              {willBeExtreme ? (
                <div className="row" style={{ gap: 7, alignItems: 'center', padding: '8px 10px', borderRadius: 9, background: 'var(--warn-soft)', border: '1px solid var(--warn-border)' }}>
                  <Icon name="shield" size={14} style={{ color: 'var(--warn)' }} />
                  <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--warn)' }}>
                    A pricing change is extreme — even a clear win goes to the approval queue, not auto-promote.
                  </span>
                </div>
              ) : null}
            </div>
          )}

          {/* name + sample size */}
          <div className="row" style={{ gap: 12 }}>
            <div className="grow">
              <div className="faint" style={{ fontSize: 11, fontWeight: 600, marginBottom: 5 }}>Name (optional)</div>
              <input className="input" style={{ width: '100%' }} value={name} onChange={(e) => setName(e.target.value)} placeholder="ROI-first ordering" />
            </div>
            <div style={{ width: 110 }}>
              <div className="faint" style={{ fontSize: 11, fontWeight: 600, marginBottom: 5 }}>Calls per arm</div>
              <input
                className="input"
                style={{ width: '100%' }}
                type="number"
                min={1}
                max={24}
                value={n}
                onChange={(e) => setN(Math.max(1, Math.min(24, Number(e.target.value) || 1)))}
              />
            </div>
          </div>

          {/* explicit cost note (before submit) */}
          <div className="row" style={{ gap: 8, alignItems: 'center', padding: '10px 12px', borderRadius: 10, background: 'var(--accent-soft)', border: '1px solid var(--accent-border)' }}>
            <Icon name="bolt" size={15} style={{ color: 'var(--accent-strong)' }} />
            <span style={{ fontSize: 12.5, color: 'var(--accent-strong)', fontWeight: 600 }}>
              This runs {n * 2} real model calls ({n} per arm × champion and challenger) and spends model credit.
            </span>
          </div>

          {err ? (
            <div className="row" style={{ gap: 7, alignItems: 'center', color: 'var(--danger)', fontSize: 12.5 }}>
              <Icon name="alert" size={14} />
              {err}
            </div>
          ) : null}

          <button className="btn btn-primary" onClick={submit} disabled={!canSubmit}>
            <Icon name="play" size={16} />
            {busy ? 'Running…' : `Run experiment · ${n * 2} calls`}
          </button>
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
  const [runOpen, setRunOpen] = useState(false);
  // experiment_ids freshly started this session, so a new card reads "Just run" until it settles.
  const [freshIds, setFreshIds] = useState<Set<string>>(new Set());
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetchExperiments();
      setRows(res.experiments);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load experiments.');
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // A run returns the finished record synchronously; insert it immediately, then poll a few times so
  // the card reflects the settled state (the backend persisted running -> result in one call).
  const onStarted = useCallback(
    (exp: Experiment) => {
      setRows((prev) => [exp, ...prev.filter((e) => e.experiment_id !== exp.experiment_id)]);
      setFreshIds((prev) => new Set(prev).add(exp.experiment_id));
      setTab(ACTIVE_STATES.includes(exp.state) ? 'active' : 'past');
      if (pollRef.current) clearInterval(pollRef.current);
      let ticks = 0;
      pollRef.current = setInterval(() => {
        ticks += 1;
        void load();
        if (ticks >= 4 && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      }, 1500);
    },
    [load],
  );

  useEffect(() => () => { if (pollRef.current) clearInterval(pollRef.current); }, []);

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
            <button className="btn btn-primary" onClick={() => setRunOpen(true)}>
              <Icon name="play" size={16} />
              Run experiment
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
                <p>Champion-vs-challenger runs appear here. Start one with &ldquo;Run experiment&rdquo; or save a draft from the editor.</p>
                <button className="btn btn-primary" style={{ marginTop: 12 }} onClick={() => setRunOpen(true)}>
                  <Icon name="play" size={16} />
                  Run experiment
                </button>
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
                      {freshIds.has(e.experiment_id) ? (
                        <span className="tag info">
                          <Icon name="spark" size={12} />
                          Just run
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
      {runOpen ? <RunDrawer onClose={() => setRunOpen(false)} onStarted={onStarted} /> : null}
    </div>
  );
}

// P4 — KPI Views (U15). Performance of a version/cohort, computed from real episodes via the backend
// (grading.kpi_score + compare_versions). The two HEADLINE tiles are deliberately DISTINCT numbers:
// the weighted commitment-ladder score (mean ladder rung, on a 0..ladder_max scale) AND the same-
// call enrollment rate (share of calls that closed at full enrollment, a 0..100% number) — a config
// can lift one without the other, so the operator sees both. Version + cohort selectors filter the
// set; "Compare versions" adds a baseline arm with a bootstrap-CI delta. Secondary panels: ladder-
// tier distribution + per-archetype conversion. All tier/outcome labels are pre-translated.
//
// The version selector is populated from REAL data: /api/versions (lineage) plus the cohort the
// episodes are actually tagged with, and it DEFAULTS to a version that has episodes so numbers show
// on first load (no more hardcoded v10/v11/v12 placeholders that matched nothing). Hash suffixes on
// raw version ids are an internal index and are NOT rendered — the selector shows a clean label.
'use client';

import { useEffect, useMemo, useState } from 'react';
import { Icon } from '@/components/cadence/Icon';
import { Spark } from '@/components/cadence/Spark';
import { fetchKpis } from '@/lib/operate-api';
import { fetchVersions } from '@/lib/improve-api';
import type { KpiResponse } from '@/lib/operate-types';

// The cohort the seeded episodes are actually tagged with — the only selection guaranteed to have
// episodes on a fresh backend, so it is the default and is always present in the selector.
const SEEDED_VERSION = 'champion_v0';
const COHORTS = ['All', 'held_out', 'training', 'live'];

function pct(v: number): string {
  return `${Math.round(v * 100)}%`;
}

// One selectable version: the raw id sent to the API + the operator-facing label. The hash suffix on
// a raw id (e.g. "v1-f3798d7a") is an internal index, so the label strips it; the champion is marked.
interface VersionOption {
  value: string; // raw version id passed to /api/kpis
  label: string; // human-readable label shown in the dropdown (no internal hash)
}

// Strip the internal suffixes from a raw version id for display: the experiment dimension/seq suffix
// ("champion_v0__playbooks_discovery_sequence__7" -> "champion_v0") and any short hash
// ("v1-f3798d7a" -> "v1"), then humanize the prefix ("champion_v0" -> "Champion v0"). Never shows the
// raw internal index to the operator (the raw id is still the value sent to /api/kpis).
function versionLabel(raw: string): string {
  let base = raw.split('__')[0];
  base = base.split('-')[0];
  if (base.startsWith('champion_')) return `Champion ${base.slice('champion_'.length)}`;
  return base;
}

function BarRow({
  label,
  value,
  max,
  color,
  asPct,
}: {
  label: string;
  value: number;
  max?: number;
  color?: string;
  asPct?: boolean;
}) {
  const width = asPct ? value * 100 : max ? (value / max) * 100 : value;
  return (
    <div style={{ marginBottom: 11 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 5 }}>
        <span style={{ fontSize: 12.5, color: 'var(--text-2)', fontWeight: 550 }}>{label}</span>
        <span className="mono" style={{ fontSize: 12, fontWeight: 650 }}>
          {asPct ? pct(value) : value}
        </span>
      </div>
      <div className="bar" style={{ height: 8 }}>
        <i style={{ width: `${Math.min(100, width)}%`, background: color ?? 'var(--accent)' }} />
      </div>
    </div>
  );
}

export default function KpiPage() {
  const [mode, setMode] = useState<'overview' | 'compare'>('overview');
  // Default to the seeded cohort so real numbers render on first load (was a hardcoded "v12" that
  // matched no episodes). The selector is repopulated from /api/versions once it loads.
  const [version, setVersion] = useState(SEEDED_VERSION);
  const [cohort, setCohort] = useState('All');
  const [versionOptions, setVersionOptions] = useState<VersionOption[]>([
    { value: SEEDED_VERSION, label: versionLabel(SEEDED_VERSION) },
  ]);
  const [data, setData] = useState<KpiResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Populate the version selector from REAL lineage data. The seeded cohort (which actually has
  // episodes) is always kept in the list and stays the default; lineage versions are appended.
  useEffect(() => {
    let cancelled = false;
    fetchVersions()
      .then((res) => {
        if (cancelled) return;
        // De-dupe by the OPERATOR-FACING label, not the raw id: experiment-suffixed ids
        // ("champion_v0__…") collapse to the same "Champion v0" label, so keep the first (the seeded,
        // populated cohort) and drop the n=0 duplicates that would only render "No calls".
        const seenLabels = new Set<string>();
        const opts: VersionOption[] = [];
        const add = (raw: string) => {
          if (!raw) return;
          const label = versionLabel(raw);
          if (seenLabels.has(label)) return;
          seenLabels.add(label);
          opts.push({ value: raw, label });
        };
        add(SEEDED_VERSION); // guaranteed-populated default first
        for (const v of res.versions) add(v.version);
        setVersionOptions(opts);
      })
      .catch(() => {
        // Lineage fetch failing is non-fatal — keep the seeded default so the page still shows data.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Compare baseline: prefer a different real version than the current selection, else fall back to
  // the seeded cohort so the compare arm always queries a version that exists.
  const compareVersion = useMemo(() => {
    const other = versionOptions.find((o) => o.value !== version);
    return other?.value ?? SEEDED_VERSION;
  }, [versionOptions, version]);

  useEffect(() => {
    let cancelled = false;
    fetchKpis({
      version,
      cohort: cohort === 'All' ? undefined : cohort,
      compare_version: mode === 'compare' ? compareVersion : undefined,
    })
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load KPIs.');
      });
    return () => {
      cancelled = true;
    };
  }, [version, cohort, mode, compareVersion]);

  return (
    <div className="page">
      <div className="page-scroll scroll">
        <div className="pad">
          {/* controls */}
          <div className="row wrap" style={{ marginBottom: 16, gap: 12 }}>
            <div className="seg">
              <button className={mode === 'overview' ? 'on' : ''} onClick={() => setMode('overview')}>
                Overview
              </button>
              <button className={mode === 'compare' ? 'on' : ''} onClick={() => setMode('compare')}>
                Compare versions
              </button>
            </div>
            <div className="grow" />
            <select className="field" value={version} onChange={(e) => setVersion(e.target.value)}>
              {versionOptions.map((v) => (
                <option key={v.value} value={v.value}>
                  {v.label}
                </option>
              ))}
            </select>
            <select className="field" value={cohort} onChange={(e) => setCohort(e.target.value)}>
              {COHORTS.map((c) => (
                <option key={c} value={c}>
                  {c === 'All' ? 'All cohorts' : c}
                </option>
              ))}
            </select>
          </div>

          {error ? (
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="chart" size={28} />
                </div>
                <h3>Couldn&apos;t load KPIs</h3>
                <p>{error}</p>
              </div>
            </div>
          ) : !data ? (
            <div className="card">
              <div className="empty">
                <p className="muted">Loading metrics…</p>
              </div>
            </div>
          ) : data.n === 0 ? (
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="chart" size={28} />
                </div>
                <h3>No calls for this selection</h3>
                <p>No episodes match {versionLabel(version)} {cohort === 'All' ? '' : `· ${cohort}`}. Try another filter.</p>
              </div>
            </div>
          ) : mode === 'compare' && data.compare ? (
            <CompareTable data={data} />
          ) : (
            <Overview data={data} />
          )}
        </div>
      </div>
    </div>
  );
}

function Overview({ data }: { data: KpiResponse }) {
  const ladderPctOfMax = data.ladder_max ? data.weighted_ladder_score / data.ladder_max : 0;
  return (
    <>
      {/* primary tiles — the ladder headline AND the DISTINCT enrollment rate, side by side */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 14, marginBottom: 16 }}>
        <div className="kpi">
          <div className="k-lbl">
            <Icon name="sigma" size={14} />
            Weighted-ladder score
          </div>
          <div className="k-val">
            {data.weighted_ladder_score.toFixed(2)}
            <span className="u">/ {data.ladder_max}</span>
          </div>
          <div className="k-delta flat">→ mean commitment rung</div>
          <div className="k-spark">
            <Spark data={[ladderPctOfMax * 0.8, ladderPctOfMax * 0.9, ladderPctOfMax]} w={70} h={28} />
          </div>
        </div>
        <div className="kpi">
          <div className="k-lbl">
            <Icon name="bolt" size={14} />
            Same-call enrollment
          </div>
          <div className="k-val">
            {Math.round(data.enrollment_rate * 100)}
            <span className="u">%</span>
          </div>
          <div className="k-delta flat">→ closed at enrollment</div>
          <div className="k-spark">
            <Spark
              data={[data.enrollment_rate * 0.8, data.enrollment_rate * 0.9, data.enrollment_rate]}
              w={70}
              h={28}
              color="var(--ok)"
            />
          </div>
        </div>
        <div className="kpi">
          <div className="k-lbl">
            <Icon name="target" size={14} />
            Qualification accuracy
          </div>
          <div className="k-val">
            {Math.round(data.qualification_accuracy * 100)}
            <span className="u">%</span>
          </div>
          <div className="k-delta flat">→ qualified correctly</div>
        </div>
        <div className="kpi">
          <div className="k-lbl">
            <Icon name="shield" size={14} />
            Escalation rate
          </div>
          <div className="k-val">
            {(data.escalation_rate * 100).toFixed(1)}
            <span className="u">%</span>
          </div>
          <div className="k-delta flat">→ flagged for a human</div>
        </div>
      </div>

      {/* mid panels — ladder distribution + archetype conversion */}
      <div style={{ display: 'grid', gridTemplateColumns: '1.1fr 1fr', gap: 14, marginBottom: 16 }}>
        <div className="card">
          <div className="card-head">
            <Icon name="sigma" size={16} style={{ color: 'var(--accent)' }} />
            <h3>Outcome ladder distribution</h3>
            <span className="sub" style={{ marginLeft: 'auto' }}>
              weighted score {data.weighted_ladder_score.toFixed(2)} / {data.ladder_max}
            </span>
          </div>
          <div className="card-pad">
            {data.ladder_distribution.map((d) => (
              <BarRow
                key={d.tier}
                label={d.label}
                value={d.rate}
                asPct
                color={d.tier >= 4 ? 'var(--ok)' : d.tier >= 2 ? 'var(--accent)' : d.tier >= 1 ? 'var(--info)' : 'var(--text-3)'}
              />
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-head">
            <Icon name="user" size={16} style={{ color: 'var(--accent)' }} />
            <h3>Conversion by archetype</h3>
          </div>
          <div className="card-pad">
            {data.archetype_conversion.length === 0 ? (
              <p className="faint" style={{ fontSize: 12 }}>
                No archetype data for this selection.
              </p>
            ) : (
              data.archetype_conversion.map((a) => (
                <BarRow
                  key={a.label}
                  label={`${a.label} · ${a.n}`}
                  value={a.conversion}
                  asPct
                  color={a.conversion < 0.4 ? 'var(--danger)' : a.conversion < 0.65 ? 'var(--warn)' : 'var(--ok)'}
                />
              ))
            )}
          </div>
        </div>
      </div>
    </>
  );
}

function CompareTable({ data }: { data: KpiResponse }) {
  const c = data.compare!;
  // Show clean, human-readable version labels (no internal hash suffix) for both arms.
  const baselineLabel = data.compare_version ? versionLabel(data.compare_version) : 'baseline';
  const championLabel = data.version ? versionLabel(data.version) : 'champion';
  const rows: [string, string, string, string, 'up' | 'down' | 'flat'][] = [
    [
      'Weighted-ladder score',
      c.baseline_ladder_score.toFixed(2),
      c.champion_ladder_score.toFixed(2),
      `${c.delta >= 0 ? '+' : ''}${c.delta.toFixed(2)}`,
      c.delta > 0 ? 'up' : c.delta < 0 ? 'down' : 'flat',
    ],
    [
      'Same-call enrollment',
      pct(c.baseline_enrollment_rate),
      pct(c.champion_enrollment_rate),
      `${c.champion_enrollment_rate - c.baseline_enrollment_rate >= 0 ? '+' : ''}${Math.round(
        (c.champion_enrollment_rate - c.baseline_enrollment_rate) * 100,
      )} pts`,
      c.champion_enrollment_rate > c.baseline_enrollment_rate ? 'up' : c.champion_enrollment_rate < c.baseline_enrollment_rate ? 'down' : 'flat',
    ],
  ];
  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="sec-head">
        <h2>Champion vs. baseline</h2>
        <span className="sub">
          {baselineLabel} (baseline) vs {championLabel} (champion) · n {c.n_baseline} / {c.n_champion}
          {' · '}
          {c.challenger_better ? 'real lift (CI excludes 0)' : 'lift not separated from noise'}
        </span>
      </div>
      <div className="card" style={{ padding: '6px 8px' }}>
        <table className="tbl">
          <thead>
            <tr>
              <th>Metric</th>
              <th className="num">{baselineLabel} (baseline)</th>
              <th className="num">{championLabel} (champion)</th>
              <th className="num">Δ</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r[0]} style={{ cursor: 'default' }}>
                <td className="b">{r[0]}</td>
                <td className="num muted mono">{r[1]}</td>
                <td className="num mono b">{r[2]}</td>
                <td className="num">
                  <span
                    className={`tag ${r[4] === 'up' ? 'ok' : r[4] === 'down' ? 'danger' : ''}`}
                    style={{ fontVariantNumeric: 'tabular-nums' }}
                  >
                    {r[3]}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

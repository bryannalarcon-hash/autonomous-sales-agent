// P4 — KPI Views (U15). Performance of a version/cohort, computed from real episodes via the backend
// (grading.kpi_score + compare_versions). The two HEADLINE tiles are deliberately DISTINCT numbers:
// the weighted commitment-ladder score (mean ladder rung, on a 0..ladder_max scale) AND the same-
// call enrollment rate (share of calls that closed at full enrollment, a 0..100% number) — a config
// can lift one without the other, so the operator sees both. Version + cohort selectors filter the
// set; "Compare versions" adds a baseline arm with a bootstrap-CI delta. Secondary panels: ladder-
// tier distribution + per-archetype conversion. All tier/outcome labels are pre-translated. The
// archetype panel humanizes raw persona slugs (lib/labels.archetypeLabel) and filters out NON-
// archetype rows (the agent's own name "Alex", and "Unknown"/empty). The same-call enrollment tile
// uses rateValue() so a small-but-nonzero rate (≈0.03%) reads "<0.1"/one-decimal, never a flat "0",
// and carries an "n of N calls" sublabel so a true 0% reads as a measured rare event, not an empty
// panel. Small-sample cohorts (n < SMALL_SAMPLE_N) — e.g. selecting vB at n=1 — show a muted
// "n=N — directional only" caveat above the headline tiles so an extreme rate (a "100%" on n=1)
// doesn't read like a settled stat; the numbers are NOT hidden, just qualified. The archetype panel
// is titled "Same-call enrollment by archetype" and, when every archetype
// is at 0% (enrollment is genuinely rare for the seeded champion), leads with the meaningful NON-zero
// signal — the commitment-reached rate (ladder tier >= 2) — so it's informative, not a wall of 0%.
//
// The version selector is populated ONLY with versions that ACTUALLY have episodes: a page of
// episodes is fetched and their `version` tags are grouped by operator label (deriveVersionOptions),
// so every option resolves to real calls — NOT the /api/versions lineage ids ("v1-0dbf7bff"), which
// tag zero episodes and would render "No calls for this selection". It DEFAULTS to champion_v0 (the
// only always-populated cohort). Hash suffixes on raw ids are an internal index and are NOT rendered.
// The COMPARE baseline reuses that same small, episode-volume-ordered option set and probes AT MOST 2
// of the most-populated non-selected candidates (not every vA-*/vB-* — which was a ~134-request
// storm), so entering Compare costs a handful of requests and still yields a baseline with real n.
'use client';

import { useEffect, useState } from 'react';
import { Icon } from '@/components/cadence/Icon';
import { Spark } from '@/components/cadence/Spark';
import { fetchKpis, fetchEpisodes } from '@/lib/operate-api';
import type { KpiResponse } from '@/lib/operate-types';
import { archetypeLabel, populationLabel, versionLabel } from '@/lib/labels';

// The cohort the seeded episodes are actually tagged with — the only selection guaranteed to have
// episodes on a fresh backend, so it is the default and is always present in the selector.
const SEEDED_VERSION = 'champion_v0';
// Cohort options are { value, label }: `value` is the raw slug sent to /api/kpis (NEVER rendered),
// `label` is the operator-facing text (populationLabel: "held_out" -> "Held-out"). The no-internal-
// index rule — a raw snake_case cohort slug must not surface in the dropdown or the empty-state line.
const COHORTS: { value: string; label: string }[] = [
  { value: 'All', label: 'All cohorts' },
  { value: 'held_out', label: populationLabel('held_out') },
  { value: 'training', label: populationLabel('training') },
  { value: 'live', label: populationLabel('live') },
];

// Below this many calls a cohort's rates are too thin to be a real statistic (a single call makes a
// flat "100%"/"0%" that reads like a settled result). At/under this we surface a muted "directional
// only" caveat next to the headline numbers — we DON'T hide the numbers, just qualify them.
const SMALL_SAMPLE_N = 10;

function pct(v: number): string {
  return `${Math.round(v * 100)}%`;
}

// Integer with thousands separators ("2465" -> "2,465"), for the small "n of N calls" sublabels.
function nfmt(v: number): string {
  return Math.round(v).toLocaleString('en-US');
}

// The number-only part of a SMALL rate where a flat "0" would read as no-data (the headline tile
// renders this big, with a separate "%" unit span). A true zero stays "0", a tiny positive (< 0.1%)
// shows "<0.1", and anything below 10% keeps one decimal (so ≈0.03% isn't shown as 0).
function rateValue(v: number): string {
  if (v <= 0) return '0';
  const p = v * 100;
  if (p < 0.1) return '<0.1';
  if (p < 10) return p.toFixed(1);
  return String(Math.round(p));
}

// Personas that are NOT real prospect archetypes and must not appear in the archetype panel: the
// agent's own persona name (mirrors DashboardShell's PERSONA_NAME) and the null/empty fallback the
// backend labels "Unknown". Compared case-insensitively.
const NON_ARCHETYPE_LABELS = new Set(['alex', 'unknown', '']);
function isProspectArchetype(label: string): boolean {
  return !NON_ARCHETYPE_LABELS.has(label.trim().toLowerCase());
}

// One selectable version: the raw id sent to the API + the operator-facing label. The hash suffix on
// a raw id (e.g. "v1-f3798d7a") is an internal index, so the label strips it; the champion is marked.
interface VersionOption {
  value: string; // raw version id passed to /api/kpis (a representative that HAS episodes)
  label: string; // human-readable label shown in the dropdown (no internal hash)
  count: number; // # episodes under this label (used to order + pick a compare baseline)
}

// Derive the selectable version set from versions episodes are ACTUALLY tagged with — NOT the
// /api/versions lineage (its ids like "v1-0dbf7bff" tag zero episodes, so they'd be dead-end options).
// Episodes are tagged "champion_v0" / "vA-<hash>" / "vB-<hash>" etc.; many raw ids collapse to one
// operator label via versionLabel ("vA-…"→"vA"). We group by that label, pick the MOST-populated raw
// id per label as the value (so /api/kpis?version=<value> always returns n>0), and carry the total
// episode count under the label for ordering + cheap baseline selection. Result: a handful of options,
// every one of which yields calls (no "No calls for this selection"), ordered by real volume.
function deriveVersionOptions(versions: string[]): VersionOption[] {
  const byLabel = new Map<string, { value: string; valueCount: number; total: number }>();
  const rawCounts = new Map<string, number>();
  for (const v of versions) if (v) rawCounts.set(v, (rawCounts.get(v) ?? 0) + 1);
  for (const [raw, c] of rawCounts) {
    const label = versionLabel(raw);
    const cur = byLabel.get(label);
    if (!cur) {
      byLabel.set(label, { value: raw, valueCount: c, total: c });
    } else {
      cur.total += c;
      // Keep the most-populated raw id as the representative (its own /api/kpis n is guaranteed > 0).
      if (c > cur.valueCount) {
        cur.value = raw;
        cur.valueCount = c;
      }
    }
  }
  return [...byLabel.entries()]
    .map(([label, e]) => ({ value: e.value, label, count: e.total }))
    .sort((a, b) => b.count - a.count);
}

// versionLabel (strip the `__…__` experiment suffix + short `-hash`, humanize "champion_v0" ->
// "Champion v0") is imported from lib/labels — never shows the raw internal index to the operator
// (the raw id is still the value sent to /api/kpis).

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
    { value: SEEDED_VERSION, label: versionLabel(SEEDED_VERSION), count: 0 },
  ]);
  const [data, setData] = useState<KpiResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Populate the version selector ONLY with versions that ACTUALLY have episodes. We fetch one page
  // of episodes and group their `version` tags by operator label (deriveVersionOptions), so every
  // option resolves to real calls — no dead-end "v1-0dbf7bff" lineage ids that tag zero episodes and
  // render "No calls for this selection". The seeded cohort (champion_v0) is guaranteed present and
  // stays the default; if the probe somehow returns nothing, the seeded default is kept as a fallback.
  useEffect(() => {
    let cancelled = false;
    fetchEpisodes({ limit: '2000' })
      .then((res) => {
        if (cancelled) return;
        const opts = deriveVersionOptions(res.episodes.map((e) => e.version));
        // Guarantee the seeded, always-populated default is present (and first) even if it didn't
        // surface in this page — it is the only selection guaranteed to have episodes on a fresh load.
        const hasSeeded = opts.some((o) => o.value === SEEDED_VERSION);
        const final = hasSeeded
          ? opts
          : [{ value: SEEDED_VERSION, label: versionLabel(SEEDED_VERSION), count: 0 }, ...opts];
        if (final.length > 0) setVersionOptions(final);
      })
      .catch(() => {
        // Episode probe failing is non-fatal — keep the seeded default so the page still shows data.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Compare baseline: a version that ACTUALLY has episodes (not just any different option). It must be
  // CHEAP to pick — an earlier build probed EVERY vA-*/vB-* candidate with its own /api/kpis call
  // (~134 requests on entering Compare). Instead we reuse the SAME small, episode-derived option set
  // as the selector (already ordered by real episode volume), take the most-populated NON-selected
  // candidates, and probe AT MOST 2 of them to confirm n>0 under the current cohort filter. So
  // entering Compare costs a handful of requests, not dozens, and still resolves to a baseline with
  // real n. Until the probe resolves we hold null (compare mode shows a "finding a baseline…" state
  // rather than a misleading n=0 arm).
  const MAX_BASELINE_PROBES = 2;
  const [compareVersion, setCompareVersion] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function pickBaseline() {
      // Candidates = the option set MINUS the current selection (dedup by raw value), already sorted
      // most-populated-first by deriveVersionOptions. Probe only the top few — these are the only
      // versions known to have episodes, so a populated baseline is found in 1–2 requests.
      const candidates = versionOptions.map((o) => o.value).filter((v) => v !== version);
      const cohortParam = cohort === 'All' ? undefined : cohort;
      let picked: string | null = null;
      for (const cand of candidates.slice(0, MAX_BASELINE_PROBES)) {
        try {
          const k = await fetchKpis({ version: cand, cohort: cohortParam });
          if (cancelled) return;
          if (k.n > 0) {
            picked = cand;
            break;
          }
        } catch {
          if (cancelled) return;
          // A failed probe just moves on to the next candidate.
        }
      }
      if (cancelled) return;
      // Fall back to the most-populated non-selected candidate even if its cohort-filtered probe was
      // empty (still a real selection), else the seeded cohort, else null (no other version exists).
      setCompareVersion(picked ?? candidates[0] ?? (version === SEEDED_VERSION ? null : SEEDED_VERSION));
    }

    void pickBaseline();
    return () => {
      cancelled = true;
    };
  }, [versionOptions, version, cohort]);

  useEffect(() => {
    let cancelled = false;
    fetchKpis({
      version,
      cohort: cohort === 'All' ? undefined : cohort,
      compare_version: mode === 'compare' && compareVersion ? compareVersion : undefined,
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
                <option key={c.value} value={c.value}>
                  {c.label}
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
                <p>No episodes match {versionLabel(version)} {cohort === 'All' ? '' : `· ${populationLabel(cohort)}`}. Try another filter.</p>
              </div>
            </div>
          ) : mode === 'compare' && data.compare ? (
            <CompareTable data={data} />
          ) : mode === 'compare' && !compareVersion ? (
            <div className="card">
              <div className="empty">
                <p className="muted">Finding a baseline version with episodes…</p>
              </div>
            </div>
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
  // Real prospect archetypes only — drop the agent persona name ("Alex") and the "Unknown"/empty
  // fallback, then humanize the slug for display ("anxious_parent" -> "Anxious parent").
  const archetypes = data.archetype_conversion.filter((a) => isProspectArchetype(a.label));

  // Same-call enrollment is a RARE terminal event (full enrollment closed on the call). For the
  // seeded champion it is genuinely 0 — calls land at "Consultation booked", not enrollment. A flat
  // "0%" reads as a broken/empty panel, so we surface (1) the raw count behind it ("0 of N calls")
  // and (2) the meaningful, NON-zero signal that IS in the data: the share of calls that reached a
  // commitment (consultation+, ladder tier >= 2), derived from the labeled ladder distribution.
  const enrolledCount = Math.round(data.enrollment_rate * data.n);
  const commitmentRate = data.ladder_distribution
    .filter((d) => d.tier >= 2)
    .reduce((sum, d) => sum + d.rate, 0);
  const allArchetypesZero = archetypes.length > 0 && archetypes.every((a) => a.conversion <= 0);
  // A thin cohort (e.g. vB at n=1): qualify the headline rates as directional rather than letting an
  // extreme "100%"/"0%" read as a settled statistic. The numbers still render below.
  const smallSample = data.n < SMALL_SAMPLE_N;
  return (
    <>
      {/* Small-sample caveat — sits directly above the headline rates so an n=1 "100%" reads as
          directional, not settled. Muted on purpose; the real numbers are still shown below. */}
      {smallSample ? (
        <div
          className="row"
          style={{
            gap: 8,
            alignItems: 'center',
            marginBottom: 12,
            padding: '8px 12px',
            borderRadius: 10,
            background: 'var(--surface-2, var(--surface))',
            border: '1px solid var(--border)',
          }}
        >
          <Icon name="alert" size={14} style={{ color: 'var(--text-3)' }} />
          <span className="muted" style={{ fontSize: 12.5 }}>
            n={data.n} — directional only. Rates on so few calls aren&apos;t a stable statistic; treat
            them as a signal, not a measurement.
          </span>
        </div>
      ) : null}

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
            {rateValue(data.enrollment_rate)}
            <span className="u">%</span>
          </div>
          {/* The raw count behind the rate, so a true 0% reads as a measured rare event ("0 of N
              calls closed at enrollment"), not a broken/empty panel. */}
          <div className="k-delta flat">
            → {nfmt(enrolledCount)} of {nfmt(data.n)} calls closed at enrollment
          </div>
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
            <h3>Same-call enrollment by archetype</h3>
            <span className="sub" style={{ marginLeft: 'auto' }}>
              {pct(commitmentRate)} reached a commitment
            </span>
          </div>
          <div className="card-pad">
            {archetypes.length === 0 ? (
              <p className="faint" style={{ fontSize: 12 }}>
                No archetype data for this selection.
              </p>
            ) : (
              <>
                {/* When every archetype sits at 0% same-call enrollment (the rare terminal event),
                    a column of empty bars looks broken. Lead with the meaningful, NON-zero signal —
                    the share of calls that reached a commitment (consultation+) — so the panel is
                    informative; the per-archetype bars below still show each cohort's call volume. */}
                {allArchetypesZero ? (
                  <p className="faint" style={{ fontSize: 12, marginBottom: 12, lineHeight: 1.5 }}>
                    No calls closed at full enrollment on this selection — calls land at a
                    consultation/callback. <b className="mono">{pct(commitmentRate)}</b> reached a
                    commitment overall. Per-archetype call volume:
                  </p>
                ) : null}
                {archetypes.map((a) => (
                  <BarRow
                    key={a.label}
                    label={`${archetypeLabel(a.label)} · ${nfmt(a.n)} calls`}
                    value={a.conversion}
                    asPct
                    color={a.conversion < 0.4 ? 'var(--danger)' : a.conversion < 0.65 ? 'var(--warn)' : 'var(--ok)'}
                  />
                ))}
              </>
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

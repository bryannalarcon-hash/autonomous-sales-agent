// P3 — Calls List (U15). Browsable/filterable index of COMPLETED episodes (the /api/episodes
// contract excludes in-progress / 0-turn live calls, so no still-running call leaks in as a finished
// row — live calls live on /operate/live): filters by version, cohort, and
// outcome (a labeled segmented control that maps each option back to its internal outcome KEY before
// the API call — the key is never shown), plus an "Escalated only" toggle and a free-text search
// over the call id. The table shows call id, outcome (dot-tag), ladder tier (labeled), duration,
// version and when; a row click opens a right-side drawer with the call's facts + "Belief at close"
// and a primary "Open full call review" link to /operate/review/{id}. Outcome/ladder labels are
// pre-translated by the backend; the raw persona archetype + version slugs are humanized client-side
// via @/lib/labels (archetypeLabel/versionLabel), and the raw call id is muted/secondary — never a
// headline (internal indices must not render as operator-facing labels).
// CB-45: the drawer also shows the call's average time-to-first-word (from EpisodeSummary
// avg_first_token_ms via fmtMsShort), omitted when the call has no voice timing.
// CB-60: (1) seeded/sim stub rows get a visible "Seeded sample" badge (is_stub from the API) and are
// excluded from "Real calls" by default via the cohort='live' filter (reachable via All cohorts);
// (2) when the API's `total` exceeds the displayed `count`, a "showing N of total" disclosure appears
// in the filter bar so operators know the list is capped (prevents mistaking 200 for "All calls").
// CB-66 (item 1): column headers are now clickable sort controls. Clicking a header sorts the loaded
// rows client-side by that column; clicking again toggles direction. An arrow indicator shows the
// active sort direction. Sortable columns: CALL (by episode_id), OUTCOME, DURATION, WHEN.
// CB-66 (item 2): OUTCOME_OPTIONS now includes all filterable outcomes from the backend enum — no
// outcome present in real data can be absent from the filter chips. The "callback_booked" chip now
// matches both "callback_booked" AND the selfplay alias "callback_scheduled" (the backend normalizes
// outcome_key to the canonical form before serializing, so the chip drives the correct key).
// CB-66 (item 6): the ARCHETYPE column now shows the prospect archetype tag; since the CALL column
// already leads with the archetype label as its primary text, the ARCHETYPE column is REMOVED to
// avoid duplication — the information is still present in the row's primary label and the drawer.
// CB-66 (item 8): count strings are properly pluralized ("1 call" not "1 calls").
'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchEpisodes, fmtDuration, fmtMsShort, fmtTimeAgo } from '@/lib/operate-api';
import { archetypeLabel, kbVersionLabel, versionLabel } from '@/lib/labels';
import type { EpisodeSummary } from '@/lib/operate-types';

// CB-66 (item 1): sortable column keys for the Calls table.
type SortKey = 'call' | 'outcome' | 'duration' | 'when';
type SortDir = 'asc' | 'desc';

/** Sortable column header: shows an up/down arrow when this column is active. */
function SortTh({
  label,
  col,
  active,
  dir,
  onSort,
  align,
}: {
  label: string;
  col: SortKey;
  active: SortKey | null;
  dir: SortDir;
  onSort: (k: SortKey) => void;
  align?: 'num';
}) {
  const isActive = active === col;
  return (
    <th
      className={align === 'num' ? 'num' : undefined}
      onClick={() => onSort(col)}
      title={`Sort by ${label}`}
      style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap' }}
      aria-sort={isActive ? (dir === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      {label}
      {' '}
      <span
        aria-hidden
        style={{
          fontSize: 10,
          opacity: isActive ? 0.85 : 0.3,
          fontWeight: 700,
          verticalAlign: 'middle',
        }}
      >
        {isActive ? (dir === 'asc' ? '▲' : '▼') : '⇅'}
      </span>
    </th>
  );
}

// Outcome filter options: the displayed label + the internal outcome KEY it maps to ('' = no filter).
// CB-66 (item 2): derives from the COMPLETE enum (matching src/api/labels.FILTERABLE_OUTCOME_KEYS)
// so no outcome present in real data is ever missing. "Callback booked" covers both the real-call
// path ("callback_booked") and the selfplay alias ("callback_scheduled") — the backend normalizes
// outcome_key to "callback_booked" for both so the filter chip always works correctly.
const OUTCOME_OPTIONS: { label: string; key: string }[] = [
  { label: 'All', key: '' },
  { label: 'Enrolled', key: 'enrolled' },
  { label: 'Trial booked', key: 'trial_booked' },
  { label: 'Consultation booked', key: 'consult_booked' },
  { label: 'Callback booked', key: 'callback_booked' },
  { label: 'Booked', key: 'booked' },
  { label: 'Interested', key: 'interested' },
  { label: 'Released', key: 'released' },
  { label: 'Abandoned', key: 'abandoned' },
  { label: 'No interest', key: 'no_interest' },
  { label: 'Walked away', key: 'walked' },
  { label: 'Disqualified', key: 'disqualified' },
  { label: 'Escalated', key: 'escalated' },
];

// Outcome key -> dot-tag color class (status semantics), for the table + drawer.
const OUTCOME_STYLE: Record<string, string> = {
  enrolled: 'ok',
  booked: 'accent',
  in_progress: 'info',
  escalated: 'warn',
  disqualified: 'danger',
  no_interest: '',
  interested: 'info',
};

function outcomeClass(key: string | null): string {
  return key ? OUTCOME_STYLE[key] ?? '' : '';
}

function Drawer({ c, onClose }: { c: EpisodeSummary; onClose: () => void }) {
  const router = useRouter();
  const cls = outcomeClass(c.outcome_key);
  // CB-45: the call's mean time-to-first-word (avg_first_token_ms), shown as a human duration. Only
  // included when the call carries voice timing (fmtMsShort('' on null) — a text/legacy call omits it
  // rather than showing a placeholder).
  const firstWord = fmtMsShort(c.avg_first_token_ms);
  const facts: [string, string][] = [
    ['Duration', fmtDuration(c.duration_ms)],
    ['Channel', c.channel === 'voice' ? 'Web-voice' : c.channel],
    ['When', fmtTimeAgo(c.created_at)],
    ['Qualified', c.qualified == null ? '—' : c.qualified ? 'Yes' : 'No'],
    ['Ladder tier', c.ladder_label],
    ...(firstWord ? ([['Time to first word', firstWord]] as [string, string][]) : []),
    ['Call ID', c.episode_id],
  ];
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="drawer">
        <div className="card-head" style={{ background: 'transparent' }}>
          <div className="avatar">{c.episode_id.replace(/[^0-9]/g, '').slice(-2) || 'CA'}</div>
          <div className="grow">
            {/* Lead with the humanized archetype (+ outcome) as the primary label; the raw call id is
                an internal index, so it's demoted to a muted mono secondary line (never the headline). */}
            <div className="b" style={{ fontSize: 15, fontFamily: 'var(--font-display)' }}>
              {c.persona ? archetypeLabel(c.persona) : 'Unknown archetype'} · {c.outcome}
            </div>
            <div className="muted mono" style={{ fontSize: 11.5 }}>
              #{c.episode_id}
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
            <span className={`tag dot ${cls}`} style={{ fontSize: 12, padding: '5px 11px' }}>
              {c.outcome}
            </span>
            <span className="tag">{c.ladder_label}</span>
            <span className="tag accent">
              {versionLabel(c.version)} · {kbVersionLabel(c.kb_version)}
            </span>
            <span className="tag">{c.channel === 'voice' ? 'Web-voice' : c.channel}</span>
            {c.escalated ? (
              <span className="tag warn">
                <Icon name="alert" size={12} />
                Escalated
              </span>
            ) : null}
            {/* CB-60: stub badge in the drawer so the operator can see it when reviewing a
                seeded call that appeared via All cohorts or an escalation link. */}
            {c.is_stub ? (
              <span className="tag" style={{ opacity: 0.7 }}>
                Seeded sample
              </span>
            ) : null}
          </div>
          <div
            className="card solid card-pad"
            style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 }}
          >
            {facts.map(([l, v]) => (
              <div key={l}>
                <div
                  className="faint"
                  style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em' }}
                >
                  {l}
                </div>
                <div className="b" style={{ fontSize: 13.5, marginTop: 3 }}>{v}</div>
              </div>
            ))}
          </div>
          <button className="btn btn-primary" onClick={() => router.push(`/operate/review/${c.episode_id}`)}>
            <Icon name="eye" size={16} />
            Open full call review
            <Icon name="arrowR" size={15} />
          </button>
        </div>
      </div>
    </>
  );
}

export default function CallsPage() {
  const [q, setQ] = useState('');
  const [outcome, setOutcome] = useState(''); // internal key ('' = All)
  const [esc, setEsc] = useState(false);
  // Scope the operator Calls log to REAL calls (cohort='live'). Experiment/eval cohorts
  // (held_out / training / sim self-play / per-run coh-*) are persisted into the same DB and would
  // otherwise flood the log with short synthetic episodes — hidden by default, toggleable.
  const [liveOnly, setLiveOnly] = useState(true);
  const [rows, setRows] = useState<EpisodeSummary[]>([]);
  // CB-60: total = completed rows matching the filter before the 200-row page cap.
  // When total > rows.length the list is capped and we show "showing N of total".
  const [total, setTotal] = useState<number | null>(null);
  const [sel, setSel] = useState<EpisodeSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  // CB-66 (item 1): client-side column sort state. sortKey=null means default (API order = newest-first).
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  useEffect(() => {
    let cancelled = false;
    fetchEpisodes({
      cohort: liveOnly ? 'live' : undefined,
      outcome: outcome || undefined,
      escalated: esc ? 'true' : undefined,
      limit: '200',
    })
      .then((res) => {
        if (!cancelled) {
          setRows(res.episodes);
          setTotal(res.total ?? res.count);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load calls.');
      });
    return () => {
      cancelled = true;
    };
  }, [outcome, esc, liveOnly]);

  // CB-66 (item 1): toggle sort column; clicking the same column flips direction.
  function handleSort(key: SortKey) {
    setSortKey((prev) => {
      if (prev === key) {
        setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
        return key;
      }
      setSortDir('asc');
      return key;
    });
  }

  // Free-text search is client-side over the loaded page (call id).
  // CB-66 (item 1): apply sort on top of the search filter.
  const visible = useMemo(() => {
    const filtered = rows.filter((c) => !q || c.episode_id.toLowerCase().includes(q.toLowerCase()));
    if (!sortKey) return filtered;
    return [...filtered].sort((a, b) => {
      let cmp = 0;
      if (sortKey === 'call') {
        cmp = a.episode_id.localeCompare(b.episode_id);
      } else if (sortKey === 'outcome') {
        cmp = (a.outcome ?? '').localeCompare(b.outcome ?? '');
      } else if (sortKey === 'duration') {
        cmp = (a.duration_ms ?? -1) - (b.duration_ms ?? -1);
      } else if (sortKey === 'when') {
        cmp = (a.created_at ?? '').localeCompare(b.created_at ?? '');
      }
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [rows, q, sortKey, sortDir]);

  return (
    <div className="page">
      <div className="page-scroll scroll">
        <div className="pad">
          {/* filter bar */}
          <div className="row wrap" style={{ gap: 10, marginBottom: 16 }}>
            <div className="row" style={{ position: 'relative' }}>
              <Icon name="search" size={15} style={{ position: 'absolute', left: 11, color: 'var(--text-3)' }} />
              <input
                className="input"
                placeholder="Search call ID…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                style={{ paddingLeft: 32, width: 240 }}
              />
            </div>
            <div className="seg">
              {OUTCOME_OPTIONS.map((o) => (
                <button key={o.label} className={outcome === o.key ? 'on' : ''} onClick={() => setOutcome(o.key)}>
                  {o.label}
                </button>
              ))}
            </div>
            <button
              className="gctl"
              onClick={() => setEsc((v) => !v)}
              style={esc ? { borderColor: 'var(--warn-border)', background: 'var(--warn-soft)', color: 'var(--warn-strong)' } : undefined}
            >
              <Icon name="alert" size={15} />
              Escalated only
            </button>
            <button
              className="gctl"
              onClick={() => setLiveOnly((v) => !v)}
              title={liveOnly
                ? 'Showing real calls only (live). Click to include sim / experiment / eval calls.'
                : 'Showing ALL cohorts (incl. sim, training, held-out eval). Click to show real calls only.'}
              style={liveOnly ? { borderColor: 'var(--accent-border)', background: 'var(--accent-soft)', color: 'var(--accent-strong)' } : undefined}
            >
              <Icon name="phone" size={15} />
              {liveOnly ? 'Real calls' : 'All cohorts'}
            </button>
            <div className="grow" />
            {/* CB-60: population disclosure. When the page is capped (200 row limit), show
                "showing N of total" so operators know they're seeing a slice, not all calls.
                `visible` is the client-side id-search filter on top of the already-loaded page. */}
            {/* CB-66 (item 8): properly pluralized count strings. */}
            {total !== null && total > rows.length ? (
              <span className="muted" style={{ fontSize: 12.5 }}>
                {visible.length} {visible.length === 1 ? 'call' : 'calls'}&nbsp;
                <span style={{ opacity: 0.5 }}>(showing {rows.length} of {total})</span>
              </span>
            ) : (
              <span className="muted" style={{ fontSize: 12.5 }}>
                {visible.length} {liveOnly
                  ? (visible.length === 1 ? 'real call' : 'real calls')
                  : (visible.length === 1 ? 'call' : 'calls')}
              </span>
            )}
            <button className="gctl">
              <Icon name="download" size={15} />
              Export
            </button>
          </div>

          {error ? (
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="alert" size={28} />
                </div>
                <h3>Couldn&apos;t load calls</h3>
                <p>{error}</p>
              </div>
            </div>
          ) : visible.length === 0 ? (
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="list" size={28} />
                </div>
                <h3>No calls match</h3>
                <p>Adjust the filters or search to see calls here.</p>
              </div>
            </div>
          ) : (
            <div className="card" style={{ padding: '14px 8px 6px' }}>
              <table className="tbl">
                <thead>
                  <tr>
                    {/* CB-66 (item 1): sortable headers; (item 6): ARCHETYPE column removed — the
                        archetype is already the primary label in the CALL cell so a separate column
                        would be a pure duplicate. The persona archetype stays accessible via CALL and
                        the row drawer. */}
                    <SortTh label="Call" col="call" active={sortKey} dir={sortDir} onSort={handleSort} />
                    <SortTh label="Outcome" col="outcome" active={sortKey} dir={sortDir} onSort={handleSort} />
                    <th>Ladder tier</th>
                    <SortTh label="Duration" col="duration" active={sortKey} dir={sortDir} onSort={handleSort} align="num" />
                    <th>Version</th>
                    <SortTh label="When" col="when" active={sortKey} dir={sortDir} onSort={handleSort} align="num" />
                  </tr>
                </thead>
                <tbody>
                  {visible.map((c) => (
                    <tr key={c.episode_id} onClick={() => setSel(c)}>
                      <td>
                        {/* The Call cell leads with the humanized archetype as the primary label; the raw
                            call id is an internal index, demoted to a muted mono secondary line (not the
                            headline). CB-66 (item 6): the separate ARCHETYPE column is removed since it
                            was an exact duplicate of this label — the info is here and in the drawer. */}
                        <div className="b" style={{ fontSize: 13 }}>
                          {c.persona ? archetypeLabel(c.persona) : 'Unknown archetype'}
                        </div>
                        <span className="mono muted" style={{ fontSize: 11 }}>
                          #{c.episode_id}
                        </span>
                      </td>
                      <td>
                        <span className={`tag dot ${outcomeClass(c.outcome_key)}`}>{c.outcome}</span>
                        {/* CB-60: seeded/sim stub badge — makes synthetic data visible when browsing
                            All cohorts. Hidden on Real calls (cohort='live' already excludes them). */}
                        {c.is_stub ? (
                          <span className="tag" style={{ marginLeft: 5, fontSize: 10.5, opacity: 0.7 }}>
                            Seeded sample
                          </span>
                        ) : null}
                      </td>
                      <td>
                        <span className="muted" style={{ fontSize: 12.5 }}>{c.ladder_label}</span>
                      </td>
                      <td className="num mono" style={{ fontSize: 12.5 }}>{fmtDuration(c.duration_ms)}</td>
                      <td>
                        <span className="tag accent">{versionLabel(c.version)}</span>
                      </td>
                      <td className="num muted" style={{ fontSize: 12 }}>{fmtTimeAgo(c.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
      {sel ? <Drawer c={sel} onClose={() => setSel(null)} /> : null}
    </div>
  );
}

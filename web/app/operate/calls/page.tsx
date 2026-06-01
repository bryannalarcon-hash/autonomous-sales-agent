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
'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchEpisodes, fmtDuration, fmtTimeAgo } from '@/lib/operate-api';
import { archetypeLabel, versionLabel } from '@/lib/labels';
import type { EpisodeSummary } from '@/lib/operate-types';

// Outcome filter options: the displayed label + the internal outcome KEY it maps to ('' = no filter).
// The key MUST equal a real `episode.outcome` value (the API filters `WHERE outcome = $key`); the
// labels mirror src/api/labels.outcome_label. The previous keys booked/disqualified/no_interest did
// not exist in the data, so those chips always returned 0 — replaced with the real outcomes.
const OUTCOME_OPTIONS: { label: string; key: string }[] = [
  { label: 'All', key: '' },
  { label: 'Enrolled', key: 'enrolled' },
  { label: 'Consultation booked', key: 'consult_booked' },
  { label: 'Escalated', key: 'escalated' },
  { label: 'Released', key: 'released' },
  { label: 'Walked away', key: 'walked' },
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
  const facts: [string, string][] = [
    ['Duration', fmtDuration(c.duration_ms)],
    ['Channel', c.channel === 'voice' ? 'Web-voice' : c.channel],
    ['When', fmtTimeAgo(c.created_at)],
    ['Qualified', c.qualified == null ? '—' : c.qualified ? 'Yes' : 'No'],
    ['Ladder tier', c.ladder_label],
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
              {versionLabel(c.version)} · {c.kb_version}
            </span>
            <span className="tag">{c.channel === 'voice' ? 'Web-voice' : c.channel}</span>
            {c.escalated ? (
              <span className="tag warn">
                <Icon name="alert" size={12} />
                Escalated
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
  const [rows, setRows] = useState<EpisodeSummary[]>([]);
  const [sel, setSel] = useState<EpisodeSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchEpisodes({
      outcome: outcome || undefined,
      escalated: esc ? 'true' : undefined,
      limit: '200',
    })
      .then((res) => {
        if (!cancelled) {
          setRows(res.episodes);
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load calls.');
      });
    return () => {
      cancelled = true;
    };
  }, [outcome, esc]);

  // Free-text search is client-side over the loaded page (call id).
  const visible = useMemo(
    () => rows.filter((c) => !q || c.episode_id.toLowerCase().includes(q.toLowerCase())),
    [rows, q],
  );

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
            <div className="grow" />
            <span className="muted" style={{ fontSize: 12.5 }}>{visible.length} calls</span>
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
                    <th>Call</th>
                    <th>Archetype</th>
                    <th>Outcome</th>
                    <th>Ladder tier</th>
                    <th className="num">Duration</th>
                    <th>Version</th>
                    <th className="num">When</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((c) => (
                    <tr key={c.episode_id} onClick={() => setSel(c)}>
                      <td>
                        {/* The Call cell leads with the humanized archetype as the primary label; the raw
                            call id is an internal index, demoted to a muted mono secondary line (not the
                            headline). The Archetype column repeats the human label as a filterable tag. */}
                        <div className="b" style={{ fontSize: 13 }}>
                          {c.persona ? archetypeLabel(c.persona) : 'Unknown archetype'}
                        </div>
                        <span className="mono muted" style={{ fontSize: 11 }}>
                          #{c.episode_id}
                        </span>
                      </td>
                      <td>
                        <span className="tag">{c.persona ? archetypeLabel(c.persona) : 'Unknown'}</span>
                      </td>
                      <td>
                        <span className={`tag dot ${outcomeClass(c.outcome_key)}`}>{c.outcome}</span>
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

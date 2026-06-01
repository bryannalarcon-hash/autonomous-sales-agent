// P8 — KB / Playbook browser (U16). Two-column screen (262px section tree + content panel) for
// browsing the agent's REAL grounded knowledge base: the facts/objection-rebuttals it grounds answers
// on, loaded from GET /api/kb (the kb_chunk corpus grouped by section). Clicking a section in the left
// tree renders THAT section's real chunks in the right panel — header = section label, body = each
// chunk's derived title + full grounding text. Counts in the tree equal what's shown (no placeholder
// text, no single hardcoded example for every section). This view is READ-ONLY: there is no in-page
// corpus editor, so it does NOT silently fork a draft (N7 fix — the old "Test edit as experiment"
// button POSTed /api/playbook on click, creating an empty draft of an edit never made). Instead the
// primary action routes to the Experiment Lab, where forking a DRAFT CHALLENGER is an explicit,
// intentional step (R20: experiments mutate config, NEVER the live champion). Degrades to an
// empty-state when the corpus is empty (or the API has not yet served the grouped shape).
// The section-tree header label is uppercased by .nav-group, but the kb_version token (kb_v0) is
// opted OUT of that transform so it renders lowercase (not the shouty "KB_V0").
'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon, type IconName } from '@/components/cadence/Icon';
import { fetchKb } from '@/lib/improve-api';
import type { KbResponse, KbSection } from '@/lib/improve-types';

// Per-section glyph for the left tree, keyed by the raw section slug (internal — never rendered as
// text). An unknown section falls back to the generic note glyph so a NEW corpus section still renders.
const SECTION_ICON: Record<string, IconName> = {
  objections: 'book',
  pricing: 'chart',
  programs: 'layers',
  policies: 'shield',
  competitors: 'target',
};

function sectionIcon(id: string): IconName {
  return SECTION_ICON[id] ?? 'note';
}

// The content panel: the selected section's REAL chunks (title + full grounding body). No hardcoded
// rule literals — everything renders from the live corpus, so each section shows its own content.
function SectionPanel({ section }: { section: KbSection | null }) {
  if (!section) {
    return (
      <div className="empty" style={{ padding: 32 }}>
        <span className="muted" style={{ fontSize: 13 }}>Select a section to browse its grounding facts.</span>
      </div>
    );
  }
  if (section.chunks.length === 0) {
    return (
      <div className="empty" style={{ padding: 32 }}>
        <span className="muted" style={{ fontSize: 13 }}>No grounding facts in “{section.label}” yet.</span>
      </div>
    );
  }
  return (
    <div className="col" style={{ gap: 12, maxWidth: 880 }}>
      {section.chunks.map((c) => (
        <div className="card" key={c.id}>
          <div className="card-head">
            <Icon name={sectionIcon(section.id)} size={15} />
            <h3 style={{ fontSize: 13 }}>{c.title}</h3>
          </div>
          <div className="card-pad">
            <p style={{ fontSize: 13, lineHeight: 1.6, color: 'var(--text-2)', margin: 0, whiteSpace: 'pre-wrap' }}>
              {c.text}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}

export default function KbPage() {
  const router = useRouter();
  const [kb, setKb] = useState<KbResponse | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchKb()
      .then((res) => {
        if (cancelled) return;
        setKb(res);
        // Default to the first section so the panel always shows real content on load.
        setActive((cur) => cur ?? res.sections[0]?.id ?? null);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load the knowledge base.');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const sections = kb?.sections ?? [];
  const activeSection = useMemo(
    () => sections.find((s) => s.id === active) ?? null,
    [sections, active],
  );

  return (
    <div className="page">
      <div style={{ flex: 1, minHeight: 0, display: 'grid', gridTemplateColumns: '262px minmax(0,1fr)' }}>
        {/* section tree — the REAL corpus sections with counts that match what's shown */}
        <div className="scroll" style={{ borderRight: '1px solid var(--border)', padding: 16, overflow: 'auto' }}>
          <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
            <span className="nav-group" style={{ margin: 0, padding: 0 }}>
              {/* The label is intentionally SHOUTY (.nav-group uppercases it), but the kb_version
                  token (`kb_v0`) is a lowercase id everywhere else — opt it out of the uppercase
                  transform so it doesn't render as "KB_V0". */}
              Knowledge base ·{' '}
              <span style={{ textTransform: 'none', letterSpacing: 'normal' }}>
                {kb?.kb_version ?? 'kb'}
              </span>
            </span>
          </div>
          <div className="col" style={{ gap: 3 }}>
            {sections.length ? (
              sections.map((s) => (
                <button
                  key={s.id}
                  className={`nav-item${active === s.id ? ' on' : ''}`}
                  onClick={() => setActive(s.id)}
                  style={{ fontSize: 13 }}
                >
                  <Icon name={sectionIcon(s.id)} size={16} />
                  <span className="grow" style={{ textAlign: 'left' }}>{s.label}</span>
                  <span className="faint" style={{ fontSize: 11 }}>{s.count}</span>
                </button>
              ))
            ) : (
              <span className="muted" style={{ fontSize: 12 }}>
                {error ? '—' : 'Loading corpus…'}
              </span>
            )}
          </div>
          <div className="card solid card-pad" style={{ marginTop: 16 }}>
            <div className="row" style={{ gap: 8, marginBottom: 6 }}>
              <Icon name="book" size={15} style={{ color: 'var(--accent)' }} />
              <span className="b" style={{ fontSize: 12.5 }}>Grounded corpus</span>
            </div>
            <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
              These are the real facts the agent grounds answers on
              {kb ? <> — <b className="mono">{kb.total_chunks}</b> across {sections.length} sections</> : null}.
              This is a read-only view. To trial a change, open the Experiment Lab and fork a draft
              challenger there — it won&rsquo;t affect live calls until promoted.
            </div>
          </div>
        </div>

        {/* content panel — renders the SELECTED section's real chunks */}
        <div className="col" style={{ minHeight: 0 }}>
          <div className="row" style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)', gap: 12 }}>
            <div>
              <div className="b" style={{ fontSize: 15, fontFamily: 'var(--font-display)' }}>
                {activeSection?.label ?? 'Knowledge base'}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {activeSection
                  ? `${activeSection.count} ${activeSection.count === 1 ? 'fact' : 'facts'} · the corpus the agent grounds on`
                  : 'Browse the agent’s grounding facts by section'}
              </div>
            </div>
            <div className="grow" />
            {/* Read-only browser: this does NOT fork a draft (that was an empty-edit fork, N7).
                It routes to the Experiment Lab, where forking a challenger is an explicit step. */}
            <button
              className="btn btn-primary btn-sm"
              onClick={() => router.push('/improve/lab')}
              title="Open the Experiment Lab to fork a draft challenger from the current playbook"
            >
              <Icon name="flask" size={14} />
              Open in Experiment Lab
            </button>
          </div>

          <div className="scroll" style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 18 }}>
            {error ? (
              <div className="card card-pad" style={{ borderColor: 'var(--danger-border)', background: 'var(--danger-soft)', marginBottom: 16 }}>
                <span style={{ fontSize: 12.5, color: 'var(--danger)' }}>{error}</span>
              </div>
            ) : null}
            <SectionPanel section={activeSection} />
          </div>
        </div>
      </div>
    </div>
  );
}

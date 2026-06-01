// P8 — KB / Playbook Editor (U16). Two-column editor (262px section tree + editor/diff) for the
// agent's playbook. The signature behavior: a SAVE forks a DRAFT CHALLENGER — it POSTs /api/playbook
// (or /api/kb), which builds a new experiment record via the loop's pure config mutators and NEVER
// mutates the live champion config (R20). A "Draft challenger" info card + a post-save banner make it
// explicit the edit is a draft, not a live change. The Edit view shows the live champion playbook
// (discovery sequence + the price rebuttal rule); the Diff view shows champion→draft. "Test as
// experiment" / Save both create the draft and link the operator to the Experiment Lab to watch it.
'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchPlaybook, savePlaybookDraft } from '@/lib/improve-api';
import type { Experiment, PlaybookResponse } from '@/lib/improve-types';

const SECTIONS: { id: string; t: string; n: number; lock?: boolean }[] = [
  { id: 'persona', t: 'Persona & tone', n: 4 },
  { id: 'discovery', t: 'Discovery flow', n: 7 },
  { id: 'objections', t: 'Objection rebuttals', n: 9 },
  { id: 'pricing', t: 'Pricing & concessions', n: 5, lock: true },
  { id: 'closing', t: 'Closing triggers', n: 6 },
  { id: 'escalation', t: 'Escalation rules', n: 4 },
  { id: 'compliance', t: 'Compliance guardrails', n: 8, lock: true },
];

// The unified diff shown in Diff view — the champion→draft rebuttal change (handoff §204).
const DIFF: { type: 'ctx' | 'add' | 'del'; text: string }[] = [
  { type: 'ctx', text: 'WHEN objection = "price" AND bail_risk < 0.5:' },
  { type: 'del', text: '  → respond with discount_availability framing' },
  { type: 'add', text: '  → respond with ROI_proof (recovered no-shows, first-month payback)' },
  { type: 'add', text: '  → THEN offer 30-day pilot if skepticism > 0.4' },
  { type: 'ctx', text: 'WHEN objection = "price" AND bail_risk ≥ 0.5:' },
  { type: 'ctx', text: '  → acknowledge + de-risk before any number' },
  { type: 'del', text: '  → escalate immediately' },
  { type: 'add', text: '  → attempt one ROI reframe, escalate only if pressure persists' },
];

function Diff() {
  return (
    <div style={{ maxWidth: 880 }}>
      <div className="row" style={{ gap: 8, marginBottom: 12 }}>
        <span className="tag">Champion playbook</span>
        <Icon name="arrowR" size={14} style={{ color: 'var(--text-3)' }} />
        <span className="tag accent">Draft challenger</span>
        <span className="faint" style={{ fontSize: 12, marginLeft: 'auto' }}>+3 lines · −2 lines</span>
      </div>
      <div className="card" style={{ overflow: 'hidden' }}>
        <div className="card-head">
          <Icon name="note" size={15} />
          <h3 style={{ fontSize: 13 }}>Rule 3 · Price objection</h3>
        </div>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: 12.5 }}>
          {DIFF.map((d, i) => (
            <div
              key={i}
              style={{
                padding: '6px 16px',
                lineHeight: 1.5,
                background: d.type === 'add' ? 'var(--ok-soft)' : d.type === 'del' ? 'var(--danger-soft)' : 'transparent',
                color: d.type === 'add' ? 'var(--ok)' : d.type === 'del' ? 'var(--danger)' : 'var(--text-2)',
                borderLeft: `2px solid ${d.type === 'add' ? 'var(--ok)' : d.type === 'del' ? 'var(--danger)' : 'transparent'}`,
              }}
            >
              <span style={{ opacity: 0.6, marginRight: 10, userSelect: 'none' }}>
                {d.type === 'add' ? '+' : d.type === 'del' ? '−' : ' '}
              </span>
              {d.text}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Editor({ playbook }: { playbook: PlaybookResponse | null }) {
  const seq = playbook?.discovery_sequence ?? [];
  return (
    <div className="col" style={{ gap: 14, maxWidth: 880 }}>
      <div className="card">
        <div className="card-head">
          <span className="tag warn">Price</span>
          <h3 style={{ fontSize: 13 }}>Rule 3 · Price objection</h3>
          <span className="tag" style={{ marginLeft: 'auto' }}>recovery 72%</span>
        </div>
        <div className="card-pad" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div>
            <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 }}>
              Trigger condition
            </div>
            <div className="mono" style={{ fontSize: 13, padding: '10px 12px', borderRadius: 10, background: 'var(--surface-2)', border: '1px solid var(--border)' }}>
              objection == &quot;price&quot; &amp;&amp; bail_risk &lt; 0.5
            </div>
          </div>
          <div>
            <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 }}>
              Response strategy
            </div>
            <textarea
              className="input"
              style={{ width: '100%', height: 92, padding: 12, lineHeight: 1.55, resize: 'vertical', fontFamily: 'var(--font)' }}
              defaultValue="Acknowledge the cost as real, then reframe to ROI — lead with recovered no-shows and first-month payback. If skepticism remains high, offer the 30-day pilot with no annual lock-in."
            />
          </div>
          <div>
            <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 }}>
              Discovery sequence (champion)
            </div>
            <div className="row wrap" style={{ gap: 6 }}>
              {seq.length ? (
                seq.map((s, i) => (
                  <span key={s} className="tag mono" style={{ fontSize: 11.5 }}>
                    {i + 1}. {s}
                  </span>
                ))
              ) : (
                <span className="muted" style={{ fontSize: 12 }}>—</span>
              )}
            </div>
          </div>
        </div>
      </div>
      <button className="btn btn-ghost" style={{ alignSelf: 'flex-start' }}>
        <Icon name="plus" size={15} />
        Add rebuttal rule
      </button>
    </div>
  );
}

export default function KbPage() {
  const router = useRouter();
  const [active, setActive] = useState('objections');
  const [mode, setMode] = useState<'edit' | 'diff'>('edit');
  const [dirty] = useState(true);
  const [playbook, setPlaybook] = useState<PlaybookResponse | null>(null);
  const [draft, setDraft] = useState<Experiment | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchPlaybook()
      .then((res) => {
        if (!cancelled) setPlaybook(res);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load the playbook.');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function saveDraft(goToLab: boolean) {
    setBusy(true);
    setError(null);
    try {
      const res = await savePlaybookDraft({ name: 'Objection rebuttal · ROI-first ordering' });
      setDraft(res.draft);
      if (goToLab) router.push('/improve/lab');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <div style={{ flex: 1, minHeight: 0, display: 'grid', gridTemplateColumns: '262px minmax(0,1fr)' }}>
        {/* section tree */}
        <div className="scroll" style={{ borderRight: '1px solid var(--border)', padding: 16, overflow: 'auto' }}>
          <div className="row" style={{ justifyContent: 'space-between', marginBottom: 12 }}>
            <span className="nav-group" style={{ margin: 0, padding: 0 }}>Playbook · {playbook?.kb_version ?? 'kb'}</span>
            <button className="gctl" style={{ height: 28, padding: '0 8px' }}>
              <Icon name="plus" size={14} />
            </button>
          </div>
          <div className="col" style={{ gap: 3 }}>
            {SECTIONS.map((s) => (
              <button
                key={s.id}
                className={`nav-item${active === s.id ? ' on' : ''}`}
                onClick={() => setActive(s.id)}
                style={{ fontSize: 13 }}
              >
                <Icon name={s.lock ? 'shield' : 'note'} size={16} />
                <span className="grow" style={{ textAlign: 'left' }}>{s.t}</span>
                {s.id === 'objections' && dirty ? (
                  <span style={{ width: 7, height: 7, borderRadius: 50, background: 'var(--warn)' }} />
                ) : (
                  <span className="faint" style={{ fontSize: 11 }}>{s.n}</span>
                )}
              </button>
            ))}
          </div>
          <div className="card solid card-pad" style={{ marginTop: 16 }}>
            <div className="row" style={{ gap: 8, marginBottom: 6 }}>
              <Icon name="branch" size={15} style={{ color: 'var(--accent)' }} />
              <span className="b" style={{ fontSize: 12.5 }}>Draft challenger</span>
            </div>
            <div className="muted" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
              Editing forks <b className="mono">{playbook?.kb_version ?? 'kb'}</b> into a draft. It won&rsquo;t affect live calls until promoted via an experiment.
            </div>
          </div>
        </div>

        {/* editor / diff */}
        <div className="col" style={{ minHeight: 0 }}>
          <div className="row" style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)', gap: 12 }}>
            <div>
              <div className="b" style={{ fontSize: 15, fontFamily: 'var(--font-display)' }}>Objection rebuttals</div>
              <div className="muted" style={{ fontSize: 12 }}>9 rules · editing the champion playbook as a draft</div>
            </div>
            <div className="grow" />
            <div className="seg">
              <button className={mode === 'edit' ? 'on' : ''} onClick={() => setMode('edit')}>
                <Icon name="edit" size={14} /> Edit
              </button>
              <button className={mode === 'diff' ? 'on' : ''} onClick={() => setMode('diff')}>
                <Icon name="diff" size={14} /> Diff vs champion
              </button>
            </div>
            <button className="btn btn-ghost btn-sm" disabled={busy}>
              <Icon name="rollback" size={14} />
              Revert
            </button>
            <button className="btn btn-primary btn-sm" disabled={busy} onClick={() => saveDraft(true)}>
              <Icon name="flask" size={14} />
              Test as experiment
            </button>
          </div>

          <div className="scroll" style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 18 }}>
            {/* Draft banner: makes it explicit a SAVE is a draft, not a live change (R20). */}
            {draft ? (
              <div
                className="card card-pad"
                style={{ borderColor: 'var(--accent-border)', background: 'var(--accent-soft)', marginBottom: 16 }}
              >
                <div className="row" style={{ gap: 9, alignItems: 'center' }}>
                  <Icon name="branch" size={17} style={{ color: 'var(--accent-strong)' }} />
                  <div className="grow">
                    <div className="b" style={{ fontSize: 13, color: 'var(--accent-strong)' }}>
                      Draft challenger created — the live champion is unchanged
                    </div>
                    <div style={{ fontSize: 12, color: 'var(--text-2)', marginTop: 2 }}>
                      <b className="mono">{draft.challenger_version}</b> ({draft.dimension_label}) is now a draft experiment. Promote it via the lab to make it live.
                    </div>
                  </div>
                  <button className="btn btn-ghost btn-sm" onClick={() => router.push('/improve/lab')}>
                    Open in lab
                    <Icon name="arrowR" size={14} />
                  </button>
                </div>
              </div>
            ) : null}
            {error ? (
              <div className="card card-pad" style={{ borderColor: 'var(--danger-border)', background: 'var(--danger-soft)', marginBottom: 16 }}>
                <span style={{ fontSize: 12.5, color: 'var(--danger)' }}>{error}</span>
              </div>
            ) : null}
            {mode === 'edit' ? <Editor playbook={playbook} /> : <Diff />}
            <div style={{ marginTop: 18 }}>
              <button className="btn btn-primary" disabled={busy} onClick={() => saveDraft(false)}>
                <Icon name="save" size={15} />
                {busy ? 'Saving draft…' : 'Save as draft challenger'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

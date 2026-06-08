// P9 — Version History & Rollback (U16). Two-column: a vertical lineage timeline (champion node is
// cyan-glowing, selected node ringed) + a detail panel (facts grid, performance bars, change-from-
// parent line, and either a "live in production" confirmation for the champion or a "Roll back to vX"
// action for an archived version). A rollback opens a confirmation modal; confirming POSTs
// /api/versions/{v}/rollback, which re-promotes that prior version as champion (the single-champion
// invariant demotes the current one). Data from /api/versions (the lineage tree + current champion).
// The KPI snapshot per node renders as mini ladder bars; no internal index renders in the version text.
// CB-02: each node's `dimension_label` is the human CHANGE label — the backend now backfills it for
// legacy promoted rows that stored no dimension (so a promoted version shows its real change, not
// "CHANGE —"); a genesis version's label stays null and renders "—". This page just renders the field.
'use client';

import { useEffect, useState } from 'react';
import { Icon } from '@/components/cadence/Icon';
import { fetchVersions, rollbackVersion } from '@/lib/improve-api';
import { kbVersionLabel } from '@/lib/labels';
import type { VersionNode } from '@/lib/improve-types';

// Pull the numeric ladder KPI out of a node's stored kpi snapshot. The backend stores it under the
// `ladder` key (e.g. {"ladder": 0.51}); older snapshots may use challenger_kpi/champion_kpi, so those
// remain as fallbacks. (Previously this only read challenger_kpi/champion_kpi, so the real `ladder`
// value rendered as 0.00.) Best-effort; defaults to 0 when no numeric value is present.
function ladderOf(node: VersionNode): number {
  const kpi = node.kpi || {};
  const v = (kpi['ladder'] ?? kpi['challenger_kpi'] ?? kpi['champion_kpi'] ?? 0) as number;
  return typeof v === 'number' ? v : 0;
}

// Operator-facing version label: strips the internal experiment suffix
// ("champion_v0__playbooks_discovery_sequence__7" -> "champion_v0") and any short hash
// ("v1-f3798d7a" -> "v1"), then humanizes the prefix ("champion_v0" -> "Champion v0") so no internal
// index leaks into the version text the operator reads. The raw `version` is still used for keys,
// selection, and the rollback POST; the change itself is shown via the dimension_label tag.
function versionLabel(raw: string): string {
  let base = raw.split('__')[0];
  base = base.split('-')[0];
  if (base.startsWith('champion_')) return `Champion ${base.slice('champion_'.length)}`;
  return base;
}

function Mini({ l, v, w }: { l: string; v: string; w: number }) {
  return (
    <div style={{ minWidth: 78 }}>
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 3 }}>
        <span className="faint" style={{ fontSize: 10.5, fontWeight: 600 }}>{l}</span>
        <span className="mono" style={{ fontSize: 11.5, fontWeight: 650 }}>{v}</span>
      </div>
      <div className="bar" style={{ height: 4 }}>
        <i style={{ width: `${Math.min(100, w * 100)}%` }} />
      </div>
    </div>
  );
}

function RollbackModal({
  v,
  championLadder,
  busy,
  onConfirm,
  onClose,
}: {
  v: VersionNode;
  championLadder: number;
  busy: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const expected = (ladderOf(v) - championLadder).toFixed(2);
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="modal">
        <div className="card-pad" style={{ display: 'flex', flexDirection: 'column', gap: 13 }}>
          <div
            style={{
              width: 42,
              height: 42,
              borderRadius: 12,
              background: 'var(--warn-soft)',
              color: 'var(--warn-strong)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <Icon name="rollback" size={20} />
          </div>
          <h3 style={{ fontSize: 17 }}>Roll back to {versionLabel(v.version)}?</h3>
          <p className="muted" style={{ fontSize: 13, lineHeight: 1.55 }}>
            This makes <b className="mono" style={{ color: 'var(--text)' }}>{versionLabel(v.version)} · {kbVersionLabel(v.kb_version)}</b> the live champion.
            The current champion will be archived — no calls in flight are interrupted.
          </p>
          <div className="card solid card-pad" style={{ display: 'flex', justifyContent: 'space-between' }}>
            <span className="muted" style={{ fontSize: 12.5 }}>Expected ladder change</span>
            <span className="mono b" style={{ fontSize: 13, color: Number(expected) < 0 ? 'var(--danger)' : 'var(--ok)' }}>
              {Number(expected) > 0 ? '+' : ''}{expected}
            </span>
          </div>
          <div className="row" style={{ gap: 10, marginTop: 4 }}>
            <button className="btn btn-ghost grow" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button className="btn btn-primary grow" onClick={onConfirm} disabled={busy}>
              <Icon name="rollback" size={15} />
              {busy ? 'Rolling back…' : 'Confirm rollback'}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

export default function VersionsPage() {
  const [versions, setVersions] = useState<VersionNode[]>([]);
  const [sel, setSel] = useState<VersionNode | null>(null);
  const [rollback, setRollback] = useState<VersionNode | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const res = await fetchVersions();
      setVersions(res.versions);
      setSel((cur) => res.versions.find((v) => v.version === cur?.version) ?? res.versions[0] ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load versions.');
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function confirmRollback() {
    if (!rollback) return;
    setBusy(true);
    try {
      await rollbackVersion(rollback.version);
      setRollback(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Rollback failed.');
    } finally {
      setBusy(false);
    }
  }

  const maxLadder = Math.max(0.0001, ...versions.map(ladderOf));
  const championLadder = ladderOf(versions.find((v) => v.is_champion) ?? ({} as VersionNode));

  if (error && versions.length === 0) {
    return (
      <div className="page">
        <div className="page-scroll scroll">
          <div className="pad">
            <div className="card">
              <div className="empty">
                <div className="ico">
                  <Icon name="alert" size={28} />
                </div>
                <h3>Couldn&apos;t load version history</h3>
                <p>{error}</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <div style={{ flex: 1, minHeight: 0, display: 'grid', gridTemplateColumns: '1fr 400px' }}>
        {/* lineage */}
        <div className="scroll" style={{ overflow: 'auto', padding: 22 }}>
          <div className="sec-head">
            <h2>Version lineage</h2>
            <span className="sub">{versions.length} versions · newest first</span>
          </div>
          <div style={{ position: 'relative' }}>
            <div style={{ position: 'absolute', left: 19, top: 12, bottom: 12, width: 2, background: 'var(--border)' }} />
            {versions.map((v) => {
              const selected = sel?.version === v.version;
              const ladder = ladderOf(v);
              return (
                <div
                  key={v.version}
                  onClick={() => setSel(v)}
                  style={{ position: 'relative', display: 'flex', gap: 16, padding: '8px 0', cursor: 'pointer' }}
                >
                  <div style={{ flex: '0 0 auto', width: 40, display: 'flex', justifyContent: 'center', zIndex: 1 }}>
                    <div
                      style={{
                        width: 16,
                        height: 16,
                        borderRadius: 50,
                        marginTop: 18,
                        background: v.is_champion ? 'var(--accent)' : 'var(--surface-3)',
                        border: '3px solid var(--panel)',
                        boxShadow: v.is_champion
                          ? '0 0 0 3px var(--accent-soft)'
                          : selected
                            ? '0 0 0 3px var(--surface-3)'
                            : 'none',
                      }}
                    />
                  </div>
                  <div
                    className={`card${selected ? '' : ' solid'}`}
                    style={{
                      flex: 1,
                      padding: '13px 16px',
                      borderColor: selected ? 'var(--accent-border)' : undefined,
                      boxShadow: selected ? '0 0 0 3px var(--accent-soft)' : undefined,
                    }}
                  >
                    <div className="row" style={{ gap: 9 }}>
                      <span className="b mono" style={{ fontSize: 14 }}>{versionLabel(v.version)}</span>
                      {v.is_champion ? <span className="tag accent dot">Champion</span> : null}
                      <span className="tag">{kbVersionLabel(v.kb_version)}</span>
                      {v.dimension_label ? <span className="tag">{v.dimension_label}</span> : null}
                    </div>
                    <div className="row" style={{ gap: 16, marginTop: 9 }}>
                      <Mini l="Ladder" v={ladder.toFixed(2)} w={ladder / maxLadder} />
                      <span className="faint" style={{ fontSize: 11.5, marginLeft: 'auto', alignSelf: 'flex-end' }}>
                        parent {v.parent_version ? versionLabel(v.parent_version) : 'genesis'}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* detail */}
        {sel ? (
          <div
            className="scroll"
            style={{ borderLeft: '1px solid var(--border)', overflow: 'auto', padding: 20, display: 'flex', flexDirection: 'column', gap: 14 }}
          >
            <div className="row" style={{ gap: 10 }}>
              <span className="b mono" style={{ fontSize: 22, fontFamily: 'var(--font-display)' }}>{versionLabel(sel.version)}</span>
              {sel.is_champion ? <span className="tag accent dot">Champion</span> : <span className="tag">Archived</span>}
            </div>
            <div className="card solid card-pad" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
              {(
                [
                  ['Knowledge base', kbVersionLabel(sel.kb_version)],
                  ['Parent', sel.parent_version ? versionLabel(sel.parent_version) : '—'],
                  ['Change', sel.dimension_label ?? '—'],
                  ['Created', sel.created_at ? new Date(sel.created_at).toLocaleDateString() : '—'],
                ] as [string, string][]
              ).map(([l, vv]) => (
                <div key={l}>
                  <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em' }}>{l}</div>
                  <div className="b" style={{ fontSize: 13.5, marginTop: 3 }}>{vv}</div>
                </div>
              ))}
            </div>
            <div className="card solid card-pad">
              <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 8 }}>
                Performance
              </div>
              <div className="row" style={{ gap: 10, marginBottom: 9 }}>
                <span style={{ width: 116, fontSize: 12, color: 'var(--text-2)', fontWeight: 600 }}>Ladder score</span>
                <span className="bar grow">
                  {/* `ladder` is a normalized 0..1 KPI; scale the bar by that fraction directly. */}
                  <i style={{ width: `${Math.min(100, ladderOf(sel) * 100)}%`, background: 'var(--accent)' }} />
                </span>
                <span className="mono" style={{ fontSize: 12, width: 50, textAlign: 'right' }}>{ladderOf(sel).toFixed(2)}</span>
              </div>
            </div>
            {sel.is_champion ? (
              <div className="card card-pad" style={{ borderColor: 'var(--accent-border)', display: 'flex', gap: 10, alignItems: 'center' }}>
                <Icon name="check" size={18} style={{ color: 'var(--accent)' }} />
                <span style={{ fontSize: 12.5, color: 'var(--text-2)' }}>This version is live in production.</span>
              </div>
            ) : (
              <div className="row" style={{ gap: 10 }}>
                <button className="btn btn-ghost grow">
                  <Icon name="diff" size={16} />
                  Diff vs champion
                </button>
                <button className="btn btn-primary grow" onClick={() => setRollback(sel)}>
                  <Icon name="rollback" size={16} />
                  Roll back to {versionLabel(sel.version)}
                </button>
              </div>
            )}
          </div>
        ) : (
          <div className="scroll" style={{ borderLeft: '1px solid var(--border)', padding: 20 }}>
            <div className="muted" style={{ fontSize: 13 }}>Select a version to inspect it.</div>
          </div>
        )}
      </div>
      {rollback ? (
        <RollbackModal
          v={rollback}
          championLadder={championLadder}
          busy={busy}
          onConfirm={confirmRollback}
          onClose={() => setRollback(null)}
        />
      ) : null}
    </div>
  );
}

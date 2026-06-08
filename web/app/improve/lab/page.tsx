// P6 — Experiment Lab (U16). Champion-vs-challenger experiments rendered as cards: the BEFORE/AFTER
// KPI delta + significance (does the delta CI exclude 0?) + guardrail status + the declared
// one-dimension diff, with running/passed/blocked/promoted/rejected state chips. Active/Past tabs.
// A card opens a right-side drawer with the full champion→challenger before/after, the "what changed"
// diff line, the lift block (enroll, ladder, 95% CI, significance) + guardrail strip, and a
// state-dependent CTA (a blocked challenger links to the Approval Queue, per the handoff §183). The
// "Run experiment" control opens a RunDrawer: pick a dimension (Discovery sequencing reorder, or a
// threshold + new value), see an explicit "this runs N real model calls + $ estimate" cost note, submit
// to /api/experiments/run. CB-15: that run is ASYNC — the POST returns 202 with a `running` record,
// the drawer closes IMMEDIATELY, the running card shows at once, and the /api/experiments poll
// transitions it running -> result (the page never freezes; the backend settles every run to a terminal
// state). When a previously-running experiment settles, a dismissible completion banner appears.
// CB-19: arriving via /improve/lab?episode=<id> (from a call's "Use in experiment") auto-opens the
// RunDrawer pre-seeded + showing the reviewed call's context, so the operator reviews + launches the
// scaffolded experiment — never a blank lab landing.
// CB-24: a rejected/blocked/failed experiment surfaces its guardrail_reason on the card + drawer (and
// in the drawer a dedicated "why it ended" callout) — a failed run never reads as a silent "gone".
// CB-25: the drawer has a "See experiment" button -> /improve/lab/[id], the detail page listing the
// A/B's per-arm mock calls (each openable in Review).
// CB-26: in the RunDrawer, each chosen metric gets a "?" that expands a value→behavior chart (the
// belief→behavior explainer; human text only, no raw slug) from lib/improve-types metricExplainer.
// CB-27: the RunDrawer's threshold picker is a CSV-like grid — each row a {metric, new value}, with
// "+"/"−" to add/remove rows; >1 row sends a multi-metric `changes` request (single row stays the
// single-dimension default), explicitly relaxing the R19 single-dimension invariant for manual runs.
// CB-56: unique run IDs (timestamp suffix) — re-running a dimension never clobbers the prior record.
// CB-57: the dialog quotes the effective (floored/capped) n from the 202 response; the running card
// shows an honest "running" state — no fabricated per-episode progress.
// CB-58: distinct terminal stories per outcome — rejected-insignificant ≠ guardrail-regressed ≠
// failed/timed-out. The guardrail strip says "needs approval" only for blocked state; failed/timed-out
// cards never show fabricated lift metrics; no Python flag phrasing renders.
// CB-70: blank experiment name falls back to humanizeDiffDescription(diff_description) inside titleOf
// so card/Past row/detail header all show e.g. "New sequence: Subject, Grade level, …" rather than
// the raw mutation string. The change label (dimension_label) remains the subtitle; humanized diff is
// the title only when name is empty/draft and diff_description is present.
// CB-71: one state machine for all records — guardrail-regression chip on cards; humanizeGuardrailReason
// on every reason surface; no-result records (timeout/crash reason text) suppress metrics + sample bar;
// zombie (Running + Draft simultaneously) renders as Draft only.
// CB-72: dialog shows "≈ $X.XX" cost estimate; disabled launch button shows helper text; completion
// banner on poll settle; term tooltips on Ladder/Significance/CI; small-n caution chip for n<10/arm.
// CB-80: (L1) no-result/timed-out records suppress the guardrail chip (no run → no verdict);
// (L2) drawer header uses effectiveState so a zombie reads "Draft" everywhere (card + drawer);
// (L3) every blocked/rejected card always shows a one-line WHY — guardrail trip, approval hold, or
// the fallback "Did not meet the promotion bar" when no guardrail_reason is stored; (L4) the CB-73
// baseline note reworded to plain English; (L5) RunDrawer action row is a sticky footer.
// Data from /api/experiments; the discovery-sequencing before/after is the headline demo artifact. All
// semantic labels (state, dimension, version, population/cohort) are humanized before render — no raw
// slug, `__…__` experiment suffix, or DRAFT-/exp- id renders. Cards/drawer show the human version +
// change label (lib/labels: versionLabel/dimensionLabel; backend dimension_label preferred via
// changeLabel()); the population/cohort value (e.g. legacy `held_out`) is humanized via
// lib/labels.populationLabel so no raw snake_case slug leaks next to "Champion v1 · …". An
// experiment whose raw `name` is empty or the placeholder "draft" (case-insensitive) shows the
// humanized diff description as its title instead (titleOf via CB-70), so no bold "draft" or raw
// mutation string renders.
'use client';

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchExperiments, fetchPlaybook, runExperiment } from '@/lib/improve-api';
import type { Experiment, MetricChange, PlaybookResponse, RunExperimentRequest } from '@/lib/improve-types';
import { metricBandFor, metricExplainer } from '@/lib/improve-types';
import { dimensionLabel, humanizeDiffDescription, humanizeGuardrailReason, populationLabel, versionLabel } from '@/lib/labels';

// Operator-facing "what changed" label for an experiment: prefer the backend's pre-translated
// dimension_label, else derive a human label from the raw `dimension` slug. Never the raw `__…__` id.
function changeLabel(e: Experiment): string {
  return e.dimension_label || dimensionLabel(e.dimension);
}

// CB-70: detect a raw mutation string in the name field — the backend sometimes persists the raw
// diff_description string (e.g. "reorder discovery_sequence -> ['subject', 'grade_level', …]") as
// the name when the operator did not supply one. These must NEVER render verbatim as titles.
const RAW_MUTATION_RE = /^(reorder|set|perturb)\s+[a-z_]+/i;

// Card/drawer title for an experiment. The raw `name` can be empty, the unhelpful placeholder
// "draft", or a raw mutation string stored verbatim as the name. In all those cases, fall back
// first to humanizeDiffDescription(diff_description) (CB-70: "reorder discovery_sequence -> [...]"
// becomes "New sequence: Subject, Grade level, …") and only then to the human change label, so no
// raw mutation string or bold "draft" renders as a title. Applies to card list, Past rows, and the
// detail header — a single seam, so all surfaces inherit the fix.
function titleOf(e: Experiment): string {
  const name = (e.name ?? '').trim();
  const isRawMutation = RAW_MUTATION_RE.test(name) && name.includes('->');
  if (!name || name.toLowerCase() === 'draft' || isRawMutation) {
    // CB-70: prefer the humanized diff description so blank/raw-name records get a meaningful title;
    // fall back to the change label only if no diff_description is stored.
    const humanDiff = humanizeDiffDescription(e.diff_description);
    return humanDiff || changeLabel(e);
  }
  return name;
}

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

// CB-71 — ONE truthful display state machine for all records (including legacy rows).
//
// "No result" detection: prefer a stored guardrail_reason that ends with "no result recorded"
// (the two known legacy strings from _settle_failed_run: "the run timed out after Ns — no result
// recorded" / "the run failed with an error (…) — no result recorded"). When that phrase is absent
// we fall back to n=0 as a secondary signal. This keeps backward-compat with any future reason text
// while being explicit about the KNOWN strings (the comment is the source of truth here).
function isNoResult(e: Experiment): boolean {
  if (e.guardrail_reason && e.guardrail_reason.endsWith('no result recorded')) return true;
  return e.n === 0 && e.state === 'rejected';
}

// A zombie record has state="running" but also has no n and effectively no real run started —
// seen as "Running" + "Draft · not yet sampled" simultaneously. Treat as Draft.
function effectiveState(e: Experiment): Experiment['state'] {
  if (e.state === 'running' && e.n === 0 && !e.target) return 'draft';
  return e.state;
}

// CB-72: per-call cost estimate constant. Measured average from production runs ≈ $0.025/call
// (two arms × N calls each; rate measured across OpenRouter usage). Labeled "approximate".
const CALLS_COST_ESTIMATE = 0.025; // USD per model call, approximate (≈$0.025/call measured average)

// CB-72: small-n caution threshold — below this per-arm count, results are directional only.
const SMALL_N_THRESHOLD = 10;

function pts(v: number): string {
  return `${v > 0 ? '+' : ''}${Math.round(v * 100)} pts`;
}
function dec(v: number): string {
  return `${v > 0 ? '+' : ''}${v.toFixed(2)}`;
}

// CB-72: 'tip' prop adds a title= tooltip so operators can hover to see term definitions.
function Cell({ l, v, good, last, tip }: { l: string; v: string; good?: boolean; last?: boolean; tip?: string }) {
  const color = good === true ? 'var(--ok)' : good === false ? 'var(--danger)' : 'var(--text)';
  return (
    <div title={tip} style={{ padding: '11px 14px', borderRight: last ? 0 : '1px solid var(--border)' }}>
      <div className="faint" style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em' }}>
        {l}
      </div>
      <div className="mono" style={{ fontSize: 16, fontWeight: 650, marginTop: 3, color }}>
        {v}
      </div>
    </div>
  );
}

// CB-72: `tip` adds a one-line title= tooltip so operators can hover/tap to learn term definitions.
function Stat({ l, v, good, mono, tip }: { l: string; v: string; good?: boolean | null; mono?: boolean; tip?: string }) {
  const color = good === true ? 'var(--ok)' : good === false ? 'var(--danger)' : 'var(--text)';
  return (
    <div title={tip}>
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

function ExpDrawer({ e, storeChampionVersion, onClose }: { e: Experiment; storeChampionVersion?: string | null; onClose: () => void }) {
  const router = useRouter();
  const tripped = e.guardrail === 'trip';
  const noResult = isNoResult(e);
  // CB-80 L2: use the same state machine as the card face so a zombie reads "Draft" everywhere —
  // not just on the card but also in the drawer header. effectiveState(e) returns 'draft' for a
  // zombie (state=running, n=0, no target) instead of the raw 'running'.
  const dispState = effectiveState(e);
  // CB-24: a rejected/blocked/failed experiment carries WHY in guardrail_reason — surface it
  // prominently (not only in the guardrail strip) so a failed run never reads as a silent "gone".
  // CB-71b: guardrail_reason is humanized via humanizeGuardrailReason on EVERY surface it renders
  // (legacy records may carry raw "challenger_better is False" Python tokens).
  // CB-58: only show the "why it ended" callout once (here), not again in the guardrail strip.
  const isFailed = e.state === 'rejected' || e.state === 'blocked';
  const rawReason = (isFailed || tripped) ? e.guardrail_reason : null;
  const failureReason = rawReason ? humanizeGuardrailReason(rawReason) : null;
  // CB-71c: no-result records show NO metrics/CI/sample bar regardless of stored n.
  // CB-58: a run that produced no comparison has no real lift data to show.
  const hasRealLift = !noResult && e.n > 0;
  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="drawer">
        <div className="card-head">
          <div className="grow">
            <div className="row" style={{ gap: 8, marginBottom: 4 }}>
              <span className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>{changeLabel(e)}</span>
              {/* CB-80 L2: use effectiveState (dispState) so a zombie renders "Draft" in the drawer
                  header, matching the card face — never "Running" and "Draft" simultaneously. */}
              <span className={`tag ${STATE_TAG[dispState]} dot`}>
                {dispState === e.state ? e.state_label : 'Draft'}
              </span>
              {/* CB-71a: guardrail-regression chip visible on the drawer header (not drawer-body-only).
                  CB-80 L1: suppress when noResult — no run completed, so no guardrail verdict exists. */}
              {tripped && !noResult ? (
                <span className="tag danger" style={{ gap: 5 }}>
                  <Icon name="shield" size={12} />
                  Guardrail regression
                </span>
              ) : null}
            </div>
            <div className="b" style={{ fontSize: 15.5, fontFamily: 'var(--font-display)' }}>{titleOf(e)}</div>
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
              <div className="b" style={{ fontSize: 17, fontFamily: 'var(--font-display)', margin: '4px 0' }}>
                {versionLabel(e.champion_version)}
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
              <div className="b" style={{ fontSize: 17, fontFamily: 'var(--font-display)', margin: '4px 0', color: 'var(--accent-strong)' }}>
                {versionLabel(e.challenger_version)} · {changeLabel(e)}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>Ladder {e.challenger_kpi.toFixed(2)} · {populationLabel(e.population)}</div>
            </div>
          </div>

          {/* CB-73: when the experiment's baseline champion differs from the current store champion,
              explain it in one plain sentence so the operator knows why the header shows a different
              version from what this experiment measured against.
              CB-80 L4: reworded to plain English — no engineer-speak ("materialized"). */}
          {storeChampionVersion && e.champion_version && storeChampionVersion !== e.champion_version ? (
            <div style={{ fontSize: 12, color: 'var(--text-3)', fontStyle: 'italic', marginTop: -4 }}>
              Tested against {versionLabel(e.champion_version)} (the live champion&apos;s saved settings aren&apos;t available here — see Versions).
            </div>
          ) : null}

          {/* CB-24: an explicit "why it ended this way" callout for a rejected/blocked/failed run — so
              the operator is never left guessing why an experiment "failed without telling me why". */}
          {failureReason ? (
            <div
              className="row"
              style={{
                gap: 9,
                alignItems: 'flex-start',
                padding: '11px 13px',
                borderRadius: 10,
                background: 'var(--danger-soft)',
                border: '1px solid var(--danger-border)',
              }}
            >
              <Icon name="alert" size={15} style={{ color: 'var(--danger)', marginTop: 1 }} />
              <div>
                <div style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em', color: 'var(--danger)' }}>
                  Why {e.state === 'blocked' ? 'it needs approval' : 'it ended'}
                </div>
                <div style={{ fontSize: 12.5, color: 'var(--danger)', marginTop: 3, lineHeight: 1.5 }}>
                  {failureReason}
                </div>
              </div>
            </div>
          ) : null}

          <div className="card solid card-pad">
            <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 7 }}>
              What changed · {e.dimension_label}
            </div>
            <div className="mono" style={{ fontSize: 13, lineHeight: 1.6 }}>{humanizeDiffDescription(e.diff_description)}</div>
          </div>

          {/* CB-25: "See experiment" -> the detail page listing this A/B's mock calls (both arms),
              each openable in Review. Always available so any experiment can be inspected. */}
          <button className="btn btn-ghost" onClick={() => router.push(`/improve/lab/${encodeURIComponent(e.experiment_id)}`)}>
            <Icon name="flask" size={16} />
            See experiment
            <Icon name="arrowR" size={15} />
          </button>

          {/* CB-58/CB-71c: only show lift stats when the run actually completed a comparison AND is
              not a no-result record. A timed-out or crashed run never produced data — showing zeroed
              schema defaults as metrics fabricates results that never existed. */}
          {hasRealLift ? (
            <div className="card solid card-pad">
              <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 10 }}>
                Lift (challenger − champion)
              </div>
              {/* CB-72: small-n caution chip when per-arm sample is below the meaningful threshold */}
              {e.n < SMALL_N_THRESHOLD ? (
                <div className="row" style={{ gap: 6, alignItems: 'center', marginBottom: 9, padding: '6px 9px', borderRadius: 8, background: 'var(--warn-soft)', border: '1px solid var(--warn-border)' }}>
                  <Icon name="alert" size={13} style={{ color: 'var(--warn)' }} />
                  <span style={{ fontSize: 11.5, color: 'var(--warn)', fontWeight: 600 }}>
                    Small sample — directional only (n={e.n}/arm; results aren&apos;t a stable statistic)
                  </span>
                </div>
              ) : null}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                <Stat l="Same-call enroll" v={pts(e.enroll_delta)} good={e.enroll_delta > 0} />
                {/* CB-72: tooltips on LADDER Δ, 95% CI, and Significance for non-engineers */}
                <Stat
                  l="Ladder Δ"
                  v={dec(e.delta)}
                  good={e.delta > 0}
                  tip="Average commitment level reached by the challenger minus the champion. 0 = no commitment → 4 = same-call enrollment."
                />
                <Stat
                  l="95% CI"
                  v={`[${e.delta_ci[0].toFixed(2)}, ${e.delta_ci[1].toFixed(2)}]`}
                  mono
                  tip="95% confidence interval for the Ladder Δ. When both bounds are on the same side of 0, the result is statistically significant."
                />
                <Stat
                  l="Significance"
                  v={`${Math.round(e.significance * 100)}%`}
                  good={e.challenger_better ? true : null}
                  tip="How confident we are the challenger's lift is real (not random noise). ≥95% = statistically significant."
                />
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
                    ? e.state === 'blocked'
                      ? 'Guardrail tripped — awaiting approval'
                      : 'Guardrail regression detected'
                    : e.guardrail === 'warn'
                      ? 'Guardrail warning'
                      : 'All guardrails pass'}
                </span>
              </div>
            </div>
          ) : (e.state === 'rejected' || noResult) ? (
            <div className="card solid card-pad">
              <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 7 }}>
                Run outcome
              </div>
              <div className="muted" style={{ fontSize: 12.5 }}>
                This run did not complete — no comparison data was recorded.
              </div>
            </div>
          ) : null}

          {e.state === 'passed' ? (
            <button className="btn btn-primary">
              <Icon name="promote" size={16} />
              Promote {versionLabel(e.challenger_version)} to champion
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

// CB-26 — the metric→behavior explainer. A compact inline chart: the metric's value bands as a small
// legend, each band's behavior in human words, with the band matching the CURRENT entered value
// highlighted. Pure presentation off lib/improve-types (metricExplainer / metricBandFor); no raw slug.
function MetricExplainer({ thrKey, value }: { thrKey: string; value: number | null }) {
  const ex = metricExplainer(thrKey);
  if (!ex) return null;
  const active = value != null && !Number.isNaN(value) ? metricBandFor(thrKey, value) : null;
  return (
    <div
      className="card solid card-pad"
      style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 8 }}
      role="note"
    >
      <div className="faint" style={{ fontSize: 11, fontWeight: 600 }}>
        What this number means · {ex.measures}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
        {ex.bands.map((band, i) => {
          const next = ex.bands[i + 1];
          const isActive = active === band;
          const range =
            ex.unit === 'fraction'
              ? `${band.from.toFixed(2)}${next ? `–${next.from.toFixed(2)}` : '+'}`
              : `${band.from}${next ? `–${next.from - 1}` : '+'}`;
          return (
            <div
              key={band.from}
              className="row"
              style={{
                gap: 9,
                alignItems: 'flex-start',
                padding: '7px 9px',
                borderRadius: 8,
                background: isActive ? 'var(--accent-soft)' : 'transparent',
                border: `1px solid ${isActive ? 'var(--accent-border)' : 'var(--border)'}`,
              }}
            >
              <span
                className="mono"
                style={{
                  fontSize: 11,
                  fontWeight: 700,
                  minWidth: 52,
                  color: isActive ? 'var(--accent-strong)' : 'var(--text-3)',
                }}
              >
                {range}
              </span>
              <span style={{ fontSize: 12, lineHeight: 1.45, color: isActive ? 'var(--accent-strong)' : 'var(--text-2)' }}>
                {band.behavior}
              </span>
            </div>
          );
        })}
      </div>
      {active ? (
        <div className="faint" style={{ fontSize: 11 }}>
          At {ex.unit === 'fraction' ? value!.toFixed(2) : value}: {active.behavior}
        </div>
      ) : null}
    </div>
  );
}

// CB-27 — one editable {metric, new value} row of the multi-metric grid. The "?" toggles the CB-26
// explainer for THIS row's metric/value. Remove is disabled when it is the only row.
interface MetricRow {
  key: string;
  value: string;
}
function MetricRowEditor({
  row,
  options,
  onChange,
  onRemove,
  canRemove,
}: {
  row: MetricRow;
  options: ThresholdOption[];
  onChange: (next: MetricRow) => void;
  onRemove: () => void;
  canRemove: boolean;
}) {
  const [open, setOpen] = useState(false);
  const num = row.value.trim() === '' ? null : Number(row.value);
  const selected = options.find((t) => t.key === row.key);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div className="row" style={{ gap: 8, alignItems: 'center' }}>
        <select
          className="field"
          value={row.key}
          onChange={(e) => onChange({ ...row, key: e.target.value })}
          style={{ flex: 2, minWidth: 0 }}
        >
          {options.map((t) => (
            <option key={t.key} value={t.key}>
              {t.label}
              {t.extreme ? ' (needs approval)' : ''}
            </option>
          ))}
        </select>
        <input
          className="input"
          style={{ flex: 1, minWidth: 0 }}
          type="number"
          step="0.05"
          value={row.value}
          onChange={(e) => onChange({ ...row, value: e.target.value })}
          placeholder="value"
          aria-label="New value"
        />
        <button
          type="button"
          className="gctl"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-label="What this metric means"
          title="What this number means"
          style={{ width: 30, padding: 0, justifyContent: 'center', fontWeight: 700 }}
        >
          ?
        </button>
        <button
          type="button"
          className="gctl"
          onClick={onRemove}
          disabled={!canRemove}
          aria-label="Remove this metric"
          style={{ width: 30, padding: 0, justifyContent: 'center' }}
        >
          <Icon name="x" size={13} />
        </button>
      </div>
      {selected?.extreme ? (
        <div className="row" style={{ gap: 6, alignItems: 'center', fontSize: 11.5, color: 'var(--warn)' }}>
          <Icon name="shield" size={13} />
          A pricing change is extreme — this run will go to the approval queue, not auto-promote.
        </div>
      ) : null}
      {open ? <MetricExplainer thrKey={row.key} value={num} /> : null}
    </div>
  );
}

// The Run-an-A/B drawer. Picks ONE dimension + value, shows the explicit cost note, and submits.
// CB-19: an optional `episodeId` (when the drawer was opened by scaffolding from a reviewed call)
// is shown as context AND threaded onto the run request, and `prefillKind` pre-selects the dimension
// so the operator confirms the scaffolded experiment before launching the (async) run.
// CB-57: the cost note quotes the effective (floored/capped) n from the 202 response, not the raw
// requested n. Before submit, a pre-compute shows the floored n so the operator knows what will run.
function RunDrawer({
  onClose,
  onStarted,
  episodeId,
  prefillKind,
}: {
  onClose: () => void;
  onStarted: (exp: Experiment) => void;
  episodeId?: string;
  prefillKind?: RunKind;
}) {
  const [playbook, setPlaybook] = useState<PlaybookResponse | null>(null);
  const [kind, setKind] = useState<RunKind>(prefillKind ?? 'discovery');
  const [seq, setSeq] = useState<string[]>([]);
  // CB-27: the threshold picker is a grid of {metric, value} rows (default ONE row = single-dimension).
  const [rows, setRows] = useState<MetricRow[]>([{ key: THRESHOLD_OPTIONS[0].key, value: '' }]);
  const [n, setN] = useState<number>(12);
  const [name, setName] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // CB-57: backend constants for honest pre-submit cost note (floor/cap).
  // These match _MIN_RUN_N=5 and _MAX_RUN_N=24 in improve.py; kept here so the dialog is honest
  // BEFORE submit (the 202 response confirms effective_n after submit).
  const N_MIN = 5;
  const N_MAX = 24;
  // The effective n the backend will actually use: floor to min, cap to max.
  const effectiveN = Math.max(N_MIN, Math.min(N_MAX, n));
  const wasFloored = effectiveN !== n;

  useEffect(() => {
    let cancelled = false;
    fetchPlaybook()
      .then((pb) => {
        if (cancelled) return;
        setPlaybook(pb);
        setSeq(pb.discovery_sequence ?? []);
        const cur = pb.thresholds?.[THRESHOLD_OPTIONS[0].key];
        setRows([{ key: THRESHOLD_OPTIONS[0].key, value: typeof cur === 'number' ? String(cur) : '' }]);
      })
      .catch((e) => !cancelled && setErr(e instanceof Error ? e.message : 'Could not load the champion config.'));
    return () => {
      cancelled = true;
    };
  }, []);

  // CB-27 — grid row ops. Adding a row defaults to the first metric NOT already in the grid (so two
  // rows don't trivially target the same knob), prefilled with the champion's current value.
  function updateRow(i: number, next: MetricRow) {
    setRows((rs) =>
      rs.map((r, idx) => {
        if (idx !== i) return r;
        // If the METRIC changed (not just the value), reset the value to that knob's champion value;
        // a pure value edit (same key) keeps exactly what the user typed.
        if (next.key !== r.key) {
          const cur = playbook?.thresholds?.[next.key];
          return { key: next.key, value: typeof cur === 'number' ? String(cur) : '' };
        }
        return next;
      }),
    );
  }
  function addRow() {
    const used = new Set(rows.map((r) => r.key));
    const nextKey = THRESHOLD_OPTIONS.find((t) => !used.has(t.key))?.key ?? THRESHOLD_OPTIONS[0].key;
    const cur = playbook?.thresholds?.[nextKey];
    setRows((rs) => [...rs, { key: nextKey, value: typeof cur === 'number' ? String(cur) : '' }]);
  }
  function removeRow(i: number) {
    setRows((rs) => (rs.length <= 1 ? rs : rs.filter((_, idx) => idx !== i)));
  }

  const willBeExtreme =
    kind === 'threshold' && rows.some((r) => THRESHOLD_OPTIONS.find((t) => t.key === r.key)?.extreme);
  // A reorder that hasn't moved anything is a no-op the backend rejects — disable submit for it.
  const reorderUnchanged =
    kind === 'discovery' &&
    !!playbook &&
    JSON.stringify(seq) === JSON.stringify(playbook.discovery_sequence ?? []);
  // CB-27: at least one row must be filled AND differ from the champion's current value for that knob.
  const changedRows =
    kind === 'threshold' && !!playbook
      ? rows.filter(
          (r) => r.value.trim() !== '' && String(playbook.thresholds?.[r.key]) !== r.value.trim(),
        )
      : [];
  const canSubmit =
    !busy &&
    !!playbook &&
    (kind === 'discovery' ? !reorderUnchanged && seq.length >= 2 : changedRows.length >= 1);

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    setErr(null);
    let req: RunExperimentRequest;
    if (kind === 'discovery') {
      req = { dimension: 'playbooks.discovery_sequence', value: seq, n, name: name || undefined, episode_id: episodeId };
    } else if (changedRows.length === 1) {
      // ONE changed row stays the single-dimension default (keeps the R19 invariant).
      const r = changedRows[0];
      req = { dimension: `thresholds.${r.key}`, value: Number(r.value), n, name: name || undefined, episode_id: episodeId };
    } else {
      // CB-27: multiple rows -> a multi-metric `changes` request (explicit invariant relaxation).
      const changes: MetricChange[] = changedRows.map((r) => ({
        dimension: `thresholds.${r.key}`,
        value: Number(r.value),
      }));
      req = { changes, n, name: name || undefined, episode_id: episodeId };
    }
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
            <div className="b" style={{ fontSize: 15.5, fontFamily: 'var(--font-display)' }}>
              {episodeId ? 'Review & run experiment' : 'Run an experiment'}
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
              {episodeId
                ? 'Scaffolded from a reviewed call — confirm the change, then launch the A/B.'
                : 'A/B the live champion against one changed value on a held-out persona set.'}
            </div>
          </div>
          <button className="gctl" onClick={onClose} disabled={busy} style={{ width: 36, padding: 0, justifyContent: 'center' }}>
            <Icon name="x" size={16} />
          </button>
        </div>

        <div className="scroll" style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 16, overflow: 'auto' }}>
          {/* CB-19: the originating reviewed call (context for a scaffolded experiment). */}
          {episodeId ? (
            <div className="row" style={{ gap: 8, alignItems: 'center', padding: '9px 12px', borderRadius: 10, background: 'var(--surface-2)', border: '1px solid var(--border)' }}>
              <Icon name="flask" size={15} style={{ color: 'var(--accent-strong)' }} />
              <span style={{ fontSize: 12.5, color: 'var(--text-2)' }}>
                Seeded from the call you just reviewed — this experiment is built around it.
              </span>
            </div>
          ) : null}
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
            // CB-27: a CSV-like grid — each row a {metric, new value}; "+" adds a row, "−" removes.
            // One row = the single-dimension default; more than one row sends a multi-metric run.
            <div className="card solid card-pad" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                <div className="faint" style={{ fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em' }}>
                  Policy thresholds
                </div>
                {rows.length > 1 ? (
                  <span className="tag warn">
                    <Icon name="bolt" size={12} />
                    {rows.length} metrics together
                  </span>
                ) : null}
              </div>
              <div className="row faint" style={{ gap: 8, fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.04em', paddingRight: 64 }}>
                <span style={{ flex: 2 }}>Metric</span>
                <span style={{ flex: 1 }}>New value</span>
              </div>
              {rows.map((row, i) => (
                <MetricRowEditor
                  key={i}
                  row={row}
                  options={THRESHOLD_OPTIONS}
                  onChange={(next) => updateRow(i, next)}
                  onRemove={() => removeRow(i)}
                  canRemove={rows.length > 1}
                />
              ))}
              <button
                type="button"
                className="btn btn-ghost"
                onClick={addRow}
                disabled={rows.length >= THRESHOLD_OPTIONS.length}
                style={{ alignSelf: 'flex-start' }}
              >
                <Icon name="plus" size={15} />
                Add another metric
              </button>
              {rows.length > 1 ? (
                <div className="faint" style={{ fontSize: 11.5, lineHeight: 1.5 }}>
                  This applies all rows to the challenger at once — a multi-metric experiment. Single-metric
                  is the norm; combining metrics here is an explicit, manual choice.
                </div>
              ) : null}
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

          {/* CB-57/CB-72: explicit cost note using the EFFECTIVE (floored/capped) n. Includes a
              $ estimate clearly labeled approximate (CB-72: ≈$X.XX, single CALLS_COST_ESTIMATE
              constant). If the requested n was below the minimum, note the floor. */}
          <div className="row" style={{ gap: 8, alignItems: 'flex-start', padding: '10px 12px', borderRadius: 10, background: 'var(--accent-soft)', border: '1px solid var(--accent-border)' }}>
            <Icon name="bolt" size={15} style={{ color: 'var(--accent-strong)', marginTop: 1 }} />
            <div>
              <span style={{ fontSize: 12.5, color: 'var(--accent-strong)', fontWeight: 600 }}>
                This runs {effectiveN * 2} real model calls ({effectiveN} per arm × champion and challenger) and spends model credit.
              </span>
              {/* CB-72: $ cost estimate — clearly labeled approximate */}
              <div style={{ fontSize: 11.5, color: 'var(--accent-strong)', marginTop: 3, opacity: 0.85 }}>
                ≈ ${(effectiveN * 2 * CALLS_COST_ESTIMATE).toFixed(2)} (approximate)
              </div>
              {wasFloored ? (
                <div style={{ fontSize: 11.5, color: 'var(--accent-strong)', marginTop: 3, opacity: 0.8 }}>
                  Your input ({n}) is below the minimum sample of {N_MIN} — the run will use {effectiveN} calls per arm.
                </div>
              ) : null}
            </div>
          </div>

        </div>

        {/* CB-80 L5: sticky action footer — always visible regardless of drawer scroll position.
            position:sticky + bottom:0 works inside an overflow:auto container; background +
            border-top prevent content from bleeding through while scrolling. Low-risk: isolated
            to this drawer, no layout changes outside of it. */}
        <div
          style={{
            position: 'sticky',
            bottom: 0,
            padding: '12px 18px 14px',
            background: 'var(--surface)',
            borderTop: '1px solid var(--border)',
          }}
        >
          {err ? (
            <div className="row" style={{ gap: 7, alignItems: 'center', color: 'var(--danger)', fontSize: 12.5, marginBottom: 8 }}>
              <Icon name="alert" size={14} />
              {err}
            </div>
          ) : null}
          <button className="btn btn-primary" style={{ width: '100%' }} onClick={submit} disabled={!canSubmit}>
            <Icon name="play" size={16} />
            {busy ? 'Running…' : `Run experiment · ${effectiveN * 2} calls`}
          </button>
          {/* CB-72: visible helper text when the launch button is disabled (not just grayed out) */}
          {!canSubmit && !busy ? (
            <div style={{ fontSize: 11.5, color: 'var(--text-3)', marginTop: 5, textAlign: 'center' }}>
              Make a change above to enable the run
            </div>
          ) : null}
        </div>
      </div>
    </>
  );
}

function LabPageInner() {
  const searchParams = useSearchParams();
  const [tab, setTab] = useState<'active' | 'past'>('active');
  const [rows, setRows] = useState<Experiment[]>([]);
  const [sel, setSel] = useState<Experiment | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runOpen, setRunOpen] = useState(false);
  // CB-19: the reviewed call this lab visit was scaffolded from (via /improve/lab?episode=<id>). When
  // present, the RunDrawer opens auto, pre-seeded + showing the call context, so "Use in experiment"
  // from a review lands on a review-and-launch view — never a blank lab. Cleared when the drawer closes.
  const scaffoldEpisode = searchParams.get('episode') ?? undefined;
  const [reviewEpisode, setReviewEpisode] = useState<string | undefined>(undefined);
  // experiment_ids freshly started this session, so a new card reads "Just run" until it settles.
  const [freshIds, setFreshIds] = useState<Set<string>>(new Set());
  // CB-72: completion banner shown when a previously-running experiment settles to a terminal state.
  // Dismissed by the operator; auto-cleared when they navigate away.
  const [completionNotice, setCompletionNotice] = useState<{ title: string; state: string } | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // CB-73: the live store champion (may differ from an experiment's champion_version when tests or
  // a rollback changed the lineage after the experiment ran — the drawer shows a mismatch note).
  const [storeChampionVersion, setStoreChampionVersion] = useState<string | null | undefined>(undefined);

  const load = useCallback(async (): Promise<Experiment[] | undefined> => {
    try {
      const res = await fetchExperiments();
      setRows(res.experiments);
      setStoreChampionVersion(res.store_champion_version ?? null);
      setError(null);
      return res.experiments;
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load experiments.');
      return undefined;
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // CB-19: arriving from a call's "Use in experiment" (/improve/lab?episode=<id>) opens the review-and-
  // launch drawer pre-seeded with that call's context — never a blank lab landing. Runs once when the
  // episode param appears.
  useEffect(() => {
    if (scaffoldEpisode) {
      setReviewEpisode(scaffoldEpisode);
      setRunOpen(true);
    }
  }, [scaffoldEpisode]);

  // CB-15: a run now returns 202 with a `running` record (the background A/B settles it later). Insert
  // the running card IMMEDIATELY (the drawer is already closing), then POLL until that experiment
  // leaves `running` — so the card transitions running -> result on its own without a refresh. Bounded
  // by a max tick count so a stuck/failed run (which the backend also settles to a terminal state, CB-20)
  // can't poll forever; the card still reflects whatever terminal state the backend lands on.
  const onStarted = useCallback(
    (exp: Experiment) => {
      setRows((prev) => [exp, ...prev.filter((e) => e.experiment_id !== exp.experiment_id)]);
      setFreshIds((prev) => new Set(prev).add(exp.experiment_id));
      setTab(ACTIVE_STATES.includes(exp.state) ? 'active' : 'past');
      if (pollRef.current) clearInterval(pollRef.current);
      let ticks = 0;
      const MAX_TICKS = 200; // ~5 min at 1.5s/tick — covers a real background run, never infinite.
      pollRef.current = setInterval(async () => {
        ticks += 1;
        const res = await load();
        const cur = res?.find((e) => e.experiment_id === exp.experiment_id);
        // Stop once the run settled out of `running`/`draft`, or the safety cap is hit.
        const settled = cur != null && cur.state !== 'running' && cur.state !== 'draft';
        if (settled && pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
          // CB-72: show a dismissible completion banner when the previously-running experiment settles.
          setCompletionNotice({ title: titleOf(cur), state: cur.state });
        } else if (ticks >= MAX_TICKS && pollRef.current) {
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
          {/* CB-72: dismissible completion banner — shown when a polled experiment settles from
              running to a terminal state. Uses the same notice strip idiom as improve/fidelity. */}
          {completionNotice ? (
            <div
              className="row"
              style={{
                gap: 9,
                alignItems: 'center',
                padding: '10px 14px',
                borderRadius: 10,
                background: completionNotice.state === 'passed' ? 'var(--ok-soft)' : completionNotice.state === 'rejected' ? 'var(--surface-2)' : 'var(--accent-soft)',
                border: `1px solid ${completionNotice.state === 'passed' ? 'var(--ok-border)' : completionNotice.state === 'rejected' ? 'var(--border)' : 'var(--accent-border)'}`,
                marginBottom: 12,
              }}
            >
              <Icon
                name={completionNotice.state === 'passed' ? 'bolt' : 'flask'}
                size={15}
                style={{ color: completionNotice.state === 'passed' ? 'var(--ok)' : 'var(--accent-strong)', flexShrink: 0 }}
              />
              <span style={{ fontSize: 13, fontWeight: 550, color: completionNotice.state === 'passed' ? 'var(--ok)' : 'var(--text-2)', flex: 1 }}>
                Experiment completed — &ldquo;{completionNotice.title}&rdquo; settled as {completionNotice.state}.
              </span>
              <button
                className="gctl"
                onClick={() => setCompletionNotice(null)}
                style={{ width: 28, padding: 0, justifyContent: 'center', flexShrink: 0 }}
                aria-label="Dismiss"
              >
                <Icon name="x" size={14} />
              </button>
            </div>
          ) : null}
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
              {visible.map((e) => {
                // CB-71d: zombie detection — state="running" with no n and no target means this record
                // never actually started a run; render as Draft so "Running + Draft" never co-exists.
                const dispState = effectiveState(e);
                // CB-71c: no-result detection — timeout/crash reason or n=0+rejected
                const noRes = isNoResult(e);
                // CB-71b: always humanize the card reason text (legacy records carry raw Python tokens)
                const cardReason = e.guardrail_reason ? humanizeGuardrailReason(e.guardrail_reason) : null;
                return (
                  <div
                    key={e.experiment_id}
                    className="card"
                    data-kind="exp-card"
                    style={{ cursor: 'pointer', overflow: 'hidden' }}
                    onClick={() => setSel(e)}
                  >
                    <div className="card-pad" style={{ paddingBottom: 12 }}>
                      <div className="row" style={{ gap: 8, marginBottom: 9, flexWrap: 'wrap' }}>
                        <span className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>{changeLabel(e)}</span>
                        {/* CB-71d: use effectiveState chip — zombie shows Draft, never Running+Draft */}
                        <span className={`tag ${STATE_TAG[dispState]} dot`}>{dispState === e.state ? e.state_label : 'Draft'}</span>
                        {/* CB-71a: guardrail-regression chip VISIBLE ON THE CARD (not drawer-only).
                            CB-80 L1: suppress when isNoResult — no run completed so no guardrail
                            determination was possible; showing the chip would be fabricated. */}
                        {e.guardrail === 'trip' && !noRes ? (
                          <span className="tag danger" style={{ gap: 5 }}>
                            <Icon name="shield" size={12} />
                            Guardrail regression
                          </span>
                        ) : null}
                        {dispState === 'passed' && e.challenger_better && !e.is_extreme ? (
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
                      <div className="b" style={{ fontSize: 15, fontFamily: 'var(--font-display)', letterSpacing: '-0.02em' }}>{titleOf(e)}</div>
                      <div className="row" style={{ gap: 8, marginTop: 6 }}>
                        <span className="tag">
                          {versionLabel(e.champion_version)}
                          <Icon name="arrowR" size={11} />
                          {versionLabel(e.challenger_version)} · {changeLabel(e)}
                        </span>
                        <span className="muted" style={{ fontSize: 12 }}>{populationLabel(e.population)}</span>
                      </div>
                    </div>
                    {/* CB-58/CB-71c: only show the lift cells when the run produced real data AND is
                        not a no-result record. A timed-out/crashed run has stored n but no real data —
                        never render those as if they were a meaningful result. */}
                    {!noRes && e.n > 0 ? (
                      <>
                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', borderTop: '1px solid var(--border)' }}>
                          <Cell l="Enroll Δ" v={pts(e.enroll_delta)} good={e.enroll_delta > 0} />
                          {/* CB-72: tooltips on card Ladder Δ and Significance labels */}
                          <Cell
                            l="Ladder Δ"
                            v={dec(e.delta)}
                            good={e.delta > 0}
                            tip="Average commitment level reached (0 none → 4 enrolled). Positive = challenger did better."
                          />
                          <Cell
                            l="Significance"
                            v={`${Math.round(e.significance * 100)}%`}
                            last
                            tip="Confidence the lift is real, not noise. ≥95% = statistically significant."
                          />
                        </div>
                        {/* CB-72: small-n caution chip below the metric cells when n < 10/arm */}
                        {e.n < SMALL_N_THRESHOLD ? (
                          <div
                            style={{
                              padding: '7px 14px',
                              borderTop: '1px solid var(--warn-border)',
                              background: 'var(--warn-soft)',
                              fontSize: 11,
                              color: 'var(--warn)',
                              display: 'flex',
                              gap: 6,
                              alignItems: 'center',
                            }}
                          >
                            <Icon name="alert" size={12} />
                            Small sample — directional only
                          </div>
                        ) : null}
                      </>
                    ) : null}
                    {/* CB-24/CB-58/CB-71b: surface the humanized reason on the CARD — distinct
                        stories per terminal outcome. Legacy raw reason text passes through
                        humanizeGuardrailReason so "challenger_better is False" never renders.
                        CB-80 L3: always show a one-line WHY on blocked/rejected cards, even when
                        guardrail_reason is absent. Derive from available fields: guardrail trip →
                        "Rejected — guardrail regression"; extreme (pricing/approval) → "Held for
                        approval"; otherwise → "Did not meet the promotion bar". cardReason, when
                        present, takes priority over these fallbacks. */}
                    {(e.state === 'blocked' || e.state === 'rejected') ? (() => {
                      const whyText = e.state === 'blocked'
                        ? (cardReason ?? 'Held for approval')
                        : cardReason
                          ? cardReason
                          : e.guardrail === 'trip' && !noRes
                            ? 'Rejected — guardrail regression'
                            : e.is_extreme
                              ? 'Held for approval'
                              : 'Did not meet the promotion bar';
                      return (
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
                          {whyText}
                        </div>
                      );
                    })() : null}
                    {/* CB-57/CB-58: honest sample display. Running → "Running — results land in one
                        batch" (no fabricated per-episode progress). Completed with data → n/target.
                        No-result/failed/timed-out → omit the bar entirely (CB-71c). */}
                    {dispState === 'running' ? (
                      <div style={{ padding: '10px 16px', borderTop: '1px solid var(--border)' }}>
                        <div className="row" style={{ gap: 7, alignItems: 'center' }}>
                          <span className="faint" style={{ fontSize: 11 }}>Running</span>
                          <span className="muted" style={{ fontSize: 11 }}>— results land in one batch</span>
                        </div>
                        <div className="bar" style={{ marginTop: 5 }}>
                          <i style={{ width: '30%', background: 'var(--accent)', opacity: 0.5 }} />
                        </div>
                      </div>
                    ) : !noRes && e.n > 0 ? (
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
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
      {sel ? <ExpDrawer e={sel} storeChampionVersion={storeChampionVersion} onClose={() => setSel(null)} /> : null}
      {runOpen ? (
        <RunDrawer
          onClose={() => {
            setRunOpen(false);
            setReviewEpisode(undefined);
          }}
          onStarted={onStarted}
          episodeId={reviewEpisode}
          prefillKind={reviewEpisode ? 'discovery' : undefined}
        />
      ) : null}
    </div>
  );
}

// useSearchParams (CB-19's ?episode= read) requires a Suspense boundary in the App Router so the page
// doesn't bail out of static optimization; the inner component holds all the lab logic.
export default function LabPage() {
  return (
    <Suspense fallback={<div className="page" />}>
      <LabPageInner />
    </Suspense>
  );
}

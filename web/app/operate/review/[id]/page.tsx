// Call Review (U15) — post-call forensics for one completed episode. Left: the full transcript with
// the per-turn DECISION TRACE (agent turns show the labeled decision + rationale + latency);
// clicking a turn selects it. CB-04: the gate rationale (`.rv-rat`) leads with an internal gate-name
// and embeds raw driver enum slugs (e.g. "pushiness_cap: bail_risk over cap…"), so it renders through
// lib/labels.humanizeRationale ("Pushiness cap: walk-away risk over cap…") — DISPLAY ONLY; the raw
// rationale stays on the turn record / in logs. Right: the BELIEF-TRAJECTORY replay — a scrubber over the turns with
// tick marks at key decision turns, and a belief snapshot (Trust + Walk-away risk gauges, stage +
// the selected turn's decision, slot mini-table) that reflects the NEAREST belief snapshot at/below
// the selected turn. CB-05: per-driver velocity deltas (↑/↓/~) rendered inline next to each gauge
// value using trends["<key>_velocity"] from the BeliefSnapshot. CB-06: agent-estimate vs
// prospect-TRUTH panel (self-play / twin only — panel is ABSENT for real voice/text calls); shows
// the agent's read vs the prospect's true hidden state for the overlapping keys (trust, urgency,
// purchase_intent) plus prospect-only context drivers (need, budget, patience). Drives entirely off
// the recorded per-turn belief log (/api/episodes/{id}).
// CB-28: a tool-use turn (one carrying Turn.retrieved — the KB facts that grounded its answer) shows
// a clickable "N sources" affordance; clicking it opens the PulledInfoPanel on the right, listing the
// retrieved snippets with a muted source attribution (raw citation slug softened, never shown bare).
// CB-30: a PERSISTED "Mark golden / Golden ✓" toggle (beside Export) tags this real call into the
// golden calibration set via POST /api/episodes/{id}/golden (round-trips through metrics['golden'],
// re-hydrated from ep.golden so it survives reload); a "Golden" pill shows when flagged. The existing
// Export already yields the full transcript+belief+outcome record for the tagged set.
// CB-45 (call timing): per agent turn, the decision trace shows voice-timing chips from
// turns[i].timing — "0.8s to first word" + "spoke for 1.4s" (omitted on text/legacy turns). A
// call-level "Voice timing" stat in the outcome strip surfaces avg_first_token_ms / avg_stream_ms
// (absent when the call carries no timing). Human phrases only — no raw ms key renders.
// CB-55: params.id from a Next.js 14 dynamic route is NOT percent-decoded automatically — composite
// episode IDs like RUN-...::arm::n that were encoded by the lab page's router.push arrive as
// RUN-...%3A%3Aarm%3A%3An. We decode once here so fetchEpisode receives the raw ID; the fetch
// helper then encodes it correctly for the HTTP path. Error display uses a safe fallback message,
// never the raw encoded ID blob.
// CB-66 (item 5): the rv-av avatar number badge in the outcome strip is now labelled via a tooltip
// so "88" doesn't read as an unlabeled quantity (it's a deterministic avatar from the episode ID).
// CB-66 (item 9): escalation chip context. When an "Escalate to human" chip appears on an agent
// turn whose NEXT turn is the prospect's explicit request (i.e. the agent escalated proactively
// before the prospect stated it), the chip shows a "(proactive)" annotation with a tooltip explaining
// the signal. Root cause: this is correct behavior — the agent fires escalate when escalation_imminent
// fires (belief signal), which can precede the prospect's explicit statement. Not a data or persistence
// bug; the chip position is accurate. The annotation removes the appearance of misattribution.
'use client';

import { useMemo, useState } from 'react';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { Ring } from '@/components/cadence/Spark';
import { fetchEpisode, fmtDuration, fmtFirstToken, fmtMsShort, fmtSpoke, setEpisodeGolden } from '@/lib/operate-api';
import { archetypeLabel, humanizeRationale, kbVersionLabel, versionLabel } from '@/lib/labels';
import type { BeliefSnapshot, EpisodeDetail, ProspectTurnState, TurnTiming } from '@/lib/operate-types';

// CB-45: per-agent-turn voice-timing chips for the decision trace. Renders up to two small muted
// chips — "0.8s to first word" (time-to-first-token) and "spoke for 1.4s" (stream duration) — from
// turns[i].timing. Returns null on a turn with no timing or all-null fields (text/legacy turn), so
// nothing renders and the trace never crashes. Reuses .rv-lat sizing; a bolt icon flags it as timing.
function TurnTimingChips({ timing }: { timing: TurnTiming | null | undefined }) {
  if (!timing) return null;
  const ftLabel = fmtFirstToken(timing.audio_to_first_token_ms ?? timing.first_token_ms);
  const spoke = fmtSpoke(timing.stream_duration_ms);
  if (!ftLabel && !spoke) return null;
  return (
    <span
      className="rv-lat"
      data-testid="turn-timing"
      style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
      title="Voice timing for this turn"
    >
      <Icon name="bolt" size={11} style={{ color: 'var(--accent)' }} />
      {ftLabel ? <span>{ftLabel}</span> : null}
      {ftLabel && spoke ? <span style={{ opacity: 0.45 }}>·</span> : null}
      {spoke ? <span>{spoke}</span> : null}
    </span>
  );
}

function driverValue(b: BeliefSnapshot | null, key: string): number | null {
  return b?.primary_drivers.find((d) => d.key === key)?.value ?? null;
}

// CB-05: resolve a driver's velocity from the trends map.
// Looks up "<key>_velocity" (e.g. "trust_velocity", "bail_risk_velocity").
function driverVelocity(b: BeliefSnapshot | null, key: string): number | null {
  if (!b?.trends) return null;
  const v = b.trends[`${key}_velocity`];
  return typeof v === 'number' ? v : null;
}

// CB-05: render a subtle signed-velocity indicator:
// ↑ +0.12 in --ok green, ↓ −0.08 in --warn amber, ~ flat in --text-faint muted.
function VelocityBadge({ velocity }: { velocity: number | null }) {
  if (velocity === null) return null;
  const abs = Math.abs(velocity);
  // treat |velocity| < 0.01 as flat
  if (abs < 0.01) {
    return (
      <span
        style={{
          fontSize: 10,
          fontWeight: 600,
          color: 'var(--text-faint)',
          marginLeft: 6,
          fontVariantNumeric: 'tabular-nums',
          letterSpacing: '-0.01em',
        }}
        title="No change this turn"
      >
        ~
      </span>
    );
  }
  const up = velocity > 0;
  const color = up ? 'var(--ok)' : 'var(--warn)';
  const arrow = up ? '↑' : '↓';
  const sign = up ? '+' : '−';
  return (
    <span
      style={{
        fontSize: 10,
        fontWeight: 700,
        color,
        marginLeft: 6,
        fontVariantNumeric: 'tabular-nums',
        letterSpacing: '-0.01em',
        whiteSpace: 'nowrap',
      }}
      title={`Changed ${sign}${abs.toFixed(2)} this turn`}
    >
      {arrow} {sign}{abs.toFixed(2)}
    </span>
  );
}

// CB-06: find the prospect trajectory entry at/just-before `turn` index.
function findProspectState(
  trajectory: ProspectTurnState[],
  turnIdx: number,
): ProspectTurnState | null {
  if (!trajectory.length) return null;
  let best: ProspectTurnState | null = null;
  for (const entry of trajectory) {
    if (entry.turn <= turnIdx) {
      if (best === null || entry.turn > best.turn) best = entry;
    }
  }
  return best;
}

// CB-06: human labels for the shared + prospect-only driver keys. These are the only keys surfaced
// in the panel. Agent keys (trust/urgency/purchase_intent) share names with prospect keys — only
// those three overlap. Raw keys never render; this map is the source of truth for human labels here.
const OVERLAP_KEYS: { agentKey: string; prospectKey: string; label: string }[] = [
  { agentKey: 'trust', prospectKey: 'trust', label: 'Trust' },
  { agentKey: 'urgency', prospectKey: 'urgency', label: 'Urgency' },
  { agentKey: 'purchase_intent', prospectKey: 'purchase_intent', label: 'Purchase intent' },
];

const PROSPECT_ONLY_KEYS: { key: string; label: string }[] = [
  { key: 'need', label: 'Need' },
  { key: 'budget', label: 'Budget' },
  { key: 'patience', label: 'Patience' },
];

// CB-06: gap color — small gap (≤0.1) is good (--ok), medium (≤0.25) is neutral (--warn), larger is --danger.
function gapColor(gap: number): string {
  const abs = Math.abs(gap);
  if (abs <= 0.1) return 'var(--ok)';
  if (abs <= 0.25) return 'var(--warn)';
  return 'var(--danger)';
}

// CB-28: split a retrieved KB fact ("[source] text") into a citation source + the snippet body.
// The source is a content citation pointer (file#section), shown as a muted attribution chip with
// the raw "#"/"_" separators softened to spaces — never a bare internal index in the reading flow.
function splitRetrieved(fact: string): { source: string | null; body: string } {
  const m = /^\s*\[([^\]]+)\]\s*(.*)$/s.exec(fact);
  if (!m) return { source: null, body: fact.trim() };
  const source = m[1].replace(/[#_\-/]+/g, ' ').replace(/\s+/g, ' ').trim();
  return { source: source || null, body: m[2].trim() || fact.trim() };
}

// CB-28: the panel that shows WHAT the agent pulled for a tool-use turn — the KB facts/chunks that
// grounded that answer, each as a readable snippet with a muted source attribution. Rendered only
// when a tool turn is selected for inspection.
function PulledInfoPanel({ facts, turnNum, onClose }: { facts: string[]; turnNum: number; onClose: () => void }) {
  return (
    <div className="card card-pad" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div className="row" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
        <h3 style={{ fontSize: 13, fontWeight: 650, display: 'flex', alignItems: 'center', gap: 6 }}>
          <Icon name="book" size={14} />
          Information pulled · turn {turnNum}
        </h3>
        <button
          className="btn btn-ghost btn-sm"
          onClick={onClose}
          title="Close the pulled-information panel"
          style={{ padding: '2px 8px' }}
        >
          Close
        </button>
      </div>
      <p className="faint" style={{ fontSize: 11, lineHeight: 1.4 }}>
        {'The knowledge-base facts the agent retrieved to ground this answer — what it actually looked at before replying.'}
      </p>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {facts.map((fact, i) => {
          const { source, body } = splitRetrieved(fact);
          return (
            <div
              key={i}
              style={{
                background: 'var(--surface-2)',
                border: '1px solid var(--border)',
                borderRadius: 9,
                padding: '8px 10px',
                display: 'flex',
                flexDirection: 'column',
                gap: 4,
              }}
            >
              {source ? (
                <span
                  style={{
                    fontSize: 10,
                    fontWeight: 700,
                    textTransform: 'uppercase',
                    letterSpacing: '0.05em',
                    color: 'var(--text-faint)',
                  }}
                >
                  {source}
                </span>
              ) : null}
              <span style={{ fontSize: 12.5, lineHeight: 1.45, color: 'var(--text-2)' }}>{body}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// CB-06: Agent-estimate vs prospect-truth comparison panel.
// Only rendered when prospect_trajectory has entries (self-play / twin episodes).
function ProspectTruthPanel({
  snap,
  prospectState,
}: {
  snap: BeliefSnapshot | null;
  prospectState: ProspectTurnState | null;
}) {
  if (!prospectState) return null;

  // Resolve agent values for overlap keys from primary + secondary drivers combined.
  function agentVal(agentKey: string): number | null {
    if (!snap) return null;
    const all = [...(snap.primary_drivers ?? []), ...(snap.secondary_drivers ?? [])];
    const found = all.find((d) => d.key === agentKey);
    return found ? found.value : null;
  }

  return (
    <div
      className="card card-pad"
      style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
    >
      <div>
        <div className="row" style={{ justifyContent: 'space-between', marginBottom: 4 }}>
          <h3 style={{ fontSize: 13, fontWeight: 650 }}>Agent read vs prospect reality</h3>
          <span className="tag info" style={{ fontSize: 10 }}>Self-play only</span>
        </div>
        <p
          className="faint"
          style={{ fontSize: 11, lineHeight: 1.4 }}
        >
          {"Agent's read vs the prospect's true state (self-play only) — the gap is how accurately the agent is reading the room."}
        </p>
      </div>

      {/* Overlapping drivers: agent-estimate vs prospect-truth + gap */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 7 }}>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: '1fr 56px 56px 50px',
            gap: 6,
            fontSize: 10,
            fontWeight: 700,
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
            color: 'var(--text-faint)',
            paddingBottom: 4,
            borderBottom: '1px solid var(--border-soft)',
          }}
        >
          <span>Driver</span>
          <span style={{ textAlign: 'right' }}>Agent</span>
          <span style={{ textAlign: 'right' }}>Prospect</span>
          <span style={{ textAlign: 'right' }}>Gap</span>
        </div>
        {OVERLAP_KEYS.map(({ agentKey, prospectKey, label }) => {
          const av = agentVal(agentKey);
          const pv = prospectState.drivers[prospectKey] ?? null;
          const gap = av !== null && pv !== null ? av - pv : null;
          const gapSign = gap !== null ? (gap >= 0 ? '+' : '−') : null;
          const gapAbs = gap !== null ? Math.abs(gap) : null;
          return (
            <div
              key={agentKey}
              style={{
                display: 'grid',
                gridTemplateColumns: '1fr 56px 56px 50px',
                gap: 6,
                alignItems: 'center',
                fontSize: 12,
              }}
            >
              <span style={{ color: 'var(--text-2)', fontWeight: 600 }}>{label}</span>
              <span
                style={{
                  textAlign: 'right',
                  fontVariantNumeric: 'tabular-nums',
                  fontWeight: 650,
                  color: av !== null ? 'var(--text)' : 'var(--text-faint)',
                }}
              >
                {av !== null ? av.toFixed(2) : '—'}
              </span>
              <span
                style={{
                  textAlign: 'right',
                  fontVariantNumeric: 'tabular-nums',
                  fontWeight: 650,
                  color: pv !== null ? 'var(--text)' : 'var(--text-faint)',
                }}
              >
                {pv !== null ? pv.toFixed(2) : '—'}
              </span>
              <span
                style={{
                  textAlign: 'right',
                  fontVariantNumeric: 'tabular-nums',
                  fontWeight: 700,
                  fontSize: 11,
                  color: gap !== null ? gapColor(gap) : 'var(--text-faint)',
                }}
                title={gap !== null ? `Agent overestimates by ${gapSign}${gapAbs?.toFixed(2)}` : undefined}
              >
                {gap !== null && gapSign !== null && gapAbs !== null
                  ? `${gapSign}${gapAbs.toFixed(2)}`
                  : '—'}
              </span>
            </div>
          );
        })}
      </div>

      {/* Prospect-only context drivers (truth only — agent has no estimate for these) */}
      {PROSPECT_ONLY_KEYS.some(({ key }) => prospectState.drivers[key] !== undefined) && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div
            style={{
              fontSize: 10,
              fontWeight: 700,
              textTransform: 'uppercase',
              letterSpacing: '0.06em',
              color: 'var(--text-faint)',
              paddingTop: 4,
              borderTop: '1px solid var(--border-soft)',
            }}
          >
            Prospect context (not tracked by agent)
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {PROSPECT_ONLY_KEYS.map(({ key, label }) => {
              const val = prospectState.drivers[key];
              if (val === undefined) return null;
              return (
                <div
                  key={key}
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 2,
                    background: 'var(--surface-2)',
                    border: '1px solid var(--border)',
                    borderRadius: 9,
                    padding: '6px 10px',
                    minWidth: 60,
                  }}
                >
                  <span style={{ fontSize: 10, color: 'var(--text-faint)', fontWeight: 600 }}>
                    {label}
                  </span>
                  <span
                    style={{
                      fontSize: 14,
                      fontWeight: 700,
                      fontVariantNumeric: 'tabular-nums',
                      color: 'var(--text-2)',
                    }}
                  >
                    {val.toFixed(2)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// Next.js 14 App Router: route `params` is a PLAIN object (the Promise+`use()` shape is Next 15+).
// Passing a plain object to React.use() throws "unsupported type" — so destructure params directly.
export default function ReviewPage({ params }: { params: { id: string } }) {
  // CB-55: Next.js 14 does NOT auto-decode dynamic route params. A composite ID like
  // RUN-...::arm::n encoded by the lab page arrives as RUN-...%3A%3Aarm%3A%3An. Decode once
  // so every downstream call (fetchEpisode, setEpisodeGolden, export filename) receives the
  // canonical raw ID. decodeURIComponent is idempotent on an already-decoded plain ID.
  const id = decodeURIComponent(params.id);
  const router = useRouter();
  const [ep, setEp] = useState<EpisodeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cur, setCur] = useState(0);
  // "Flag" has no backend in this demo, so it's an HONEST local-only marker: toggling it shows a
  // "Flagged" pill on the call strip (a clear state change), reset per visit. Not persisted.
  const [flagged, setFlagged] = useState(false);
  // CB-30: golden calibration flag. Unlike "Flag", this DOES persist — it round-trips through
  // metrics['golden'] (POST /api/episodes/{id}/golden) and is re-hydrated from ep.golden on load, so
  // it survives reload. `goldenPending` disables the toggle mid-request; `goldenError` surfaces a 404.
  const [golden, setGolden] = useState(false);
  const [goldenPending, setGoldenPending] = useState(false);
  const [goldenError, setGoldenError] = useState<string | null>(null);
  // Timed replay: when playing, auto-advance the scrubber through the turns, pacing EACH turn by its
  // real measured latency (Turn.latency_ms) so the replay unfolds at the agent's actual thinking speed.
  const [playing, setPlaying] = useState(false);
  // CB-28: index of the tool-use turn whose "what was pulled" panel is open (null when none). A turn
  // with retrieved facts is clickable into this panel; selecting another turn closes it.
  const [pulledTurn, setPulledTurn] = useState<number | null>(null);

  // "Export" — client-side download of the already-fetched episode as JSON. No silent no-op: it
  // produces a real file (the forensic record the operator is looking at) named by the call id.
  function exportEpisode() {
    if (!ep) return;
    const blob = new Blob([JSON.stringify(ep, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${ep.episode_id}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // CB-30: toggle this call into/out of the golden calibration set. Persists via
  // POST /api/episodes/{id}/golden, then reflects the confirmed flag locally (so the indicator + the
  // button label update and the change survives a reload). Optimistically flips, reverting on error.
  async function toggleGolden() {
    if (!ep || goldenPending) return;
    const next = !golden;
    setGoldenPending(true);
    setGoldenError(null);
    setGolden(next); // optimistic
    try {
      const res = await setEpisodeGolden(ep.episode_id, next);
      setGolden(res.golden);
    } catch (err) {
      setGolden(!next); // revert
      setGoldenError(err instanceof Error ? err.message : 'Could not update the golden tag.');
    } finally {
      setGoldenPending(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    fetchEpisode(id)
      .then((e) => {
        if (!cancelled) {
          setEp(e);
          setCur(Math.max(0, e.turns.length - 1));
          setError(null);
          // CB-30: re-hydrate the golden flag from the persisted episode so it survives reload.
          setGolden(Boolean(e.golden));
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load the call.');
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  // nearest belief snapshot at/below the selected turn (the replay reflects that moment).
  const snap = useMemo<BeliefSnapshot | null>(() => {
    if (!ep) return null;
    for (let i = cur; i >= 0; i--) {
      if (ep.turns[i]?.belief) return ep.turns[i].belief;
    }
    return null;
  }, [ep, cur]);

  // CB-06: prospect truth state at/just-before the current turn (self-play / twin only).
  const prospectState = useMemo<ProspectTurnState | null>(() => {
    if (!ep || !ep.prospect_trajectory?.length) return null;
    return findProspectState(ep.prospect_trajectory, cur);
  }, [ep, cur]);

  // Drive the timed replay: hold the CURRENT turn for its real latency, then advance one turn. A
  // prospect turn (no measured latency) uses a short default; floored so 0-latency turns aren't
  // instant. Stops at the last turn; cleared on pause/unmount.
  useEffect(() => {
    if (!playing || !ep) return;
    if (cur >= ep.turns.length - 1) {
      setPlaying(false);
      return;
    }
    const ms = ep.turns[cur]?.latency_ms ?? 1000;
    const timerId = setTimeout(() => setCur((c) => Math.min(c + 1, ep.turns.length - 1)), Math.max(350, ms));
    return () => clearTimeout(timerId);
  }, [playing, cur, ep]);

  if (error) {
    // CB-55: never render the raw error string — it may contain an encoded episode ID blob
    // (e.g. "unknown episode 'RUN-...%3A%3A...'"). Show a clean, stable human message instead.
    const isNotFound = error.toLowerCase().includes('unknown episode') || error.includes('404');
    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="ico">
            <Icon name="eye" size={28} />
          </div>
          <h3>Call not found</h3>
          <p>
            {isNotFound
              ? 'This call record does not exist or may have been removed.'
              : 'Could not load this call — the server may be unavailable.'}
          </p>
        </div>
      </div>
    );
  }
  if (!ep) {
    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <p className="muted">Loading call…</p>
        </div>
      </div>
    );
  }

  // A still-connecting / never-started call has no turns to replay — show a graceful empty state
  // (with the call id + a link to the live monitor) instead of a blank transcript + dash belief.
  if (ep.turns.length === 0) {
    const live = ep.outcome_key == null || ep.outcome_key === '' || ep.outcome_key === 'in_progress';
    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="ico">
            <Icon name="eye" size={28} />
          </div>
          <h3>{live ? 'Call still in progress' : 'Nothing recorded yet'}</h3>
          <p>
            <span className="mono">{ep.episode_id}</span> has no turns to review yet.{' '}
            {live
              ? 'The transcript and decision trace will be here once the call wraps up.'
              : 'This call ended without a recorded transcript.'}
          </p>
          {live ? (
            <a className="btn btn-ghost btn-sm" href="/operate/live" style={{ marginTop: 12 }}>
              <Icon name="broadcast" size={14} />
              Watch it live
            </a>
          ) : null}
        </div>
      </div>
    );
  }

  const total = ep.turns.length;
  const denom = total - 1 || 1;
  const active = ep.turns[cur];
  // CB-45: call-level timing summary from the LOCKED contract (means over agent turns that have
  // timing). Both are nullable — a text/legacy call carries no timing and the stat is omitted.
  const avgFirstToken = ep.avg_first_token_ms ?? null;
  const avgStream = ep.avg_stream_ms ?? null;
  const togglePlay = () => {
    if (playing) {
      setPlaying(false);
    } else {
      if (cur >= denom) setCur(0); // restart from the top if parked at the end
      setPlaying(true);
    }
  };
  const trust = driverValue(snap, 'trust');
  const bail = driverValue(snap, 'bail_risk');
  // CB-05: per-gauge velocities
  const trustVelocity = driverVelocity(snap, 'trust');
  const bailVelocity = driverVelocity(snap, 'bail_risk');
  // key decision turns -> tick marks on the scrubber.
  const marks = ep.turns
    .map((t, i) => ({ t, i }))
    .filter(({ t }) => t.speaker === 'agent' && /pivot|close|de-?risk|reframe/i.test(t.decision_label ?? ''));

  const qualifiedRight = (ep.ladder_tier >= 2) === ep.qualified;
  // CB-06: panel visible only when prospect_trajectory is non-empty (self-play / twin episodes).
  const hasTruth = (ep.prospect_trajectory?.length ?? 0) > 0;

  return (
    <div className="page">
      <div className="rv">
        {/* outcome strip */}
        <div className="rv-strip">
          {/* CB-66 (item 5): the number badge is a deterministic avatar derived from the last 2
              digits of the episode ID (the same pattern used across the dashboard for call avatars —
              there is no caller name in this build). The tooltip makes this visible so operators
              know the number is not a call-count or external reference. */}
          <div
            className="rv-av"
            title={`Call avatar — last 2 digits of episode ID ${ep.episode_id}`}
            aria-label={`Call avatar: ${ep.episode_id.replace(/[^0-9]/g, '').slice(-2) || 'CA'}`}
          >
            {ep.episode_id.replace(/[^0-9]/g, '').slice(-2) || 'CA'}
          </div>
          <div>
            {/* Lead with the humanized archetype as the title; the raw episode id is an internal
                index, demoted to a muted mono secondary line. The cohort id (`coh-…`) has no human
                form so it's dropped — only the outcome accompanies the archetype here. */}
            <div className="rv-name">{ep.persona ? archetypeLabel(ep.persona) : 'Unknown archetype'} · {ep.outcome}</div>
            <div className="rv-co mono" style={{ opacity: 0.7 }}>
              #{ep.episode_id}
            </div>
          </div>
          <div className="rv-stat">
            <div className="l">Outcome</div>
            <div className="v" style={{ color: ep.ladder_tier >= 3 ? 'var(--ok)' : undefined }}>
              {ep.outcome} · {ep.ladder_label}
            </div>
          </div>
          <div className="rv-stat">
            <div className="l">Duration</div>
            <div className="v">{fmtDuration((ep.metrics?.duration_ms as number) ?? null)}</div>
          </div>
          <div className="rv-stat">
            <div className="l">Turns</div>
            <div className="v">{ep.turn_count}</div>
          </div>
          {/* CB-45: call-level voice timing — mean time-to-first-token + mean stream duration over
              this call's timed agent turns. Shown only when the call carries timing (avg fields are
              non-null); a text/legacy call simply omits the stat. Human phrases, no raw ms key. */}
          {avgFirstToken != null || avgStream != null ? (
            <div className="rv-stat" data-testid="call-timing">
              <div className="l">Voice timing</div>
              <div
                className="v"
                style={{ display: 'flex', alignItems: 'center', gap: 6 }}
                title="Average time-to-first-word and reply duration across this call's agent turns"
              >
                <Icon name="bolt" size={13} style={{ color: 'var(--accent)' }} />
                {avgFirstToken != null ? `${fmtMsShort(avgFirstToken)} to first word` : null}
                {avgFirstToken != null && avgStream != null ? (
                  <span style={{ opacity: 0.45 }}>·</span>
                ) : null}
                {avgStream != null ? `${fmtMsShort(avgStream)} avg reply` : null}
              </div>
            </div>
          ) : null}
          <div className="rv-stat">
            <div className="l">Version</div>
            <div className="v">
              {versionLabel(ep.version)} · {kbVersionLabel(ep.kb_version)}
            </div>
          </div>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: 9, alignItems: 'center' }}>
            {/* CB-30: golden-set indicator — shown whenever this call is tagged into the calibration
                set. A human label ("Golden") + a star, never the raw metrics key. */}
            {golden ? (
              <span className="tag accent dot" title="In the golden calibration set">
                <Icon name="badge" size={12} /> Golden
              </span>
            ) : null}
            {flagged ? (
              <span className="tag warn dot" title="Flagged for follow-up (local to this view)">
                Flagged
              </span>
            ) : null}
            <button
              className={`btn btn-sm ${flagged ? 'btn-ok' : 'btn-ghost'}`}
              onClick={() => setFlagged((f) => !f)}
              title={flagged ? 'Remove the flag from this call' : 'Flag this call for follow-up (local to this view)'}
            >
              <Icon name="flag" size={14} />
              {flagged ? 'Unflag' : 'Flag'}
            </button>
            {/* CB-30: persisted golden-set toggle (beside Export). The label is a human phrase — "Mark
                golden" / "Golden ✓" — never the raw metrics key; flipping it round-trips through the
                backend so the set is enumerable + exportable and survives reload. */}
            <button
              className={`btn btn-sm ${golden ? 'btn-ok' : 'btn-ghost'}`}
              onClick={toggleGolden}
              disabled={goldenPending}
              title={
                goldenError
                  ? goldenError
                  : golden
                    ? 'Remove this call from the golden calibration set'
                    : 'Add this call to the golden calibration set (for calibration & replay)'
              }
            >
              <Icon name="badge" size={14} />
              {golden ? 'Golden ✓' : 'Mark golden'}
            </button>
            <button className="btn btn-ghost btn-sm" onClick={exportEpisode} title="Download this call's full record as JSON">
              <Icon name="download" size={14} />
              Export
            </button>
            <button
              className="btn btn-ghost btn-sm"
              onClick={async () => {
                // CB-19: scaffold an experiment SEEDED FROM THIS CALL, then open its review-and-launch
                // view in the lab (?episode=<id>) — not a blank lab landing. The scaffold seam persists
                // a draft built around this episode; the lab pre-populates from the episode + lets the
                // operator confirm before running. A scaffold failure still routes (the lab degrades to
                // a pre-seeded run drawer), so the button never dead-ends.
                try {
                  const { scaffoldExperiment } = await import('@/lib/improve-api');
                  await scaffoldExperiment({ episode_id: ep.episode_id });
                } catch {
                  /* non-fatal — the lab still opens pre-seeded from the episode below. */
                }
                router.push(`/improve/lab?episode=${encodeURIComponent(ep.episode_id)}`);
              }}
              title="Scaffold an experiment from this call and open it in the Experiment Lab to review and run"
            >
              <Icon name="flask" size={14} />
              Use in experiment
            </button>
          </div>
        </div>

        {/* transcript + decision trace */}
        <div className="rv-left scroll">
          {ep.turns.map((t, i) => {
            const isAgent = t.speaker === 'agent';
            const latency = t.latency_ms != null ? `${(t.latency_ms / 1000).toFixed(1)}s` : null;
            // CB-28: a tool-use turn carries the retrieved KB facts that grounded its answer.
            const pulled = t.retrieved && t.retrieved.length ? t.retrieved : null;
            // CB-66 (item 9): detect a proactive escalation — the agent's "Escalate to human" chip
            // fires BEFORE the prospect's explicit request (the NEXT turn is a prospect turn). This is
            // correct behavior (the agent fires on escalation_imminent belief signal), but without
            // context it reads as misattribution. We annotate the chip "(proactive)" with a tooltip.
            const isEscalateChip = isAgent && t.decision_label === 'Escalate to human';
            const nextTurn = ep.turns[i + 1];
            const isProactiveEscalation = isEscalateChip && nextTurn?.speaker === 'prospect';
            return (
              <div
                key={t.turn_id}
                className={`rv-turn ${isAgent ? 'a' : 'p'}${i === cur ? ' cur' : ''}`}
                onClick={() => {
                  setCur(i);
                  // Selecting a turn closes any open pulled-info panel unless this turn has its own.
                  setPulledTurn(pulled ? i : null);
                }}
              >
                <div className="rv-tnum">{i + 1}</div>
                <div className="rv-tc">
                  <div className="rv-twho">
                    {isAgent ? 'Alex (agent)' : 'Prospect'}
                    <span>·</span>
                    {t.belief?.stage ? (
                      <span className="tag" style={{ padding: '1px 7px' }}>
                        {t.belief.stage}
                      </span>
                    ) : null}
                  </div>
                  <div className="rv-ttext">{t.text}</div>
                  {isAgent && t.decision_label ? (
                    <div className="rv-trace">
                      <span
                        className="rv-dchip"
                        title={isProactiveEscalation
                          ? 'The agent detected escalation risk (escalation_imminent signal) before the prospect stated the request — this is a proactive escalation, not a misattribution.'
                          : undefined}
                      >
                        <Icon name="spark" size={12} />
                        {t.decision_label}
                        {/* CB-66 (item 9): annotate proactive escalations so operators understand
                            the chip precedes the prospect's explicit request by design — the agent
                            is responding to the belief signal, not the explicit text. */}
                        {isProactiveEscalation ? (
                          <span
                            style={{ fontSize: 10, opacity: 0.75, marginLeft: 3, fontWeight: 600 }}
                          >
                            proactive
                          </span>
                        ) : null}
                      </span>
                      {t.rationale ? <span className="rv-rat">{`"${humanizeRationale(t.rationale)}"`}</span> : null}
                      {latency ? <span className="rv-lat">{latency}</span> : null}
                      {/* CB-45: voice-timing chips for this agent turn (omitted when the turn has no
                          timing — text/legacy). first-token = how long until the agent started
                          speaking; spoke-for = how long the reply streamed. Human phrases only. */}
                      <TurnTimingChips timing={t.timing} />
                      {/* end CB-45 */}
                      {/* CB-28: clickable affordance — this turn looked something up; reveal what. */}
                      {pulled ? (
                        <button
                          type="button"
                          className="rv-pulled"
                          onClick={(e) => {
                            e.stopPropagation();
                            setCur(i);
                            setPulledTurn((p) => (p === i ? null : i));
                          }}
                          title="Show the knowledge-base facts the agent pulled to ground this answer"
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: 4,
                            background: pulledTurn === i ? 'var(--accent-soft, var(--surface-2))' : 'transparent',
                            border: '1px solid var(--border)',
                            borderRadius: 7,
                            padding: '1px 7px',
                            fontSize: 11,
                            fontWeight: 600,
                            color: 'var(--text-2)',
                            cursor: 'pointer',
                          }}
                        >
                          <Icon name="book" size={11} />
                          {pulled.length === 1 ? '1 source' : `${pulled.length} sources`}
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>

        {/* belief replay */}
        <div className="rv-right scroll">
          <div className="card card-pad rv-replay">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <h3 style={{ fontSize: 14 }}>Belief replay</h3>
              <span className="tag accent">Turn {cur + 1}</span>
            </div>
            <div className="rv-scrub">
              <button
                type="button"
                onClick={togglePlay}
                aria-label={playing ? 'Pause replay' : 'Play replay'}
                title={playing ? 'Pause' : 'Play — advances each turn at its real latency'}
                style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', display: 'flex', alignItems: 'center' }}
              >
                <Icon name={playing ? 'pause' : 'play'} size={16} style={{ color: 'var(--accent)' }} />
              </button>
              <div
                className="rv-track"
                onClick={(e) => {
                  const r = e.currentTarget.getBoundingClientRect();
                  setCur(Math.round(((e.clientX - r.left) / r.width) * denom));
                }}
              >
                <div className="rv-trackfill" style={{ width: `${(cur / denom) * 100}%` }} />
                {marks.map(({ i, t }) => (
                  <div
                    key={i}
                    className="rv-mk"
                    style={{ left: `${(i / denom) * 100}%` }}
                    title={t.decision_label ?? ''}
                  />
                ))}
                <div className="rv-knob" style={{ left: `${(cur / denom) * 100}%` }} />
              </div>
            </div>
            <div className="faint" style={{ fontSize: 11 }}>
              Press play to replay the call turn-by-turn at its real per-turn latency, or drag the
              scrubber / click a turn — the belief state below reflects that moment.
            </div>
          </div>

          {/* CB-05 + CB-06: belief gauges with velocity deltas */}
          <div className="card card-pad">
            <div className="rv-gg">
              <div className="rv-g">
                <div className="rv-gt">
                  <span>Trust</span>
                  {/* CB-05: trust velocity delta */}
                  <VelocityBadge velocity={trustVelocity} />
                </div>
                <div className="rv-gv">{trust == null ? '—' : trust.toFixed(2)}</div>
                <div className="bar" style={{ marginTop: 8 }}>
                  <i style={{ width: `${trust == null ? 0 : Math.round(trust * 100)}%`, background: 'var(--ok)' }} />
                </div>
              </div>
              <div className="rv-g">
                <div className="rv-gt">
                  <span>Walk-away risk</span>
                  {/* CB-05: bail_risk velocity delta */}
                  <VelocityBadge velocity={bailVelocity} />
                </div>
                <div className="rv-gv">{bail == null ? '—' : bail.toFixed(2)}</div>
                <div className="bar" style={{ marginTop: 8 }}>
                  <i style={{ width: `${bail == null ? 0 : Math.round(bail * 100)}%`, background: 'var(--warn)' }} />
                </div>
              </div>
            </div>
            <div className="row" style={{ margin: '14px 0 12px', gap: 10 }}>
              {snap?.stage ? <span className="tag accent">Stage · {snap.stage}</span> : null}
              {active?.decision_label ? <span className="tag violet">{active.decision_label}</span> : null}
            </div>
            {(snap?.slots ?? []).map((s) => (
              <div className="rv-slot" key={s.key}>
                <span className="n">{s.label}</span>
                <span className="bar">
                  <i
                    style={{
                      width: `${Math.round(s.confidence * 100)}%`,
                      background: s.confidence < 0.6 ? 'var(--warn)' : 'var(--accent)',
                    }}
                  />
                </span>
                <span className="v">{s.confidence.toFixed(2)}</span>
              </div>
            ))}
          </div>

          {/* CB-28: what the agent pulled for the selected tool-use turn (its grounding facts). */}
          {pulledTurn !== null && ep.turns[pulledTurn]?.retrieved?.length ? (
            <PulledInfoPanel
              facts={ep.turns[pulledTurn]!.retrieved as string[]}
              turnNum={pulledTurn + 1}
              onClose={() => setPulledTurn(null)}
            />
          ) : null}

          {/* CB-06: agent-estimate vs prospect-truth panel (self-play / twin only) */}
          {hasTruth && (
            <ProspectTruthPanel snap={snap} prospectState={prospectState} />
          )}

          <div className="rv-outcome">
            <Ring value={ep.qualified ? 0.93 : 0.4} size={46} color={qualifiedRight ? 'var(--ok)' : 'var(--warn)'} label={qualifiedRight ? '✓' : '!'} />
            <div>
              <div className="b" style={{ fontSize: 13.5 }}>
                {qualifiedRight ? 'Qualified correctly' : 'Qualification mismatch'}
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {ep.outcome} · {ep.ladder_label}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

// P1 — Live Call Monitor (U15/U16). Polls /api/live/active every 5 s to maintain a QUEUE of
// in-progress calls. When ≥1 real call is active: renders a queue rail (newest-first) that lets
// the operator switch which call the detailed monitor shows; the detail snapshot is fetched via
// /api/live?episode_id=<selected>. When no calls are active: shows the "No active call" empty
// state with a "Show sample call" toggle; toggling ON fetches /api/live/sample and renders the
// monitor with a SAMPLE badge (no LIVE pill, no recording dot — cannot be mistaken for a real
// call). Preserves the "Connecting…" state for a live 0-turn call. Belief/stage/act labels are
// pre-translated by the backend; raw persona slug is humanized via @/lib/labels (archetypeLabel).
// N3 invariant: LIVE pill + recording dot are ONLY shown when snap.active is true AND snap.sample
// is NOT true. Internal episode IDs never render as primary labels; persona_label is used.
// "Run demo call" (empty state): POSTs /api/demo/auto/start (lib/api.startAutoDemo) to launch a
// SERVER-SIDE demo call that streams turns into the monitor over ~1-2 min, independent of the
// browser; the existing queue poll surfaces + auto-selects it (no navigation, no client orchestrator
// — navigating away no longer kills the call). A 503 (no LLM key / capacity) shows its reason.
// The call-duration timer ticks live (useNow, 1 s) off created_at for active calls; auto-scroll is a
// real toggle that pins the transcript scroller to the newest turn while on.
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchActiveCalls, fetchLive, fetchSampleCall, fmtDuration, initials } from '@/lib/operate-api';
import { archetypeLabel, versionLabel } from '@/lib/labels';
import type { ActiveCallSummary, ActiveCallsResponse, BeliefSnapshot, LiveSnapshot, Turn } from '@/lib/operate-types';
import { ApiError, startAutoDemo } from '@/lib/api';

const POLL_MS = 5000;

/** A `now` timestamp that ticks once a second so a live call's elapsed duration counts up in the UI
 *  (the live snapshot has no duration_ms until the call ends). Pure clock — no data fetch. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

function fmtPct(v: number | null | undefined): string {
  return v == null ? '—' : v.toFixed(2);
}

function Gauge({ label, value, color }: { label: string; value: number | null; color: string }) {
  const pct = value == null ? 0 : Math.round(value * 100);
  return (
    <div className="lv-g">
      <div className="lv-gt">{label}</div>
      <div className="lv-gv">{fmtPct(value)}</div>
      <div className="bar" style={{ marginTop: 8 }}>
        <i style={{ width: `${pct}%`, background: color }} />
      </div>
    </div>
  );
}

function TranscriptTurn({ turn }: { turn: Turn }) {
  const isAgent = turn.speaker === 'agent';
  const latency = turn.latency_ms != null ? `${(turn.latency_ms / 1000).toFixed(1)}s` : null;
  return (
    <div className={`lv-turn ${isAgent ? 'a' : 'p'}`}>
      <div className="lv-th">
        {isAgent ? 'Alex (agent)' : 'Prospect'}
        <span>·</span>
        {`turn ${turn.turn_id + 1}`}
      </div>
      <div className="lv-bub">{turn.text}</div>
      {isAgent && turn.decision_label ? (
        <div className="lv-dec">
          <span className="lv-dchip">
            <Icon name="spark" size={12} />
            {turn.decision_label}
          </span>
          {latency ? <span className="lv-lat">{latency}</span> : null}
        </div>
      ) : null}
      {isAgent && turn.rationale ? <div className="lv-rat">{`"${turn.rationale}"`}</div> : null}
    </div>
  );
}

function BeliefColumn({ belief, escalationImminent }: { belief: BeliefSnapshot | null; escalationImminent: boolean }) {
  const trust = belief?.primary_drivers.find((d) => d.key === 'trust')?.value ?? null;
  const bail = belief?.primary_drivers.find((d) => d.key === 'bail_risk')?.value ?? null;
  const stage = belief?.stage ?? '—';
  const conf = belief?.decision_confidence ?? null;
  const slots = belief?.slots ?? [];
  const drivers = belief?.secondary_drivers ?? [];

  return (
    <div className="card lv-belief">
      <div className="card-head">
        <Icon name="spark" size={16} style={{ color: 'var(--accent)' }} />
        <h3>Belief State</h3>
        <span className="tag" style={{ marginLeft: 'auto' }}>
          {stage}
        </span>
      </div>
      <div className="lv-bb">
        {/* FOREMOST: trust + walk-away risk gauges (the prioritized signals) */}
        <div className="lv-gg">
          <Gauge label="Trust" value={trust} color="var(--ok)" />
          <Gauge label="Walk-away risk" value={bail} color="var(--warn)" />
        </div>
        <div className="lv-box">
          <div>
            <div className="l">Stage</div>
            <div className="v">{stage}</div>
          </div>
          <div style={{ borderLeft: '1px solid var(--border)', paddingLeft: 14 }}>
            <div className="l">Active objection</div>
            <div className="v">{belief?.active_objection ?? '—'}</div>
          </div>
        </div>
        {/* escalation-imminent flag — one of the four foremost signals */}
        {escalationImminent ? (
          <div className="lv-escal">
            <div className="i">
              <Icon name="alert" size={17} />
            </div>
            <div>
              <div style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--warn-strong)' }}>Escalation · Armed</div>
              <div style={{ fontSize: 11, color: 'var(--warn-strong)', opacity: 0.8 }}>
                Watching for a moment that needs a human
              </div>
            </div>
          </div>
        ) : null}
        <div className="lv-dcard">
          <div className="l">Decision confidence</div>
          <div className="big">
            <Icon name="gauge" size={16} />
            {conf != null ? `${(conf * 100).toFixed(0)}% confident` : 'Pending'}
          </div>
          <div className="r">The agent&apos;s confidence in its current move, from the live decision trace.</div>
          {conf != null ? (
            <div className="lv-conf">
              Confidence
              <span className="bar" style={{ width: 54 }}>
                <i style={{ width: `${Math.round(conf * 100)}%` }} />
              </span>
              {conf.toFixed(2)}
            </div>
          ) : null}
        </div>
      </div>

      {/* SECONDARY: the full belief state — slots + latent drivers (expand-on-demand IA) */}
      <div className="lv-exp scroll">
        <div className="lv-eh">
          Full belief state
          <Icon name="chevDown" size={14} style={{ marginLeft: 'auto' }} />
        </div>
        {slots.length === 0 ? (
          <div className="faint" style={{ fontSize: 12 }}>
            No discovery slots filled yet.
          </div>
        ) : (
          slots.map((s) => (
            <div className="lv-slot" key={s.key}>
              <span className="n">{s.label}</span>
              <span className="bar">
                <i
                  style={{
                    width: `${Math.round(s.confidence * 100)}%`,
                    background: s.confidence < 0.5 ? 'var(--warn)' : 'var(--accent)',
                  }}
                />
              </span>
              <span className="v">{s.confidence.toFixed(2)}</span>
            </div>
          ))
        )}
        {drivers.length > 0 ? (
          <div className="lv-drivers">
            {drivers.map((d) => (
              <span className="lv-driver" key={d.key}>
                {d.label}
                <span className="ar fl">{d.value.toFixed(2)}</span>
              </span>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

/** Risk indicator bar shown in the queue rail — dual trust/bail-risk mini-bars. */
function QueueRiskBar({ trust, bailRisk }: { trust: number | null; bailRisk: number | null }) {
  const t = trust ?? 0;
  const b = bailRisk ?? 0;
  return (
    <div className="lv-qrisk" title={`Trust ${fmtPct(trust)} · Walk-away ${fmtPct(bailRisk)}`}>
      <span className="bar" style={{ flex: 1 }}>
        <i style={{ width: `${Math.round(t * 100)}%`, background: 'var(--ok)' }} />
      </span>
      <span className="bar" style={{ flex: 1 }}>
        <i style={{ width: `${Math.round(b * 100)}%`, background: 'var(--warn)' }} />
      </span>
    </div>
  );
}

/** One row in the active-calls queue rail. */
function QueueEntry({
  call,
  selected,
  onSelect,
}: {
  call: ActiveCallSummary;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  const label = call.persona_label ?? 'Caller';
  const stage = call.stage ?? '—';
  return (
    <button
      className={`lv-qrow${selected ? ' sel' : ''}`}
      onClick={() => onSelect(call.episode_id)}
      title={`Switch to this call (${label})`}
    >
      <div className="lv-qleft">
        <div className="lv-qav">{initials(label)}</div>
        <div className="lv-qmeta">
          <div className="lv-qlabel">{label}</div>
          <div className="lv-qstage">{stage}</div>
        </div>
      </div>
      <div className="lv-qright">
        <QueueRiskBar trust={call.trust} bailRisk={call.bail_risk} />
        <div className="lv-qturns">{call.turn_count}t</div>
        {/* Pulsing LIVE dot — always shown in the queue since all entries are active calls */}
        <span className="live-dot" />
        {call.escalation_imminent ? (
          <Icon name="alert" size={13} style={{ color: 'var(--warn-strong)', flexShrink: 0 }} />
        ) : null}
      </div>
    </button>
  );
}

/** The detailed call monitor (transcript + belief column). Shared between real-live and sample. */
function CallMonitor({
  snap,
  onOpenReview,
}: {
  snap: LiveSnapshot;
  onOpenReview: (id: string) => void;
}) {
  const ep = snap.episode!;
  const lastBelief = [...ep.turns].reverse().find((t) => t.belief)?.belief ?? null;
  const escalationImminent = snap.priority?.escalation_imminent ?? lastBelief?.escalation_imminent ?? false;
  const lastAct = snap.priority?.last_act_label ?? null;
  // "Genuinely live" — active AND not a sample preview. Controls all LIVE affordances (pill, dot).
  const isLive = snap.active === true && snap.sample !== true;
  const isSample = snap.sample === true;

  // Live elapsed timer: an active call has no metrics.duration_ms until it ends, so for a live call
  // count up from created_at against a 1 s ticking clock; a completed/sample call uses the recorded
  // duration. created_at may be null (show "—" via fmtDuration(null)).
  const now = useNow();
  const startedMs = ep.created_at ? Date.parse(ep.created_at) : NaN;
  const durationMs =
    isLive && Number.isFinite(startedMs)
      ? now - startedMs
      : ((ep.metrics?.duration_ms as number | undefined) ?? null);

  // Auto-scroll: when ON, pin the transcript scroller to the bottom as new turns stream in.
  const [autoScroll, setAutoScroll] = useState(true);
  const streamRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!autoScroll) return;
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [ep.turns.length, autoScroll]);

  return (
    <div className="lv">
      <div className="lv-body">
        {/* LEFT — prospect header + transcript with decision trace */}
        <div className="lv-col">
          <div className="card lv-pros">
            <div className="lv-pav">{initials(ep.episode_id)}</div>
            <div>
              <div className="lv-pname">{ep.episode_id}</div>
              <div className="lv-pco">{ep.channel === 'voice' ? 'Web-voice' : 'Text'}</div>
              <div className="row gap6" style={{ marginTop: 8 }}>
                {/* Humanize the persona archetype; the raw cohort id (`coh-…`) is an internal
                    grouping index with no human label, so it's dropped from operator-facing chips. */}
                {ep.persona ? <span className="tag accent">{archetypeLabel(ep.persona)}</span> : null}
                <span className="tag">{ep.channel === 'voice' ? 'Web-voice' : 'Text'}</span>
                {/* SAMPLE badge — rendered when this is a preview call, never an episode id */}
                {isSample ? (
                  <span className="tag warn" style={{ fontWeight: 700, letterSpacing: '0.04em' }}>
                    SAMPLE
                  </span>
                ) : null}
              </div>
            </div>
            <div className="lv-pright">
              <div className="lv-dur">{fmtDuration(durationMs)}</div>
              <div className="lv-vtag">
                {versionLabel(ep.version)} · {ep.kb_version}
              </div>
            </div>
          </div>

          <div className="card lv-tcard">
            <div className="card-head">
              <h3>Transcript</h3>
              {/* LIVE pill ONLY when genuinely on the line; SAMPLE tag when preview; neutral tag
                  when showing a completed call — never contradicts the footer (N3). */}
              {isLive ? (
                <span className="live-pill" style={{ marginLeft: 10 }}>
                  <i />
                  LIVE
                </span>
              ) : isSample ? (
                <span className="tag warn" style={{ marginLeft: 10, fontWeight: 700, letterSpacing: '0.04em' }}>
                  SAMPLE
                </span>
              ) : (
                <span className="tag" style={{ marginLeft: 10 }}>
                  Most recent call
                </span>
              )}
              <span
                className="row gap8"
                style={{ marginLeft: 'auto', fontSize: 11.5, color: 'var(--text-3)', fontWeight: 550 }}
              >
                Auto-scroll
                <span
                  className={`toggle${autoScroll ? ' on' : ''}`}
                  role="switch"
                  aria-checked={autoScroll}
                  aria-label="Toggle auto-scroll"
                  onClick={() => setAutoScroll((v) => !v)}
                >
                  <i />
                </span>
              </span>
            </div>
            <div className="lv-stream scroll" ref={streamRef}>
              {ep.turns.map((t) => (
                <TranscriptTurn key={t.turn_id} turn={t} />
              ))}
              {isLive ? (
                <div className="lv-turn a">
                  <div className="lv-th">
                    Alex (agent)<span>·</span>now
                  </div>
                  <div className="lv-bub lv-speak">
                    <i />
                    <i />
                    <i />
                  </div>
                </div>
              ) : null}
            </div>
            <div className="lv-fade" />
          </div>
        </div>

        {/* RIGHT — prioritized belief state */}
        <div className="lv-col">
          <BeliefColumn belief={lastBelief} escalationImminent={escalationImminent} />
        </div>
      </div>

      {/* ACTION BAR */}
      <div className="lv-actions">
        <span className="lv-rec">
          {/* Pulsing red recording dot ONLY while genuinely active (not sample); static neutral dot
              otherwise, so the footer never reads "recording now" when it isn't (N3). */}
          <span
            className="rd"
            style={isLive ? undefined : { background: 'var(--text-3)', animation: 'none' }}
          />
          {isLive
            ? `Recording · Turn ${ep.turn_count}`
            : isSample
              ? 'Sample call (not live)'
              : 'Most recent call (not live)'}
        </span>
        <div className="lv-tl">
          <span style={{ fontSize: 11, fontWeight: 650, color: 'var(--text-3)' }}>Last act</span>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)' }}>{lastAct ?? '—'}</span>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 10 }}>
          <button className="btn btn-ghost" onClick={() => onOpenReview(ep.episode_id)}>
            <Icon name="eye" size={16} />
            Open review
          </button>
          {/* Operator live-takeover (barging into the live LiveKit room as a human) isn't wired in
              this build, so the control is honestly disabled rather than a click-that-does-nothing. */}
          <button
            className="btn btn-primary btn-lg"
            disabled
            title="Live takeover isn't available in this build — the monitor is view-only. Interact via the demo console or the phone line."
          >
            <Icon name="hand" size={17} />
            Take over
          </button>
        </div>
      </div>
    </div>
  );
}

/** Transient status for the server-side demo trigger. The call itself runs on the backend and shows
 *  up via the live queue poll — this only narrates the kick-off ('starting') and any 503 ('error'). */
type DemoStatus = { kind: 'idle' | 'starting' | 'error'; msg?: string };

/** Human-readable narration for the demo-trigger status (no internal slugs shown). */
function demoStatusText(d: DemoStatus): string {
  switch (d.kind) {
    case 'starting':
      return 'Starting demo… it will appear here in a few seconds.';
    case 'error':
      return d.msg ?? 'Could not start the demo call.';
    default:
      return '';
  }
}

export default function LivePage() {
  const router = useRouter();
  // Queue state: full list from /api/live/active
  const [activeCalls, setActiveCalls] = useState<ActiveCallsResponse | null>(null);
  // The episode the operator has selected to watch in detail; defaults to calls[0].episode_id
  const [selectedEpisodeId, setSelectedEpisodeId] = useState<string | null>(null);
  // The detailed snapshot for the selected call (or empty-state / sample)
  const [snap, setSnap] = useState<LiveSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Sample toggle state — only active when no real calls
  const [showSample, setShowSample] = useState(false);
  const [sampleSnap, setSampleSnap] = useState<LiveSnapshot | null>(null);
  const [sampleLoading, setSampleLoading] = useState(false);

  // "Run demo call" state — POSTs /api/demo/auto/start to launch a SERVER-SIDE demo call that runs
  // independent of the browser; the queue poll surfaces + auto-selects it. We only track the
  // transient kick-off ('starting', cleared after ~10 s) and any 503 reason ('error').
  const [demoStatus, setDemoStatus] = useState<DemoStatus>({ kind: 'idle' });
  const demoStarting = demoStatus.kind === 'starting';

  const handleRunDemo = useCallback(async () => {
    if (demoStarting) return;
    setShowSample(false); // a real (demo) call is about to appear — drop the sample preview
    setDemoStatus({ kind: 'starting' });
    try {
      await startAutoDemo();
      // The server-side call now streams into the monitor via the queue poll (POLL_MS). Clear the
      // transient "starting" note after a beat — by then the call has surfaced and taken over.
      setTimeout(() => setDemoStatus((s) => (s.kind === 'starting' ? { kind: 'idle' } : s)), 10000);
    } catch (e) {
      // 503 → LLM key missing / demo capacity reached. Show the backend's human-readable reason.
      const msg = e instanceof ApiError ? e.message : 'Could not start the demo call.';
      setDemoStatus({ kind: 'error', msg });
    }
  }, [demoStarting]);

  // Keep a ref so the detail-polling interval always sees the current selectedEpisodeId
  const selectedRef = useRef<string | null>(null);
  selectedRef.current = selectedEpisodeId;

  // Load the sample snapshot when toggle is turned on
  const loadSample = useCallback(async () => {
    if (sampleSnap) return; // already loaded
    setSampleLoading(true);
    try {
      const s = await fetchSampleCall();
      setSampleSnap(s);
    } catch {
      // /api/live/sample not available yet (404) or transient error — swallow; the toggle will
      // leave the empty state in place which is safe.
    } finally {
      setSampleLoading(false);
    }
  }, [sampleSnap]);

  const handleToggleSample = useCallback(() => {
    setShowSample((prev) => {
      const next = !prev;
      if (next) void loadSample();
      return next;
    });
  }, [loadSample]);

  useEffect(() => {
    let cancelled = false;

    const pollQueue = async () => {
      try {
        const q = await fetchActiveCalls();
        if (cancelled) return;
        setActiveCalls(q);
        setError(null);

        // Auto-select the first call if nothing is selected (or selected call left the queue)
        if (q.count > 0) {
          const ids = q.calls.map((c) => c.episode_id);
          setSelectedEpisodeId((prev) => {
            if (prev && ids.includes(prev)) return prev;
            return ids[0];
          });
        } else {
          setSelectedEpisodeId(null);
        }
      } catch {
        // /api/live/active not available yet (404) or transient network error — treat as no active
        // calls so the page degrades gracefully while the backend endpoint is being built.
        if (!cancelled) setActiveCalls({ count: 0, calls: [] });
      }
    };

    const pollDetail = async () => {
      const epId = selectedRef.current;
      // If no calls are active, clear the detail snap (show empty state / sample)
      if (!epId) {
        setSnap(null);
        return;
      }
      try {
        const s = await fetchLive(epId);
        if (!cancelled) {
          setSnap(s);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load the live call.');
      }
    };

    void pollQueue();
    void pollDetail();

    const queueId = setInterval(pollQueue, POLL_MS);
    const detailId = setInterval(pollDetail, POLL_MS);

    return () => {
      cancelled = true;
      clearInterval(queueId);
      clearInterval(detailId);
    };
  }, []);

  // Also re-fetch the detail immediately whenever the selected episode changes
  useEffect(() => {
    if (!selectedEpisodeId) return;
    let cancelled = false;
    void fetchLive(selectedEpisodeId).then((s) => {
      if (!cancelled) setSnap(s);
    }).catch(() => {/* ignore */});
    return () => { cancelled = true; };
  }, [selectedEpisodeId]);

  // N3: gate the shell's top-title LIVE pill on whether we're genuinely active (not sample)
  const titleActive = snap?.active === true && snap.sample !== true;
  useEffect(() => {
    const pill = document.querySelector<HTMLElement>('.top-title .live-pill');
    if (pill) pill.style.display = titleActive ? '' : 'none';
    return () => {
      const p = document.querySelector<HTMLElement>('.top-title .live-pill');
      if (p) p.style.display = '';
    };
  }, [titleActive]);

  const hasActiveCalls = (activeCalls?.count ?? 0) > 0;

  // --- ERROR STATE — only if the detail-polling also failed AND we have no queue info at all ---
  if (error && !snap && activeCalls === null) {
    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="ico">
            <Icon name="broadcast" size={28} />
          </div>
          <h3>Can&apos;t reach the call feed</h3>
          <p>{error}</p>
        </div>
      </div>
    );
  }

  // --- EMPTY STATE (no active calls) ---
  if (!hasActiveCalls) {
    // If toggle is ON and sample is loaded, show the sample monitor
    if (showSample && sampleSnap?.episode) {
      return (
        <div className="page" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          {/* Sample toggle banner */}
          <div className="lv-sample-banner">
            <span className="tag warn" style={{ fontWeight: 700, letterSpacing: '0.04em' }}>
              SAMPLE
            </span>
            <span style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
              Showing a sample call for preview — not a live call
            </span>
            <button
              className="btn btn-ghost btn-sm"
              onClick={handleToggleSample}
              style={{ marginLeft: 'auto' }}
            >
              Hide sample
            </button>
          </div>
          <CallMonitor snap={sampleSnap} onOpenReview={(id) => router.push(`/operate/review/${id}`)} />
        </div>
      );
    }

    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="ico">
            <Icon name="broadcast" size={28} />
          </div>
          <h3>No active call</h3>
          <p>Nothing is on the line right now. The monitor will light up the moment a call starts.</p>

          {/* "Run demo call" — POSTs /api/demo/auto/start to launch a SERVER-SIDE demo call. It runs
              on the backend and surfaces in the monitor via the queue poll a few seconds later (no
              navigation, and navigating away no longer kills it). */}
          <div className="lv-demo" data-testid="run-demo" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8, marginTop: 4 }}>
            <button
              className="btn btn-primary"
              onClick={handleRunDemo}
              disabled={demoStarting}
              aria-label="Run a demo call"
            >
              <Icon name="broadcast" size={16} />
              {demoStarting ? 'Starting demo…' : 'Run demo call'}
            </button>
            {demoStatus.kind !== 'idle' ? (
              <span style={{ fontSize: 12.5, color: demoStatus.kind === 'error' ? 'var(--danger)' : 'var(--text-3)' }}>
                {demoStatusText(demoStatus)}
              </span>
            ) : (
              <span style={{ fontSize: 12, color: 'var(--text-faint)' }}>
                Plays a real call into the monitor — no phone needed.
              </span>
            )}
          </div>

          {/* "Show sample call" toggle — lets operators preview the monitor with real data */}
          <div className="lv-sample-toggle" data-testid="sample-toggle">
            <span style={{ fontSize: 13, color: 'var(--text-2)', fontWeight: 550 }}>
              Show sample call
            </span>
            <button
              className={`toggle${showSample ? ' on' : ''}`}
              onClick={handleToggleSample}
              aria-label="Toggle sample call preview"
            >
              <i />
            </button>
            {sampleLoading ? (
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>Loading…</span>
            ) : null}
          </div>
        </div>
      </div>
    );
  }

  // --- CONNECTING STATE (active 0-turn call, no detail yet) ---
  if (snap?.active && snap.episode && snap.episode.turns.length === 0) {
    return (
      <div className="page" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        {activeCalls && activeCalls.count > 1 ? (
          <QueueRail
            calls={activeCalls.calls}
            selectedId={selectedEpisodeId}
            onSelect={setSelectedEpisodeId}
          />
        ) : null}
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="ico">
            <span className="live-pill">
              <i />
              LIVE
            </span>
          </div>
          <h3>Connecting…</h3>
          <p>A call just started. The transcript and belief state will appear here the moment the first turn lands.</p>
        </div>
      </div>
    );
  }

  // --- FULL MONITOR (active call with turns, or snap still loading) ---
  if (!snap?.episode) {
    // Queue says active but detail not loaded yet — brief loading interstitial
    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="ico">
            <span className="live-pill">
              <i />
              LIVE
            </span>
          </div>
          <h3>Connecting…</h3>
          <p>Loading call detail…</p>
        </div>
      </div>
    );
  }

  return (
    <div className="page" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      {/* Queue rail — shown when ≥2 calls are active so the operator can switch */}
      {activeCalls && activeCalls.count > 1 ? (
        <QueueRail
          calls={activeCalls.calls}
          selectedId={selectedEpisodeId}
          onSelect={setSelectedEpisodeId}
        />
      ) : null}
      <CallMonitor snap={snap} onOpenReview={(id) => router.push(`/operate/review/${id}`)} />
    </div>
  );
}

/** Horizontal queue rail of active calls. Shown when ≥2 calls are simultaneously active. */
function QueueRail({
  calls,
  selectedId,
  onSelect,
}: {
  calls: ActiveCallSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="lv-queue-rail" data-testid="queue-rail">
      <div className="lv-qhead">
        <span className="live-dot" style={{ marginRight: 4 }} />
        <span style={{ fontSize: 11.5, fontWeight: 700, color: 'var(--text-2)', letterSpacing: '0.04em', textTransform: 'uppercase' }}>
          {calls.length} Active {calls.length === 1 ? 'call' : 'calls'}
        </span>
      </div>
      <div className="lv-qlist scroll">
        {calls.map((c) => (
          <QueueEntry
            key={c.episode_id}
            call={c}
            selected={c.episode_id === selectedId}
            onSelect={onSelect}
          />
        ))}
      </div>
    </div>
  );
}

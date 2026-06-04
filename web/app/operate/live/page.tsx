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
// The call-duration timer ticks live (useNow, 1 s) off created_at for active calls.
// CB-39 (auto-follow, don't trap the operator): the transcript scroller auto-follows the newest turn
// ONLY while the operator is stuck to the bottom (within ~40px, tracked by an onScroll handler into
// stuckToBottomRef); if they scroll UP to read earlier turns on a long call, new turns no longer yank
// them down, and follow re-arms once they return to the bottom. The Auto-scroll toggle is the explicit
// override on top of this (OFF = never follow; turning it back ON jumps to bottom and re-arms).
// CB-12 (real-time streaming): for the SELECTED active call the page ALSO opens an SSE stream
// (GET /api/demo/auto/stream?episode_id=<id>) and re-fetches the detail snapshot on each per-turn
// event, so prospect/agent bubbles appear within sub-second of the server committing them instead of
// only on the 5 s poll (which stays as a fallback). The demo's prospect is now LLM-GENERATED (varied
// run-to-run), not a fixed script — handled server-side in demo_routes._run_auto_demo.
// CB-13 (call ended): when the selected call transitions active -> ended it does NOT auto-clear.
// The page FREEZES the last snapshot, renders a "Call ended — <outcome>" banner at the bottom of the
// transcript, and shows a "Back to live" control; the operator stays on the finished call until they
// explicitly dismiss it. The ended episode's detail persists because /api/live?episode_id=<id>
// returns {active:false, episode:<detail>} for a terminal call (transcript never vanishes).
// CB-21 (generation spinner + word streaming): the SSE also carries a "generating" event (emitted
// before the brain composes a turn) and the committed reply text on each "turn" event. ActiveAgentTurn
// renders a clearly-LABELED "Generating…" spinner during the compose window (a — so the ~6 s gap is
// never blank) and then REVEALS the reply word-by-word (b — useWordReveal) until the snapshot catches
// up. The reveal text is the REAL committed reply, shown incrementally on the demo SSE path.
// CB-31 (real-voice word streaming): for a REAL phone/web-voice call (no demo SSE), the voice worker
// streams the agent reply to TTS in word chunks and upserts the growing partial onto the live row
// (snap.live_partial). This page polls the DETAIL faster (~1 s via ACTIVE_DETAIL_POLL_MS) while a call
// is active, and ActiveAgentTurn renders snap.live_partial on the active turn (taking precedence over
// the spinner/dots), so the words fill in near-real-time and the partial clears once the turn commits.
// Both reuse the existing `.lv-speak` dots + inline styles (no new cadence.css tokens).
// CB-45 (live timing): when the voice worker is streaming a turn it also carries snap.live_timing
// (first-token + stream-elapsed). ActiveAgentTurn renders a small human timing readout in its header
// ("0.8s to first word · speaking 1.2s") beside the "speaking…" label — Cadence tokens, no raw ms key.
'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchActiveCalls, fetchLive, fetchSampleCall, fmtDuration, fmtFirstToken, fmtMsShort, initials } from '@/lib/operate-api';
import { archetypeLabel, versionLabel } from '@/lib/labels';
import type { ActiveCallSummary, ActiveCallsResponse, BeliefSnapshot, LiveSnapshot, LiveTiming, Turn } from '@/lib/operate-types';
import { ApiError, API_BASE, startAutoDemo } from '@/lib/api';

// Queue poll cadence. The 0-turn connect upsert (CB-32) makes a call appear the instant the room is
// up — but the page only SEES it on the next poll tick, so a slow cadence makes a fresh call show
// "after the first round" instead of on ring. 2 s surfaces a ringing call near-instantly while staying
// cheap when idle.
const POLL_MS = 2000;
// CB-31/CB-38: while a call is ACTIVE, poll the DETAIL snapshot fast so the agent reply the voice
// worker STREAMS to TTS (snap.live_partial) fills in word-by-word instead of snapping to the committed
// turn. A streamed turn lasts only ~1-2 s, so a 1 s poll often misses the partial window before the
// turn commits; 500 ms catches several partials per turn. Runs ONLY while a real call is active.
const ACTIVE_DETAIL_POLL_MS = 500;

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

/** CB-21(b): progressively reveal `text` word-by-word, restarting whenever `seq` changes (a new
 *  agent reply arrived). Returns the substring revealed so far + whether it is still revealing. A
 *  token-stream-like effect for the demo SSE path — the text is the REAL committed reply, just shown
 *  incrementally; not fabricated. ~45 ms/word, capped so a long reply never reveals for too long. */
function useWordReveal(text: string | null, seq: number): { shown: string; revealing: boolean } {
  const [count, setCount] = useState(0);
  const words = text ? text.split(/(\s+)/) : []; // keep whitespace tokens so spacing is preserved
  const wordCount = text ? text.split(/\s+/).filter(Boolean).length : 0;

  useEffect(() => {
    if (!text) {
      setCount(0);
      return;
    }
    setCount(0);
    let shownWords = 0;
    const stepMs = Math.max(20, Math.min(60, 900 / Math.max(1, wordCount))); // total reveal ≤ ~0.9s
    const id = setInterval(() => {
      shownWords += 1;
      setCount(shownWords);
      if (shownWords >= wordCount) clearInterval(id);
    }, stepMs);
    return () => clearInterval(id);
    // Restart only on a new reply (seq) — not on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seq, text]);

  if (!text) return { shown: '', revealing: false };
  // Rebuild the shown prefix from the word+whitespace token list up to `count` non-space words.
  let seen = 0;
  let out = '';
  for (const tok of words) {
    if (/\S/.test(tok)) {
      if (seen >= count) break;
      seen += 1;
    }
    out += tok;
  }
  return { shown: out, revealing: count < wordCount };
}

/** CB-21 / CB-31: the active agent turn at the bottom of a LIVE transcript. Priority order:
 *  (1) CB-31 livePartial — the REAL voice worker is streaming this turn's reply word-by-word to TTS
 *      (polled ~1 s via snap.live_partial); render the partial text directly as it grows, with a
 *      trailing caret. This is the real-phone/web-voice streaming path.
 *  (2) CB-21(b) streamReveal — the DEMO SSE path's just-committed reply, revealed word-by-word.
 *  (3) CB-21(a) generating — the brain is composing (demo SSE "generating"): labeled spinner.
 *  (4) idle live: bare pulsing dots between turns.
 *  Reuses the existing `.lv-speak` dots (cadence.css) — label + reveal/partial text are inline-styled,
 *  no new design tokens. */
function ActiveAgentTurn({
  generating,
  streamReveal,
  livePartial,
  liveTiming,
  lastAgentText,
}: {
  generating: boolean;
  streamReveal: { text: string; seq: number } | null;
  // CB-31: the in-progress reply being streamed to TTS on a real voice call (snap.live_partial).
  livePartial?: string | null;
  // CB-45: timing for the in-progress streamed turn (first-token + stream-elapsed), present only
  // while a partial is in flight; rendered as a small human readout in the header.
  liveTiming?: LiveTiming | null;
  // The text of the last committed agent turn in the snapshot — when it already equals the reveal
  // text, the snapshot has caught up so we stop revealing (no duplicate bubble).
  lastAgentText?: string | null;
}) {
  const { shown, revealing } = useWordReveal(streamReveal?.text ?? null, streamReveal?.seq ?? 0);
  // Suppress the reveal once the snapshot's last agent turn already shows this exact reply (caught up).
  const snapshotCaughtUp = !!streamReveal && lastAgentText === streamReveal.text;
  // While a fresh reply is still revealing AND the snapshot hasn't caught up, show the typed words;
  // otherwise the snapshot's full bubble already carries this turn, so we only need the spinner (when
  // generating) or the idle dots.
  const showReveal = !!streamReveal && revealing && shown.length > 0 && !snapshotCaughtUp;
  // CB-31: the live streamed partial takes precedence — it's the actual words being spoken right now
  // on a real call. Suppress it once the snapshot's last committed agent turn already equals it (the
  // turn committed; its full bubble now carries the text, so no duplicate).
  const showLivePartial =
    !!livePartial && livePartial.length > 0 && lastAgentText !== livePartial;

  // CB-45: live timing readout for the in-progress turn — "0.8s to first word" once the first token
  // has landed, then "· speaking 1.2s" as the reply streams. Both pieces are independently nullable
  // (first_token_ms can be null before the first token), so each clause is conditional; nothing shows
  // until there's a real number. Only meaningful while the turn is actually streaming (showLivePartial).
  const ftLabel = fmtFirstToken(liveTiming?.first_token_ms);
  const elapsed = fmtMsShort(liveTiming?.stream_elapsed_ms);
  const showLiveTiming = showLivePartial && (ftLabel !== '' || elapsed !== '');

  return (
    <div className="lv-turn a" data-testid="active-agent-turn">
      <div className="lv-th">
        Alex (agent)<span>·</span>
        {showLivePartial ? 'speaking…' : generating ? 'generating…' : 'now'}
        {showLiveTiming ? (
          <span
            className="lv-lat"
            data-testid="live-timing"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 5, marginLeft: 2 }}
          >
            <Icon name="bolt" size={11} style={{ color: 'var(--accent)' }} />
            {ftLabel}
            {ftLabel && elapsed ? <span style={{ opacity: 0.5 }}>·</span> : null}
            {elapsed ? `speaking ${elapsed}` : null}
          </span>
        ) : null}
      </div>
      {showLivePartial ? (
        // CB-31: the real voice reply streamed so far (the SAME text the brain decided, surfaced as
        // the worker speaks it) + a trailing caret. data-testid lets the e2e assert the partial path.
        <div className="lv-bub" data-testid="live-partial">
          {livePartial}
          <span aria-hidden style={{ opacity: 0.6 }}>▍</span>
        </div>
      ) : showReveal ? (
        // The reply text revealed so far (real committed text, shown incrementally) + a trailing caret.
        <div className="lv-bub" data-testid="stream-reveal">
          {shown}
          <span aria-hidden style={{ opacity: 0.6 }}>▍</span>
        </div>
      ) : generating ? (
        // Labeled generation indicator: the existing pulsing dots + an explicit "Generating…" caption
        // so it can't be mistaken for an idle/stuck call (CB-21a priority).
        <div
          className="row gap8"
          data-testid="generating-indicator"
          style={{ alignItems: 'center', alignSelf: 'flex-end' }}
        >
          <span style={{ fontSize: 11.5, fontWeight: 600, color: 'var(--text-3)' }}>Generating…</span>
          <div className="lv-bub lv-speak" aria-label="Agent is generating a reply">
            <i />
            <i />
            <i />
          </div>
        </div>
      ) : (
        // Idle live indicator (between turns / older backend with no "generating" event): bare dots.
        <div className="lv-bub lv-speak">
          <i />
          <i />
          <i />
        </div>
      )}
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

/** The detailed call monitor (transcript + belief column). Shared between real-live, sample, and the
 *  CB-13 ENDED (frozen) view. When `ended` is set, the call has gone terminal: the transcript shows a
 *  "Call ended — <outcome>" marker at the bottom and the action bar offers "Back to live" (it does NOT
 *  show any LIVE/recording affordance, and there is no live typing indicator). */
function CallMonitor({
  snap,
  onOpenReview,
  ended,
  onBackToLive,
  generating,
  streamReveal,
}: {
  snap: LiveSnapshot;
  onOpenReview: (id: string) => void;
  ended?: boolean;
  onBackToLive?: () => void;
  // CB-21(a): the agent is composing the next turn — render the labeled "Generating…" spinner.
  generating?: boolean;
  // CB-21(b): the latest committed reply text to reveal word-by-word on the active (demo) turn.
  streamReveal?: { text: string; seq: number } | null;
}) {
  // CB-31: the real voice worker's in-progress streamed reply for the active turn, carried on the
  // live snapshot (only present while active). Rendered on ActiveAgentTurn as the words it speaks.
  const livePartial = snap.live_partial ?? null;
  // CB-45: timing for that same in-progress turn (first-token + stream-elapsed), present only while
  // a partial streams. Rendered as a small human readout in the active turn's header.
  const liveTiming = snap.live_timing ?? null;
  const ep = snap.episode!;
  const lastBelief = [...ep.turns].reverse().find((t) => t.belief)?.belief ?? null;
  const escalationImminent = snap.priority?.escalation_imminent ?? lastBelief?.escalation_imminent ?? false;
  const lastAct = snap.priority?.last_act_label ?? null;
  // "Genuinely live" — active AND not a sample preview AND not the frozen ended view. Controls all
  // LIVE affordances (pill, dot, typing indicator).
  const isLive = snap.active === true && snap.sample !== true && !ended;
  const isSample = snap.sample === true;
  // The human outcome label the backend already translated (e.g. "Consultation booked"); shown only
  // in the ended marker. Falls back to a neutral phrase so the marker is never blank.
  const endedOutcome = ep.outcome && ep.outcome !== '—' ? ep.outcome : 'Call complete';

  // Live elapsed timer: an active call has no metrics.duration_ms until it ends, so for a live call
  // count up from created_at against a 1 s ticking clock; a completed/sample call uses the recorded
  // duration. created_at may be null (show "—" via fmtDuration(null)).
  const now = useNow();
  const startedMs = ep.created_at ? Date.parse(ep.created_at) : NaN;
  const durationMs =
    isLive && Number.isFinite(startedMs)
      ? now - startedMs
      : ((ep.metrics?.duration_ms as number | undefined) ?? null);

  // Auto-scroll (CB-39): "auto-follow" the newest turn, but NEVER yank the operator down while they
  // are reading earlier turns. The follow fires on a new turn ONLY when (a) the toggle is ON and
  // (b) the scroller is currently stuck to the bottom (within ~40px). An onScroll handler tracks that
  // stuck-to-bottom boolean in a ref (so it's read synchronously by the append-effect without an extra
  // render): the moment the operator scrolls up, follow pauses; when they scroll back to the bottom it
  // re-arms. The toggle is the operator's explicit override on top of this — OFF means never follow.
  const FOLLOW_THRESHOLD_PX = 40;
  const [autoScroll, setAutoScroll] = useState(true);
  const streamRef = useRef<HTMLDivElement>(null);
  // True while the scroller sits within FOLLOW_THRESHOLD_PX of the bottom. Starts true (transcript
  // mounts pinned to the newest turn). A ref (not state) so the append-effect below reads the live
  // value without resubscribing or causing a render on every scroll event.
  const stuckToBottomRef = useRef(true);

  const handleStreamScroll = useCallback(() => {
    const el = streamRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stuckToBottomRef.current = distanceFromBottom < FOLLOW_THRESHOLD_PX;
  }, []);

  useEffect(() => {
    // Follow only when the toggle is on AND the operator hasn't scrolled up. If they scrolled up,
    // leave their scroll position untouched so they can read earlier turns on a long call.
    if (!autoScroll || !stuckToBottomRef.current) return;
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [ep.turns.length, autoScroll, livePartial]);

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
              ) : ended ? (
                <span className="tag" style={{ marginLeft: 10, fontWeight: 700, letterSpacing: '0.04em' }}>
                  Call ended
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
                  onClick={() =>
                    setAutoScroll((v) => {
                      const next = !v;
                      // Turning follow back ON re-arms it: jump to the newest turn and mark stuck so
                      // the next append keeps following (otherwise enabling it while scrolled up would
                      // do nothing until the operator manually returned to the bottom).
                      if (next) {
                        const el = streamRef.current;
                        if (el) el.scrollTop = el.scrollHeight;
                        stuckToBottomRef.current = true;
                      }
                      return next;
                    })
                  }
                >
                  <i />
                </span>
              </span>
            </div>
            <div className="lv-stream scroll" ref={streamRef} onScroll={handleStreamScroll}>
              {ep.turns.map((t) => (
                <TranscriptTurn key={t.turn_id} turn={t} />
              ))}
              {/* CB-21: the active-turn indicator on the agent side. While the brain is COMPOSING
                  (generating), show a clearly-LABELED "Generating…" spinner so the ~6 s compose window
                  is never a blank gap (a). Once a turn's text arrives, REVEAL it word-by-word (b) until
                  the snapshot's full bubble takes over. Falls back to the bare pulsing dots when live
                  but no explicit generation signal (older backend without the SSE "generating" event). */}
              {isLive ? (
                <ActiveAgentTurn
                  generating={!!generating}
                  streamReveal={streamReveal ?? null}
                  livePartial={livePartial}
                  liveTiming={liveTiming}
                  lastAgentText={
                    [...ep.turns].reverse().find((t) => t.speaker === 'agent')?.text ?? null
                  }
                />
              ) : null}
              {/* CB-13: an unmistakable end-of-call marker at the BOTTOM of the transcript. Shown
                  only on the frozen ended view (never on a live/sample call). The outcome is the
                  backend-humanized label — no raw outcome slug renders here. */}
              {ended ? (
                <div
                  className="lv-ended-marker"
                  data-testid="call-ended-marker"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: 8,
                    margin: '16px auto 6px',
                    padding: '10px 16px',
                    borderRadius: 999,
                    border: '1px solid var(--border)',
                    background: 'var(--surface-2, rgba(255,255,255,0.04))',
                    fontSize: 13,
                    fontWeight: 650,
                    color: 'var(--text-2)',
                    width: 'fit-content',
                  }}
                >
                  <Icon name="check" size={15} style={{ color: 'var(--ok)' }} />
                  {`Call ended — ${endedOutcome}`}
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
            : ended
              ? `Call ended · ${endedOutcome}`
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
          {/* CB-13: on the frozen ended view, the primary control returns the operator to the live
              queue (it does NOT auto-revert) instead of the disabled "Take over". */}
          {ended ? (
            <button
              className="btn btn-primary btn-lg"
              data-testid="back-to-live"
              onClick={() => onBackToLive?.()}
            >
              <Icon name="broadcast" size={17} />
              Back to live
            </button>
          ) : (
            /* Operator live-takeover (barging into the live LiveKit room as a human) isn't wired in
               this build, so the control is honestly disabled rather than a click-that-does-nothing. */
            <button
              className="btn btn-primary btn-lg"
              disabled
              title="Live takeover isn't available in this build — the monitor is view-only. Interact via the demo console or the phone line."
            >
              <Icon name="hand" size={17} />
              Take over
            </button>
          )}
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
  // CB-13: the FROZEN snapshot of a call the operator was watching when it went terminal. While set,
  // the page shows that finished call (transcript + "Call ended" marker) and does NOT auto-revert to
  // the empty/queue state — the operator dismisses it explicitly via "Back to live" (handleBackToLive).
  const [endedSnap, setEndedSnap] = useState<LiveSnapshot | null>(null);
  // Sample toggle state — only active when no real calls
  const [showSample, setShowSample] = useState(false);
  const [sampleSnap, setSampleSnap] = useState<LiveSnapshot | null>(null);
  const [sampleLoading, setSampleLoading] = useState(false);

  // CB-21(a): the agent is COMPOSING the next turn — set by the SSE "generating" event (emitted
  // before the ~6 s brain round-trip) and cleared by the matching "turn" event (or when the call
  // ends / selection changes). Drives a clear "Generating…" spinner on the active turn so there is
  // never a multi-second blank gap. Keyed nowhere — it always refers to the SELECTED call's stream.
  const [generating, setGenerating] = useState(false);
  // CB-21(b): the agent's freshly-committed reply text from the latest "turn" event, revealed
  // word-by-word on the demo SSE path (a token-stream-like effect) before the snapshot's full bubble
  // takes over. `seq` bumps each turn so the reveal animation restarts for each new reply.
  const [streamReveal, setStreamReveal] = useState<{ text: string; seq: number } | null>(null);

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

  // CB-13 bookkeeping (refs so the polling closures see the latest without re-subscribing):
  //  - seenActiveRef: episode ids we have observed as ACTIVE this session — the precondition for an
  //    active->ended freeze (we never "freeze" a call we never watched live).
  //  - dismissedEndedRef: ended calls the operator already dismissed via "Back to live" — so a stale
  //    in-progress->terminal row that lingers in a recent-episodes scan can't re-freeze the page.
  //  - frozenIdRef: the episode currently frozen (mirrors endedSnap) so the poll closure can read it.
  const seenActiveRef = useRef<Set<string>>(new Set());
  const dismissedEndedRef = useRef<Set<string>>(new Set());
  const frozenIdRef = useRef<string | null>(null);
  frozenIdRef.current = endedSnap?.episode?.episode_id ?? null;

  // CB-13: dismiss the frozen ended call and return to the live queue. The dismissed id is remembered
  // so the next poll cannot immediately re-freeze the same finished call.
  const handleBackToLive = useCallback(() => {
    const id = frozenIdRef.current;
    if (id) dismissedEndedRef.current.add(id);
    setEndedSnap(null);
  }, []);

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

        const ids = q.calls.map((c) => c.episode_id);
        // Remember every call seen active this session (the freeze precondition below).
        for (const id of ids) seenActiveRef.current.add(id);

        // CB-13: did the call the operator is WATCHING just leave the active queue? If we saw it
        // active before and it isn't currently frozen/dismissed, FREEZE its last snapshot instead of
        // reverting to empty. We fetch /api/live?episode_id=<id>, which returns the now-terminal
        // episode's full detail with active:false (the transcript persists), and pin it as endedSnap.
        const watching = selectedRef.current;
        if (
          watching &&
          !ids.includes(watching) &&
          seenActiveRef.current.has(watching) &&
          frozenIdRef.current === null &&
          !dismissedEndedRef.current.has(watching)
        ) {
          // Claim the freeze slot optimistically so a second poll tick (the fetch below is async)
          // can't double-fetch the same ended call.
          frozenIdRef.current = watching;
          try {
            const ended = await fetchLive(watching);
            // Only freeze a real finished call (it has a transcript + is no longer active). A 0-turn
            // stale dial returns {active:false, episode:null} — nothing to freeze, so release the
            // optimistic claim and fall through to the normal (auto-select/empty) behavior.
            if (!cancelled && ended.episode && ended.active !== true && ended.episode.turns.length > 0) {
              setEndedSnap(ended);
            } else {
              frozenIdRef.current = null;
            }
          } catch {
            // couldn't fetch the ended detail — release the claim and degrade gracefully.
            frozenIdRef.current = null;
          }
        }

        // Auto-select the first call if nothing is selected (or selected call left the queue)
        if (q.count > 0) {
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

  // CB-12 (real-time streaming): subscribe to the server's per-turn SSE for the SELECTED call so each
  // prospect/agent bubble appears within sub-second of the server committing it — not only on the 5 s
  // poll. On each "turn" event we re-fetch the detail snapshot (cheap, and reuses the exact same
  // rendering path as the poll, so the SSE never assembles turns client-side or diverges from the
  // poll). The 5 s poll stays as a fallback if SSE is unavailable (older backend / proxy buffering).
  // Skipped while a call is frozen-ended (endedSnap) — a finished call has nothing left to stream.
  useEffect(() => {
    if (!selectedEpisodeId || endedSnap) return;
    if (typeof window === 'undefined' || typeof EventSource === 'undefined') return;

    let cancelled = false;
    let refreshing = false;
    const epId = selectedEpisodeId;
    const url = `${API_BASE}/api/demo/auto/stream?episode_id=${encodeURIComponent(epId)}`;
    let es: EventSource | null = null;

    // Pull the latest detail when the server signals a new turn. Guarded so overlapping events
    // collapse into one in-flight fetch (no request pile-up under a burst).
    const refresh = async () => {
      if (cancelled || refreshing) return;
      refreshing = true;
      try {
        const s = await fetchLive(epId);
        if (!cancelled) {
          setSnap(s);
          setError(null);
        }
      } catch {
        // transient — the 5 s poll will reconcile.
      } finally {
        refreshing = false;
      }
    };

    try {
      es = new EventSource(url);
      // CB-21(a): the agent started composing the next turn — show the "Generating…" spinner until
      // the matching "turn" event lands (so the ~6 s brain compose window is never a blank gap).
      es.addEventListener('generating', () => {
        if (!cancelled) setGenerating(true);
      });
      // The backend emits a named "turn" event per committed turn (CB-12) carrying the just-composed
      // reply text (CB-21b). Clear the spinner, kick a word-by-word reveal of that reply, then
      // re-fetch the detail snapshot (the durable source of truth) which replaces the reveal.
      es.addEventListener('turn', (ev) => {
        if (cancelled) return;
        setGenerating(false);
        try {
          const data = JSON.parse((ev as MessageEvent).data || '{}');
          if (typeof data.text === 'string' && data.text) {
            setStreamReveal((prev) => ({ text: data.text, seq: (prev?.seq ?? 0) + 1 }));
          }
        } catch {
          /* malformed event data — the snapshot refresh below still renders the turn */
        }
        void refresh();
      });
      es.addEventListener('end', () => {
        // Refresh once more so the final turn is on screen, then let the queue poll handle the
        // active->ended freeze (CB-13). Close the stream — nothing more will arrive.
        if (!cancelled) setGenerating(false);
        void refresh();
        es?.close();
      });
      // onerror fires on close/network hiccup; EventSource auto-reconnects, but if the endpoint is
      // unavailable entirely we close to avoid a reconnect storm — the poll remains the fallback.
      es.onerror = () => {
        if (es && es.readyState === EventSource.CLOSED) es = null;
      };
    } catch {
      es = null; // EventSource construction failed — rely on the poll.
    }

    return () => {
      cancelled = true;
      setGenerating(false);
      setStreamReveal(null);
      es?.close();
    };
  }, [selectedEpisodeId, endedSnap]);

  // CB-31: a fast (~1 s) detail poll while a REAL call is on the line, so the voice worker's streamed
  // partial reply (snap.live_partial) fills in word-by-word in near-real-time instead of waiting for
  // the 5 s poll. Active only while the selected call is genuinely active and not frozen-ended; it
  // tears down (back to the cheap 5 s cadence) the moment the call ends or no call is selected. The
  // 5 s pollDetail above stays as the always-on fallback; this just adds finer-grained refreshes.
  const callActive = snap?.active === true && snap.sample !== true && !endedSnap;
  useEffect(() => {
    if (!selectedEpisodeId || !callActive) return;
    let cancelled = false;
    let refreshing = false;
    const epId = selectedEpisodeId;
    const fastPoll = async () => {
      if (cancelled || refreshing) return;
      refreshing = true;
      try {
        const s = await fetchLive(epId);
        if (!cancelled) {
          setSnap(s);
          setError(null);
        }
      } catch {
        // transient — the 5 s poll reconciles.
      } finally {
        refreshing = false;
      }
    };
    const id = setInterval(fastPoll, ACTIVE_DETAIL_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [selectedEpisodeId, callActive]);

  // N3: gate the shell's top-title LIVE pill on whether we're genuinely active (not sample, and not
  // showing the frozen ended view — a finished call is never "live").
  const titleActive = endedSnap === null && snap?.active === true && snap.sample !== true;
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

  // --- CB-13: FROZEN ENDED CALL (takes precedence — the monitor must NOT auto-clear) ---
  // The call the operator was watching went terminal. Keep it on screen (transcript + "Call ended"
  // marker) until the operator explicitly returns to the live queue. We render the queue rail above
  // it when other calls are still active so the operator can switch away if they want to.
  if (endedSnap?.episode) {
    return (
      <div className="page" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        {activeCalls && activeCalls.count > 1 ? (
          <QueueRail
            calls={activeCalls.calls}
            selectedId={selectedEpisodeId}
            onSelect={(id) => {
              // Switching to another call dismisses the frozen view and resumes live watching.
              handleBackToLive();
              setSelectedEpisodeId(id);
            }}
          />
        ) : null}
        <CallMonitor
          snap={endedSnap}
          ended
          onBackToLive={handleBackToLive}
          onOpenReview={(id) => router.push(`/operate/review/${id}`)}
        />
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
      <CallMonitor
        snap={snap}
        generating={generating}
        streamReveal={streamReveal}
        onOpenReview={(id) => router.push(`/operate/review/${id}`)}
      />
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

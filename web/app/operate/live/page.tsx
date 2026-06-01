// P1 — Live Call Monitor (U15). Watches the live (or most-recent) call and, per the IA spec,
// PRIORITIZES the belief-state signals: Trust + Walk-away risk + current stage/last act +
// escalation-imminent are FOREMOST (the two gauges, the stage box, the amber escalation strip, and
// the current-decision card sit at the top of the belief column); the full slot table + latent
// drivers are the secondary "Full belief state" expander below. No realtime infra — it polls
// /api/live every few seconds and shows the most-recent call, with a clear "no active call" empty
// state AND a "Connecting…" state for an active call that has no turns yet (so a just-started call
// never renders as a wall of dashes). Belief/stage/act labels are pre-translated by the backend; the
// raw persona archetype + version slugs are humanized client-side via @/lib/labels (archetypeLabel/
// versionLabel) and the raw cohort id (`coh-…`) is dropped — no internal index reaches the operator.
// N3 FIX: every "LIVE" affordance (transcript-card pill, footer
// recording dot, and the shell top-title pill the layout renders statically) is now gated on the
// REAL snap.active flag — when a COMPLETED call is shown (active:false) the surface reads "Most
// recent call" with no live/recording treatment, instead of contradicting the not-live footer.
'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { Icon } from '@/components/cadence/Icon';
import { fetchLive, fmtDuration, initials } from '@/lib/operate-api';
import { archetypeLabel, versionLabel } from '@/lib/labels';
import type { BeliefSnapshot, LiveSnapshot, Turn } from '@/lib/operate-types';

const POLL_MS = 5000;

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
      {isAgent && turn.rationale ? <div className="lv-rat">{`“${turn.rationale}”`}</div> : null}
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

export default function LivePage() {
  const router = useRouter();
  const [snap, setSnap] = useState<LiveSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const s = await fetchLive();
        if (!cancelled) {
          setSnap(s);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load the live call.');
      }
    };
    void load();
    const id = setInterval(load, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // The shared shell (DashboardShell) renders a STATIC "LIVE" pill in the top-title for this route,
  // independent of whether a call is actually on the line — which contradicts the not-live footer
  // when a COMPLETED call is shown. The shell keys the pill off static nav config (not our snapshot),
  // so we reconcile it here from the page that owns the real signal: hide the title pill whenever
  // we're not genuinely active, and restore it (and the shell on unmount) otherwise. Toggling
  // visibility is layout-preserving and reversible; we never remove the node.
  const titleActive = snap?.active === true;
  useEffect(() => {
    const pill = document.querySelector<HTMLElement>('.top-title .live-pill');
    if (pill) pill.style.display = titleActive ? '' : 'none';
    return () => {
      // Leaving the page: restore the shell's pill to its default so other routes are unaffected.
      const p = document.querySelector<HTMLElement>('.top-title .live-pill');
      if (p) p.style.display = '';
    };
  }, [titleActive]);

  if (error && !snap) {
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

  if (!snap || !snap.episode) {
    return (
      <div className="page">
        <div className="empty" style={{ margin: 'auto' }}>
          <div className="ico">
            <Icon name="broadcast" size={28} />
          </div>
          <h3>No active call</h3>
          <p>Nothing is on the line right now. The monitor will light up the moment a call starts.</p>
        </div>
      </div>
    );
  }

  // A live call that hasn't produced a turn yet is CONNECTING — show a clean connecting state
  // instead of the full monitor rendered as a wall of "—" (no transcript, no belief yet).
  if (snap.active && snap.episode.turns.length === 0) {
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
          <p>A call just started. The transcript and belief state will appear here the moment the first turn lands.</p>
        </div>
      </div>
    );
  }

  const ep = snap.episode;
  const lastBelief = [...ep.turns].reverse().find((t) => t.belief)?.belief ?? null;
  const escalationImminent = snap.priority?.escalation_imminent ?? lastBelief?.escalation_imminent ?? false;
  const lastAct = snap.priority?.last_act_label ?? null;

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
                {/* Humanize the persona archetype; the raw cohort id (`coh-…`) is an internal grouping
                    index with no human label, so it's dropped from the operator-facing chips. */}
                {ep.persona ? <span className="tag accent">{archetypeLabel(ep.persona)}</span> : null}
                <span className="tag">{ep.channel === 'voice' ? 'Web-voice' : 'Text'}</span>
              </div>
            </div>
            <div className="lv-pright">
              <div className="lv-dur">{fmtDuration((ep.metrics?.duration_ms as number) ?? null)}</div>
              <div className="lv-vtag">
                {versionLabel(ep.version)} · {ep.kb_version}
              </div>
            </div>
          </div>

          <div className="card lv-tcard">
            <div className="card-head">
              <h3>Transcript</h3>
              {/* LIVE pill ONLY when the call is genuinely on the line; otherwise a neutral
                  "Most recent call" tag so a COMPLETED call never wears a live treatment (N3). */}
              {snap.active ? (
                <span className="live-pill" style={{ marginLeft: 10 }}>
                  <i />
                  LIVE
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
                <span className="toggle on">
                  <i />
                </span>
              </span>
            </div>
            <div className="lv-stream scroll">
              {ep.turns.map((t) => (
                <TranscriptTurn key={t.turn_id} turn={t} />
              ))}
              {snap.active ? (
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
          {/* Pulsing red recording dot ONLY while active; a static neutral dot when showing a
              completed call, so the footer dot doesn't read as "recording now" off-air (N3). */}
          <span
            className="rd"
            style={snap.active ? undefined : { background: 'var(--text-3)', animation: 'none' }}
          />
          {snap.active ? `Recording · Turn ${ep.turn_count}` : 'Most recent call (not live)'}
        </span>
        <div className="lv-tl">
          <span style={{ fontSize: 11, fontWeight: 650, color: 'var(--text-3)' }}>Last act</span>
          <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)' }}>{lastAct ?? '—'}</span>
        </div>
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 10 }}>
          <button className="btn btn-ghost" onClick={() => router.push(`/operate/review/${ep.episode_id}`)}>
            <Icon name="eye" size={16} />
            Open review
          </button>
          <button className="btn btn-primary btn-lg" disabled={!snap.active} title={snap.active ? 'Take over the live call' : 'No active call to take over'}>
            <Icon name="hand" size={17} />
            Take over
          </button>
        </div>
      </div>
    </div>
  );
}

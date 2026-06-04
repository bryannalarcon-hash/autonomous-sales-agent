// Operator-dashboard (Operate mode, U15) API contract — mirrors the JSON the FastAPI Operate read
// router (src/api/operate.py) returns for /api/episodes, /api/episodes/{id}, /api/kpis,
// /api/escalations, /api/live, /api/live/active, and /api/live/sample. Keep in lockstep with that
// module. The backend has already translated every internal index (ladder tier ints, driver enum
// slugs, stage/act slugs, escalation reasons) into human-readable *_label fields; the raw *_key
// fields are internal-only and MUST NOT render in operator-facing text — the UI shows the labels.
// Additions (U16 live-monitor): LiveSnapshot gains optional `sample` flag; ActiveCallSummary +
// ActiveCallsResponse support the queue-rail of concurrent in-progress calls (/api/live/active).
// Additions (CB-06): EpisodeDetail gains optional prospect_trajectory for self-play / digital-twin
// episodes — the prospect's true hidden driver state captured per turn by the on_turn hook.
// Additions (CB-30): EpisodeSummary/EpisodeDetail gain optional `golden` (the calibration-set flag);
// the golden set is fetched via fetchGoldenEpisodes and toggled via setEpisodeGolden in operate-api.
// lib/operate-api.ts types against these.
// Additions (CB-45, against CB-44's LOCKED API contract): per-agent-turn voice timing on Turn.timing
// (audio-in → first-token → stream-end, all nullable on text/legacy turns); LiveSnapshot.live_timing
// (first-token + stream-elapsed, present ONLY while a partial streams); and EpisodeSummary
// avg_first_token_ms / avg_stream_ms (means over agent turns that carry timing). All are number|null
// and rendered as human phrases (e.g. "0.8s to first word") — never as raw ms keys.

/** One belief-state driver signal at a turn, already labeled (e.g. "Walk-away risk"). */
export interface DriverSignal {
  key: string; // internal slug (e.g. "bail_risk") — never rendered
  label: string; // human label (e.g. "Walk-away risk") — what the UI shows
  value: number; // 0..1
}

/** One discovery slot at a turn. */
export interface SlotSignal {
  key: string;
  label: string;
  value: unknown;
  confidence: number; // 0..1
}

/** The agent's factored belief state at one turn, with the prioritized signals split out. */
export interface BeliefSnapshot {
  stage: string | null;
  active_objection: string | null;
  decision_confidence: number | null;
  escalation_imminent: boolean;
  /** trust + walk-away risk, in priority order — the Live monitor's foremost signals. */
  primary_drivers: DriverSignal[];
  /** everything else (price sensitivity, urgency, …) — the "full belief state" expander. */
  secondary_drivers: DriverSignal[];
  slots: SlotSignal[];
  trends: Record<string, number>;
}

/** CB-44/CB-45: voice-call timing for ONE agent turn (audio-in → first NLG token → stream-end).
 *  Every field is nullable: text turns, legacy turns, and turns recorded before CB-44 carry nulls
 *  (the UI shows "—" / omits the chip — it never derives a label from these raw ms numbers). */
export interface TurnTiming {
  /** ms from the user's final STT landing to the first streamed NLG token (the felt "dead air"). */
  audio_to_first_token_ms: number | null;
  /** ms the brain took to emit its first token after starting to compose. */
  first_token_ms: number | null;
  /** ms spent streaming the reply (first token → stream end) — roughly how long the agent "spoke". */
  stream_duration_ms: number | null;
  /** ms end-to-end from audio-in to stream-end. */
  total_ms: number | null;
}

/** One transcript turn + its decision trace (agent turns carry decision_label + rationale). */
export interface Turn {
  turn_id: number;
  speaker: 'agent' | 'prospect' | 'system';
  text: string;
  decision_label: string | null; // labeled act (e.g. "Trial close") — null on prospect turns
  rationale: string | null;
  latency_ms: number | null;
  belief: BeliefSnapshot | null;
  // CB-28: the KB facts ("[source] text") that grounded a tool-use answer; null on non-tool turns.
  // When present, the Review page makes the turn clickable into a panel of what was pulled.
  retrieved?: string[] | null;
  // CB-44/CB-45: per-turn voice timing (null on text/legacy turns). The Review page renders it as
  // first-token / spoke-for chips beside the decision trace; null timing shows nothing (no crash).
  timing?: TurnTiming | null;
}

/** A summary row for the Calls list (P3) / Live tile — no transcript body. */
export interface EpisodeSummary {
  episode_id: string;
  outcome: string; // labeled (e.g. "Enrolled")
  outcome_key: string | null; // internal key — used to map a filter selection, not displayed
  ladder_tier: number;
  ladder_label: string; // labeled (e.g. "Same-call enrollment")
  qualified: boolean;
  version: string;
  kb_version: string;
  channel: string;
  persona: string | null;
  cohort: string | null;
  escalated: boolean;
  turn_count: number;
  duration_ms: number | null;
  /** CB-30: true when this call is tagged into the golden calibration set (metrics['golden']). The
   *  Review page shows a golden indicator + a "Mark golden / Golden ✓" toggle that flips this. */
  golden?: boolean;
  /** CB-44/CB-45: mean time-to-first-token across this call's agent turns that carry timing, in ms;
   *  null when no turn has timing (text/legacy call). Surfaced as "0.8s to first word" in the UI. */
  avg_first_token_ms?: number | null;
  /** CB-44/CB-45: mean stream duration across this call's timed agent turns, in ms; null when none. */
  avg_stream_ms?: number | null;
  created_at: string | null;
}

/** One entry in the prospect's true-driver trajectory (self-play / digital-twin episodes only).
 *  Captured by the on_turn hook in selfplay.py and persisted in episode.metrics.
 *  drivers keys: trust, need, urgency, purchase_intent, budget, patience (prospect-side naming). */
export interface ProspectTurnState {
  turn: number;
  drivers: Record<string, number>;
}

/** The full Call Review payload (P2): summary + per-turn trace + belief trajectory.
 *  prospect_trajectory is [] for real voice/text calls — the panel must be ABSENT in that case. */
export interface EpisodeDetail extends EpisodeSummary {
  turns: Turn[];
  belief_trajectory: (BeliefSnapshot | null)[];
  disqualifier_reason: string | null;
  metrics: Record<string, unknown>;
  /** Per-turn prospect true-driver states (self-play / twin only). Empty array for real calls. */
  prospect_trajectory: ProspectTurnState[];
}

export interface EpisodeListResponse {
  episodes: EpisodeSummary[];
  count: number;
}

/** CB-44/CB-45: live timing for the turn currently STREAMING on a real voice call. Present on the
 *  snapshot ONLY while a partial is in flight (mirrors live_partial); null/absent otherwise. The
 *  monitor shows it near the active agent turn as "0.8s to first word · speaking 1.2s". */
export interface LiveTiming {
  /** ms to the first NLG token of the in-progress turn (null until the first token lands). */
  first_token_ms: number | null;
  /** ms elapsed since the first token — how long the agent has been speaking this turn so far. */
  stream_elapsed_ms: number | null;
}

/** The P1 Live monitor snapshot: the most-recent call + the hoisted prioritized belief IA. */
export interface LiveSnapshot {
  active: boolean;
  /** True when this snapshot was synthesized from a completed call as a preview/demo; the monitor
   *  must display a "SAMPLE" badge and suppress all LIVE/recording affordances. */
  sample?: boolean;
  /** CB-31: the in-progress (not-yet-committed) agent reply being STREAMED to TTS on a real voice
   *  call — present ONLY while active, so the monitor renders the words filling in on the active
   *  turn near-real-time. null/absent once the turn commits or on an inactive/sample call. */
  live_partial?: string | null;
  /** CB-44/CB-45: timing for that same in-progress streamed turn (first-token + stream-elapsed);
   *  present only while a partial streams, null/absent otherwise. */
  live_timing?: LiveTiming | null;
  episode: EpisodeDetail | null;
  priority?: {
    trust: number | null;
    bail_risk: number | null;
    stage: string | null;
    last_act_label: string | null;
    escalation_imminent: boolean;
  };
}

/** One entry in the active-calls queue — enough to populate the queue rail without the full
 *  transcript. Humanized labels come from the backend; persona_label is already human-readable. */
export interface ActiveCallSummary {
  episode_id: string;
  channel: 'voice' | 'text';
  /** Pre-humanized persona archetype label, or null if the call has no persona yet. */
  persona_label: string | null;
  /** Current conversation stage (already labeled), or null if not yet established. */
  stage: string | null;
  trust: number | null;
  bail_risk: number | null;
  turn_count: number;
  escalation_imminent: boolean;
  started_at: string | null;
}

/** Response from GET /api/live/active — the full set of currently in-progress calls. */
export interface ActiveCallsResponse {
  count: number;
  calls: ActiveCallSummary[];
}

export interface LadderDistRow {
  tier: number;
  label: string;
  count: number;
  rate: number; // 0..1
}

export interface ArchetypeConvRow {
  label: string;
  conversion: number; // 0..1
  n: number;
}

/** The champion-vs-baseline compare arm (P4 compare mode). */
export interface KpiCompare {
  baseline_ladder_score: number;
  champion_ladder_score: number;
  delta: number;
  delta_ci: [number, number];
  challenger_better: boolean;
  baseline_enrollment_rate: number;
  champion_enrollment_rate: number;
  n_baseline: number;
  n_champion: number;
}

/** P4 KPI payload. The two headline numbers are DISTINCT: weighted_ladder_score (ladder mean) and
 *  enrollment_rate (share of calls that closed at enrollment) — never the same metric. */
export interface KpiResponse {
  n: number;
  weighted_ladder_score: number; // headline 1 — on the 0..ladder_max scale
  ladder_max: number;
  enrollment_rate: number; // headline 2 — DISTINCT, on a 0..1 scale
  qualification_accuracy: number;
  escalation_rate: number;
  ladder_distribution: LadderDistRow[];
  archetype_conversion: ArchetypeConvRow[];
  compare?: KpiCompare;
  version: string | null;
  cohort: string | null;
  compare_version: string | null;
}

/** One escalation in the review queue (P5). */
export interface Escalation {
  escalation_id: string;
  episode_id: string;
  reason: string; // labeled (e.g. "Pricing concession")
  reason_key: string | null; // internal key — not displayed
  moment: string | null;
  turn_id: number | null;
  lifecycle: 'unreviewed' | 'reviewed' | 'resolved' | 'dismissed';
  lifecycle_label: string;
  created_at: string | null;
}

export interface EscalationListResponse {
  escalations: Escalation[];
  count: number;
  counts: { unreviewed: number; reviewed: number; resolved: number; dismissed: number };
}

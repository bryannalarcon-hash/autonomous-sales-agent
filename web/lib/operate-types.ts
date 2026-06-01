// Operator-dashboard (Operate mode, U15) API contract — mirrors the JSON the FastAPI Operate read
// router (src/api/operate.py) returns for /api/episodes, /api/episodes/{id}, /api/kpis,
// /api/escalations, /api/live. Keep in lockstep with that module. The backend has already translated
// every internal index (ladder tier ints, driver enum slugs, stage/act slugs, escalation reasons)
// into human-readable *_label fields; the raw *_key fields are internal-only and MUST NOT render in
// operator-facing text — the UI shows the labels. lib/operate-api.ts types against these.

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

/** One transcript turn + its decision trace (agent turns carry decision_label + rationale). */
export interface Turn {
  turn_id: number;
  speaker: 'agent' | 'prospect' | 'system';
  text: string;
  decision_label: string | null; // labeled act (e.g. "Trial close") — null on prospect turns
  rationale: string | null;
  latency_ms: number | null;
  belief: BeliefSnapshot | null;
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
  created_at: string | null;
}

/** The full Call Review payload (P2): summary + per-turn trace + belief trajectory. */
export interface EpisodeDetail extends EpisodeSummary {
  turns: Turn[];
  belief_trajectory: (BeliefSnapshot | null)[];
  disqualifier_reason: string | null;
  metrics: Record<string, unknown>;
}

export interface EpisodeListResponse {
  episodes: EpisodeSummary[];
  count: number;
}

/** The P1 Live monitor snapshot: the most-recent call + the hoisted prioritized belief IA. */
export interface LiveSnapshot {
  active: boolean;
  episode: EpisodeDetail | null;
  priority?: {
    trust: number | null;
    bail_risk: number | null;
    stage: string | null;
    last_act_label: string | null;
    escalation_imminent: boolean;
  };
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

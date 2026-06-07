// Operator-dashboard (Improve mode, U16) API contract — mirrors the JSON the FastAPI Improve router
// (src/api/improve.py) returns for /api/experiments, /api/experiments/run (ASYNC — 202 + a `running`
// record, CB-15; multi-metric via `changes`, CB-27), /api/experiments/scaffold (seed a draft from a
// reviewed call, CB-19), /api/experiments/{id} (one experiment's detail + its A/B mock calls per arm,
// CB-25), /api/approvals(+approve/reject), /api/kb, /api/playbook, /api/versions(+rollback). Keep in
// lockstep with that module. The backend has already translated every internal index (dimension slug ->
// dimension_label, lifecycle state -> state_label) into human-readable fields; the raw
// `dimension`/`state` slugs are kept ONLY for client branching (chip color, action gating) and MUST
// NOT render in operator text — the UI shows the *_label fields.
// CB-57: RunExperimentResponse includes effective_n/n_min/n_max so the dialog quotes the n that will
// actually run (after the backend's floor/cap), never the raw requested value.
// lib/improve-api.ts types against these.

/** One persisted champion-vs-challenger experiment (P6 lab / P7 approvals). */
export interface Experiment {
  experiment_id: string;
  name: string;
  champion_version: string | null; // the parent (champion) the challenger forked from
  challenger_version: string;
  // before/after comparison (the demo artifact).
  champion_kpi: number;
  challenger_kpi: number;
  delta: number; // challenger − champion (ladder KPI)
  delta_ci: [number, number]; // delta CI excludes 0 => challenger_better
  challenger_better: boolean;
  enroll_delta: number; // same-call enrollment delta (distinct from the ladder delta)
  significance: number; // 0..1
  // guardrail verdict drives the chip color; the reason explains a trip/warn.
  guardrail: 'pass' | 'warn' | 'trip';
  guardrail_reason: string | null;
  champion_qual_acc: number;
  challenger_qual_acc: number;
  // the single declared diff (R17): raw slug for logic, label for display.
  dimension: string; // internal slug — never rendered
  dimension_label: string; // human label — what the UI shows
  declared_diff: string[];
  diff_description: string;
  is_extreme: boolean;
  population: string;
  n: number;
  target: number;
  kb_version: string;
  // lifecycle: raw `state` for branching + a human label for the chip.
  state: 'draft' | 'running' | 'passed' | 'blocked' | 'promoted' | 'rejected' | 'paused';
  state_label: string;
  created_at: string | null;
}

export interface ExperimentListResponse {
  experiments: Experiment[];
  count: number;
  counts: { active: number; past: number };
}

/** One {metric, new value} row of a multi-metric experiment (CB-27). `dimension` is the same supported
 *  mutation surface as the single-dimension run ('playbooks.discovery_sequence' or 'thresholds.<key>');
 *  `value` is the reordered slot list or the new number for that knob. */
export interface MetricChange {
  dimension: string;
  value: string[] | number;
}

/** A "run an A/B between values" request (POST /api/experiments/run). `n` is the held-out sample PER
 *  ARM — modest, since each call spends real model credit in prod. `episode_id` (optional, CB-19)
 *  records the reviewed call a run was scaffolded from; it never changes the A/B (frozen by seed).
 *
 *  SINGLE-DIMENSION (default): `dimension` ('playbooks.discovery_sequence' | 'thresholds.<key>') +
 *  `value` (a reordered slot-name list | the new number). Keeps the R19 single-dimension invariant.
 *
 *  MULTI-METRIC (CB-27, explicit opt-in): `changes` — a list of {dimension, value} rows — A/Bs several
 *  knobs at once. This RELAXES the single-dimension R19 invariant for a MANUAL experiment; the
 *  challenger applies ALL rows. When present, `changes` WINS over `dimension`/`value`. */
export interface RunExperimentRequest {
  dimension?: string;
  value?: string[] | number;
  changes?: MetricChange[];
  n?: number;
  name?: string;
  episode_id?: string;
}

/** The run result (CB-15 — the run is ASYNC): the freshly-persisted `running` experiment (already
 *  label-translated) + the gate decision, which is "running" until the background A/B settles it (the
 *  /api/experiments poll catches the terminal state). The endpoint replies 202 Accepted, not 200.
 *  CB-57: effective_n is the actual n the backend will use (after flooring to the minimum and capping
 *  at the maximum). n_min / n_max are the bounds, present so the dialog can note when input was floored.
 *  These may be absent on legacy/test responses — fall back to experiment.n when missing. */
export interface RunExperimentResponse {
  experiment: Experiment;
  decision: string;
  effective_n?: number;
  n_min?: number;
  n_max?: number;
}

/** One A/B "mock call" — a per-arm self-play episode summary (CB-25). `outcome` is a raw outcome slug
 *  (mapped to a human label before render, never shown raw); `episode_id` is the Review deep-link
 *  target (/operate/review/<id>). */
export interface ArmCall {
  episode_id: string;
  outcome: string | null; // raw outcome slug — humanized before render, never shown raw
  ladder_tier: number;
  qualified: boolean;
  turn_count: number;
  version: string;
}

/** The experiment detail payload (GET /api/experiments/{id}, CB-25): the same flat experiment DTO the
 *  lab cards use, PLUS the champion-arm + challenger-arm mock calls the A/B produced (each openable in
 *  Review). `has_calls` is false for a draft / legacy / timed-out run that persisted no arm episodes —
 *  the page then shows outcomes/KPIs + the failure reason without a calls list. */
export interface ExperimentDetailResponse {
  experiment: Experiment;
  champion_calls: ArmCall[];
  challenger_calls: ArmCall[];
  has_calls: boolean;
}

/** Scaffold-an-experiment-from-a-call request (POST /api/experiments/scaffold, CB-19). `episode_id` is
 *  the reviewed call; an optional `dimension`/`value` pre-picks the change (omitted -> a default
 *  minimal discovery reorder). Builds a DRAFT — it launches nothing. */
export interface ScaffoldExperimentRequest {
  episode_id: string;
  dimension?: string;
  value?: string[] | number;
  name?: string;
}

/** The scaffold result (CB-19): the persisted `draft` experiment + the originating episode id, for the
 *  lab's per-experiment review view to open pre-populated before the operator launches the run. */
export interface ScaffoldExperimentResponse {
  experiment: Experiment;
  episode_id: string;
  is_scaffold: true;
}

export interface ApprovalListResponse {
  approvals: Experiment[];
  count: number;
}

/** The approve/reject mutation result. */
export interface DecisionResponse {
  experiment: Experiment;
  champion_version?: string;
}

/** One grounded fact/rebuttal the agent retrieves on — a kb_chunk row, ready to render. `id` and
 *  `source` are internal slugs ("<section>#<id>") kept for client keys/logic only and MUST NOT render
 *  as text; the UI shows `title` (a short derived label) + `text` (the full real body). */
export interface KbChunk {
  id: string; // internal chunk slug — never rendered as operator text
  source: string; // "<section>#<id>" — internal, never rendered
  title: string; // short human title derived from the body — what the panel header shows
  text: string; // the full real grounding content — what the panel body shows
}

/** One section of the grounded corpus (P8 left tree node). `id` is the raw section slug for keys/logic
 *  only; `label` is the operator-facing name; `count` matches `chunks.length` (the tree count never
 *  lies about what's shown). */
export interface KbSection {
  id: string; // raw section slug — for React keys / selection logic only, never rendered
  label: string; // human section name — what the tree shows
  count: number; // == chunks.length
  chunks: KbChunk[];
}

/** The REAL grounded corpus the agent retrieves on, grouped by section (P8 KB browser). Replaces the
 *  former placeholder `rebuttals` map — the operator browses actual facts, not PLACEHOLDER strings. */
export interface KbResponse {
  kb_version: string;
  version: string;
  sections: KbSection[];
  total_chunks: number;
}

/** The current champion playbook (P8 editor view). */
export interface PlaybookResponse {
  version: string;
  kb_version: string;
  discovery_sequence: string[];
  rebuttals: Record<string, unknown>;
  thresholds: Record<string, number>;
}

/** A KB/playbook SAVE result — a DRAFT challenger (R20: NOT a live mutation). */
export interface DraftResponse {
  draft: Experiment;
  is_draft: true;
}

/** One node in the version lineage (P9). */
export interface VersionNode {
  version: string;
  parent_version: string | null;
  kb_version: string;
  is_champion: boolean;
  kpi: Record<string, unknown>;
  dimension_label: string | null;
  created_at: string | null;
}

export interface VersionListResponse {
  versions: VersionNode[];
  count: number;
  champion_version: string | null;
}

/** The rollback mutation result. */
export interface RollbackResponse {
  champion_version: string;
  version: VersionNode;
}

// =================================================================================================
// Presentation helpers (Improve-domain) — humanize raw slugs the Improve UI shows. These live here
// (not in lib/labels.ts) so the lab + the experiment-detail page share ONE source; every output is
// human text, never a raw slug (project rule: no internal index in viewer-facing output).
// =================================================================================================

// Raw episode-outcome slug -> the operator-facing label the A/B mock-call rows show (CB-25). An
// unmapped/empty outcome falls back to a humanized title (e.g. "trial_started" -> "Trial started"),
// so a NEW outcome never renders a bare snake_case slug.
const OUTCOME_LABELS: Record<string, string> = {
  enrolled: 'Enrolled',
  trial_booked: 'Trial booked',
  consult_booked: 'Consultation booked',
  released: 'Released (unqualified)',
  walked: 'Walked away',
  escalated: 'Escalated to a human',
};

export function outcomeLabel(outcome: string | null | undefined): string {
  if (!outcome) return 'No outcome';
  return (
    OUTCOME_LABELS[outcome] ??
    outcome.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
  );
}

// CB-26 — the metric→behavior explainer. For a tunable metric, a few value BANDS each map a 0..1 level
// to a rough qualitative description of how a prospect/agent at that level behaves. This is the
// belief→behavior translation the operator asked for: human text only, never a raw slug. Keyed by the
// thresholds.<key> tail (the same keys the run drawer offers). `unit` lets a non-0..1 knob (a turn
// count, a slot count) describe its scale honestly instead of pretending to be a fraction.
export interface MetricBand {
  /** inclusive lower bound of the band on the metric's own scale */
  from: number;
  /** human description of behavior at this level */
  behavior: string;
}
export interface MetricExplainer {
  /** what the number measures, in plain words */
  measures: string;
  /** 'fraction' (0..1) | 'count' (an integer scale) — drives the chart's tick labels */
  unit: 'fraction' | 'count';
  /** ascending bands; the matching band is the highest `from` <= value */
  bands: MetricBand[];
}

const METRIC_EXPLAINERS: Record<string, MetricExplainer> = {
  pushiness_cap: {
    measures: 'how hard the agent is allowed to push before it eases off',
    unit: 'fraction',
    bands: [
      { from: 0, behavior: 'Very gentle — backs off at the first hesitation; rarely asks for the sale.' },
      { from: 0.4, behavior: 'Balanced — nudges toward a decision but yields when the prospect resists.' },
      { from: 0.75, behavior: 'Assertive — keeps pressing for commitment; risks feeling pushy on a cold call.' },
    ],
  },
  pushiness_pressure_count_cap: {
    measures: 'how many times in a row the agent may re-apply pressure',
    unit: 'count',
    bands: [
      { from: 0, behavior: 'One ask, then it changes tack — never repeats the same push.' },
      { from: 2, behavior: 'Will re-ask a couple of times before moving on.' },
      { from: 4, behavior: 'Persistent — repeats the push several times; can read as badgering.' },
    ],
  },
  discovery_slots_required: {
    measures: 'how much the agent must learn before it tries to close',
    unit: 'count',
    bands: [
      { from: 0, behavior: 'Closes fast — will pitch with almost no discovery.' },
      { from: 2, behavior: 'Learns the basics (goal, who it is for) before pitching.' },
      { from: 4, behavior: 'Thorough — fully qualifies the need before any close; slower but better-targeted.' },
    ],
  },
  low_confidence_level: {
    measures: 'how unsure the agent can be before it slows down / escalates',
    unit: 'fraction',
    bands: [
      { from: 0, behavior: 'Tolerates uncertainty — keeps going even when it has read the prospect poorly.' },
      { from: 0.4, behavior: 'Pauses to clarify when it is unsure rather than guessing.' },
      { from: 0.7, behavior: 'Cautious — frequently checks understanding or hands off when confidence dips.' },
    ],
  },
  max_concession_band: {
    measures: 'the biggest price concession the agent may offer',
    unit: 'fraction',
    bands: [
      { from: 0, behavior: 'Holds the line — no discount; protects margin, may lose price-sensitive buyers.' },
      { from: 0.15, behavior: 'Offers a modest, standard concession to close a hesitant buyer.' },
      { from: 0.3, behavior: 'Deep discounting — wins more deals but erodes price integrity (needs approval).' },
    ],
  },
  trust_gate_open_price: {
    measures: 'how warmed-up the prospect must be before price comes up',
    unit: 'fraction',
    bands: [
      { from: 0, behavior: 'Talks price early — even before trust is built; can feel transactional.' },
      { from: 0.5, behavior: 'Waits for some warmth/value-acknowledgement before pricing.' },
      { from: 0.75, behavior: 'Strict — only opens price once clear trust is established; protects the pitch.' },
    ],
  },
};

/** The metric→behavior explainer for a threshold key (CB-26), or null if none is defined. Accepts the
 *  bare key or the dotted "thresholds.<key>" form. */
export function metricExplainer(key: string | null | undefined): MetricExplainer | null {
  if (!key) return null;
  const token = key.includes('.') ? key.split('.').pop()! : key;
  return METRIC_EXPLAINERS[token] ?? null;
}

/** The band whose behavior matches `value` on the metric's scale (the highest `from` <= value), or
 *  null if the value is below the first band / no explainer exists. */
export function metricBandFor(
  key: string | null | undefined,
  value: number,
): MetricBand | null {
  const ex = metricExplainer(key);
  if (!ex) return null;
  let match: MetricBand | null = null;
  for (const band of ex.bands) {
    if (value >= band.from) match = band;
  }
  return match;
}

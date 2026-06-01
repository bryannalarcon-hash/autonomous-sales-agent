// Operator-dashboard (Improve mode, U16) API contract — mirrors the JSON the FastAPI Improve router
// (src/api/improve.py) returns for /api/experiments, /api/approvals(+approve/reject), /api/kb,
// /api/playbook, /api/versions(+rollback). Keep in lockstep with that module. The backend has already
// translated every internal index (dimension slug -> dimension_label, lifecycle state -> state_label)
// into human-readable fields; the raw `dimension`/`state` slugs are kept ONLY for client branching
// (chip color, action gating) and MUST NOT render in operator text — the UI shows the *_label fields.
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

/** A "run an A/B between two values" request (POST /api/experiments/run). `dimension` is a supported
 *  mutation surface: 'playbooks.discovery_sequence' (value = a reordered slot-name list) or
 *  'thresholds.<key>' (value = the new number). `n` is the held-out sample PER ARM — modest, since
 *  each call spends real model credit in prod. */
export interface RunExperimentRequest {
  dimension: string;
  value: string[] | number;
  n?: number;
  name?: string;
}

/** The run result: the freshly-persisted experiment (already label-translated) + the gate decision. */
export interface RunExperimentResponse {
  experiment: Experiment;
  decision: string;
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

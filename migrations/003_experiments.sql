-- 003_experiments.sql — persisted champion/challenger experiment records (plan U16, dashboard P6/P7).
-- Backs the Experiment Lab (P6) + Approval Queue (P7): one row per challenger run, carrying the
-- before/after comparison (champion_kpi/challenger_kpi/delta/delta_ci/challenger_better), the
-- guardrail verdict, the per-arm qualification accuracy, the declared one-dimension diff, the
-- is_extreme flag, and a lifecycle `state`. Mirrors the escalation_log table pattern: JSONB for the
-- variable-shape declared_diff + delta_ci, scalar columns for the hot filters (state, is_extreme).
-- Idempotent: safe to re-run (CREATE TABLE/INDEX ... IF NOT EXISTS).

-- ---------------------------------------------------------------------------
-- experiment — one persisted champion-vs-challenger experiment (dashboard P6/P7).
-- `state` lifecycle: draft/running (in flight) -> passed (clean lift) | blocked (extreme, awaiting
-- human approval = pending_approval) | rejected | paused | promoted (approved/auto-promoted).
-- A KB/playbook editor SAVE writes a NEW row in `running` state (a DRAFT CHALLENGER) — it never
-- mutates version_lineage, so the live champion is untouched until an experiment is promoted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiment (
    experiment_id       TEXT PRIMARY KEY,
    name                TEXT         NOT NULL DEFAULT '',
    challenger_version  TEXT         NOT NULL,
    parent_version      TEXT,
    dimension           TEXT         NOT NULL DEFAULT '',
    declared_diff       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    diff_description    TEXT         NOT NULL DEFAULT '',
    population          TEXT         NOT NULL DEFAULT '',
    n                   INTEGER      NOT NULL DEFAULT 0,
    target              INTEGER      NOT NULL DEFAULT 0,
    -- before/after comparison fields (from grading.ComparisonResult).
    champion_kpi        DOUBLE PRECISION NOT NULL DEFAULT 0,
    challenger_kpi      DOUBLE PRECISION NOT NULL DEFAULT 0,
    delta               DOUBLE PRECISION NOT NULL DEFAULT 0,
    delta_ci            JSONB        NOT NULL DEFAULT '[0,0]'::jsonb,
    challenger_better   BOOLEAN      NOT NULL DEFAULT FALSE,
    enroll_delta        DOUBLE PRECISION NOT NULL DEFAULT 0,
    significance        DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- guardrail verdict ('pass' | 'warn' | 'trip') + per-arm qualification accuracy (R29).
    guardrail           TEXT         NOT NULL DEFAULT 'pass'
                        CHECK (guardrail IN ('pass', 'warn', 'trip')),
    guardrail_reason    TEXT,
    champion_qual_acc   DOUBLE PRECISION NOT NULL DEFAULT 1,
    challenger_qual_acc DOUBLE PRECISION NOT NULL DEFAULT 1,
    -- is_extreme blocks for human approval (R19); state is the lifecycle (see header).
    is_extreme          BOOLEAN      NOT NULL DEFAULT FALSE,
    state               TEXT         NOT NULL DEFAULT 'running'
                        CHECK (state IN ('draft', 'running', 'passed', 'blocked',
                                         'promoted', 'rejected', 'paused')),
    kb_version          TEXT         NOT NULL DEFAULT '',
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Dashboard hot filters: the lab tabs active/past by state (P6); the approval queue lists `blocked`
-- (P7); the version detail joins by challenger_version.
CREATE INDEX IF NOT EXISTS ix_experiment_state      ON experiment (state);
CREATE INDEX IF NOT EXISTS ix_experiment_challenger ON experiment (challenger_version);
CREATE INDEX IF NOT EXISTS ix_experiment_created_at ON experiment (created_at DESC);

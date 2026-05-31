-- 001_init.sql — initial schema for the unified memory store (plan U2, KTD3/KTD4).
-- Creates the pgvector extension and the three core tables: episode (real+sim, one shape),
-- lead (per-caller memory keyed by phone-hash — raw phone never stored), and version_lineage
-- (champion/challenger tree). escalation_log backs the dashboard escalation queue (P5).
-- JSONB carries the variable-shape payloads (turns/belief, slots, KPI); indexes cover the
-- dashboard's hot filters (episode by version/channel/cohort, lead by phone_hash PK).
-- Idempotent: safe to re-run (CREATE ... IF NOT EXISTS / CREATE EXTENSION IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- lead — per-caller memory, PRIMARY KEY is the sha256 phone-hash (plan R42).
-- The raw phone number is NEVER a column here. Slots merge across calls.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lead (
    phone_hash      TEXT PRIMARY KEY,
    slots           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    prior_objections JSONB       NOT NULL DEFAULT '[]'::jsonb,
    last_outcome    TEXT,
    assigned_voice  TEXT,
    persona_read    TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- version_lineage — champion/challenger lineage (dashboard P6/P9).
-- parent_version self-references the tree; kpi holds the per-version R34 metric record.
-- A partial unique index enforces at most one champion at a time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS version_lineage (
    version         TEXT PRIMARY KEY,
    parent_version  TEXT REFERENCES version_lineage(version),
    config_ref      TEXT,
    kb_version      TEXT         NOT NULL DEFAULT '',
    kpi             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    is_champion     BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_version_lineage_champion
    ON version_lineage (is_champion)
    WHERE is_champion;

CREATE INDEX IF NOT EXISTS ix_version_lineage_parent
    ON version_lineage (parent_version);

-- ---------------------------------------------------------------------------
-- episode — one conversation (real or sim) normalized to one shape (plan R26).
-- turns (incl. per-turn decision/rationale/belief snapshot) and metrics are JSONB.
-- lead_phone_hash links to lead when the caller is known (NULL for anonymous sim).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episode (
    episode_id          TEXT PRIMARY KEY,
    turns               JSONB        NOT NULL DEFAULT '[]'::jsonb,
    outcome             TEXT,
    ladder_tier         INTEGER      NOT NULL DEFAULT 0,
    qualified           BOOLEAN      NOT NULL DEFAULT FALSE,
    disqualifier_reason TEXT,
    version             TEXT         NOT NULL DEFAULT '',
    kb_version          TEXT         NOT NULL DEFAULT '',
    channel             TEXT         NOT NULL DEFAULT 'sim'
                        CHECK (channel IN ('sim', 'text', 'voice')),
    lead_phone_hash     TEXT REFERENCES lead(phone_hash),
    persona             TEXT,
    cohort              TEXT,
    escalated           BOOLEAN      NOT NULL DEFAULT FALSE,
    metrics             JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Dashboard hot filters: KPI views aggregate per version (P4), calls list filters by
-- channel/cohort/outcome/escalated (P3), call review pulls recent by time.
CREATE INDEX IF NOT EXISTS ix_episode_version    ON episode (version);
CREATE INDEX IF NOT EXISTS ix_episode_channel    ON episode (channel);
CREATE INDEX IF NOT EXISTS ix_episode_cohort     ON episode (cohort);
CREATE INDEX IF NOT EXISTS ix_episode_outcome    ON episode (outcome);
CREATE INDEX IF NOT EXISTS ix_episode_lead       ON episode (lead_phone_hash);
CREATE INDEX IF NOT EXISTS ix_episode_created_at ON episode (created_at DESC);
-- Partial index for the P3 "escalated?" filter / P5 join.
CREATE INDEX IF NOT EXISTS ix_episode_escalated  ON episode (escalated) WHERE escalated;

-- ---------------------------------------------------------------------------
-- escalation_log — deferred/extreme moments linked to episodes (dashboard P5).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS escalation_log (
    escalation_id   TEXT PRIMARY KEY,
    episode_id      TEXT NOT NULL REFERENCES episode(episode_id) ON DELETE CASCADE,
    reason          TEXT,
    moment          TEXT,
    turn_id         INTEGER,
    lifecycle       TEXT NOT NULL DEFAULT 'unreviewed'
                    CHECK (lifecycle IN ('unreviewed', 'reviewed', 'resolved', 'dismissed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_escalation_episode   ON escalation_log (episode_id);
CREATE INDEX IF NOT EXISTS ix_escalation_lifecycle ON escalation_log (lifecycle);

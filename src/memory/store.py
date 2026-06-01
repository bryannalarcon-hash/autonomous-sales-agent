# Async datastore (plan U2) over Postgres+pgvector via asyncpg. Persists/queries the unified
# Episode schema, per-lead memory (upsert merges slots by phone-hash), version lineage
# (record/champion/lineage), the ESCALATION REVIEW QUEUE (save/get/list escalation_log rows;
# the deferred-extreme-moment records U14 writes — dashboard P5), and the EXPERIMENT records
# (save/get/list experiment rows — the persisted champion-vs-challenger runs the dashboard P6 lab /
# P7 approval queue read; U16). Reads DATABASE_URL from the environment (.env via python-dotenv).
# A module-level connection pool is created lazily and reused. JSONB columns are (de)serialized
# with a datetime-aware encoder so created_at round-trips. NO LiveKit/voice imports — sim, text,
# and voice channels all write through this one interface (plan R26 single-shape guarantee).
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

import asyncpg
from dotenv import load_dotenv

from src.memory.schema import Episode, EscalationLog, ExperimentRecord, Lead, VersionLineage

# Load .env once at import so DATABASE_URL is available without an explicit call.
load_dotenv()

_POOL: Optional[asyncpg.Pool] = None


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env (it points at the "
            "docker-compose Postgres+pgvector) or export DATABASE_URL."
        )
    return url


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default)


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register JSON/JSONB codecs so dicts/lists pass through transparently."""
    await conn.set_type_codec(
        "jsonb", encoder=_dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=_dumps, decoder=json.loads, schema="pg_catalog"
    )


# Per-query timeout (seconds) applied to every connection in the pool so a hung/slow statement fails
# loudly instead of pinning a connection forever. Overridable via DB_COMMAND_TIMEOUT for slow envs.
def _command_timeout() -> float:
    try:
        return float(os.environ.get("DB_COMMAND_TIMEOUT", "30"))
    except (TypeError, ValueError):
        return 30.0


async def get_pool() -> asyncpg.Pool:
    """Return the lazily-created shared connection pool.

    A `command_timeout` bounds every statement so a stuck query surfaces as a timeout rather than
    leaking a held connection; min/max sizing keeps a small warm pool for the dashboard read path.
    """
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(
            dsn=_database_url(),
            init=_init_connection,
            min_size=1,
            max_size=10,
            command_timeout=_command_timeout(),
        )
    return _POOL


async def connect() -> asyncpg.Pool:
    """Explicitly establish the pool (alias of get_pool for call-site clarity)."""
    return await get_pool()


async def close_pool() -> None:
    """Close and drop the shared pool (use in test teardown / shutdown)."""
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------
async def save_episode(episode: Episode) -> str:
    """Insert or replace an episode row (upsert on episode_id). Returns the episode_id.

    Turns (with belief snapshots) and metrics serialize to JSONB; channel/version/kb_version
    are stored as scalar columns for the dashboard's hot filters.
    """
    pool = await get_pool()
    turns_json = [t.to_dict() for t in episode.turns]
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO episode (
                episode_id, turns, outcome, ladder_tier, qualified, disqualifier_reason,
                version, kb_version, channel, lead_phone_hash, persona, cohort,
                escalated, metrics, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT (episode_id) DO UPDATE SET
                turns = EXCLUDED.turns,
                outcome = EXCLUDED.outcome,
                ladder_tier = EXCLUDED.ladder_tier,
                qualified = EXCLUDED.qualified,
                disqualifier_reason = EXCLUDED.disqualifier_reason,
                version = EXCLUDED.version,
                kb_version = EXCLUDED.kb_version,
                channel = EXCLUDED.channel,
                lead_phone_hash = EXCLUDED.lead_phone_hash,
                persona = EXCLUDED.persona,
                cohort = EXCLUDED.cohort,
                escalated = EXCLUDED.escalated,
                metrics = EXCLUDED.metrics,
                created_at = EXCLUDED.created_at
            """,
            episode.episode_id,
            turns_json,
            episode.outcome,
            episode.ladder_tier,
            episode.qualified,
            episode.disqualifier_reason,
            episode.version,
            episode.kb_version,
            episode.channel,
            episode.lead_phone_hash,
            episode.persona,
            episode.cohort,
            episode.escalated,
            episode.metrics,
            episode.created_at,
        )
    return episode.episode_id


async def get_episode(episode_id: str) -> Optional[Episode]:
    """Fetch one full episode by id, or None if absent."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM episode WHERE episode_id = $1", episode_id
        )
    if row is None:
        return None
    return _row_to_episode(row)


async def list_episodes(
    *,
    version: Optional[str] = None,
    cohort: Optional[str] = None,
    outcome: Optional[str] = None,
    escalated: Optional[bool] = None,
    limit: int = 200,
) -> list[Episode]:
    """List episodes newest-first for the dashboard Calls list (P3) + KPI aggregation (P4).

    The dashboard's hot filters (version / cohort / outcome, plus an escalated-only toggle) are
    pushed down to scalar columns so the index stays cheap; all are optional and AND together.
    Ordered created_at DESC so the most recent call leads the list; `limit` caps the page. Returns
    full Episode objects (turns + belief trajectory included) — the same shape get_episode returns,
    so callers can render summary rows or drill into one without a second fetch shape.
    """
    pool = await get_pool()
    clauses: list[str] = []
    args: list[Any] = []
    if version is not None:
        args.append(version)
        clauses.append(f"version = ${len(args)}")
    if cohort is not None:
        args.append(cohort)
        clauses.append(f"cohort = ${len(args)}")
    if outcome is not None:
        args.append(outcome)
        clauses.append(f"outcome = ${len(args)}")
    if escalated is not None:
        args.append(escalated)
        clauses.append(f"escalated = ${len(args)}")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    args.append(limit)
    sql = f"SELECT * FROM episode{where} ORDER BY created_at DESC LIMIT ${len(args)}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [_row_to_episode(r) for r in rows]


def _row_to_episode(row: asyncpg.Record) -> Episode:
    d = dict(row)
    # turns/metrics come back as parsed JSON via the codec.
    return Episode.from_dict(
        {
            "episode_id": d["episode_id"],
            "turns": d.get("turns") or [],
            "outcome": d.get("outcome"),
            "ladder_tier": d.get("ladder_tier", 0),
            "qualified": d.get("qualified", False),
            "disqualifier_reason": d.get("disqualifier_reason"),
            "version": d.get("version", ""),
            "kb_version": d.get("kb_version", ""),
            "channel": d.get("channel", "sim"),
            "lead_phone_hash": d.get("lead_phone_hash"),
            "persona": d.get("persona"),
            "cohort": d.get("cohort"),
            "escalated": d.get("escalated", False),
            "metrics": d.get("metrics") or {},
            "created_at": d.get("created_at"),
        }
    )


# ---------------------------------------------------------------------------
# Leads (per-caller memory, phone-hash key, slot merge)
# ---------------------------------------------------------------------------
async def upsert_lead_by_phone(lead: Lead) -> Lead:
    """Insert a new lead or MERGE into an existing one by phone_hash.

    Slots merge (existing || new — new keys win), prior_objections union (order-preserving,
    de-duplicated), and the scalar fields (last_outcome / assigned_voice / persona_read) overwrite
    only when the incoming value is non-None. updated_at advances; created_at is preserved.
    Returns the merged Lead as stored.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT * FROM lead WHERE phone_hash = $1 FOR UPDATE",
                lead.phone_hash,
            )
            if existing is None:
                await conn.execute(
                    """
                    INSERT INTO lead (
                        phone_hash, slots, prior_objections, last_outcome,
                        assigned_voice, persona_read, created_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, now(), now())
                    """,
                    lead.phone_hash,
                    lead.slots,
                    lead.prior_objections,
                    lead.last_outcome,
                    lead.assigned_voice,
                    lead.persona_read,
                )
            else:
                ex = dict(existing)
                merged_slots = {**(ex.get("slots") or {}), **(lead.slots or {})}
                merged_objections = _union_keep_order(
                    ex.get("prior_objections") or [], lead.prior_objections or []
                )
                await conn.execute(
                    """
                    UPDATE lead SET
                        slots = $2,
                        prior_objections = $3,
                        last_outcome = COALESCE($4, last_outcome),
                        assigned_voice = COALESCE($5, assigned_voice),
                        persona_read = COALESCE($6, persona_read),
                        updated_at = now()
                    WHERE phone_hash = $1
                    """,
                    lead.phone_hash,
                    merged_slots,
                    merged_objections,
                    lead.last_outcome,
                    lead.assigned_voice,
                    lead.persona_read,
                )
            row = await conn.fetchrow(
                "SELECT * FROM lead WHERE phone_hash = $1", lead.phone_hash
            )
    return _row_to_lead(row)


def _union_keep_order(a: list[Any], b: list[Any]) -> list[Any]:
    out: list[Any] = []
    for item in [*a, *b]:
        if item not in out:
            out.append(item)
    return out


async def get_lead_by_phone(phone_hash: str) -> Optional[Lead]:
    """Fetch a lead by its phone-hash key, or None if no record exists."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM lead WHERE phone_hash = $1", phone_hash)
    if row is None:
        return None
    return _row_to_lead(row)


def _row_to_lead(row: asyncpg.Record) -> Lead:
    d = dict(row)
    return Lead.from_dict(
        {
            "phone_hash": d["phone_hash"],
            "slots": d.get("slots") or {},
            "prior_objections": d.get("prior_objections") or [],
            "last_outcome": d.get("last_outcome"),
            "assigned_voice": d.get("assigned_voice"),
            "persona_read": d.get("persona_read"),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
        }
    )


# ---------------------------------------------------------------------------
# Version lineage
# ---------------------------------------------------------------------------
async def record_version(lineage: VersionLineage) -> str:
    """Insert or update a lineage node (upsert on version). Returns the version.

    When is_champion is True, demotes any prior champion first so the partial unique index
    (one champion at a time) is satisfied.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if lineage.is_champion:
                await conn.execute(
                    "UPDATE version_lineage SET is_champion = FALSE "
                    "WHERE is_champion AND version <> $1",
                    lineage.version,
                )
            await conn.execute(
                """
                INSERT INTO version_lineage (
                    version, parent_version, config_ref, kb_version, kpi, is_champion, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (version) DO UPDATE SET
                    parent_version = EXCLUDED.parent_version,
                    config_ref = EXCLUDED.config_ref,
                    kb_version = EXCLUDED.kb_version,
                    kpi = EXCLUDED.kpi,
                    is_champion = EXCLUDED.is_champion
                """,
                lineage.version,
                lineage.parent_version,
                lineage.config_ref,
                lineage.kb_version,
                lineage.kpi,
                lineage.is_champion,
                lineage.created_at,
            )
    return lineage.version


async def get_lineage(version: str) -> Optional[VersionLineage]:
    """Fetch one lineage node by version, or None if absent."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM version_lineage WHERE version = $1", version
        )
    if row is None:
        return None
    return _row_to_lineage(row)


async def get_champion() -> Optional[VersionLineage]:
    """Fetch the current champion lineage node, or None if none is marked."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM version_lineage WHERE is_champion LIMIT 1"
        )
    if row is None:
        return None
    return _row_to_lineage(row)


def _row_to_lineage(row: asyncpg.Record) -> VersionLineage:
    d = dict(row)
    return VersionLineage.from_dict(
        {
            "version": d["version"],
            "parent_version": d.get("parent_version"),
            "config_ref": d.get("config_ref"),
            "kb_version": d.get("kb_version", ""),
            "kpi": d.get("kpi") or {},
            "is_champion": d.get("is_champion", False),
            "created_at": d.get("created_at"),
        }
    )


# ---------------------------------------------------------------------------
# Escalation review queue (U14/R10 — the deferred-extreme-moment records)
# ---------------------------------------------------------------------------
async def save_escalation(escalation: EscalationLog) -> str:
    """Insert or replace an escalation_log row (upsert on escalation_id). Returns the escalation_id.

    This IS the review queue's write side: U14's handle_escalation persists the deferred moment here
    so the dashboard escalation queue (P5) can list/review it later. The escalation_log row FKs to an
    existing episode (migrations/001_init.sql) — the caller must have saved that episode first.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO escalation_log (
                escalation_id, episode_id, reason, moment, turn_id, lifecycle, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (escalation_id) DO UPDATE SET
                episode_id = EXCLUDED.episode_id,
                reason = EXCLUDED.reason,
                moment = EXCLUDED.moment,
                turn_id = EXCLUDED.turn_id,
                lifecycle = EXCLUDED.lifecycle,
                created_at = EXCLUDED.created_at
            """,
            escalation.escalation_id,
            escalation.episode_id,
            escalation.reason,
            escalation.moment,
            escalation.turn_id,
            escalation.lifecycle,
            escalation.created_at,
        )
    return escalation.escalation_id


async def get_escalation(escalation_id: str) -> Optional[EscalationLog]:
    """Fetch one escalation by id, or None if absent."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM escalation_log WHERE escalation_id = $1", escalation_id
        )
    if row is None:
        return None
    return _row_to_escalation(row)


async def list_escalations(
    *, lifecycle: Optional[str] = None, limit: int = 100
) -> list[EscalationLog]:
    """Read the escalation review queue (NEWEST first), optionally filtered by lifecycle.

    The dashboard escalation queue (P5) reads this: pass lifecycle="unreviewed" for the pending items
    an operator still has to triage. Ordered created_at DESC + `limit` so the MOST RECENT escalations
    always surface — with ASC, a backlog larger than `limit` would hide every new escalation behind the
    oldest ones (a new item could never appear until the queue drained). Page back for older items.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if lifecycle is None:
            rows = await conn.fetch(
                "SELECT * FROM escalation_log ORDER BY created_at DESC LIMIT $1", limit
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM escalation_log WHERE lifecycle = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                lifecycle,
                limit,
            )
    return [_row_to_escalation(r) for r in rows]


def _row_to_escalation(row: asyncpg.Record) -> EscalationLog:
    d = dict(row)
    return EscalationLog.from_dict(
        {
            "escalation_id": d["escalation_id"],
            "episode_id": d["episode_id"],
            "reason": d.get("reason"),
            "moment": d.get("moment"),
            "turn_id": d.get("turn_id"),
            "lifecycle": d.get("lifecycle", "unreviewed"),
            "created_at": d.get("created_at"),
        }
    )


# ---------------------------------------------------------------------------
# Experiment records (U16 — the persisted champion-vs-challenger runs; dashboard P6/P7)
# ---------------------------------------------------------------------------
async def save_experiment(experiment: ExperimentRecord) -> str:
    """Insert or replace an experiment row (upsert on experiment_id). Returns the experiment_id.

    This is the lab/approvals write side: the loop's in-memory ExperimentResult is flattened into a
    durable ExperimentRecord (before/after comparison + guardrail + declared diff + state) and stored
    here so the Experiment Lab (P6) can list it and the Approval Queue (P7) can act on the `blocked`
    ones. The declared_diff list + delta_ci pair serialize to JSONB. Mirrors save_escalation's shape.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO experiment (
                experiment_id, name, challenger_version, parent_version, dimension,
                declared_diff, diff_description, population, n, target,
                champion_kpi, challenger_kpi, delta, delta_ci, challenger_better,
                enroll_delta, significance, guardrail, guardrail_reason,
                champion_qual_acc, challenger_qual_acc, is_extreme, state, kb_version, created_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                    $18, $19, $20, $21, $22, $23, $24, $25)
            ON CONFLICT (experiment_id) DO UPDATE SET
                name = EXCLUDED.name,
                challenger_version = EXCLUDED.challenger_version,
                parent_version = EXCLUDED.parent_version,
                dimension = EXCLUDED.dimension,
                declared_diff = EXCLUDED.declared_diff,
                diff_description = EXCLUDED.diff_description,
                population = EXCLUDED.population,
                n = EXCLUDED.n,
                target = EXCLUDED.target,
                champion_kpi = EXCLUDED.champion_kpi,
                challenger_kpi = EXCLUDED.challenger_kpi,
                delta = EXCLUDED.delta,
                delta_ci = EXCLUDED.delta_ci,
                challenger_better = EXCLUDED.challenger_better,
                enroll_delta = EXCLUDED.enroll_delta,
                significance = EXCLUDED.significance,
                guardrail = EXCLUDED.guardrail,
                guardrail_reason = EXCLUDED.guardrail_reason,
                champion_qual_acc = EXCLUDED.champion_qual_acc,
                challenger_qual_acc = EXCLUDED.challenger_qual_acc,
                is_extreme = EXCLUDED.is_extreme,
                state = EXCLUDED.state,
                kb_version = EXCLUDED.kb_version,
                created_at = EXCLUDED.created_at
            """,
            experiment.experiment_id,
            experiment.name,
            experiment.challenger_version,
            experiment.parent_version,
            experiment.dimension,
            experiment.declared_diff,
            experiment.diff_description,
            experiment.population,
            experiment.n,
            experiment.target,
            experiment.champion_kpi,
            experiment.challenger_kpi,
            experiment.delta,
            experiment.delta_ci,
            experiment.challenger_better,
            experiment.enroll_delta,
            experiment.significance,
            experiment.guardrail,
            experiment.guardrail_reason,
            experiment.champion_qual_acc,
            experiment.challenger_qual_acc,
            experiment.is_extreme,
            experiment.state,
            experiment.kb_version,
            experiment.created_at,
        )
    return experiment.experiment_id


async def get_experiment(experiment_id: str) -> Optional[ExperimentRecord]:
    """Fetch one experiment by id, or None if absent."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM experiment WHERE experiment_id = $1", experiment_id
        )
    if row is None:
        return None
    return _row_to_experiment(row)


async def list_experiments(
    *, state: Optional[str] = None, limit: int = 200
) -> list[ExperimentRecord]:
    """List experiments newest-first for the lab (P6); pass state='blocked' for the approval queue (P7).

    Ordered created_at DESC so the freshest run leads; `limit` caps the page. `state` filters to one
    lifecycle bucket (the only hot filter the lab tabs + approval queue need).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if state is None:
            rows = await conn.fetch(
                "SELECT * FROM experiment ORDER BY created_at DESC LIMIT $1", limit
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM experiment WHERE state = $1 ORDER BY created_at DESC LIMIT $2",
                state,
                limit,
            )
    return [_row_to_experiment(r) for r in rows]


def _row_to_experiment(row: asyncpg.Record) -> ExperimentRecord:
    d = dict(row)
    return ExperimentRecord.from_dict(
        {
            "experiment_id": d["experiment_id"],
            "name": d.get("name", ""),
            "challenger_version": d["challenger_version"],
            "parent_version": d.get("parent_version"),
            "dimension": d.get("dimension", ""),
            "declared_diff": d.get("declared_diff") or [],
            "diff_description": d.get("diff_description", ""),
            "population": d.get("population", ""),
            "n": d.get("n", 0),
            "target": d.get("target", 0),
            "champion_kpi": d.get("champion_kpi", 0.0),
            "challenger_kpi": d.get("challenger_kpi", 0.0),
            "delta": d.get("delta", 0.0),
            "delta_ci": d.get("delta_ci") or [0.0, 0.0],
            "challenger_better": d.get("challenger_better", False),
            "enroll_delta": d.get("enroll_delta", 0.0),
            "significance": d.get("significance", 0.0),
            "guardrail": d.get("guardrail", "pass"),
            "guardrail_reason": d.get("guardrail_reason"),
            "champion_qual_acc": d.get("champion_qual_acc", 1.0),
            "challenger_qual_acc": d.get("challenger_qual_acc", 1.0),
            "is_extreme": d.get("is_extreme", False),
            "state": d.get("state", "running"),
            "kb_version": d.get("kb_version", ""),
            "created_at": d.get("created_at"),
        }
    )

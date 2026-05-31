# Async datastore (plan U2) over Postgres+pgvector via asyncpg. Persists/queries the unified
# Episode schema, per-lead memory (upsert merges slots by phone-hash), and version lineage
# (record/champion/lineage). Reads DATABASE_URL from the environment (.env via python-dotenv).
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

from src.memory.schema import Episode, Lead, VersionLineage

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


async def get_pool() -> asyncpg.Pool:
    """Return the lazily-created shared connection pool."""
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(
            dsn=_database_url(), init=_init_connection, min_size=1, max_size=10
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

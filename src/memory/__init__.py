# Memory package (plan U2) — the single Postgres+pgvector datastore for the sales agent.
# Re-exports the typed episode/lead/lineage models (schema.py), the phone_hash helper, and the
# async store API (store.py) so callers do `from src.memory import Episode, save_episode, ...`.
# No LiveKit/voice imports here: sim and voice channels share this one schema.
from __future__ import annotations

from src.memory.schema import (
    CHANNELS,
    BeliefSnapshot,
    Episode,
    EscalationLog,
    Lead,
    Turn,
    VersionLineage,
    phone_hash,
)
from src.memory.store import (
    close_pool,
    connect,
    get_champion,
    get_episode,
    get_lead_by_phone,
    get_lineage,
    get_pool,
    record_version,
    save_episode,
    upsert_lead_by_phone,
)

__all__ = [
    # schema
    "CHANNELS",
    "BeliefSnapshot",
    "Episode",
    "EscalationLog",
    "Lead",
    "Turn",
    "VersionLineage",
    "phone_hash",
    # store
    "close_pool",
    "connect",
    "get_champion",
    "get_episode",
    "get_lead_by_phone",
    "get_lineage",
    "get_pool",
    "record_version",
    "save_episode",
    "upsert_lead_by_phone",
]

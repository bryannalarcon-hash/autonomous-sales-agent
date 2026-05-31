# Memory package (plan U2) — the single Postgres+pgvector datastore for the sales agent.
# Re-exports the typed episode/lead/lineage models (schema.py, pure stdlib), the phone_hash
# helper, and — LAZILY — the async store API (store.py) so callers do
# `from src.memory import Episode, save_episode, ...`. The store re-exports are loaded on first
# access via module __getattr__ so that importing only the schema models (which the transport-
# agnostic core/ depends on) never drags in asyncpg/DB deps. No LiveKit/voice imports here:
# sim and voice channels share this one schema.
from __future__ import annotations

from typing import Any

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

# Names served lazily from src.memory.store (kept out of import-time so schema-only consumers,
# i.e. the DB-free core/, don't require asyncpg). Resolved on first attribute access.
_STORE_EXPORTS = (
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
)


def __getattr__(name: str) -> Any:
    """Lazily forward store-API names to src.memory.store on first access (PEP 562)."""
    if name in _STORE_EXPORTS:
        from src.memory import store

        return getattr(store, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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

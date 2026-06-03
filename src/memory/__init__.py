# Memory package (plan U2/U6) — the single Postgres+pgvector datastore for the sales agent.
# Re-exports the typed episode/lead/lineage models (schema.py, pure stdlib), the phone_hash
# helper, and — LAZILY — the async store API (store.py) plus the cross-call hydration bridge
# (hydrate.py: hydrate_belief / persist_lead_after_call / seed_slots) so callers do
# `from src.memory import Episode, save_episode, hydrate_belief, ...`. The store/hydrate re-exports
# are loaded on first access via module __getattr__ so that importing only the schema models (which
# the transport-agnostic core/ depends on) never drags in asyncpg/DB deps. No LiveKit/voice imports
# here: sim and voice channels share this one schema. hydrate.py is the only seam touching BOTH
# core (belief_state) and the store — core/ itself stays DB-free.
from __future__ import annotations

from typing import Any

from src.memory.schema import (
    CHANNELS,
    EXPERIMENT_STATES,
    GUARDRAIL_VERDICTS,
    BeliefSnapshot,
    Episode,
    EscalationLog,
    ExperimentRecord,
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
    "get_escalation",
    "get_experiment",
    "get_lead_by_phone",
    "get_lineage",
    "get_pool",
    "list_episodes",
    "list_escalations",
    "list_experiments",
    "list_golden_episodes",
    "record_version",
    "save_episode",
    "save_escalation",
    "save_experiment",
    "set_episode_golden",
    "upsert_lead_by_phone",
)

# Names served lazily from src.memory.hydrate (the cross-call bridge). Also deferred because it
# imports the store (asyncpg) — schema-only / DB-free core consumers never trigger that import.
_HYDRATE_EXPORTS = (
    "hydrate_belief",
    "persist_lead_after_call",
    "seed_slots",
    "HYDRATED_SLOT_CONFIDENCE",
)


def __getattr__(name: str) -> Any:
    """Lazily forward store/hydrate-API names to their modules on first access (PEP 562)."""
    if name in _STORE_EXPORTS:
        from src.memory import store

        return getattr(store, name)
    if name in _HYDRATE_EXPORTS:
        from src.memory import hydrate

        return getattr(hydrate, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # schema
    "CHANNELS",
    "EXPERIMENT_STATES",
    "GUARDRAIL_VERDICTS",
    "BeliefSnapshot",
    "Episode",
    "EscalationLog",
    "ExperimentRecord",
    "Lead",
    "Turn",
    "VersionLineage",
    "phone_hash",
    # store
    "close_pool",
    "connect",
    "get_champion",
    "get_episode",
    "get_escalation",
    "get_experiment",
    "get_lead_by_phone",
    "get_lineage",
    "get_pool",
    "list_episodes",
    "list_escalations",
    "list_experiments",
    "list_golden_episodes",
    "record_version",
    "save_episode",
    "save_escalation",
    "save_experiment",
    "set_episode_golden",
    "upsert_lead_by_phone",
    # hydrate bridge (U6)
    "hydrate_belief",
    "persist_lead_after_call",
    "seed_slots",
    "HYDRATED_SLOT_CONFIDENCE",
]

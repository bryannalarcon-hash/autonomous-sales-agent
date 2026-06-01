# Operator-dashboard IMPROVE API (plan U16 — Improve mode). The 4 Improve screens read/write JSON
# through these endpoints: P6 Experiment Lab (GET /api/experiments — the before/after champion-vs-
# challenger records), P7 Approval Queue (GET /api/approvals = experiments in `blocked` state, plus
# POST .../approve -> promote the challenger to champion, POST .../reject), P8 KB/Playbook Editor
# (GET /api/kb + /api/playbook show the current champion config; POST /api/kb + /api/playbook SAVE a
# DRAFT CHALLENGER — a NEW experiment record built via the generator's pure config mutators that
# NEVER touches version_lineage, so the live champion is unchanged, R20), and P9 Version History
# (GET /api/versions = the lineage tree + champion; POST /api/versions/{v}/rollback re-promotes a
# prior version). DB-INJECTABLE the same way src.api.operate is: an ImproveStore protocol abstracts
# the data layer so create_improve_router(improve_store=...) takes the real src.memory.store in prod
# and a seeded in-memory fake in tests (no Postgres). REUSES the loop (generator config mutators,
# evaluate_promotion semantics) and src.api.labels so NO raw internal index (dimension slug, state
# slug, version internals) ever renders in operator-facing text.
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.api import labels
from src.config.settings import AgentConfig, load_config
from src.loop.generator import (
    _build_challenger,
    declared_diff,
    mutate_threshold,
    reorder_discovery,
)
from src.memory.schema import ExperimentRecord, VersionLineage

# ---------------------------------------------------------------------------
# Injectable data layer
# ---------------------------------------------------------------------------


class ImproveStore(Protocol):
    """The read+write surface the Improve pages need. The default impl forwards to src.memory.store
    (Postgres); tests pass a seeded in-memory fake so the endpoints run DB-free, mirroring how
    src.api.operate injects a ReadStore. All methods are async (the store is asyncpg-backed)."""

    async def list_experiments(
        self, *, state: Optional[str] = None, limit: int = 200
    ) -> list[ExperimentRecord]: ...

    async def get_experiment(self, experiment_id: str) -> Optional[ExperimentRecord]: ...

    async def save_experiment(self, experiment: ExperimentRecord) -> str: ...

    async def get_lineage(self, version: str) -> Optional[VersionLineage]: ...

    async def get_champion(self) -> Optional[VersionLineage]: ...

    async def record_version(self, lineage: VersionLineage) -> str: ...

    # Lineage list for P9. The default store has no list-all yet, so the router falls back to walking
    # parent links from the champion when this is absent (see _collect_lineage).
    async def list_lineage(self, *, limit: int = 200) -> list[VersionLineage]: ...


class _StoreBackedImproveStore:
    """Default ImproveStore forwarding to src.memory.store module functions. Imported lazily so
    merely building the demo app never drags asyncpg onto an import path that doesn't need it."""

    def __init__(self) -> None:
        from src.memory import store

        self._store = store

    async def list_experiments(self, **kw: Any) -> list[ExperimentRecord]:
        return await self._store.list_experiments(**kw)

    async def get_experiment(self, experiment_id: str) -> Optional[ExperimentRecord]:
        return await self._store.get_experiment(experiment_id)

    async def save_experiment(self, experiment: ExperimentRecord) -> str:
        return await self._store.save_experiment(experiment)

    async def get_lineage(self, version: str) -> Optional[VersionLineage]:
        return await self._store.get_lineage(version)

    async def get_champion(self) -> Optional[VersionLineage]:
        return await self._store.get_champion()

    async def record_version(self, lineage: VersionLineage) -> str:
        return await self._store.record_version(lineage)

    async def list_lineage(self, *, limit: int = 200) -> list[VersionLineage]:
        # The Postgres store exposes no list-all; walk parent links from the champion instead so the
        # lineage list still renders without a new store method.
        champ = await self._store.get_champion()
        if champ is None:
            return []
        return await _collect_lineage(self, champ.version, limit=limit)


async def _collect_lineage(
    store: "ImproveStore", start_version: str, *, limit: int = 200
) -> list[VersionLineage]:
    """Walk parent links from `start_version` up the tree, newest-first, de-duplicated. Used when an
    ImproveStore has no list-all (the Postgres default): the champion's ancestry IS the lineage."""
    out: list[VersionLineage] = []
    seen: set[str] = set()
    cursor: Optional[str] = start_version
    while cursor and cursor not in seen and len(out) < limit:
        node = await store.get_lineage(cursor)
        if node is None:
            break
        seen.add(cursor)
        out.append(node)
        cursor = node.parent_version
    return out


# ---------------------------------------------------------------------------
# Serializers — ExperimentRecord / VersionLineage -> flat display DTOs (labels applied)
# ---------------------------------------------------------------------------


def experiment_to_dict(exp: ExperimentRecord) -> dict[str, Any]:
    """Flatten one experiment for the lab (P6) + approval queue (P7). Every internal index (the
    dimension slug, the state slug) is translated to an operator-facing label; the raw `state` slug is
    kept under `state` for the client's logic-only branching (it is NOT rendered as text)."""
    return {
        "experiment_id": exp.experiment_id,
        "name": exp.name,
        "champion_version": exp.parent_version,
        "challenger_version": exp.challenger_version,
        # before/after comparison (the headline demo artifact).
        "champion_kpi": round(exp.champion_kpi, 4),
        "challenger_kpi": round(exp.challenger_kpi, 4),
        "delta": round(exp.delta, 4),
        "delta_ci": [round(exp.delta_ci[0], 4), round(exp.delta_ci[1], 4)],
        "challenger_better": exp.challenger_better,
        "enroll_delta": round(exp.enroll_delta, 4),
        "significance": round(exp.significance, 4),
        # guardrail + qualification (R24/R29).
        "guardrail": exp.guardrail,  # 'pass' | 'warn' | 'trip' (drives the chip color, not text)
        "guardrail_reason": exp.guardrail_reason,
        "champion_qual_acc": round(exp.champion_qual_acc, 4),
        "challenger_qual_acc": round(exp.challenger_qual_acc, 4),
        # the declared one-dimension diff (R17), translated to a readable name + the raw description.
        "dimension": exp.dimension,  # internal slug — for client logic only, never rendered
        "dimension_label": labels.dimension_label(exp.dimension),
        "declared_diff": exp.declared_diff,
        "diff_description": exp.diff_description,
        "is_extreme": exp.is_extreme,
        "population": exp.population,
        "n": exp.n,
        "target": exp.target,
        "kb_version": exp.kb_version,
        # lifecycle: the raw `state` for client branching + a human label for the chip.
        "state": exp.state,
        "state_label": labels.experiment_state_label(exp.state),
        "created_at": exp.created_at.isoformat() if exp.created_at else None,
    }


def lineage_to_dict(node: VersionLineage) -> dict[str, Any]:
    """Flatten one lineage node for the version history (P9). The KPI snapshot is passed through as
    stored (already numeric, no internal index); the dimension inside it is labeled."""
    kpi = node.kpi or {}
    dim = kpi.get("dimension")
    return {
        "version": node.version,
        "parent_version": node.parent_version,
        "kb_version": node.kb_version,
        "is_champion": node.is_champion,
        "kpi": kpi,
        "dimension_label": labels.dimension_label(dim) if dim else None,
        "created_at": node.created_at.isoformat() if node.created_at else None,
    }


# ---------------------------------------------------------------------------
# Draft-challenger construction (P8 KB/Playbook SAVE) — REUSES the generator's pure mutators
# ---------------------------------------------------------------------------

# How far down the player a save shifts to keep the diff minimal + non-extreme. A playbook
# discovery-sequence edit swaps the two leading slots; a KB edit appends a draft kb_version suffix.
_DRAFT_SUFFIX = "d1"


def _draft_challenger_record(
    champion: AgentConfig,
    *,
    surface: str,
    name: str,
    body: Optional[dict[str, Any]],
    seed: int,
) -> ExperimentRecord:
    """Build a DRAFT CHALLENGER as an ExperimentRecord WITHOUT mutating the live champion (R20).

    The save forks the champion config via the generator's PURE deep-copy mutators (reorder_discovery
    / mutate a non-pricing playbook / bump kb_version) — the champion AgentConfig is never touched —
    and wraps it as a Challenger (_build_challenger enforces the R17 minimal one-dimension diff).
    The result is persisted as a `running` ExperimentRecord; version_lineage is NEVER written here,
    so get_champion()/the live config are unchanged. Returns the draft record (not yet saved).
    """
    if surface == "playbook":
        # Discovery-sequence edit: reorder via the generator's pure mutator (the demo text dim). If a
        # client supplied an explicit sequence, honor it; else swap the two leading slots minimally.
        seq = list(champion.playbooks.get("discovery_sequence") or [])
        new_seq = (
            list(body["discovery_sequence"])
            if body and isinstance(body.get("discovery_sequence"), list)
            else _swap_first_two(seq)
        )
        challenger_config = reorder_discovery(champion, new_seq)
        diff_desc = f"reorder discovery_sequence -> {new_seq}"
    elif surface == "kb":
        # KB edit: a non-pricing playbook rebuttal change is the minimal, non-extreme KB diff in this
        # build (the rebuttal map is the KB-backed surface). Bump the draft kb_version for display.
        challenger_config = _mutate_rebuttal(champion, body)
        diff_desc = "edit objection rebuttal (KB draft)"
    else:  # pragma: no cover - guarded by the caller
        raise ValueError(f"unknown draft surface {surface!r}")

    challenger = _build_challenger(
        champion, challenger_config, dimension_hint=None, seed=seed, diff_description=diff_desc
    )
    draft_kb = (
        f"{champion.kb_version}-{_DRAFT_SUFFIX}" if surface == "kb" else champion.kb_version
    )
    return ExperimentRecord(
        experiment_id=f"DRAFT-{challenger.challenger_version}",
        challenger_version=challenger.challenger_version,
        parent_version=champion.version,
        name=name,
        dimension=challenger.dimension,
        declared_diff=sorted(declared_diff(champion, challenger.config)),
        diff_description=challenger.diff_description,
        population="Draft · not yet sampled",
        n=0,
        target=0,
        kb_version=draft_kb,
        is_extreme=challenger.is_extreme,
        state="running",  # a draft challenger is an active (not-yet-evaluated) experiment
    )


def _swap_first_two(seq: list[str]) -> list[str]:
    """Minimal deterministic reorder: swap the first two slots (a one-dimension discovery edit)."""
    out = list(seq)
    if len(out) >= 2:
        out[0], out[1] = out[1], out[0]
    return out


def _mutate_rebuttal(champion: AgentConfig, body: Optional[dict[str, Any]]) -> AgentConfig:
    """Fork the champion's objection-rebuttal playbook into a draft (deep-copy via reorder_discovery's
    sibling path). Edits playbooks.rebuttals only — a non-pricing, non-extreme single-dimension diff.
    The champion config is never mutated (reorder_discovery / mutate_threshold deep-copy)."""
    import copy

    playbooks = copy.deepcopy(champion.playbooks)
    rebuttals = dict(playbooks.get("rebuttals") or {})
    if body and isinstance(body.get("rebuttals"), dict):
        rebuttals.update(body["rebuttals"])
    else:
        # A default minimal edit so a bare save still produces a real one-dimension diff.
        rebuttals["price"] = (rebuttals.get("price", "") or "") + " (draft: ROI-first reframe)"
    playbooks["rebuttals"] = rebuttals
    return AgentConfig(
        version=champion.version,
        kb_version=champion.kb_version,
        persona=champion.persona,
        prompts=copy.deepcopy(champion.prompts),
        playbooks=playbooks,
        thresholds=copy.deepcopy(champion.thresholds),
    )


# ---------------------------------------------------------------------------
# Request DTOs
# ---------------------------------------------------------------------------


class SaveDraftRequest(BaseModel):
    """A KB/playbook SAVE. `name` titles the draft challenger; `body` carries the edited content
    (optional — a bare save still forks a minimal one-dimension draft)."""

    name: str = "Draft challenger"
    body: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

# Active lab tab states (P6 'Active'): in-flight + decided-but-not-shipped.
_ACTIVE_STATES = frozenset({"draft", "running", "passed", "blocked"})


def create_improve_router(
    *,
    improve_store: Optional[ImproveStore] = None,
    champion_config_provider: Optional[Callable[[], AgentConfig]] = None,
) -> APIRouter:
    """Build the /api Improve router. `improve_store` is injectable (default Postgres store, tests
    pass a seeded fake). `champion_config_provider` returns the champion AgentConfig a KB/playbook
    SAVE forks into a draft (default: load_config('champion_v0')). All endpoints are async."""
    store: ImproveStore = improve_store if improve_store is not None else _StoreBackedImproveStore()
    get_config = champion_config_provider or (lambda: load_config("champion_v0"))
    router = APIRouter()

    # --- P6 Experiment Lab -------------------------------------------------------------------
    @router.get("/api/experiments")
    async def list_experiments_ep(state: Optional[str] = None, limit: int = 200) -> dict[str, Any]:
        """The lab list (P6): the before/after fields + declared diff + state per experiment. The
        discovery-sequencing experiment's before/after is queryable end-to-end here (the demo
        artifact). Optional `state` filter; otherwise all, newest-first."""
        exps = await store.list_experiments(state=state, limit=limit)
        rows = [experiment_to_dict(e) for e in exps]
        active = [r for r in rows if r["state"] in _ACTIVE_STATES]
        past = [r for r in rows if r["state"] not in _ACTIVE_STATES]
        return {
            "experiments": rows,
            "count": len(rows),
            "counts": {"active": len(active), "past": len(past)},
        }

    # --- P7 Approval Queue -------------------------------------------------------------------
    @router.get("/api/approvals")
    async def list_approvals_ep(limit: int = 200) -> dict[str, Any]:
        """The human-approval queue (P7): experiments in `blocked` state (extreme challengers that
        passed the bar but breach a guardrail — R19/AE6). Each shows WHY it's extreme + the lift."""
        blocked = await store.list_experiments(state="blocked", limit=limit)
        return {"approvals": [experiment_to_dict(e) for e in blocked], "count": len(blocked)}

    @router.post("/api/approvals/{experiment_id}/approve")
    async def approve_ep(experiment_id: str) -> dict[str, Any]:
        """Approve a blocked challenger -> PROMOTE it to champion (R19). Records the challenger as the
        new champion via record_version (single-champion invariant demotes the prior champion), then
        flips the experiment state -> promoted. Only a `blocked` experiment may be approved."""
        exp = await store.get_experiment(experiment_id)
        if exp is None:
            raise HTTPException(status_code=404, detail=f"unknown experiment {experiment_id!r}")
        if exp.state != "blocked":
            raise HTTPException(
                status_code=409,
                detail=f"only a blocked experiment can be approved (state={exp.state!r})",
            )
        # Promote: the challenger becomes the live champion. record_version enforces single-champion.
        lineage = VersionLineage(
            version=exp.challenger_version,
            parent_version=exp.parent_version,
            kb_version=exp.kb_version,
            kpi={
                "dimension": exp.dimension,
                "champion_kpi": exp.champion_kpi,
                "challenger_kpi": exp.challenger_kpi,
                "delta": exp.delta,
                "delta_ci": exp.delta_ci,
                "enroll_delta": exp.enroll_delta,
                "champion_qual_acc": exp.champion_qual_acc,
                "challenger_qual_acc": exp.challenger_qual_acc,
            },
            is_champion=True,
        )
        await store.record_version(lineage)
        exp.state = "promoted"
        await store.save_experiment(exp)
        return {"experiment": experiment_to_dict(exp), "champion_version": exp.challenger_version}

    @router.post("/api/approvals/{experiment_id}/reject")
    async def reject_ep(experiment_id: str) -> dict[str, Any]:
        """Reject a blocked challenger -> state `rejected`. The champion is LEFT UNCHANGED (no
        record_version call), so get_champion() still returns the prior champion."""
        exp = await store.get_experiment(experiment_id)
        if exp is None:
            raise HTTPException(status_code=404, detail=f"unknown experiment {experiment_id!r}")
        if exp.state != "blocked":
            raise HTTPException(
                status_code=409,
                detail=f"only a blocked experiment can be rejected (state={exp.state!r})",
            )
        exp.state = "rejected"
        await store.save_experiment(exp)
        return {"experiment": experiment_to_dict(exp)}

    # --- P8 KB / Playbook Editor -------------------------------------------------------------
    @router.get("/api/kb")
    async def get_kb_ep() -> dict[str, Any]:
        """The current champion config's KB reference (P8 left tree / draft banner). Read-only."""
        cfg = get_config()
        return {
            "kb_version": cfg.kb_version,
            "version": cfg.version,
            "rebuttals": cfg.playbooks.get("rebuttals", {}),
        }

    @router.get("/api/playbook")
    async def get_playbook_ep() -> dict[str, Any]:
        """The current champion config's playbook (P8 editor view). Read-only — editing happens via
        POST, which forks a draft challenger rather than mutating this live config (R20)."""
        cfg = get_config()
        return {
            "version": cfg.version,
            "kb_version": cfg.kb_version,
            "discovery_sequence": cfg.playbooks.get("discovery_sequence", []),
            "rebuttals": cfg.playbooks.get("rebuttals", {}),
            "thresholds": cfg.thresholds,
        }

    @router.post("/api/kb")
    async def save_kb_ep(req: SaveDraftRequest) -> dict[str, Any]:
        """SAVE a KB edit -> a DRAFT CHALLENGER (a new `running` experiment). The live champion config
        / version is UNCHANGED (no record_version), per R20 — it's a draft until promoted."""
        cfg = get_config()
        draft = _draft_challenger_record(
            cfg, surface="kb", name=req.name, body=req.body, seed=0
        )
        await store.save_experiment(draft)
        return {"draft": experiment_to_dict(draft), "is_draft": True}

    @router.post("/api/playbook")
    async def save_playbook_ep(req: SaveDraftRequest) -> dict[str, Any]:
        """SAVE a playbook edit (e.g. discovery_sequence) -> a DRAFT CHALLENGER. The live champion is
        UNCHANGED (no record_version), per R20."""
        cfg = get_config()
        draft = _draft_challenger_record(
            cfg, surface="playbook", name=req.name, body=req.body, seed=0
        )
        await store.save_experiment(draft)
        return {"draft": experiment_to_dict(draft), "is_draft": True}

    # --- P9 Version History & Rollback -------------------------------------------------------
    @router.get("/api/versions")
    async def list_versions_ep(limit: int = 200) -> dict[str, Any]:
        """The lineage tree + current champion (P9). Newest-first; the champion is flagged so the UI
        marks it and disables rollback to itself."""
        nodes = await store.list_lineage(limit=limit)
        champ = await store.get_champion()
        return {
            "versions": [lineage_to_dict(n) for n in nodes],
            "count": len(nodes),
            "champion_version": champ.version if champ else None,
        }

    @router.post("/api/versions/{version}/rollback")
    async def rollback_ep(version: str) -> dict[str, Any]:
        """Roll back to a PRIOR version -> re-promote it as champion via record_version (the single-
        champion invariant demotes the current champion and restores `version`). get_champion() then
        returns `version`."""
        node = await store.get_lineage(version)
        if node is None:
            raise HTTPException(status_code=404, detail=f"unknown version {version!r}")
        node.is_champion = True
        await store.record_version(node)
        return {"champion_version": version, "version": lineage_to_dict(node)}

    return router

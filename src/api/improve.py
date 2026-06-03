# Operator-dashboard IMPROVE API (plan U16 — Improve mode). The 4 Improve screens read/write JSON
# through these endpoints: P6 Experiment Lab (GET /api/experiments — the before/after champion-vs-
# challenger records; POST /api/experiments/run kicks off a live A/B between the champion and a single
# changed value ASYNCHRONOUSLY — it persists a `running` record + returns 202 IMMEDIATELY, then runs
# the (paid, minutes-long) A/B in a BACKGROUND task that settles the SAME record to its terminal state
# on completion/timeout/crash (CB-15/CB-20: the button never hangs and a run never lingers `running`
# forever); POST /api/experiments/scaffold builds a draft experiment seeded from a reviewed call so the
# review page can open a per-experiment review before running, CB-19; GET /api/experiments/{id} returns
# ONE experiment's detail + its A/B "mock calls" — the champion-arm + challenger-arm self-play episodes
# the run persisted, each openable in Review, plus the failure reason, CB-24/CB-25), P7 Approval Queue (GET /api/approvals = experiments in
# `blocked` state, plus POST .../approve -> promote the challenger to champion, POST .../reject), P8
# KB/Playbook Editor (GET /api/kb returns the REAL grounded corpus from the kb_chunk table grouped by
# section — the facts/objection-rebuttals the agent grounds answers on, NOT placeholder strings; GET
# /api/playbook shows the current champion config; POST /api/kb + /api/playbook SAVE a DRAFT CHALLENGER
# — a NEW experiment record built via the generator's pure config mutators that NEVER touches
# version_lineage, so the live champion is unchanged, R20), and P9 Version History (GET /api/versions =
# the lineage tree + champion; POST /api/versions/{v}/rollback re-promotes a prior version).
# DB-INJECTABLE the same way src.api.operate is: an ImproveStore protocol abstracts the data layer so
# create_improve_router(improve_store=...) takes the real src.memory.store in prod and a seeded
# in-memory fake in tests (no Postgres). The run endpoint also takes an injected LLM factory (prod: a
# REAL OpenRouterClient — a run SPENDS CREDIT; tests: a free MockLLMClient) and an optional injectable
# runner (tests stack a deterministic deck). REUSES the loop (generator mutators, run_experiment,
# evaluate_promotion, experiment_record_from) and src.api.labels so NO raw internal index (dimension
# slug, state slug, version internals) ever renders in operator-facing text.
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional, Protocol

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from src.api import labels
from src.config.settings import AgentConfig, load_config
from src.core.llm import LLMClient
from src.loop.experiment import experiment_record_from, frozen_held_out, run_experiment
from src.loop.generator import (
    apply_dimension_changes,
    build_challenger,
    build_multi_challenger,
    declared_diff,
    mutate_threshold,
    reorder_discovery,
)
from src.memory.schema import Episode
from src.loop.promotion import QUAL_TOLERANCE, PromotionDecision, evaluate_promotion
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

    # The REAL grounded corpus for P8: the kb_chunk rows for a kb_version, each a {"source", "text"}
    # mapping (source is "<section>#<id>"). Optional — the router degrades to an empty corpus when a
    # store does not implement it (e.g. a fake seeded only with experiments). The Postgres default
    # impl queries the kb_chunk table.
    async def list_kb_chunks(self, *, kb_version: str) -> list[dict[str, Any]]: ...

    # CB-25: the A/B "mock calls" the experiment detail page lists are the per-arm self-play episodes
    # the background run produced — persisted here so /operate/review/<id> can open each in Review.
    # Both are OPTIONAL (the router guards with getattr): a fake store seeded only with experiments may
    # omit them, in which case the run simply doesn't persist arm episodes and the detail page shows
    # none. The Postgres default forwards to src.memory.store.save_episode / get_episode.
    async def save_episode(self, episode: Episode) -> str: ...

    async def get_episode(self, episode_id: str) -> Optional[Episode]: ...


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

    async def list_kb_chunks(self, *, kb_version: str) -> list[dict[str, Any]]:
        # The REAL grounded corpus the agent retrieves on. Read straight from the kb_chunk table for
        # the active kb_version (same pool/pattern as src.memory.store). Ordered by source so the
        # grouping/output is stable.
        pool = await self._store.get_pool()
        rows = await pool.fetch(
            "SELECT source, text FROM kb_chunk WHERE kb_version = $1 ORDER BY source",
            kb_version,
        )
        return [{"source": r["source"], "text": r["text"]} for r in rows]

    async def save_episode(self, episode: Episode) -> str:
        # CB-25: persist a per-arm A/B episode so the experiment detail page + Review can read it.
        return await self._store.save_episode(episode)

    async def get_episode(self, episode_id: str) -> Optional[Episode]:
        return await self._store.get_episode(episode_id)


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
        # the declared diff (R17 single-dimension; CB-27 multi for a manual multi-metric experiment),
        # translated to a readable name + the raw description.
        "dimension": exp.dimension,  # internal slug — for client logic only, never rendered
        # CB-27: the "multi" sentinel (a manual multi-metric experiment) labels as "Multiple changes"
        # so no bare sentinel renders; every other dimension goes through the shared label map.
        "dimension_label": (
            "Multiple changes" if exp.dimension == "multi" else labels.dimension_label(exp.dimension)
        ),
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


# ---------------------------------------------------------------------------
# CB-25 — per-arm A/B "mock call" episodes (deterministic, experiment-scoped ids)
# ---------------------------------------------------------------------------

# The detail page lists the champion-arm + challenger-arm self-play episodes a run produced. We do NOT
# add an episode-id column to the experiment record (the durable schema/columns are fixed); instead the
# run re-stamps each arm episode with a DETERMINISTIC, experiment-scoped id so the same ids are
# RE-DERIVABLE from the experiment_id + arm + index alone — no new persisted field needed. The detail
# endpoint reconstructs the id list from the record's `n` and fetches each via the store.
_ARM_ID_SEP = "::"


def arm_episode_id(experiment_id: str, arm: str, index: int) -> str:
    """A deterministic, experiment-scoped id for one A/B arm episode (CB-25). `arm` is "champion" or
    "challenger". Re-derivable from (experiment_id, arm, index) so the detail page can list arm calls
    without persisting their ids on the record."""
    return f"{experiment_id}{_ARM_ID_SEP}{arm}{_ARM_ID_SEP}{index}"


def arm_episode_ids(experiment_id: str, arm: str, n: int) -> list[str]:
    """The full ordered id list for one arm of an `n`-persona run (CB-25)."""
    return [arm_episode_id(experiment_id, arm, i) for i in range(max(0, n))]


def _stamp_arm_episodes(
    experiment_id: str, arm: str, episodes: list[Episode]
) -> list[Episode]:
    """Re-key a run's arm episodes with deterministic experiment-scoped ids + an arm cohort so the
    detail page can list them and Review (/operate/review/<id>) can open each (CB-25). The episodes
    otherwise keep everything the run produced (turns, outcome, belief)."""
    out: list[Episode] = []
    for i, ep in enumerate(episodes):
        ep.episode_id = arm_episode_id(experiment_id, arm, i)
        ep.cohort = f"experiment_{arm}"  # raw arm slug — for grouping/logic, labeled before render
        out.append(ep)
    return out


def arm_episode_summary(ep: Episode) -> dict[str, Any]:
    """A compact per-arm episode summary for the detail page (CB-25): enough to show the call's
    outcome + ladder + turn count and a link target for Review, without re-sending the full turn log.
    `outcome` is a raw outcome slug the client maps to a human label (never the raw slug as bold text).
    """
    return {
        "episode_id": ep.episode_id,
        "outcome": ep.outcome,
        "ladder_tier": int(ep.ladder_tier),
        "qualified": bool(ep.qualified),
        "turn_count": len(ep.turns),
        "version": ep.version,
    }


# CB-02 — recover the human CHANGE DIMENSION for a lineage node that has none stored.
#
# version_lineage rows carry the dimension under kpi["dimension"] when written by promote()/approve
# (both stamp it). But pre-existing/legacy rows store only {"ladder": ...}, so the version history
# rendered "CHANGE —" for a genuinely-promoted version. The NON-MIGRATION fix: look the dimension up
# from a matching `experiment` record. The lineage `version` and an experiment's `challenger_version`
# don't always share a literal string (e.g. "v1-1fb2bc0e" vs "champion_v1"), so we match on a NORMALIZED
# version BASE: strip the experiment-suffix ("__dim__seed"), strip a trailing short hash ("-1fb2bc0e"),
# and strip a leading "champion_". Genesis nodes (no parent) intentionally KEEP "—".


def _version_base(version: Optional[str]) -> str:
    """Normalize a lineage/experiment version to a comparable BASE so a lineage node can be matched to
    the experiment that produced it (CB-02). Drops the "__dimension__seed" experiment suffix, a trailing
    "-<hash>" disambiguator, and a leading "champion_" prefix — so "v1-1fb2bc0e" and "champion_v1" both
    reduce to "v1"."""
    if not version:
        return ""
    base = version.split("__", 1)[0]
    base = base.rsplit("-", 1)[0] if "-" in base else base
    if base.startswith("champion_"):
        base = base[len("champion_"):]
    return base


def build_dimension_index(experiments: list[ExperimentRecord]) -> dict[str, str]:
    """Map a normalized version BASE -> the change-dimension slug, from experiments (CB-02 backfill).

    PROMOTED/BLOCKED experiments win (a shipped/decided change is the authoritative source of a
    version's dimension); any other experiment fills a base only if nothing decided already claimed it.
    Used to recover the dimension for legacy lineage rows that stored no kpi["dimension"]."""
    decided: dict[str, str] = {}
    other: dict[str, str] = {}
    for e in experiments:
        if not e.dimension:
            continue
        base = _version_base(e.challenger_version)
        if not base:
            continue
        if e.state in ("promoted", "blocked"):
            decided.setdefault(base, e.dimension)
        else:
            other.setdefault(base, e.dimension)
    return {**other, **decided}  # decided overrides other on a base collision


def lineage_to_dict(
    node: VersionLineage, dimension_index: Optional[dict[str, str]] = None
) -> dict[str, Any]:
    """Flatten one lineage node for the version history (P9). The KPI snapshot is passed through as
    stored (already numeric, no internal index); the dimension inside it is labeled.

    CB-02: when the node stores no kpi["dimension"] AND it is NOT a genesis node (it has a parent —
    a genesis version legitimately shows "—"), recover the dimension from `dimension_index` (built from
    the experiment table) so a promoted version shows its real change label instead of "CHANGE —"."""
    kpi = node.kpi or {}
    dim = kpi.get("dimension")
    if not dim and node.parent_version and dimension_index:
        dim = dimension_index.get(_version_base(node.version))
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
# KB corpus grouping — kb_chunk rows -> sections the P8 left tree browses
# ---------------------------------------------------------------------------

# Operator-facing names for the section prefix of a kb_chunk source ("<section>#<id>"). An unknown
# prefix falls back to a titleized slug, so a NEW section added to the corpus still renders sensibly
# without a code change — counts always reflect what's actually in the table.
_KB_SECTION_LABELS: dict[str, str] = {
    "objections": "Objection rebuttals",
    "pricing": "Pricing & plans",
    "programs": "Programs & services",
    "policies": "Policies & guarantees",
    "competitors": "Competitor comparisons",
}


def _kb_section_label(slug: str) -> str:
    return _KB_SECTION_LABELS.get(slug, slug.replace("_", " ").replace("-", " ").strip().capitalize())


def _kb_chunk_title(text: str) -> str:
    """Derive a short, human title from a kb_chunk body. The corpus text is written as
    "<Title>. <body…>"; take the leading sentence — the first '. ' that precedes a capital with
    balanced parens and is not a known abbreviation (e.g./i.e.) — capped so it stays a label, not a
    paragraph. The full `text` is still returned as the body, so an imperfect title never hides
    content."""
    t = " ".join((text or "").split())
    title = t
    for m in re.finditer(r"\. (?=[A-Z(])", t):
        head = t[: m.start()]
        if head.count("(") != head.count(")"):
            continue  # the period sits inside a parenthetical — keep scanning
        if re.search(r"\b(e\.g|i\.e)$", head):
            continue  # an abbreviation, not a sentence end
        title = head
        break
    if len(title) > 90:
        title = title[:87].rstrip() + "…"
    return title or "(untitled)"


def kb_chunks_to_sections(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group raw kb_chunk rows (each {"source": "<section>#<id>", "text": ...}) into the sections the
    P8 left tree browses. Returns one entry per section: its raw slug (client logic only — never
    rendered), an operator-facing label, the chunk count, and the chunks (id + derived title + full
    body) so the panel can render THAT section's real grounding content. Order follows first
    appearance (the rows arrive sorted by source), so the tree is stable."""
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in chunks:
        source = str(row.get("source", ""))
        section, _, chunk_id = source.partition("#")
        section = section or "general"
        text = str(row.get("text", ""))
        if section not in grouped:
            grouped[section] = []
            order.append(section)
        grouped[section].append(
            {"id": chunk_id or source, "source": source, "title": _kb_chunk_title(text), "text": text}
        )
    return [
        {
            "id": section,  # raw slug — for client keys/logic only, never rendered as text
            "label": _kb_section_label(section),
            "count": len(grouped[section]),
            "chunks": grouped[section],
        }
        for section in order
    ]


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
    and wraps it as a Challenger (build_challenger enforces the R17 minimal one-dimension diff).
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

    challenger = build_challenger(
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


def _challenger_for(champion: AgentConfig, dimension: str, value: Any, *, seed: int):
    """Build a minimal-diff Challenger for a "run an A/B" request from one (dimension, value) pair.

    Supports the two loop-mutable run dimensions via the generator's PURE deep-copy mutators (the
    champion is never touched): "playbooks.discovery_sequence" (value = a reordered slot list, via
    reorder_discovery) and "thresholds.<key>" (value = a number, via mutate_threshold). build_challenger
    enforces the R17 minimal one-dimension diff and stamps is_extreme (a pricing threshold is extreme,
    R19). Raises ValueError for an unsupported dimension or a value of the wrong shape (validated at
    the boundary BEFORE any paid run starts)."""
    if dimension == "playbooks.discovery_sequence":
        if not isinstance(value, (list, tuple)) or not all(isinstance(s, str) for s in value):
            raise ValueError("discovery_sequence value must be a list of slot-name strings")
        chal_config = reorder_discovery(champion, list(value))
        desc = f"reorder discovery_sequence -> {list(value)}"
    elif dimension.startswith("thresholds."):
        key = dimension.split(".", 1)[1]
        if key not in champion.thresholds:
            raise ValueError(f"unknown threshold {key!r}")
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"threshold value must be a number, got {value!r}") from exc
        chal_config = mutate_threshold(champion, key, num)
        desc = f"set {key} -> {num} (was {champion.thresholds[key]})"
    else:
        raise ValueError(
            f"unsupported run dimension {dimension!r}: only 'playbooks.discovery_sequence' or "
            "'thresholds.<key>' can be A/B-tested"
        )
    # A value equal to the champion's is a no-op (zero changed dimensions) — reject BEFORE a paid run
    # instead of surfacing the opaque R17 minimal-diff error from build_challenger.
    if not declared_diff(champion, chal_config):
        raise ValueError(
            f"value for {dimension!r} matches the current champion — nothing to A/B test"
        )
    return build_challenger(
        champion, chal_config, dimension_hint=dimension, seed=seed, diff_description=desc
    )


def _multi_challenger_for(
    champion: AgentConfig, changes: list[tuple[str, Any]], *, seed: int
):
    """Build a MULTI-dimension Challenger for a manual experiment (CB-27).

    RELAXES the single-dimension R19 invariant DELIBERATELY: the operator chose to A/B several knobs at
    once. Applies every (dimension, value) row onto one deep copy via apply_dimension_changes (the
    champion is never touched), then wraps it with build_multi_challenger (is_extreme True if ANY row
    touches an extreme pricing/persona knob). A 1-row change reduces to a normal single-dimension diff.
    Raises ValueError (validated at the boundary BEFORE any paid run) for an unsupported dimension, a
    value of the wrong shape, or a change set that nets to no diff (every row matched the champion)."""
    if not changes:
        raise ValueError("a multi-metric experiment needs at least one metric row")
    chal_config = apply_dimension_changes(champion, changes)
    diff = declared_diff(champion, chal_config)
    if not diff:
        raise ValueError(
            "the metric rows all match the current champion — nothing to A/B test"
        )
    parts = [f"{dim.split('.')[-1]} -> {val}" for dim, val in changes]
    desc = "; ".join(parts)
    return build_multi_challenger(champion, chal_config, seed=seed, diff_description=desc)


def _running_record(
    experiment_id: str,
    champion: AgentConfig,
    challenger: Any,
    *,
    n: int,
    population: str,
    name: str,
) -> ExperimentRecord:
    """Build the `running` ExperimentRecord persisted the instant a run is kicked off (CB-15), BEFORE
    the A/B produces any comparison. The before/after fields stay at their neutral schema defaults
    (the background task upserts the SAME experiment_id with the real result on completion). Carries
    the declared one-dimension diff + is_extreme so the running card already shows what's being tested.
    """
    return ExperimentRecord(
        experiment_id=experiment_id,
        challenger_version=challenger.challenger_version,
        parent_version=challenger.parent_version,
        name=name,
        dimension=challenger.dimension,
        declared_diff=sorted(declared_diff(champion, challenger.config)),
        diff_description=challenger.diff_description,
        population=population,
        n=n,
        target=n,
        kb_version=challenger.config.kb_version,
        is_extreme=challenger.is_extreme,
        state="running",
    )


# ---------------------------------------------------------------------------
# Request DTOs
# ---------------------------------------------------------------------------


class SaveDraftRequest(BaseModel):
    """A KB/playbook SAVE. `name` titles the draft challenger; `body` carries the edited content
    (optional — a bare save still forks a minimal one-dimension draft)."""

    name: str = "Draft challenger"
    body: Optional[dict[str, Any]] = None


# Modest default sample size for a run-an-A/B request. COST: in the running app the injected LLM is a
# REAL OpenRouterClient, so each held-out persona is a paid self-play conversation per arm — keep N
# small. Tests inject a MockLLMClient + a deterministic runner (free). Capped so a UI typo can't bill.
_DEFAULT_RUN_N = 12
_MAX_RUN_N = 24
# Minimum per-arm sample. Mirrors grading._MIN_ARM_SAMPLE: below this the bootstrap CI cannot express
# uncertainty (n=1 collapses to a point), so a tiny run could fabricate a degenerate "significant"
# comparison. Floor every request to at least this many personas (still capped at _MAX_RUN_N).
_MIN_RUN_N = 5
# A fixed held-out seed so a given (dimension, value, n) re-runs the SAME frozen scenarios (R20).
_RUN_HELD_OUT_SEED = 7

# COST/availability bound for the A/B (CB-15: it is a full real self-play run — minutes, paid — so it
# runs in a BACKGROUND task, not inline, and is bounded + serialized rather than hanging the request or
# stacking paid work). The background run is wrapped in asyncio.wait_for(<budget>) and guarded by a
# semaphore of _RUN_CONCURRENCY permits acquired BEFORE the task is spawned: a 2nd run while one holds
# the permit returns 503 (busy) without starting more paid work; a run that exceeds the budget settles
# the record to a terminal `rejected` (CB-20 — never a perpetual `running`). A full job-queue is out of
# scope here.
#
# CB-40: the budget is NOT a fixed 180s any more. Since CB-15 made the run ASYNC (the request returns
# 202 and a background task settles the record), the cap protects no caller — it only acted as a
# guillotine that killed every honest real-LLM A/B (a less-pushy challenger holds LONGER conversations,
# so a modest run blew past 180s and settled "rejected — timed out"; confirmed at n=8 AND n=4). The
# budget now SCALES with the run's actual size (compute_run_timeout_s): both arms run sequentially, so
# the work is ~ n_per_arm * 2 arms * max_turns turns, each turn a paid LLM round-trip. We size to a
# per-turn budget with a generous floor (tiny runs still get minutes) and a sane absolute ceiling so a
# genuinely hung run still settles failed instead of running forever.
_DEFAULT_RUN_CONCURRENCY = 1
# Turns the A/B self-play is bounded to PER episode. Mirrors run_experiment / selfplay.run_batch's
# max_turns default (24); the background run passes this SAME value to run_experiment so the budget and
# the actual cap can never drift. Kept here so compute_run_timeout_s sizes from the real bound.
_RUN_MAX_TURNS = 24
# Per-turn wall-clock budget (s): amortized over a self-play episode (most episodes terminate well
# before max_turns, so this is a per-MAX-turn allowance, not a per-actual-turn one). Sized so a typical
# run (n up to the _MAX_RUN_N cap) lands inside the scaling band below the ceiling — the budget then
# genuinely scales with n + turns — while a pathological all-arms-run-to-the-cap job is what the ceiling
# catches. Still generous: a slow model or a retry must not trip the budget on a healthy run.
_RUN_SECONDS_PER_TURN = 3.0
# Floor: even a tiny run (n=5, the minimum sample) gets at least this long, so the budget never starves
# a legitimately short A/B. Comfortably above the old 180s cap.
_RUN_TIMEOUT_FLOOR_S = 300.0
# Absolute ceiling: a genuinely hung/runaway run still settles `rejected` rather than holding the single
# concurrency permit forever (CB-20). ~30 min.
_RUN_TIMEOUT_CEILING_S = 1800.0


def compute_run_timeout_s(
    n: int,
    max_turns: int = _RUN_MAX_TURNS,
    *,
    seconds_per_turn: float = _RUN_SECONDS_PER_TURN,
    floor_s: float = _RUN_TIMEOUT_FLOOR_S,
    ceiling_s: float = _RUN_TIMEOUT_CEILING_S,
) -> float:
    """The wall-clock budget (seconds) for a background A/B of `n` personas PER ARM at `max_turns`
    turns/episode (CB-40). Replaces the vestigial fixed 180s cap, which since CB-15 (async runs)
    protected no request and only killed honest real-LLM runs (a less-pushy challenger talks longer ->
    >180s -> falsely "timed out").

    Sizing: both arms run SEQUENTIALLY on the same frozen held-out set, so the run does about
    `n * 2 arms * max_turns` paid LLM turns; at `seconds_per_turn` each that is the estimate. The result
    is clamped to [floor_s, ceiling_s] so a tiny run still gets minutes (floor) and a genuinely hung run
    still settles failed (ceiling, CB-20). Monotonic non-decreasing in both `n` and `max_turns`. Inputs
    are coerced to non-negative so a degenerate 0 can't yield a negative budget (the floor wins)."""
    arms = 2
    estimate = max(0, int(n)) * arms * max(0, int(max_turns)) * float(seconds_per_turn)
    return float(min(max(estimate, float(floor_s)), float(ceiling_s)))


class MetricChange(BaseModel):
    """One {metric, new value} row of a multi-metric experiment (CB-27). `dimension` is the same
    mutation-surface label the single-dimension run path supports ("playbooks.discovery_sequence" or
    "thresholds.<key>"); `value` is the reordered slot list or the new number for that knob."""

    dimension: str
    value: Any


class RunExperimentRequest(BaseModel):
    """A "run an A/B between values" request (POST /api/experiments/run). `n` is the held-out sample
    size PER ARM — modest by default because each call spends real model credit in prod.

    SINGLE-DIMENSION (default): `dimension` is a mutation-surface label the loop supports
    ("playbooks.discovery_sequence" with `value` = a reordered slot list, or "thresholds.<key>" with
    `value` = the new number). This keeps the R19 single-dimension invariant.

    MULTI-METRIC (CB-27, explicit opt-in): supply `changes` — a list of {dimension, value} rows — to
    A/B more than one knob at once. This DELIBERATELY RELAXES the single-dimension R19 invariant for a
    MANUAL operator experiment (the operator chose to combine knobs); the challenger applies ALL rows
    and routes to human approval if any row touches an extreme pricing/persona knob. When `changes` is
    present it WINS over `dimension`/`value`; a one-row `changes` reduces to the single-dimension path.

    `episode_id` (optional, CB-19): the reviewed call this run was scaffolded from. It is carried onto
    the persisted record's population label so the lab/review surface can show "seeded from this call";
    it never changes the A/B itself (the held-out set is frozen by seed)."""

    dimension: Optional[str] = None
    value: Any = None
    changes: Optional[list[MetricChange]] = None
    n: int = _DEFAULT_RUN_N
    name: str = ""
    episode_id: Optional[str] = None


class ScaffoldExperimentRequest(BaseModel):
    """Scaffold a DRAFT experiment seeded from a reviewed call (CB-19, POST /api/experiments/scaffold).

    The review page hands the episode it is inspecting; this builds a NON-extreme draft (a discovery-
    sequence reorder by default — the safe, auto-promote-eligible dimension) WITHOUT running anything,
    persists it as a `draft` ExperimentRecord, and returns it so the lab's per-experiment review view
    can open it pre-populated (episode context attached) for the operator to confirm before running.
    The optional `dimension`/`value` let a caller pre-pick the change; omitted -> a minimal default
    reorder so the scaffold always yields a real one-dimension diff."""

    episode_id: str
    dimension: Optional[str] = None
    value: Any = None
    name: str = ""


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

# Active lab tab states (P6 'Active'): in-flight + decided-but-not-shipped.
_ACTIVE_STATES = frozenset({"draft", "running", "passed", "blocked"})


def create_improve_router(
    *,
    improve_store: Optional[ImproveStore] = None,
    champion_config_provider: Optional[Callable[[], AgentConfig]] = None,
    llm_client_factory: Optional[Callable[[], LLMClient]] = None,
    experiment_runner: Optional[Callable[..., Awaitable[list[Any]]]] = None,
    run_timeout_s: Optional[float] = None,
    run_max_concurrency: int = _DEFAULT_RUN_CONCURRENCY,
) -> APIRouter:
    """Build the /api Improve router. `improve_store` is injectable (default Postgres store, tests
    pass a seeded fake). `champion_config_provider` returns the champion AgentConfig a KB/playbook
    SAVE forks into a draft (default: load_config('champion_v0')).

    `llm_client_factory` builds the LLMClient both arms of POST /api/experiments/run use (prod: a REAL
    OpenRouterClient — a run SPENDS CREDIT; tests: a free MockLLMClient). `experiment_runner` is the
    optional run_experiment runner seam (tests inject a deterministic deck so the A/B is reproducible
    without self-play; prod leaves it None -> real self-play).

    `run_timeout_s` / `run_max_concurrency` BOUND the paid BACKGROUND A/B (CB-15): POST
    /api/experiments/run persists a `running` record + returns 202 immediately, then a background task
    runs the A/B wrapped in asyncio.wait_for(<budget>) and guarded by a semaphore of
    run_max_concurrency permits — a contended concurrent run returns 503 (busy) up front, and a run
    past the budget settles the record to a terminal state rather than hanging or lingering `running`
    forever (CB-20).

    CB-40: `run_timeout_s` defaults to None = AUTO — the budget is computed PER RUN from its size via
    compute_run_timeout_s(n, _RUN_MAX_TURNS) (scales with n and turns, floored to minutes, capped at
    ~30 min). The old fixed 180s cap killed every honest real-LLM A/B once CB-15 made runs async, so it
    is gone. Pass an explicit `run_timeout_s` ONLY to override the auto budget (tests set a tiny value
    to force a fast timeout deterministically). server.create_app uses the auto default. All endpoints
    are async."""
    store: ImproveStore = improve_store if improve_store is not None else _StoreBackedImproveStore()
    get_config = champion_config_provider or (lambda: load_config("champion_v0"))
    # One semaphore for the whole router instance bounds concurrent paid runs. Created here (3.10+
    # binds it to the loop lazily on first use), shared across requests served by this router.
    run_semaphore = asyncio.Semaphore(max(1, int(run_max_concurrency)))
    # Strong refs to in-flight background run tasks: asyncio only weakly references tasks, so without
    # this an A/B could be garbage-collected mid-run (mirrors demo_routes._demo_tasks, CB-08). Each
    # task discards itself from the set on completion.
    _run_tasks: "set[asyncio.Task[Any]]" = set()

    def _make_llm() -> LLMClient:
        if llm_client_factory is not None:
            return llm_client_factory()
        from src.core.llm import OpenRouterClient  # lazy: keep httpx/env off the test import path.

        return OpenRouterClient()

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

    async def _run_ab_background(
        *,
        experiment_id: str,
        champion: AgentConfig,
        challenger: Any,
        n: int,
        population: str,
        name: str,
    ) -> None:
        """Background task (CB-15/CB-20): run the (paid, minutes-long) A/B and SETTLE the already-
        persisted `running` record to its TERMINAL state — passed/blocked/rejected/promoted per the
        gate, or `rejected` on timeout/crash. The semaphore permit is held for the WHOLE run and
        released in the finally, so the concurrency guard is honest and a permit is never leaked.

        GUARANTEE (CB-20): the record can never linger `running` forever — every exit path (success,
        timeout, crash) re-saves the SAME experiment_id with a terminal state. Mirrors the CB-08
        server-side task discipline (try/except + always-settle in finally).

        FAIL-CLOSED (R36): this run does NOT measure sim-to-real divergence, so an otherwise auto-
        promote-eligible (clean, non-extreme) result is downgraded to pending_approval (blocked) —
        the central-risk divergence gate is never bypassed by this path.

        CB-24: a timeout settles with a SPECIFIC reason ("timed out after Ns"); a crash settles with
        the exception class name — not the generic "timed out or failed".

        CB-25: the SAME run's per-arm episodes are persisted with deterministic experiment-scoped ids
        (when the store can save episodes) so the detail page lists them + Review can open each.
        """
        settled = False
        # CB-24: a specific failure reason captured here is threaded to _settle_failed_run so the lab
        # explains a timeout vs an exception class rather than the generic "timed out or failed".
        failure_reason: Optional[str] = None
        # CB-40: size the budget to THIS run. AUTO (run_timeout_s is None) -> scale from n + the run's
        # own max_turns so a longer (less-pushy) A/B isn't falsely killed; an explicit override (tests)
        # wins so a tiny value can force a fast, deterministic timeout. Pass the SAME max_turns to
        # run_experiment so the budget and the actual turn cap can never drift.
        timeout_s = (
            run_timeout_s
            if run_timeout_s is not None
            else compute_run_timeout_s(n, _RUN_MAX_TURNS)
        )
        try:
            held_out = frozen_held_out(seed=_RUN_HELD_OUT_SEED, n=n, rotation_index=0)
            experiment = await asyncio.wait_for(
                run_experiment(
                    champion,
                    challenger,
                    held_out,
                    _make_llm,
                    _make_llm,
                    seed=_RUN_HELD_OUT_SEED,
                    max_turns=_RUN_MAX_TURNS,
                    runner=experiment_runner,
                ),
                timeout=timeout_s,
            )
            # CB-25: persist the A/B "mock calls" (both arms) with deterministic ids before settling, so
            # the detail page can list them and Review can open each. Best-effort — a persist hiccup
            # must not flip an otherwise-good run to failed (it only costs the arm-call drill-down).
            await _persist_arm_episodes(experiment_id, experiment)
            decision = evaluate_promotion(experiment, is_extreme=challenger.is_extreme)
            if decision.status == "promoted":
                decision = PromotionDecision(
                    status="pending_approval",
                    promote=False,
                    requires_human=True,
                    reasons=[
                        "passed the bar but sim-to-real divergence was NOT measured on this run "
                        "(R36) — routed to human approval instead of auto-promoting (fail-closed)"
                    ],
                )
            record = experiment_record_from(
                experiment,
                decision,
                challenger=challenger,
                n=n,
                target=n,
                population=population,
                name=name,
                experiment_id=experiment_id,  # SAME id -> upserts the `running` row to its result
            )
            await store.save_experiment(record)
            settled = True
        except asyncio.TimeoutError:
            # CB-24: distinguish a timeout — show how long it ran before the budget tripped. CB-40: the
            # budget is the size-scaled (or overridden) value computed above, not a fixed 180s.
            failure_reason = (
                f"the run timed out after {timeout_s:g}s — no result recorded"
            )
            logger.warning("experiment run %s timed out after %ss", experiment_id, timeout_s)
        except Exception as exc:
            # CB-24: name the failure (the exception class) instead of the generic "failed".
            failure_reason = (
                f"the run failed with an error ({type(exc).__name__}) — no result recorded"
            )
            logger.warning("experiment run %s crashed", experiment_id, exc_info=True)
        finally:
            # CB-20 guard: if the run never produced a terminal record (timeout/crash), settle the
            # row to `rejected` so it can NEVER stay `running` forever. Swallow a settle failure so a
            # hiccup here doesn't leak the task — the run is already off the request path.
            if not settled:
                try:
                    await _settle_failed_run(
                        experiment_id, champion, challenger, n, population, name,
                        reason=failure_reason,
                    )
                except Exception:
                    logger.warning("failed to settle stuck run %s to terminal", experiment_id)
            run_semaphore.release()

    async def _persist_arm_episodes(experiment_id: str, experiment: Any) -> None:
        """CB-25: persist the run's champion-arm + challenger-arm episodes with deterministic,
        experiment-scoped ids so the detail page lists them and Review (/operate/review/<id>) opens
        each. Best-effort: if the injected store can't save episodes (no save_episode), or a save
        hiccups, skip silently — the arm-call drill-down is a nice-to-have, never a reason to fail an
        otherwise-good run."""
        save = getattr(store, "save_episode", None)
        if save is None:
            return
        try:
            arms = (
                ("champion", list(getattr(experiment, "champion_eps", []) or [])),
                ("challenger", list(getattr(experiment, "challenger_eps", []) or [])),
            )
            for arm, episodes in arms:
                for ep in _stamp_arm_episodes(experiment_id, arm, episodes):
                    await save(ep)
        except Exception:
            logger.warning("failed to persist A/B arm episodes for run %s", experiment_id)

    async def _settle_failed_run(
        experiment_id: str,
        champion: AgentConfig,
        challenger: Any,
        n: int,
        population: str,
        name: str,
        *,
        reason: Optional[str] = None,
    ) -> None:
        """Re-save a timed-out/crashed run's record in a TERMINAL `rejected` state (CB-20) — never
        `running`. Keeps the before/after fields at their neutral defaults (the A/B never produced a
        comparison) and carries a clear guardrail_reason so the lab card explains the failure.

        CB-24: `reason` is the SPECIFIC failure detail captured by the background task (a timeout's
        elapsed budget, or the exception class) — falls back to a generic message only when the caller
        couldn't classify the failure."""
        record = _running_record(
            experiment_id, champion, challenger, n=n, population=population, name=name
        )
        record.state = "rejected"
        record.guardrail = "trip"
        record.guardrail_reason = reason or (
            "the run did not complete (timed out or failed) — no result recorded"
        )
        await store.save_experiment(record)

    @router.post("/api/experiments/run", status_code=202)
    async def run_experiment_ep(req: RunExperimentRequest) -> JSONResponse:
        """Kick off an A/B between the current champion and a single changed value, ASYNCHRONOUSLY
        (P6 "Run experiment"; CB-15).

        Builds the champion config + a minimal-diff challenger for {dimension, value} via the
        generator's pure mutators (is_extreme set per R19), PERSISTS a `running` ExperimentRecord, and
        RETURNS IT IMMEDIATELY (202) — so the UI button never hangs (it closes the drawer + shows the
        running card at once; the existing /api/experiments poll settles it). The full (paid, minutes-
        long in prod) self-play A/B then runs in a BACKGROUND task that settles the SAME record to its
        terminal state on completion/timeout/crash (CB-20 — never a perpetual `running`).

        CONCURRENCY GUARD (kept): a 2nd run while one holds the permit returns 503 (busy) up front,
        without starting more paid work — never a frozen button.

        COST: in the running app the injected client is a REAL OpenRouterClient, so the background run
        spends model credit — `n` is modest by default (floored to a minimum sample, capped at the
        max). Tests inject a MockLLMClient + a deterministic runner (free). Input is validated at the
        boundary (400) BEFORE anything is persisted or any paid run is spawned.
        """
        # Floor N to the minimum trustworthy sample (>=_MIN_RUN_N) and cap it — never run a degenerate
        # tiny sample that the grading floor would reject anyway, and never let a UI typo bill a huge N.
        n = max(_MIN_RUN_N, min(int(req.n), _MAX_RUN_N))
        champion = get_config()
        try:
            if req.changes:
                # CB-27: a multi-metric experiment (explicit opt-in) — relaxes the single-dimension
                # invariant for this MANUAL run. `changes` wins over the single `dimension`/`value`.
                challenger = _multi_challenger_for(
                    champion,
                    [(c.dimension, c.value) for c in req.changes],
                    seed=_RUN_HELD_OUT_SEED,
                )
            elif req.dimension is not None:
                challenger = _challenger_for(
                    champion, req.dimension, req.value, seed=_RUN_HELD_OUT_SEED
                )
            else:
                raise ValueError("a run needs a `dimension`+`value` or a `changes` list")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Concurrency guard: refuse to START a second paid run while one holds the permit (-> 503 busy,
        # NOT a hang). Acquire BEFORE spawning so the background task owns the permit for its whole run.
        if run_semaphore.locked():
            raise HTTPException(
                status_code=503,
                detail="an experiment run is already in progress — retry once it completes",
            )
        await run_semaphore.acquire()

        # Persist a `running` record + return it immediately. The background task upserts this SAME id.
        experiment_id = f"RUN-{challenger.challenger_version}"
        population = f"Held-out · {n} personas"
        if req.episode_id:  # CB-19: a run scaffolded from a reviewed call carries its origin.
            population = f"{population} · seeded from a reviewed call"
        name = req.name or challenger.diff_description
        running = _running_record(
            experiment_id, champion, challenger, n=n, population=population, name=name
        )
        try:
            await store.save_experiment(running)
        except Exception:
            run_semaphore.release()  # never leak the permit if the initial persist fails.
            raise

        task = asyncio.create_task(
            _run_ab_background(
                experiment_id=experiment_id,
                champion=champion,
                challenger=challenger,
                n=n,
                population=population,
                name=name,
            )
        )
        _run_tasks.add(task)
        task.add_done_callback(_run_tasks.discard)

        # 202 Accepted: the run is started, not finished. `decision` is "running" until the poll settles.
        return JSONResponse(
            status_code=202,
            content={"experiment": experiment_to_dict(running), "decision": "running"},
        )

    @router.post("/api/experiments/scaffold")
    async def scaffold_experiment_ep(req: ScaffoldExperimentRequest) -> dict[str, Any]:
        """Scaffold a DRAFT experiment seeded from a reviewed call (CB-19) — WITHOUT running anything.

        The review page's "Use in experiment" hands the episode it is inspecting; this builds a minimal
        one-dimension challenger (a discovery-sequence reorder by default — the safe, non-extreme,
        auto-promote-eligible dimension; an explicit dimension/value overrides), persists it as a
        `draft` ExperimentRecord whose population carries the originating call, and returns it. The lab
        opens a per-experiment REVIEW view (/improve/lab?experiment=<id>) pre-populated from this draft
        so the operator confirms it before launching the (async, paid) run — not a blank lab landing.

        The live champion config / version is UNCHANGED (no record_version) — a scaffold is a draft."""
        champion = get_config()
        dimension = req.dimension or "playbooks.discovery_sequence"
        value = req.value
        if value is None and dimension == "playbooks.discovery_sequence":
            # Default minimal reorder so the scaffold always yields a real one-dimension diff.
            value = _swap_first_two(list(champion.playbooks.get("discovery_sequence") or []))
        try:
            challenger = _challenger_for(champion, dimension, value, seed=_RUN_HELD_OUT_SEED)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        experiment_id = f"SCAFFOLD-{challenger.challenger_version}"
        draft = ExperimentRecord(
            experiment_id=experiment_id,
            challenger_version=challenger.challenger_version,
            parent_version=champion.version,
            name=req.name or challenger.diff_description,
            dimension=challenger.dimension,
            declared_diff=sorted(declared_diff(champion, challenger.config)),
            diff_description=challenger.diff_description,
            population=f"Scaffolded from a reviewed call · {req.episode_id}",
            n=0,
            target=_DEFAULT_RUN_N,
            kb_version=challenger.config.kb_version,
            is_extreme=challenger.is_extreme,
            state="draft",
        )
        await store.save_experiment(draft)
        return {
            "experiment": experiment_to_dict(draft),
            "episode_id": req.episode_id,
            "is_scaffold": True,
        }

    @router.get("/api/experiments/{experiment_id}")
    async def experiment_detail_ep(experiment_id: str) -> dict[str, Any]:
        """ONE experiment's detail + its A/B "mock calls" (CB-25): the same flat experiment DTO the lab
        cards use, PLUS the per-arm self-play episodes the run produced — the champion-arm + challenger-
        arm calls, each with a compact summary (outcome/ladder/turns) and an `episode_id` the UI links
        to Review (/operate/review/<id>). The arm episode ids are DETERMINISTIC (experiment-scoped), so
        they're reconstructed from the record's `n` and fetched from the store — no episode-id column on
        the record. Arms come back empty when the store can't read episodes or the run didn't persist
        them (e.g. a draft, or a timed-out/crashed run). `guardrail_reason` (CB-24) is on the experiment
        DTO so the page can explain a rejection/block. 404 for an unknown id."""
        exp = await store.get_experiment(experiment_id)
        if exp is None:
            raise HTTPException(status_code=404, detail=f"unknown experiment {experiment_id!r}")

        get_ep = getattr(store, "get_episode", None)

        async def _arm(arm: str) -> list[dict[str, Any]]:
            if get_ep is None or exp.n <= 0:
                return []
            calls: list[dict[str, Any]] = []
            for eid in arm_episode_ids(experiment_id, arm, exp.n):
                ep = await get_ep(eid)
                if ep is not None:
                    calls.append(arm_episode_summary(ep))
            return calls

        champion_calls = await _arm("champion")
        challenger_calls = await _arm("challenger")
        return {
            "experiment": experiment_to_dict(exp),
            "champion_calls": champion_calls,
            "challenger_calls": challenger_calls,
            # convenience flag for the UI empty-state: did this run persist any arm calls?
            "has_calls": bool(champion_calls or challenger_calls),
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
        flips the experiment state -> promoted. Only a `blocked` experiment may be approved.

        DEFENSE-IN-DEPTH (Fix): a human approval CANNOT bypass the bar. Before promoting, re-assert the
        recorded result still meets the promotion bar — challenger_better AND guardrail=='pass' (no
        regression, R24) AND qualification held within tolerance (R29) AND is_extreme (only an extreme
        pass should be sitting in the human queue). A record that does not meet the bar (e.g. stuck in
        `blocked` with a tripped guardrail) is 409'd rather than promoted."""
        exp = await store.get_experiment(experiment_id)
        if exp is None:
            raise HTTPException(status_code=404, detail=f"unknown experiment {experiment_id!r}")
        if exp.state != "blocked":
            raise HTTPException(
                status_code=409,
                detail=f"only a blocked experiment can be approved (state={exp.state!r})",
            )
        # Re-check the bar BEFORE recording the version (the gate is not bypassable via approve).
        qual_held = exp.challenger_qual_acc >= exp.champion_qual_acc - QUAL_TOLERANCE
        meets_bar = (
            exp.challenger_better
            and exp.guardrail == "pass"
            and qual_held
            and exp.is_extreme
        )
        if not meets_bar:
            raise HTTPException(
                status_code=409,
                detail=(
                    "experiment no longer meets the promotion bar — refusing to promote on approve "
                    f"(challenger_better={exp.challenger_better}, guardrail={exp.guardrail!r}, "
                    f"qualification_held={qual_held}, is_extreme={exp.is_extreme})"
                ),
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
        """The REAL grounded corpus the agent retrieves on, for the P8 KB browser (read-only).

        Returns the kb_chunk rows for the champion's active kb_version GROUPED BY section (the prefix
        of each source "<section>#<id>") — so the operator browses the actual facts / objection
        rebuttals the agent grounds answers on, organized by section, with counts that match what is
        shown. The champion config's placeholder rebuttal strings are intentionally DROPPED from this
        response so no PLACEHOLDER text ever reaches the operator — `sections` is the corpus the UI
        renders. The draft-save path (POST /api/kb) forks the champion config directly, so it does not
        depend on this read returning the rebuttal map.

        Degrades safely: if the injected store has no list_kb_chunks (e.g. a fake seeded only with
        experiments) or the table is empty, `sections` is [] and the page shows an empty-state rather
        than failing.
        """
        cfg = get_config()
        chunks: list[dict[str, Any]] = []
        list_chunks = getattr(store, "list_kb_chunks", None)
        if list_chunks is not None:
            chunks = await list_chunks(kb_version=cfg.kb_version)
        sections = kb_chunks_to_sections(chunks)
        return {
            "kb_version": cfg.kb_version,
            "version": cfg.version,
            "sections": sections,
            "total_chunks": sum(s["count"] for s in sections),
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
        marks it and disables rollback to itself.

        CB-02: builds a dimension index from the experiment table so a promoted version whose lineage
        row stored no change-dimension still renders its real change label (not "CHANGE —")."""
        nodes = await store.list_lineage(limit=limit)
        champ = await store.get_champion()
        # CB-02 backfill: recover the change dimension for legacy lineage rows from the experiment table.
        experiments = await store.list_experiments(limit=500)
        dim_index = build_dimension_index(experiments)
        return {
            "versions": [lineage_to_dict(n, dim_index) for n in nodes],
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

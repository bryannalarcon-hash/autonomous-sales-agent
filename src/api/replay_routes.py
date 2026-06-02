# Replay-build HTTP router (plan U15+): builds fidelity experiments from all completed real calls.
# Exposes POST /api/replay/build (start background job) and GET /api/replay/results (progress/done).
# Concurrency is bounded via asyncio.Semaphore(3). Each create_replay_router() call owns its own
# _ReplayStore instance (closure-local, not module-global) so test isolation is preserved when
# multiple apps are built in the same process. No DB writes. Collaborators: src.loop.replay.replay_call,
# src.api.operate (_is_completed), src.api.labels (_titleize), src.config.settings.AgentConfig,
# src.core.llm.LLMClient. The background job is ONLY started from POST — never at import/mount time.
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api import labels
from src.api.operate import ReadStore, _is_completed
from src.config.settings import AgentConfig
from src.core.llm import LLMClient
from src.loop.replay import ReplayFidelity, replay_call

_log = logging.getLogger(__name__)

# Type alias matching src.loop.replay.LLMFactory.
LLMFactory = Callable[[], LLMClient]

# ---------------------------------------------------------------------------
# Pydantic request model
# ---------------------------------------------------------------------------

_N_MIN, _N_MAX = 1, 5
_MAX_CALLS_MIN, _MAX_CALLS_MAX = 1, 25
_CONCURRENCY_CAP = 3  # max concurrent replay_call tasks inside a build job


class BuildRequest(BaseModel):
    """Body for POST /api/replay/build. Both fields are optional with defaults; values are clamped
    server-side so out-of-range requests are accepted and corrected, not rejected."""

    n: int = 1
    max_calls: int = 10


# ---------------------------------------------------------------------------
# In-memory replay store (per-router-instance; closure-local, NOT module-global)
# ---------------------------------------------------------------------------


@dataclass
class _CallResult:
    """Per-call fidelity result stored after replay_call returns."""

    episode_id: str
    real_outcome: str
    mean_divergence: float
    outcome_match_rate: float
    n: int
    persona_label: str


@dataclass
class _ReplayStore:
    """Ephemeral in-memory state for the current (or last) replay build.
    Updated live by the background task as each call finishes. One instance per router
    (i.e. per create_app() call) so tests that build multiple apps don't share state.
    """

    status: str = "idle"           # idle | running | done
    calls_total: int = 0
    calls_done: int = 0
    results: list[_CallResult] = field(default_factory=list)
    # Background asyncio task handle — used to detect a still-running job.
    _task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)

    def reset(self, calls_total: int) -> None:
        """Prepare for a new build: clear previous results and mark running."""
        self.status = "running"
        self.calls_total = calls_total
        self.calls_done = 0
        self.results = []
        self._task = None

    @property
    def is_running(self) -> bool:
        return self.status == "running" and self._task is not None and not self._task.done()


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_replay_router(
    read_store: ReadStore,
    agent_llm_factory: LLMFactory,
    twin_llm_factory: LLMFactory,
    config: AgentConfig,
    *,
    has_llm_key: bool = True,
) -> APIRouter:
    """Build the /api/replay router. `read_store`, `agent_llm_factory`, `twin_llm_factory`, and
    `config` are all injectable so the router runs fully DB-free and LLM-free in tests.

    `has_llm_key` controls the graceful-degradation guard: when False (no injected factory AND no
    OPENROUTER_API_KEY in env), POST /api/replay/build returns 503 at request time — the router
    still mounts and GET /api/replay/results still works; nothing calls any LLM at import or mount.

    The background task that calls replay_call is ONLY launched from POST /api/replay/build —
    never at import time or mount time. A Semaphore(3) caps concurrent per-call tasks inside the
    job. The in-memory store is updated as each call finishes so GET /api/replay/results shows
    live progress. Each router call owns its own _ReplayStore instance for test isolation.
    """
    # Closure-local store: isolated per create_replay_router() call (i.e. per create_app() call).
    # This means tests that build multiple app instances don't share replay state.
    _state = _ReplayStore()

    router = APIRouter()

    # ------------------------------------------------------------------
    # POST /api/replay/build
    # ------------------------------------------------------------------

    @router.post("/api/replay/build", status_code=202)
    async def build_replay(req: BuildRequest) -> dict[str, Any]:
        """Start a background job that runs replay_call over the most-recent completed calls.

        Clamps n to [1,5] and max_calls to [1,25]. Returns 202 when a job starts, 200 status=empty
        when there are no eligible calls, 409 when a job is already running, and 503 when no LLM
        key is configured and no factory was injected at mount time.
        """
        nonlocal _state

        # --- guard: no LLM key and no injected factory ---
        if not has_llm_key:
            raise HTTPException(
                status_code=503,
                detail=(
                    "replay build requires an LLM key: set OPENROUTER_API_KEY or inject "
                    "replay_agent_llm_factory / replay_twin_llm_factory into create_app"
                ),
            )

        # --- check for an already-running job ---
        if _state.is_running:
            raise HTTPException(status_code=409, detail="replay build already running")

        # --- clamp inputs ---
        n = max(_N_MIN, min(_N_MAX, req.n))
        max_calls = max(_MAX_CALLS_MIN, min(_MAX_CALLS_MAX, req.max_calls))

        # --- select eligible calls ---
        # Over-fetch to account for non-completed rows; filter server-side.
        all_eps = await read_store.list_episodes(limit=max_calls * 5)
        eligible = [
            ep for ep in all_eps
            if _is_completed(ep) and len(ep.turns) >= 4
        ]
        # newest-first; already sorted by list_episodes — just cap.
        eligible = eligible[:max_calls]

        if not eligible:
            return JSONResponse(status_code=200, content={"status": "empty", "calls": 0})

        # --- prepare in-memory store ---
        _state.reset(calls_total=len(eligible))

        # --- launch single background task ---
        task = asyncio.create_task(
            _run_build_job(
                episodes=eligible,
                agent_llm_factory=agent_llm_factory,
                twin_llm_factory=twin_llm_factory,
                config=config,
                n=n,
                store=_state,
            )
        )
        _state._task = task

        return {"status": "started", "calls": len(eligible)}

    # ------------------------------------------------------------------
    # GET /api/replay/results
    # ------------------------------------------------------------------

    @router.get("/api/replay/results")
    async def get_replay_results() -> dict[str, Any]:
        """Return current build status and per-call fidelity results.

        `status` is idle | running | done. `results` are sorted by mean_divergence ascending
        (best mimics first). `aggregate` is null when no calls have finished yet.
        """
        s = _state

        # Build sorted results snapshot
        sorted_results = sorted(s.results, key=lambda r: r.mean_divergence)
        result_dicts = [
            {
                "episode_id": r.episode_id,
                "real_outcome": r.real_outcome,
                "mean_divergence": r.mean_divergence,
                "outcome_match_rate": r.outcome_match_rate,
                "n": r.n,
                "persona_label": r.persona_label,
            }
            for r in sorted_results
        ]

        # Compute aggregate over completed results
        agg = _compute_aggregate(s.results)

        return {
            "status": s.status,
            "calls_total": s.calls_total,
            "calls_done": s.calls_done,
            "results": result_dicts,
            "aggregate": agg,
        }

    return router


# ---------------------------------------------------------------------------
# Background job implementation
# ---------------------------------------------------------------------------


async def _run_build_job(
    *,
    episodes: list,
    agent_llm_factory: LLMFactory,
    twin_llm_factory: LLMFactory,
    config: AgentConfig,
    n: int,
    store: _ReplayStore,
) -> None:
    """Run replay_call for each episode with bounded concurrency (Semaphore(3)).
    Updates `store` as each call finishes. Sets store.status = 'done' when all calls complete.
    Exceptions from individual calls are logged and skipped (partial results are still surfaced).
    """
    sem = asyncio.Semaphore(_CONCURRENCY_CAP)

    async def _run_one(ep) -> None:
        async with sem:
            try:
                fidelity: ReplayFidelity = await replay_call(
                    ep,
                    agent_llm_factory,
                    twin_llm_factory,
                    config,
                    n=n,
                    max_turns=40,
                    seed=0,
                )
                persona_slug = ep.persona or ""
                persona_label = _humanize_persona(persona_slug)
                result = _CallResult(
                    episode_id=fidelity.real_episode_id,
                    real_outcome=fidelity.real_outcome,
                    mean_divergence=fidelity.mean_divergence,
                    outcome_match_rate=fidelity.outcome_match_rate,
                    n=fidelity.n,
                    persona_label=persona_label,
                )
                store.results.append(result)
            except Exception as exc:
                _log.warning("replay_call failed for %s: %s", getattr(ep, "episode_id", "?"), exc)
            finally:
                store.calls_done += 1

    tasks = [asyncio.create_task(_run_one(ep)) for ep in episodes]
    await asyncio.gather(*tasks, return_exceptions=True)
    store.status = "done"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _humanize_persona(slug: str) -> str:
    """Translate a persona slug to a human-readable label.
    Already-readable labels (no underscores, capitalized) pass through via _titleize fallback.
    Raw slugs like 'anxious_parent' -> 'Anxious parent'; 'warm_champion' -> 'Warm champion'.
    """
    if not slug:
        return "Unknown"
    return labels._titleize(slug)


def _compute_aggregate(results: list[_CallResult]) -> dict[str, Any]:
    """Compute mean divergence + mean outcome match over all completed results.
    Returns null values when results list is empty.
    """
    if not results:
        return {"mean_divergence": None, "mean_outcome_match": None, "calls": 0}
    mean_div = sum(r.mean_divergence for r in results) / len(results)
    mean_match = sum(r.outcome_match_rate for r in results) / len(results)
    return {
        "mean_divergence": round(mean_div, 4),
        "mean_outcome_match": round(mean_match, 4),
        "calls": len(results),
    }

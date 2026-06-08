# THE PROMOTION GATE + round orchestrator for the U10 improvement loop (plan R19/R20/R36/R12) — the
# integrity-critical logic that decides whether a challenger replaces the champion. evaluate_promotion
# is a PURE function over an injected ExperimentResult (testable with no self-play): it pauses the loop
# when sim and real diverge beyond threshold (R36), then applies the bar — a statistically significant
# lift (challenger_better) AND zero guardrail regression AND qualification accuracy held — rejecting
# any failure, blocking EXTREME (pricing-concession/persona) passes for human approval (R19/AE6), and
# auto-promoting clean non-extreme passes (R19/AE5). promote() records the challenger as the new
# champion via the single-champion store (R12); run_improvement_round wires generator -> experiment ->
# MEASURE sim-to-real divergence (sim2real.sim_to_real_report on a small matched probe, injectable) ->
# gate -> optional promote, so the R36 pause is LIVE on the default path (not a dead None). Reuses
# src.loop.grading.guardrails_regressed + src.loop.sim2real; async for the store/runner/reporter
# seams. NO LiveKit / numpy / scipy / pandas; SEEDED random only.
# CB-58: all operator-facing reason strings are written in plain English — no Python flag names
# (challenger_better is False, is_extreme, etc.) ever appear in a reason the UI renders.
# CB-59: promote() MATERIALIZES the new champion's config to src/config/versions/<version>.yaml
# (settings.save_config) and stamps config_ref on the lineage node, so resolve_champion_config can
# re-load the promoted champion's REAL content later. Best-effort: a yaml-write failure still
# promotes (config_ref=None) — the API's honest fallback covers unmaterialized champions.
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

from src.config.settings import AgentConfig, save_config
from src.core.llm import LLMClient
from src.loop import sim2real
from src.loop.experiment import (
    ExperimentResult,
    HeldOutSet,
    experiment_record_from,
    run_experiment,
)
from src.loop.generator import Challenger, generate_challengers
from src.loop.grading import guardrails_regressed
from src.loop.sim2real import MatchedScenario, Sim2RealReport
from src.memory import store
from src.memory.schema import Episode, ExperimentRecord, VersionLineage

logger = logging.getLogger(__name__)


class VersionRecorder(Protocol):
    """The minimal write seam promote() needs to record the new champion lineage (CB-59). The real
    src.memory.store satisfies it; tests inject an in-memory fake so promotion runs DB-free."""

    async def record_version(self, lineage: VersionLineage) -> str: ...


class ExperimentSaver(Protocol):
    """The minimal write seam run_improvement_round needs to PERSIST a round's ExperimentRecord so it
    shows in the dashboard lab (P6). The real src.memory.store satisfies it; tests inject an in-memory
    fake (DB-free). Kept separate from the full Improve ImproveStore so the loop depends on nothing
    more than save_experiment."""

    async def save_experiment(self, experiment: ExperimentRecord) -> str: ...

# The sim-to-real divergence ceiling: above this the loop PAUSES (R36 — divergence is a GATE, not a
# report). run_improvement_round now MEASURES it via sim2real.sim_to_real_report when a caller doesn't
# supply one (the gate is live, not dead); None at evaluate_promotion still means "not measured" ->
# the pause check is skipped (kept for the pure-function gate tests).
DIVERGENCE_THRESHOLD = 0.2
# How many held-out personas the round turns into matched sim-vs-voice scenarios to MEASURE divergence
# (R36) when one wasn't supplied. Kept small: each scenario runs both arms (sim self-play + a voice
# session), so this is a cheap probe of the channel gap, not a full re-eval.
_DIVERGENCE_SCENARIO_COUNT = 3
# Qualification accuracy may dip by at most this much vs the champion before it counts as a regression
# (small tolerance for resampling noise; a real drop beyond it blocks promotion, R29).
QUAL_TOLERANCE = 0.02


@dataclass
class PromotionDecision:
    """The gate's verdict (plan R19/R20/R36). `status` ∈ {"promoted","rejected","pending_approval",
    "paused"}. `promote` is True only for an auto-promoted (clean, non-extreme) pass; `requires_human`
    is True for an extreme pass blocked for approval; `reasons` explains every failed/triggered
    criterion so the decision is auditable."""

    status: str
    promote: bool
    requires_human: bool
    reasons: list[str] = field(default_factory=list)


def evaluate_promotion(
    experiment: ExperimentResult,
    *,
    is_extreme: bool,
    divergence: Optional[float] = None,
    divergence_threshold: float = DIVERGENCE_THRESHOLD,
    qual_tolerance: float = QUAL_TOLERANCE,
) -> PromotionDecision:
    """Decide whether to promote a challenger (plan R19/R20/R36). PURE — no self-play, no I/O.

    Logic, IN THIS ORDER:
      1. Sim-to-real divergence pause (R36): if divergence is not None AND > threshold -> "paused"
         (nothing promotes while sim and real disagree). divergence=None means not-yet-measured ->
         skip this check.
      2. Compute the bar `passed` (the conjunction below). Record a reason for EACH failed criterion.
      3. If not passed -> "rejected".
      4. If passed AND is_extreme -> "pending_approval" (requires_human; R19/AE6 — block for a human).
      5. If passed AND not is_extreme -> "promoted" (auto-promote, HOTL post-hoc review; R19/AE5).

    The bar `passed` predicate (the over-claim-resistant core):
        passed = comparison.challenger_better
                 AND (not guardrails_regressed(champion_guardrails, challenger_guardrails))
                 AND (challenger_qual_acc >= champion_qual_acc - qual_tolerance)
    """
    # 1. Sim-to-real divergence pause (R36) — runs FIRST, before the bar.
    if divergence is not None and divergence > divergence_threshold:
        return PromotionDecision(
            status="paused",
            promote=False,
            requires_human=False,
            reasons=[
                f"sim-to-real divergence {divergence:.3f} > threshold {divergence_threshold:.3f}: "
                "loop paused (R36) — sim and real disagree, no promotion trusted"
            ],
        )

    # 2. The bar — evaluate each criterion and record WHY any failed.
    # CB-58: all reason strings use plain English; no Python flag names (challenger_better, is_extreme,
    # etc.) appear — these reasons may render on operator-facing card footers.
    reasons: list[str] = []
    lift_ok = experiment.comparison.challenger_better
    if not lift_ok:
        reasons.append(
            "No significant lift detected — the difference between the two configs fell within "
            "statistical noise (delta confidence interval includes zero)."
        )
    regressed = guardrails_regressed(
        experiment.champion_guardrails, experiment.challenger_guardrails
    )
    if regressed:
        reasons.append(
            "Guardrail regression: the challenger pushed harder or made more unsupported claims "
            "than the current champion (R24)."
        )
    qual_ok = experiment.challenger_qual_acc >= experiment.champion_qual_acc - qual_tolerance
    if not qual_ok:
        reasons.append(
            f"Qualification accuracy dropped from {experiment.champion_qual_acc:.0%} to "
            f"{experiment.challenger_qual_acc:.0%} — beyond the allowed tolerance (R29)."
        )

    passed = lift_ok and (not regressed) and qual_ok

    # 3. Not passed -> rejected (reasons already name which criterion failed).
    if not passed:
        return PromotionDecision(
            status="rejected", promote=False, requires_human=False, reasons=reasons
        )

    # 4. Passed AND extreme -> block for human approval (R19/AE6).
    if is_extreme:
        return PromotionDecision(
            status="pending_approval",
            promote=False,
            requires_human=True,
            reasons=[
                "Passed the bar but the change involves a pricing or persona boundary — held for "
                "human review before promoting (R19)."
            ],
        )

    # 5. Passed AND non-extreme -> auto-promote with post-hoc review (R19/AE5).
    return PromotionDecision(
        status="promoted",
        promote=True,
        requires_human=False,
        reasons=["Passed the bar (significant lift, guardrails clean, qualification held) — "
                 "auto-promoted with post-hoc review (R19)."],
    )


async def promote(
    challenger: Challenger,
    experiment: ExperimentResult,
    *,
    seed: int = 0,
    version_store: Optional[VersionRecorder] = None,
) -> VersionLineage:
    """Record the challenger as the NEW champion in the version lineage (plan R12).

    CB-59 (root fix): BEFORE recording the lineage, MATERIALIZE the new champion's config content to
    src/config/versions/<challenger_version>.yaml via save_config — the challenger.config already
    carries challenger_version as its `version` field (re-stamped by _build_challenger), so no
    relabeling happens here. config_ref on the lineage node points to that yaml, making the champion
    re-loadable by resolve_champion_config (experiments then baseline its REAL content, not the
    bootstrap). The yaml write is best-effort: a write failure logs a warning and records the
    lineage with config_ref=None (promotion itself must never fail on a disk hiccup — the honest
    fallback in resolve_champion_config covers an unmaterialized champion).

    Builds a VersionLineage(version=challenger.challenger_version, parent_version=its parent,
    kb_version from the challenger config, kpi snapshot from the comparison, is_champion=True) and
    persists it via record_version — which demotes any prior champion first (single-champion
    invariant). `version_store` is injectable (CB-59) so tests promote against an in-memory fake;
    None -> the real src.memory.store. Returns the recorded lineage node. `seed` is accepted for
    API symmetry / future reproducible config-ref derivation.
    """
    _ = seed
    # CB-59: materialize the new champion's config so it is re-loadable later. Best-effort.
    config_ref: Optional[str] = None
    try:
        save_config(challenger.config)
        config_ref = challenger.challenger_version
    except Exception:
        logger.warning(
            "promote: could not materialize config yaml for new champion %r — lineage will carry "
            "config_ref=None (resolve_champion_config will fall back to the bootstrap)",
            challenger.challenger_version,
            exc_info=True,
        )
    cmp = experiment.comparison
    lineage = VersionLineage(
        version=challenger.challenger_version,
        parent_version=challenger.parent_version,
        config_ref=config_ref,
        kb_version=challenger.config.kb_version,
        kpi={
            "dimension": experiment.dimension,
            "champion_kpi": cmp.champion_kpi,
            "challenger_kpi": cmp.challenger_kpi,
            "delta": cmp.delta,
            "delta_ci": list(cmp.delta_ci),
            "champion_qual_acc": experiment.champion_qual_acc,
            "challenger_qual_acc": experiment.challenger_qual_acc,
        },
        is_champion=True,
    )
    recorder: VersionRecorder = version_store if version_store is not None else store
    await recorder.record_version(lineage)
    return lineage


# A sim_to_real_report-shaped seam (injectable so tests measure divergence without self-play/voice).
DivergenceReporter = Callable[..., Awaitable[Sim2RealReport]]


async def _measure_divergence(
    champion_config: AgentConfig,
    held_out: HeldOutSet,
    agent_llm_factory: Callable[[], LLMClient],
    prospect_llm_factory: Callable[[], LLMClient],
    *,
    seed: int,
    reporter: DivergenceReporter,
) -> Optional[float]:
    """MEASURE sim-to-real divergence on a small matched-scenario probe (plan R36) so the promotion
    gate's central-risk pause is LIVE on the default round path (it used to pass None -> dead gate).

    Builds up to _DIVERGENCE_SCENARIO_COUNT MatchedScenarios from the FROZEN held-out personas (same
    population the experiment graded, so the probe matches the eval), then runs the injected reporter
    (default sim2real.sim_to_real_report — sim self-play vs a voice session on matched inputs) and
    returns the MEAN divergence. Returns None (skip the gate, don't crash the round) if there are no
    personas to probe or the reporter raises — fail-OPEN here is acceptable because the experiment's
    own bar still gates promotion; the divergence pause is an ADDITIONAL safety the round measures when
    it can. Cheap by construction (a handful of matched scenarios)."""
    personas = list(held_out.personas)[:_DIVERGENCE_SCENARIO_COUNT]
    if not personas:
        return None
    scenarios = [
        MatchedScenario(
            persona=p,
            prospect_script_or_seed=seed + i,
            label=f"r{held_out.rotation_index}_{p.persona_id}",
        )
        for i, p in enumerate(personas)
    ]
    try:
        report = await reporter(
            scenarios,
            champion_config,
            agent_llm_factory,
            prospect_llm_factory,
            seed=seed,
        )
    except Exception:
        # A measurement failure must not crash the round; the experiment bar still gates promotion.
        return None
    return report.mean_divergence


@dataclass
class RoundResult:
    """One improvement-round outcome (plan U10): the generated challenger, its graded experiment, the
    gate decision, and the recorded lineage node (None unless it promoted AND persistence was on)."""

    challenger: Optional[Challenger]
    experiment: Optional[ExperimentResult]
    decision: PromotionDecision
    lineage: Optional[VersionLineage] = None


async def run_improvement_round(
    champion_config: AgentConfig,
    *,
    training_episodes: Sequence[Episode],
    held_out: HeldOutSet,
    agent_llm_factory: Callable[[], LLMClient],
    prospect_llm_factory: Callable[[], LLMClient],
    llm: Optional[LLMClient] = None,
    seed: int,
    kb_chunks: Optional[Sequence[Any]] = None,
    divergence: Optional[float] = None,
    divergence_reporter: Optional[DivergenceReporter] = None,
    persist_promotion: bool = False,
    persist_experiment: bool = False,
    experiment_store: Optional[ExperimentSaver] = None,
    population: str = "",
    runner: Optional[Callable[..., Awaitable[list[Episode]]]] = None,
) -> RoundResult:
    """Orchestrate ONE improvement round (plan U10): generate a challenger -> run the experiment on
    the frozen held-out set -> MEASURE sim-to-real divergence (R36) -> evaluate the promotion gate ->
    (if promoted AND persist_promotion) record it as the new champion, and (if persist_experiment)
    write the round's ExperimentRecord so it SHOWS in the dashboard lab (P6).

    `training_episodes` are the mined champion runs the generator clusters (failure-conditioned +
    tactic-mined). `runner` is forwarded to run_experiment (injectable for deterministic tests;
    defaults to self-play).

    SIM-TO-REAL DIVERGENCE (R36) is now WIRED, not dead: if a caller passes an explicit `divergence`
    it is used as-is; otherwise the round MEASURES one via `divergence_reporter` (default
    sim2real.sim_to_real_report) on a small matched-scenario probe of the held-out personas and feeds
    the mean to the gate, so a sim-vs-real disagreement actually PAUSES the loop. A measurement that
    can't run (no personas / reporter error) degrades to None (skip the pause); the experiment bar
    still gates promotion.

    PERSISTENCE is opt-in + injectable so DB-free gate tests stay pure: `persist_promotion` writes the
    champion lineage on an auto-promote (existing behavior); `persist_experiment` flattens the graded
    ExperimentResult + the gate decision into an ExperimentRecord (state from the decision) and saves
    it via `experiment_store` (defaults to src.memory.store when None). Returns a RoundResult; when the
    generator finds NO candidate, decision is a 'rejected' with a reason and challenger/experiment are
    None (and nothing is persisted).
    """
    challengers = generate_challengers(champion_config, training_episodes, llm=llm, seed=seed, n=1)
    if not challengers:
        return RoundResult(
            challenger=None,
            experiment=None,
            decision=PromotionDecision(
                status="rejected",
                promote=False,
                requires_human=False,
                reasons=["no minimal-diff challenger could be generated this round"],
            ),
            lineage=None,
        )
    challenger = challengers[0]

    experiment = await run_experiment(
        champion_config,
        challenger,
        held_out,
        agent_llm_factory,
        prospect_llm_factory,
        seed=seed,
        kb_chunks=kb_chunks,
        runner=runner,
    )

    # MEASURE divergence (R36) when the caller didn't supply one, so the central-risk pause is LIVE on
    # the default path. The reporter defaults to the real sim_to_real_report; tests inject a cheap one.
    if divergence is None:
        reporter = divergence_reporter if divergence_reporter is not None else sim2real.sim_to_real_report
        divergence = await _measure_divergence(
            champion_config,
            held_out,
            agent_llm_factory,
            prospect_llm_factory,
            seed=seed,
            reporter=reporter,
        )

    decision = evaluate_promotion(experiment, is_extreme=challenger.is_extreme, divergence=divergence)

    # Persist the round as a durable ExperimentRecord so it shows in the lab (P6) — in ADDITION to the
    # lineage write below (which only fires on an auto-promote). Injectable store keeps the gate tests
    # DB-free; defaults to the real Postgres store in prod.
    if persist_experiment:
        saver: ExperimentSaver = experiment_store if experiment_store is not None else store
        record = experiment_record_from(
            experiment, decision, challenger=challenger, population=population
        )
        await saver.save_experiment(record)

    lineage: Optional[VersionLineage] = None
    if decision.promote and persist_promotion:
        lineage = await promote(challenger, experiment, seed=seed)

    return RoundResult(
        challenger=challenger, experiment=experiment, decision=decision, lineage=lineage
    )

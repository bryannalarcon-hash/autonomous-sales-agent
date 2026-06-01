# THE PROMOTION GATE + round orchestrator for the U10 improvement loop (plan R19/R20/R36/R12) — the
# integrity-critical logic that decides whether a challenger replaces the champion. evaluate_promotion
# is a PURE function over an injected ExperimentResult (testable with no self-play): it pauses the loop
# when sim and real diverge beyond threshold (R36), then applies the bar — a statistically significant
# lift (challenger_better) AND zero guardrail regression AND qualification accuracy held — rejecting
# any failure, blocking EXTREME (pricing-concession/persona) passes for human approval (R19/AE6), and
# auto-promoting clean non-extreme passes (R19/AE5). promote() records the challenger as the new
# champion via the single-champion store (R12); run_improvement_round wires generator -> experiment ->
# gate -> optional promote. Reuses src.loop.grading.guardrails_regressed; async for the store/runner
# seams. NO LiveKit / numpy / scipy / pandas; SEEDED random only.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

from src.config.settings import AgentConfig
from src.core.llm import LLMClient
from src.loop.experiment import ExperimentResult, HeldOutSet, run_experiment
from src.loop.generator import Challenger, generate_challengers
from src.loop.grading import guardrails_regressed
from src.memory import store
from src.memory.schema import Episode, VersionLineage

# The sim-to-real divergence ceiling: above this the loop PAUSES (R36 — divergence is a GATE, not a
# report). Fed by U14 later; None at the gate means "not yet measured" -> the pause check is skipped.
DIVERGENCE_THRESHOLD = 0.2
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
    reasons: list[str] = []
    lift_ok = experiment.comparison.challenger_better
    if not lift_ok:
        reasons.append(
            "no significant lift: challenger_better is False (delta CI includes 0 — not "
            "statistically separated from noise)"
        )
    regressed = guardrails_regressed(
        experiment.champion_guardrails, experiment.challenger_guardrails
    )
    if regressed:
        reasons.append(
            "guardrail regression: challenger pushes harder and/or makes more false promises than "
            "the champion (R24)"
        )
    qual_ok = experiment.challenger_qual_acc >= experiment.champion_qual_acc - qual_tolerance
    if not qual_ok:
        reasons.append(
            f"qualification regression: challenger qual_acc {experiment.challenger_qual_acc:.3f} < "
            f"champion {experiment.champion_qual_acc:.3f} - tolerance {qual_tolerance:.3f} (R29)"
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
                "passed the bar but the diff is EXTREME (pricing concession or persona) — blocked "
                "pending human approval (R19)"
            ],
        )

    # 5. Passed AND non-extreme -> auto-promote with post-hoc review (R19/AE5).
    return PromotionDecision(
        status="promoted",
        promote=True,
        requires_human=False,
        reasons=["passed the bar (significant lift, guardrails clean, qualification held) and is "
                 "non-extreme — auto-promoted with HOTL post-hoc review (R19)"],
    )


async def promote(
    challenger: Challenger,
    experiment: ExperimentResult,
    *,
    seed: int = 0,
) -> VersionLineage:
    """Record the challenger as the NEW champion in the version lineage (plan R12).

    Builds a VersionLineage(version=challenger.challenger_version, parent_version=its parent,
    kb_version from the challenger config, kpi snapshot from the comparison, is_champion=True) and
    persists it via store.record_version — which demotes any prior champion first (single-champion
    invariant). Returns the recorded lineage node. `seed` is accepted for API symmetry / future
    reproducible config-ref derivation.
    """
    _ = seed
    cmp = experiment.comparison
    lineage = VersionLineage(
        version=challenger.challenger_version,
        parent_version=challenger.parent_version,
        config_ref=None,
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
    await store.record_version(lineage)
    return lineage


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
    persist_promotion: bool = False,
    runner: Optional[Callable[..., Awaitable[list[Episode]]]] = None,
) -> RoundResult:
    """Orchestrate ONE improvement round (plan U10): generate a challenger -> run the experiment on
    the frozen held-out set -> evaluate the promotion gate -> (if promoted AND persist_promotion)
    record it as the new champion.

    `training_episodes` are the mined champion runs the generator clusters (failure-conditioned +
    tactic-mined). `runner` is forwarded to run_experiment (injectable for deterministic tests;
    defaults to self-play). `divergence` (None = not yet measured) feeds the R36 pause check.
    Returns a RoundResult; when the generator finds NO candidate, decision is a 'rejected' with a
    reason and challenger/experiment are None.
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

    decision = evaluate_promotion(experiment, is_extreme=challenger.is_extreme, divergence=divergence)

    lineage: Optional[VersionLineage] = None
    if decision.promote and persist_promotion:
        lineage = await promote(challenger, experiment, seed=seed)

    return RoundResult(
        challenger=challenger, experiment=experiment, decision=decision, lineage=lineage
    )

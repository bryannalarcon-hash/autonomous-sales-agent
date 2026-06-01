# Improvement-loop package (plan U9/U10) — the EVAL + self-improvement layer of the sales agent.
# U9 lives in grading.py: deterministic groundedness (reuses src.kb.retriever.grounded), guardrail
# metrics (reuse src.core.gates pressure vocabulary), the Arena-style pairwise LLM-judge with
# position-bias mitigation, pure-stdlib bootstrap CIs, the weighted-ladder KPI + champion/challenger
# ranking that GATES promotion. U10 adds generator.py (minimal-diff challenger generation),
# experiment.py (champion-vs-challenger on a frozen held-out set + qualification accuracy), and
# promotion.py (THE promotion gate + round orchestrator). U11 adds validation.py (empirical driver
# validation: keep/drop the six latent drivers, config-only graceful degradation, fallback experiment).
# Re-exports the public U9+U10+U11 API so callers do
# `from src.loop import compare_versions, evaluate_promotion, generate_challengers, analyze_drivers, ...`.
# Pure stdlib + the src.core.llm seam for the judge; NO LiveKit / numpy / scipy / pandas imports.
from __future__ import annotations

from src.loop.experiment import (
    ExperimentResult,
    HeldOutSet,
    frozen_held_out,
    qualification_accuracy,
    rotate,
    run_experiment,
)
from src.loop.generator import (
    Challenger,
    EXTREME_DIMENSIONS,
    build_challenger,
    declared_diff,
    generate_challengers,
    is_minimal_diff,
    mutate_threshold,
    reorder_discovery,
)
from src.loop.grading import (
    ComparisonResult,
    GroundednessSummary,
    GroundednessViolation,
    GuardrailReport,
    HeadlineGate,
    PairwiseVerdict,
    JUDGE_MODEL,
    RUBRICS,
    bootstrap_ci,
    can_report_headline,
    compare_versions,
    episode_groundedness,
    guardrail_report,
    guardrails_regressed,
    judge_pairwise,
    kpi_score,
)
from src.loop.promotion import (
    DIVERGENCE_THRESHOLD,
    PromotionDecision,
    QUAL_TOLERANCE,
    RoundResult,
    evaluate_promotion,
    promote,
    run_improvement_round,
)
from src.loop.validation import (
    DriverStats,
    DriverValidationReport,
    analyze_drivers,
    annotation_agreement_ok,
    apply_validation_to_config,
    dropped_drivers,
    fallback_experiment,
    inter_annotator_agreement,
    validated_drivers,
)

__all__ = [
    # grading (U9)
    "ComparisonResult",
    "GroundednessSummary",
    "GroundednessViolation",
    "GuardrailReport",
    "HeadlineGate",
    "PairwiseVerdict",
    "JUDGE_MODEL",
    "RUBRICS",
    "bootstrap_ci",
    "can_report_headline",
    "compare_versions",
    "episode_groundedness",
    "guardrail_report",
    "guardrails_regressed",
    "judge_pairwise",
    "kpi_score",
    # generator (U10)
    "Challenger",
    "EXTREME_DIMENSIONS",
    "build_challenger",
    "declared_diff",
    "generate_challengers",
    "is_minimal_diff",
    "mutate_threshold",
    "reorder_discovery",
    # experiment (U10)
    "ExperimentResult",
    "HeldOutSet",
    "frozen_held_out",
    "qualification_accuracy",
    "rotate",
    "run_experiment",
    # promotion (U10)
    "DIVERGENCE_THRESHOLD",
    "PromotionDecision",
    "QUAL_TOLERANCE",
    "RoundResult",
    "evaluate_promotion",
    "promote",
    "run_improvement_round",
    # validation (U11)
    "DriverStats",
    "DriverValidationReport",
    "analyze_drivers",
    "annotation_agreement_ok",
    "apply_validation_to_config",
    "dropped_drivers",
    "fallback_experiment",
    "inter_annotator_agreement",
    "validated_drivers",
]

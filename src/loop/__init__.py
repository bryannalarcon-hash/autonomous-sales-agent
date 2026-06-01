# Improvement-loop package (plan U9/U10) — the EVAL + self-improvement layer of the sales agent.
# U9 lives in grading.py: deterministic groundedness (reuses src.kb.retriever.grounded), guardrail
# metrics (reuse src.core.gates pressure vocabulary), the Arena-style pairwise LLM-judge with
# position-bias mitigation, pure-stdlib bootstrap CIs, the weighted-ladder KPI + champion/challenger
# ranking that GATES promotion (U10), and the R39 human-calibration headline gate. Re-exports the
# public grading API so callers do `from src.loop import compare_versions, can_report_headline, ...`.
# Pure stdlib + the src.core.llm seam for the judge; NO LiveKit / numpy / scipy / pandas imports.
from __future__ import annotations

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

__all__ = [
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
]

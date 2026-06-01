# Champion-vs-challenger experiments for the U10 improvement loop (plan R20/R22/R29/R36). Runs BOTH
# configs on the SAME frozen held-out adversarial persona set (same base_seed so scenarios match),
# grades each arm via the U9 layer (compare_versions + a worst-case GuardrailReport per arm), and
# computes qualification accuracy per arm. The held-out set is FROZEN + deterministically rotatable:
# frozen_held_out(seed,n,rotation_index) reproduces the exact set; rotate() advances the rotation
# while EXCLUDING any R22-mined persona ids (mined failures feed the TRAINING population only, never
# the held-out eval set). Reuses src.sim.selfplay.run_batch (injectable as `runner` for deterministic
# tests), src.sim.personas.sample_population, and src.loop.grading. Also exposes experiment_record_from
# — the ExperimentResult + PromotionDecision -> durable ExperimentRecord mapping the loop AND the
# Improve API persist so a run SHOWS in the dashboard lab (P6). Async (matches run_batch/store);
# NO LiveKit / numpy / scipy / pandas; SEEDED random only.
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, Protocol, Sequence

from src.config.settings import AgentConfig
from src.core.llm import LLMClient
from src.loop.generator import Challenger
from src.loop.grading import (
    ComparisonResult,
    GuardrailReport,
    compare_versions,
    guardrail_report,
    guardrails_regressed,
)
from src.memory.schema import Episode, ExperimentRecord
from src.sim import selfplay
from src.sim.personas import Persona, sample_population

if TYPE_CHECKING:  # annotation-only import — promotion.py imports this module (avoid a cycle).
    from src.loop.promotion import PromotionDecision

# The standard confidence level of the comparison's delta CI (compare_versions uses alpha=0.05).
# significance is surfaced as a % chip = 1 - alpha when the CI cleanly excludes 0 (a real lift).
_COMPARISON_CONFIDENCE = 0.95

# PromotionDecision.status -> the durable ExperimentRecord lifecycle state. `pending_approval` (an
# extreme pass blocked for a human, R19) maps to the dashboard's `blocked` chip; the rest are 1:1.
_STATUS_TO_STATE: dict[str, str] = {
    "promoted": "promoted",
    "rejected": "rejected",
    "pending_approval": "blocked",
    "paused": "paused",
}

# A persona is "qualified" by the DERIVED agent verdict (v0 proxy) when the agent pursued a
# substantive commitment — ladder_tier at/above consultation (2). See qualification_accuracy.
_SUBSTANTIVE_LADDER_TIER = 2

# How rotate() derives a fresh seed when it must RE-SAMPLE to exclude mined personas: it walks this
# many seed offsets at the next rotation_index until none of the excluded ids appear. Bounded so a
# pathological exclusion set can't loop forever.
_MAX_RESAMPLE_ATTEMPTS = 256


@dataclass
class HeldOutSet:
    """A frozen held-out adversarial persona set (plan R20). `seed` + `rotation_index` fully
    determine `personas` (frozen_held_out reproduces it). Rotation advances rotation_index."""

    personas: list[Persona]
    rotation_index: int
    seed: int


def _rotation_seed(seed: int, rotation_index: int) -> int:
    """Derive the sampling seed for a given (base seed, rotation_index). A simple, deterministic
    mix so each rotation draws a different population while staying reproducible."""
    return seed * 1_000_003 + rotation_index * 97


def _content_id(persona: Persona, rotation_index: int) -> str:
    """A CONTENT-derived, rotation-namespaced persona id for the held-out set.

    sample_population mints ids POSITIONALLY (qual_0000, unqual_0000, ...), so two different seeds
    reuse the same id slots — that makes R22 exclusion meaningless (an excluded id reappears in every
    draw regardless of who actually fills that slot). We therefore re-key each held-out persona by a
    stable hash of its content (archetype + qualified + disqualifier + sorted utility/resistance),
    prefixed "ho{rotation_index}_". Same content -> same id (deterministic, reproducible); a genuinely
    different draw yields different ids, so excluding a mined persona truly removes IT and re-sampling
    can find a disjoint set.
    """
    payload = "|".join([
        persona.archetype,
        persona.style,
        str(persona.qualified),
        str(persona.disqualifier),
        persona.max_commitment,
        ",".join(f"{k}={persona.utility[k]}" for k in sorted(persona.utility)),
        ",".join(f"{k}={persona.resistance[k]}" for k in sorted(persona.resistance)),
        f"diff={persona.difficulty}",
    ])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"ho{rotation_index}_{digest}"


def _rekey(personas: Sequence[Persona], rotation_index: int) -> list[Persona]:
    """Return copies of `personas` with content-derived, rotation-namespaced ids (de-collided so two
    identical-content personas in one draw still get unique ids by appending an occurrence index)."""
    out: list[Persona] = []
    seen: dict[str, int] = {}
    for p in personas:
        base_id = _content_id(p, rotation_index)
        n = seen.get(base_id, 0)
        seen[base_id] = n + 1
        new_id = base_id if n == 0 else f"{base_id}_{n}"
        out.append(replace(p, persona_id=new_id))
    return out


def frozen_held_out(*, seed: int, n: int, rotation_index: int = 0) -> HeldOutSet:
    """Build a DETERMINISTIC held-out adversarial persona set (plan R20).

    Draws `n` personas via sample_population with a seed derived from (seed, rotation_index), then
    re-keys them with content-derived, rotation-namespaced ids (see _content_id) so R22 exclusion is
    meaningful. FROZEN: the same (seed, n, rotation_index) reproduces the EXACT set (same ids, same
    order). Adversarial by construction — sample_population mixes qualified + structurally-unqualified
    personas so the eval set probes both close-the-qualified and release-the-unqualified behavior.
    """
    drawn = sample_population(n, seed=_rotation_seed(seed, rotation_index))
    return HeldOutSet(personas=_rekey(drawn, rotation_index), rotation_index=rotation_index, seed=seed)


def rotate(held_out: HeldOutSet, *, exclude_persona_ids: Sequence[str] = ()) -> HeldOutSet:
    """Periodic light rotation to a new held-out sample (plan R20), EXCLUDING any mined-failure
    persona ids (plan R22).

    R22: real-call failure modes are mined back into the TRAINING population only — they must never
    leak into the held-out eval set (that would make the bar measure on data the challenger was tuned
    against). So this advances rotation_index and RE-SAMPLES (walking derived seeds, re-keying each
    draw at the next rotation_index) until the new set contains NONE of `exclude_persona_ids`,
    preserving the population size. Deterministic for a given (seed, exclusion set).
    """
    excluded = set(exclude_persona_ids)
    n = len(held_out.personas)
    next_index = held_out.rotation_index + 1
    candidate: list[Persona] = []
    for attempt in range(_MAX_RESAMPLE_ATTEMPTS):
        # Re-sampling walks a derived seed at the next rotation_index so each attempt is a fresh,
        # reproducible draw; the first attempt is the canonical next rotation.
        draw_seed = _rotation_seed(held_out.seed, next_index) + attempt * 7919
        candidate = _rekey(sample_population(n, seed=draw_seed), next_index)
        if not any(p.persona_id in excluded for p in candidate):
            return HeldOutSet(personas=candidate, rotation_index=next_index, seed=held_out.seed)
    # Could not find a clean draw within the attempt budget: return the last draw filtered of any
    # excluded ids (degrade gracefully — never return a set containing a mined id).
    cleaned = [p for p in candidate if p.persona_id not in excluded]
    return HeldOutSet(personas=cleaned, rotation_index=next_index, seed=held_out.seed)


def qualification_accuracy(episodes: Sequence[Episode]) -> float:
    """Agreement between a DERIVED agent qualification verdict and ground truth (plan R29).

    Episode.qualified is GROUND TRUTH (the persona's hidden label). The v0 PROXY derives the agent's
    verdict from its behavior: agent_verdict = "qualified" if the agent pursued a substantive
    commitment (ladder_tier >= 2, i.e. consultation or stronger), else "unqualified". Accuracy is the
    fraction of episodes where derived == ground truth.

    LIMITATION (v0 proxy): the agent has no explicit "disqualify" act yet, so we INFER its verdict
    from the ladder tier it reached. This conflates "judged unqualified" with "tried but failed to
    close" — an agent that mis-handles a qualified prospect and ends low-tier is scored as if it
    judged them unqualified. A later version should read an explicit agent disqualify decision
    instead of this behavioral proxy. An empty set returns 1.0 (nothing to disagree on).
    """
    eps = list(episodes)
    if not eps:
        return 1.0
    correct = 0
    for ep in eps:
        agent_verdict_qualified = int(ep.ladder_tier) >= _SUBSTANTIVE_LADDER_TIER
        if agent_verdict_qualified == bool(ep.qualified):
            correct += 1
    return correct / len(eps)


@dataclass
class ExperimentResult:
    """The graded outcome of one champion-vs-challenger experiment (plan R20). Carries the U9
    comparison (the significant-lift decision), a worst-case GuardrailReport per arm, the per-arm
    qualification accuracy (R29), and the raw episode sets for downstream lineage/KPI snapshots."""

    dimension: str
    comparison: ComparisonResult
    champion_guardrails: GuardrailReport
    challenger_guardrails: GuardrailReport
    champion_qual_acc: float
    challenger_qual_acc: float
    champion_eps: list[Episode]
    challenger_eps: list[Episode]


class _Runner(Protocol):
    """The run_batch-shaped seam run_experiment drives (injectable so tests stack the deck without
    self-play). Matches selfplay.run_batch's signature for the args run_experiment passes."""

    def __call__(
        self,
        personas: Sequence[Persona],
        agent_llm_factory: Callable[[], LLMClient],
        prospect_llm_factory: Callable[[], LLMClient],
        config: AgentConfig,
        **kwargs: Any,
    ) -> Awaitable[list[Episode]]: ...


def _worst_case_guardrails(
    episodes: Sequence[Episode], *, kb_chunks: Optional[Sequence[Any]]
) -> GuardrailReport:
    """Aggregate per-episode GuardrailReports into ONE worst-case report for the arm (plan R24).

    Worst-case is the conservative choice for a gate: the arm's pushiness_rate is the MAX over its
    episodes, false_promise_flags the SUM, pushiness_over_cap True if ANY episode tripped it,
    escalation_appropriate True only if ALL episodes were appropriate. An empty arm is a clean
    (zeroed) report.
    """
    reports = [guardrail_report(ep, kb_chunks=kb_chunks) for ep in episodes]
    if not reports:
        return GuardrailReport(
            pushiness_rate=0.0,
            pushiness_violation_turns=[],
            false_promise_flags=0,
            escalation_appropriate=True,
            pushiness_over_cap=False,
            agent_turns=0,
        )
    violation_turns: list[int] = []
    for r in reports:
        violation_turns.extend(r.pushiness_violation_turns)
    return GuardrailReport(
        pushiness_rate=max(r.pushiness_rate for r in reports),
        pushiness_violation_turns=violation_turns,
        false_promise_flags=sum(r.false_promise_flags for r in reports),
        escalation_appropriate=all(r.escalation_appropriate for r in reports),
        pushiness_over_cap=any(r.pushiness_over_cap for r in reports),
        agent_turns=sum(r.agent_turns for r in reports),
    )


async def run_experiment(
    champion_config: AgentConfig,
    challenger: Challenger,
    held_out: HeldOutSet,
    agent_llm_factory: Callable[[], LLMClient],
    prospect_llm_factory: Callable[[], LLMClient],
    *,
    seed: int,
    kb_chunks: Optional[Sequence[Any]] = None,
    max_turns: int = 24,
    persist: bool = False,
    runner: Optional[_Runner] = None,
) -> ExperimentResult:
    """Run champion vs challenger on the SAME frozen held-out personas and grade both arms (R20).

    Both arms run on `held_out.personas` with the SAME base_seed (`seed`) so the scenario per persona
    matches across arms (a paired comparison — the only difference is the config). Grades via the U9
    layer: compare_versions on the two episode sets (significant-lift decision + bootstrap CIs), a
    worst-case GuardrailReport per arm, and qualification_accuracy per arm (R29).

    `runner` defaults to selfplay.run_batch; tests inject a deterministic runner to stack the deck
    without self-play (network-free). `persist` (off by default for experiments) forwards to the
    runner if it accepts it; the default run_batch persists each episode when True.
    """
    run = runner if runner is not None else selfplay.run_batch
    cohort = f"held_out_r{held_out.rotation_index}"

    champion_eps = await run(
        held_out.personas,
        agent_llm_factory,
        prospect_llm_factory,
        champion_config,
        base_seed=seed,
        cohort=cohort,
        max_turns=max_turns,
        persist=persist,
    )
    challenger_eps = await run(
        held_out.personas,
        agent_llm_factory,
        prospect_llm_factory,
        challenger.config,
        base_seed=seed,
        cohort=cohort,
        max_turns=max_turns,
        persist=persist,
    )

    comparison = compare_versions(champion_eps, challenger_eps, seed=seed)
    champion_guardrails = _worst_case_guardrails(champion_eps, kb_chunks=kb_chunks)
    challenger_guardrails = _worst_case_guardrails(challenger_eps, kb_chunks=kb_chunks)

    return ExperimentResult(
        dimension=challenger.dimension,
        comparison=comparison,
        champion_guardrails=champion_guardrails,
        challenger_guardrails=challenger_guardrails,
        champion_qual_acc=qualification_accuracy(champion_eps),
        challenger_qual_acc=qualification_accuracy(challenger_eps),
        champion_eps=list(champion_eps),
        challenger_eps=list(challenger_eps),
    )


# === ExperimentResult + PromotionDecision -> durable ExperimentRecord (dashboard P6/P7) ========


def _enroll_rate(episodes: Sequence[Episode]) -> float:
    """Same-call enrollment rate for an arm: the fraction of episodes whose outcome is 'enrolled'.
    Distinct from the weighted-ladder KPI (which counts partial commitments). Empty -> 0.0."""
    eps = list(episodes)
    if not eps:
        return 0.0
    return sum(1 for ep in eps if ep.outcome == "enrolled") / len(eps)


def _significance(comparison: ComparisonResult) -> float:
    """A 0..1 significance the lab renders as a % chip (schema: 1 - alpha at which the delta CI
    excludes 0). compare_versions decides challenger_better at the 95% level; when it holds, the lift
    is separated from noise so significance is _COMPARISON_CONFIDENCE, else 0.0 (CI includes 0)."""
    return _COMPARISON_CONFIDENCE if comparison.challenger_better else 0.0


def experiment_record_from(
    experiment: ExperimentResult,
    decision: "PromotionDecision",
    *,
    challenger: Challenger,
    n: Optional[int] = None,
    population: str = "",
    target: int = 0,
    name: str = "",
    experiment_id: Optional[str] = None,
) -> ExperimentRecord:
    """Flatten a graded ExperimentResult + the gate's PromotionDecision into the DURABLE
    ExperimentRecord the dashboard lab (P6) / approval queue (P7) read (plan U16). This is the wiring
    that makes a loop round SHOW up: the loop AND the Improve `run` endpoint both build the record here.

    State comes from the decision (promoted/rejected/blocked(=pending_approval)/paused). The
    before/after comparison (champion/challenger KPI, delta, delta_ci, challenger_better) + the
    same-call enroll delta + significance are pulled from the comparison; the guardrail verdict is
    `trip` when the challenger regressed a guardrail vs the champion (R24) else `pass`, with the
    decision's first reason carried as the human-readable guardrail_reason on a non-promote; the
    per-arm qualification accuracies (R29) + the single declared diff + is_extreme (R19) come from the
    experiment/challenger. `n` defaults to the challenger arm size when omitted.
    """
    cmp = experiment.comparison
    regressed = guardrails_regressed(
        experiment.champion_guardrails, experiment.challenger_guardrails
    )
    guardrail = "trip" if regressed else "pass"
    # On a non-promote, surface WHY (the gate's first reason) so the lab card explains the block.
    guardrail_reason: Optional[str] = None
    if decision.status != "promoted" and decision.reasons:
        guardrail_reason = decision.reasons[0]
    state = _STATUS_TO_STATE.get(decision.status, "running")
    eid = experiment_id or f"RUN-{challenger.challenger_version}"
    return ExperimentRecord(
        experiment_id=eid,
        challenger_version=challenger.challenger_version,
        parent_version=challenger.parent_version,
        name=name or challenger.diff_description or f"changed {challenger.dimension}",
        dimension=challenger.dimension,
        declared_diff=[challenger.dimension],
        diff_description=challenger.diff_description,
        population=population,
        n=n if n is not None else cmp.n_challenger,
        target=target,
        kb_version=challenger.config.kb_version,
        champion_kpi=cmp.champion_kpi,
        challenger_kpi=cmp.challenger_kpi,
        delta=cmp.delta,
        delta_ci=[cmp.delta_ci[0], cmp.delta_ci[1]],
        challenger_better=cmp.challenger_better,
        enroll_delta=_enroll_rate(experiment.challenger_eps) - _enroll_rate(experiment.champion_eps),
        significance=_significance(cmp),
        guardrail=guardrail,
        guardrail_reason=guardrail_reason,
        champion_qual_acc=experiment.champion_qual_acc,
        challenger_qual_acc=experiment.challenger_qual_acc,
        is_extreme=challenger.is_extreme,
        state=state,
    )

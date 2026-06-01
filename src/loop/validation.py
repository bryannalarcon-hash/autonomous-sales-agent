# U11 driver-validation gate (plan R38/R6): VALIDATE the six latent drivers empirically over logged
# belief trajectories BEFORE the policy/experiment depend on them, DEGRADE gracefully if a driver
# drops, and NAME the fallback experiment if `trust` fails. analyze_drivers computes per-driver
# variance (signal), max pairwise |Pearson| (collinearity), and point-biserial vs a binary commit
# outcome (separation); a driver is KEPT iff it has signal AND is not collinear-redundant AND
# separates outcomes. apply_validation_to_config returns a CONFIG COPY whose DROPPED driver's gate
# path is neutralized (unreachable threshold) so the REAL src.core.gates read only validated drivers
# via their independent fallback triggers — NO change to gates.py. Reuses src.loop._stats (cohens_kappa
# / variance / pearson_correlation / point_biserial; seeded-only stats, NO numpy/scipy/pandas; NO
# LiveKit). DB-free: operates on in-memory Episode trajectories.
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional, Sequence

from src.config.settings import AgentConfig
from src.core.belief_state import DRIVERS, BeliefState
from src.loop import _stats
from src.memory.schema import Episode

# The provisional driver(s) — collinearity ties drop the provisional one FIRST (plan U11: urgency is
# provisional, may fold into need_intensity). Documented tie-break, see _collinear_drop_choice.
PROVISIONAL_DRIVERS: tuple[str, ...] = ("urgency",)

# An unreachable threshold: a driver level can never exceed 1.0 (belief_state._clamp01), so setting a
# driver-gated threshold to this value PERMANENTLY closes that gate's driver path, forcing the gate
# onto its independent fallback trigger (the mechanism behind graceful degradation, R6).
_UNREACHABLE = 1.01

# Map each DROPPED driver -> the gate threshold whose driver PATH must be neutralized so the gate
# "reads only validated drivers". Dropping the driver pushes this threshold to _UNREACHABLE:
#   trust       -> trust_gate_open_price : price_gate's trust path dies; it must open via the
#                  asked-price OR discovery-slots-filled trigger instead (gates._price_gate_open).
#   bail_risk   -> pushiness_cap         : the pushiness SIGNAL path dies; the cap must trip via the
#                  consecutive-pressure COUNT instead (gates.pushiness_cap count trigger).
# Drivers with NO single threshold-gated path (need_intensity / price_sensitivity / urgency /
# purchase_intent) are not in the map — no gate reads them directly, so dropping them needs no config
# neutralization (the gates already don't depend on them; this is why R6 holds for those too).
DRIVER_THRESHOLD_MAP: dict[str, str] = {
    "trust": "trust_gate_open_price",
    "bail_risk": "pushiness_cap",
}

# Fallback experiment names (R38). The default first experiment is discovery_sequencing; if `trust`
# is dropped that sequencing can't be trust-grounded, so we fall back to a price_sensitivity-grounded
# experiment instead (U10's generator / U16 lab consume this name; we don't modify U10).
DEFAULT_EXPERIMENT = "discovery_sequencing"
TRUST_FALLBACK_EXPERIMENT = "price_sensitivity_grounded"


@dataclass
class DriverStats:
    """Per-driver empirical validation stats (plan U11).

    `variance` is the population variance of the driver's per-turn levels across all episodes (its
    SIGNAL); `max_abs_corr` is the largest |Pearson r| with any OTHER driver (its COLLINEARITY) and
    `most_correlated_with` names that partner; `outcome_separation` is the point-biserial of the
    driver's per-episode MEAN against the binary commit outcome (does it distinguish good vs bad
    calls). `kept` is the final verdict; `reasons` lists every reason it was dropped (empty if kept).
    """

    name: str
    variance: float
    max_abs_corr: float
    most_correlated_with: Optional[str]
    outcome_separation: float
    kept: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class DriverValidationReport:
    """The U11 verification artifact: which drivers are KEPT vs DROPPED, the per-driver stats, and
    the named fallback experiment. `kept`/`dropped` partition the six DRIVERS; `stats` carries a
    DriverStats for every driver; `fallback_experiment` is the experiment the lab should run given
    what survived (price_sensitivity_grounded if trust dropped, else discovery_sequencing)."""

    kept: list[str]
    dropped: list[str]
    stats: dict[str, DriverStats]
    fallback_experiment: str


# === driver-value gathering over trajectories ===================================================


def _per_turn_values(episodes: Sequence[Episode]) -> dict[str, list[float]]:
    """Gather every driver's per-turn level across all episodes' belief snapshots (the SIGNAL series).

    Walks each episode's belief_trajectory; a turn with no belief, or a snapshot missing a driver,
    is skipped for that driver (so a partial/older snapshot can't fabricate a 0). Returns one flat
    list per driver in DRIVERS order.
    """
    values: dict[str, list[float]] = {d: [] for d in DRIVERS}
    for ep in episodes:
        for snap in ep.belief_trajectory:
            if snap is None:
                continue
            for d in DRIVERS:
                v = snap.drivers.get(d)
                if v is not None:
                    values[d].append(float(v))
    return values


def _episode_means(episodes: Sequence[Episode]) -> tuple[dict[str, list[float]], list[int]]:
    """Per-driver per-EPISODE mean level + the parallel binary commit outcome (ladder_tier>=1).

    For the outcome-separation point-biserial we need one observation per episode (its mean driver
    level) aligned with that episode's commit label. Episodes with no belief snapshots contribute no
    mean (and no outcome row), so the two lists stay aligned and a belief-less episode is ignored.
    """
    means: dict[str, list[float]] = {d: [] for d in DRIVERS}
    outcomes: list[int] = []
    for ep in episodes:
        snaps = [s for s in ep.belief_trajectory if s is not None]
        if not snaps:
            continue
        outcomes.append(1 if int(ep.ladder_tier) >= 1 else 0)
        for d in DRIVERS:
            levels = [float(s.drivers[d]) for s in snaps if s.drivers.get(d) is not None]
            means[d].append(sum(levels) / len(levels) if levels else 0.0)
    return means, outcomes


# === collinearity tie-break =====================================================================


def _collinear_drop_choice(a: str, b: str) -> str:
    """Which of two mutually-collinear drivers to DROP (documented tie-break, plan U11).

    Rule: drop the PROVISIONAL one first (default `urgency`, per PROVISIONAL_DRIVERS) — it is the
    redundant/provisional driver the plan expects may fold into another; if NEITHER is provisional,
    drop the one that appears LATER in the canonical DRIVERS order (the earlier driver is the more
    established anchor). This makes the drop deterministic and keeps the more-foundational driver.
    """
    a_prov = a in PROVISIONAL_DRIVERS
    b_prov = b in PROVISIONAL_DRIVERS
    if a_prov and not b_prov:
        return a
    if b_prov and not a_prov:
        return b
    # Neither (or both) provisional -> drop the later-in-DRIVERS-order driver.
    return a if DRIVERS.index(a) > DRIVERS.index(b) else b


# === the analysis ===============================================================================


def analyze_drivers(
    episodes: Sequence[Episode],
    *,
    collinearity_threshold: float = 0.9,
    min_variance: float = 1e-4,
    separation_threshold: float = 0.1,
) -> DriverValidationReport:
    """Validate the six drivers empirically over the episodes' belief trajectories (plan U11/R38).

    For each driver compute variance (signal), max pairwise |Pearson| with the other drivers
    (collinearity), and the point-biserial of its per-episode mean vs the binary commit outcome
    (separation). KEEP predicate (quoted in the report): a driver is KEPT iff it has signal
    (variance >= min_variance) AND is not collinear-redundant (it is not the one chosen to drop from
    any >threshold collinear pair) AND separates outcomes (|separation| >= separation_threshold);
    otherwise it is DROPPED with the specific reason(s). Collinearity tie-break: when two drivers are
    mutually collinear, the PROVISIONAL one (default urgency) is dropped, else the later in DRIVERS
    order (see _collinear_drop_choice).
    """
    series = _per_turn_values(episodes)
    means, outcomes = _episode_means(episodes)

    variances = {d: _stats.variance(series[d]) for d in DRIVERS}

    # Pairwise |Pearson| over the per-turn series (only pairs of equal length carry meaning; we use
    # the shared prefix length via pearson_correlation, which truncates to the shorter series).
    max_abs_corr: dict[str, float] = {d: 0.0 for d in DRIVERS}
    partner: dict[str, Optional[str]] = {d: None for d in DRIVERS}
    collinear_redundant: set[str] = set()
    for i, a in enumerate(DRIVERS):
        for b in DRIVERS[i + 1 :]:
            r = abs(_stats.pearson_correlation(series[a], series[b]))
            if r > max_abs_corr[a]:
                max_abs_corr[a], partner[a] = r, b
            if r > max_abs_corr[b]:
                max_abs_corr[b], partner[b] = r, a
            if r > collinearity_threshold:
                collinear_redundant.add(_collinear_drop_choice(a, b))

    separation = {d: _stats.point_biserial(means[d], outcomes) for d in DRIVERS}

    stats: dict[str, DriverStats] = {}
    kept: list[str] = []
    dropped: list[str] = []
    for d in DRIVERS:
        reasons: list[str] = []
        if variances[d] < min_variance:
            reasons.append(f"low-signal: variance {variances[d]:.2e} < {min_variance:.2e}")
        if d in collinear_redundant:
            reasons.append(
                f"collinear: |r|={max_abs_corr[d]:.3f} with {partner[d]} > {collinearity_threshold}"
            )
        if abs(separation[d]) < separation_threshold:
            reasons.append(
                f"non-separating: |point-biserial|={abs(separation[d]):.3f} < {separation_threshold}"
            )
        is_kept = not reasons
        stats[d] = DriverStats(
            name=d,
            variance=variances[d],
            max_abs_corr=max_abs_corr[d],
            most_correlated_with=partner[d],
            outcome_separation=separation[d],
            kept=is_kept,
            reasons=reasons,
        )
        (kept if is_kept else dropped).append(d)

    return DriverValidationReport(
        kept=kept,
        dropped=dropped,
        stats=stats,
        fallback_experiment=_fallback_for(dropped),
    )


# === validated-driver set + graceful-degradation config wiring (R6) ============================


def validated_drivers(report: DriverValidationReport) -> frozenset[str]:
    """The KEPT (validated) driver set — the only drivers the policy/gates should rely on."""
    return frozenset(report.kept)


def dropped_drivers(report: DriverValidationReport) -> frozenset[str]:
    """The DROPPED driver set — drivers whose gate path apply_validation_to_config neutralizes."""
    return frozenset(report.dropped)


def apply_validation_to_config(
    config: AgentConfig, report: DriverValidationReport
) -> AgentConfig:
    """Return a CONFIG COPY in which every DROPPED driver's gate PATH is neutralized so the REAL
    gates read only validated drivers (plan R6 — graceful degradation; gates.py is UNCHANGED).

    For each dropped driver in DRIVER_THRESHOLD_MAP, the mapped threshold is set to _UNREACHABLE
    (1.01): a driver level can never exceed 1.0, so that gate's driver-comparison branch can never
    fire and the gate must use its INDEPENDENT fallback trigger instead (price_gate -> asked-price /
    discovery-slots-filled; pushiness_cap -> consecutive-pressure COUNT). The input config is NEVER
    mutated (AgentConfig is frozen; we replace() it with a fresh thresholds dict). Dropped drivers
    with no threshold-gated path (need_intensity/price_sensitivity/urgency/purchase_intent) are
    no-ops here because no gate reads them directly.
    """
    thresholds = dict(config.thresholds)  # COPY — never mutate the caller's config.
    for driver in report.dropped:
        key = DRIVER_THRESHOLD_MAP.get(driver)
        if key is not None:
            thresholds[key] = _UNREACHABLE
    return replace(config, thresholds=thresholds)


# === fallback experiment (R38) =================================================================


def _fallback_for(dropped: Sequence[str]) -> str:
    """The fallback experiment name given the dropped-driver list: price_sensitivity_grounded when
    `trust` is dropped (discovery sequencing can't be trust-grounded), else discovery_sequencing."""
    return TRUST_FALLBACK_EXPERIMENT if "trust" in set(dropped) else DEFAULT_EXPERIMENT


def fallback_experiment(report: DriverValidationReport) -> str:
    """Select the fallback experiment from a report (plan R38). If `trust` is dropped, the discovery-
    sequencing experiment can't be trust-grounded -> 'price_sensitivity_grounded'; otherwise the
    default first experiment 'discovery_sequencing'."""
    return _fallback_for(report.dropped)


# === inter-annotator agreement (R38) ===========================================================


def inter_annotator_agreement(labels_a: Sequence[object], labels_b: Sequence[object]) -> float:
    """Cohen's kappa between two annotators' driver hand-labels (plan U11/R38) — REUSES
    src.loop._stats.cohens_kappa (no reimplementation). Used to validate that a pilot slice annotated
    on a MIND emotion-band rubric (affect drivers) or an ordinal 1–5 intent rubric (purchase_intent)
    is human-reproducible before the driver is trusted. 1.0 = perfect agreement, 0.0 = chance."""
    return _stats.cohens_kappa(list(labels_a), list(labels_b))


def annotation_agreement_ok(
    labels_a: Sequence[object], labels_b: Sequence[object], *, min_kappa: float = 0.6
) -> bool:
    """True iff inter-annotator kappa clears `min_kappa` (default 0.6 — the substantial-agreement
    floor). A driver whose hand-labels don't clear this is not human-reproducible enough to trust."""
    return inter_annotator_agreement(labels_a, labels_b) >= min_kappa


# === test/inspection helpers ====================================================================


def _belief_state_with(**driver_overrides: float) -> BeliefState:
    """A fresh BeliefState with the named driver levels overridden — a tiny convenience for the
    degradation tests (and any caller probing a single driver's effect on a gate). Non-overridden
    drivers keep the neutral cold-start prior; slots/stage are the fresh defaults."""
    state = BeliefState.fresh()
    for name, level in driver_overrides.items():
        if name in state.drivers:
            state.drivers[name] = float(level)
    return state

# Pure-stdlib statistics primitives for the U9 grading layer (src/loop/grading.py) AND the U11
# driver-validation layer (src/loop/validation.py). Kept in its own module so callers stay under the
# file-size cap and the stats stay dependency-free + testable in isolation. Provides: bootstrap_ci
# (percentile bootstrap of the MEAN), bootstrap_delta_ci (CI of a two-arm mean difference),
# percentile (linear-interpolated), cohens_kappa (chance-corrected agreement for the R39 headline
# gate), and the U11 additions variance / pearson_correlation / point_biserial (driver signal,
# collinearity, outcome separation). All randomness flows through a SEEDED random.Random — NEVER the
# global random module — so every interval is reproducible. NO numpy/scipy/pandas; NO LiveKit.
from __future__ import annotations

import math
from random import Random
from typing import Any, List, Sequence, Tuple


def percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0,1]) over an ALREADY-sorted list. Pure stdlib."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def bootstrap_ci(
    values: Sequence[float],
    *,
    seed: int,
    n_resamples: int = 1000,
    alpha: float = 0.05,
) -> Tuple[float, float]:
    """Percentile bootstrap CI of the MEAN over `values` (plan U9 — pure stdlib).

    Resamples `values` WITH replacement n_resamples times using a SEEDED random.Random (never the
    global random module — reproducible + avoids the banned argless Date/random), and returns the
    (alpha/2, 1-alpha/2) percentile bounds of the resample means. Empty input -> (0.0, 0.0). The
    same (values, seed) always yields an identical CI.
    """
    vals = [float(v) for v in values]
    if not vals:
        return (0.0, 0.0)

    rng = Random(seed)
    n = len(vals)
    means: List[float] = []
    for _ in range(max(1, n_resamples)):
        resample = [vals[rng.randrange(n)] for _ in range(n)]
        means.append(sum(resample) / n)
    means.sort()
    return (percentile(means, alpha / 2.0), percentile(means, 1.0 - alpha / 2.0))


def bootstrap_delta_ci(
    arm_a: Sequence[float],
    arm_b: Sequence[float],
    *,
    seed: int,
    n_resamples: int = 1000,
    alpha: float = 0.05,
) -> Tuple[float, float]:
    """Bootstrap CI of (mean(arm_b) - mean(arm_a)). Resamples each arm independently with a single
    seeded RNG so the same seed reproduces the delta CI. Either arm empty -> (0.0, 0.0)."""
    a = [float(v) for v in arm_a]
    b = [float(v) for v in arm_b]
    if not a or not b:
        return (0.0, 0.0)
    rng = Random(seed)
    na, nb = len(a), len(b)
    deltas: List[float] = []
    for _ in range(max(1, n_resamples)):
        ma = sum(a[rng.randrange(na)] for _ in range(na)) / na
        mb = sum(b[rng.randrange(nb)] for _ in range(nb)) / nb
        deltas.append(mb - ma)
    deltas.sort()
    return (percentile(deltas, alpha / 2.0), percentile(deltas, 1.0 - alpha / 2.0))


def cohens_kappa(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Cohen's kappa for two paired label sequences (chance-corrected agreement), pure stdlib.

    kappa = (po - pe) / (1 - pe), where po is observed agreement and pe is expected-by-chance
    agreement from the marginal label frequencies. 1.0 = perfect, 0.0 = at chance, negative = worse
    than chance. pe==1 (a single label only) -> fall back to raw agreement (kappa undefined).
    """
    n = len(a)
    if n == 0:
        return 0.0
    labels = set(a) | set(b)
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pe = 0.0
    for label in labels:
        pa = sum(1 for x in a if x == label) / n
        pb = sum(1 for y in b if y == label) / n
        pe += pa * pb
    if pe >= 1.0:
        return po  # degenerate (single label) — kappa undefined; report raw agreement
    return (po - pe) / (1.0 - pe)


# === U11 driver-validation primitives (variance / correlation / point-biserial), pure stdlib ===

def variance(values: Sequence[float]) -> float:
    """Population variance of `values` (the U11 driver SIGNAL measure). Pure stdlib.

    Returns 0.0 for fewer than two values (a single observation can carry no variance). Population
    (divide-by-n) not sample variance: we want the spread of the observed levels themselves, and the
    low-signal threshold (min_variance) is calibrated against this absolute spread, not an estimator.
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 2:
        return 0.0
    mean = sum(vals) / n
    return sum((v - mean) ** 2 for v in vals) / n


def pearson_correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson product-moment correlation of two paired series (the U11 COLLINEARITY measure).

    r = cov(x, y) / (std(x) * std(y)), in [-1, 1]. Returns 0.0 when the series are mismatched/empty
    or when EITHER series is constant (zero variance -> correlation undefined; 0.0 means "no linear
    redundancy we can claim"). Pure stdlib — NO numpy/scipy. Reused by analyze_drivers for the
    max-pairwise-|r| collinearity flag.
    """
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    x = [float(v) for v in xs[:n]]
    y = [float(v) for v in ys[:n]]
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    var_x = sum((a - mx) ** 2 for a in x)
    var_y = sum((b - my) ** 2 for b in y)
    denom = math.sqrt(var_x * var_y)
    if denom == 0.0:
        return 0.0  # a constant series has no defined linear correlation
    return cov / denom


def point_biserial(values: Sequence[float], binary: Sequence[int]) -> float:
    """Point-biserial correlation between a continuous `values` series and a 0/1 `binary` outcome
    (the U11 OUTCOME-SEPARATION measure). Equals the Pearson r of the continuous series with the
    binary one, so a value that is systematically higher for the 1-group separates the outcomes.

    Returns 0.0 for mismatched/empty input or when either side is constant (e.g. all-committed or a
    driver that never moves) — in those degenerate cases separation is undefined and we report 0.0
    (NON-separating). Pure stdlib; coerces the binary labels to floats and delegates to
    pearson_correlation so there is ONE correlation implementation.
    """
    return pearson_correlation([float(v) for v in values], [float(b) for b in binary])

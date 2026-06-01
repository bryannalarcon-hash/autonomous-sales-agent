# Pure-stdlib statistics primitives for the U9 grading layer (src/loop/grading.py). Kept in its own
# module so grading.py stays under the file-size cap and the stats stay dependency-free + testable in
# isolation. Provides: bootstrap_ci (percentile bootstrap of the MEAN), bootstrap_delta_ci (CI of a
# two-arm mean difference), _percentile (linear-interpolated), and cohens_kappa (chance-corrected
# agreement for the R39 headline gate). All randomness flows through a SEEDED random.Random — NEVER
# the global random module — so every interval is reproducible. NO numpy/scipy/pandas; NO LiveKit.
from __future__ import annotations

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

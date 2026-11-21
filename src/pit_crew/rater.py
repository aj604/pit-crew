"""Scores a candidate. Deterministic code: no model call, and the agent cannot edit it.

A candidate is kept only if it catches every bug the baseline caught, achieves at least a
5% median speedup (`MIN_SPEEDUP = 1.05`), and beats timing noise (`p <= 0.05`).
Also computes whether the candidate satisfies Pit Crew's repo CI SLA
(`median <= 60s` and `p95 <= 120s`) so the Dispatcher knows when to stop compounding.
"""

import random
from itertools import combinations
from statistics import mean, median

from pit_crew.contracts import Evidence, Verdict

MIN_RUNS = 5
MAX_P_VALUE = 0.05
MIN_SPEEDUP = 1.05
TARGET_MEDIAN_SECONDS = 60.0
TARGET_P95_SECONDS = 120.0
MAX_EXACT_SIDE_RUNS = 10
MONTE_CARLO_SPLITS = 20_000


def rate(evidence: Evidence) -> Verdict:
    """Deterministically score ``evidence`` into a keep/reject ``Verdict``."""
    base, cand = evidence.baseline_seconds, evidence.candidate_seconds
    cand_med = float(median(cand)) if cand else 0.0
    cand_p95 = _p95(cand) if cand else 0.0
    sla_met = bool(cand) and cand_med <= TARGET_MEDIAN_SECONDS and cand_p95 <= TARGET_P95_SECONDS

    def verdict(*, keep: bool, reason: str, speedup: float = 0.0, p: float | None = None) -> Verdict:
        return Verdict(
            evidence.candidate_id,
            keep,
            speedup,
            p,
            reason,
            median_seconds=cand_med,
            p95_seconds=cand_p95,
            sla_met=sla_met,
        )

    precheck_failure = _precheck_failure(evidence)
    if precheck_failure is not None:
        return verdict(keep=False, reason=precheck_failure)

    base_med = float(median(base))
    speedup = (1.0 if base_med == 0.0 else float("inf")) if cand_med == 0.0 else base_med / cand_med
    p = _p_faster(base, cand)

    if speedup <= 1.0 or p > MAX_P_VALUE:
        return verdict(keep=False, reason="speedup is within noise", speedup=speedup, p=p)
    if speedup < MIN_SPEEDUP:
        return verdict(
            keep=False,
            reason=f"speedup {speedup:.2f}x is below {MIN_SPEEDUP:.2f}x minimum",
            speedup=speedup,
            p=p,
        )

    sla_note = " (SLA met)" if sla_met else ""
    return verdict(
        keep=True,
        reason=f"{speedup:.2f}x faster, caught {evidence.bugs_caught}/{evidence.bugs_total} bugs{sla_note}",
        speedup=speedup,
        p=p,
    )


def _precheck_failure(evidence: Evidence) -> str | None:
    if evidence.bugs_total <= 0:
        return "no bugs replayed, so safety is unproven"
    if evidence.bugs_caught < evidence.bugs_total:
        return f"caught {evidence.bugs_caught}/{evidence.bugs_total} bugs"
    base, cand = evidence.baseline_seconds, evidence.candidate_seconds
    if min(len(base), len(cand)) < MIN_RUNS:
        return f"needs {MIN_RUNS} runs per side"
    if min(base) < 0 or min(cand) < 0:
        return "negative run duration is invalid"
    return None


def _p95(values: tuple[float, ...]) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = 0.95 * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return float(ordered[lo] + frac * (ordered[hi] - ordered[lo]))


def _p_faster(base: tuple[float, ...], cand: tuple[float, ...]) -> float:
    """One-sided permutation test: chance of a mean gap this large if the change did nothing.

    Uses exact enumeration up to ``MAX_EXACT_SIDE_RUNS`` per side and a deterministically
    seeded Monte Carlo permutation test above that so runtime stays bounded.
    """
    pooled = list(base + cand)
    n_base, n_cand = len(base), len(cand)
    observed = mean(base) - mean(cand)
    total = sum(pooled)
    eps = 1e-12

    if max(n_base, n_cand) <= MAX_EXACT_SIDE_RUNS:
        splits = hits = 0
        for group in combinations(pooled, n_base):
            group_sum = sum(group)
            gap = group_sum / n_base - (total - group_sum) / n_cand
            splits += 1
            hits += gap >= observed - eps
        return hits / splits

    rng = random.Random(0)  # noqa: S311 - deterministic permutation seed, not cryptographic
    hits = 1  # include the observed split itself
    for _ in range(MONTE_CARLO_SPLITS - 1):
        sample = rng.sample(pooled, n_base)
        group_sum = sum(sample)
        gap = group_sum / n_base - (total - group_sum) / n_cand
        hits += gap >= observed - eps
    return hits / MONTE_CARLO_SPLITS

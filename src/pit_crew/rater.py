"""Scores a candidate. Deterministic code: no model call, and the agent cannot edit it.

A candidate is kept only if it catches every bug the baseline caught and is faster
by more than timing noise.
"""

from itertools import combinations
from statistics import mean, median

from pit_crew.contracts import Evidence, Verdict

MIN_RUNS = 5
MAX_P_VALUE = 0.05


def rate(evidence: Evidence) -> Verdict:
    base, cand = evidence.baseline_seconds, evidence.candidate_seconds

    def verdict(keep: bool, reason: str, speedup: float = 0.0, p: float | None = None) -> Verdict:
        return Verdict(evidence.candidate_id, keep, speedup, p, reason)

    if evidence.bugs_total == 0:
        return verdict(False, "no bugs replayed, so safety is unproven")
    if evidence.bugs_caught < evidence.bugs_total:
        return verdict(False, f"caught {evidence.bugs_caught}/{evidence.bugs_total} bugs")
    if min(len(base), len(cand)) < MIN_RUNS:
        return verdict(False, f"needs {MIN_RUNS} runs per side")

    speedup = median(base) / median(cand)
    p = _p_faster(base, cand)
    if speedup <= 1 or p > MAX_P_VALUE:
        return verdict(False, "speedup is within noise", speedup, p)
    return verdict(True, f"{speedup:.2f}x faster, caught {evidence.bugs_caught}/{evidence.bugs_total} bugs", speedup, p)


def _p_faster(base: tuple[float, ...], cand: tuple[float, ...]) -> float:
    """Exact one-sided permutation test: chance of a gap this large if the change did nothing.

    Enumerates every split, so keep it to about 10 runs per side.
    """
    pooled = base + cand
    observed = mean(base) - mean(cand)
    total = sum(pooled)
    splits = hits = 0
    for group in combinations(pooled, len(base)):
        group_sum = sum(group)
        gap = group_sum / len(base) - (total - group_sum) / len(cand)
        splits += 1
        hits += gap >= observed
    return hits / splits

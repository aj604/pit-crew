from pit_crew.contracts import Evidence
from pit_crew.rater import rate

SLOW = (600.0, 610.0, 590.0, 605.0, 595.0)
FAST = (300.0, 310.0, 290.0, 305.0, 295.0)
SLA_FAST = (48.0, 52.0, 55.0, 90.0, 50.0)


def evidence(
    baseline: tuple[float, ...] = SLOW,
    candidate: tuple[float, ...] = FAST,
    bugs_total: int = 20,
    bugs_caught: int = 20,
) -> Evidence:
    return Evidence("c1", baseline, candidate, bugs_total, bugs_caught)


def test_keeps_a_faster_candidate_that_catches_every_bug() -> None:
    verdict = rate(evidence())
    assert verdict.keep
    assert round(verdict.speedup, 1) == 2.0
    assert verdict.median_seconds == 300.0
    assert not verdict.sla_met


def test_marks_sla_met_when_median_under_60s_and_p95_under_120s() -> None:
    verdict = rate(evidence(candidate=SLA_FAST))
    assert verdict.keep
    assert verdict.sla_met
    assert verdict.median_seconds == 52.0
    assert verdict.p95_seconds <= 120.0
    assert "SLA met" in verdict.reason


def test_rejects_a_faster_candidate_that_misses_a_bug() -> None:
    verdict = rate(evidence(bugs_caught=19))
    assert not verdict.keep
    assert "19/20" in verdict.reason


def test_rejects_a_speedup_within_noise() -> None:
    noisy = (598.0, 612.0, 588.0, 607.0, 593.0)
    assert not rate(evidence(candidate=noisy)).keep


def test_rejects_statistically_significant_but_trivial_speedup_below_5_percent() -> None:
    trivial = (580.0, 581.0, 579.0, 580.5, 579.5)  # ~1.03x vs 600s
    verdict = rate(evidence(candidate=trivial))
    assert not verdict.keep
    assert "minimum" in verdict.reason


def test_rejects_a_slower_candidate() -> None:
    assert not rate(evidence(baseline=FAST, candidate=SLOW)).keep


def test_rejects_identical_and_zero_and_negative_timings() -> None:
    assert not rate(evidence(baseline=SLOW, candidate=SLOW)).keep
    assert not rate(evidence(baseline=(0.0, 0.0, 0.0, 0.0, 0.0), candidate=(0.0, 0.0, 0.0, 0.0, 0.0))).keep
    assert not rate(evidence(candidate=(-1.0, 10.0, 10.0, 10.0, 10.0))).keep
    assert rate(evidence(baseline=SLOW, candidate=(0.0, 0.0, 0.0, 0.0, 0.0))).keep


def test_rejects_too_few_runs() -> None:
    assert not rate(evidence(baseline=SLOW[:3], candidate=FAST[:3])).keep


def test_rejects_a_candidate_with_no_bugs_replayed() -> None:
    assert not rate(evidence(bugs_total=0, bugs_caught=0)).keep


def test_handles_large_sample_sizes_via_deterministic_monte_carlo() -> None:
    large_base = tuple(200.0 + (i % 5) for i in range(15))
    large_cand = tuple(50.0 + (i % 5) for i in range(15))
    v1 = rate(evidence(baseline=large_base, candidate=large_cand))
    v2 = rate(evidence(baseline=large_base, candidate=large_cand))
    assert v1.keep
    assert v1.sla_met
    assert v1.p_value == v2.p_value

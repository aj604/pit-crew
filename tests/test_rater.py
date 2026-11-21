from pit_crew.contracts import Evidence
from pit_crew.rater import rate

SLOW = (600.0, 610.0, 590.0, 605.0, 595.0)
FAST = (300.0, 310.0, 290.0, 305.0, 295.0)


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


def test_rejects_a_faster_candidate_that_misses_a_bug() -> None:
    verdict = rate(evidence(bugs_caught=19))
    assert not verdict.keep
    assert "19/20" in verdict.reason


def test_rejects_a_speedup_within_noise() -> None:
    noisy = (598.0, 612.0, 588.0, 607.0, 593.0)
    assert not rate(evidence(candidate=noisy)).keep


def test_rejects_a_slower_candidate() -> None:
    assert not rate(evidence(baseline=FAST, candidate=SLOW)).keep


def test_rejects_too_few_runs() -> None:
    assert not rate(evidence(baseline=SLOW[:3], candidate=FAST[:3])).keep


def test_rejects_a_candidate_with_no_bugs_replayed() -> None:
    assert not rate(evidence(bugs_total=0, bugs_caught=0)).keep

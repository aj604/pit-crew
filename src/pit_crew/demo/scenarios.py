"""Realistic offline M1 demo scenarios backed by real contracts and rater math."""

from dataclasses import dataclass
from typing import Any

from pit_crew.contracts import BugCase, Candidate, Evidence, Hypothesis, ReplayPlan

DEMO_REPO = "acme/checkout-service"
_BASE_SHA = "e4f8c2a1903b"

_COMMON_BUG_CASES: tuple[BugCase, ...] = (
    BugCase(
        kind="commit",
        commit_sha="a1b2c3d",
        reason="Null pointer in auth middleware when session header is missing",
    ),
    BugCase(
        kind="commit",
        commit_sha="d4e5f6a",
        reason="Rounding regression in multi-currency tax calculation",
    ),
    BugCase(
        kind="commit",
        commit_sha="7b8c9d0",
        reason="Race condition in webhook idempotency lock",
    ),
    BugCase(
        kind="commit",
        commit_sha="1f2e3d4",
        reason="SQLite migration failure on fresh database schema",
    ),
)

_COMMON_BUG_CHECKS: dict[str, str] = {
    "a1b2c3d": "unit-tests",
    "d4e5f6a": "unit-tests",
    "7b8c9d0": "integration-tests",
    "1f2e3d4": "integration-tests",
}


@dataclass(frozen=True)
class DemoScenario:
    """One self-contained M1 demo scenario with real Pit Crew contract records."""

    id: str
    title: str
    description: str
    expected_outcome: str  # "keep" or "reject"
    repo: str
    hypothesis: Hypothesis
    candidate: Candidate
    replay_plan: ReplayPlan
    evidence: Evidence
    diff: str
    bug_summaries: dict[str, dict[str, Any]]

    def summary_dict(self) -> dict[str, str]:
        """Return lightweight metadata for GET /api/scenarios."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "expected_outcome": self.expected_outcome,
            "repo": self.repo,
            "skill": self.hypothesis.skill,
        }


def _build_bug_summaries(caught_shas: set[str]) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for case in _COMMON_BUG_CASES:
        sha = case.commit_sha or ""
        summaries[sha] = {
            "bug_sha": sha,
            "summary": case.reason,
            "check": _COMMON_BUG_CHECKS[sha],
            "caught": sha in caught_shas,
        }
    return summaries


def _build_scenarios() -> tuple[DemoScenario, ...]:
    # 1. Fast but unsafe: skips integration tests on unrelated paths -> misses 1/4 bugs
    h1 = Hypothesis(
        id="hyp-missed-bug",
        repo=DEMO_REPO,
        description="Run only tests affected by changed files to cut CI runtime by ~140s",
        skill="affected-tests",
        expected_savings_seconds=140.0,
        base_sha=_BASE_SHA,
        workflow_path=".github/workflows/ci.yml",
        job="test",
        step="Run pytest",
        bucket="running",
        evidence={"step_p50_seconds": 182.0, "runs_sampled": 20.0},
        confidence=0.72,
        risk="high",
    )
    c1 = Candidate(
        id="cand-missed-bug",
        hypothesis_id=h1.id,
        branch="pc/hyp-missed-bug-affected-tests",
        summary="Filter pytest targets with --testmon based on git diff",
        repo=DEMO_REPO,
        base_sha=_BASE_SHA,
        head_sha="9c11a4f02b8e",
        changed_files=(".github/workflows/ci.yml",),
    )
    p1 = ReplayPlan(id="plan-missed-bug", candidate_id=c1.id, cases=_COMMON_BUG_CASES)
    caught_1 = {"a1b2c3d", "d4e5f6a", "1f2e3d4"}  # misses 7b8c9d0
    bug_summaries_1 = _build_bug_summaries(caught_1)
    e1 = Evidence(
        id="ev-missed-bug",
        candidate_id=c1.id,
        baseline_seconds=(238.4, 244.1, 236.8, 241.5, 240.2),
        candidate_seconds=(94.2, 97.6, 92.8, 95.1, 96.4),
        bugs_total=len(_COMMON_BUG_CASES),
        bugs_caught=len(caught_1),
        bug_outcomes=tuple(bug_summaries_1.values()),
    )
    diff_1 = (
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -24,3 +24,3 @@\n"
        "       - name: Run pytest\n"
        "-        run: uv run pytest\n"
        "+        run: uv run pytest --testmon\n"
    )

    # 2. Fast and proven: caches uv dependencies -> ~1.85x faster, p < 0.05, 4/4 bugs caught
    h2 = Hypothesis(
        id="hyp-dep-cache",
        repo=DEMO_REPO,
        description="Cache uv virtualenv and wheel downloads across CI runs",
        skill="dep-cache",
        expected_savings_seconds=110.0,
        base_sha=_BASE_SHA,
        workflow_path=".github/workflows/ci.yml",
        job="test",
        step="Install dependencies",
        bucket="setup",
        evidence={"step_p50_seconds": 114.5, "runs_sampled": 20.0},
        confidence=0.94,
        risk="low",
    )
    c2 = Candidate(
        id="cand-dep-cache",
        hypothesis_id=h2.id,
        branch="pc/hyp-dep-cache-dep-cache",
        summary="Enable astral-sh/setup-uv dependency caching keyed on uv.lock",
        repo=DEMO_REPO,
        base_sha=_BASE_SHA,
        head_sha="4a72d9e10c3f",
        changed_files=(".github/workflows/ci.yml",),
    )
    p2 = ReplayPlan(id="plan-dep-cache", candidate_id=c2.id, cases=_COMMON_BUG_CASES)
    caught_2 = {"a1b2c3d", "d4e5f6a", "7b8c9d0", "1f2e3d4"}
    bug_summaries_2 = _build_bug_summaries(caught_2)
    e2 = Evidence(
        id="ev-dep-cache",
        candidate_id=c2.id,
        baseline_seconds=(239.2, 243.8, 237.5, 242.1, 240.0),
        candidate_seconds=(129.4, 132.1, 128.0, 131.5, 130.2),
        bugs_total=len(_COMMON_BUG_CASES),
        bugs_caught=len(caught_2),
        bug_outcomes=tuple(bug_summaries_2.values()),
    )
    diff_2 = (
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -16,2 +16,5 @@\n"
        "       - uses: astral-sh/setup-uv@v5\n"
        "+        with:\n"
        "+          enable-cache: true\n"
        "+          cache-dependency-glob: uv.lock\n"
    )

    # 3. Within noise: small overlapping speedup -> p > 0.05 -> REJECT
    h3 = Hypothesis(
        id="hyp-noise",
        repo=DEMO_REPO,
        description="Deduplicate lint checkout step across workflow jobs",
        skill="dedupe-jobs",
        expected_savings_seconds=12.0,
        base_sha=_BASE_SHA,
        workflow_path=".github/workflows/ci.yml",
        job="lint",
        step="Checkout",
        bucket="setup",
        evidence={"step_p50_seconds": 14.0, "runs_sampled": 20.0},
        confidence=0.55,
        risk="low",
    )
    c3 = Candidate(
        id="cand-noise",
        hypothesis_id=h3.id,
        branch="pc/hyp-noise-dedupe-jobs",
        summary="Share checkout step between lint and typecheck jobs",
        repo=DEMO_REPO,
        base_sha=_BASE_SHA,
        head_sha="8f31c0b74d2a",
        changed_files=(".github/workflows/ci.yml",),
    )
    p3 = ReplayPlan(id="plan-noise", candidate_id=c3.id, cases=_COMMON_BUG_CASES)
    caught_3 = {"a1b2c3d", "d4e5f6a", "7b8c9d0", "1f2e3d4"}
    bug_summaries_3 = _build_bug_summaries(caught_3)
    e3 = Evidence(
        id="ev-noise",
        candidate_id=c3.id,
        baseline_seconds=(232.0, 245.0, 238.0, 249.0, 240.0),
        candidate_seconds=(234.0, 241.0, 231.0, 246.0, 236.0),
        bugs_total=len(_COMMON_BUG_CASES),
        bugs_caught=len(caught_3),
        bug_outcomes=tuple(bug_summaries_3.values()),
    )
    diff_3 = (
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -35,5 +35,2 @@\n"
        "-  typecheck:\n"
        "-    runs-on: ubuntu-latest\n"
        "-    steps:\n"
        "-      - uses: actions/checkout@v4\n"
        "+      - name: Typecheck\n"
        "+        run: uv run mypy src\n"
    )

    return (
        DemoScenario(
            id="rejected-missed-bug",
            title="Fast but Unsafe (Missed Bug)",
            description="2.5x faster via affected-tests, but misses 1 of 4 historical bugs -> REJECTED.",
            expected_outcome="reject",
            repo=DEMO_REPO,
            hypothesis=h1,
            candidate=c1,
            replay_plan=p1,
            evidence=e1,
            diff=diff_1,
            bug_summaries=bug_summaries_1,
        ),
        DemoScenario(
            id="kept-dep-cache",
            title="Fast and Proven (Dependency Cache)",
            description="~1.85x faster via dep-cache (p < 0.05) and catches all 4/4 bugs -> KEPT.",
            expected_outcome="keep",
            repo=DEMO_REPO,
            hypothesis=h2,
            candidate=c2,
            replay_plan=p2,
            evidence=e2,
            diff=diff_2,
            bug_summaries=bug_summaries_2,
        ),
        DemoScenario(
            id="rejected-noise",
            title="Within Timing Noise",
            description="Catches all 4/4 bugs, but speedup is statistically indistinguishable from noise -> REJECTED.",
            expected_outcome="reject",
            repo=DEMO_REPO,
            hypothesis=h3,
            candidate=c3,
            replay_plan=p3,
            evidence=e3,
            diff=diff_3,
            bug_summaries=bug_summaries_3,
        ),
    )


SCENARIOS: dict[str, DemoScenario] = {s.id: s for s in _build_scenarios()}


class UnknownScenarioError(KeyError):
    """Raised when a requested demo scenario ID does not exist."""

    def __init__(self, scenario_id: str) -> None:
        """Build error for an unknown scenario ID."""
        super().__init__(f"Unknown demo scenario: {scenario_id!r}")


def get_scenario(scenario_id: str) -> DemoScenario:
    """Look up a demo scenario by ID, raising UnknownScenarioError if unknown."""
    if scenario_id not in SCENARIOS:
        raise UnknownScenarioError(scenario_id)
    return SCENARIOS[scenario_id]

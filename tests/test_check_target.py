"""Offline tests for the ``check-target`` readiness preflight.

The GitHub API is replaced by a getter serving ``fetch_history``'s two endpoints from an in-memory
history, and Analysis ``find_bottlenecks`` by a stub, so every preflight condition can be set up
on its own. Job display names come from the real frozen template ``ci.yml``.
"""

import io
import json
import os
import threading
from collections.abc import Mapping
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import yaml

from pit_crew import check_target as ct
from pit_crew import cli
from pit_crew.benchmark_seeder import DEFAULT_TEMPLATE, WORKFLOW_PATH
from pit_crew.lanes import analysis
from pit_crew.lanes.analysis import tools as analysis_tools

REPO = "octo/pit-crew-bench"
TEMPLATE_CI = (DEFAULT_TEMPLATE / WORKFLOW_PATH).read_text()
JOB_NAMES: dict[str, str] = {job_id: body["name"] for job_id, body in yaml.safe_load(TEMPLATE_CI)["jobs"].items()}
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


class FakeHistory:
    """GitHub Actions run history for one workflow, served the way ``fetch_history`` asks for it."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []  # oldest first
        self._lock = threading.Lock()
        self.urls: list[str] = []

    def add(  # noqa: PLR0913  one keyword per run field
        self,
        sha: str,
        *,
        branch: str = "main",
        event: str = "workflow_dispatch",
        failing: Mapping[str, list[str]] | None = None,
        seconds: float = 60.0,
        path: str = WORKFLOW_PATH,
    ) -> None:
        """Add a completed run of every template job; ``failing`` maps job id -> conclusion per attempt."""
        failing = failing or {}
        attempts = max((len(v) for v in failing.values()), default=1)
        start = T0 + timedelta(hours=len(self.runs))
        run_id = 100 + len(self.runs)
        jobs: list[dict[str, Any]] = []
        for attempt in range(1, attempts + 1):
            for job_id, name in JOB_NAMES.items():
                outcomes = failing.get(job_id)
                conclusion = outcomes[attempt - 1] if outcomes else "success"
                jobs.append(
                    {
                        "id": run_id * 100 + len(jobs),
                        "run_id": run_id,
                        "run_attempt": attempt,
                        "name": name,
                        "status": "completed",
                        "conclusion": conclusion,
                        "created_at": _iso(start),
                        "started_at": _iso(start),
                        "completed_at": _iso(start + timedelta(seconds=seconds)),
                        "labels": ["ubuntu-latest"],
                        "steps": [],
                    }
                )
        red = any(o and o[-1] == "failure" for o in failing.values())
        self.runs.append(
            {
                "id": run_id,
                "name": "CI",
                "path": path,
                "event": event,
                "head_branch": branch,
                "head_sha": sha,
                "status": "completed",
                "conclusion": "failure" if red else "success",
                "run_attempt": attempts,
                "created_at": _iso(start),
                "run_started_at": _iso(start),
                "updated_at": _iso(start + timedelta(seconds=seconds)),
                "jobs": jobs,
            }
        )

    def bug(self, job_id: str, *, fixed: bool = True) -> None:
        """A commit on its own branch that fails ``job_id`` deterministically, then (if ``fixed``) its fix."""
        branch = f"bug/{job_id}"
        self.add(f"{job_id}-red", branch=branch, event="push", failing={job_id: ["failure"]})
        if fixed:
            self.add(f"{job_id}-green", branch=branch, event="push")

    def get(self, url: str) -> tuple[Any, str | None]:
        with self._lock:
            self.urls.append(url)
        path = urlparse(url).path
        if path == f"/repos/{REPO}/actions/workflows/ci.yml/runs":
            return {"workflow_runs": [{k: v for k, v in r.items() if k != "jobs"} for r in reversed(self.runs)]}, None
        prefix = f"/repos/{REPO}/actions/runs/"
        assert path.startswith(prefix), url
        run_id = int(path.removeprefix(prefix).split("/", 1)[0])
        return {"jobs": next(r["jobs"] for r in self.runs if r["id"] == run_id)}, None


def _ready_history(baseline: int = 3) -> FakeHistory:
    history = FakeHistory()
    for i in range(baseline):
        history.add(f"base{i}", seconds=100.0 + 10 * i)
    for job_id in ct.REQUIRED_CHECKS:
        history.bug(job_id)
    return history


def _finding(skill: str = "dep-cache", savings: float = 30.0) -> dict[str, Any]:
    return {"skill": skill, "expected_savings_seconds": savings}


def _finder(*findings: dict[str, Any]) -> ct.FindBottlenecks:
    chosen = list(findings) if findings else [_finding(s) for s in ("dep-cache", "parallelize-dag", "dedupe-jobs")]

    def find(repo: str, workflow: str) -> Mapping[str, Any]:
        assert (repo, workflow) == (REPO, "ci.yml")
        return {"findings": chosen}

    return find


def _check(history: FakeHistory, finder: ct.FindBottlenecks | None = None, **kwargs: Any) -> ct.Readiness:  # noqa: ANN401
    return ct.check_target(REPO, get=history.get, find_bottlenecks=finder or _finder(), **kwargs)


def _main(argv: list[str], **kwargs: Any) -> tuple[int, dict[str, Any]]:  # noqa: ANN401
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(argv, **kwargs)
    return code, json.loads(out.getvalue())


# --- passing ------------------------------------------------------------------------------------


def test_ready_target_reports_every_field() -> None:
    report = _check(_ready_history()).as_json()

    assert report == {
        "repo": REPO,
        "base_sha": "base2",
        "green_runs": 3,
        "baseline_p50_seconds": 110.0,
        "baseline_p95_seconds": 120.0,
        "findings_count": 3,
        "red_commits_by_check": {job_id: [f"{job_id}-red"] for job_id in ct.REQUIRED_CHECKS},
        "ready": True,
        "problems": [],
    }


def test_cli_exits_zero_and_prints_json_when_ready() -> None:
    code, report = _main(["check-target", "--repo", REPO], get=_ready_history().get, find_bottlenecks=_finder())

    assert code == 0
    assert report["command"] == "check-target"
    assert report["ready"] is True
    assert report["findings_count"] == 3


def test_baseline_counts_only_green_dispatch_runs_on_main_of_the_frozen_workflow() -> None:
    history = _ready_history(baseline=1)
    history.add("pushed", event="push")
    history.add("feature", branch="feature")
    history.add("red-main", failing={"lint": ["failure"]})
    history.add("other-file", path=".github/workflows/other.yml")
    history.add("base-newest", seconds=200.0)

    report = _check(history)

    assert report.green_runs == 2
    assert report.base_sha == "base-newest"
    assert (report.baseline_p50_seconds, report.baseline_p95_seconds) == (100.0, 200.0)


# --- failing: Analysis --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "findings",
    [
        pytest.param([_finding(), _finding("parallelize-dag")], id="too-few"),
        pytest.param([_finding(), _finding("parallelize-dag"), _finding("dedupe-jobs", 0.0)], id="zero-savings"),
        pytest.param([_finding(), _finding("parallelize-dag"), _finding("not-a-skill")], id="unknown-skill"),
        pytest.param([_finding(), _finding("parallelize-dag"), {"skill": "dedupe-jobs"}], id="no-savings"),
        pytest.param(
            [_finding(), _finding("parallelize-dag"), _finding("dedupe-jobs") | {"expected_savings_seconds": "x"}],
            id="bad-savings",
        ),
    ],
)
def test_too_few_qualifying_findings_is_not_ready(findings: list[dict[str, Any]]) -> None:
    report = _check(_ready_history(), _finder(*findings))

    assert report.findings_count == 2
    assert not report.ready
    assert report.problems == [
        "Analysis found 2 ranked PLAYBOOK_SKILLS finding(s) saving > 0s; need at least 3",
    ]


def test_cli_exits_non_zero_when_not_ready() -> None:
    code, report = _main(
        ["check-target", "--repo", REPO], get=_ready_history().get, find_bottlenecks=_finder(_finding())
    )

    assert code == 1
    assert report["ready"] is False
    assert report["findings_count"] == 1


# --- failing: Proof -----------------------------------------------------------------------------


def test_red_commit_never_fixed_leaves_its_check_uncovered() -> None:
    history = FakeHistory()
    history.add("base")
    for job_id in ct.REQUIRED_CHECKS:
        history.bug(job_id, fixed=job_id != "ui-check")

    report = _check(history)

    assert not report.ready
    assert report.red_commits_by_check["ui-check"] == []
    assert report.problems == ["Proof has no fixed deterministic red commit for required check 'ui-check'"]


def test_check_without_any_red_commit_is_uncovered() -> None:
    history = FakeHistory()
    history.add("base")
    for job_id in ct.REQUIRED_CHECKS:
        if job_id != "typecheck":
            history.bug(job_id)

    report = _check(history)

    assert not report.ready
    assert report.red_commits_by_check["typecheck"] == []
    assert report.problems == ["Proof has no fixed deterministic red commit for required check 'typecheck'"]


def test_flaky_failure_is_not_a_deterministic_red_commit() -> None:
    history = FakeHistory()
    history.add("base")
    for job_id in ct.REQUIRED_CHECKS:
        if job_id == "lint":
            history.add("lint-flake", branch="bug/lint", event="push", failing={"lint": ["failure", "success"]})
            history.add("lint-after", branch="bug/lint", event="push")
        else:
            history.bug(job_id)

    report = _check(history)

    assert report.problems == ["Proof has no fixed deterministic red commit for required check 'lint'"]


def test_required_check_missing_from_workflow_is_not_ready() -> None:
    doc = yaml.safe_load(TEMPLATE_CI)
    del doc["jobs"]["ui-check"]

    report = _check(_ready_history(), workflow_text=yaml.safe_dump(doc))

    assert not report.ready
    assert "ui-check" not in report.red_commits_by_check
    assert report.problems == [f"required check 'ui-check' is not a job in {WORKFLOW_PATH}"]


def test_unparseable_workflow_is_not_ready() -> None:
    report = _check(_ready_history(), workflow_text="jobs: [not, a, mapping]")

    assert not report.ready
    assert report.red_commits_by_check == {}
    assert report.problems == [f"{WORKFLOW_PATH} is not a parseable workflow"]


def test_empty_history_is_not_ready_without_crashing() -> None:
    report = _check(FakeHistory())

    assert report.as_json() | {"problems": []} == {
        "repo": REPO,
        "base_sha": None,
        "green_runs": 0,
        "baseline_p50_seconds": 0.0,
        "baseline_p95_seconds": 0.0,
        "findings_count": 3,
        "red_commits_by_check": {job_id: [] for job_id in ct.REQUIRED_CHECKS},
        "ready": False,
        "problems": [],
    }
    assert report.problems[0] == f"no green workflow_dispatch baseline runs of {WORKFLOW_PATH} on main"
    assert len(report.problems) == 1 + len(ct.REQUIRED_CHECKS)


# --- Analysis -> Proof conversion ---------------------------------------------------------------


def test_to_proof_job_runs_keeps_workflow_and_display_names() -> None:
    raw: list[dict[str, Any]] = [
        {
            "name": "CI",
            "head_sha": "abc",
            "head_branch": None,
            "created_at": "2026-11-01T00:00:00Z",
            "run_attempt": 2,
            "jobs": [
                {"name": "Unit test", "created_at": "2026-11-01T00:00:05Z", "run_attempt": 1, "conclusion": "failure"},
                {"name": "Lint", "conclusion": None},
            ],
        },
        {"head_sha": "def", "jobs": None},
    ]

    assert ct.to_proof_job_runs(raw) == [
        {
            "sha": "abc",
            "branch": "",
            "created_at": "2026-11-01T00:00:05Z",
            "workflow": "CI",
            "job": "Unit test",
            "attempt": 1,
            "conclusion": "failure",
        },
        {
            "sha": "abc",
            "branch": "",
            "created_at": "2026-11-01T00:00:00Z",
            "workflow": "CI",
            "job": "Lint",
            "attempt": 2,
            "conclusion": "",
        },
    ]


# --- the default find_bottlenecks ---------------------------------------------------------------


def _without_find_bottlenecks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(analysis, "find_bottlenecks", raising=False)
    monkeypatch.delattr(analysis_tools, "find_bottlenecks", raising=False)


def test_default_find_bottlenecks_explains_when_analysis_has_not_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_find_bottlenecks(monkeypatch)

    with pytest.raises(ct.FindBottlenecksUnavailableError, match="find_bottlenecks is not yet installed"):
        ct.default_find_bottlenecks()


def test_cli_reports_missing_find_bottlenecks_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_find_bottlenecks(monkeypatch)

    code, report = _main(["check-target", "--repo", REPO], get=_ready_history().get)

    assert code == 1
    assert report["ready"] is False
    assert report["error"].startswith("FindBottlenecksUnavailableError: Analysis find_bottlenecks is not yet installed")


def test_default_find_bottlenecks_calls_the_analysis_tool_in_the_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _without_find_bottlenecks(monkeypatch)
    monkeypatch.delenv(ct.WORKSPACE_ENV, raising=False)
    calls: list[tuple[Any, ...]] = []

    def find_bottlenecks(ledger: object, repo: str, workflow: str) -> dict[str, Any]:
        calls.append((type(ledger).__name__, repo, workflow, os.environ.get(ct.WORKSPACE_ENV)))
        return {"findings": [_finding(s) for s in ("dep-cache", "parallelize-dag", "dedupe-jobs")]}

    monkeypatch.setattr(analysis_tools, "find_bottlenecks", find_bottlenecks, raising=False)

    code, report = _main(["check-target", "--repo", REPO, "--workspace", str(tmp_path)], get=_ready_history().get)

    assert code == 0, report
    assert calls == [("InMemoryLedger", REPO, "ci.yml", str(tmp_path))]
    assert ct.WORKSPACE_ENV not in os.environ


def test_default_find_bottlenecks_restores_a_previous_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_find_bottlenecks(monkeypatch)
    monkeypatch.setenv(ct.WORKSPACE_ENV, "/previous")
    monkeypatch.setattr(analysis, "find_bottlenecks", lambda _ledger, _repo, _workflow: {}, raising=False)

    assert ct.default_find_bottlenecks()(REPO, "ci.yml") == {}
    assert os.environ[ct.WORKSPACE_ENV] == "/previous"


def test_cli_rejects_a_malformed_repo() -> None:
    with pytest.raises(SystemExit):
        cli.main(["check-target", "--repo", "not-a-repo"])

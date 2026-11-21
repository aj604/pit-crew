import base64
import gzip
import io
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email.message import Message
from http.client import HTTPMessage
from pathlib import Path
from typing import Any

import pytest

from pit_crew import tools
from pit_crew.lanes.analysis import history
from pit_crew.lanes.analysis.buckets import Buckets, BucketStats, bucket_stats, is_setup_step, job_buckets
from pit_crew.lanes.analysis.critical_path import critical_path, job_timings, max_saving, profile
from pit_crew.lanes.analysis.history import WorkflowRun, fetch_history, fetch_workflow, next_link, parse_runs
from pit_crew.lanes.analysis.stats import percentile
from pit_crew.lanes.analysis.tools import WorkflowNotFoundError
from pit_crew.lanes.analysis.workflow import Workflow, parse_workflow, topological_order
from pit_crew.ledger import InMemoryLedger
from pit_crew.managed_agent import build_request

FIXTURES = Path(__file__).parent / "fixtures"

Raw = dict[str, Any]

# lint ─────────────────────────────┐
# build ─┬─ test (ubuntu, macos) ───┼─ deploy
#        └─ docs ───────────────────┘
WORKFLOW = """
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
  build:
    runs-on: ubuntu-latest
  test:
    needs: build
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
  docs:
    needs: [build]
    runs-on: ubuntu-latest
  deploy:
    needs: [lint, test, docs]
    runs-on: ubuntu-latest
"""

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def job(  # noqa: PLR0913  mirrors the API's job fields
    name: str,
    created: float,
    started: float,
    completed: float,
    *,
    attempt: int = 1,
    steps: Sequence[tuple[str, float, str]] = (),
) -> Raw:
    return {
        "id": hash((name, created, attempt)),
        "name": name,
        "run_attempt": attempt,
        "conclusion": "success",
        "created_at": at(created),
        "started_at": at(started),
        "completed_at": at(completed),
        "labels": [],
        "steps": [
            {"number": i, "name": n, "conclusion": c, "started_at": at(0), "completed_at": at(s)}
            for i, (n, s, c) in enumerate(steps, 1)
        ],
    }


def run(
    run_id: int = 1,
    conclusion: str = "success",
    attempt: int = 1,
    scale: float = 1.0,
    extra_jobs: Sequence[Raw] = (),
) -> Raw:
    """build 0-100, test on ubuntu 100-300 and on macos queued 100-150 then 150-400, docs 100-150,
    lint 0-60, deploy 400-410. Critical path: build -> test -> deploy = 410s."""
    s = scale
    return {
        "id": run_id,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "run_started_at": at(0),
        "jobs": [
            job("lint", 0, 0, 60 * s, attempt=attempt),
            job(
                "build",
                0,
                0,
                100 * s,
                attempt=attempt,
                steps=[("Install", 70 * s, "success"), ("Lint fix", 0, "skipped")],
            ),
            job("test (ubuntu-latest)", 100 * s, 100 * s, 300 * s, attempt=attempt),
            job("test (macos-latest)", 100 * s, 150 * s, 400 * s, attempt=attempt),
            job("docs", 100 * s, 100 * s, 150 * s, attempt=attempt),
            job("deploy", 400 * s, 400 * s, 410 * s, attempt=attempt),
            *extra_jobs,
        ],
    }


@pytest.fixture
def workflow() -> Workflow:
    return parse_workflow(WORKFLOW)


# workflow.py


def test_display_names_map_to_job_ids(workflow: Workflow) -> None:
    assert workflow.job_id_for("test (macos-latest)") == "test"
    assert workflow.job_id_for("build") == "build"
    assert workflow.job_id_for("nope") is None


def test_named_and_reusable_jobs_map_to_job_ids() -> None:
    wf = parse_workflow("""
jobs:
  unit:
    name: Unit ${{ matrix.py }}
    strategy: {matrix: {py: ["3.12"]}}
  release:
    uses: ./.github/workflows/release.yml
  test:
    strategy: {matrix: {os: [a]}}
  test-alls-green:
    needs: test
""")
    assert wf.job_id_for("Unit 3.12") == "unit"
    assert wf.job_id_for("release / publish") == "release"
    assert wf.job_id_for("test-alls-green") == "test-alls-green"  # not swallowed by the matrix job "test"


def test_needs_as_string_or_list(workflow: Workflow) -> None:
    assert workflow.jobs["test"].needs == ("build",)
    assert workflow.jobs["deploy"].needs == ("lint", "test", "docs")


def test_unknown_need_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown"):
        parse_workflow("jobs:\n  a:\n    needs: b\n")


def test_needs_cycle_is_rejected() -> None:
    wf = parse_workflow("jobs:\n  a:\n    needs: b\n  b:\n    needs: a\n")
    with pytest.raises(ValueError, match="cycle"):
        topological_order(wf)


# critical_path.py


def test_critical_path_follows_the_longest_needs_chain(workflow: Workflow) -> None:
    path = critical_path(workflow, {"lint": 60, "build": 100, "test": 300, "docs": 50, "deploy": 10})
    assert path.jobs == ("build", "test", "deploy")
    assert path.seconds == 410


def test_max_saving_is_zero_off_the_critical_path_and_capped_by_the_next_longest_chain(workflow: Workflow) -> None:
    seconds = {"lint": 60, "build": 100, "test": 300, "docs": 50, "deploy": 10}
    assert max_saving(workflow, seconds, "docs") == 0
    assert max_saving(workflow, seconds, "lint") == 0
    # Without test, build -> docs -> deploy (160s) becomes the critical path.
    assert max_saving(workflow, seconds, "test") == 410 - 160


def test_matrix_instances_fold_into_one_job_from_first_queued_to_last_done(workflow: Workflow) -> None:
    timings, unmatched = job_timings(parse_runs([run()])[0], workflow)
    assert unmatched == []
    test = timings["test"]
    assert test.seconds == 300  # 100 -> 400
    assert test.run_seconds == 250  # macos ran 150 -> 400
    assert test.queued_seconds == 50
    assert test.slowest_instance == "test (macos-latest)"
    assert test.instances == 2


def test_profile_ranks_jobs_by_wall_clock_saving(workflow: Workflow) -> None:
    p = profile(workflow, parse_runs([run(1), run(2), run(3, scale=2.0)]))
    assert p.critical_path.jobs == ("build", "test", "deploy")
    assert [j.job_id for j in p.jobs][:3] == ["test", "build", "deploy"]
    by_id = {j.job_id: j for j in p.jobs}
    assert by_id["test"].critical_runs == 3
    assert by_id["docs"].max_saving_seconds == 0
    assert by_id["test"].straggler == "test (macos-latest)"
    assert p.wall_clock_median_seconds == 410
    assert p.wall_clock_p95_seconds == 820


def test_profile_leaves_out_failed_runs_and_counts_retries(workflow: Workflow) -> None:
    p = profile(workflow, parse_runs([run(1), run(2, conclusion="failure"), run(3, attempt=2)]))
    assert p.runs_analyzed == 2
    assert p.runs_excluded == {"failure": 1}
    assert p.retried_runs == 1


def test_runs_of_an_older_workflow_version_are_left_out(workflow: Workflow) -> None:
    old = run(2)
    old["jobs"] = [job("check", 0, 0, 30)]  # the workflow used to be a single job
    p = profile(workflow, parse_runs([run(1), old]))
    assert p.runs_analyzed == 1
    assert p.runs_excluded == {"workflow_changed": 1}
    assert p.unmatched_jobs == ("check",)
    assert p.wall_clock_median_seconds == 410


def test_only_the_final_attempt_counts(workflow: Workflow) -> None:
    stale = job("deploy", 0, 0, 5000, attempt=1)  # first attempt; superseded
    raw = run(attempt=2, extra_jobs=[stale])
    p = profile(workflow, parse_runs([raw]))
    assert p.wall_clock_median_seconds == 410


def test_step_profiles_skip_skipped_steps_and_fold_shas(workflow: Workflow) -> None:
    sha_step = job("lint", 0, 0, 60, steps=[(f"Coverage for {'b' * 40}", 30, "success")])
    other = job("lint", 0, 0, 60, steps=[(f"Coverage for {'c' * 40}", 40, "success")])
    p = profile(workflow, parse_runs([run(1, extra_jobs=[sha_step]), run(2, extra_jobs=[other])]))
    names = {(s.job_id, s.name): s for s in p.steps}
    assert ("build", "Lint fix") not in names
    assert names[("lint", "Coverage for <sha>")].samples == 2
    assert names[("build", "Install")].median_seconds == 70


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 95) == 0
    assert percentile([5.0], 95) == 5
    assert percentile([float(v) for v in range(1, 21)], 95) == 19
    assert percentile([float(v) for v in range(1, 21)], 50) == 10


# buckets.py


@pytest.mark.parametrize(
    "name",
    [
        "Set up job",
        "Complete job",
        "Post Run actions/checkout@v4",
        "Run actions/checkout@v4",
        "Check out the base revision",
        "Setup uv",
        "Set up Python",
        "Run actions/setup-node@v4",
        "Install Dependencies",
        "Uninstall httpx2 to run tests with httpx",
        "Run npm ci",
        "Run uv sync --locked",
        "Run actions/cache@v4",
        "Restore build cache",
        "Run go mod download",
    ],
)
def test_setup_steps(name: str) -> None:
    assert is_setup_step(name)


@pytest.mark.parametrize(
    "name",
    [
        "Test",
        "Run uv run pytest",
        "Run npm test",
        "CodSpeed benchmarks",
        "Run uv run ruff check",
        "Store coverage files",
    ],
)
def test_check_steps_are_running(name: str) -> None:
    assert not is_setup_step(name)


def test_job_buckets_count_queue_as_waiting_and_unstepped_runner_time_as_setup() -> None:
    raw = run(
        extra_jobs=[
            job("lint", 0, 20, 120, steps=[("Run actions/checkout@v4", 10, "success"), ("Lint", 80, "success")])
        ]
    )
    lint = next(j for j in parse_runs([raw])[0].jobs if j.started_at and j.queued_seconds == 20)
    assert job_buckets(lint) == Buckets(waiting=20, setup=10 + 10, running=80)


def test_bucket_stats_and_biggest_bucket() -> None:
    stats = bucket_stats([Buckets(1, 10, 5), Buckets(3, 30, 5), Buckets(2, 20, 50)])
    assert stats == BucketStats(2, 3, 20, 30, 5, 50)
    assert stats.biggest(50) == "setup"
    assert stats.biggest(95) == "running"
    assert bucket_stats([]).biggest() == "waiting"  # all zero: ties go to the earlier bucket


def test_profile_splits_the_critical_path_into_buckets(workflow: Workflow) -> None:
    p = profile(workflow, parse_runs([run(1), run(2)]))
    # build (setup: 70s install + 30s unstepped) -> test macos (50s queued, 250s unstepped) -> deploy (10s unstepped)
    assert p.critical_path_buckets == BucketStats(50, 50, 360, 360, 0, 0)
    assert p.biggest_bucket == "setup"
    assert p.biggest_bucket_p95 == "setup"
    assert {j.job_id: j.buckets.waiting_p95 for j in p.jobs}["test"] == 50


# history.py


def test_fetch_follows_pages_trims_fields_and_reads_every_attempt() -> None:
    def page(ids: list[int], next_url: str | None = None) -> tuple[Raw, str | None]:
        return {
            "workflow_runs": [{"id": i, "conclusion": "success", "run_attempt": 1, "secret": "x"} for i in ids]
        }, next_url

    second = "https://api.github.com/repos/o/r/actions/workflows/ci.yml/runs?page=2"
    requested: list[str] = []

    def get(url: str) -> tuple[Raw, str | None]:
        requested.append(url)
        if url == second:
            return page([3, 4])
        if "/workflows/" in url:
            return page([1, 2], second)
        run_id = int(url.split("/runs/")[1].split("/", maxsplit=1)[0])
        jobs = [{"id": run_id * 10, "name": "build", "run_attempt": 1, "steps": [{"number": 1, "name": "s", "x": 1}]}]
        return {"jobs": jobs}, None

    raw = fetch_history("o/r", "ci.yml", runs=3, branch="main", get=get)
    assert [r["id"] for r in raw] == [1, 2, 3]
    assert [r["jobs"][0]["id"] for r in raw] == [10, 20, 30]  # job lists fetched in parallel stay with their run
    assert "secret" not in raw[0]
    assert set(raw[0]["jobs"][0]["steps"][0]) == {
        "number",
        "name",
        "status",
        "conclusion",
        "started_at",
        "completed_at",
    }
    assert "branch=main" in requested[0]
    assert "status=completed" in requested[0]
    assert all("filter=all" in u for u in requested if "/jobs" in u)


def test_next_link_is_read_from_the_link_header() -> None:
    header = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=9>; rel="last"'
    assert next_link(header) == "https://api.github.com/x?page=2"
    assert next_link(None) is None


def test_http_getter_only_sends_the_token_to_the_github_api() -> None:
    get = history._http_getter("secret-token")  # noqa: SLF001
    with pytest.raises(ValueError, match="refusing"):
        get("https://evil.example/repos/o/r")


def test_redirect_handler_refuses_a_foreign_host_and_follows_the_api() -> None:
    handler = history._NoForeignRedirect()  # noqa: SLF001
    request = urllib.request.Request(  # noqa: S310  https-only
        f"{history.API}/repos/o/r/actions/runs", headers={"Authorization": "Bearer t"}
    )
    headers = HTTPMessage()
    with pytest.raises(ValueError, match="refusing to follow"):
        handler.redirect_request(request, io.BytesIO(), 302, "Found", headers, "https://other.example/x")
    followed = handler.redirect_request(request, io.BytesIO(), 302, "Found", headers, f"{history.API}/other")
    assert followed is not None
    assert followed.full_url == f"{history.API}/other"


# Real history: FastAPI's test.yml, 12 pushes to master (Nov 2026).


def fastapi_raw() -> list[Raw]:
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as f:
        raw: list[Raw] = json.load(f)
    return raw


@pytest.fixture
def fastapi() -> tuple[Workflow, list[WorkflowRun]]:
    return parse_workflow((FIXTURES / "fastapi" / "test.yml").read_text()), parse_runs(fastapi_raw())


def test_fastapi_critical_path_matches_measured_wall_clock(fastapi: tuple[Workflow, list[WorkflowRun]]) -> None:
    wf, runs = fastapi
    for r in runs:
        if r.conclusion == "success":
            timings, unmatched = job_timings(r, wf)
            assert unmatched == []
            path = critical_path(wf, {j: t.seconds for j, t in timings.items()})
            assert path.jobs == ("changes", "test", "coverage-combine", "test-alls-green")
            assert abs(path.seconds - r.wall_clock_seconds) < 10


def test_fastapi_profile(fastapi: tuple[Workflow, list[WorkflowRun]]) -> None:
    p = profile(*fastapi)
    assert p.runs_analyzed == 10
    assert p.runs_excluded == {"failure": 1, "cancelled": 1}
    assert p.retried_runs == 1
    by_id = {j.job_id: j for j in p.jobs}
    assert by_id["test"].instances == 17
    assert by_id["benchmark"].max_saving_seconds == 0  # runs beside test, never on the critical path
    assert by_id["regression-test"].max_saving_seconds == 0
    # changes runs for seconds but waited ~20 minutes for a runner in the Nov 16 merge burst.
    changes = by_id["changes"].buckets
    assert changes.setup_p50 + changes.running_p50 < 30 < changes.waiting_p50
    # Across all runs the merge burst makes waiting the bucket to fix first.
    assert p.biggest_bucket == "waiting"
    assert p.biggest_bucket_p95 == "waiting"


def test_fastapi_without_the_merge_burst_is_dominated_by_running(
    fastapi: tuple[Workflow, list[WorkflowRun]],
) -> None:
    wf, runs = fastapi
    calm = [r for r in runs if r.run_started_at and r.run_started_at.day != 16]
    p = profile(wf, calm)
    assert p.runs_analyzed == 4
    assert p.biggest_bucket == "running"
    assert p.biggest_bucket_p95 == "waiting"  # even calm runs sometimes queue for minutes
    assert p.critical_path_buckets.setup_p50 > 60  # macOS test instances spend minutes on setup


def test_fastapi_critical_path_buckets_add_up_to_wall_clock(fastapi: tuple[Workflow, list[WorkflowRun]]) -> None:
    wf, runs = fastapi
    for r in runs:
        if r.conclusion == "success":
            timings, _ = job_timings(r, wf)
            path = critical_path(wf, {j: t.seconds for j, t in timings.items()})
            total = sum((timings[j].buckets for j in path.jobs), Buckets(0, 0, 0))
            assert abs(total.waiting + total.setup + total.running - r.wall_clock_seconds) < 10


# tools.py


def test_profile_ci_tool_reads_the_workspace_workflow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "test.yml").write_text((FIXTURES / "fastapi" / "test.yml").read_text())
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    raw = fastapi_raw()
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)
    blob = history.workflow_blob_sha((FIXTURES / "fastapi" / "test.yml").read_text())
    monkeypatch.setattr(history, "fetch_workflow_blob_shas", lambda _r, _p, refs, **_k: dict.fromkeys(refs, blob))

    build_request("analysis", "")
    result = tools.call("profile_ci", InMemoryLedger(), {"repo": "fastapi/fastapi", "workflow": "test.yml"})

    json.dumps(result)  # must be JSON-serializable for the agent
    assert result["critical_path"]["jobs"] == ["changes", "test", "coverage-combine", "test-alls-green"]
    assert len(result["steps"]) == 15


@pytest.mark.parametrize(
    "args",
    [
        {"repo": "o/r", "workflow": "../../etc/passwd"},
        {"repo": "o/r", "workflow": "sub/ci.yml"},
        {"repo": "o/r", "workflow": "..yml"},
        {"repo": "o/r/../../user", "workflow": "ci.yml"},
        {"repo": "o/..", "workflow": "ci.yml"},
        {"repo": "o", "workflow": "ci.yml"},
    ],
)
def test_profile_ci_rejects_paths_outside_the_workflows_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: dict[str, str]
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: pytest.fail("must not fetch"))
    build_request("analysis", "")
    with pytest.raises(ValueError, match="must be"):
        tools.call("profile_ci", InMemoryLedger(), args)


def test_profile_ci_rejects_a_workflow_symlinked_out_of_the_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "secret.yml").write_text("jobs: {}")
    (tmp_path / ".github" / "workflows" / "ci.yml").symlink_to(tmp_path / "secret.yml")
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    build_request("analysis", "")
    with pytest.raises(ValueError, match="outside"):
        tools.call("profile_ci", InMemoryLedger(), {"repo": "o/r", "workflow": "ci.yml"})


def test_profile_ci_accepts_workflow_content_without_host_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    raw = fastapi_raw()
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(history, "fetch_workflow", lambda *_args, **_kwargs: pytest.fail("must not fetch"))
    _match_every_run(monkeypatch, (FIXTURES / "fastapi" / "test.yml").read_text())

    build_request("analysis", "")
    result = tools.call(
        "profile_ci",
        InMemoryLedger(),
        {
            "repo": "fastapi/fastapi",
            "workflow": "test.yml",
            "workflow_content": (FIXTURES / "fastapi" / "test.yml").read_text(),
        },
    )
    assert result["critical_path"]["jobs"] == ["changes", "test", "coverage-combine", "test-alls-green"]


def test_profile_ci_accepts_explicit_ref_without_host_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    raw = fastapi_raw()
    workflow_text = (FIXTURES / "fastapi" / "test.yml").read_text()
    fetched: list[tuple[str, str, str | None]] = []

    def fake_fetch_workflow(repo: str, workflow: str, *, ref: str | None = None) -> str:
        fetched.append((repo, workflow, ref))
        return workflow_text

    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(history, "fetch_workflow", fake_fetch_workflow)
    _match_every_run(monkeypatch, workflow_text)

    build_request("analysis", "")
    result = tools.call(
        "profile_ci",
        InMemoryLedger(),
        {"repo": "fastapi/fastapi", "workflow": "test.yml", "ref": "deadbeef"},
    )
    assert fetched == [("fastapi/fastapi", "test.yml", "deadbeef")]
    assert result["critical_path"]["jobs"] == ["changes", "test", "coverage-combine", "test-alls-green"]


def test_fetch_workflow_decodes_github_contents_response() -> None:
    yaml_text = "name: CI\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    encoded = base64.encodebytes(yaml_text.encode("utf-8")).decode("ascii")
    seen: list[str] = []

    def fake_get(url: str) -> tuple[Any, str | None]:
        seen.append(url)
        return {"type": "file", "encoding": "base64", "content": encoded}, None

    assert fetch_workflow("o/r", "ci.yml", get=fake_get) == yaml_text
    assert fetch_workflow("o/r", "ci.yml", ref="feat/x", get=fake_get) == yaml_text
    assert seen == [
        "https://api.github.com/repos/o/r/contents/.github/workflows/ci.yml",
        "https://api.github.com/repos/o/r/contents/.github/workflows/ci.yml?ref=feat%2Fx",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"type": "dir", "encoding": "base64", "content": ""},
        {"type": "file", "encoding": "none", "content": ""},
        {"type": "file", "encoding": "base64", "content": None},
    ],
)
def test_fetch_workflow_rejects_unexpected_github_payloads(payload: object) -> None:
    with pytest.raises(ValueError, match="unexpected GitHub contents response"):
        fetch_workflow("o/r", "ci.yml", get=lambda _url: (payload, None))


def test_profile_ci_fetches_workflow_from_github_when_workspace_has_no_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path / "missing"))
    yaml_text = (FIXTURES / "fastapi" / "test.yml").read_text()
    raw = fastapi_raw()
    fetched: list[tuple[str, str, str | None]] = []

    def fake_fetch_workflow(repo: str, workflow: str, *, ref: str | None = None) -> str:
        fetched.append((repo, workflow, ref))
        return yaml_text

    monkeypatch.setattr(history, "fetch_workflow", fake_fetch_workflow)
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)
    _match_every_run(monkeypatch, yaml_text)

    build_request("analysis", "")
    result = tools.call(
        "profile_ci", InMemoryLedger(), {"repo": "fastapi/fastapi", "workflow": "test.yml", "branch": "master"}
    )

    assert fetched == [("fastapi/fastapi", "test.yml", "master")]
    assert result["critical_path"]["jobs"] == ["changes", "test", "coverage-combine", "test-alls-green"]


def _match_every_run(monkeypatch: pytest.MonkeyPatch, yaml_text: str) -> None:
    """Make every run's remote workflow blob equal `yaml_text`, so revision matching keeps all runs."""
    blob = history.workflow_blob_sha(yaml_text)
    monkeypatch.setattr(history, "fetch_workflow_blob_shas", lambda _r, _p, refs, **_k: dict.fromkeys(refs, blob))


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com/repos/o/r", code, "err", Message(), None)


def test_profile_ci_resolves_deleted_branch_workflow_from_run_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path / "missing"))
    yaml_text = (FIXTURES / "fastapi" / "test.yml").read_text()
    raw = fastapi_raw()
    expected_sha = next(r["head_sha"] for r in raw if r.get("head_sha"))
    refs_tried: list[str | None] = []

    def fake_fetch_workflow(_repo: str, _workflow: str, *, ref: str | None = None) -> str:
        refs_tried.append(ref)
        if ref == "release/1.2":
            raise _http_error(404)
        assert ref == expected_sha
        return yaml_text

    monkeypatch.setattr(history, "fetch_workflow", fake_fetch_workflow)
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)
    _match_every_run(monkeypatch, yaml_text)

    build_request("analysis", "")
    result = tools.call(
        "profile_ci", InMemoryLedger(), {"repo": "fastapi/fastapi", "workflow": "test.yml", "branch": "release/1.2"}
    )

    assert refs_tried == ["release/1.2", expected_sha]
    assert result["critical_path"]["jobs"] == ["changes", "test", "coverage-combine", "test-alls-green"]


def test_profile_ci_errors_explicitly_when_deleted_branch_has_no_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path / "missing"))
    monkeypatch.setattr(history, "fetch_workflow", lambda *_args, **_kwargs: (_ for _ in ()).throw(_http_error(404)))
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: [])

    build_request("analysis", "")
    with pytest.raises(WorkflowNotFoundError, match="no completed runs record a commit"):
        tools.call("profile_ci", InMemoryLedger(), {"repo": "o/r", "workflow": "ci.yml", "branch": "release/1.2"})


def test_profile_ci_errors_explicitly_when_deleted_branch_commit_is_also_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path / "missing"))
    monkeypatch.setattr(history, "fetch_workflow", lambda *_args, **_kwargs: (_ for _ in ()).throw(_http_error(404)))
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: [{"head_sha": "deadbeef"}])

    build_request("analysis", "")
    with pytest.raises(WorkflowNotFoundError, match="deadbeef"):
        tools.call("profile_ci", InMemoryLedger(), {"repo": "o/r", "workflow": "ci.yml", "branch": "release/1.2"})


@pytest.mark.parametrize(
    ("branch", "first_code", "second_code"),
    [
        (None, 404, None),
        ("release/1.2", 403, None),
        ("release/1.2", 404, 403),
    ],
)
def test_profile_ci_propagates_non_fallback_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    branch: str | None,
    first_code: int,
    second_code: int | None,
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path / "missing"))
    codes = [first_code] if second_code is None else [first_code, second_code]

    def fake_fetch_workflow(*_args: object, **_kwargs: object) -> str:
        raise _http_error(codes.pop(0))

    monkeypatch.setattr(history, "fetch_workflow", fake_fetch_workflow)
    monkeypatch.setattr(
        history,
        "fetch_history",
        (lambda *_args, **_kwargs: [{"head_sha": "deadbeef"}])
        if second_code is not None
        else (lambda *_args, **_kwargs: []),
    )

    build_request("analysis", "")
    args: dict[str, Any] = {"repo": "o/r", "workflow": "ci.yml"}
    if branch is not None:
        args["branch"] = branch
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        tools.call("profile_ci", InMemoryLedger(), args)
    assert exc_info.value.code == (second_code if second_code is not None else first_code)

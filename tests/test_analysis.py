import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pit_crew import tools
from pit_crew.lanes.analysis import history
from pit_crew.lanes.analysis.critical_path import critical_path, job_timings, max_saving, percentile, profile
from pit_crew.lanes.analysis.history import fetch_history, parse_runs
from pit_crew.lanes.analysis.workflow import parse_workflow, topological_order
from pit_crew.ledger import InMemoryLedger
from pit_crew.managed_agent import build_request

FIXTURES = Path(__file__).parent / "fixtures"

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


def job(name, created, started, completed, attempt=1, steps=()):
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


def run(run_id=1, conclusion="success", attempt=1, scale=1.0, extra_jobs=()):
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
            job("lint", 0, 0, 60 * s),
            job("build", 0, 0, 100 * s, steps=[("Install", 70 * s, "success"), ("Lint fix", 0, "skipped")]),
            job("test (ubuntu-latest)", 100 * s, 100 * s, 300 * s),
            job("test (macos-latest)", 100 * s, 150 * s, 400 * s),
            job("docs", 100 * s, 100 * s, 150 * s),
            job("deploy", 400 * s, 400 * s, 410 * s),
            *extra_jobs,
        ],
    }


@pytest.fixture
def workflow():
    return parse_workflow(WORKFLOW)


# workflow.py


def test_display_names_map_to_job_ids(workflow):
    assert workflow.job_id_for("test (macos-latest)") == "test"
    assert workflow.job_id_for("build") == "build"
    assert workflow.job_id_for("nope") is None


def test_named_and_reusable_jobs_map_to_job_ids():
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


def test_needs_as_string_or_list(workflow):
    assert workflow.jobs["test"].needs == ("build",)
    assert workflow.jobs["deploy"].needs == ("lint", "test", "docs")


def test_unknown_need_is_rejected():
    with pytest.raises(ValueError, match="unknown"):
        parse_workflow("jobs:\n  a:\n    needs: b\n")


def test_needs_cycle_is_rejected():
    wf = parse_workflow("jobs:\n  a:\n    needs: b\n  b:\n    needs: a\n")
    with pytest.raises(ValueError, match="cycle"):
        topological_order(wf)


# critical_path.py


def test_critical_path_follows_the_longest_needs_chain(workflow):
    path = critical_path(workflow, {"lint": 60, "build": 100, "test": 300, "docs": 50, "deploy": 10})
    assert path.jobs == ("build", "test", "deploy")
    assert path.seconds == 410


def test_max_saving_is_zero_off_the_critical_path_and_capped_by_the_next_longest_chain(workflow):
    seconds = {"lint": 60, "build": 100, "test": 300, "docs": 50, "deploy": 10}
    assert max_saving(workflow, seconds, "docs") == 0
    assert max_saving(workflow, seconds, "lint") == 0
    # Without test, build -> docs -> deploy (160s) becomes the critical path.
    assert max_saving(workflow, seconds, "test") == 410 - 160


def test_matrix_instances_fold_into_one_job_from_first_queued_to_last_done(workflow):
    timings, unmatched = job_timings(parse_runs([run()])[0], workflow)
    assert unmatched == []
    test = timings["test"]
    assert test.seconds == 300  # 100 -> 400
    assert test.run_seconds == 250  # macos ran 150 -> 400
    assert test.queued_seconds == 50
    assert test.slowest_instance == "test (macos-latest)"
    assert test.instances == 2


def test_profile_ranks_jobs_by_wall_clock_saving(workflow):
    p = profile(workflow, parse_runs([run(1), run(2), run(3, scale=2.0)]))
    assert p.critical_path.jobs == ("build", "test", "deploy")
    assert [j.job_id for j in p.jobs][:3] == ["test", "build", "deploy"]
    by_id = {j.job_id: j for j in p.jobs}
    assert by_id["test"].critical_runs == 3
    assert by_id["docs"].max_saving_seconds == 0
    assert by_id["test"].straggler == "test (macos-latest)"
    assert p.wall_clock_median_seconds == 410
    assert p.wall_clock_p95_seconds == 820


def test_profile_leaves_out_failed_runs_and_counts_retries(workflow):
    p = profile(workflow, parse_runs([run(1), run(2, conclusion="failure"), run(3, attempt=2)]))
    assert p.runs_analyzed == 2
    assert p.runs_excluded == {"failure": 1}
    assert p.retried_runs == 1


def test_only_the_final_attempt_counts(workflow):
    stale = job("deploy", 0, 0, 5000, attempt=1)  # first attempt; superseded
    raw = run(attempt=2, extra_jobs=[stale])
    for j in raw["jobs"]:
        if j is not stale:
            j["run_attempt"] = 2
    p = profile(workflow, parse_runs([raw]))
    assert p.wall_clock_median_seconds == 410


def test_step_profiles_skip_skipped_steps_and_fold_shas(workflow):
    sha_step = job("lint", 0, 0, 60, steps=[(f"Coverage for {'b' * 40}", 30, "success")])
    other = job("lint", 0, 0, 60, steps=[(f"Coverage for {'c' * 40}", 40, "success")])
    p = profile(workflow, parse_runs([run(1, extra_jobs=[sha_step]), run(2, extra_jobs=[other])]))
    names = {(s.job_id, s.name): s for s in p.steps}
    assert ("build", "Lint fix") not in names
    assert names[("lint", "Coverage for <sha>")].samples == 2
    assert names[("build", "Install")].median_seconds == 70


def test_percentile_is_nearest_rank():
    assert percentile([], 95) == 0
    assert percentile([5.0], 95) == 5
    assert percentile([float(v) for v in range(1, 21)], 95) == 19
    assert percentile([float(v) for v in range(1, 21)], 50) == 10


# history.py


def test_fetch_follows_pages_trims_fields_and_reads_every_attempt():
    def page(ids, next_url=None):
        return {
            "workflow_runs": [{"id": i, "conclusion": "success", "run_attempt": 1, "secret": "x"} for i in ids]
        }, next_url

    second = "https://api.github.com/repos/o/r/actions/workflows/ci.yml/runs?page=2"
    requested = []

    def get(url):
        requested.append(url)
        if url == second:
            return page([3, 4])
        if "/workflows/" in url:
            return page([1, 2], second)
        run_id = int(url.split("/runs/")[1].split("/")[0])
        jobs = [{"id": run_id * 10, "name": "build", "run_attempt": 1, "steps": [{"number": 1, "name": "s", "x": 1}]}]
        return {"jobs": jobs}, None

    raw = fetch_history("o/r", "ci.yml", runs=3, branch="main", get=get)
    assert [r["id"] for r in raw] == [1, 2, 3]
    assert "secret" not in raw[0]
    assert set(raw[0]["jobs"][0]["steps"][0]) == {
        "number",
        "name",
        "status",
        "conclusion",
        "started_at",
        "completed_at",
    }
    assert "branch=main" in requested[0] and "status=completed" in requested[0]
    assert all("filter=all" in u for u in requested if "/jobs" in u)


def test_next_link_is_read_from_the_link_header():
    header = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=9>; rel="last"'
    assert history._next_link(header) == "https://api.github.com/x?page=2"
    assert history._next_link(None) is None


# Real history: FastAPI's test.yml, 12 pushes to master (Nov 2026).


def fastapi_raw():
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as f:
        return json.load(f)


@pytest.fixture
def fastapi():
    return parse_workflow((FIXTURES / "fastapi" / "test.yml").read_text()), parse_runs(fastapi_raw())


def test_fastapi_critical_path_matches_measured_wall_clock(fastapi):
    wf, runs = fastapi
    for r in runs:
        if r.conclusion == "success":
            timings, unmatched = job_timings(r, wf)
            assert unmatched == []
            path = critical_path(wf, {j: t.seconds for j, t in timings.items()})
            assert path.jobs == ("changes", "test", "coverage-combine", "test-alls-green")
            assert abs(path.seconds - r.wall_clock_seconds) < 10


def test_fastapi_profile(fastapi):
    p = profile(*fastapi)
    assert p.runs_analyzed == 10
    assert p.runs_excluded == {"failure": 1, "cancelled": 1}
    assert p.retried_runs == 1
    by_id = {j.job_id: j for j in p.jobs}
    assert by_id["test"].instances == 17
    assert by_id["benchmark"].max_saving_seconds == 0  # runs beside test, never on the critical path
    assert by_id["regression-test"].max_saving_seconds == 0
    # changes runs for seconds but waited ~20 minutes for a runner in the Nov 16 merge burst.
    assert by_id["changes"].median_run_seconds < 30 < by_id["changes"].median_queued_seconds


# tools.py


def test_profile_ci_tool_reads_the_workspace_workflow(monkeypatch, tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "test.yml").write_text((FIXTURES / "fastapi" / "test.yml").read_text())
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    raw = fastapi_raw()
    monkeypatch.setattr(history, "fetch_history", lambda repo, workflow, runs, branch: raw)

    build_request("analysis", "")
    result = tools.call("profile_ci", InMemoryLedger(), {"repo": "fastapi/fastapi", "workflow": "test.yml"})

    json.dumps(result)  # must be JSON-serializable for the agent
    assert result["critical_path"]["jobs"] == ["changes", "test", "coverage-combine", "test-alls-green"]
    assert len(result["steps"]) == 15

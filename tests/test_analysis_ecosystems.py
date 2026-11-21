"""Analysis against real run history from a JavaScript project (Vite) and a Rust project (Tokio).

The FastAPI fixture only covers Python. These check that job-name matching, critical-path timing
and setup-step classification also hold for pnpm/Node and cargo workflows. Offline: the tests
read the committed fixtures and never call the network.

Fixtures (workflow file taken at the newest run's head_sha; push runs on the default branch):
- vite:  vitejs/vite, ci.yml, branch main, 20 runs (10 successful), pnpm
- tokio: tokio-rs/tokio, ci.yml, branch master, 10 runs (9 successful), cargo
"""

import gzip
import json
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from pit_crew.lanes.analysis.buckets import is_setup_step
from pit_crew.lanes.analysis.critical_path import critical_path, job_timings
from pit_crew.lanes.analysis.history import WorkflowRun, parse_runs
from pit_crew.lanes.analysis.workflow import Workflow, parse_workflow

FIXTURES = Path(__file__).parent / "fixtures"

INSTALL_MARKERS = ("npm ci", "pnpm install", "yarn install", "cargo fetch", "Install")


class Ecosystem(NamedTuple):
    slug: str
    tolerance_seconds: float  # allowed gap between critical path and measured wall clock


# Tokio's wasm32-wasip1 job is a 15-way matrix; the critical path adds the slowest instance's run
# time to the longest queue wait of any instance, which overshoots wall clock by 13 s in one run.
ECOSYSTEMS = [Ecosystem("vite", 10), Ecosystem("tokio", 15)]
IDS = [e.slug for e in ECOSYSTEMS]


def load(slug: str) -> tuple[Workflow, list[WorkflowRun]]:
    with gzip.open(FIXTURES / slug / "runs.json.gz") as f:
        raw: list[dict[str, Any]] = json.load(f)
    return parse_workflow((FIXTURES / slug / "ci.yml").read_text()), parse_runs(raw)


def successful(runs: list[WorkflowRun]) -> list[WorkflowRun]:
    done = [r for r in runs if r.conclusion == "success"]
    assert done, "fixture has no successful runs"
    return done


@pytest.mark.parametrize("eco", ECOSYSTEMS, ids=IDS)
def test_jobs_all_match(eco: Ecosystem) -> None:
    wf, runs = load(eco.slug)
    for r in successful(runs):
        timings, unmatched = job_timings(r, wf)
        assert unmatched == [], f"run {r.id}"
        assert timings


@pytest.mark.parametrize("eco", ECOSYSTEMS, ids=IDS)
def test_critical_path_matches_wall_clock(eco: Ecosystem) -> None:
    wf, runs = load(eco.slug)
    for r in successful(runs):
        timings, _ = job_timings(r, wf)
        path = critical_path(wf, {j: t.seconds for j, t in timings.items()})
        assert abs(path.seconds - r.wall_clock_seconds) < eco.tolerance_seconds, f"run {r.id}"


@pytest.mark.parametrize("eco", ECOSYSTEMS, ids=IDS)
def test_install_steps_are_setup(eco: Ecosystem) -> None:
    _, runs = load(eco.slug)
    seen = {
        s.name for r in runs for j in r.jobs for s in j.steps if any(marker in s.name for marker in INSTALL_MARKERS)
    }
    assert seen, "fixture has no install steps"
    assert [n for n in sorted(seen) if not is_setup_step(n)] == []


def test_vite_critical_path_runs_through_test_matrix() -> None:
    wf, runs = load("vite")
    paths = set()
    for r in successful(runs):
        timings, _ = job_timings(r, wf)
        paths.add(critical_path(wf, {j: t.seconds for j, t in timings.items()}).jobs)
    assert ("changed", "test", "test-passed") in paths


def test_tokio_critical_path_goes_through_basics() -> None:
    wf, runs = load("tokio")
    for r in successful(runs):
        timings, _ = job_timings(r, wf)
        assert critical_path(wf, {j: t.seconds for j, t in timings.items()}).jobs[:2] == ("clippy", "basics")

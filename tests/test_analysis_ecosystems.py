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
    known_drift: frozenset[str]  # job names the newest workflow no longer produces, see test_jobs_all_match
    min_clean_runs: int  # successful runs with every job matched, the ones profile() keeps


ECOSYSTEMS = [
    Ecosystem("vite", frozenset(), 10),
    Ecosystem("tokio", frozenset({"test tokio for wasm32-wasip2"}), 4),
]
IDS = [e.slug for e in ECOSYSTEMS]
TOLERANCE_SECONDS = 10


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
    """Every job name matches a job id, except known drift.

    The workflow file is the newest one, but older captured runs ran older versions: tokio renamed
    "test tokio for wasm32-wasip2" afterwards, so older runs report a name the file no longer has.
    Each allowed name must really occur unmatched, so the allowance cannot go stale silently.
    """
    wf, runs = load(eco.slug)
    unmatched_seen: set[str] = set()
    for r in successful(runs):
        timings, unmatched = job_timings(r, wf)
        assert timings
        unmatched_seen.update(unmatched)
    assert unmatched_seen <= eco.known_drift
    assert eco.known_drift <= unmatched_seen


def clean_runs(wf: Workflow, runs: list[WorkflowRun]) -> list[WorkflowRun]:
    """Successful runs where every job matched."""
    return [r for r in successful(runs) if not job_timings(r, wf)[1]]


@pytest.mark.parametrize("eco", ECOSYSTEMS, ids=IDS)
def test_critical_path_matches_wall_clock(eco: Ecosystem) -> None:
    wf, runs = load(eco.slug)
    clean = clean_runs(wf, runs)
    assert len(clean) >= eco.min_clean_runs
    for r in clean:
        timings, _ = job_timings(r, wf)
        path = critical_path(wf, {j: t.seconds for j, t in timings.items()})
        assert abs(path.seconds - r.wall_clock_seconds) < TOLERANCE_SECONDS, f"run {r.id}"


@pytest.mark.parametrize("eco", ECOSYSTEMS, ids=IDS)
def test_install_steps_are_setup(eco: Ecosystem) -> None:
    """Reads steps from all runs, failed ones included."""
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


def test_matrix_job_instances_are_not_swallowed() -> None:
    """Tokio's wasm32-wasip1 job is named `${{ matrix.target }}`; it once matched 13 unrelated jobs."""
    wf, runs = load("tokio")
    for r in successful(runs):
        mapped = [j for j in r.latest_jobs if wf.job_id_for(j.name) == "wasm32-wasip1"]
        assert len(mapped) <= 2, f"run {r.id}: {[j.name for j in mapped]}"

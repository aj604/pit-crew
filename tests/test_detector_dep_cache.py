import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pit_crew.lanes.analysis.critical_path import CIProfile, profile
from pit_crew.lanes.analysis.detectors.dep_cache import INSTALL_COMMANDS, detect_dep_cache
from pit_crew.lanes.analysis.history import parse_runs
from pit_crew.lanes.analysis.workflow import Workflow, parse_workflow

FIXTURES = Path(__file__).parent / "fixtures"
PATH = ".github/workflows/ci.yml"
T0 = datetime(2026, 11, 1, tzinfo=UTC)

BASE = """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
{extra}      - run: npm ci
      - run: npm test
"""
CACHED = "      - uses: actions/setup-node@v4\n        with:\n          cache: npm\n"


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def job(name: str, end: float, steps: tuple[tuple[str, float], ...] = ()) -> dict[str, Any]:
    return {
        "id": int.from_bytes(name.encode(), "big") % 10**9,
        "name": name,
        "run_attempt": 1,
        "conclusion": "success",
        "created_at": at(0),
        "started_at": at(0),
        "completed_at": at(end),
        "labels": [],
        "steps": [
            {"number": i, "name": n, "conclusion": "success", "started_at": at(0), "completed_at": at(s)}
            for i, (n, s) in enumerate(steps, 1)
        ],
    }


def make_profile(yaml_text: str, jobs: list[dict[str, Any]], n: int = 3) -> tuple[Workflow, CIProfile]:
    wf = parse_workflow(yaml_text)
    runs = [
        {
            "id": i,
            "path": PATH,
            "event": "push",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "conclusion": "success",
            "run_attempt": 1,
            "run_started_at": at(0),
            "jobs": jobs,
        }
        for i in range(1, n + 1)
    ]
    return wf, profile(wf, parse_runs(runs))


NPM_JOB = job("test", 200, (("Run npm ci", 120), ("Run npm test", 60)))


def test_dep_cache_flags_uncached_npm_ci() -> None:
    wf, prof = make_profile(BASE.format(extra=""), [NPM_JOB])
    (f,) = detect_dep_cache(prof, wf, PATH)
    assert f.skill == "dep-cache"
    assert f.step == "Run npm ci"
    assert f.job == "test"
    assert f.expected_savings_seconds == 84.0
    assert f.bucket == "setup"
    assert f.risk == "low"
    assert f.confidence == 0.4
    assert f.evidence["step_p50_seconds"] == 120.0
    assert f.evidence["runs_sampled"] == 3


def test_dep_cache_confidence_rises_with_runs() -> None:
    wf, prof = make_profile(BASE.format(extra=""), [NPM_JOB], n=20)
    assert detect_dep_cache(prof, wf, PATH)[0].confidence == 0.7


def test_dep_cache_saving_capped_by_job_max_saving() -> None:
    wf, prof = make_profile(BASE.format(extra=""), [job("test", 100, (("Run npm ci", 160),))])
    (f,) = detect_dep_cache(prof, wf, PATH)
    assert f.expected_savings_seconds == f.evidence["max_saving_seconds"] == 100.0


def test_dep_cache_respects_setup_action_cache() -> None:
    wf, prof = make_profile(BASE.format(extra=CACHED), [NPM_JOB])
    assert detect_dep_cache(prof, wf, PATH) == []


def test_dep_cache_respects_actions_cache_and_enable_cache() -> None:
    for extra in (
        "      - uses: actions/cache@v4\n        with: {path: ~/.npm, key: k}\n",
        "      - uses: some/setup-thing@v1\n        with: {enable-cache: 'TRUE'}\n",
    ):
        wf, prof = make_profile(BASE.format(extra=extra), [NPM_JOB])
        assert detect_dep_cache(prof, wf, PATH) == []


def test_dep_cache_ignores_setup_action_without_cache() -> None:
    extra = (
        "      - uses: actions/setup-node@v4\n        with: {node-version: 20}\n"
        "      - uses: other/tool@v1\n        with: {cache: npm}\n"
    )
    wf, prof = make_profile(BASE.format(extra=extra), [NPM_JOB])
    assert len(detect_dep_cache(prof, wf, PATH)) == 1


def test_dep_cache_skips_off_critical_path_jobs() -> None:
    yaml_text = """
jobs:
  slow:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
  test:
    runs-on: ubuntu-latest
    steps:
      - run: npm ci
"""
    jobs = [job("slow", 300, (("Run echo hi", 300),)), job("test", 150, (("Run npm ci", 120),))]
    wf, prof = make_profile(yaml_text, jobs)
    assert detect_dep_cache(prof, wf, PATH) == []


def test_dep_cache_skips_reusable_jobs() -> None:
    yaml_text = "jobs:\n  call:\n    uses: org/repo/.github/workflows/x.yml@main\n"
    wf, prof = make_profile(yaml_text, [job("call", 200)])
    assert wf.jobs["call"].uses is not None
    assert wf.jobs["call"].steps == ()
    assert detect_dep_cache(prof, wf, PATH) == []


def test_dep_cache_skips_steps_never_measured() -> None:
    wf, prof = make_profile(BASE.format(extra=""), [job("test", 200, (("Run npm test", 60),))])
    assert detect_dep_cache(prof, wf, PATH) == []


def test_dep_cache_skips_non_install_runs() -> None:
    yaml_text = "jobs:\n  test:\n    steps:\n      - run: make\n      - uses: a/b@v1\n"
    wf, prof = make_profile(yaml_text, [job("test", 200)])
    assert detect_dep_cache(prof, wf, PATH) == []


def test_dep_cache_drops_small_savings() -> None:
    wf, prof = make_profile(BASE.format(extra=""), [job("test", 200, (("Run npm ci", 10),))])
    assert detect_dep_cache(prof, wf, PATH) == []


def test_dep_cache_empty_profile() -> None:
    wf = parse_workflow(BASE.format(extra=""))
    assert detect_dep_cache(profile(wf, []), wf, PATH) == []


def test_dep_cache_fastapi_already_caches() -> None:
    wf = parse_workflow((FIXTURES / "fastapi" / "test.yml").read_text())
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz", "rt") as fh:
        runs = parse_runs(json.load(fh))
    assert detect_dep_cache(profile(wf, runs), wf, PATH) == []


@pytest.mark.parametrize(
    "command", ["yarn test", "yarn build", "yarn lint", "cargo build --release", "cat yarn.lock", "npm install-test"]
)
def test_install_regex_rejects_non_installs(command: str) -> None:
    assert INSTALL_COMMANDS.search(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "yarn",
        "yarn --frozen-lockfile",
        "yarn install --immutable",
        "cargo fetch",
        "npm ci && npm test",
        "yarn && yarn test",
    ],
)
def test_install_regex_accepts_installs(command: str) -> None:
    assert INSTALL_COMMANDS.search(command) is not None


def test_dep_cache_setup_go_cache_false_is_flagged() -> None:
    yaml_text = (
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        '      - uses: actions/setup-go@v5\n        with:\n          cache: "false"\n'
        "      - run: go mod download\n"
    )
    wf, prof = make_profile(yaml_text, [job("test", 200, (("Run go mod download", 100),))])
    (f,) = detect_dep_cache(prof, wf, PATH)
    assert f.step == "Run go mod download"


def test_dep_cache_inline_unquoted_enable_cache_counts() -> None:
    extra = "      - uses: astral-sh/setup-uv@v5\n        with: {enable-cache: true}\n"
    wf, prof = make_profile(BASE.format(extra=extra), [NPM_JOB])
    assert detect_dep_cache(prof, wf, PATH) == []

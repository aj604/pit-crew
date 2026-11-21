"""Fetch and parse GitHub Actions run history: runs, their jobs, and each job's steps.

`fetch_history` returns trimmed API JSON, the same shape saved as fixtures under
`tests/fixtures/`, so `parse_runs` is tested offline against exactly what the fetcher returns.

    GITHUB_TOKEN=... uv run python -m pit_crew.lanes.analysis.history fastapi/fastapi test.yml > runs.json
"""

import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

API = "https://api.github.com"

# Fields kept from the API, so fixtures stay small and hold nothing analysis does not use.
_RUN_FIELDS = (
    "id", "name", "path", "event", "head_branch", "head_sha", "status", "conclusion",
    "run_attempt", "created_at", "run_started_at", "updated_at",
)  # fmt: skip
_JOB_FIELDS = (
    "id", "run_id", "run_attempt", "name", "status", "conclusion",
    "created_at", "started_at", "completed_at", "labels", "runner_name",
)  # fmt: skip
_STEP_FIELDS = ("number", "name", "status", "conclusion", "started_at", "completed_at")

# (url) -> (decoded JSON body, url of the next page or None)
Getter = Callable[[str], tuple[Any, str | None]]


@dataclass(frozen=True)
class StepRun:
    number: int
    name: str
    conclusion: str | None
    seconds: float


@dataclass(frozen=True)
class JobRun:
    id: int
    name: str  # display name as GitHub shows it, e.g. "test (ubuntu-latest, 3.15)"
    run_attempt: int
    conclusion: str | None
    created_at: datetime | None  # when the job was queued, i.e. once its `needs` finished
    started_at: datetime | None
    completed_at: datetime | None
    labels: tuple[str, ...]
    steps: tuple[StepRun, ...]

    @property
    def seconds(self) -> float:
        """Time on the runner."""
        return _span(self.started_at, self.completed_at)

    @property
    def queued_seconds(self) -> float:
        """Time between being queued and getting a runner."""
        return _span(self.created_at, self.started_at)


@dataclass(frozen=True)
class WorkflowRun:
    id: int
    path: str
    event: str
    head_branch: str | None
    head_sha: str
    conclusion: str | None
    run_attempt: int
    run_started_at: datetime | None
    jobs: tuple[JobRun, ...]  # every attempt's jobs; see `latest_jobs`

    @property
    def latest_jobs(self) -> tuple[JobRun, ...]:
        """Jobs from the final attempt only, which is what decided the run's outcome."""
        return tuple(j for j in self.jobs if j.run_attempt == self.run_attempt)

    @property
    def wall_clock_seconds(self) -> float:
        """From the final attempt starting to its last job finishing."""
        ends = [j.completed_at for j in self.latest_jobs if j.completed_at]
        return _span(self.run_started_at, max(ends)) if ends else 0.0


def fetch_history(
    repo: str,
    workflow: str,
    *,
    runs: int = 30,
    branch: str | None = None,
    event: str | None = None,
    token: str | None = None,
    get: Getter | None = None,
) -> list[dict[str, Any]]:
    """The most recent completed runs of one workflow, newest first, each with its jobs and steps.

    `workflow` is the file name under .github/workflows (e.g. "ci.yml") or the numeric id.
    Jobs from every attempt are included, so re-runs (a flake signal) stay visible.
    """
    get = get or _http_getter(token if token is not None else os.environ.get("GITHUB_TOKEN"))
    query = {"status": "completed", "per_page": min(runs, 100)}
    if branch:
        query["branch"] = branch
    if event:
        query["event"] = event

    found: list[dict[str, Any]] = []
    url: str | None = f"{API}/repos/{repo}/actions/workflows/{workflow}/runs?{urlencode(query)}"
    while url and len(found) < runs:
        body, url = get(url)
        found.extend(body["workflow_runs"])

    history = []
    for run in found[:runs]:
        trimmed = _pick(run, _RUN_FIELDS)
        trimmed["jobs"] = _fetch_jobs(get, repo, run["id"])
        history.append(trimmed)
    return history


def parse_runs(raw: list[dict[str, Any]]) -> list[WorkflowRun]:
    """Typed runs from `fetch_history` output (or a fixture of it)."""
    return [_parse_run(r) for r in raw]


def _fetch_jobs(get: Getter, repo: str, run_id: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    url: str | None = f"{API}/repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100"
    while url:
        body, url = get(url)
        jobs.extend(body["jobs"])
    return [{**_pick(j, _JOB_FIELDS), "steps": [_pick(s, _STEP_FIELDS) for s in j.get("steps") or []]} for j in jobs]


def _parse_run(r: dict[str, Any]) -> WorkflowRun:
    return WorkflowRun(
        id=r["id"],
        path=r.get("path", ""),
        event=r.get("event", ""),
        head_branch=r.get("head_branch"),
        head_sha=r.get("head_sha", ""),
        conclusion=r.get("conclusion"),
        run_attempt=r.get("run_attempt") or 1,
        run_started_at=_time(r.get("run_started_at")),
        jobs=tuple(_parse_job(j, r.get("run_attempt") or 1) for j in r.get("jobs", [])),
    )


def _parse_job(j: dict[str, Any], default_attempt: int) -> JobRun:
    return JobRun(
        id=j["id"],
        name=j["name"],
        run_attempt=j.get("run_attempt") or default_attempt,
        conclusion=j.get("conclusion"),
        created_at=_time(j.get("created_at")),
        started_at=_time(j.get("started_at")),
        completed_at=_time(j.get("completed_at")),
        labels=tuple(j.get("labels") or ()),
        steps=tuple(
            StepRun(
                s["number"],
                s["name"],
                s.get("conclusion"),
                _span(_time(s.get("started_at")), _time(s.get("completed_at"))),
            )
            for s in j.get("steps", [])
        ),
    )


def _http_getter(token: str | None) -> Getter:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pit-crew"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def get(url: str) -> tuple[Any, str | None]:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return json.load(response), _next_link(response.headers.get("Link"))

    return get


def _next_link(header: str | None) -> str | None:
    match = re.search(r'<([^>]+)>;\s*rel="next"', header or "")
    return match.group(1) if match else None


def _pick(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: d.get(k) for k in keys}


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _span(start: datetime | None, end: datetime | None) -> float:
    """Seconds from start to end; 0 when either is missing or the clock runs backwards."""
    if not start or not end:
        return 0.0
    return max((end - start).total_seconds(), 0.0)


if __name__ == "__main__":
    repo_arg, workflow_arg, *rest = sys.argv[1:]
    print(json.dumps(fetch_history(repo_arg, workflow_arg, runs=int(rest[0]) if rest else 30), indent=1))

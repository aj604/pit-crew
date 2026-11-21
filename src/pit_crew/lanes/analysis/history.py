"""Fetch and parse GitHub Actions run history: runs, their jobs, and each job's steps.

`fetch_history` returns trimmed API JSON, the same shape saved as fixtures under
`tests/fixtures/`, so `parse_runs` is tested offline against exactly what the fetcher returns.

    GITHUB_TOKEN=... uv run python -m pit_crew.lanes.analysis.history fastapi/fastapi test.yml > runs.json
"""

import base64
import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

API = "https://api.github.com"
DEFAULT_RUNS = 150  # the architect's audit window
_JOB_FETCHERS = 8  # parallel job-list requests; well inside GitHub's secondary rate limits

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
    """One step of one job."""

    number: int
    name: str
    conclusion: str | None
    seconds: float


@dataclass(frozen=True)
class JobRun:
    """One job instance in one attempt of a run; a matrix job has one per combination."""

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
    """One run of a workflow, with the jobs of every attempt."""

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


def fetch_history(  # noqa: PLR0913  keyword-only filters mirror the API's query parameters
    repo: str,
    workflow: str,
    *,
    runs: int = DEFAULT_RUNS,
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

    selected = found[:runs]
    with ThreadPoolExecutor(max_workers=_JOB_FETCHERS) as pool:
        jobs = list(pool.map(lambda run: _fetch_jobs(get, repo, run["id"]), selected))
    return [{**_pick(run, _RUN_FIELDS), "jobs": run_jobs} for run, run_jobs in zip(selected, jobs, strict=True)]


def fetch_workflow(
    repo: str,
    workflow: str,
    *,
    ref: str | None = None,
    token: str | None = None,
    get: Getter | None = None,
) -> str:
    """Fetch a workflow YAML file under ``.github/workflows/`` from GitHub's Contents API."""
    get = get or _http_getter(token if token is not None else os.environ.get("GITHUB_TOKEN"))
    url = f"{API}/repos/{repo}/contents/.github/workflows/{workflow}"
    if ref:
        url = f"{url}?{urlencode({'ref': ref})}"
    body, _ = get(url)
    content = body.get("content") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or body.get("type") != "file"
        or body.get("encoding") != "base64"
        or not isinstance(content, str)
    ):
        msg = f"unexpected GitHub contents response for {repo}/.github/workflows/{workflow}"
        raise ValueError(msg)
    return base64.b64decode(content).decode("utf-8")


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
        if not url.startswith(f"{API}/"):  # the token must only ever go to the GitHub API
            msg = f"refusing to fetch {url!r}: not under {API}"
            raise ValueError(msg)
        request = urllib.request.Request(url, headers=headers)  # noqa: S310  https-only, checked above
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response), next_link(response.headers.get("Link"))

    return get


def next_link(header: str | None) -> str | None:
    """The rel="next" URL from a GitHub Link header, or None on the last page."""
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
    sys.stdout.write(
        json.dumps(fetch_history(repo_arg, workflow_arg, runs=int(rest[0]) if rest else DEFAULT_RUNS), indent=1)
    )
    sys.stdout.write("\n")

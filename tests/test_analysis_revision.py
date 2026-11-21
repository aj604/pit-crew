"""Only runs of the exact workflow file in the checkout are profiled."""

import io
import json
import urllib.error
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from pit_crew import tools
from pit_crew.lanes.analysis import history
from pit_crew.ledger import InMemoryLedger
from pit_crew.managed_agent import build_request

REPO = "acme/app"
T0 = datetime(2026, 11, 1, tzinfo=UTC)

CHECKOUT = """name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run checks
        run: pytest
"""
OTHER = CHECKOUT.replace("run: pytest", "run: python -m build")

MATCH_NEW, MATCH_OLD, OTHER_NEWEST, GONE = "a" * 40, "b" * 40, "c" * 40, "d" * 40


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def make_run(run_id: int, head_sha: str, day: int, checks_seconds: float) -> dict[str, Any]:
    start = day * 86400
    steps = [("Run checks", checks_seconds)]
    return {
        "id": run_id,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": head_sha,
        "conclusion": "success",
        "run_attempt": 1,
        "run_started_at": at(start),
        "jobs": [
            {
                "id": run_id,
                "name": "test",
                "run_attempt": 1,
                "conclusion": "success",
                "created_at": at(start),
                "started_at": at(start),
                "completed_at": at(start + checks_seconds),
                "labels": [],
                "steps": [
                    {
                        "number": i,
                        "name": n,
                        "conclusion": "success",
                        "started_at": at(start),
                        "completed_at": at(start + s),
                    }
                    for i, (n, s) in enumerate(steps, 1)
                ],
            }
        ],
    }


@pytest.fixture
def checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(CHECKOUT)
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    build_request("analysis", "")


def patch_history(monkeypatch: pytest.MonkeyPatch, raw: list[dict[str, Any]], blobs: dict[str, str | None]) -> None:
    monkeypatch.setattr(history, "fetch_history", lambda *_a, **_k: raw)
    monkeypatch.setattr(history, "fetch_workflow_blob_shas", lambda _r, _p, refs, **_k: {r: blobs[r] for r in refs})


def mixed_history() -> list[dict[str, Any]]:
    """The newest run is of another revision, whose "Run checks" builds for 200 s."""
    return [
        make_run(4, OTHER_NEWEST, 4, 200),
        make_run(3, MATCH_NEW, 3, 100),
        make_run(2, MATCH_OLD, 2, 110),
        make_run(1, OTHER_NEWEST, 1, 200),
    ]


@pytest.mark.usefixtures("checkout")
def test_only_runs_of_the_checkouts_workflow_file_are_profiled(monkeypatch: pytest.MonkeyPatch) -> None:
    local = history.workflow_blob_sha(CHECKOUT)
    blobs: dict[str, str | None] = {MATCH_NEW: local, MATCH_OLD: local, OTHER_NEWEST: history.workflow_blob_sha(OTHER)}
    patch_history(monkeypatch, mixed_history(), blobs)
    ledger = InMemoryLedger()

    ci = tools.call("profile_ci", ledger, {"repo": REPO, "workflow": "ci.yml"})
    found = tools.call("find_bottlenecks", ledger, {"repo": REPO, "workflow": "ci.yml"})

    json.dumps(found)
    assert ci["runs_analyzed"] == 2
    assert ci["runs_excluded_revision"] == 2
    assert ci["steps"][0]["median_seconds"] == pytest.approx(105)  # not pulled towards the 200 s build
    assert found["profile_summary"]["runs_analyzed"] == 2
    assert found["profile_summary"]["runs_excluded_revision"] == 2
    assert found["profile_summary"]["base_sha"] == MATCH_NEW  # the newest matching run, not the newest run


@pytest.mark.usefixtures("checkout")
def test_no_matching_run_gives_no_findings_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    other = history.workflow_blob_sha(OTHER)
    patch_history(monkeypatch, mixed_history()[:1] + mixed_history()[3:], {OTHER_NEWEST: other})

    result = tools.call("find_bottlenecks", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})

    assert result["findings"] == []
    assert result["profile_summary"]["runs_analyzed"] == 0
    assert result["profile_summary"]["runs_excluded_revision"] == 2
    assert result["profile_summary"]["base_sha"] == ""
    assert len(result["uncovered"]) == 1
    assert "exact workflow file" in result["uncovered"][0]
    assert "branch" in result["uncovered"][0]


def test_workflow_blob_sha_matches_git_hash_object() -> None:
    # printf 'name: CI\njobs: {}\n' | git hash-object --stdin
    assert history.workflow_blob_sha("name: CI\njobs: {}\n") == "9c176ad8516f3333a7419f15c073bc624b91fcb5"
    assert history.workflow_blob_sha("") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"  # the empty blob


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", None, io.BytesIO(b""))  # type: ignore[arg-type]


def test_fetch_workflow_blob_shas_reads_the_contents_api_once_per_distinct_sha() -> None:
    urls: list[str] = []

    def get(url: str) -> tuple[Any, str | None]:
        urls.append(url)
        return {"sha": "f" * 40, "type": "file"}, None

    found = history.fetch_workflow_blob_shas(
        REPO, ".github/workflows/ci.yml", [MATCH_NEW, MATCH_NEW, MATCH_OLD, "main", "../x", ""], get=get
    )

    assert found == {MATCH_NEW: "f" * 40, MATCH_OLD: "f" * 40}  # non-SHA refs are skipped
    assert sorted(urls) == [
        f"https://api.github.com/repos/{REPO}/contents/.github/workflows/ci.yml?ref={sha}"
        for sha in (MATCH_NEW, MATCH_OLD)
    ]


@pytest.mark.parametrize(
    "path", [".github/workflows/../../etc/passwd", "ci.yml", ".github/workflows/.x.yml", "x/ci.yml"]
)
def test_fetch_workflow_blob_shas_rejects_paths_other_than_a_workflow_file(path: str) -> None:
    with pytest.raises(ValueError, match="path must be"):
        history.fetch_workflow_blob_shas(REPO, path, [MATCH_NEW], get=lambda _u: pytest.fail("must not fetch"))


def test_a_404_for_one_ref_is_a_missing_file_not_a_failure() -> None:
    def get(url: str) -> tuple[Any, str | None]:
        if GONE in url:
            raise http_error(404)
        return {"sha": "f" * 40}, None

    found = history.fetch_workflow_blob_shas(REPO, ".github/workflows/ci.yml", [MATCH_NEW, GONE], get=get)

    assert found == {MATCH_NEW: "f" * 40, GONE: None}


def test_other_http_errors_still_fail_loudly() -> None:
    def get(_url: str) -> tuple[Any, str | None]:
        raise http_error(403)

    with pytest.raises(urllib.error.HTTPError):
        history.fetch_workflow_blob_shas(REPO, ".github/workflows/ci.yml", [MATCH_NEW], get=get)


@pytest.mark.usefixtures("checkout")
def test_a_run_whose_file_is_missing_at_its_commit_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    local = history.workflow_blob_sha(CHECKOUT)
    raw = [make_run(2, MATCH_NEW, 2, 100), make_run(1, GONE, 1, 100)]
    patch_history(monkeypatch, raw, {MATCH_NEW: local, GONE: None})

    result = tools.call("profile_ci", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})

    assert result["runs_analyzed"] == 1
    assert result["runs_excluded_revision"] == 1


def with_event(run: dict[str, Any], event: str) -> dict[str, Any]:
    return {**run, "event": event}


@pytest.mark.usefixtures("checkout")
def test_only_runs_that_execute_the_workflow_at_head_sha_are_profiled(monkeypatch: pytest.MonkeyPatch) -> None:
    local = history.workflow_blob_sha(CHECKOUT)
    raw = [
        make_run(1, MATCH_NEW, 1, 100),
        with_event(make_run(2, OTHER_NEWEST, 2, 200), "pull_request"),
        make_run(3, MATCH_OLD, 3, 110),
        with_event(make_run(4, GONE, 4, 200), "pull_request_target"),
    ]
    asked: list[str] = []

    def blobs(_repo: str, _path: str, refs: Iterable[str], **_k: object) -> dict[str, str | None]:
        asked.extend(refs)
        return dict.fromkeys(refs, local)

    monkeypatch.setattr(history, "fetch_history", lambda *_a, **_k: raw)
    monkeypatch.setattr(history, "fetch_workflow_blob_shas", blobs)

    ci = tools.call("profile_ci", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})
    found = tools.call("find_bottlenecks", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})

    assert ci["runs_analyzed"] == 2
    assert ci["runs_excluded_event"] == 2
    assert ci["runs_excluded_revision"] == 0
    assert found["profile_summary"]["runs_excluded_event"] == 2
    assert set(asked) == {MATCH_NEW, MATCH_OLD}  # no blob fetch for the pull-request runs
    assert found["profile_summary"]["base_sha"] == MATCH_OLD  # newest trusted run (day 3), not the newer PR run
    assert found["uncovered"] == []  # a mix of trusted and pull-request runs is analysed normally


@pytest.mark.usefixtures("checkout")
def test_only_pull_request_runs_gives_no_findings_and_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = [with_event(make_run(i, MATCH_NEW, i, 100), "pull_request") for i in range(1, 4)]
    monkeypatch.setattr(history, "fetch_history", lambda *_a, **_k: raw)
    monkeypatch.setattr(history, "fetch_workflow_blob_shas", lambda *_a, **_k: pytest.fail("must not fetch"))

    result = tools.call("find_bottlenecks", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})

    assert result["findings"] == []
    assert result["profile_summary"]["runs_analyzed"] == 0
    assert result["profile_summary"]["runs_excluded_event"] == 3
    assert len(result["uncovered"]) == 1
    assert "No push, workflow_dispatch, schedule or merge_group runs were found" in result["uncovered"][0]
    assert "Pull-request and other runs" in result["uncovered"][0]
    assert "branch=<default>" in result["uncovered"][0]


@pytest.mark.usefixtures("checkout")
@pytest.mark.parametrize("event", ["pull_request", "pull_request_target", "repository_dispatch", "", "made_up"])
def test_other_and_unknown_events_are_excluded(monkeypatch: pytest.MonkeyPatch, event: str) -> None:
    local = history.workflow_blob_sha(CHECKOUT)
    raw = [make_run(1, MATCH_NEW, 1, 100), with_event(make_run(2, MATCH_OLD, 2, 100), event)]
    patch_history(monkeypatch, raw, {MATCH_NEW: local, MATCH_OLD: local})

    result = tools.call("profile_ci", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})

    assert result["runs_analyzed"] == 1
    assert result["runs_excluded_event"] == 1


@pytest.mark.usefixtures("checkout")
@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "schedule", "merge_group"])
def test_events_that_execute_the_workflow_at_head_sha_are_trusted(monkeypatch: pytest.MonkeyPatch, event: str) -> None:
    local = history.workflow_blob_sha(CHECKOUT)
    patch_history(monkeypatch, [with_event(make_run(1, MATCH_NEW, 1, 100), event)], {MATCH_NEW: local})

    result = tools.call("profile_ci", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})

    assert result["runs_analyzed"] == 1
    assert result["runs_excluded_event"] == 0


@pytest.mark.usefixtures("checkout")
def test_empty_history_says_neither_the_event_nor_the_revision_note(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_history(monkeypatch, [], {})

    result = tools.call("find_bottlenecks", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})

    assert result["findings"] == []
    assert result["profile_summary"]["base_sha"] == ""
    assert result["profile_summary"]["runs_excluded_event"] == 0
    assert result["profile_summary"]["runs_excluded_revision"] == 0
    assert result["uncovered"] == ["No runs analyzed (all excluded or none fetched); nothing was measured."]


@pytest.mark.usefixtures("checkout")
def test_event_exclusion_alongside_a_revision_mismatch_gives_only_the_revision_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every trusted run is of another revision, and the rest are pull-request runs: the trusted runs
    # are what matter, so the note is the revision one, not a contradictory "only pull-request runs".
    other = history.workflow_blob_sha(OTHER)
    raw = [
        make_run(1, OTHER_NEWEST, 1, 200),
        with_event(make_run(2, MATCH_NEW, 2, 100), "pull_request"),
    ]
    patch_history(monkeypatch, raw, {OTHER_NEWEST: other})

    result = tools.call("find_bottlenecks", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})

    assert result["findings"] == []
    assert result["profile_summary"]["runs_excluded_event"] == 1
    assert result["profile_summary"]["runs_excluded_revision"] == 1
    assert len(result["uncovered"]) == 1
    assert "exact workflow file" in result["uncovered"][0]
    assert "pull-request" not in result["uncovered"][0]


@pytest.mark.usefixtures("checkout")
def test_the_window_is_filled_per_trusted_event_so_pull_request_runs_cannot_crowd_it_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = history.workflow_blob_sha(CHECKOUT)
    by_event = {
        "pull_request": [with_event(make_run(100 + i, OTHER_NEWEST, 10 + i, 200), "pull_request") for i in range(9)],
        "push": [make_run(2, MATCH_NEW, 5, 100), make_run(1, MATCH_OLD, 1, 110)],
        "schedule": [with_event(make_run(i, MATCH_OLD, 3, 105), "schedule") for i in (3, 4)],
    }
    every = {r["id"]: r for runs in by_event.values() for r in runs}
    run_requests: list[tuple[str, int]] = []
    jobs_requested: list[int] = []

    def get(url: str) -> tuple[Any, str | None]:
        if "/jobs" in url:
            run_id = int(url.split("/runs/")[1].split("/", maxsplit=1)[0])
            jobs_requested.append(run_id)
            return {"jobs": every[run_id]["jobs"]}, None
        query = parse_qs(urlparse(url).query)
        event, per_page = query["event"][0], int(query["per_page"][0])
        run_requests.append((event, per_page))
        page = [{k: v for k, v in r.items() if k != "jobs"} for r in by_event.get(event, [])[:per_page]]
        return {"workflow_runs": page}, None

    real_fetch = history.fetch_history
    monkeypatch.setattr(history, "fetch_history", lambda *a, **k: real_fetch(*a, **k, get=get))
    monkeypatch.setattr(history, "fetch_workflow_blob_shas", lambda _r, _p, refs, **_k: dict.fromkeys(refs, local))

    result = tools.call("profile_ci", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml", "runs": 3})

    # Push first, then each call asks only for what is missing; pull_request is never requested.
    assert run_requests == [("push", 3), ("merge_group", 1), ("workflow_dispatch", 1), ("schedule", 1)]
    assert sorted(jobs_requested) == [1, 2, 3]  # jobs only for the three kept runs
    assert result["runs_analyzed"] == 3
    assert result["runs_excluded_event"] == 0


@pytest.mark.usefixtures("checkout")
def test_runs_returned_by_more_than_one_event_call_are_counted_once(monkeypatch: pytest.MonkeyPatch) -> None:
    local = history.workflow_blob_sha(CHECKOUT)
    raw = [make_run(2, MATCH_NEW, 2, 100), make_run(1, MATCH_OLD, 1, 110)]
    patch_history(monkeypatch, raw, {MATCH_NEW: local, MATCH_OLD: local})

    result = tools.call("find_bottlenecks", InMemoryLedger(), {"repo": REPO, "workflow": "ci.yml"})

    assert result["profile_summary"]["runs_analyzed"] == 2  # the same fake answers every event call
    assert result["profile_summary"]["base_sha"] == MATCH_NEW

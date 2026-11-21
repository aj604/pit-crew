import json
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from typing import override

import httpx
import pytest

from pit_crew.github import (
    API_VERSION,
    USER_AGENT,
    DispatchPollConfig,
    GitHubClient,
    HttpxGitHubClient,
    WorkflowRunResult,
)


class FakeGitHubClient:
    """In-memory offline implementation of ``GitHubClient`` for protocol conformance tests."""

    def __init__(self, default_shas: dict[str, str]) -> None:
        self._default_shas = dict(default_shas)
        self.branches: dict[tuple[str, str], str] = {}
        self.commits: list[dict[str, object]] = []
        self.dispatches: list[tuple[str, str, str]] = []
        self.runs: dict[tuple[str, int], WorkflowRunResult] = {}
        self.pull_requests: list[dict[str, str]] = []
        self._next_run_id = 100
        self._commit_seq = 0

    def get_default_branch_sha(self, repo: str) -> str:
        return self._default_shas[repo]

    def create_branch(self, repo: str, branch: str, base_sha: str) -> str:
        self.branches[(repo, branch)] = base_sha
        return base_sha

    def delete_branch(self, repo: str, branch: str) -> None:
        del self.branches[(repo, branch)]

    def commit_files(
        self,
        repo: str,
        branch: str,
        base_sha: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        self._commit_seq += 1
        head_sha = f"commit-{self._commit_seq:04d}"
        self.branches[(repo, branch)] = head_sha
        self.commits.append(
            {
                "repo": repo,
                "branch": branch,
                "base_sha": base_sha,
                "head_sha": head_sha,
                "files": dict(files),
                "message": message,
            }
        )
        return head_sha

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> int:
        self._next_run_id += 1
        run_id = self._next_run_id
        self.dispatches.append((repo, workflow, ref))
        head_sha = self.branches.get((repo, ref), self._default_shas[repo])
        self.runs[(repo, run_id)] = WorkflowRunResult(
            run_id=run_id,
            status="completed",
            conclusion="success",
            duration_seconds=42.5,
            html_url=f"https://github.com/{repo}/actions/runs/{run_id}",
            head_sha=head_sha,
            head_branch=ref,
        )
        return run_id

    def get_workflow_run(self, repo: str, run_id: int) -> WorkflowRunResult:
        return self.runs[(repo, run_id)]

    def open_pull_request(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> str:
        pr_number = len(self.pull_requests) + 1
        url = f"https://github.com/{repo}/pull/{pr_number}"
        self.pull_requests.append(
            {
                "repo": repo,
                "title": title,
                "body": body,
                "head": head,
                "base": base,
                "url": url,
            }
        )
        return url


def _exercise_github_client(client: GitHubClient, repo: str) -> tuple[str, WorkflowRunResult, str]:
    base_sha = client.get_default_branch_sha(repo)
    branch = "pc/candidate-1"
    created_sha = client.create_branch(repo, branch, base_sha)
    assert created_sha == base_sha

    head_sha = client.commit_files(
        repo,
        branch,
        base_sha,
        {".github/workflows/ci.yml": "name: CI\n"},
        "Add dependency cache",
    )
    run_id = client.dispatch_workflow(repo, "ci.yml", branch)
    run_result = client.get_workflow_run(repo, run_id)
    pr_url = client.open_pull_request(
        repo,
        title="Speed up CI with dep cache",
        body="Measured 2.1x speedup.",
        head=branch,
        base="main",
    )
    client.delete_branch(repo, branch)
    return head_sha, run_result, pr_url


def test_workflow_run_result_exposes_all_fields_and_is_frozen() -> None:
    result = WorkflowRunResult(
        run_id=77,
        status="completed",
        conclusion="success",
        duration_seconds=51.25,
        html_url="https://github.com/org/repo/actions/runs/77",
        head_sha="abc123def",
        head_branch="pc/cand-1",
    )

    assert [f.name for f in fields(WorkflowRunResult)] == [
        "run_id",
        "status",
        "conclusion",
        "duration_seconds",
        "html_url",
        "head_sha",
        "head_branch",
    ]
    assert result.run_id == 77
    assert result.status == "completed"
    assert result.conclusion == "success"
    assert result.duration_seconds == 51.25
    assert result.html_url == "https://github.com/org/repo/actions/runs/77"
    assert result.head_sha == "abc123def"
    assert result.head_branch == "pc/cand-1"
    assert hash(result) == hash(
        WorkflowRunResult(
            run_id=77,
            status="completed",
            conclusion="success",
            duration_seconds=51.25,
            html_url="https://github.com/org/repo/actions/runs/77",
            head_sha="abc123def",
            head_branch="pc/cand-1",
        )
    )

    with pytest.raises(FrozenInstanceError):
        result.status = "in_progress"  # type: ignore[misc]


def test_workflow_run_result_allows_none_conclusion_for_active_runs() -> None:
    queued = WorkflowRunResult(
        run_id=88,
        status="queued",
        conclusion=None,
        duration_seconds=0.0,
        html_url="https://github.com/org/repo/actions/runs/88",
        head_sha="deadbeef",
        head_branch="main",
    )
    assert queued.status == "queued"
    assert queued.conclusion is None


def test_fake_github_client_conforms_to_protocol_and_exercises_all_methods() -> None:
    fake = FakeGitHubClient({"org/repo": "base-sha-000"})
    # Assign to the Protocol type before isinstance() so mypy checks structural
    # subtyping without narrowing `fake` to an intersection type first.
    client: GitHubClient = fake

    head_sha, run_result, pr_url = _exercise_github_client(client, "org/repo")
    assert isinstance(fake, GitHubClient)

    assert head_sha == "commit-0001"
    assert run_result == WorkflowRunResult(
        run_id=101,
        status="completed",
        conclusion="success",
        duration_seconds=42.5,
        html_url="https://github.com/org/repo/actions/runs/101",
        head_sha="commit-0001",
        head_branch="pc/candidate-1",
    )
    assert pr_url == "https://github.com/org/repo/pull/1"
    assert ("org/repo", "pc/candidate-1") not in fake.branches
    assert len(fake.commits) == 1
    assert fake.commits[0]["files"] == {".github/workflows/ci.yml": "name: CI\n"}
    assert fake.dispatches == [("org/repo", "ci.yml", "pc/candidate-1")]
    assert len(fake.pull_requests) == 1
    assert fake.pull_requests[0]["head"] == "pc/candidate-1"
    assert fake.pull_requests[0]["base"] == "main"


def test_incomplete_client_fails_runtime_protocol_check() -> None:
    class IncompleteClient:
        def get_default_branch_sha(self, repo: str) -> str:
            return repo

    assert not isinstance(IncompleteClient(), GitHubClient)


_TEST_TOKEN = "ghp_test_secret_token_123"  # noqa: S105


def _assert_github_headers(request: httpx.Request, expected_token: str = _TEST_TOKEN) -> None:
    assert request.headers["Authorization"] == f"Bearer {expected_token}"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == API_VERSION
    assert request.headers["User-Agent"] == USER_AGENT


def test_httpx_client_requires_token_and_validates_init_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GitHub token is required"):
        HttpxGitHubClient()

    with pytest.raises(ValueError, match="GitHub token is required"):
        HttpxGitHubClient("   ")

    with pytest.raises(ValueError, match="base_url must not be empty"):
        HttpxGitHubClient(_TEST_TOKEN, base_url="///")

    with pytest.raises(ValueError, match="base_url must not be empty"):
        HttpxGitHubClient(_TEST_TOKEN, base_url="   ")

    with pytest.raises(ValueError, match="attempts must be at least 1"):
        DispatchPollConfig(attempts=0)

    with pytest.raises(ValueError, match="interval_seconds must be non-negative"):
        DispatchPollConfig(interval_seconds=-0.1)

    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={}))
    with (
        httpx.Client(transport=transport) as raw_client,
        pytest.raises(ValueError, match="not both"),
    ):
        HttpxGitHubClient(_TEST_TOKEN, transport=transport, client=raw_client)


def test_httpx_client_uses_github_token_env_and_normalizes_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_token = "ghp_from_env_999"  # noqa: S105
    monkeypatch.setenv("GITHUB_TOKEN", env_token)
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request, expected_token=env_token)
        seen_urls.append(str(request.url))
        if request.url.path == "/api/v3/repos/org/repo":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path == "/api/v3/repos/org/repo/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "env-sha-1"}})
        return httpx.Response(404)

    with HttpxGitHubClient(
        base_url="https://ghe.example.com/api/v3/",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert isinstance(client, GitHubClient)
        assert client.get_default_branch_sha("org/repo") == "env-sha-1"

    assert seen_urls == [
        "https://ghe.example.com/api/v3/repos/org/repo",
        "https://ghe.example.com/api/v3/repos/org/repo/git/ref/heads/main",
    ]


def test_httpx_client_close_respects_injected_client_ownership() -> None:
    transport = httpx.MockTransport(
        lambda req: (
            httpx.Response(200, json={"default_branch": "main"})
            if req.url.path.endswith("/repos/org/repo")
            else httpx.Response(200, json={"object": {"sha": "abc"}})
        )
    )
    external = httpx.Client(transport=transport)
    with HttpxGitHubClient(_TEST_TOKEN, client=external) as wrapped:
        assert wrapped.get_default_branch_sha("org/repo") == "abc"
    assert not external.is_closed
    external.close()


def test_httpx_client_owned_close_closes_transport_and_never_leaks_token() -> None:
    class TrackingMockTransport(httpx.MockTransport):
        def __init__(self) -> None:
            super().__init__(lambda _req: httpx.Response(401, json={"message": "Bad credentials"}))
            self.closed = False

        @override
        def close(self) -> None:
            self.closed = True
            super().close()

    transport = TrackingMockTransport()
    with HttpxGitHubClient(_TEST_TOKEN, transport=transport) as client:
        assert not transport.closed
        assert _TEST_TOKEN not in repr(client)
        assert _TEST_TOKEN not in str(client)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.get_default_branch_sha("org/repo")
        assert _TEST_TOKEN not in str(exc_info.value)

    assert transport.closed


@pytest.mark.parametrize("bad_repo", ["", "noslash", "/name", "owner/", "a/b/c"])
def test_httpx_client_rejects_invalid_repo_format(bad_repo: str) -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={}))
    with (
        HttpxGitHubClient(_TEST_TOKEN, transport=transport) as client,
        pytest.raises(ValueError, match="Invalid repository"),
    ):
        client.get_default_branch_sha(bad_repo)


def test_httpx_get_default_branch_sha_and_errors() -> None:
    calls: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request)
        calls.append((request.method, request.url.raw_path))
        if request.url.raw_path == b"/repos/aj604/pit-crew":
            return httpx.Response(200, json={"default_branch": "release/v1"})
        if request.url.raw_path == b"/repos/aj604/pit-crew/git/ref/heads/release/v1":
            return httpx.Response(200, json={"ref": "refs/heads/release/v1", "object": {"sha": "deadbeef01"}})
        return httpx.Response(404, json={"message": "Not Found"})

    with HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler)) as client:
        assert client.get_default_branch_sha("aj604/pit-crew") == "deadbeef01"
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.get_default_branch_sha("aj604/missing")
        assert exc_info.value.response.status_code == 404

    assert calls == [
        ("GET", b"/repos/aj604/pit-crew"),
        ("GET", b"/repos/aj604/pit-crew/git/ref/heads/release/v1"),
        ("GET", b"/repos/aj604/missing"),
    ]


def test_httpx_create_and_delete_branch_happy_and_error_paths() -> None:
    recorded: list[tuple[str, bytes, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request)
        body = json.loads(request.content) if request.content else None
        recorded.append((request.method, request.url.raw_path, body))
        if request.method == "POST" and request.url.raw_path == b"/repos/org/repo/git/refs":
            if isinstance(body, dict) and body.get("ref") == "refs/heads/existing":
                return httpx.Response(422, json={"message": "Reference already exists"})
            return httpx.Response(201, json={"ref": "refs/heads/pc/cand-1", "object": {"sha": "base123"}})
        if request.method == "DELETE" and request.url.raw_path == b"/repos/org/repo/git/refs/heads/pc/cand-1":
            return httpx.Response(204)
        return httpx.Response(422, json={"message": "Reference does not exist"})

    with HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler)) as client:
        assert client.create_branch("org/repo", "pc/cand-1", "base123") == "base123"
        client.delete_branch("org/repo", "pc/cand-1")

        with pytest.raises(httpx.HTTPStatusError) as create_err:
            client.create_branch("org/repo", "existing", "base123")
        assert create_err.value.response.status_code == 422

        with pytest.raises(httpx.HTTPStatusError) as delete_err:
            client.delete_branch("org/repo", "nonexistent")
        assert delete_err.value.response.status_code == 422

    assert recorded[:2] == [
        ("POST", b"/repos/org/repo/git/refs", {"ref": "refs/heads/pc/cand-1", "sha": "base123"}),
        ("DELETE", b"/repos/org/repo/git/refs/heads/pc/cand-1", None),
    ]


def test_httpx_commit_files_git_data_api_sequence() -> None:
    recorded: list[tuple[str, bytes, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request)
        body = json.loads(request.content) if request.content else None
        recorded.append((request.method, request.url.raw_path, body))
        if request.method == "GET" and request.url.raw_path == b"/repos/org/repo/git/commits/base-sha":
            return httpx.Response(200, json={"sha": "base-sha", "tree": {"sha": "base-tree-sha"}})
        if request.method == "POST" and request.url.raw_path == b"/repos/org/repo/git/trees":
            return httpx.Response(201, json={"sha": "new-tree-sha"})
        if request.method == "POST" and request.url.raw_path == b"/repos/org/repo/git/commits":
            return httpx.Response(201, json={"sha": "new-commit-sha"})
        if request.method == "PATCH" and request.url.raw_path == b"/repos/org/repo/git/refs/heads/pc/cand-1":
            return httpx.Response(200, json={"ref": "refs/heads/pc/cand-1", "object": {"sha": "new-commit-sha"}})
        return httpx.Response(500)

    with HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler)) as client:
        new_sha = client.commit_files(
            "org/repo",
            "pc/cand-1",
            "base-sha",
            {
                "b.txt": "second\n",
                ".github/workflows/ci.yml": "name: CI\n",
            },
            "Apply workflow overlay",
        )

    assert new_sha == "new-commit-sha"
    assert recorded == [
        ("GET", b"/repos/org/repo/git/commits/base-sha", None),
        (
            "POST",
            b"/repos/org/repo/git/trees",
            {
                "base_tree": "base-tree-sha",
                "tree": [
                    {
                        "path": ".github/workflows/ci.yml",
                        "mode": "100644",
                        "type": "blob",
                        "content": "name: CI\n",
                    },
                    {
                        "path": "b.txt",
                        "mode": "100644",
                        "type": "blob",
                        "content": "second\n",
                    },
                ],
            },
        ),
        (
            "POST",
            b"/repos/org/repo/git/commits",
            {
                "message": "Apply workflow overlay",
                "tree": "new-tree-sha",
                "parents": ["base-sha"],
            },
        ),
        (
            "PATCH",
            b"/repos/org/repo/git/refs/heads/pc/cand-1",
            {"sha": "new-commit-sha"},
        ),
    ]


def test_httpx_commit_files_empty_overlay_reuses_base_tree() -> None:
    recorded: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request)
        body = json.loads(request.content) if request.content else None
        recorded.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/repos/org/repo/git/commits/base-sha":
            return httpx.Response(200, json={"sha": "base-sha", "tree": {"sha": "base-tree-sha"}})
        if request.method == "POST" and request.url.path == "/repos/org/repo/git/commits":
            return httpx.Response(201, json={"sha": "empty-commit-sha"})
        if request.method == "PATCH" and request.url.path == "/repos/org/repo/git/refs/heads/pc/empty":
            return httpx.Response(200, json={"object": {"sha": "empty-commit-sha"}})
        return httpx.Response(500)

    with HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler)) as client:
        assert client.commit_files("org/repo", "pc/empty", "base-sha", {}, "Empty trigger commit") == "empty-commit-sha"

    assert recorded == [
        ("GET", "/repos/org/repo/git/commits/base-sha", None),
        (
            "POST",
            "/repos/org/repo/git/commits",
            {"message": "Empty trigger commit", "tree": "base-tree-sha", "parents": ["base-sha"]},
        ),
        ("PATCH", "/repos/org/repo/git/refs/heads/pc/empty", {"sha": "empty-commit-sha"}),
    ]


def test_httpx_commit_files_propagates_http_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"sha": "base-sha", "tree": {"sha": "base-tree"}})
        if request.url.path.endswith("/git/trees"):
            return httpx.Response(422, json={"message": "Invalid tree"})
        return httpx.Response(500)

    with (
        HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        client.commit_files("org/repo", "pc/cand-1", "base-sha", {"a.txt": "x"}, "msg")
    assert exc_info.value.response.status_code == 422


@pytest.mark.parametrize(
    "workflow_arg",
    [
        "ci.yml",
        ".github/workflows/ci.yml",
        "/.github/workflows/ci.yml",
        "./.github/workflows/ci.yml",
    ],
)
def test_httpx_dispatch_workflow_returns_run_id_from_200_response(workflow_arg: str) -> None:
    recorded: list[tuple[str, bytes, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request)
        body = json.loads(request.content) if request.content else None
        recorded.append((request.method, request.url.raw_path, body))
        return httpx.Response(
            200,
            json={"workflow_run_id": 987654, "html_url": "https://github.com/org/repo/actions/runs/987654"},
        )

    with HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler)) as client:
        assert client.dispatch_workflow("org/repo", workflow_arg, "pc/cand-1") == 987654

    assert recorded == [
        (
            "POST",
            b"/repos/org/repo/actions/workflows/ci.yml/dispatches",
            {"ref": "pc/cand-1", "return_run_details": True},
        )
    ]


@pytest.mark.parametrize("bad_workflow", ["", "   ", ".github/workflows/", "/.github/workflows/"])
def test_httpx_dispatch_workflow_rejects_empty_workflow_identifier(bad_workflow: str) -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json={}))
    with (
        HttpxGitHubClient(_TEST_TOKEN, transport=transport) as client,
        pytest.raises(ValueError, match="Invalid workflow identifier"),
    ):
        client.dispatch_workflow("org/repo", bad_workflow, "main")


def test_httpx_dispatch_workflow_falls_back_to_runs_lookup_on_204() -> None:
    recorded: list[tuple[str, bytes, dict[str, str], object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request)
        body = json.loads(request.content) if request.content else None
        recorded.append((request.method, request.url.raw_path, dict(request.url.params), body))
        if request.method == "POST":
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {"id": 55501, "created_at": "2026-11-21T10:00:05Z"},
                    {"id": 55499, "created_at": "2026-11-21T09:59:59Z"},
                ]
            },
        )

    poll_config = DispatchPollConfig(
        attempts=2,
        interval_seconds=0.0,
        sleep=lambda _s: None,
        clock=lambda: datetime(2026, 11, 21, 10, 0, 5, 750_000, tzinfo=UTC),
    )
    with HttpxGitHubClient(
        _TEST_TOKEN,
        transport=httpx.MockTransport(handler),
        dispatch_poll=poll_config,
    ) as client:
        assert client.dispatch_workflow("org/repo", ".github/workflows/ci.yml", "refs/heads/pc/cand-1") == 55501

    assert recorded == [
        (
            "POST",
            b"/repos/org/repo/actions/workflows/ci.yml/dispatches",
            {},
            {"ref": "refs/heads/pc/cand-1", "return_run_details": True},
        ),
        (
            "GET",
            b"/repos/org/repo/actions/workflows/ci.yml/runs?branch=pc%2Fcand-1&event=workflow_dispatch&per_page=5",
            {"branch": "pc/cand-1", "event": "workflow_dispatch", "per_page": "5"},
            None,
        ),
    ]


def test_httpx_dispatch_workflow_204_polls_and_skips_older_and_seen_runs() -> None:
    dispatch_count = 0
    get_count = 0
    sleeps: list[float] = []
    poll_responses: list[dict[str, object]] = [
        # Second dispatch, attempt 1: older run, already-seen run (same second), missing timestamp, non-dict entry.
        {
            "workflow_runs": [
                "unexpected-non-dict",
                {"id": 55501, "created_at": "2026-11-21T10:00:05Z"},
                {"id": 55500, "created_at": "2026-11-21T10:00:04Z"},
                {"id": 55499},
            ]
        },
        # Second dispatch, attempt 2: new run appears with non-UTC offset equal to 10:00:06Z.
        {
            "workflow_runs": [
                {"id": 55502, "created_at": "2026-11-21T05:00:06-05:00"},
                {"id": 55501, "created_at": "2026-11-21T10:00:05Z"},
            ]
        },
        # Third dispatch, attempt 1 & 2: only previously seen runs ever appear.
        {"workflow_runs": [{"id": 55502, "created_at": "2026-11-21T10:00:06Z"}]},
        {"workflow_runs": [{"id": 55502, "created_at": "2026-11-21T10:00:06Z"}]},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal dispatch_count, get_count
        if request.method == "POST":
            dispatch_count += 1
            if dispatch_count == 1:
                return httpx.Response(200, json={"workflow_run_id": 55501})
            return httpx.Response(204)
        payload = poll_responses[get_count]
        get_count += 1
        return httpx.Response(200, json=payload)

    poll_config = DispatchPollConfig(
        attempts=2,
        interval_seconds=0.25,
        sleep=sleeps.append,
        clock=lambda: datetime(2026, 11, 21, 5, 0, 5, tzinfo=timezone(timedelta(hours=-5))),
    )
    with HttpxGitHubClient(
        _TEST_TOKEN,
        transport=httpx.MockTransport(handler),
        dispatch_poll=poll_config,
    ) as client:
        assert client.dispatch_workflow("org/repo", "ci.yml", "pc/cand-1") == 55501
        assert client.dispatch_workflow("org/repo", "ci.yml", "pc/cand-1") == 55502
        with pytest.raises(RuntimeError, match="No workflow_dispatch run found"):
            client.dispatch_workflow("org/repo", "ci.yml", "pc/cand-1")

    assert dispatch_count == 3
    assert get_count == 4
    assert sleeps == [0.25, 0.25]


def test_httpx_dispatch_workflow_200_without_run_id_falls_back_and_errors_when_empty() -> None:
    # Naive clock and naive created_at are treated as UTC.
    naive_poll = DispatchPollConfig(
        attempts=2,
        interval_seconds=0.0,
        sleep=lambda _s: None,
        clock=lambda: datetime(2026, 11, 21, 10, 0, 0),  # noqa: DTZ001
    )

    def handler_with_run(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"workflow_runs": [{"id": 444, "created_at": "2026-11-21T10:00:00Z"}]})

    with HttpxGitHubClient(
        _TEST_TOKEN,
        transport=httpx.MockTransport(handler_with_run),
        dispatch_poll=naive_poll,
    ) as client:
        assert client.dispatch_workflow("org/repo", "ci.yml", "main") == 444

    empty_sleeps: list[float] = []
    empty_poll = DispatchPollConfig(
        attempts=2,
        interval_seconds=0.1,
        sleep=empty_sleeps.append,
        clock=lambda: datetime(2026, 11, 21, 10, 0, 0, tzinfo=UTC),
    )

    def handler_empty_runs(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(204)
        return httpx.Response(200, json={"workflow_runs": []})

    with (
        HttpxGitHubClient(
            _TEST_TOKEN,
            transport=httpx.MockTransport(handler_empty_runs),
            dispatch_poll=empty_poll,
        ) as client,
        pytest.raises(RuntimeError, match="No workflow_dispatch run found"),
    ):
        client.dispatch_workflow("org/repo", "ci.yml", "main")
    assert empty_sleeps == [0.1]

    def handler_non_list_runs(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(204)
        return httpx.Response(200, json={"workflow_runs": None})

    single_poll = DispatchPollConfig(
        attempts=1,
        interval_seconds=0.1,
        sleep=lambda _s: pytest.fail("should not sleep on single attempt"),
        clock=lambda: datetime(2026, 11, 21, 10, 0, 0, tzinfo=UTC),
    )
    with (
        HttpxGitHubClient(
            _TEST_TOKEN,
            transport=httpx.MockTransport(handler_non_list_runs),
            dispatch_poll=single_poll,
        ) as client,
        pytest.raises(RuntimeError, match="No workflow_dispatch run found"),
    ):
        client.dispatch_workflow("org/repo", "ci.yml", "main")

    def handler_404(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Workflow not found"})

    with (
        HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler_404)) as client,
        pytest.raises(httpx.HTTPStatusError) as exc_info,
    ):
        client.dispatch_workflow("org/repo", "missing.yml", "main")
    assert exc_info.value.response.status_code == 404


def test_httpx_get_workflow_run_duration_and_fields() -> None:
    payloads: dict[int, dict[str, object]] = {
        1: {
            "id": 1,
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-11-21T10:00:00Z",
            "run_started_at": "2026-11-21T10:00:10Z",
            "updated_at": "2026-11-21T10:01:42Z",
            "html_url": "https://github.com/org/repo/actions/runs/1",
            "head_sha": "sha-1",
            "head_branch": "pc/cand-1",
        },
        2: {
            "id": 2,
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-11-21T10:00:00Z",
            "run_started_at": None,
            "updated_at": "2026-11-21T10:00:45Z",
            "html_url": "https://github.com/org/repo/actions/runs/2",
            "head_sha": "sha-2",
            "head_branch": None,
        },
        3: {
            "id": 3,
            "status": "queued",
            "conclusion": None,
            "created_at": "2026-11-21T10:00:00Z",
            "run_started_at": None,
            "updated_at": "2026-11-21T10:00:20Z",
            "html_url": "https://github.com/org/repo/actions/runs/3",
            "head_sha": "sha-3",
            "head_branch": "main",
        },
        4: {
            "id": 4,
            "status": "completed",
            "conclusion": "cancelled",
            "run_started_at": "2026-11-21T10:05:00Z",
            "updated_at": "2026-11-21T10:04:00Z",
            "html_url": "https://github.com/org/repo/actions/runs/4",
            "head_sha": "sha-4",
            "head_branch": "main",
        },
        5: {
            "id": 5,
            "status": "in_progress",
            "conclusion": None,
            "run_started_at": None,
            "created_at": None,
            "updated_at": None,
            "html_url": "https://github.com/org/repo/actions/runs/5",
            "head_sha": "sha-5",
            "head_branch": "main",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request)
        run_id = int(request.url.path.rsplit("/", maxsplit=1)[-1])
        if run_id in payloads:
            return httpx.Response(200, json=payloads[run_id])
        return httpx.Response(404, json={"message": "Not Found"})

    with HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler)) as client:
        assert client.get_workflow_run("org/repo", 1) == WorkflowRunResult(
            run_id=1,
            status="completed",
            conclusion="success",
            duration_seconds=92.0,
            html_url="https://github.com/org/repo/actions/runs/1",
            head_sha="sha-1",
            head_branch="pc/cand-1",
        )
        run2 = client.get_workflow_run("org/repo", 2)
        assert run2.duration_seconds == 45.0
        assert run2.head_branch == ""
        assert run2.conclusion == "failure"

        run3 = client.get_workflow_run("org/repo", 3)
        assert run3.duration_seconds == 0.0
        assert run3.conclusion is None

        run4 = client.get_workflow_run("org/repo", 4)
        assert run4.duration_seconds == 0.0

        run5 = client.get_workflow_run("org/repo", 5)
        assert run5.duration_seconds == 0.0

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.get_workflow_run("org/repo", 999)
        assert exc_info.value.response.status_code == 404


def test_httpx_open_pull_request_happy_and_error_paths() -> None:
    recorded: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request)
        body = json.loads(request.content) if request.content else None
        recorded.append((request.method, request.url.path, body))
        if isinstance(body, dict) and body.get("head") == "bad-branch":
            return httpx.Response(422, json={"message": "Validation Failed"})
        return httpx.Response(201, json={"html_url": "https://github.com/org/repo/pull/42"})

    with HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler)) as client:
        url = client.open_pull_request(
            "org/repo",
            title="Cache pip wheels",
            body="2.3x faster CI.",
            head="pc/cand-1",
            base="main",
        )
        assert url == "https://github.com/org/repo/pull/42"

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.open_pull_request(
                "org/repo",
                title="Broken PR",
                body="body",
                head="bad-branch",
                base="main",
            )
        assert exc_info.value.response.status_code == 422

    assert recorded[0] == (
        "POST",
        "/repos/org/repo/pulls",
        {
            "title": "Cache pip wheels",
            "body": "2.3x faster CI.",
            "head": "pc/cand-1",
            "base": "main",
        },
    )


@pytest.mark.parametrize(
    ("response_json", "expected_error"),
    [
        (["not", "a", "dict"], "Expected JSON object from GitHub API"),
        ({"default_branch": 123}, "Expected string for field 'default_branch'"),
    ],
)
def test_httpx_malformed_repo_responses_raise_type_error(
    response_json: object,
    expected_error: str,
) -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=response_json))
    with (
        HttpxGitHubClient(_TEST_TOKEN, transport=transport) as client,
        pytest.raises(TypeError, match=expected_error),
    ):
        client.get_default_branch_sha("org/repo")


def test_httpx_malformed_nested_and_typed_fields_raise_type_error() -> None:
    transport_bad_object = httpx.MockTransport(lambda _req: httpx.Response(201, json={"object": "oops"}))
    with (
        HttpxGitHubClient(_TEST_TOKEN, transport=transport_bad_object) as client,
        pytest.raises(TypeError, match="Expected JSON object for field 'object'"),
    ):
        client.create_branch("org/repo", "pc/x", "abc")

    transport_bool_id = httpx.MockTransport(lambda _req: httpx.Response(200, json={"workflow_run_id": True}))
    with (
        HttpxGitHubClient(_TEST_TOKEN, transport=transport_bool_id) as client,
        pytest.raises(TypeError, match="Expected integer for field 'workflow_run_id'"),
    ):
        client.dispatch_workflow("org/repo", "ci.yml", "main")

    transport_bad_conclusion = httpx.MockTransport(
        lambda _req: httpx.Response(
            200,
            json={
                "id": 1,
                "status": "completed",
                "conclusion": 999,
                "html_url": "https://github.com/org/repo/actions/runs/1",
                "head_sha": "abc",
                "head_branch": "main",
            },
        )
    )
    with (
        HttpxGitHubClient(_TEST_TOKEN, transport=transport_bad_conclusion) as client,
        pytest.raises(TypeError, match="Expected string or null for field 'conclusion'"),
    ):
        client.get_workflow_run("org/repo", 1)


def test_httpx_client_conforms_to_protocol_end_to_end() -> None:
    routes: dict[tuple[str, str], tuple[int, dict[str, object] | None]] = {
        ("GET", "/repos/org/repo"): (200, {"default_branch": "main"}),
        ("GET", "/repos/org/repo/git/ref/heads/main"): (200, {"object": {"sha": "base-sha-000"}}),
        ("POST", "/repos/org/repo/git/refs"): (201, {"object": {"sha": "base-sha-000"}}),
        ("GET", "/repos/org/repo/git/commits/base-sha-000"): (200, {"sha": "base-sha-000", "tree": {"sha": "tree-0"}}),
        ("POST", "/repos/org/repo/git/trees"): (201, {"sha": "tree-1"}),
        ("POST", "/repos/org/repo/git/commits"): (201, {"sha": "commit-0001"}),
        ("PATCH", "/repos/org/repo/git/refs/heads/pc/candidate-1"): (200, {"object": {"sha": "commit-0001"}}),
        ("POST", "/repos/org/repo/actions/workflows/ci.yml/dispatches"): (200, {"workflow_run_id": 101}),
        ("GET", "/repos/org/repo/actions/runs/101"): (
            200,
            {
                "id": 101,
                "status": "completed",
                "conclusion": "success",
                "run_started_at": "2026-11-21T10:00:00Z",
                "updated_at": "2026-11-21T10:00:42.500000Z",
                "html_url": "https://github.com/org/repo/actions/runs/101",
                "head_sha": "commit-0001",
                "head_branch": "pc/candidate-1",
            },
        ),
        ("POST", "/repos/org/repo/pulls"): (201, {"html_url": "https://github.com/org/repo/pull/1"}),
        ("DELETE", "/repos/org/repo/git/refs/heads/pc/candidate-1"): (204, None),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_github_headers(request)
        match = routes.get((request.method, request.url.path))
        if match is None:
            return httpx.Response(404)
        status, payload = match
        return httpx.Response(status) if payload is None else httpx.Response(status, json=payload)

    with HttpxGitHubClient(_TEST_TOKEN, transport=httpx.MockTransport(handler)) as concrete:
        client: GitHubClient = concrete
        assert isinstance(concrete, GitHubClient)
        head_sha, run_result, pr_url = _exercise_github_client(client, "org/repo")

    assert head_sha == "commit-0001"
    assert run_result == WorkflowRunResult(
        run_id=101,
        status="completed",
        conclusion="success",
        duration_seconds=42.5,
        html_url="https://github.com/org/repo/actions/runs/101",
        head_sha="commit-0001",
        head_branch="pc/candidate-1",
    )
    assert pr_url == "https://github.com/org/repo/pull/1"

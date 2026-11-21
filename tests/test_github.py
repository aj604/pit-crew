from dataclasses import FrozenInstanceError, fields

import pytest

from pit_crew.github import GitHubClient, WorkflowRunResult


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

"""An in-memory GitHub for seeder tests: repositories, commits, refs and Actions runs.

``FakeGitHubClient`` models state rather than replaying scripted responses, so seeding flows
(provisioning, baseline runs, and later red-to-green bug pairs) can be checked end to end offline.
Blob SHAs are real git-blob hashes, so workflow-hash assertions mean what they say.

Moving ``main`` starts a push run whose job conclusions come from ``ci`` (by default
:func:`template_ci`, a model of the template's ``ci.yml`` that ``test_benchmark_target`` checks
against the real job commands).
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from pit_crew.benchmark_seeder import BUG_PAIRS, DISPATCH_EVENT, PUSH_EVENT, WorkflowRunInfo, git_blob_sha

RunOutcome = Callable[["FakeRun"], str]
"""Decides a dispatched run's conclusion when it completes (``"success"``, ``"failure"``...)."""

PushCI = Callable[[Mapping[str, bytes]], dict[str, str]]
"""Job display name to conclusion for a push run of a commit with these files."""

CI_JOBS = ("Lint", "Typecheck", "Unit test", "UI check", "Smoke test")
_GREEN = dict.fromkeys(CI_JOBS, "success")

TEMPLATE_CI: dict[str, dict[str, str]] = {
    "clean": _GREEN,
    # Lint fails, so every job that needs it is skipped.
    "b1": {**dict.fromkeys(CI_JOBS, "skipped"), "Lint": "failure"},
    # Typecheck fails, so Unit test (which needs it) is skipped.
    "b2": {**_GREEN, "Typecheck": "failure", "Unit test": "skipped"},
    "b3": {**_GREEN, "Unit test": "failure"},
    # The badge bug also trips tests/unit/test_ui_check.py and the e2e suite Smoke test reruns.
    "b4": {**_GREEN, "UI check": "failure", "Unit test": "failure", "Smoke test": "failure"},
}
"""What the template's ``ci.yml`` concludes per job on c0 (``clean``) and with each bug applied."""


def template_ci(files: Mapping[str, bytes]) -> dict[str, str]:
    """The template's CI job conclusions for a commit: the row of the bug it carries, else ``clean``."""
    for pair in BUG_PAIRS:
        if pair.new.encode() in files.get(pair.path, b""):
            return dict(TEMPLATE_CI[pair.key])
    return dict(TEMPLATE_CI["clean"])


@dataclass
class FakeCommit:
    sha: str
    files: dict[str, bytes]
    message: str
    parents: list[str]


@dataclass
class FakeRun:
    id: int
    head_sha: str
    head_branch: str
    event: str
    path: str
    status: str = "queued"
    conclusion: str | None = None
    polls: int = 0
    jobs: dict[str, str] = field(default_factory=dict)

    def info(self) -> WorkflowRunInfo:
        return WorkflowRunInfo(
            self.id, self.head_sha, self.head_branch, self.event, self.path, self.status, self.conclusion
        )


@dataclass
class FakeRepo:
    default_branch: str
    private: bool = False
    refs: dict[str, str] = field(default_factory=dict)
    commits: dict[str, FakeCommit] = field(default_factory=dict)
    runs: list[FakeRun] = field(default_factory=list)


@dataclass
class FakeGitHubClient:
    """A ``GitHubClient`` held in memory.

    Dispatched runs stay invisible to ``list_workflow_runs`` for ``visible_after`` lists, then run
    for ``complete_after`` more lists before finishing with the conclusion ``outcome`` picks. Every
    mutating call is appended to ``writes``.
    """

    repos: dict[str, FakeRepo] = field(default_factory=dict)
    outcome: RunOutcome = lambda _run: "success"
    visible_after: int = 0
    complete_after: int = 1
    writes: list[tuple[str, ...]] = field(default_factory=list)
    filter_runs: bool = True
    """When false, ``list_workflow_runs`` ignores its filters, like a sloppy API page would."""
    ci: PushCI | None = template_ci
    """Job conclusions of the push run moving ``main`` starts; ``None`` starts no push runs."""
    _next_run: int = 1000
    _hidden: list[tuple[str, FakeRun, int]] = field(default_factory=list)

    # --- fixtures ---------------------------------------------------------------------------

    def add_repo(self, repo: str, files: Mapping[str, bytes] | None = None, *, branch: str = "main") -> str:
        """Create ``repo`` with one commit of ``files`` on ``branch`` and return its SHA."""
        self.repos[repo] = FakeRepo(default_branch=branch)
        sha = self._commit(repo, dict(files or {"README.md": b"# init\n"}), "Initial commit", [])
        self.repos[repo].refs[branch] = sha
        return sha

    def add_run(  # noqa: PLR0913  one keyword per run field
        self,
        repo: str,
        head_sha: str,
        *,
        conclusion: str | None = "success",
        event: str = DISPATCH_EVENT,
        branch: str = "main",
        path: str = ".github/workflows/ci.yml",
    ) -> FakeRun:
        """Add a run that already exists (completed when ``conclusion`` is set)."""
        run = FakeRun(self._run_id(), head_sha, branch, event, path)
        if conclusion is not None:
            run.status, run.conclusion = "completed", conclusion
        else:
            run.status = "in_progress"
        self.repos[repo].runs.append(run)
        return run

    def commits_touching(self, repo: str, prefix: str) -> list[FakeCommit]:
        """Commits reachable from any ref whose change versus their first parent touches ``prefix``."""
        state = self.repos[repo]
        touching: list[FakeCommit] = []
        seen: set[str] = set()
        stack = list(state.refs.values())
        while stack:
            sha = stack.pop()
            if sha in seen:
                continue
            seen.add(sha)
            commit = state.commits[sha]
            before = state.commits[commit.parents[0]].files if commit.parents else {}
            changed = {p for p in commit.files.keys() | before.keys() if commit.files.get(p) != before.get(p)}
            if any(p.startswith(prefix) for p in changed):
                touching.append(commit)
            stack.extend(commit.parents)
        return touching

    def writes_of(self, kind: str) -> list[tuple[str, ...]]:
        return [w for w in self.writes if w[0] == kind]

    # --- GitHubClient -----------------------------------------------------------------------

    def get_default_branch(self, repo: str) -> str | None:
        state = self.repos.get(repo)
        return None if state is None else state.default_branch

    def create_repo(self, repo: str, *, private: bool) -> None:
        self.writes.append(("create_repo", repo))
        self.add_repo(repo)
        self.repos[repo].private = private

    def get_ref(self, repo: str, branch: str) -> str | None:
        return self.repos[repo].refs.get(branch)

    def get_file_sha(self, repo: str, path: str, ref: str) -> str | None:
        state = self.repos[repo]
        commit = state.commits.get(state.refs.get(ref, ref))
        if commit is None or path not in commit.files:
            return None
        return git_blob_sha(commit.files[path])

    def create_commit(self, repo: str, files: Mapping[str, bytes], message: str, parents: Sequence[str]) -> str:
        state = self.repos[repo]
        for parent in parents:
            assert parent in state.commits, f"unknown parent {parent}"
        merged = {**state.commits[parents[0]].files, **files} if parents else dict(files)
        sha = self._commit(repo, merged, message, list(parents))
        self.writes.append(("create_commit", repo, sha))
        return sha

    def set_ref(self, repo: str, branch: str, sha: str, *, force: bool = False) -> None:
        state = self.repos[repo]
        assert sha in state.commits
        current = state.refs.get(branch)
        if not force and current is not None and current not in state.commits[sha].parents:
            msg = f"{repo}: non-fast-forward update on {branch} ({current} -> {sha})"
            raise RuntimeError(msg)
        self.writes.append(("set_ref", repo, branch, sha))
        state.refs[branch] = sha
        path = ".github/workflows/ci.yml"
        if self.ci is not None and branch == "main" and path in state.commits[sha].files:
            run = FakeRun(self._run_id(), sha, branch, PUSH_EVENT, path, jobs=self.ci(state.commits[sha].files))
            self._hidden.append((repo, run, self.visible_after))

    def set_default_branch(self, repo: str, branch: str) -> None:
        assert branch in self.repos[repo].refs
        self.writes.append(("set_default_branch", repo, branch))
        self.repos[repo].default_branch = branch

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> None:
        state = self.repos[repo]
        head = state.refs[ref]
        path = f".github/workflows/{workflow}"
        assert path in state.commits[head].files, f"{workflow} does not exist at {ref}"
        self.writes.append(("dispatch", repo, workflow, ref))
        self._hidden.append((repo, FakeRun(self._run_id(), head, ref, DISPATCH_EVENT, path), self.visible_after))

    def list_workflow_runs(self, repo: str, workflow: str, *, branch: str, event: str) -> list[WorkflowRunInfo]:
        self._advance(repo)
        path = f".github/workflows/{workflow}"
        state = self.repos[repo]
        return [
            r.info()
            for r in reversed(state.runs)
            if not self.filter_runs
            or (r.path.split("@", 1)[0] == path and r.head_branch == branch and r.event == event)
        ]

    def list_run_jobs(self, repo: str, run_id: int) -> dict[str, str | None]:
        (run,) = [r for r in self.repos[repo].runs if r.id == run_id]
        if run.status != "completed":
            return dict.fromkeys(run.jobs)
        return dict(run.jobs)

    def list_commits(self, repo: str, branch: str) -> list[tuple[str, str]]:
        state = self.repos[repo]
        history: list[tuple[str, str]] = []
        sha: str | None = state.refs[branch]
        while sha is not None:
            commit = state.commits[sha]
            history.append((sha, commit.message))
            sha = commit.parents[0] if commit.parents else None
        return history

    # --- internals --------------------------------------------------------------------------

    def _advance(self, repo: str) -> None:
        still_hidden = []
        for owner, run, wait in self._hidden:
            if owner != repo:
                still_hidden.append((owner, run, wait))
            elif wait > 0:
                still_hidden.append((owner, run, wait - 1))
            else:
                self.repos[repo].runs.append(run)
        self._hidden = still_hidden
        for run in self.repos[repo].runs:
            if run.status == "completed":
                continue
            if run.polls >= self.complete_after:
                run.status, run.conclusion = "completed", self._conclude(run)
            else:
                run.status = "in_progress"
                run.polls += 1

    def _conclude(self, run: FakeRun) -> str:
        if run.event == PUSH_EVENT and run.jobs:
            return "failure" if "failure" in run.jobs.values() else "success"
        return self.outcome(run)

    def _commit(self, repo: str, files: dict[str, bytes], message: str, parents: list[str]) -> str:
        digest = hashlib.sha1(usedforsecurity=False)
        for path in sorted(files):
            digest.update(path.encode() + b"\0" + git_blob_sha(files[path]).encode())
        digest.update(message.encode() + b"\0" + ",".join(parents).encode())
        sha = digest.hexdigest()
        self.repos[repo].commits[sha] = FakeCommit(sha, files, message, parents)
        return sha

    def _run_id(self) -> int:
        self._next_run += 1
        return self._next_run

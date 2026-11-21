"""Provision a benchmark target repository and seed its green baseline: ``pit-crew seed-target``.

The seeder resets ``main`` of the target repository to ``c0``, a parentless commit holding the
``benchmarks/target_template`` tree, then dispatches ``workflow_dispatch`` runs of ``ci.yml`` on
``main`` until at least N of them finished green. Every run it counts was made with the same
``.github/workflows/ci.yml`` blob (the *frozen workflow*), which is the invariant Analysis's
workflow-version filter relies on: nothing after ``c0`` may change ``.github/``.

Re-running is idempotent. A repository whose ``main`` already carries the frozen workflow is not
reset, and green frozen runs from earlier invocations count toward N, so only the shortfall is
dispatched. All GitHub access goes through the narrow :class:`GitHubClient` protocol so tests can
substitute an in-memory fake.
"""

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlencode

API = "https://api.github.com"
MAIN = "main"
WORKFLOW_FILE = "ci.yml"
WORKFLOW_PATH = f".github/workflows/{WORKFLOW_FILE}"
WORKFLOW_DIR = ".github/"
DISPATCH_EVENT = "workflow_dispatch"
C0_MESSAGE = "c0: benchmark target baseline"
DEFAULT_BASELINE_RUNS = 10
DEFAULT_TEMPLATE = Path(__file__).resolve().parents[2] / "benchmarks" / "target_template"

# Local clutter the template's own test runs leave behind; never part of c0.
_SKIP_DIRS = frozenset({"__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache", "ui-snapshots"})
_SKIP_SUFFIXES = (".pyc", ".pyo")

Provisioning = Literal["created", "reset", "existing"]


class SeedError(RuntimeError):
    """The target repository could not be provisioned or its baseline could not be seeded."""


class WorkflowDriftError(SeedError):
    """``main`` does not carry the frozen workflow blob it is supposed to."""

    def __init__(self, repo: str, expected: str, found: str | None) -> None:
        """Name the repository and the two blob hashes."""
        super().__init__(f"{repo}: {WORKFLOW_PATH} on {MAIN} is blob {found!r}, expected frozen blob {expected!r}")


class FrozenWorkflowEditError(SeedError):
    """A commit after ``c0`` tried to touch ``.github/``."""

    def __init__(self, paths: Iterable[str]) -> None:
        """List the offending paths."""
        super().__init__(f"the workflow is frozen after c0; refusing to change {sorted(paths)}")


class BaselineFailedError(SeedError):
    """Too many baseline runs on the frozen workflow finished red."""

    def __init__(self, repo: str, failed: Sequence[int]) -> None:
        """Name the failed run ids."""
        super().__init__(f"{repo}: baseline runs {list(failed)} did not succeed; the baseline must be green")


class BaselineTimeoutError(SeedError):
    """The baseline did not reach N green runs before the deadline."""

    def __init__(self, repo: str, green: int, wanted: int, timeout: float) -> None:
        """Say how far the baseline got."""
        super().__init__(f"{repo}: only {green}/{wanted} green baseline runs after {timeout:g}s")


@dataclass(frozen=True)
class WorkflowRunInfo:
    """The fields of one Actions workflow run the seeder needs."""

    id: int
    head_sha: str
    head_branch: str | None
    event: str
    path: str
    status: str
    conclusion: str | None

    @property
    def completed(self) -> bool:
        """Whether the run has finished, green or not."""
        return self.status == "completed"

    @property
    def green(self) -> bool:
        """Whether the run finished successfully."""
        return self.completed and self.conclusion == "success"


class GitHubClient(Protocol):
    """The GitHub operations seeding needs. ``repo`` is always ``owner/name``."""

    def get_default_branch(self, repo: str) -> str | None:
        """The repository's default branch, or ``None`` when the repository does not exist."""
        ...

    def create_repo(self, repo: str, *, private: bool) -> None:
        """Create the repository with one throwaway initial commit (the Git Data API rejects empty repos)."""
        ...

    def get_ref(self, repo: str, branch: str) -> str | None:
        """The commit SHA ``branch`` points at, or ``None`` when it does not exist."""
        ...

    def get_file_sha(self, repo: str, path: str, ref: str) -> str | None:
        """The git blob SHA of ``path`` at ``ref``, or ``None`` when either does not exist."""
        ...

    def create_commit(self, repo: str, files: Mapping[str, bytes], message: str, parents: Sequence[str]) -> str:
        """Create a commit and return its SHA without moving any ref.

        With no ``parents`` the tree is exactly ``files``; otherwise ``files`` are laid over the
        first parent's tree.
        """
        ...

    def set_ref(self, repo: str, branch: str, sha: str) -> None:
        """Point ``branch`` at ``sha``, creating or force-updating it."""
        ...

    def set_default_branch(self, repo: str, branch: str) -> None:
        """Make ``branch`` the repository's default branch."""
        ...

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> None:
        """Trigger one ``workflow_dispatch`` run of ``workflow`` (a file name) on ``ref``."""
        ...

    def list_workflow_runs(self, repo: str, workflow: str, *, branch: str, event: str) -> list[WorkflowRunInfo]:
        """Recent runs of ``workflow`` on ``branch`` triggered by ``event``, any status, newest first."""
        ...


def git_blob_sha(content: bytes) -> str:
    """The SHA-1 git assigns ``content`` as a blob, the same value GitHub reports for a file."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


def load_template(template: Path = DEFAULT_TEMPLATE) -> dict[str, bytes]:
    """Every file of the template that belongs in ``c0``, keyed by POSIX path, sorted."""
    if not (template / WORKFLOW_PATH).is_file():
        msg = f"{template} is not a benchmark target template: it has no {WORKFLOW_PATH}"
        raise SeedError(msg)
    files: dict[str, bytes] = {}
    for path in sorted(template.rglob("*")):
        rel = path.relative_to(template)
        if not path.is_file() or _SKIP_DIRS.intersection(rel.parts) or path.suffix in _SKIP_SUFFIXES:
            continue
        files[rel.as_posix()] = path.read_bytes()
    return files


def commit_on_main(client: GitHubClient, repo: str, files: Mapping[str, bytes], message: str) -> str:
    """Add a commit on top of ``main`` changing ``files`` and move ``main`` to it.

    The seam for later seeding steps (bug pairs). It refuses any change under ``.github/``, so
    the workflow stays frozen at its ``c0`` version.
    """
    if touched := [p for p in files if p.startswith(WORKFLOW_DIR)]:
        raise FrozenWorkflowEditError(touched)
    head = client.get_ref(repo, MAIN)
    if head is None:
        msg = f"{repo} has no {MAIN} branch; run seed-target first"
        raise SeedError(msg)
    sha = client.create_commit(repo, files, message, [head])
    client.set_ref(repo, MAIN, sha)
    return sha


def provision(
    client: GitHubClient, repo: str, files: Mapping[str, bytes], *, private: bool = False
) -> tuple[Provisioning, str]:
    """Make ``main`` carry the frozen workflow and return what was done and ``main``'s head.

    A repository whose ``main`` already has the frozen workflow blob is left untouched; anything
    else has ``main`` reset to a fresh ``c0`` built from ``files``.
    """
    frozen = git_blob_sha(files[WORKFLOW_PATH])
    default = client.get_default_branch(repo)
    action: Provisioning
    if default is None:
        client.create_repo(repo, private=private)
        action = "created"
    else:
        head = client.get_ref(repo, MAIN)
        action = (
            "existing" if head is not None and client.get_file_sha(repo, WORKFLOW_PATH, head) == frozen else "reset"
        )
    if action != "existing":
        client.set_ref(repo, MAIN, client.create_commit(repo, files, C0_MESSAGE, []))
    if default != MAIN:
        client.set_default_branch(repo, MAIN)
    head = client.get_ref(repo, MAIN)
    found = None if head is None else client.get_file_sha(repo, WORKFLOW_PATH, head)
    if head is None or found != frozen:
        raise WorkflowDriftError(repo, frozen, found)
    return action, head


@dataclass
class _Tally:
    """One poll of the baseline: which frozen dispatch runs are green, red, or still in flight."""

    green: list[int] = field(default_factory=list)
    failed_new: list[int] = field(default_factory=list)
    in_flight: int = 0
    appeared: int = 0


@dataclass
class BaselineWaiter:
    """Dispatch and await ``workflow_dispatch`` runs on ``main`` until ``runs`` are green on the frozen workflow."""

    client: GitHubClient
    repo: str
    frozen: str
    runs: int = DEFAULT_BASELINE_RUNS
    max_concurrent: int = 2
    max_failures: int = 3
    poll_seconds: float = 30.0
    timeout_seconds: float = 3600.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    dispatched: int = 0
    _frozen_at: dict[str, bool] = field(default_factory=dict)

    def wait(self) -> list[int]:
        """Return the ids of the green frozen baseline runs (at least ``runs`` of them), oldest first."""
        deadline = self.clock() + self.timeout_seconds
        initial: set[int] | None = None
        while True:
            listed = self._frozen_runs()
            if initial is None:
                initial = {r.id for r in listed}
            tally = self._tally(listed, initial)
            if len(tally.green) >= self.runs:
                return sorted(tally.green)
            if len(tally.failed_new) >= self.max_failures:
                raise BaselineFailedError(self.repo, tally.failed_new)
            if self.clock() >= deadline:
                raise BaselineTimeoutError(self.repo, len(tally.green), self.runs, self.timeout_seconds)
            pending = tally.in_flight + max(0, self.dispatched - tally.appeared)
            for _ in range(min(self.runs - len(tally.green) - pending, self.max_concurrent - pending)):
                self.client.dispatch_workflow(self.repo, WORKFLOW_FILE, MAIN)
                self.dispatched += 1
            self.sleep(self.poll_seconds)

    def _tally(self, listed: Sequence[WorkflowRunInfo], initial: set[int]) -> _Tally:
        tally = _Tally()
        for run in listed:
            new = run.id not in initial
            tally.appeared += new
            if run.green:
                tally.green.append(run.id)
            elif run.completed:
                if new:
                    tally.failed_new.append(run.id)
            else:
                tally.in_flight += 1
        return tally

    def _frozen_runs(self) -> list[WorkflowRunInfo]:
        """Dispatch runs of ``ci.yml`` on ``main`` whose commit carries the frozen workflow blob."""
        runs = self.client.list_workflow_runs(self.repo, WORKFLOW_FILE, branch=MAIN, event=DISPATCH_EVENT)
        return [
            r
            for r in runs
            if r.event == DISPATCH_EVENT
            and r.head_branch == MAIN
            and r.path.split("@", 1)[0] == WORKFLOW_PATH
            and self._is_frozen(r.head_sha)
        ]

    def _is_frozen(self, sha: str) -> bool:
        if sha not in self._frozen_at:
            self._frozen_at[sha] = self.client.get_file_sha(self.repo, WORKFLOW_PATH, sha) == self.frozen
        return self._frozen_at[sha]


@dataclass(frozen=True)
class SeedReport:
    """What ``seed_target`` did, printed by the CLI as JSON."""

    repo: str
    provisioning: Provisioning
    head_sha: str
    workflow_sha: str
    dispatched: int
    green_runs: list[int]

    def as_json(self) -> dict[str, Any]:
        """A JSON-ready summary."""
        return {
            "repo": self.repo,
            "provisioning": self.provisioning,
            "head_sha": self.head_sha,
            "workflow_sha": self.workflow_sha,
            "dispatched": self.dispatched,
            "green_runs": self.green_runs,
        }


def seed_target(  # noqa: PLR0913  keyword-only knobs mirror the CLI flags
    client: GitHubClient,
    repo: str,
    *,
    baseline_runs: int = DEFAULT_BASELINE_RUNS,
    template: Path = DEFAULT_TEMPLATE,
    private: bool = False,
    max_concurrent: int = 2,
    poll_seconds: float = 30.0,
    timeout_seconds: float = 3600.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SeedReport:
    """Provision ``repo`` from ``template`` and wait for ``baseline_runs`` green runs on the frozen workflow."""
    if baseline_runs < 1 or max_concurrent < 1:
        msg = "baseline_runs and max_concurrent must be at least 1"
        raise ValueError(msg)
    files = load_template(template)
    frozen = git_blob_sha(files[WORKFLOW_PATH])
    action, head = provision(client, repo, files, private=private)
    waiter = BaselineWaiter(
        client,
        repo,
        frozen,
        runs=baseline_runs,
        max_concurrent=max_concurrent,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
        sleep=sleep,
        clock=clock,
    )
    green = waiter.wait()
    final = client.get_ref(repo, MAIN)
    found = None if final is None else client.get_file_sha(repo, WORKFLOW_PATH, final)
    if found != frozen:
        raise WorkflowDriftError(repo, frozen, found)
    return SeedReport(repo, action, head, frozen, waiter.dispatched, green)


# --- The real client ---------------------------------------------------------------------------

Transport = Callable[[str, str, Mapping[str, Any] | None], tuple[int, Any]]
"""``(method, url, json_body) -> (status, json_response)``; non-2xx statuses are returned, not raised."""


class GitHubAPIError(SeedError):
    """The GitHub REST API answered with an unexpected status."""

    def __init__(self, method: str, url: str, status: int, body: Any) -> None:  # noqa: ANN401  any JSON body
        """Name the request and response."""
        super().__init__(f"{method} {url} -> {status}: {body!r}")


def urllib_transport(token: str) -> Transport:
    """A transport that sends ``token`` to the GitHub REST API, and only there."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pit-crew",
        "Authorization": f"Bearer {token}",
    }

    def send(method: str, url: str, body: Mapping[str, Any] | None) -> tuple[int, Any]:  # pragma: no cover  network
        if not url.startswith(f"{API}/"):
            msg = f"refusing to call {url!r}: not under {API}"
            raise ValueError(msg)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310  https only
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            raw = err.read()
            return err.code, json.loads(raw) if raw else None

    return send


_NOT_FOUND = 404
_EMPTY_REPO = 409


class RestGitHubClient:
    """:class:`GitHubClient` over the GitHub REST API."""

    def __init__(self, transport: Transport) -> None:
        """Send every request through ``transport``."""
        self._send = transport

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None, *, missing_ok: bool = False) -> Any:  # noqa: ANN401
        url = f"{API}{path}"
        status, payload = self._send(method, url, body)
        if missing_ok and status in {_NOT_FOUND, _EMPTY_REPO}:
            return None
        if not 200 <= status < 300:  # noqa: PLR2004  the 2xx range
            raise GitHubAPIError(method, url, status, payload)
        return payload

    def get_default_branch(self, repo: str) -> str | None:
        """The repository's default branch, or ``None`` when it does not exist."""
        body = self._call("GET", f"/repos/{repo}", missing_ok=True)
        return None if body is None else str(body["default_branch"])

    def create_repo(self, repo: str, *, private: bool) -> None:
        """Create ``repo`` under the authenticated user or, failing that, the owner organization."""
        owner, name = repo.split("/", 1)
        user = self._call("GET", "/user")
        path = "/user/repos" if user["login"] == owner else f"/orgs/{owner}/repos"
        self._call("POST", path, {"name": name, "private": private, "auto_init": True})

    def get_ref(self, repo: str, branch: str) -> str | None:
        """The commit ``branch`` points at, or ``None``."""
        body = self._call("GET", f"/repos/{repo}/git/ref/heads/{quote(branch)}", missing_ok=True)
        return None if body is None else str(body["object"]["sha"])

    def get_file_sha(self, repo: str, path: str, ref: str) -> str | None:
        """The blob SHA of ``path`` at ``ref``, or ``None``."""
        query = urlencode({"ref": ref})
        body = self._call("GET", f"/repos/{repo}/contents/{quote(path)}?{query}", missing_ok=True)
        return None if body is None or isinstance(body, list) else str(body["sha"])

    def create_commit(self, repo: str, files: Mapping[str, bytes], message: str, parents: Sequence[str]) -> str:
        """Upload ``files`` as base64 blobs (byte-exact) and commit them, over the first parent's tree if any."""
        tree = []
        for path, content in sorted(files.items()):
            blob = self._call(
                "POST",
                f"/repos/{repo}/git/blobs",
                {"content": base64.b64encode(content).decode(), "encoding": "base64"},
            )
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        request: dict[str, Any] = {"tree": tree}
        if parents:
            request["base_tree"] = self._call("GET", f"/repos/{repo}/git/commits/{parents[0]}")["tree"]["sha"]
        tree_sha = self._call("POST", f"/repos/{repo}/git/trees", request)["sha"]
        commit = self._call(
            "POST", f"/repos/{repo}/git/commits", {"message": message, "tree": tree_sha, "parents": list(parents)}
        )
        return str(commit["sha"])

    def set_ref(self, repo: str, branch: str, sha: str) -> None:
        """Force ``branch`` to ``sha``, creating it if needed."""
        if self.get_ref(repo, branch) is None:
            self._call("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
        else:
            self._call("PATCH", f"/repos/{repo}/git/refs/heads/{quote(branch)}", {"sha": sha, "force": True})

    def set_default_branch(self, repo: str, branch: str) -> None:
        """Make ``branch`` the default."""
        self._call("PATCH", f"/repos/{repo}", {"default_branch": branch})

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> None:
        """Trigger one ``workflow_dispatch`` run."""
        self._call("POST", f"/repos/{repo}/actions/workflows/{quote(workflow)}/dispatches", {"ref": ref})

    def list_workflow_runs(self, repo: str, workflow: str, *, branch: str, event: str) -> list[WorkflowRunInfo]:
        """The newest 100 runs of ``workflow`` on ``branch`` from ``event``."""
        query = urlencode({"branch": branch, "event": event, "per_page": 100})
        body = self._call("GET", f"/repos/{repo}/actions/workflows/{quote(workflow)}/runs?{query}")
        return [
            WorkflowRunInfo(
                id=int(r["id"]),
                head_sha=str(r["head_sha"]),
                head_branch=r.get("head_branch"),
                event=str(r["event"]),
                path=str(r.get("path", "")),
                status=str(r["status"]),
                conclusion=r.get("conclusion"),
            )
            for r in body["workflow_runs"]
        ]

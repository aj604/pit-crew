"""Pit Crew command line: ``pit-crew <command>`` (or ``python -m pit_crew.cli <command>``).

``smoke-lane`` runs one lane live against the Managed Agents API and checks it wrote its record
to a SQLite ledger. It is the Milestone 2 check that a lane works end to end; see docs/lane-guide.md.

``seed-target`` provisions a benchmark target repository from ``benchmarks/target_template`` and
waits for its green ``workflow_dispatch`` baseline runs; see ``pit_crew.benchmark_seeder``.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pit_crew import benchmark_seeder, managed_agent
from pit_crew.contracts import Candidate, Hypothesis, Record, ReplayPlan
from pit_crew.ledger import Ledger, SqliteLedger, normalize_repo

LANES = ("analysis", "optimizer", "proof")
EXPECTED_RECORD: dict[str, type[Record]] = {
    "analysis": Hypothesis,
    "optimizer": Candidate,
    "proof": ReplayPlan,
}


TOKEN_ENV = "GITHUB_TOKEN"  # noqa: S105  the variable name, not a secret
WORKSPACE_ENV = "PIT_CREW_WORKSPACE"
DEFAULT_WORKSPACE = "/workspace"


class MissingWorkspaceError(FileNotFoundError):
    """Raised when the analysis lane's local checkout (read by profile_ci) is not a directory."""

    def __init__(self, path: Path) -> None:
        """Name the missing path and how to point the CLI at a checkout."""
        super().__init__(
            f"analysis reads the workflow from a local checkout, but {str(path)!r} is not a directory; "
            f"pass --workspace <path> or set {WORKSPACE_ENV}"
        )


class MissingPrerequisiteError(LookupError):
    """Raised when a lane's default prompt needs an upstream record the ledger does not have."""

    def __init__(self, lane: str, kind: str, repo: str) -> None:
        """Name the lane, the record it needs, and the repo searched."""
        super().__init__(
            f"lane {lane!r} needs a {kind} for {repo!r} in the ledger; run the previous lane first or pass --task"
        )


def default_task(lane: str, repo: str, ledger: Ledger) -> str:
    """The built-in smoke prompt for ``lane``, filled in from the newest upstream record for ``repo``."""
    if lane == "analysis":
        return (
            f"Repository: {repo} (checked out at /workspace).\n"
            "Profile its CI, find the single highest-leverage bottleneck, and record it with record_hypothesis."
        )
    if lane == "optimizer":
        hypotheses = ledger.hypotheses_for_repo(repo)
        if not hypotheses:
            raise MissingPrerequisiteError(lane, "Hypothesis", repo)
        return (
            f"Repository: {repo} (checked out at /workspace).\n"
            f"Hypothesis id: {hypotheses[-1].id}\n"
            "Implement this hypothesis and report it with report_candidate."
        )
    repo_key = normalize_repo(repo)
    candidates = [c for c in ledger.all(Candidate) if normalize_repo(c.repo) == repo_key]
    if not candidates:
        raise MissingPrerequisiteError(lane, "Candidate", repo)
    candidate = candidates[-1]
    return (
        f"Repository: {repo} (candidate base checked out at /workspace).\n"
        f"Candidate id: {candidate.id}\n"
        f"Branch: {candidate.branch}\n"
        f"Summary: {candidate.summary}\n"
        f"Changed files (path -> new text):\n{json.dumps(candidate.files, indent=2)}\n"
        "No required replays or uncovered checks were precomputed. Use the repository's git history for "
        "commits that broke CI, or propose mutants for the checks this change touches, then call plan_replays."
    )


@contextmanager
def _workspace_env(workspace: str | None) -> Iterator[None]:
    """Set ``PIT_CREW_WORKSPACE`` to ``workspace`` for the block and restore the previous value after."""
    if workspace is None:
        yield
        return
    previous = os.environ.get(WORKSPACE_ENV)
    os.environ[WORKSPACE_ENV] = workspace
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(WORKSPACE_ENV, None)
        else:
            os.environ[WORKSPACE_ENV] = previous


def _check_workspace(lane: str, workspace: str | None) -> None:
    """Fail fast when ``analysis`` would read its workflow from a checkout that is not there."""
    if lane != "analysis":
        return
    path = Path(workspace if workspace is not None else os.environ.get(WORKSPACE_ENV, DEFAULT_WORKSPACE))
    if not path.is_dir():
        raise MissingWorkspaceError(path)


def smoke_lane(  # noqa: PLR0913  each keyword mirrors one smoke-lane flag
    lane: str,
    repo: str,
    ledger: Ledger | str | Path,
    task: str | None = None,
    *,
    workspace: str | None = None,
    client: managed_agent.InteractionsClientLike | None = None,
) -> tuple[dict[str, Any], bool]:
    """Run ``lane`` once and return its JSON summary and whether it passed.

    ``ledger`` is a Ledger or the path of a SQLite ledger to open. Opening and reading it happen inside
    the same error boundary as the run, so a bad ledger is reported in the summary like any other failure.
    ``workspace`` is the local checkout the analysis tools read (``PIT_CREW_WORKSPACE`` while the lane runs).
    """
    kind = EXPECTED_RECORD[lane]
    started = time.monotonic()
    summary: dict[str, Any] = {"lane": lane, "interaction_id": None, "status": "error"}
    records_written = 0
    try:
        opened = SqliteLedger(ledger) if isinstance(ledger, (str, Path)) else ledger
        before = len(opened.all(kind))
        try:
            _check_workspace(lane, workspace)
            prompt = task if task is not None else default_task(lane, repo, opened)
            with _workspace_env(workspace):
                interaction = managed_agent.run_lane(lane, prompt, opened, repo=repo, client=client)
        finally:
            records_written = len(opened.all(kind)) - before
    except Exception as exc:  # noqa: BLE001  a smoke check reports every failure as JSON instead of a traceback
        summary["error"] = f"{type(exc).__name__}: {exc}"
    else:
        summary["interaction_id"] = interaction.id
        summary["status"] = interaction.status
    summary["expected_record"] = kind.__name__
    summary["records_written"] = records_written
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return summary, summary["status"] == "completed" and records_written > 0


class MissingTokenError(LookupError):
    """Raised when ``seed-target`` has no GitHub token to talk to the API with."""

    def __init__(self) -> None:
        """Say which variable to set."""
        super().__init__(f"seed-target needs a GitHub token with repo and workflow scopes in ${TOKEN_ENV}")


def _rest_client() -> benchmark_seeder.GitHubClient:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise MissingTokenError
    return benchmark_seeder.RestGitHubClient(benchmark_seeder.urllib_transport(token))


def seed_target(
    args: argparse.Namespace, github: benchmark_seeder.GitHubClient | None = None
) -> tuple[dict[str, Any], bool]:
    """Run ``seed-target`` from parsed flags and return its JSON summary and whether it succeeded."""
    summary: dict[str, Any] = {"command": "seed-target", "repo": args.repo, "status": "error"}
    try:
        github = github if github is not None else _rest_client()
        report = benchmark_seeder.seed_target(
            github,
            args.repo,
            baseline_runs=args.baseline_runs,
            template=Path(args.template),
            private=args.private,
            max_concurrent=args.max_concurrent,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout,
        )
    except Exception as exc:  # noqa: BLE001  report every failure as JSON instead of a traceback
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary, False
    return {**summary, **report.as_json(), "status": "seeded"}, True


def _repo(value: str) -> str:
    owner, _, name = value.partition("/")
    if not owner or not name or "/" in name:
        msg = f"expected owner/repo, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        msg = f"must be at least 1, got {number}"
        raise argparse.ArgumentTypeError(msg)
    return number


def _add_seed_target(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    seed = commands.add_parser("seed-target", help="provision a benchmark target repo and seed its green baseline")
    seed.add_argument("--repo", required=True, type=_repo, help="target repository, owner/name")
    seed.add_argument(
        "--baseline-runs",
        type=_positive_int,
        default=benchmark_seeder.DEFAULT_BASELINE_RUNS,
        help="green workflow_dispatch runs on main to wait for (default: %(default)s)",
    )
    seed.add_argument(
        "--template", default=str(benchmark_seeder.DEFAULT_TEMPLATE), help="template directory to commit as c0"
    )
    seed.add_argument("--private", action="store_true", help="create the repository as private if it is missing")
    seed.add_argument("--max-concurrent", type=_positive_int, default=2, help="baseline runs in flight at once")
    seed.add_argument("--poll-seconds", type=float, default=30.0, help="seconds between run-status polls")
    seed.add_argument("--timeout", type=float, default=3600.0, help="seconds to wait for the baseline")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pit-crew", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    smoke = commands.add_parser("smoke-lane", help="run one lane live and check it wrote its record")
    smoke.add_argument("--lane", required=True, choices=LANES)
    smoke.add_argument("--repo", required=True, help="repository to mount, e.g. owner/name")
    smoke.add_argument("--ledger", required=True, help="path to the SQLite ledger (created if missing)")
    smoke.add_argument("--task", help="prompt to send instead of the lane's built-in default")
    smoke.add_argument(
        "--workspace",
        help=f"local checkout of --repo that analysis tools read (default: ${WORKSPACE_ENV} or {DEFAULT_WORKSPACE})",
    )
    _add_seed_target(commands)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client: managed_agent.InteractionsClientLike | None = None,
    github: benchmark_seeder.GitHubClient | None = None,
) -> int:
    """Run the CLI and return its exit code: 0 when the command succeeded.

    ``client`` replaces the Managed Agents client for ``smoke-lane``; ``github`` replaces the
    GitHub client for ``seed-target``.
    """
    args = _parser().parse_args(argv)
    if args.command == "seed-target":
        summary, passed = seed_target(args, github)
    else:
        summary, passed = smoke_lane(
            args.lane, args.repo, args.ledger, args.task, workspace=args.workspace, client=client
        )
    sys.stdout.write(json.dumps(summary) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

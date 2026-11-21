"""dep-cache: dependency installs on the critical path that nothing caches."""

import re

from pit_crew.lanes.analysis.critical_path import CIProfile
from pit_crew.lanes.analysis.detectors.base import Finding, job_profile, keep_worthwhile, step_profile
from pit_crew.lanes.analysis.workflow import JobSpec, StepSpec, Workflow

INSTALL_COMMANDS = re.compile(
    r"(?<![\w./-])("
    r"npm (ci|install)|pnpm (i|install)|yarn install|bun install|pip install"
    r"|uv (sync|pip install)|poetry install|cargo fetch|go mod download|bundle install"
    r"|yarn(?=\s*($|&&|;|\|\||\s-))"  # bare `yarn` is an install; `yarn test` is not
    r")(?![\w-])",
    re.MULTILINE,
)
SAVED_FRACTION = 0.7  # share of the install a warm cache typically removes
CONFIDENT_RUNS = 20  # runs needed before the estimate is trusted


_CACHE_INPUT_VALUES = {  # `cache:` values of setup actions whose value names the manager it caches
    "setup-node": frozenset({"npm", "pnpm", "yarn"}),
    "setup-python": frozenset({"pip", "poetry", "pipenv"}),
}
# Directories (as consecutive path components) a generic `actions/cache` must include to cover a manager.
_CACHE_DIRS: dict[str, tuple[tuple[str, ...], ...]] = {
    "npm": ((".npm",), ("node_modules",)),
    "pnpm": (("pnpm-store",), (".pnpm-store",), ("node_modules",)),
    "yarn": ((".yarn",), ("yarn",), ("node_modules",)),
    "bun": ((".bun",), ("node_modules",)),
    "pip": ((".cache", "pip"), ("pip",)),
    "uv": ((".cache", "uv"), ("uv",)),
    "poetry": (("pypoetry",), (".cache", "pypoetry")),
    "cargo": ((".cargo",), ("target",)),
    "go": (("go", "pkg", "mod"), ("go-build",)),
    "bundle": (("vendor", "bundle"),),
}
_GENERIC_CACHE_ACTIONS = frozenset({"actions/cache", "actions/cache/restore"})


def _managers(command: str) -> set[str]:
    """The package managers whose install commands appear in a shell command."""
    managers = set()
    for match in INSTALL_COMMANDS.finditer(command):
        managers.add(match.group(1).split()[0])
    return managers


def _path_covers(path: str, manager: str) -> bool:
    for line in path.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("!"):
            continue
        parts = [p for p in entry.split("/") if p]
        for hint in _CACHE_DIRS[manager]:
            width = len(hint)
            if any(tuple(parts[i : i + width]) == hint for i in range(len(parts) - width + 1)):
                return True
    return False


def _setup_covered(name: str, with_: dict[str, str]) -> set[str]:
    """Managers a `setup-*` action's built-in cache covers."""
    cache = with_.get("cache", "").strip().lower()
    managers: set[str] = set()
    if cache in _CACHE_INPUT_VALUES.get(name, frozenset()):
        managers.add(cache)
    elif name == "setup-uv" and with_.get("enable-cache", "").strip().lower() == "true":
        managers.add("uv")
    elif name == "setup-go" and cache not in {"", "false"}:
        managers.add("go")
    elif name == "setup-ruby" and with_.get("bundler-cache", "").strip().lower() == "true":
        managers.add("bundle")
    return managers


def _covered(step: StepSpec) -> set[str]:
    """The package managers whose dependencies this step caches."""
    action = (step.uses or "").split("@", 1)[0].lower()
    if action in _GENERIC_CACHE_ACTIONS:
        path = step.with_.get("path", "")
        return {m for m in _CACHE_DIRS if _path_covers(path, m)}
    if action == "swatinem/rust-cache":
        return {"cargo"}
    return _setup_covered(action.rsplit("/", 1)[-1], step.with_)


def _job_covered(job: JobSpec) -> set[str]:
    covered: set[str] = set()
    for step in job.steps:
        covered |= _covered(step)
    return covered


def detect_dep_cache(profile: CIProfile, workflow: Workflow, workflow_path: str) -> list[Finding]:
    """One finding per uncached install step in a job that sits on the critical path."""
    findings: list[Finding] = []
    for job in workflow.jobs.values():
        jp = job_profile(profile, job.id)
        if jp is None or jp.max_saving_seconds <= 0:
            continue
        covered = _job_covered(job)
        for step in job.steps:
            if step.run is None or not _managers(step.run) - covered:
                continue
            sp = step_profile(profile, job.id, step)
            if sp is None:
                continue
            findings.append(
                Finding(
                    skill="dep-cache",
                    workflow_path=workflow_path,
                    job=job.id,
                    step=step.name,
                    bucket="setup",
                    description=f"'{step.name}' installs dependencies on the critical path with no cache.",
                    expected_savings_seconds=min(SAVED_FRACTION * sp.median_seconds, jp.max_saving_seconds),
                    evidence={
                        "step_p50_seconds": sp.median_seconds,
                        "step_p95_seconds": sp.p95_seconds,
                        "max_saving_seconds": jp.max_saving_seconds,
                        "runs_sampled": sp.samples,
                    },
                    confidence=0.7 if profile.runs_analyzed >= CONFIDENT_RUNS else 0.4,
                    risk="low",
                )
            )
    return keep_worthwhile(findings)

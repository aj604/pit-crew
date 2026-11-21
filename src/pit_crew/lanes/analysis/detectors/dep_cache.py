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


def _caches(step: StepSpec) -> bool:
    uses = step.uses or ""
    if uses.startswith("actions/cache"):
        return True
    if not uses.split("@", 1)[0].rsplit("/", 1)[-1].startswith("setup-"):
        return False
    cache = step.with_.get("cache", "").strip().lower()
    return cache not in {"", "false"} or step.with_.get("enable-cache", "").lower() == "true"


def _job_caches(job: JobSpec) -> bool:
    return any(_caches(s) for s in job.steps)


def detect_dep_cache(profile: CIProfile, workflow: Workflow, workflow_path: str) -> list[Finding]:
    """One finding per uncached install step in a job that sits on the critical path."""
    findings: list[Finding] = []
    for job in workflow.jobs.values():
        jp = job_profile(profile, job.id)
        if jp is None or jp.max_saving_seconds <= 0 or _job_caches(job):
            continue
        for step in job.steps:
            if step.run is None or not INSTALL_COMMANDS.search(step.run):
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

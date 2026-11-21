"""Work out which CI checks a candidate's change can affect.

When in doubt this answers "every check" (None): proving too much costs replay minutes,
proving too little lets a change hide a bug. Workflows are compared as parsed YAML, so
comments and formatting never count as a change.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypedDict

import yaml

from pit_crew.lanes.proof.miner import check_key

_WORKFLOWS = ".github/workflows"  # GitHub runs only the files directly in here

Check = frozenset[str]
"""One job's check keys: by job id and by display name, and across a change, old and new.

CI history names a job by its display name, so a check counts as covered when a bug was
recorded under any one of its keys.
"""


class FileChange(TypedDict):
    """One file in the candidate's diff; before is None when added, after is None when deleted."""

    path: str
    before: str | None
    after: str | None


@dataclass(frozen=True)
class Workflow:
    """A parsed workflow: its name, every top-level key but `jobs`, and each job's body."""

    name: str
    header: dict[str, Any] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def needs(self, job_id: str) -> tuple[str, ...]:
        """Return the jobs `job_id` waits for."""
        needs = self.jobs.get(job_id, {}).get("needs") or ()
        return (needs,) if isinstance(needs, str) else tuple(needs)

    def keys(self, job_id: str) -> Check:
        """Return the job's check keys, by id and by display name, or none if there is no such job."""
        if job_id not in self.jobs:
            return frozenset()
        name = self.jobs[job_id].get("name")
        return frozenset(
            {check_key(self.name, job_id), check_key(self.name, name if isinstance(name, str) and name else job_id)}
        )


def parse_workflow(text: str, path: str) -> Workflow | None:
    """Parse a workflow file, or return None if it is not YAML with a mapping of well-formed jobs."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict) or not _is_jobs(doc.get("jobs")):
        return None
    name = doc.get("name")
    return Workflow(
        name=name if isinstance(name, str) and name else path,
        # YAML 1.1 reads the key `on` as true, so it is named back here.
        header={("on" if k is True else str(k)): v for k, v in doc.items() if k != "jobs"},
        jobs=doc["jobs"],
    )


def touched_checks(changes: Iterable[FileChange]) -> set[Check] | None:
    """Return the checks a change can affect, or None for every check.

    A changed file that is not a workflow directly in .github/workflows/ (a script, a test
    config, a lockfile, a composite action) can affect any check, so it answers None, as
    does a workflow that won't parse. So does a reusable workflow (`on: workflow_call`):
    its jobs report under each caller's name, as "<caller job> / <job>", which this file
    alone cannot say. A change outside `jobs:` (triggers, env, permissions) touches every
    job in that workflow. A removed job is reported as touched, so its old bugs are
    replayed and the removal fails.
    """
    touched: set[Check] = set()
    for change in changes:
        path = change["path"]
        if not _is_workflow(path):
            return None
        before = _side(change["before"], path)
        after = _side(change["after"], path)
        if before is None or after is None or _is_reusable(before) or _is_reusable(after):
            return None
        every_job = before.jobs.keys() | after.jobs.keys()
        if before.header == after.header:
            changed = {j for j in every_job if before.jobs.get(j) != after.jobs.get(j)}
        else:
            changed = set(every_job)
        changed = _with_dependents(changed, before, after)
        # A renamed job or workflow is one check, so its old and new spellings go together.
        touched |= {before.keys(job_id) | after.keys(job_id) for job_id in changed}
    return touched


def all_checks(workflows: Mapping[str, str]) -> set[Check] | None:
    """Return every check defined by these workflow files (path -> text), or None if one won't parse."""
    checks: set[Check] = set()
    for path, text in workflows.items():
        workflow = parse_workflow(text, path)
        if workflow is None:
            return None
        checks |= {workflow.keys(job_id) for job_id in workflow.jobs}
    return checks


def _side(text: str | None, path: str) -> Workflow | None:
    return Workflow(path) if text is None else parse_workflow(text, path)


def _is_reusable(workflow: Workflow) -> bool:
    on = workflow.header.get("on")
    events = {on} if isinstance(on, str) else set(on) if isinstance(on, list | dict) else set()
    return "workflow_call" in events


def _is_workflow(path: str) -> bool:
    folder, _, file = path.rpartition("/")
    return folder == _WORKFLOWS and file.endswith((".yml", ".yaml"))


def _is_jobs(jobs: object) -> bool:
    return isinstance(jobs, dict) and all(isinstance(k, str) and _is_job(v) for k, v in jobs.items())


def _is_job(job: object) -> bool:
    if not isinstance(job, dict):
        return False
    needs = job.get("needs")
    return (
        needs is None or isinstance(needs, str) or (isinstance(needs, list) and all(isinstance(n, str) for n in needs))
    )


def _with_dependents(changed: set[str], *workflows: Workflow) -> set[str]:
    """A job that needs a changed job sees its outputs, artifacts and skip status change too."""
    out = set(changed)
    grew = True
    while grew:
        grew = False
        for workflow in workflows:
            for job_id in workflow.jobs.keys() - out:
                if out.intersection(workflow.needs(job_id)):
                    out.add(job_id)
                    grew = True
    return out

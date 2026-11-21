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

_WORKFLOWS = ".github/workflows/"


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
        return (needs,) if isinstance(needs, str) else tuple(str(n) for n in needs)

    def keys(self, job_ids: Iterable[str]) -> set[str]:
        """Return check keys for these jobs, by id and by display name, so either spelling matches."""
        out = set()
        for job_id in set(job_ids) & self.jobs.keys():
            name = self.jobs[job_id].get("name")
            out.add(check_key(self.name, job_id))
            out.add(check_key(self.name, name if isinstance(name, str) and name else job_id))
        return out


def parse_workflow(text: str, path: str) -> Workflow | None:
    """Parse a workflow file, or return None if it is not YAML with a mapping of jobs."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict) or not _is_jobs(doc.get("jobs")):
        return None
    name = doc.get("name")
    return Workflow(
        name=name if isinstance(name, str) and name else path,
        header={str(k): v for k, v in doc.items() if k != "jobs"},
        jobs=doc["jobs"],
    )


def touched_checks(changes: Iterable[FileChange]) -> set[str] | None:
    """Return the check keys a change can affect, or None for every check.

    A changed file outside .github/workflows/ (a script, a test config, a lockfile, a
    composite action) can affect any check, so it answers None, as does a workflow that
    won't parse. A change outside `jobs:` (triggers, env, permissions) touches every job in
    that workflow. A removed job is reported as touched, so its old bugs are replayed and
    the removal fails.
    """
    touched: set[str] = set()
    for change in changes:
        path = change["path"]
        if not (path.startswith(_WORKFLOWS) and path.endswith((".yml", ".yaml"))):
            return None
        before = _side(change["before"], path)
        after = _side(change["after"], path)
        if before is None or after is None:
            return None
        every_job = before.jobs.keys() | after.jobs.keys()
        if before.header == after.header:
            changed = {j for j in every_job if before.jobs.get(j) != after.jobs.get(j)}
        else:
            changed = set(every_job)
        changed = _with_dependents(changed, before, after)
        touched |= before.keys(changed) | after.keys(changed)
    return touched


def all_checks(workflows: Mapping[str, str]) -> set[str] | None:
    """Return every check key defined by these workflow files (path -> text), or None if one won't parse."""
    keys: set[str] = set()
    for path, text in workflows.items():
        workflow = parse_workflow(text, path)
        if workflow is None:
            return None
        keys |= workflow.keys(workflow.jobs)
    return keys


def _side(text: str | None, path: str) -> Workflow | None:
    return Workflow(path) if text is None else parse_workflow(text, path)


def _is_jobs(jobs: object) -> bool:
    return isinstance(jobs, dict) and all(isinstance(k, str) and isinstance(v, dict) for k, v in jobs.items())


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

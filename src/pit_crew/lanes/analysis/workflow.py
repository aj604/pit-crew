"""Read a workflow file's job graph and map GitHub's job display names back to job ids.

The API reports jobs by display name ("test (ubuntu-latest, 3.12)"), while `needs:` refers
to job ids ("test"). Analysis needs both sides joined to build the dependency graph.
"""

import re
from dataclasses import dataclass

import yaml

_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


@dataclass(frozen=True)
class JobSpec:
    """One entry under `jobs:` in a workflow file."""

    id: str
    name: str  # the `name:` field, or the id when there is none; may contain ${{ }} expressions
    needs: tuple[str, ...]
    has_matrix: bool
    uses: str | None  # set when the job calls a reusable workflow


@dataclass(frozen=True)
class Workflow:
    """A workflow file's name and jobs, keyed by job id."""

    name: str
    jobs: dict[str, JobSpec]

    def job_id_for(self, display_name: str) -> str | None:
        """The job id a display name from the API belongs to, or None if it matches no job."""
        for job in self.jobs.values():
            if _matches(job, display_name):
                return job.id
        caller = display_name.split(" / ", 1)[0]  # reusable workflows report "caller / inner job"
        if caller != display_name:
            return self.job_id_for(caller)
        return None


def parse_workflow(text: str) -> Workflow:
    """Parse workflow YAML. Raises ValueError when a job needs a job that does not exist."""
    doc = yaml.safe_load(text) or {}
    jobs = {}
    for job_id, raw_body in (doc.get("jobs") or {}).items():
        body = raw_body or {}
        needs = body.get("needs") or ()
        jobs[job_id] = JobSpec(
            id=job_id,
            name=str(body.get("name") or job_id),
            needs=(needs,) if isinstance(needs, str) else tuple(needs),
            has_matrix=bool((body.get("strategy") or {}).get("matrix")),
            uses=body.get("uses"),
        )
    for job in jobs.values():
        unknown = [n for n in job.needs if n not in jobs]
        if unknown:
            msg = f"job {job.id!r} needs unknown job(s) {unknown}"
            raise ValueError(msg)
    return Workflow(name=str(doc.get("name") or ""), jobs=jobs)


def topological_order(workflow: Workflow) -> list[str]:
    """Job ids with every job after the jobs it needs. Raises ValueError on a cycle."""
    order: list[str] = []
    state: dict[str, str] = {}  # "visiting" or "done"

    def visit(job_id: str, path: tuple[str, ...]) -> None:
        if state.get(job_id) == "done":
            return
        if state.get(job_id) == "visiting":
            msg = f"needs cycle: {' -> '.join((*path, job_id))}"
            raise ValueError(msg)
        state[job_id] = "visiting"
        for dep in workflow.jobs[job_id].needs:
            visit(dep, (*path, job_id))
        state[job_id] = "done"
        order.append(job_id)

    for job_id in workflow.jobs:
        visit(job_id, ())
    return order


def _matches(job: JobSpec, display_name: str) -> bool:
    if display_name == job.name:
        return True
    pattern = ".+?".join(re.escape(part) for part in _EXPRESSION.split(job.name))
    if job.has_matrix and not _EXPRESSION.search(job.name):
        pattern += r"( \(.*\))?"  # GitHub appends the matrix values when the name does not use them
    return re.fullmatch(pattern, display_name) is not None

"""parallelize-dag: find `needs:` edges that make independent jobs wait for each other."""

import re
from dataclasses import replace

from pit_crew.lanes.analysis.critical_path import CIProfile, critical_path
from pit_crew.lanes.analysis.detectors.base import Finding, keep_worthwhile
from pit_crew.lanes.analysis.workflow import JobSpec, Workflow

_UPLOAD = "actions/upload-artifact"
_DOWNLOAD = "actions/download-artifact"
_CONFIDENCE = 0.6
_SEGMENT_SEPARATOR = re.compile(r"&&|\|\||[;|&]")  # shell command separators; alternation of fixed tokens, linear
_TRIVIAL_COMMANDS = frozenset({"echo", "exit", "true"})


def _trivial_run(run: str) -> bool:
    """True when every shell segment of every line starts with a trivial command; substitutions are never trivial."""
    for raw in run.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "$(" in line or "`" in line:
            return False
        for segment in _SEGMENT_SEPARATOR.split(line):
            words = segment.split(maxsplit=1)
            if words and words[0].lower() not in _TRIVIAL_COMMANDS:
                return False
    return True


def _only_waits(job: JobSpec) -> bool:
    """True for a job that does no work of its own (echo, exit, true or nothing): it exists to wait, as a gate."""
    if job.uses is not None or not job.steps:  # a reusable-workflow call does real work; a step-less job is a stub
        return False
    return all(s.uses is None and (s.run is None or _trivial_run(s.run)) for s in job.steps)


def _uses(job: JobSpec, action: str) -> bool:
    return any(s.uses is not None and s.uses.startswith(action) for s in job.steps)


def _removable(dep: JobSpec, job: JobSpec) -> bool:
    """False when the job reads the dependency's outputs, downloads what it uploads, or only waits on it."""
    if _only_waits(job):
        return False
    if dep.id in job.needs_refs:
        return False
    return not (_uses(dep, _UPLOAD) and _uses(job, _DOWNLOAD))


def _ancestors(jobs: dict[str, JobSpec], start: str, cut: tuple[str, str]) -> set[str]:
    """Every job `start` transitively needs, ignoring the one edge `cut` (job, dependency). Linear BFS."""
    seen: set[str] = set()
    queue = [start]
    while queue:
        current = queue.pop()
        for need in jobs[current].needs:
            if (current, need) != cut and need not in seen:
                seen.add(need)
                queue.append(need)
    return seen


def _loses_artifact_handoff(workflow: Workflow, dep_id: str, job_id: str) -> bool:
    """True when cutting `job.needs: dep` strands a downloading job (job or a descendant) from an uploader."""
    jobs = workflow.jobs
    uploaders = {i for i, j in jobs.items() if _uses(j, _UPLOAD)}
    if not uploaders:
        return False
    no_cut = ("", "")
    for downloader_id, downloader in jobs.items():
        if not _uses(downloader, _DOWNLOAD):
            continue
        before = _ancestors(jobs, downloader_id, no_cut)
        if downloader_id != job_id and job_id not in before:
            continue  # the cut sits upstream of neither this job nor anything it needs
        after = _ancestors(jobs, downloader_id, (job_id, dep_id))
        if (before - after) & uploaders:
            return True
    return False


def detect_parallelize_dag(profile: CIProfile, workflow: Workflow, workflow_path: str) -> list[Finding]:
    """Flag critical-path `needs:` edges that carry no data, so removing them lets both jobs run at once."""
    seconds = {j.job_id: j.median_seconds for j in profile.jobs}
    on_path = set(profile.critical_path.jobs)
    before = critical_path(workflow, seconds).seconds
    findings = []
    for job_id in sorted(on_path & workflow.jobs.keys()):
        job = workflow.jobs[job_id]
        for dep_id in job.needs:
            dep = workflow.jobs.get(dep_id)
            if dep is None or dep_id not in on_path or not _removable(dep, job):
                continue
            if _loses_artifact_handoff(workflow, dep_id, job_id):
                continue
            trimmed = replace(job, needs=tuple(n for n in job.needs if n != dep_id))
            after = critical_path(replace(workflow, jobs={**workflow.jobs, job_id: trimmed}), seconds).seconds
            if before - after <= 0:
                continue
            findings.append(
                Finding(
                    skill="parallelize-dag",
                    workflow_path=workflow_path,
                    job=job_id,
                    step="",
                    bucket="",  # not waiting for a runner, setup or running: it is time waiting on another job
                    description=(
                        f"`{job_id}` waits for `{dep_id}` but uses none of its outputs or artifacts. "
                        f"Dropping `{dep_id}` from its `needs:` lets them run in parallel. The saving is "
                        "neither runner waiting, setup nor running time; it is time spent waiting on "
                        f"another job. `{job_id}` will now also run when `{dep_id}` fails, which costs compute. "
                        "Each edge is scored alone against the current graph, so savings from different "
                        "edges do not add up."
                    ),
                    expected_savings_seconds=before - after,
                    evidence={
                        "removed_need": dep_id,
                        "critical_path_seconds": before,
                        "critical_path_after_seconds": after,
                        "max_saving_seconds": before - after,  # the critical-path drop is the ceiling for this fix
                        "scored_independently": "true",
                    },
                    confidence=_CONFIDENCE,
                    risk="medium",
                )
            )
    return keep_worthwhile(findings)

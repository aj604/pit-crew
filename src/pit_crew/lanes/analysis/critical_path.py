"""Where a workflow's wall-clock time goes: the critical path through its job graph.

Only jobs on the longest `needs:` chain decide how long CI takes. Speeding up a job that runs
alongside a longer one saves nothing, so jobs are ranked by how much wall-clock time removing
them would save, not by their own duration.

A job's time in a run spans from its first matrix instance being queued to its last instance
finishing. That includes waiting for a runner, which is real wall-clock time (macOS runners
often queue for minutes). Every job, and the critical path as a whole, is split into waiting,
setup and running (see `buckets.py`) so the biggest bucket can be fixed first.
"""

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from statistics import median
from typing import Any

from pit_crew.lanes.analysis.buckets import Buckets, BucketStats, bucket_stats, job_buckets
from pit_crew.lanes.analysis.history import JobRun, WorkflowRun
from pit_crew.lanes.analysis.stats import percentile
from pit_crew.lanes.analysis.workflow import SHA, Workflow, topological_order


@dataclass(frozen=True)
class JobTiming:
    """One job id in one run, its matrix instances folded together."""

    seconds: float  # first instance queued -> last instance finished
    run_seconds: float  # longest any instance spent on its runner
    queued_seconds: float  # longest any instance waited for a runner
    slowest_instance: str  # display name of the instance that finished last
    instances: int
    buckets: Buckets  # of the instance that finished last, which is what held up later jobs
    instance_buckets: tuple[Buckets, ...]  # one per matrix instance


@dataclass(frozen=True)
class CriticalPath:
    """The longest `needs:` chain and its length in seconds."""

    seconds: float
    jobs: tuple[str, ...]  # job ids, first to last


@dataclass(frozen=True)
class JobProfile:
    """One job id summarised across runs."""

    job_id: str
    median_seconds: float  # wall-clock span, queueing included
    p95_seconds: float
    buckets: BucketStats  # waiting / setup / running across every instance in every run
    instances: int
    critical_runs: int  # runs in which this job was on the critical path
    max_saving_seconds: float  # wall-clock saved, on median timings, if this job took no time
    straggler: str  # the instance that most often finished last
    straggler_runs: int


@dataclass(frozen=True)
class StepProfile:
    """One step, by job id and name, summarised across runs and matrix instances."""

    job_id: str
    name: str
    median_seconds: float
    p95_seconds: float
    samples: int
    occurrence: int = 1  # 1 for the first step of this name in the job, 2 for the next, and so on


@dataclass(frozen=True)
class CIProfile:
    """Where one workflow's wall-clock time goes, across the runs profiled."""

    workflow: str
    runs_analyzed: int
    runs_excluded: dict[str, int]  # reason (a conclusion, or "workflow_changed") -> runs left out
    retried_runs: int  # runs that needed more than one attempt: a flake signal
    wall_clock_median_seconds: float
    wall_clock_p95_seconds: float
    critical_path: CriticalPath  # on median job timings
    critical_path_buckets: BucketStats  # waiting / setup / running summed along each run's critical path
    biggest_bucket: str  # the critical-path bucket with the largest p50: fix it first
    biggest_bucket_p95: str  # the same at p95, which bursts and queueing usually dominate
    jobs: tuple[JobProfile, ...]  # largest max_saving_seconds first
    steps: tuple[StepProfile, ...]  # slowest median first
    unmatched_jobs: tuple[str, ...] = field(default=())  # API job names not found in the workflow file
    newest_head_sha: str = ""  # commit of the most recent analyzed run: what the measurements were taken on

    def to_dict(self, max_steps: int = 15) -> dict[str, Any]:
        """JSON-ready summary for the agent, keeping only the slowest steps."""
        data: dict[str, Any] = json.loads(json.dumps(asdict(self)))  # tuples become lists, as the agent receives them
        data["steps"] = data["steps"][:max_steps]
        return data


def job_timings(run: WorkflowRun, workflow: Workflow) -> tuple[dict[str, JobTiming], list[str]]:
    """Per job id timings for the run's final attempt, plus display names that matched no job."""
    instances: dict[str, list[JobRun]] = defaultdict(list)
    unmatched = []
    for job in run.latest_jobs:
        job_id = workflow.job_id_for(job.name)
        if job_id is None:
            unmatched.append(job.name)
        else:
            instances[job_id].append(job)

    timings = {}
    for job_id, jobs in instances.items():
        starts = [j.created_at or j.started_at for j in jobs]
        ends = [j.completed_at for j in jobs]
        if None in starts or None in ends:
            continue
        last = max(jobs, key=lambda j: j.completed_at)  # type: ignore[arg-type,return-value]
        timings[job_id] = JobTiming(
            seconds=max((max(ends) - min(starts)).total_seconds(), 0.0),  # type: ignore[type-var,operator]
            run_seconds=max(j.seconds for j in jobs),
            queued_seconds=max(j.queued_seconds for j in jobs),
            slowest_instance=last.name,
            instances=len(jobs),
            buckets=job_buckets(last),
            instance_buckets=tuple(job_buckets(j) for j in jobs),
        )
    return timings, unmatched


def critical_path(workflow: Workflow, seconds: Mapping[str, float]) -> CriticalPath:
    """The longest chain of `needs:` through the jobs. Jobs missing from `seconds` count as 0."""
    finish: dict[str, float] = {}
    previous: dict[str, str | None] = {}
    for job_id in topological_order(workflow):
        needs = workflow.jobs[job_id].needs
        before = max(needs, key=lambda n: finish[n]) if needs else None
        finish[job_id] = (finish[before] if before else 0.0) + seconds.get(job_id, 0.0)
        previous[job_id] = before
    if not finish:
        return CriticalPath(0.0, ())

    last: str | None = max(finish, key=lambda j: finish[j])
    total = finish[last]  # type: ignore[index]
    chain = []
    while last:
        chain.append(last)
        last = previous[last]
    return CriticalPath(total, tuple(reversed(chain)))


_EPOCH = datetime.min.replace(tzinfo=UTC)


def max_saving(workflow: Workflow, seconds: Mapping[str, float], job_id: str) -> float:
    """Wall-clock seconds saved if `job_id` took no time: the most any fix to it can save."""
    full = critical_path(workflow, seconds).seconds
    return full - critical_path(workflow, {**seconds, job_id: 0.0}).seconds


def profile(workflow: Workflow, runs: list[WorkflowRun], include: tuple[str, ...] = ("success",)) -> CIProfile:
    """Profile the runs whose conclusion is in `include`.

    Failed and cancelled runs stop early, so their timings would understate the pipeline. Runs
    with a job the current workflow file does not have ran an older version of the pipeline,
    so they are left out too, counted as "workflow_changed".
    """
    finished = [r for r in runs if r.conclusion in include]
    excluded = Counter(str(r.conclusion) for r in runs if r.conclusion not in include)

    per_job: dict[str, list[JobTiming]] = defaultdict(list)
    critical_counts: Counter[str] = Counter()
    critical_buckets: list[Buckets] = []
    unmatched: set[str] = set()
    kept = []
    for run in finished:
        timings, missing = job_timings(run, workflow)
        if missing or not timings:
            unmatched.update(missing)
            excluded["workflow_changed"] += 1
            continue
        kept.append(run)
        for job_id, timing in timings.items():
            per_job[job_id].append(timing)
        path = critical_path(workflow, {j: t.seconds for j, t in timings.items()})
        critical_counts.update(path.jobs)
        critical_buckets.append(sum((timings[j].buckets for j in path.jobs if j in timings), Buckets(0, 0, 0)))

    medians = {job_id: median(t.seconds for t in ts) for job_id, ts in per_job.items()}
    jobs = []
    for job_id, ts in per_job.items():
        straggler, straggler_runs = Counter(t.slowest_instance for t in ts).most_common(1)[0]
        jobs.append(
            JobProfile(
                job_id=job_id,
                median_seconds=medians[job_id],
                p95_seconds=percentile([t.seconds for t in ts], 95),
                buckets=bucket_stats([b for t in ts for b in t.instance_buckets]),
                instances=max(t.instances for t in ts),
                critical_runs=critical_counts[job_id],
                max_saving_seconds=max_saving(workflow, medians, job_id),
                straggler=straggler,
                straggler_runs=straggler_runs,
            )
        )
    jobs.sort(key=lambda j: (j.max_saving_seconds, j.median_seconds), reverse=True)

    walls = [r.wall_clock_seconds for r in kept]
    path_stats = bucket_stats(critical_buckets)
    return CIProfile(
        workflow=workflow.name,
        runs_analyzed=len(kept),
        runs_excluded=dict(excluded),
        retried_runs=sum(1 for r in runs if r.run_attempt > 1),
        wall_clock_median_seconds=median(walls) if walls else 0.0,
        wall_clock_p95_seconds=percentile(walls, 95),
        critical_path=critical_path(workflow, medians),
        critical_path_buckets=path_stats,
        biggest_bucket=path_stats.biggest(50),
        biggest_bucket_p95=path_stats.biggest(95),
        jobs=tuple(jobs),
        steps=tuple(step_profiles(workflow, kept)),
        unmatched_jobs=tuple(sorted(unmatched)),
        newest_head_sha=max(kept, key=lambda r: r.run_started_at or _EPOCH).head_sha if kept else "",
    )


def step_profiles(workflow: Workflow, runs: list[WorkflowRun]) -> list[StepProfile]:
    """Median and p95 per (job id, step name, occurrence) across runs and matrix instances, slowest first.

    Skipped steps are left out so a step that rarely runs is not reported as fast. Commit SHAs
    in step names (e.g. `--title "Coverage for <sha>"`) are folded so each run's step groups together.
    Steps sharing a name within one job run are told apart by their order: the first is occurrence 1.
    """
    samples: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    for run in runs:
        for job in run.latest_jobs:
            job_id = workflow.job_id_for(job.name)
            if job_id is None:
                continue
            seen: Counter[str] = Counter()
            for step in job.steps:
                name = SHA.sub("<sha>", step.name)
                seen[name] += 1
                if step.conclusion != "skipped":
                    samples[(job_id, name, seen[name])].append(step.seconds)
    steps = [
        StepProfile(job_id, name, median(values), percentile(values, 95), len(values), occurrence)
        for (job_id, name, occurrence), values in samples.items()
    ]
    return sorted(steps, key=lambda s: s.median_seconds, reverse=True)

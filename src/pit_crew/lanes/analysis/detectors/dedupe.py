"""dedupe-jobs: flag jobs that run identical steps on the same runner, usually a copy-paste."""

from collections import defaultdict

from pit_crew.lanes.analysis.critical_path import CIProfile
from pit_crew.lanes.analysis.detectors.base import Finding, job_profile, keep_worthwhile
from pit_crew.lanes.analysis.workflow import MATRIX_TOO_LARGE, NON_SCALAR, JobSpec, StepSpec, Workflow

StepSig = tuple[str, str, tuple[tuple[str, str], ...], str, str, str, str, str, str]  # uses, run, with, if, extras
Signature = tuple[
    str, tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...], tuple[StepSig, ...], str, str, tuple[str, ...]
]


def _script(run: str | None) -> str:
    """The script exactly as written, minus whitespace around the whole text.

    YAML already removes block-scalar indentation. Stripping each line would merge scripts whose indentation
    matters (heredocs, Python) and could turn a trailing backslash-space into a line continuation.
    """
    return (run or "").strip()


def _step_signature(s: StepSpec) -> StepSig:
    return (
        s.uses or "",
        _script(s.run),
        tuple(sorted(s.with_.items())),
        s.if_ or "",
        s.env,
        s.working_directory,
        s.shell,
        s.timeout_minutes,
        s.continue_on_error,
    )


def _job_extras(job: JobSpec) -> tuple[str, ...]:
    return (job.env, job.container, job.services, job.defaults, job.timeout_minutes, job.continue_on_error)


def _unknown(job: JobSpec) -> bool:
    """True when any compared field was too large to canonicalise (or a `with:` value was not a scalar)."""
    fields = [job.matrix_definition, *_job_extras(job)]
    for s in job.steps:
        fields += [s.env, s.working_directory, s.shell, s.timeout_minutes, s.continue_on_error, *s.with_.values()]
    return MATRIX_TOO_LARGE in fields or NON_SCALAR in fields


def _signature(job: JobSpec) -> Signature:
    """What a job does and where: runner, matrix, `if:`, job settings and each step's command, inputs and settings."""
    steps = tuple(_step_signature(s) for s in job.steps)
    matrix = tuple(sorted((key, tuple(sorted(values))) for key, values in job.matrix_values))
    return (job.runs_on, job.matrix_keys, matrix, steps, job.matrix_definition, job.if_ or "", _job_extras(job))


def detect_dedupe_jobs(profile: CIProfile, workflow: Workflow, workflow_path: str) -> list[Finding]:
    """One finding per group of identical measured jobs, on the member with the most to save."""
    groups: dict[Signature, list[str]] = defaultdict(list)
    savings: dict[str, float] = {}
    for job_id, job in workflow.jobs.items():
        measured = job_profile(profile, job_id)
        if job.steps and measured is not None and not _unknown(job):  # unknown coverage
            groups[_signature(job)].append(job_id)
            savings[job_id] = measured.max_saving_seconds

    findings = []
    for members in groups.values():
        if len(members) < 2:  # noqa: PLR2004  a group needs at least a pair
            continue
        target = max(members, key=lambda m: savings[m])
        others = ", ".join(m for m in members if m != target)
        saving = savings[target]
        findings.append(
            Finding(
                skill="dedupe-jobs",
                workflow_path=workflow_path,
                job=target,
                step="",
                bucket="running",
                description=(
                    f"Job '{target}' runs the same steps on the same runner as {others}. "
                    f"They were found identical in runner, matrix and every step's command and inputs. "
                    f"Confirm they are equivalent, then keep one copy: drop '{target}' and keep {others}, "
                    f"since the saving assumes '{target}' is the one removed; "
                    f"never remove the check itself, since the job that stays still runs it."
                ),
                expected_savings_seconds=saving,
                evidence={"duplicate_of": others, "max_saving_seconds": saving},
                confidence=0.8,
                risk="medium",
            )
        )
    return keep_worthwhile(findings)

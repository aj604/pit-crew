"""The Finding record every detector returns, and helpers for tying YAML steps to measured timings."""

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

from pit_crew.contracts import BUCKETS, PLAYBOOK_SKILLS, RISKS
from pit_crew.lanes.analysis.critical_path import CIProfile, JobProfile, StepProfile
from pit_crew.lanes.analysis.workflow import StepSpec, match_step_name

MIN_SAVING_SECONDS = 10.0  # findings worth less than this are noise


class InvalidFindingError(ValueError):
    """Raised when a Finding names an unknown skill, bucket or risk."""

    @classmethod
    def unknown_skill(cls, skill: str) -> "InvalidFindingError":
        """Build error for a skill outside PLAYBOOK_SKILLS."""
        return cls(f"Finding.skill must be one of {PLAYBOOK_SKILLS}, got {skill!r}")

    @classmethod
    def unknown_bucket(cls, bucket: str) -> "InvalidFindingError":
        """Build error for a bucket outside BUCKETS."""
        return cls(f"Finding.bucket must be one of {BUCKETS}, got {bucket!r}")

    @classmethod
    def unknown_risk(cls, risk: str) -> "InvalidFindingError":
        """Build error for a risk outside RISKS."""
        return cls(f"Finding.risk must be one of {RISKS}, got {risk!r}")


@dataclass(frozen=True)
class Finding:
    """One bottleneck a detector found, with the evidence and the skill that should fix it."""

    skill: str
    workflow_path: str
    job: str
    step: str
    bucket: str
    description: str
    expected_savings_seconds: float
    evidence: dict[str, float | str]
    confidence: float
    risk: str

    def __post_init__(self) -> None:
        """Reject values outside the shared vocabularies."""
        if self.skill not in PLAYBOOK_SKILLS:
            raise InvalidFindingError.unknown_skill(self.skill)
        if self.bucket not in BUCKETS:
            raise InvalidFindingError.unknown_bucket(self.bucket)
        if self.risk not in RISKS:
            raise InvalidFindingError.unknown_risk(self.risk)

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Identity of the finding: skill, workflow file, job id, step name."""
        return (self.skill, self.workflow_path, self.job, self.step)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return asdict(self)


def job_profile(profile: CIProfile, job_id: str) -> JobProfile | None:
    """The measured profile for a job id, or None when it never ran."""
    return next((j for j in profile.jobs if j.job_id == job_id), None)


def step_profile(profile: CIProfile, job_id: str, step: StepSpec) -> StepProfile | None:
    """The measured step a YAML step corresponds to, or None when it was never measured."""
    return next(
        (s for s in profile.steps if s.job_id == job_id and match_step_name(step.name, s.name)),
        None,
    )


def keep_worthwhile(findings: Iterable[Finding]) -> list[Finding]:
    """Drop findings saving under MIN_SAVING_SECONDS; biggest saving first."""
    kept = [f for f in findings if f.expected_savings_seconds >= MIN_SAVING_SECONDS]
    return sorted(kept, key=lambda f: f.expected_savings_seconds, reverse=True)

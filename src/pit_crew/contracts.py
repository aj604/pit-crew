"""The records lanes exchange. Lanes never call each other; they read and write these.

Changing a field here affects every lane, so change it in its own PR.
All fields added after the initial scaffold carry defaults so existing positional callers stay compatible.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

PLAYBOOK_SKILLS: tuple[str, ...] = (
    "dep-cache",
    "parallelize-dag",
    "shard-by-timing",
    "dedupe-jobs",
    "affected-tests",
)

BUCKETS: tuple[str, ...] = ("waiting", "setup", "running")  # where a job's time goes; see lanes/analysis/buckets.py
RISKS: tuple[str, ...] = ("low", "medium", "high")

_MIN_GITHUB_PATH_PARTS = 2

_ALLOWED_CONFIG_NAMES: frozenset[str] = frozenset(
    {
        "pyproject.toml",
        "pytest.ini",
        "tox.ini",
        "noxfile.py",
        "package.json",
        "Makefile",
        "Cargo.toml",
    }
)

_ALLOWED_CONFIG_PREFIXES: tuple[str, ...] = (
    "playwright.config.",
    "vitest.config.",
    "jest.config.",
    "cypress.config.",
    "tsconfig",
)

_FORBIDDEN_DIRS: frozenset[str] = frozenset({"src", "app", "lib", "tests", "test", "spec", "specs", "e2e", "__tests__"})


class InvalidHypothesisError(ValueError):
    """Raised when a Hypothesis names an unknown skill, bucket or risk, or a confidence outside 0..1."""

    @classmethod
    def unknown_skill(cls, skill: str) -> "InvalidHypothesisError":
        """Build error for a skill outside PLAYBOOK_SKILLS."""
        return cls(f"Hypothesis.skill must be one of {PLAYBOOK_SKILLS}, got {skill!r}")

    @classmethod
    def unknown_bucket(cls, bucket: str) -> "InvalidHypothesisError":
        """Build error for a bucket outside BUCKETS."""
        return cls(f"Hypothesis.bucket must be one of {BUCKETS} or empty, got {bucket!r}")

    @classmethod
    def unknown_risk(cls, risk: str) -> "InvalidHypothesisError":
        """Build error for a risk outside RISKS."""
        return cls(f"Hypothesis.risk must be one of {RISKS} or empty, got {risk!r}")

    @classmethod
    def bad_confidence(cls, confidence: float) -> "InvalidHypothesisError":
        """Build error for a confidence outside 0..1."""
        return cls(f"Hypothesis.confidence must be between 0 and 1, got {confidence!r}")


class InvalidBugCaseError(ValueError):
    """Raised when a BugCase is missing required commit or mutation fields or has invalid expected_checks."""

    @classmethod
    def unknown_kind(cls, kind: str) -> "InvalidBugCaseError":
        """Build error for an unsupported BugCase.kind."""
        return cls(f"BugCase.kind must be 'commit' or 'mutation', got {kind!r}")

    @classmethod
    def missing_commit_sha(cls) -> "InvalidBugCaseError":
        """Build error when a commit BugCase omits commit_sha."""
        return cls("commit BugCase requires commit_sha")

    @classmethod
    def missing_mutations(cls) -> "InvalidBugCaseError":
        """Build error when a mutation BugCase omits mutated_files."""
        return cls("mutation BugCase requires non-empty mutated_files")

    @classmethod
    def invalid_expected_checks(cls, checks: object) -> "InvalidBugCaseError":
        """Build error when expected_checks is not a tuple of strings."""
        return cls(f"BugCase.expected_checks must be a tuple of non-empty strings, got {checks!r}")

    @classmethod
    def invalid_expected_check(cls, check: object) -> "InvalidBugCaseError":
        """Build error when an expected_checks entry is empty or not a string."""
        return cls(f"BugCase.expected_checks entries must be non-empty strings, got {check!r}")


def _validate_expected_checks(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, tuple):
        raise InvalidBugCaseError.invalid_expected_checks(raw)
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise InvalidBugCaseError.invalid_expected_check(item)
        stripped = item.strip()
        if stripped not in cleaned:
            cleaned.append(stripped)
    return tuple(cleaned)


def _id() -> str:
    return uuid4().hex[:12]


def is_allowed_candidate_path(path: str) -> bool:
    """True when Optimizer is permitted to modify ``path`` on a candidate branch.

    Allows ``.github/**`` (workflows and composite actions) plus runner/build config files
    outside application source and test directories.
    """
    if not path or path.startswith("/") or "\\" in path:
        return False
    pure = PurePosixPath(path)
    parts = pure.parts
    if not parts or ".." in parts or "." in parts:
        return False
    if parts[0] == ".github":
        return len(parts) >= _MIN_GITHUB_PATH_PARTS
    if any(part in _FORBIDDEN_DIRS for part in parts[:-1]):
        return False
    name = pure.name
    if name in _ALLOWED_CONFIG_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _ALLOWED_CONFIG_PREFIXES)


@dataclass(frozen=True)
class Hypothesis:
    """Analysis lane output: one bottleneck worth trying to fix.

    `workflow_path`, `job` and `step` say where the Optimizer should make its change, so it does
    not have to parse `description`. `evidence` holds the measured numbers behind the claim
    (for example step_p50_seconds, max_saving_seconds, runs_sampled) as plain JSON values.
    """

    repo: str
    description: str
    skill: str  # which playbook skill should fix it: one of PLAYBOOK_SKILLS
    expected_savings_seconds: float
    id: str = field(default_factory=_id)
    base_sha: str = ""
    workflow_path: str = ""
    job: str = ""  # job id under `jobs:` in workflow_path, e.g. "test"
    step: str = ""  # step name within the job, e.g. "Install dependencies"; empty for job-level changes
    bucket: str = ""  # which part of the job's time it shrinks: one of BUCKETS, or "" if unknown
    evidence: dict[str, float | str] = field(default_factory=dict)
    confidence: float | None = None  # 0..1: how sure Analysis is that the saving is real
    risk: str = ""  # how likely the change is to weaken what CI catches: one of RISKS, or "" if unknown

    def __post_init__(self) -> None:
        """Reject values outside the shared vocabularies so typos fail when written, not downstream."""
        if self.skill not in PLAYBOOK_SKILLS:
            raise InvalidHypothesisError.unknown_skill(self.skill)
        if self.bucket and self.bucket not in BUCKETS:
            raise InvalidHypothesisError.unknown_bucket(self.bucket)
        if self.risk and self.risk not in RISKS:
            raise InvalidHypothesisError.unknown_risk(self.risk)
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise InvalidHypothesisError.bad_confidence(self.confidence)


@dataclass(frozen=True)
class Candidate:
    """Optimizer lane output: one change, pushed as a branch on the fork."""

    hypothesis_id: str
    branch: str
    summary: str
    id: str = field(default_factory=_id)
    repo: str = ""
    base_sha: str = ""
    head_sha: str = ""
    files: dict[str, str] = field(default_factory=dict)
    changed_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Populate changed_files from files when not provided explicitly."""
        if self.files and not self.changed_files:
            object.__setattr__(self, "changed_files", tuple(sorted(self.files)))


@dataclass(frozen=True)
class BugCase:
    """One historical red commit or synthetic file mutation to replay against a candidate.

    `expected_checks` holds the CI check keys (for example a job's id and display-name aliases
    such as `("CI/test", "CI/Unit tests")`, or every check a multi-check red commit failed on)
    expected to fail when this case is replayed. Empty when unspecified.
    """

    kind: str  # "commit" or "mutation"
    reason: str = ""
    commit_sha: str | None = None
    mutated_files: dict[str, str] = field(default_factory=dict)
    artifact_ref: str | None = None  # screenshot / Playwright trace / build artifact inspected by Proof
    expected_checks: tuple[str, ...] = ()  # check keys expected to fail on replay; empty if unspecified

    def __post_init__(self) -> None:
        """Validate required fields for commit vs. mutation replay cases."""
        if self.kind not in ("commit", "mutation"):
            raise InvalidBugCaseError.unknown_kind(self.kind)
        if self.kind == "commit" and not self.commit_sha:
            raise InvalidBugCaseError.missing_commit_sha()
        if self.kind == "mutation" and not self.mutated_files:
            raise InvalidBugCaseError.missing_mutations()
        cleaned = _validate_expected_checks(self.expected_checks)
        if cleaned != self.expected_checks:
            object.__setattr__(self, "expected_checks", cleaned)


@dataclass(frozen=True)
class ReplayPlan:
    """Proof lane output: historical red commits and/or synthetic mutations to replay."""

    candidate_id: str
    bug_shas: tuple[str, ...] = ()
    id: str = field(default_factory=_id)
    cases: tuple[BugCase, ...] = ()

    def __post_init__(self) -> None:
        """Keep bug_shas and structured cases synchronized for backwards compatibility."""
        if self.bug_shas and not self.cases:
            inferred = tuple(
                BugCase(kind="commit", commit_sha=sha, reason="historical red commit") for sha in self.bug_shas
            )
            object.__setattr__(self, "cases", inferred)
        elif self.cases and not self.bug_shas:
            shas = tuple(c.commit_sha for c in self.cases if c.kind == "commit" and c.commit_sha)
            object.__setattr__(self, "bug_shas", shas)


@dataclass(frozen=True)
class Evidence:
    """Measured by code from GitHub Actions results. The agent never writes this."""

    candidate_id: str
    baseline_seconds: tuple[float, ...]
    candidate_seconds: tuple[float, ...]
    bugs_total: int
    bugs_caught: int
    id: str = field(default_factory=_id)
    run_urls: tuple[str, ...] = ()
    bug_outcomes: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Verdict:
    """Rater output: keep or reject, with the numbers behind it."""

    candidate_id: str
    keep: bool
    speedup: float
    p_value: float | None
    reason: str
    id: str = field(default_factory=_id)
    median_seconds: float = 0.0
    p95_seconds: float = 0.0
    sla_met: bool = False


Record = Hypothesis | Candidate | ReplayPlan | Evidence | Verdict

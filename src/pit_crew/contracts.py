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


class InvalidBugCaseError(ValueError):
    """Raised when a BugCase is missing required commit or mutation fields."""

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
    """Analysis lane output: one bottleneck worth trying to fix."""

    repo: str
    description: str
    skill: str  # which playbook skill should fix it, e.g. "dep-cache"
    expected_savings_seconds: float
    id: str = field(default_factory=_id)
    base_sha: str = ""
    workflow_path: str = ""


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
    """One historical red commit or synthetic file mutation to replay against a candidate."""

    kind: str  # "commit" or "mutation"
    reason: str = ""
    commit_sha: str | None = None
    mutated_files: dict[str, str] = field(default_factory=dict)
    artifact_ref: str | None = None  # screenshot / Playwright trace / build artifact inspected by Proof

    def __post_init__(self) -> None:
        """Validate required fields for commit vs. mutation replay cases."""
        if self.kind not in ("commit", "mutation"):
            raise InvalidBugCaseError.unknown_kind(self.kind)
        if self.kind == "commit" and not self.commit_sha:
            raise InvalidBugCaseError.missing_commit_sha()
        if self.kind == "mutation" and not self.mutated_files:
            raise InvalidBugCaseError.missing_mutations()


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

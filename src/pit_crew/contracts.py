"""The records lanes exchange. Lanes never call each other; they read and write these.

Changing a field here affects every lane, so change it in its own PR.
"""

from dataclasses import dataclass, field
from uuid import uuid4


def _id() -> str:
    return uuid4().hex[:12]


@dataclass(frozen=True)
class Hypothesis:
    """Analysis lane output: one bottleneck worth trying to fix."""

    repo: str
    description: str
    skill: str  # which playbook skill should fix it, e.g. "dep-cache"
    expected_savings_seconds: float
    id: str = field(default_factory=_id)


@dataclass(frozen=True)
class Candidate:
    """Optimizer lane output: one change, pushed as a branch on the fork."""

    hypothesis_id: str
    branch: str
    summary: str
    id: str = field(default_factory=_id)


@dataclass(frozen=True)
class ReplayPlan:
    """Proof lane output: which historical red commits to replay against a candidate."""

    candidate_id: str
    bug_shas: tuple[str, ...]
    id: str = field(default_factory=_id)


@dataclass(frozen=True)
class Evidence:
    """Measured by code from GitHub Actions results. The agent never writes this."""

    candidate_id: str
    baseline_seconds: tuple[float, ...]
    candidate_seconds: tuple[float, ...]
    bugs_total: int
    bugs_caught: int
    id: str = field(default_factory=_id)


@dataclass(frozen=True)
class Verdict:
    """Rater output: keep or reject, with the numbers behind it."""

    candidate_id: str
    keep: bool
    speedup: float
    p_value: float | None
    reason: str
    id: str = field(default_factory=_id)


Record = Hypothesis | Candidate | ReplayPlan | Evidence | Verdict

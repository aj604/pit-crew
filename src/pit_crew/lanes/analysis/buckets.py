"""Split each job's time into waiting, setup and running.

- waiting: queued until a runner picked the job up. Fixed by runner choice and warm machines.
- setup: everything that prepares the machine rather than checking the code: runner start-up,
  checkout, toolchain, dependency installs, cache restores, post-job cleanup, and runner time
  no step accounts for. Fixed by images with the toolchain baked in and by caches.
- running: the remaining steps, i.e. the lint, build and test work itself.

Steps are classified by name, because the API reports nothing else about them. The default
names GitHub gives are "Run <action or command>" and "Post Run <action>", so both the
action (actions/checkout, setup-uv) and the command (npm ci, uv sync) are matched.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pit_crew.lanes.analysis.history import JobRun
from pit_crew.lanes.analysis.stats import percentile

SETUP_STEP = re.compile(
    r"""
    ^set\ up\ job$ | ^complete\ job$ | ^post\b      # runner start-up and cleanup
    | check\ ?out                                  # actions/checkout, "Check out the base revision"
    | \bset\ ?up\b | setup                         # actions/setup-*, "Set up Python", "Setup uv"
    | install                                      # npm/pip/bun install, "Install dependencies", uninstall
    | \bcache\b | \brestore\b                      # actions/cache, sccache, "Restore build cache"
    | \btoolchain\b | \brustup\b
    | \b(uv\ sync|npm\ ci|yarn|pnpm\ i|cargo\ fetch|go\ mod\ download)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

BUCKETS = ("waiting", "setup", "running")


def is_setup_step(name: str) -> bool:
    """Whether a step prepares the machine rather than checking the code."""
    return SETUP_STEP.search(name) is not None


@dataclass(frozen=True)
class Buckets:
    """Seconds one job instance spent waiting for a runner, setting up, and running checks."""

    waiting: float
    setup: float
    running: float

    def __add__(self, other: "Buckets") -> "Buckets":
        """Bucket-wise sum, for adding up the jobs along a chain."""
        return Buckets(self.waiting + other.waiting, self.setup + other.setup, self.running + other.running)


@dataclass(frozen=True)
class BucketStats:
    """p50 and p95 of each bucket across samples (nearest rank, so always observed values)."""

    waiting_p50: float
    waiting_p95: float
    setup_p50: float
    setup_p95: float
    running_p50: float
    running_p95: float

    def biggest(self, pct: int = 50) -> str:
        """The bucket with the largest p50 (or p95): the one to fix first. Ties go to the earlier bucket."""
        return max(BUCKETS, key=lambda b: getattr(self, f"{b}_p{pct}"))


def job_buckets(job: JobRun) -> Buckets:
    """Waiting, setup and running for one job instance.

    Runner time not covered by any step (start-up and shutdown gaps) counts as setup.
    """
    setup = sum(s.seconds for s in job.steps if is_setup_step(s.name))
    running = sum(s.seconds for s in job.steps if not is_setup_step(s.name))
    unaccounted = max(job.seconds - setup - running, 0.0)
    return Buckets(job.queued_seconds, setup + unaccounted, running)


def bucket_stats(samples: Sequence[Buckets]) -> BucketStats:
    """p50 and p95 per bucket. All zeros for no samples."""
    waiting = [s.waiting for s in samples]
    setup = [s.setup for s in samples]
    running = [s.running for s in samples]
    return BucketStats(
        percentile(waiting, 50),
        percentile(waiting, 95),
        percentile(setup, 50),
        percentile(setup, 95),
        percentile(running, 50),
        percentile(running, 95),
    )

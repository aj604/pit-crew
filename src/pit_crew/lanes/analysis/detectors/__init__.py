"""Bottleneck detectors: each reads a workflow and its CIProfile and returns Findings."""

from collections.abc import Callable

from pit_crew.lanes.analysis.critical_path import CIProfile
from pit_crew.lanes.analysis.detectors.base import Finding
from pit_crew.lanes.analysis.detectors.dedupe import detect_dedupe_jobs
from pit_crew.lanes.analysis.detectors.dep_cache import detect_dep_cache
from pit_crew.lanes.analysis.detectors.parallelize import detect_parallelize_dag
from pit_crew.lanes.analysis.detectors.shard import detect_shard_by_timing
from pit_crew.lanes.analysis.workflow import Workflow

type Detector = Callable[[CIProfile, Workflow, str], list[Finding]]

DETECTORS: tuple[Detector, ...] = (
    detect_dep_cache,
    detect_shard_by_timing,
    detect_parallelize_dag,
    detect_dedupe_jobs,
)

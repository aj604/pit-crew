"""shard-by-timing: a long test step on the critical path could run as parallel shards."""

import math
import re

from pit_crew.lanes.analysis.buckets import is_setup_step
from pit_crew.lanes.analysis.critical_path import CIProfile
from pit_crew.lanes.analysis.detectors.base import (
    Finding,
    job_profile,
    keep_worthwhile,
    step_label,
    step_occurrences,
    step_profile,
)
from pit_crew.lanes.analysis.workflow import StepSpec, Workflow

SHARD_MIN_SECONDS = 60.0  # shorter steps are not worth the sharding overhead
TARGET_SHARD_SECONDS = 30.0  # the brief: tests under 30 seconds per shard
MAX_SHARDS = 8
CONFIDENT_RUNS = 20  # runs needed before the timing estimate is trusted
TEST_STEP = re.compile(
    r"\b(test|tests|pytest|jest|vitest|mocha|ava|rspec|phpunit|nextest|go test|cargo test|bun test|tox|nox|ctest)\b",
    re.IGNORECASE,
)
_WRAPPERS = frozenset(
    {"uv", "poetry", "pipenv", "npx", "pnpm", "yarn", "python", "python3", "run", "exec", "dlx"}  # runners
    | {"sudo", "env", "time", "nice", "nohup", "xargs"}  # prefixes that run the command that follows
)
_STATIC_TOOLS = frozenset(
    {"mypy", "ruff", "pyright", "tsc", "eslint", "flake8", "black", "isort", "pylint", "prettier", "biome"}
)  # linters and type checkers take `tests/` as a path to read, not a suite to run
_NON_TEST_COMMANDS = _STATIC_TOOLS | frozenset(
    {"cp", "mv", "rsync", "tar", "zip", "aws", "gsutil", "az", "curl", "wget", "echo", "cat", "ls", "mkdir", "gh"}
)  # `gh` downloads artifacts and cuts releases; it never runs a suite
_UPLOAD_COMMANDS = frozenset({"codecov", "coveralls"})
_UPLOAD_MARKERS = ("upload", "publish")  # `junit-upload`, `./publish.sh`: they ship results, not run tests
_QUOTES = ("'", '"')
_WORD = re.compile(r"\w+")
_SEGMENTS = re.compile(r"&&|\|\||[;|\n]")
NO_COMMAND_ACTIONS = ("actions/upload-artifact", "actions/download-artifact", "actions/cache")
_SHARD_FLAGS = frozenset({"--shard", "--splits", "--group", "--num-shards", "--shard-id", "--partition"})
_SHARD_ENV_VARS = ("CI_NODE_INDEX", "CI_NODE_TOTAL", "SHARD_INDEX", "SPLIT_INDEX")
ALREADY_SHARDED = re.compile(r"shard|split|group|chunk|partition|slice", re.IGNORECASE)


def _is_assignment(token: str) -> bool:
    """Whether a token is a shell `NAME=value` prefix: what precedes the first `=` must be a valid variable name.

    That keeps `ruff>"out/run=1.json"` (a command with a redirection) from reading as an assignment.
    """
    name, equals, _ = token.partition("=")
    return (
        bool(equals)
        and bool(name)
        and name.isascii()
        and (name[0].isalpha() or name[0] == "_")
        and all(c.isalnum() or c == "_" for c in name)
    )


def _unquote(token: str) -> str:
    """Strip one level of matching surrounding quotes."""
    if token[:1] in _QUOTES and len(token) > 1 and token[-1] == token[0]:
        return token[1:-1]
    return token


def _first_token(segment: str) -> str:
    """The command token of a shell segment, past its wrappers, flags and `KEY=VALUE` assignments; "" if none.

    A plain token loop: a regex with nested quantifiers backtracks exponentially on workflow text it does not control.
    """
    for raw in segment.split():
        token = _unquote(raw)
        if token in _WRAPPERS or token.startswith("-") or _is_assignment(token):
            continue
        return token
    return ""


def _word(token: str) -> str:
    """The token's leading word characters, or the whole token when it does not start with one."""
    match = _WORD.match(token)
    return match.group() if match is not None else token


def _first_command(segment: str) -> str:
    """The command word of a shell segment (see `_first_token`)."""
    return _word(_first_token(segment))


def _is_static_check(segment: str) -> bool:
    """Whether the segment's command is a linter or type checker."""
    return _first_command(segment) in _STATIC_TOOLS


def _is_non_test_command(segment: str) -> bool:
    """Whether the segment's command is a static check or a file-moving/printing tool, never a test runner."""
    token = _first_token(segment).lower()
    return (
        _first_command(segment) in _NON_TEST_COMMANDS
        or _word(token) in _UPLOAD_COMMANDS
        or any(marker in token for marker in _UPLOAD_MARKERS)
    )


def _step_is_sharded(step: StepSpec) -> bool:
    """Whether the step's own command already shards: a split flag, or a CI node/shard index variable.

    `pytest -n` / `--numprocesses` (xdist) is parallel, not sharded, so it only counts next to a split flag,
    which is already enough on its own.
    """
    text = f"{step.run or ''}\n{step.env}"
    if any(var in text for var in _SHARD_ENV_VARS):
        return True
    for raw in (step.run or "").split():
        flag = _unquote(raw).partition("=")[0]
        if flag in _SHARD_FLAGS:
            return True
    return False


def _runs_tests(step: StepSpec) -> bool:
    """Whether the step runs a test suite: judged by its command, or by its name when it has none (a `uses:` step).

    A shell step with no recognised test command still counts when its name says tests and none of its
    commands is a known non-test one (a wrapper script such as `./scripts/ci.sh`).
    """
    if step.run is not None:
        segments = [seg.strip() for seg in _SEGMENTS.split(step.run)]
        if any(TEST_STEP.search(seg) and not _is_non_test_command(seg) for seg in segments):
            return True
        return TEST_STEP.search(step.name) is not None and not any(_is_non_test_command(seg) for seg in segments)
    if (step.uses or "").startswith(NO_COMMAND_ACTIONS):
        return False  # moving files around, however the step is named
    return TEST_STEP.search(step.name) is not None


def detect_shard_by_timing(profile: CIProfile, workflow: Workflow, workflow_path: str) -> list[Finding]:
    """Flag measured, non-setup steps of at least SHARD_MIN_SECONDS in jobs that still shorten the critical path."""
    findings: list[Finding] = []
    for job_id, spec in workflow.jobs.items():
        measured = job_profile(profile, job_id)
        if measured is None or measured.max_saving_seconds <= 0:
            continue
        if any(ALREADY_SHARDED.search(key) for key in spec.matrix_keys):
            continue
        for step, occurrence in zip(spec.steps, step_occurrences(spec.steps), strict=True):
            if is_setup_step(step.name):
                continue
            if not _runs_tests(step) or _step_is_sharded(step):
                continue
            timing = step_profile(profile, job_id, step, occurrence)
            if timing is None or timing.median_seconds < SHARD_MIN_SECONDS:
                continue
            shards = min(math.ceil(timing.median_seconds / TARGET_SHARD_SECONDS), MAX_SHARDS)
            expected = min(timing.median_seconds * (1 - 1 / shards), measured.max_saving_seconds)
            label = step_label(step.name, occurrence)
            findings.append(
                Finding(
                    skill="shard-by-timing",
                    workflow_path=workflow_path,
                    job=job_id,
                    step=label,
                    bucket="running",
                    description=(
                        f"Step '{label}' takes a median {timing.median_seconds:.0f}s on the critical path. "
                        f"Split it into {shards} parallel shards balanced by recorded test timings. "
                        "The shards together must run every test file: prove it by comparing the union of "
                        "the shards' test files with the unsharded run's before merging."
                    ),
                    expected_savings_seconds=expected,
                    evidence={
                        "step_p50_seconds": timing.median_seconds,
                        "step_p95_seconds": timing.p95_seconds,
                        "shards": float(shards),
                        "max_saving_seconds": measured.max_saving_seconds,
                        "runs_sampled": timing.samples,
                    },
                    confidence=0.6 if profile.runs_analyzed >= CONFIDENT_RUNS else 0.4,
                    risk="low",
                )
            )
    return keep_worthwhile(findings)

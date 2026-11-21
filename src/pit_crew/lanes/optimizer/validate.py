"""Static safety guard for an Optimizer change: CI may get faster, it may never check less.

Pure functions over file text, so the host can run them without a checkout of the repository.
A change fails the guard when, compared with the original workflow, it:

- no longer parses, or has no jobs;
- drops a trigger event, a check command, or a check action;
- removes values from a test matrix, or adds matrix exclusions;
- adds ``continue-on-error``, a disabled ``if:``, a wider ``timeout-minutes``, or retries;
- adds command flags that hide failures or run fewer tests (``|| true``, ``-k``, ``--exit-zero``...);
- uploads fewer artifacts (the Proof lane needs failure screenshots and videos).

Install commands and setup or cache actions may change freely: they check nothing, and if one
breaks, the checks after it fail.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import yaml

WORKFLOW_DIR = ".github/workflows/"
YAML_SUFFIXES = (".yml", ".yaml")

_SETUP_COMMAND = re.compile(
    r"^(?:sudo\s+)?(?:"
    r"(?:npm|pnpm|yarn|bun)\s+(?:install|ci|i)\b"
    r"|(?:python3?\s+-m\s+)?pip3?\s+install\b"
    r"|uv\s+(?:sync|venv|pip\s+install|python\s+install)\b"
    r"|poetry\s+install\b"
    r"|apt(?:-get)?\s+(?:update|install)\b"
    r"|brew\s+install\b"
    r"|cargo\s+(?:fetch|install)\b"
    r"|go\s+mod\s+download\b"
    r")"
)
_SETUP_ACTION = re.compile(r"(?:^actions/checkout$|setup|cache|^actions/download-artifact$)", re.IGNORECASE)
_UPLOAD_ACTION = "actions/upload-artifact"
_RETRY_ACTION = re.compile(r"retry", re.IGNORECASE)
_DISABLED_IF = re.compile(r"^\s*(?:\$\{\{\s*)?false(?:\s*\}\})?\s*$", re.IGNORECASE)

# Added to a run command, each of these hides a failure, retries one away, or runs fewer tests.
WEAKENING_FLAGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignores the exit code", re.compile(r"\|\|\s*(?:true\b|:(?:\s|$)|exit\s+0\b)")),
    ("turns off fail-on-error", re.compile(r"(?:^|\s)set\s+\+e\b")),
    ("exits zero on findings", re.compile(r"--exit-zero\b")),
    ("passes with no tests", re.compile(r"--passWithNoTests\b")),
    ("filters tests by keyword", re.compile(r"(?:^|\s)-k(?:\s|=|$)")),
    ("deselects or ignores tests", re.compile(r"--(?:deselect|ignore(?:-glob)?)(?:\s|=)")),
    ("ignores test paths", re.compile(r"--testPathIgnorePatterns\b")),
    ("skips tests", re.compile(r"(?:^|\s)--?skip(?:\s|=)")),
    ("retries failed tests", re.compile(r"--(?:reruns|retries)\b")),
)


@dataclass
class GuardReport:
    """What the guard found. ``problems`` block the change; ``warnings`` are for the summary."""

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing blocks the change."""
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form returned to the agent."""
        return {"ok": self.ok, "problems": list(self.problems), "warnings": list(self.warnings)}


def is_workflow(path: str) -> bool:
    """True for a workflow file directly under ``.github/workflows/``."""
    rest = path.removeprefix(WORKFLOW_DIR)
    return rest != path and "/" not in rest and rest.endswith(YAML_SUFFIXES)


def check_change(path: str, before: str, after: str) -> GuardReport:
    """Compare a file's original text (``before``, empty for a new file) with its changed text."""
    report = GuardReport()
    if not path.endswith(YAML_SUFFIXES):
        return report
    if not after.strip():
        if before.strip():
            report.problems.append(f"{path}: deletes the file; keep every check")
        return report
    new = _load(path, after, report)
    if new is not None and is_workflow(path):
        _compare_workflow(path, before, new, report)
    return report


def _compare_workflow(path: str, before: str, new: dict[Any, Any], report: GuardReport) -> None:
    if not _jobs(new):
        report.problems.append(f"{path}: has no jobs")
        return
    if not before.strip():
        report.warnings.append(f"{path}: new workflow; nothing to compare against")
        return
    old = _load(path, before, GuardReport())
    if old is None:
        report.warnings.append(f"{path}: the original does not parse, so it could not be compared")
        return
    for compare in (_triggers_kept, _commands_kept, _actions_kept, _matrix_kept, _no_new_weakening, _timeouts_kept):
        report.problems.extend(f"{path}: {problem}" for problem in compare(old, new))
    report.warnings.extend(f"{path}: {note}" for note in _setup_changes(old, new))


def _load(path: str, text: str, report: GuardReport) -> dict[Any, Any] | None:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        report.problems.append(f"{path}: is not valid YAML ({exc})")
        return None
    if not isinstance(data, dict):
        report.problems.append(f"{path}: is not a YAML mapping")
        return None
    return data


# --- reading a workflow -------------------------------------------------------------------


def _jobs(workflow: dict[Any, Any]) -> dict[str, dict[Any, Any]]:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    return {str(job_id): job for job_id, job in jobs.items() if isinstance(job, dict)}


def _steps(workflow: dict[Any, Any]) -> Iterator[dict[Any, Any]]:
    for job in _jobs(workflow).values():
        steps = job.get("steps")
        if isinstance(steps, list):
            yield from (step for step in steps if isinstance(step, dict))


def _blocks(workflow: dict[Any, Any]) -> Iterator[dict[Any, Any]]:
    """Every job and every step: the places ``if:`` and ``continue-on-error`` can appear."""
    yield from _jobs(workflow).values()
    yield from _steps(workflow)


def _triggers(workflow: dict[Any, Any]) -> set[str]:
    # YAML 1.1 reads a bare `on:` key as the boolean True.
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, list):
        return {str(event) for event in on}
    if isinstance(on, dict):
        return {str(event) for event in on}
    return set()


def _command_lines(workflow: dict[Any, Any]) -> list[str]:
    lines: list[str] = []
    for step in _steps(workflow):
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for raw in run.replace("\\\n", " ").splitlines():
            line = " ".join(raw.split())
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def _check_commands(workflow: dict[Any, Any]) -> list[str]:
    return [line for line in _command_lines(workflow) if not _SETUP_COMMAND.match(line)]


def _actions(workflow: dict[Any, Any]) -> list[str]:
    """Action names without their version, from steps and from reusable-workflow jobs."""
    uses = [step.get("uses") for step in _steps(workflow)]
    uses += [job.get("uses") for job in _jobs(workflow).values()]
    return [ref.split("@", 1)[0] for ref in uses if isinstance(ref, str)]


# --- comparisons: each returns the problems it found ----------------------------------------


def _triggers_kept(old: dict[Any, Any], new: dict[Any, Any]) -> list[str]:
    return [f"no longer runs on `{event}`" for event in sorted(_triggers(old) - _triggers(new))]


def _commands_kept(old: dict[Any, Any], new: dict[Any, Any]) -> list[str]:
    """Every check command must survive, exactly or with arguments added (as sharding does)."""
    after = _command_lines(new)
    return [
        f"drops the check command `{command}`"
        for command in dict.fromkeys(_check_commands(old))
        if not any(line == command or line.startswith(command + " ") for line in after)
    ]


def _actions_kept(old: dict[Any, Any], new: dict[Any, Any]) -> list[str]:
    before, after = Counter(_actions(old)), Counter(_actions(new))
    problems = [
        f"drops the check action `{name}`"
        for name in sorted(before)
        if name not in after and not _SETUP_ACTION.search(name) and name != _UPLOAD_ACTION
    ]
    if after[_UPLOAD_ACTION] < before[_UPLOAD_ACTION]:
        problems.append("uploads fewer artifacts; the Proof lane needs failure screenshots and videos")
    if sum(after[n] for n in after if _RETRY_ACTION.search(n)) > sum(
        before[n] for n in before if _RETRY_ACTION.search(n)
    ):
        problems.append("adds a retry action; a check that needs a retry has a bug")
    return problems


def _matrix_kept(old: dict[Any, Any], new: dict[Any, Any]) -> list[str]:
    problems = []
    new_jobs = _jobs(new)
    for job_id, job in _jobs(old).items():
        if job_id not in new_jobs:
            continue
        before, after = _matrix(job), _matrix(new_jobs[job_id])
        for key, values in before.items():
            if key == "exclude":
                continue
            if lost := values - after.get(key, set()):
                problems.append(f"job `{job_id}` drops matrix {key} values {sorted(lost)}")
        if len(after.get("exclude", set())) > len(before.get("exclude", set())):
            problems.append(f"job `{job_id}` excludes more matrix combinations")
    return problems


def _matrix(job: dict[Any, Any]) -> dict[str, set[str]]:
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return {}
    return {str(key): {repr(v) for v in values} for key, values in matrix.items() if isinstance(values, list)}


def _no_new_weakening(old: dict[Any, Any], new: dict[Any, Any]) -> list[str]:
    problems = []
    before, after = "\n".join(_command_lines(old)), "\n".join(_command_lines(new))
    for label, pattern in WEAKENING_FLAGS:
        if len(pattern.findall(after)) > len(pattern.findall(before)):
            problems.append(f"adds a command that {label}")
    if _count(new, _continues_on_error) > _count(old, _continues_on_error):
        problems.append("adds `continue-on-error`, which hides failures")
    if _count(new, _disabled) > _count(old, _disabled):
        problems.append("adds `if: false`, which turns a check off")
    return problems


def _count(workflow: dict[Any, Any], predicate: Callable[[dict[Any, Any]], bool]) -> int:
    return sum(1 for block in _blocks(workflow) if predicate(block))


def _continues_on_error(block: dict[Any, Any]) -> bool:
    value = block.get("continue-on-error")
    return value is True or (isinstance(value, str) and "true" in value.lower())


def _disabled(block: dict[Any, Any]) -> bool:
    value = block.get("if")
    return value is False or (isinstance(value, str) and bool(_DISABLED_IF.match(value)))


def _timeouts_kept(old: dict[Any, Any], new: dict[Any, Any]) -> list[str]:
    problems = []
    new_jobs = _jobs(new)
    for job_id, job in _jobs(old).items():
        if job_id not in new_jobs:
            continue  # a removed job is judged by the commands it ran, in _commands_kept
        before = job.get("timeout-minutes")
        after = new_jobs[job_id].get("timeout-minutes")
        if not isinstance(before, int | float):
            continue
        if after is None or (isinstance(after, int | float) and after > before):
            problems.append(f"job `{job_id}` widens its timeout from {before} minutes")
    return problems


def _setup_changes(old: dict[Any, Any], new: dict[Any, Any]) -> list[str]:
    after = set(_command_lines(new))
    return [
        f"changes the install command `{line}`"
        for line in dict.fromkeys(_command_lines(old))
        if _SETUP_COMMAND.match(line) and line not in after
    ]

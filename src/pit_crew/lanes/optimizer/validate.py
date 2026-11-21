"""Static safety guard for an Optimizer change: CI may get faster, it may never check less.

Pure functions over file text, so the host can run them without a checkout of the repository.
Every check (a command, or a non-setup action with its inputs) is compared together with where
it runs: runner, container, toolchain versions and working directory, per matrix combination.
A change fails the guard when, compared with the original workflow, it:

- no longer parses, uses YAML aliases, is too large, or has no jobs;
- drops a trigger event, narrows a trigger (paths, ignores, tags), or drops a check from any
  runner, OS, toolchain version or directory it ran in;
- keeps a job id but moves its checks elsewhere without making that job wait on them and fail
  when they fail (branch protection may require the job by name);
- removes matrix values or adds matrix exclusions;
- adds ``continue-on-error``, a disabled ``if:``, a wider ``timeout-minutes``, or retries;
- adds command flags that hide failures or run fewer tests (``|| true``, ``-k``, ``--exit-zero``...);
- uploads fewer artifacts (the Proof lane needs failure screenshots and videos).

Standalone install commands and setup or cache actions may change freely: they check nothing,
and if one breaks, the checks after it fail. An install chained to a check (``npm ci && npm
test``) is split, and the check part must survive.
"""

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import yaml
from yaml.tokens import AliasToken

WORKFLOW_DIR = ".github/workflows/"
YAML_SUFFIXES = (".yml", ".yaml")
MAX_FILE_CHARS = 200_000
MAX_MATRIX_COMBINATIONS = 256

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
_SHELL_SEPARATOR = re.compile(r"\s*(?:&&|\|\||;)\s*")
_NO_OP_SEGMENTS = frozenset({"", "true", ":", "exit 0", "then", "else", "fi", "do", "done", "esac", "{", "}"})
_MATRIX_EXPRESSION = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_READS_NEEDS = re.compile(r"\bneeds\b")
_PUSH_REF_FILTERS = frozenset({"branches", "branches-ignore", "tags", "tags-ignore"})
_NARROWING_FILTERS = ("paths", "paths-ignore", "branches-ignore", "tags-ignore")

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

Workflow = dict[Any, Any]
Context = tuple[str, str, str, str]  # runner, container, toolchain, working directory


class MatrixTooLargeError(ValueError):
    """Raised when a job's matrix expands to more combinations than the guard will check."""

    def __init__(self, job_id: str) -> None:
        """Name the job whose matrix is too large."""
        super().__init__(f"job `{job_id}` expands to more than {MAX_MATRIX_COMBINATIONS} matrix combinations")


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


@dataclass(frozen=True)
class _Unit:
    """One check, and the place it runs in one matrix combination."""

    check: str
    context: Context


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


def _compare_workflow(path: str, before: str, new: Workflow, report: GuardReport) -> None:
    if not _jobs(new):
        report.problems.append(f"{path}: has no jobs")
        return
    if not before.strip():
        report.warnings.append(f"{path}: new workflow; nothing to compare against")
        return
    old = _load(path, before, GuardReport())
    if old is None:
        report.problems.append(f"{path}: the original does not parse, so the change cannot be checked")
        return
    try:
        for compare in _COMPARISONS:
            report.problems.extend(f"{path}: {problem}" for problem in compare(old, new))
    except MatrixTooLargeError as exc:
        report.problems.append(f"{path}: {exc}")
        return
    report.warnings.extend(f"{path}: {note}" for note in _setup_changes(old, new))


def _load(path: str, text: str, report: GuardReport) -> Workflow | None:
    if len(text) > MAX_FILE_CHARS:
        report.problems.append(f"{path}: is larger than {MAX_FILE_CHARS} characters")
        return None
    try:
        # Aliases can expand a small file into a huge one; workflows never need them.
        if any(isinstance(token, AliasToken) for token in yaml.scan(text, Loader=yaml.SafeLoader)):
            report.problems.append(f"{path}: uses YAML aliases (*name); write the values out")
            return None
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        report.problems.append(f"{path}: is not valid YAML ({exc})")
        return None
    if not isinstance(data, dict):
        report.problems.append(f"{path}: is not a YAML mapping")
        return None
    return data


# --- reading a workflow -------------------------------------------------------------------


def _jobs(workflow: Workflow) -> dict[str, Workflow]:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    return {str(job_id): job for job_id, job in jobs.items() if isinstance(job, dict)}


def _job_steps(job: Workflow) -> list[Workflow]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _steps(workflow: Workflow) -> Iterator[Workflow]:
    for job in _jobs(workflow).values():
        yield from _job_steps(job)


def _blocks(workflow: Workflow) -> Iterator[Workflow]:
    """Every job and every step: the places ``if:`` and ``continue-on-error`` can appear."""
    yield from _jobs(workflow).values()
    yield from _steps(workflow)


def _events(workflow: Workflow) -> dict[str, Any]:
    """Trigger events mapped to their configuration (None when unfiltered)."""
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on:` key as True
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {str(event): None for event in on}
    if isinstance(on, dict):
        return {str(event): config for event, config in on.items()}
    return {}


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _lines(run: str) -> list[str]:
    lines = []
    for raw in run.replace("\\\n", " ").splitlines():
        line = " ".join(raw.split())
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _segments(run: str) -> list[str]:
    """Each command of a run block, with `&&`, `||` and `;` chains split apart."""
    return [seg for line in _lines(run) for seg in _SHELL_SEPARATOR.split(line) if seg not in _NO_OP_SEGMENTS]


def _command_lines(workflow: Workflow) -> list[str]:
    return [line for step in _steps(workflow) if isinstance(step.get("run"), str) for line in _lines(step["run"])]


def _step_checks(step: Workflow) -> list[str]:
    run, uses = step.get("run"), step.get("uses")
    if isinstance(run, str):
        return [seg for seg in _segments(run) if not _SETUP_COMMAND.match(seg)]
    if isinstance(uses, str):
        name = uses.split("@", 1)[0]
        if not _SETUP_ACTION.search(name) and name != _UPLOAD_ACTION:
            return [f"uses {name} {_text(step.get('with'))}".rstrip()]
    return []


def _job_checks(job: Workflow) -> list[str]:
    checks = [check for step in _job_steps(job) for check in _step_checks(step)]
    if isinstance(job.get("uses"), str):  # a job that calls a reusable workflow
        checks.append(f"uses {job['uses'].split('@', 1)[0]} {_text(job.get('with'))}".rstrip())
    return checks


def _covered(check: str, candidates: Iterable[str]) -> bool:
    """True when a candidate runs ``check`` exactly or with arguments added (as sharding does)."""
    return any(c == check or c.startswith(check + " ") for c in candidates)


def _actions(workflow: Workflow) -> list[str]:
    """Action names without their version, from steps and from reusable-workflow jobs."""
    uses = [step.get("uses") for step in _steps(workflow)]
    uses += [job.get("uses") for job in _jobs(workflow).values()]
    return [ref.split("@", 1)[0] for ref in uses if isinstance(ref, str)]


# --- matrix expansion and execution contexts ------------------------------------------------


def _matrix_spec(job: Workflow) -> Workflow:
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    return matrix if isinstance(matrix, dict) else {}


def _entries(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [{str(k): _text(v) for k, v in entry.items()} for entry in value if isinstance(entry, dict)]


def _combinations(job_id: str, job: Workflow) -> list[dict[str, str]]:
    spec = _matrix_spec(job)
    axes = {str(k): v for k, v in spec.items() if k not in ("include", "exclude") and isinstance(v, list)}
    combos: list[dict[str, str]] = [{}] if axes or not spec.get("include") else []
    for key, values in axes.items():
        combos = [{**combo, key: _text(value)} for combo in combos for value in values]
        if len(combos) > MAX_MATRIX_COMBINATIONS:
            raise MatrixTooLargeError(job_id)
    excluded = _entries(spec.get("exclude"))
    combos = [c for c in combos if not any(all(c.get(k) == v for k, v in e.items()) for e in excluded)]
    combos += _entries(spec.get("include"))
    if len(combos) > MAX_MATRIX_COMBINATIONS:
        raise MatrixTooLargeError(job_id)
    return combos


def _substitute(text: str, combo: dict[str, str]) -> str:
    return _MATRIX_EXPRESSION.sub(lambda m: combo.get(m.group(1), m.group(0)), text)


def _toolchain(job: Workflow) -> str:
    """Versions pinned by setup actions, e.g. ``actions/setup-node:node-version=20``."""
    parts = []
    for step in _job_steps(job):
        uses, inputs = step.get("uses"), step.get("with")
        if isinstance(uses, str) and "setup" in uses.lower() and isinstance(inputs, dict):
            name = uses.split("@", 1)[0]
            parts += [f"{name}:{k}={_text(v)}" for k, v in sorted(inputs.items(), key=str) if "version" in str(k)]
    return ";".join(parts)


def _default_directory(job: Workflow) -> str:
    defaults = job.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    return _text(run.get("working-directory")) if isinstance(run, dict) else ""


def _units(workflow: Workflow) -> set[_Unit]:
    units: set[_Unit] = set()
    for job_id, job in _jobs(workflow).items():
        for combo in _combinations(job_id, job):
            units.update(_job_units(job, combo))
    return units


def _job_units(job: Workflow, combo: dict[str, str]) -> Iterator[_Unit]:
    runner = _substitute(_text(job.get("runs-on")), combo)
    container = _substitute(_text(job.get("container")), combo)
    toolchain = _substitute(_toolchain(job), combo)
    directory = _default_directory(job)
    for step in _job_steps(job):
        context = (runner, container, toolchain, _text(step.get("working-directory")) or directory)
        for check in _step_checks(step):
            yield _Unit(_substitute(check, combo), context)
    if isinstance(job.get("uses"), str):
        for check in _job_checks({"uses": job["uses"], "with": job.get("with")}):
            yield _Unit(_substitute(check, combo), (runner, container, toolchain, directory))


def _describe(context: Context) -> str:
    runner, container, toolchain, directory = context
    where = runner or "the default runner"
    where += f" in {container}" if container else ""
    where += f" with {toolchain}" if toolchain else ""
    return where + (f" in {directory}" if directory else "")


# --- comparisons: each returns the problems it found ----------------------------------------


def _triggers_kept(old: Workflow, new: Workflow) -> list[str]:
    before, after = _events(old), _events(new)
    problems = [f"no longer runs on `{event}`" for event in sorted(set(before) - set(after))]
    for event in sorted(set(before) & set(after)):
        old_filters = before[event] if isinstance(before[event], dict) else {}
        new_filters = after[event] if isinstance(after[event], dict) else {}
        problems += _narrowed(event, old_filters, new_filters)
    return problems


def _narrowed(event: str, before: Workflow, after: Workflow) -> list[str]:
    added = [key for key in _NARROWING_FILTERS if key in after and key not in before]
    if event != "push" and "branches" in after and "branches" not in before:
        added.append("branches")
    problems = [f"`{event}` gains a `{key}` filter, so it runs less often" for key in added]
    if event == "push" and not _PUSH_REF_FILTERS & before.keys() and "branches" in after and "tags" not in after:
        problems.append("`push` gains a branch filter and stops running for tags; add `tags: ['**']`")
    return problems


def _checks_kept(old: Workflow, new: Workflow) -> list[str]:
    """Every check must still run in every context it ran in, exactly or with arguments added."""
    after: dict[Context, list[str]] = {}
    for unit in _units(new):
        after.setdefault(unit.context, []).append(unit.check)
    missing: dict[str, set[str]] = {}
    for unit in _units(old):
        if not _covered(unit.check, after.get(unit.context, ())):
            missing.setdefault(unit.check, set()).add(_describe(unit.context))
    return [f"drops the check `{check}` on {'; '.join(sorted(missing[check]))}" for check in sorted(missing)]


def _kept_jobs_still_gate(old: Workflow, new: Workflow) -> list[str]:
    """A job that keeps its id but hands its checks to other jobs must wait on them and fail with them."""
    problems = []
    new_jobs = _jobs(new)
    for job_id, job in _jobs(old).items():
        if job_id not in new_jobs:
            continue
        kept = new_jobs[job_id]
        elsewhere = [check for other, j in new_jobs.items() if other != job_id for check in _job_checks(j)]
        # A check that runs nowhere any more is reported by _checks_kept; here, only moved ones.
        moved = [c for c in _job_checks(job) if not _covered(c, _job_checks(kept)) and _covered(c, elsewhere)]
        needed = [_job_checks(new_jobs[n]) for n in _needs(kept) if n in new_jobs]
        gated = all(any(_covered(c, checks) for checks in needed) for c in moved)
        if moved and not (gated and _aggregates(kept)):
            problems.append(
                f"job `{job_id}` no longer runs `{moved[0]}` itself; it must `needs:` the job that does, "
                "run with `if: always()`, and fail unless every needed job succeeded"
            )
    return problems


def _needs(job: Workflow) -> list[str]:
    needs = job.get("needs")
    if isinstance(needs, str):
        return [needs]
    return [str(n) for n in needs] if isinstance(needs, list) else []


def _aggregates(job: Workflow) -> bool:
    """True for a job that always runs and has a step reading its dependencies' results."""
    condition = job.get("if")
    always = isinstance(condition, str) and "always()" in condition
    reads_needs = any(isinstance(s.get("run"), str) and _READS_NEEDS.search(s["run"]) for s in _job_steps(job))
    return always and reads_needs


def _actions_kept(old: Workflow, new: Workflow) -> list[str]:
    before, after = Counter(_actions(old)), Counter(_actions(new))
    problems = []
    if after[_UPLOAD_ACTION] < before[_UPLOAD_ACTION]:
        problems.append("uploads fewer artifacts; the Proof lane needs failure screenshots and videos")
    if sum(after[n] for n in after if _RETRY_ACTION.search(n)) > sum(
        before[n] for n in before if _RETRY_ACTION.search(n)
    ):
        problems.append("adds a retry action; a check that needs a retry has a bug")
    return problems


def _matrix_kept(old: Workflow, new: Workflow) -> list[str]:
    problems = []
    new_jobs = _jobs(new)
    for job_id, job in _jobs(old).items():
        if job_id not in new_jobs:
            continue
        before, after = _matrix_values(job), _matrix_values(new_jobs[job_id])
        for key, values in before.items():
            if key == "exclude":
                continue
            if lost := values - after.get(key, set()):
                problems.append(f"job `{job_id}` drops matrix {key} values {sorted(lost)}")
        if len(after.get("exclude", set())) > len(before.get("exclude", set())):
            problems.append(f"job `{job_id}` excludes more matrix combinations")
    return problems


def _matrix_values(job: Workflow) -> dict[str, set[str]]:
    return {str(k): {_text(v) for v in values} for k, values in _matrix_spec(job).items() if isinstance(values, list)}


def _no_new_weakening(old: Workflow, new: Workflow) -> list[str]:
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


def _count(workflow: Workflow, predicate: Callable[[Workflow], bool]) -> int:
    return sum(1 for block in _blocks(workflow) if predicate(block))


def _continues_on_error(block: Workflow) -> bool:
    value = block.get("continue-on-error")
    return value is True or (isinstance(value, str) and "true" in value.lower())


def _disabled(block: Workflow) -> bool:
    value = block.get("if")
    return value is False or (isinstance(value, str) and bool(_DISABLED_IF.match(value)))


def _timeouts_kept(old: Workflow, new: Workflow) -> list[str]:
    problems = []
    new_jobs = _jobs(new)
    for job_id, job in _jobs(old).items():
        if job_id not in new_jobs:
            continue  # a removed job is judged by the checks it ran, in _checks_kept
        before = job.get("timeout-minutes")
        after = new_jobs[job_id].get("timeout-minutes")
        if not isinstance(before, int | float):
            continue
        if after is None or (isinstance(after, int | float) and after > before):
            problems.append(f"job `{job_id}` widens its timeout from {before} minutes")
    return problems


def _setup_changes(old: Workflow, new: Workflow) -> list[str]:
    after = {seg for line in _command_lines(new) for seg in _segments(line)}
    before = (seg for line in _command_lines(old) for seg in _segments(line))
    return [
        f"changes the install command `{seg}`"
        for seg in dict.fromkeys(before)
        if _SETUP_COMMAND.match(seg) and seg not in after
    ]


_COMPARISONS: tuple[Callable[[Workflow, Workflow], list[str]], ...] = (
    _triggers_kept,
    _checks_kept,
    _kept_jobs_still_gate,
    _actions_kept,
    _matrix_kept,
    _no_new_weakening,
    _timeouts_kept,
)

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
and if one breaks, the checks after it fail. In a chained line (``npm ci && npm test``) the
install becomes ``<install>`` and the operators stay, so the checks and how they chain must
survive. A gate job must use a recognized assertion that fails unless every needed job succeeded.
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
    r"^(?:\w+=\S*\s+)*(?:sudo\s+)?(?:"
    r"(?:npx\s+|pnpm\s+(?:exec\s+)?|yarn\s+|bunx\s+)?playwright\s+install\b"
    r"|(?:npm|pnpm|yarn)\s+(?:env\s+use|runtime\s+set)\b|"
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
_SETUP_ACTION = re.compile(
    r"(?:^actions/checkout$|setup|cache|toolchain|install-action|^actions/download-artifact$)", re.IGNORECASE
)
_UPLOAD_ACTION = "actions/upload-artifact"
_RETRY_ACTION = re.compile(r"retry", re.IGNORECASE)
_BROWSER_TESTS = re.compile(r"\b(?:playwright|cypress|wdio|webdriverio|puppeteer)\b", re.IGNORECASE)
# A package script that by its name runs browser tests: `npm run test:e2e`, `pnpm playwright`...
_BROWSER_SCRIPT = re.compile(
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?[\w:.-]*(?:e2e|playwright|cypress|browser|wdio|puppeteer)[\w:.-]*",
    re.IGNORECASE,
)
_RECORDING_PATH = re.compile(
    r"test-results|playwright-report|cypress/(?:videos|screenshots)|screenshots|videos|traces?\b|\.(?:webm|mp4|zip)\b",
    re.IGNORECASE,
)
_RUNS_ON_FAILURE = re.compile(r"\b(?:failure|always)\(\)|!\s*cancelled\(\)")
_DISABLED_IF = re.compile(r"^\s*(?:\$\{\{\s*)?false(?:\s*\}\})?\s*$", re.IGNORECASE)
_SHELL_SEPARATOR = re.compile(r"\s*(?:&&|\|\||;)\s*")
_NO_OP_SEGMENTS = frozenset({"", "true", ":", "exit 0", "then", "else", "fi", "do", "done", "esac", "{", "}"})
# Commands that print, set variables or move around: they decide nothing about pass or fail, so
# dropping one is not dropping a check. `exit 1`, `test`, `[ ... ]` and real tools stay checks.
_NOT_A_CHECK = re.compile(
    r"^(?:"
    r"(?:echo|printf)\b"  # output, including >> $GITHUB_ENV / _OUTPUT / _STEP_SUMMARY
    r"|(?:ls|mv|cp|rm|touch|chmod|ln)\s"  # moving files around
    r"|git\s+(?:clone|fetch|checkout|switch|config|apply|submodule|remote)\b"  # getting code; `git diff` stays a check
    r"|export\s+\w+="
    r"|\$env:\w+\s*="  # PowerShell assignment
    r"|\w+=(?:\S*|\"[^\"]*\"|'[^']*')$"  # a bare shell assignment, X=... with no command after it
    r"|\w+=[\"']?\$\(.*\)[\"']?$"  # X="$(...)": capturing output to a variable
    r"|(?:done|fi|esac)\b"  # closing a block, maybe redirecting its output
    r"|cd\s|pushd\s|popd\b|mkdir\s|set\s+[-+][a-z]+(?:\s+\w+)?$"
    r"|(?:if|elif|while|until|for|case)\b.*(?:then|do|in)$"  # control-flow headers; their bodies are lines too
    r")"
)
_MATRIX_EXPRESSION = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_MASKING_TAIL = re.compile(r"\s*\|\|\s*(?:true|:|exit\s+0)\s*$")
_EXTENSION_OPERATOR = re.compile(r"[;&|]")
# Arguments a check may gain: they split, parallelize or report on the same tests; none runs fewer.
_SAFE_VALUED_OPTION = re.compile(
    r"--?(?:shard|splits|group|partition|n|numprocesses|dist|maxWorkers|max-workers|workers|j|jobs|"
    r"test-threads|reporter|reporters|junitxml|cov|cov-report|durations|maxfail|color|profile)(?:=\S+)?"
)
_SAFE_FLAG = re.compile(
    r"--?(?:ci|q|quiet|v|verbose|silent|no-cache|frozen|locked|offline|bail|x|exitfirst|no-fail-fast|"
    r"nocapture|show-output|runInBand|forceExit|detectOpenHandles|logHeapUsage)"
)
_NO_ERREXIT_SHELL = re.compile(r"\{0\}")
_ERREXIT = re.compile(r"(?:^|\s)-[a-z]*e|errexit")
_ALWAYS = re.compile(r"^\s*(?:\$\{\{\s*)?always\(\)(?:\s*\}\})?\s*$")
_JQ_GATE = re.compile(r'toJSON\(needs\).*\|\s*jq\s+-e\b.*\ball\(.*\.result\s*==\s*"success"')
_GATE_ACTION = "re-actors/alls-green"
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
    ("excludes tests", re.compile(r"--(?:grep-invert|exclude)\b")),
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
    report.warnings.extend(f"{path}: {note}" for note in _missing_recordings(new))


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


def run_lines(run: str) -> list[str]:
    """The command lines of a run block: backslash continuations joined, blanks and comments dropped."""
    lines = []
    for raw in run.replace("\\\n", " ").splitlines():
        line = " ".join(raw.split())
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _segments(run: str) -> list[str]:
    """Each command of a run block, with `&&`, `||` and `;` chains split apart."""
    return [seg for line in run_lines(run) for seg in _SHELL_SEPARATOR.split(line) if seg not in _NO_OP_SEGMENTS]


def _command_lines(workflow: Workflow) -> list[str]:
    return [line for step in _steps(workflow) if isinstance(step.get("run"), str) for line in run_lines(step["run"])]


def split_shell(line: str) -> tuple[list[str], list[str]]:
    """Split a command line on `&&`, `||` and `;` outside quotes: (commands, operators between them)."""
    commands, operators, current, quote, i = [], [], "", "", 0
    while i < len(line):
        char = line[i]
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif line.startswith(("&&", "||"), i) or char == ";":
            operator = ";" if char == ";" else line[i : i + 2]
            commands.append(current.strip())
            operators.append(operator)
            current, i = "", i + len(operator)
            continue
        current += char
        i += 1
    return [*commands, current.strip()], operators


def check_line(line: str) -> str:
    """A run line as a check: install commands become `<install>`, shell operators are kept.

    Keeping `&&`, `||` and `;` means rewiring how two checks chain is a change. Returns "" for a
    line that only installs, prints or sets variables (`echo ... >> $GITHUB_ENV`, `export X=1`,
    `cd dir`); those decide nothing about pass or fail. A trailing `|| true` is dropped: removing one
    makes CI stricter, and adding one is caught by WEAKENING_FLAGS.
    """
    commands, operators = split_shell(_MASKING_TAIL.sub("", line))
    if all(c in _NO_OP_SEGMENTS or _SETUP_COMMAND.match(c) or _NOT_A_CHECK.match(c) for c in commands):
        return ""
    normalized = [
        "<install>" if _SETUP_COMMAND.match(c) else "<setup>" if _NOT_A_CHECK.match(c) else c for c in commands
    ]
    return normalized[0] + "".join(f" {op} {cmd}" for op, cmd in zip(operators, normalized[1:], strict=True))


def _step_checks(step: Workflow) -> list[str]:
    run, uses = step.get("run"), step.get("uses")
    if isinstance(run, str):
        return [check for line in run_lines(run) if (check := check_line(line))]
    if isinstance(uses, str):
        name = uses.split("@", 1)[0]
        if not _SETUP_ACTION.search(name) and name != _UPLOAD_ACTION:
            return [f"uses {name} {_text(step.get('with'))}".rstrip()]
    return []


def covered(check: str, candidates: Iterable[str]) -> bool:
    """True when a candidate runs ``check`` exactly, or with safe arguments added (as sharding does).

    Added text may not chain another command (`npm test || npm run coverage`), and every added
    argument must be one that splits, parallelizes or reports on the same tests: `--shard=1/2`,
    `-n auto` or `--ci` keep `pytest` covered; a path, `-m slow` or `--project chromium` do not.
    """
    return any(c == check or (c.startswith(check + " ") and safe_extension(c[len(check) :])) for c in candidates)


def safe_extension(extra: str) -> bool:
    """True when every argument added to a command is a known-safe option or its value."""
    if _EXTENSION_OPERATOR.search(extra):
        return False
    expects_value = False
    for arg in extra.split():
        if _SAFE_VALUED_OPTION.fullmatch(arg):
            expects_value = "=" not in arg
        elif arg == "--" or _SAFE_FLAG.fullmatch(arg) or (expects_value and not arg.startswith("-")):
            expects_value = False  # `--` separates a wrapper's options from the test runner's
        else:
            return False
    return True


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
    """The matrix rows GitHub runs: the product of the axes, minus `exclude`, plus `include`."""
    spec = _matrix_spec(job)
    axes = {str(k): v for k, v in spec.items() if k not in ("include", "exclude") and isinstance(v, list)}
    base: list[dict[str, str]] = [{}] if axes or not spec.get("include") else []
    for key, values in axes.items():
        base = [{**row, key: _text(value)} for row in base for value in values]
        if len(base) > MAX_MATRIX_COMBINATIONS:
            raise MatrixTooLargeError(job_id)
    excluded = _entries(spec.get("exclude"))
    base = [row for row in base if not any(all(row.get(k) == v for k, v in e.items()) for e in excluded)]
    extra = [entry for entry in _entries(spec.get("include")) if not _merge_include(base, entry, set(axes))]
    if len(base) + len(extra) > MAX_MATRIX_COMBINATIONS:
        raise MatrixTooLargeError(job_id)
    return base + extra


def _merge_include(rows: list[dict[str, str]], entry: dict[str, str], axes: set[str]) -> bool:
    """Add ``entry``'s new keys to every row it does not contradict. False if it fits none."""
    merged = False
    for row in rows:
        if all(row.get(k) == v for k, v in entry.items() if k in axes):
            row.update({k: v for k, v in entry.items() if k not in axes})
            merged = True
    return merged


def _substitute(text: str, combo: dict[str, str]) -> str:
    return _MATRIX_EXPRESSION.sub(lambda m: combo.get(m.group(1), m.group(0)), text)


def _setup_versions(step: Workflow) -> dict[str, str]:
    """Versions a setup step pins, e.g. ``{"actions/setup-node:node-version": "20"}``."""
    uses, inputs = step.get("uses"), step.get("with")
    if not (isinstance(uses, str) and "setup" in uses.lower() and isinstance(inputs, dict)):
        return {}
    name = uses.split("@", 1)[0]
    return {f"{name}:{k}": _text(v) for k, v in inputs.items() if "version" in str(k)}


def _default_directory(job: Workflow) -> str:
    defaults = job.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    return _text(run.get("working-directory")) if isinstance(run, dict) else ""


def _units_by_job(workflow: Workflow) -> dict[str, set[_Unit]]:
    return {
        job_id: {unit for combo in _combinations(job_id, job) for unit in _job_units(job, combo)}
        for job_id, job in _jobs(workflow).items()
    }


def _job_units(job: Workflow, combo: dict[str, str]) -> Iterator[_Unit]:
    runner = _substitute(_text(job.get("runs-on")), combo)
    container = _substitute(_text(job.get("container")), combo)
    directory = _default_directory(job)
    active: dict[str, str] = {}  # setup versions installed so far; steps run in order
    for step in _job_steps(job):
        active.update(_setup_versions(step))
        toolchain = _substitute(";".join(f"{k}={v}" for k, v in sorted(active.items())), combo)
        context = (runner, container, toolchain, _text(step.get("working-directory")) or directory)
        for check in _step_checks(step):
            yield _Unit(_substitute(check, combo), context)
    if isinstance(job.get("uses"), str):  # a job that calls a reusable workflow
        check = f"uses {job['uses'].split('@', 1)[0]} {_text(job.get('with'))}".rstrip()
        yield _Unit(_substitute(check, combo), (runner, container, "", directory))


def _index(units: Iterable[_Unit]) -> dict[Context, list[str]]:
    index: dict[Context, list[str]] = {}
    for unit in units:
        index.setdefault(unit.context, []).append(unit.check)
    return index


def _runs(unit: _Unit, index: dict[Context, list[str]]) -> bool:
    return covered(unit.check, index.get(unit.context, ()))


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
    after = _index(unit for units in _units_by_job(new).values() for unit in units)
    missing: dict[str, set[str]] = {}
    for units in _units_by_job(old).values():
        for unit in units:
            if not _runs(unit, after):
                missing.setdefault(unit.check, set()).add(_describe(unit.context))
    return [f"drops the check `{check}` on {'; '.join(sorted(missing[check]))}" for check in sorted(missing)]


def _kept_jobs_still_gate(old: Workflow, new: Workflow) -> list[str]:
    """A job that keeps its id but hands checks to other jobs must wait on them and fail with them."""
    problems = []
    old_units, new_units, new_jobs = _units_by_job(old), _units_by_job(new), _jobs(new)
    for job_id, units in old_units.items():
        if job_id not in new_units:
            continue
        here = _index(new_units[job_id])
        elsewhere = _index(u for other, us in new_units.items() if other != job_id for u in us)
        # A check that runs nowhere any more is reported by _checks_kept; here, only moved ones.
        moved = [u for u in units if not _runs(u, here) and _runs(u, elsewhere)]
        if not moved:
            continue
        needed = _index(u for n in _needs(new_jobs[job_id]) for u in new_units.get(n, ()))
        if not (all(_runs(u, needed) for u in moved) and _aggregates(new_jobs[job_id])):
            problems.append(
                f"job `{job_id}` no longer runs `{min(u.check for u in moved)}` itself; it must `needs:` "
                "every job that does, run with `if: always()`, and fail unless every needed job succeeded"
            )
    return problems


def _needs(job: Workflow) -> list[str]:
    needs = job.get("needs")
    if isinstance(needs, str):
        return [needs]
    return [str(n) for n in needs] if isinstance(needs, list) else []


def _aggregates(job: Workflow) -> bool:
    """True for a gate job that always runs and has an unconditional step failing on any bad result."""
    condition = job.get("if")
    if not (isinstance(condition, str) and _ALWAYS.match(condition)):
        return False
    return any(_asserts_needs(step) for step in _job_steps(job))


def _asserts_needs(step: Workflow) -> bool:
    """A recognized step that fails unless every needed job succeeded (skipped and cancelled fail too)."""
    if "if" in step or _continues_on_error(step):
        return False
    run, uses = step.get("run"), step.get("uses")
    if isinstance(run, str):
        masks = WEAKENING_FLAGS[0][1]
        return any(_JQ_GATE.search(line) and not masks.search(line) for line in run_lines(run))
    if isinstance(uses, str) and uses.split("@", 1)[0] == _GATE_ACTION:
        inputs = step.get("with")
        return isinstance(inputs, dict) and "toJSON(needs)" in _text(inputs.get("jobs"))
    return False


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
            if key in ("include", "exclude"):  # include and exclude are judged by the rows they produce
                continue
            if lost := values - after.get(key, set()):
                problems.append(f"job `{job_id}` drops matrix {key} values {sorted(lost)}")
        if len(after.get("exclude", set())) > len(before.get("exclude", set())):
            problems.append(f"job `{job_id}` excludes more matrix combinations")
        problems += _rows_kept(job_id, job, new_jobs[job_id])
    return problems


def _rows_kept(job_id: str, old: Workflow, new: Workflow) -> list[str]:
    """Every original matrix row must still run, matched on all of its keys.

    A row can matter only through a value a step's `if:` or `env` reads (FastAPI's
    `without-httpx2` row), so commands and runners alone cannot tell rows apart. A new row may add
    keys, such as `shard`, and still stands for the old one.
    """
    new_rows = _combinations(job_id, new)
    return [
        f"job `{job_id}` drops the matrix row {json.dumps(row, sort_keys=True)}"
        for row in _combinations(job_id, old)
        if row and not any(all(candidate.get(k) == v for k, v in row.items()) for candidate in new_rows)
    ]


def _matrix_values(job: Workflow) -> dict[str, set[str]]:
    return {str(k): {_text(v) for v in values} for k, values in _matrix_spec(job).items() if isinstance(values, list)}


def _no_new_weakening(old: Workflow, new: Workflow) -> list[str]:
    problems = []
    # Flags can hide in env too, e.g. PYTEST_ADDOPTS: -k smoke.
    before = "\n".join([*_command_lines(old), *_env_lines(old)])
    after = "\n".join([*_command_lines(new), *_env_lines(new)])
    for label, pattern in WEAKENING_FLAGS:
        if len(pattern.findall(after)) > len(pattern.findall(before)):
            problems.append(f"adds a command that {label}")
    if _count(new, _continues_on_error) > _count(old, _continues_on_error):
        problems.append("adds `continue-on-error`, which hides failures")
    problems += _continue_on_error_widened(old, new)
    if _count(new, _disabled) > _count(old, _disabled):
        problems.append("adds `if: false`, which turns a check off")
    if _no_errexit_shells(new) > _no_errexit_shells(old):
        problems.append("adds a custom `shell:` without `-e`, so a failing line in a step no longer fails it")
    return problems


def _continue_on_error_widened(old: Workflow, new: Workflow) -> list[str]:
    """A job whose `continue-on-error` changes to a value that can be true more often.

    `${{ matrix.xfail && true || false }}` becoming `true` lets every row fail, not only xfail rows;
    counting keys misses that, so kept jobs are compared value by value.
    """
    new_jobs = _jobs(new)
    return [
        f"job `{job_id}` widens `continue-on-error` to {after!r}"
        for job_id, job in _jobs(old).items()
        if job_id in new_jobs
        and (after := new_jobs[job_id].get("continue-on-error")) != job.get("continue-on-error")
        and _continues_on_error({"continue-on-error": after})
    ]


def _env_lines(workflow: Workflow) -> list[str]:
    blocks = [workflow, *_blocks(workflow)]
    return [f"{k}: {_text(v)}" for b in blocks if isinstance(b.get("env"), dict) for k, v in b["env"].items()]


def _no_errexit_shells(workflow: Workflow) -> int:
    """Steps and run defaults whose custom shell template (`bash {0}`) drops fail-on-error."""
    shells = [step.get("shell") for step in _steps(workflow)]
    for block in [workflow, *_jobs(workflow).values()]:
        defaults = block.get("defaults")
        run = defaults.get("run") if isinstance(defaults, dict) else None
        shells.append(run.get("shell") if isinstance(run, dict) else None)
    return sum(
        1
        for shell in shells
        if isinstance(shell, str) and _NO_ERREXIT_SHELL.search(shell) and not _ERREXIT.search(shell)
    )


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


def _missing_recordings(workflow: Workflow) -> list[str]:
    """Jobs that run browser tests without an upload that would carry their failure recordings."""
    notes = []
    for job_id, job in _jobs(workflow).items():
        steps = _job_steps(job)
        runs = [s["run"] for s in steps if isinstance(s.get("run"), str)]
        if not any(_BROWSER_TESTS.search(run) or _BROWSER_SCRIPT.search(run) for run in runs):
            continue
        uploads = [s for s in steps if isinstance(s.get("uses"), str) and s["uses"].startswith(_UPLOAD_ACTION)]
        if not uploads:
            notes.append(
                f"job `{job_id}` runs browser tests but uploads no failure recordings; "
                "apply the artifact-capture companion playbook"
            )
        elif not any(_uploads_recordings_on_failure(step) for step in uploads):
            notes.append(
                f"job `{job_id}` runs browser tests, but no upload both runs on failure and includes "
                "recording paths; check it, and apply the artifact-capture companion playbook if needed"
            )
    return notes


def _uploads_recordings_on_failure(step: Workflow) -> bool:
    """An upload that runs after a failed test (`failure()`, `always()`, `!cancelled()`) and carries recordings.

    By default a step runs only on success, so a plain coverage upload proves nothing.
    """
    inputs = step.get("with")
    path = _text(inputs.get("path")) if isinstance(inputs, dict) else ""
    return bool(_RUNS_ON_FAILURE.search(_text(step.get("if")))) and bool(_RECORDING_PATH.search(path))

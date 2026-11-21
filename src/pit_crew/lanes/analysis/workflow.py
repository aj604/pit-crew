"""Read a workflow file's job graph and map GitHub's job display names back to job ids.

The API reports jobs by display name ("test (ubuntu-latest, 3.12)"), while `needs:` refers
to job ids ("test"). Analysis needs both sides joined to build the dependency graph.
"""

import itertools
import re
from dataclasses import dataclass
from typing import Any

import yaml

_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")
SHA = re.compile(r"\b[0-9a-f]{40}\b")  # step names embed commit SHAs that differ per run; fold to "<sha>"
_MATRIX_EXPRESSION = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_MAX_EXPANSIONS = 1000
_NEEDS_REF = re.compile(r"needs\.([A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class StepSpec:
    """One entry under a job's `steps:`."""

    name: str  # what GitHub shows: `name:`, else "Run <uses>", else "Run <first line of run>"
    uses: str | None
    run: str | None
    with_: dict[str, str]
    if_: str | None


@dataclass(frozen=True)
class JobSpec:
    """One entry under `jobs:` in a workflow file."""

    id: str
    name: str  # the `name:` field, or the id when there is none; may contain ${{ }} expressions
    needs: tuple[str, ...]
    has_matrix: bool
    uses: str | None  # set when the job calls a reusable workflow
    steps: tuple[StepSpec, ...] = ()
    runs_on: str = ""
    matrix_keys: tuple[str, ...] = ()  # keys under strategy.matrix, minus include/exclude
    needs_refs: frozenset[str] = frozenset()  # jobs whose outputs this job reads via `needs.<id>`
    matrix_values: tuple[tuple[str, tuple[str, ...]], ...] = ()  # literal values per matrix key, include entries added


@dataclass(frozen=True)
class Workflow:
    """A workflow file's name and jobs, keyed by job id."""

    name: str
    jobs: dict[str, JobSpec]

    def job_id_for(self, display_name: str) -> str | None:
        """The job id a display name from the API belongs to, or None if it matches no job."""
        jobs = list(self.jobs.values())
        for job in jobs:  # a literal name is the strongest evidence
            if _EXPRESSION.search(job.name) is None and job.name == display_name:
                return job.id
        expanded = {job.id: _expand_name(job) for job in jobs}
        for job in jobs:  # `${{ matrix.key }}` names with known values are literal names in disguise
            if display_name in (expanded[job.id] or ()):
                return job.id
        # Names left over are patterns: most literal characters first, bare expressions last.
        patterns = sorted((j for j in jobs if expanded[j.id] is None), key=lambda j: -_literal_length(j.name))
        for job in patterns:
            if _matches(job, display_name):
                return job.id
        caller = display_name.split(" / ", 1)[0]  # reusable workflows report "caller / inner job"
        if caller != display_name:
            return self.job_id_for(caller)
        return None


def parse_workflow(text: str) -> Workflow:
    """Parse workflow YAML. Raises ValueError when a job needs a job that does not exist."""
    doc = yaml.safe_load(text) or {}
    jobs = {}
    for job_id, raw_body in (doc.get("jobs") or {}).items():
        body = raw_body or {}
        raw_needs = body.get("needs") or ()
        needs = (raw_needs,) if isinstance(raw_needs, str) else tuple(raw_needs)
        jobs[job_id] = JobSpec(
            id=job_id,
            name=str(body.get("name") or job_id),
            needs=needs,
            has_matrix=bool((body.get("strategy") or {}).get("matrix")),
            uses=body.get("uses"),
            steps=_parse_steps(body.get("steps")),
            runs_on=_runs_on(body.get("runs-on")),
            matrix_keys=_matrix_keys(body),
            needs_refs=_needs_refs(body, needs),
            matrix_values=_matrix_values(body),
        )
    for job in jobs.values():
        unknown = [n for n in job.needs if n not in jobs]
        if unknown:
            msg = f"job {job.id!r} needs unknown job(s) {unknown}"
            raise ValueError(msg)
    return Workflow(name=str(doc.get("name") or ""), jobs=jobs)


def _step_name(item: dict[str, Any]) -> str:
    if item.get("name"):
        return str(item["name"])
    if item.get("uses"):
        return f"Run {item['uses']}"
    lines = str(item.get("run") or "").strip().splitlines()
    return f"Run {lines[0].strip()}" if lines else "Run"


def _parse_steps(raw: object) -> tuple[StepSpec, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        StepSpec(
            name=_step_name(item),
            uses=str(item["uses"]) if item.get("uses") else None,
            run=str(item["run"]) if item.get("run") is not None else None,
            with_={str(k): str(v) for k, v in (item["with"] if isinstance(item.get("with"), dict) else {}).items()},
            if_=str(item["if"]) if item.get("if") is not None else None,
        )
        for item in raw
        if isinstance(item, dict)
    )


def _runs_on(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return ", ".join(str(r) for r in raw)
    return ""


def _matrix_keys(body: dict[str, Any]) -> tuple[str, ...]:
    matrix = (body.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict):
        return ()
    return tuple(str(k) for k in matrix if k not in ("include", "exclude"))


def _matrix_values(body: dict[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Literal values per matrix key: the key's list plus any `include` entries that set it."""
    matrix = (body.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict):
        return ()
    values: dict[str, list[str]] = {}
    for key, raw in matrix.items():
        if key in ("include", "exclude"):
            continue
        if isinstance(raw, list) and all(isinstance(v, str | int | float | bool) for v in raw):
            values[str(key)] = [str(v) for v in raw]
    includes = matrix.get("include")
    for entry in includes if isinstance(includes, list) else ():
        for key, value in entry.items() if isinstance(entry, dict) else ():
            if isinstance(value, str | int | float | bool) and (str(key) in values or key not in matrix):
                values.setdefault(str(key), []).append(str(value))
    return tuple((k, tuple(dict.fromkeys(v))) for k, v in values.items())


def _expand_name(job: JobSpec) -> tuple[str, ...] | None:
    """Every concrete display name of a job named only by `${{ matrix.<key> }}` expressions, or None."""
    expressions = _EXPRESSION.findall(job.name)
    keys = [m.group(1) if (m := _MATRIX_EXPRESSION.fullmatch(e)) else None for e in expressions]
    values = dict(job.matrix_values)
    if not expressions or any(k is None or k not in values for k in keys):
        return None
    used = list(dict.fromkeys(k for k in keys if k is not None))
    combos = 1
    for k in used:
        combos *= len(values[k])
    if combos > _MAX_EXPANSIONS:
        return None
    literals = _EXPRESSION.split(job.name)
    names = []
    for combo in itertools.product(*(values[k] for k in used)):
        chosen = dict(zip(used, combo, strict=True))
        pieces = [literals[0]]
        for key, literal in zip(keys, literals[1:], strict=True):
            pieces += [chosen[str(key)], literal]
        names.append("".join(pieces))
    return tuple(names)


def _literal_length(name: str) -> int:
    return len(_EXPRESSION.sub("", name))


def _needs_refs(body: dict[str, Any], needs: tuple[str, ...]) -> frozenset[str]:
    text = yaml.safe_dump(body)
    if "toJSON(needs)" in text:
        return frozenset(needs)
    return frozenset(_NEEDS_REF.findall(text)) & frozenset(needs)


def match_step_name(spec_name: str, measured_name: str) -> bool:
    """Whether a YAML step's display name is the measured step `measured_name` (SHAs already folded to <sha>)."""
    spec_name = SHA.sub("<sha>", spec_name)  # measured names are folded; a SHA-pinned `uses:` must be too
    if spec_name == measured_name:
        return True
    return _wildcard_match(_EXPRESSION.split(spec_name), measured_name)


def _wildcard_match(parts: list[str], text: str) -> bool:
    """Whether text is `parts` joined by one-or-more-character wildcards. Linear: no regex backtracking."""
    if len(parts) == 1:
        return text == parts[0]
    first, last = parts[0], parts[-1]
    if not (text.startswith(first) and text.endswith(last)):
        return False
    end = len(text) - len(last)
    pos = len(first)
    for literal in parts[1:-1]:
        found = text.find(literal, pos + 1, end)  # the wildcard before it eats at least one character
        if found == -1:
            return False
        pos = found + len(literal)
    return end - pos >= 1


def topological_order(workflow: Workflow) -> list[str]:
    """Job ids with every job after the jobs it needs. Raises ValueError on a cycle."""
    order: list[str] = []
    state: dict[str, str] = {}  # "visiting" or "done"

    def visit(job_id: str, path: tuple[str, ...]) -> None:
        if state.get(job_id) == "done":
            return
        if state.get(job_id) == "visiting":
            msg = f"needs cycle: {' -> '.join((*path, job_id))}"
            raise ValueError(msg)
        state[job_id] = "visiting"
        for dep in workflow.jobs[job_id].needs:
            visit(dep, (*path, job_id))
        state[job_id] = "done"
        order.append(job_id)

    for job_id in workflow.jobs:
        visit(job_id, ())
    return order


def _matches(job: JobSpec, display_name: str) -> bool:
    if display_name == job.name:
        return True
    if not _EXPRESSION.search(job.name):
        # GitHub appends the matrix values when the name does not use them
        return job.has_matrix and display_name.startswith(f"{job.name} (") and display_name.endswith(")")
    return _wildcard_match(_EXPRESSION.split(job.name), display_name)

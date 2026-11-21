"""Read a workflow file's job graph and map GitHub's job display names back to job ids.

The API reports jobs by display name ("test (ubuntu-latest, 3.12)"), while `needs:` refers
to job ids ("test"). Analysis needs both sides joined to build the dependency graph.
"""

import itertools
import re
from dataclasses import dataclass
from typing import Any

import yaml

SHA = re.compile(r"\b[0-9a-f]{40}\b")  # step names embed commit SHAs that differ per run; fold to "<sha>"
_MATRIX_EXPRESSION = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")
_MAX_EXPANSIONS = 1000
_NEEDS_REF = re.compile(r"needs\.([A-Za-z0-9_-]+)", re.IGNORECASE)  # GitHub expressions are case-insensitive
_READS_EVERY_NEED = re.compile(r"needs\.\*|needs\[|toJSON\s*\(\s*needs\s*\)", re.IGNORECASE)
_STATUS_FUNCTION = re.compile(r"\b(success|failure|always|cancelled)\s*\(", re.IGNORECASE)


def _tokenize(text: str) -> tuple[list[str], list[str]]:
    """Split text into its literal pieces and its `${{ ... }}` expressions: len(literals) == len(expressions) + 1.

    Linear in the text (names are repo-controlled, so a regex with `.*?` would be quadratic). An expression
    does not span a newline, and an unclosed `${{` is literal text.
    """
    literals: list[str] = []
    expressions: list[str] = []
    start = 0  # where the current literal piece began
    pos = text.find("${{")
    newline = -1  # next "\n" at or after pos (len(text) when there is none)
    close = -1  # first "}}" at or after the last position searched from; reused while still ahead of pos
    while pos != -1:
        if close < pos + 3:
            close = text.find("}}", pos + 3)
            if close == -1:
                break  # nothing closes any later `${{` either
        if newline < pos:  # the next newline at or after pos, found once and reused while still ahead
            newline = text.find("\n", pos)
            newline = len(text) if newline == -1 else newline
        if newline > close:
            literals.append(text[start:pos])
            expressions.append(text[pos : close + 2])
            start = close + 2
            pos = text.find("${{", start)
        else:  # no opener before the newline can reach a closer without crossing it
            pos = text.find("${{", newline + 1)
    literals.append(text[start:])
    return literals, expressions


def _has_expression(text: str) -> bool:
    return bool(_tokenize(text)[1])


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
    matrix_definition: str = ""  # canonical JSON of the whole strategy.matrix (include/exclude too), or its expression


@dataclass(frozen=True)
class Workflow:
    """A workflow file's name and jobs, keyed by job id."""

    name: str
    jobs: dict[str, JobSpec]

    def job_id_for(self, display_name: str) -> str | None:
        """The job id a display name from the API belongs to, or None if it matches no job."""
        jobs = list(self.jobs.values())
        wanted = _normalize(display_name)
        for job in jobs:  # a literal name, or the raw one GitHub shows for a skipped matrix job
            if display_name == job.name or (not _has_expression(job.name) and wanted == _normalize(job.name)):
                return job.id
        expanded = {job.id: _expand_name(job) for job in jobs}
        for job in jobs:  # `${{ matrix.key }}` names with known values are literal names in disguise
            if wanted in (expanded[job.id] or ()):
                return job.id
        # Names left over are patterns, most literal characters first. A name with no literal text that
        # could be expanded must never be a wildcard: it would swallow every unrelated job.
        patterns = sorted(
            (j for j in jobs if expanded[j.id] is None or _literal_length(j.name) > 0),
            key=lambda j: -_literal_length(j.name),
        )
        for job in patterns:
            if _matches(job, wanted):
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
            name=_text(body.get("name")) or str(job_id),
            needs=needs,
            has_matrix=bool((body.get("strategy") or {}).get("matrix")),
            uses=_text(body.get("uses")),
            steps=_parse_steps(body.get("steps")),
            runs_on=_runs_on(body.get("runs-on")),
            matrix_keys=_matrix_keys(body),
            needs_refs=_needs_refs(body, needs),
            matrix_values=_matrix_values(body),
            matrix_definition=_matrix_definition(body),
        )
    for job in jobs.values():
        unknown = [n for n in job.needs if n not in jobs]
        if unknown:
            msg = f"job {job.id!r} needs unknown job(s) {unknown}"
            raise ValueError(msg)
    return Workflow(name=str(doc.get("name") or ""), jobs=jobs)


_NON_SCALAR = "<non-scalar>"  # stands in for a `with:` value that is a list or mapping


def _text(value: object) -> str | None:
    """The value when it is a non-empty string, else None.

    GitHub requires these fields to be strings, and stringifying a YAML container would expand every alias.
    """
    return value if isinstance(value, str) and value else None


def _with_value(value: object) -> str:
    return str(value) if isinstance(value, str | int | float | bool) or value is None else _NON_SCALAR


def _step_name(item: dict[str, Any]) -> str:
    if name := _text(item.get("name")):
        return name
    if uses := _text(item.get("uses")):
        return f"Run {uses}"
    lines = (_text(item.get("run")) or "").strip().splitlines()
    return f"Run {lines[0].strip()}" if lines else "Run"


def _parse_steps(raw: object) -> tuple[StepSpec, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        StepSpec(
            name=_step_name(item),
            uses=_text(item.get("uses")),
            run=item["run"] if isinstance(item.get("run"), str) else None,
            with_={
                str(k): _with_value(v) for k, v in (item["with"] if isinstance(item.get("with"), dict) else {}).items()
            },
            if_=item["if"] if isinstance(item.get("if"), str) else None,
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
        if isinstance(raw, list):
            values[str(key)] = [str(v) for v in raw if _is_literal(v)]  # computed elements are left out
    includes = matrix.get("include")
    for entry in includes if isinstance(includes, list) else ():
        for key, value in entry.items() if isinstance(entry, dict) else ():
            if _is_literal(value) and (str(key) in values or key not in matrix):
                values.setdefault(str(key), []).append(str(value))
    return tuple((k, tuple(dict.fromkeys(v))) for k, v in values.items())


MATRIX_TOO_LARGE = "<matrix too large>"  # sentinel: coverage unknown, so dedupe never treats such a job as a copy
_MAX_MATRIX_BYTES = 64 * 1024
_MAX_BODY_BYTES = 1_000_000
_ENTRY_COST = 2  # rough serialized overhead per container entry and per key


def _expanded_size_within(obj: object, limit: int) -> bool:
    """Whether `obj`, with every shared reference expanded, serializes to at most about `limit` characters.

    YAML aliases to scalars are written out in full on every use, so counting distinct nodes is not enough.
    Shared nodes are visited each time they are referenced; the visit count is capped too, which bounds cycles.
    """
    total = 0
    visits = 0
    stack = [obj]
    while stack:
        node = stack.pop()
        visits += 1
        if visits > limit:
            return False
        if isinstance(node, dict):
            total += _ENTRY_COST * len(node)
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            total += _ENTRY_COST * len(node)
            stack.extend(node)
        else:
            total += len(str(node))
        if total > limit:
            return False
    return True


def _matrix_definition(body: dict[str, Any]) -> str:
    """The full `strategy.matrix` as a canonical string, so exclusions and inclusions tell matrices apart.

    Workflow YAML is hostile input: `safe_dump` emits anchors for shared nodes instead of expanding them,
    and an oversized or unserializable matrix becomes a sentinel rather than being walked further.
    """
    strategy = body.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if isinstance(matrix, str):
        return matrix  # an expression such as fromJSON(...): compared as written
    if not isinstance(matrix, dict):
        return ""
    if not _expanded_size_within(matrix, _MAX_MATRIX_BYTES):
        return MATRIX_TOO_LARGE
    try:
        text = yaml.safe_dump(matrix, sort_keys=True, default_flow_style=True, width=2**31)
    except (yaml.YAMLError, TypeError, ValueError, RecursionError):
        return MATRIX_TOO_LARGE
    return text if len(text) <= _MAX_MATRIX_BYTES else MATRIX_TOO_LARGE


def _is_literal(value: object) -> bool:
    return isinstance(value, str | int | float | bool) and "${{" not in str(value)


def _normalize(name: str) -> str:
    """GitHub trims a display name and collapses runs of whitespace (an empty matrix value leaves a gap)."""
    return " ".join(name.split())


def _expand_name(job: JobSpec) -> tuple[str, ...] | None:
    """Every concrete display name of a job named only by `${{ matrix.<key> }}` expressions, or None."""
    literals, expressions = _tokenize(job.name)
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
    names = []
    for combo in itertools.product(*(values[k] for k in used)):
        chosen = dict(zip(used, combo, strict=True))
        pieces = [literals[0]]
        for key, literal in zip(keys, literals[1:], strict=True):
            pieces += [chosen[str(key)], literal]
        names.append(_normalize("".join(pieces)))
    return tuple(names)


def _literal_length(name: str) -> int:
    return sum(len(piece) for piece in _tokenize(name)[0])


def _needs_refs(body: dict[str, Any], needs: tuple[str, ...]) -> frozenset[str]:
    """The needs a job reads. A gate (status-function `if:`, `needs.*`, `toJSON(needs)`) reads all of them."""
    if_ = body.get("if")
    if isinstance(if_, str) and _STATUS_FUNCTION.search(if_):
        return frozenset(needs)  # the job decides on its needs' results, so every edge carries a signal
    if not _expanded_size_within(body, _MAX_BODY_BYTES):
        return frozenset(needs)  # too large to scan: assume it reads every need, so no edge is ever cut
    text = yaml.safe_dump(body, width=2**31)  # no line wrapping, so a pattern is never split across lines
    if _READS_EVERY_NEED.search(text):
        return frozenset(needs)
    named = {ref.lower() for ref in _NEEDS_REF.findall(text)}
    return frozenset(n for n in needs if n.lower() in named)


def match_step_name(spec_name: str, measured_name: str) -> bool:
    """Whether a YAML step's display name is the measured step `measured_name` (SHAs already folded to <sha>)."""
    spec_name = SHA.sub("<sha>", spec_name)  # measured names are folded; a SHA-pinned `uses:` must be too
    if spec_name == measured_name:
        return True
    return _wildcard_match(_tokenize(spec_name)[0], measured_name)


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
    if not _has_expression(job.name):
        # GitHub appends the matrix values when the name does not use them
        return job.has_matrix and display_name.startswith(f"{job.name} (") and display_name.endswith(")")
    return _wildcard_match(_tokenize(job.name)[0], display_name)

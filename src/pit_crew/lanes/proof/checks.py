"""Work out which CI checks a candidate's change can affect.

When in doubt this answers "every check" (None): proving too much costs replay minutes,
proving too little lets a change hide a bug.

The parser reads only the block structure of a GitHub Actions workflow: top-level keys,
the jobs under `jobs:`, and each job's `name:` and `needs:`. Anything it does not
recognise, such as flow-style YAML, makes it give up and answer None.
"""

import re
from dataclasses import dataclass, field

from pit_crew.lanes.proof.miner import check_key

_WORKFLOWS = ".github/workflows/"
_KEY = re.compile(r"""^(\s*)["']?([\w.-]+)["']?:\s*(.*)$""")


@dataclass(frozen=True)
class Workflow:
    name: str
    header: tuple[str, ...] = ()  # every line outside `jobs:`
    jobs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)  # job id -> display name
    needs: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def keys(self, job_ids: set[str]) -> set[str]:
        """Check keys for these jobs, by id and by display name, so either spelling matches."""
        out = set()
        for job_id in job_ids & self.jobs.keys():
            out.add(check_key(self.name, job_id))
            out.add(check_key(self.name, self.names.get(job_id, job_id)))
        return out


def touched_checks(changes: list[dict]) -> set[str] | None:
    """Check keys a change can affect, or None for every check.

    Each change is {"path", "before", "after"}; before is None for an added file and after
    is None for a deleted one. A changed file outside .github/workflows/ (a script, a test
    config, a lockfile, a composite action) can affect any check, so it answers None. A
    removed job is reported as touched, so its old bugs are replayed and the removal fails.
    """
    touched: set[str] = set()
    for change in changes:
        path = change["path"]
        if not (path.startswith(_WORKFLOWS) and path.endswith((".yml", ".yaml"))):
            return None
        before = parse_workflow(change["before"], path) if change["before"] is not None else Workflow(path)
        after = parse_workflow(change["after"], path) if change["after"] is not None else Workflow(path)
        if before is None or after is None:
            return None
        if before.header != after.header:
            changed = before.jobs.keys() | after.jobs.keys()
        else:
            changed = {j for j in before.jobs.keys() | after.jobs.keys() if before.jobs.get(j) != after.jobs.get(j)}
        changed = _with_dependents(changed, before, after)
        touched |= before.keys(changed) | after.keys(changed)
    return touched


def all_checks(workflows: dict[str, str]) -> set[str] | None:
    """Every check key defined by these workflow files (path -> text), or None if one won't parse."""
    keys: set[str] = set()
    for path, text in workflows.items():
        workflow = parse_workflow(text, path)
        if workflow is None:
            return None
        keys |= workflow.keys(set(workflow.jobs))
    return keys


def parse_workflow(text: str, path: str) -> Workflow | None:
    lines = [line.rstrip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    start = next((i for i, line in enumerate(lines) if line.startswith("jobs:")), None)
    if start is None or _value(lines[start]):
        return None
    end = next((i for i in range(start + 1, len(lines)) if not lines[i][0].isspace()), len(lines))
    header = lines[:start] + lines[end:]
    body = lines[start + 1 : end]

    jobs: dict[str, list[str]] = {}
    indent = _indent(body[0]) if body else 0
    for line in body:
        if _indent(line) < indent:
            return None
        if _indent(line) == indent:
            match = _KEY.match(line)
            if not match or _value(line):
                return None
            current = jobs.setdefault(match.group(2), [])
        else:
            current.append(line)

    name = next((_unquote(_value(line)) for line in header if line.startswith("name:")), path)
    return Workflow(
        name=name,
        header=tuple(header),
        jobs={job_id: tuple(job) for job_id, job in jobs.items()},
        names={job_id: n for job_id, job in jobs.items() if (n := _field(job, "name"))},
        needs={job_id: _needs(job) for job_id, job in jobs.items()},
    )


def _field(job: list[str], key: str) -> str | None:
    child = _indent(job[0]) if job else 0
    for line in job:
        match = _KEY.match(line)
        if match and len(match.group(1)) == child and match.group(2) == key:
            return _unquote(match.group(3))
    return None


def _needs(job: list[str]) -> tuple[str, ...]:
    child = _indent(job[0]) if job else 0
    for i, line in enumerate(job):
        match = _KEY.match(line)
        if not (match and len(match.group(1)) == child and match.group(2) == "needs"):
            continue
        value = match.group(3)
        if value.startswith("["):
            return tuple(_unquote(v) for v in value.strip("[]").split(",") if v.strip())
        if value:
            return (_unquote(value),)
        items = []
        for item in job[i + 1 :]:
            if _indent(item) <= child or not item.lstrip().startswith("- "):
                break
            items.append(_unquote(item.lstrip()[2:]))
        return tuple(items)
    return ()


def _with_dependents(changed: set[str], *workflows: Workflow) -> set[str]:
    """A job that needs a changed job sees its outputs, artifacts and skip status change too."""
    out = set(changed)
    grew = True
    while grew:
        grew = False
        for workflow in workflows:
            for job_id, needs in workflow.needs.items():
                if job_id not in out and out & set(needs):
                    out.add(job_id)
                    grew = True
    return out


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _value(line: str) -> str:
    match = _KEY.match(line)
    value = match.group(3) if match else ""
    return "" if value.startswith("#") else value


def _unquote(value: str | None) -> str:
    value = (value or "").split(" #")[0].strip()
    return value.strip("'\"")

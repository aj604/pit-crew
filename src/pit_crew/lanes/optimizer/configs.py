"""Never-check-less guard for the runner and build config files the Optimizer may edit.

Workflows are checked in `validate.py`. These files can hide the same weakening one level down:
a package.json test script gaining `--passWithNoTests`, pytest `addopts` gaining `-k`, a Makefile
recipe gaining a leading `-`, Playwright retries going up, a coverage threshold going down, or a
test config turning off the failure recordings the Proof lane needs. Each changed file is compared
with its original text. A file that does not parse fails, because it cannot be checked.
"""

import configparser
import json
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pit_crew.lanes.optimizer.validate import WEAKENING_FLAGS, GuardReport, check_line, covered, run_lines

Checker = Callable[[str, str], list[str]]

_JS_TEST_CONFIG = re.compile(r"^(?:playwright|vitest|jest|cypress)\.config\.")
_RETRIES = re.compile(r"['\"]?\bretries\b['\"]?\s*:\s*(\{[^}]*\}|[^,\n}]+)")
_NUMBER = re.compile(r"\d+")
_BOOLEAN_NARROWING_KEYS = frozenset({"passWithNoTests"})
_TRUTHY = frozenset({"true", "1", "yes", "on"})
_BRACES = re.compile(r"\{([^{}]*)\}")
_MAKE_RULE = re.compile(r"^([^\s:=#][^:=#]*?)\s*::?(?!=)\s*(.*)$")
_MAKE_CALL = re.compile(r"(?:\$\(MAKE\)|\$\{MAKE\}|^make)\s+(?:-\S+\s+)*([A-Za-z0-9_.-]+)")
_MAKE_UNRESOLVED = re.compile(r"^\s*(?:-?include|sinclude)\s|\$\(eval\b", re.MULTILINE)
_RECORDING = re.compile(r"\b(video|screenshot|trace)\s*:\s*['\"]?([A-Za-z-]+)")
# Keys that, when added, make a JS test runner collect or fail on fewer tests.
_JS_NARROWING_KEYS = (
    "testPathIgnorePatterns",
    "modulePathIgnorePatterns",
    "testIgnore",
    "grepInvert",
    "passWithNoTests",
    "exclude",
    "grep",
)
_JS_COLLECTION_KEY = re.compile(
    r"^\s*['\"]?(?:testMatch|testRegex|testDir|specPattern|include|roots)['\"]?\s*:.*$", re.MULTILINE
)
_TS_STRICT_FLAGS = (
    "strict",
    "noImplicitAny",
    "strictNullChecks",
    "strictFunctionTypes",
    "strictBindCallApply",
    "strictPropertyInitialization",
    "noImplicitThis",
    "alwaysStrict",
    "noImplicitReturns",
    "noUncheckedIndexedAccess",
    "noUnusedLocals",
    "noUnusedParameters",
)
_PYTEST_COLLECTION_KEYS = ("python_files", "python_classes", "python_functions")
_RUST_RUNTIME_CHECKS = ("debug-assertions", "overflow-checks")


def check_config(path: str, before: str, after: str) -> GuardReport:
    """Compare a config file's original text (empty for a new file) with its changed text."""
    report = GuardReport()
    if not after.strip():
        if before.strip():
            report.problems.append(f"{path}: deletes the file; keep every check")
        return report
    checker = _checker(PurePosixPath(path).name)
    if checker is None:
        return report
    try:
        problems = checker(before, after)
    except (ValueError, configparser.Error) as exc:  # json and toml errors are ValueErrors
        problems = [f"does not parse ({exc}), so the change cannot be checked"]
    report.problems.extend(f"{path}: {problem}" for problem in problems)
    return report


def _checker(name: str) -> Checker | None:
    if name in _BY_NAME:
        return _BY_NAME[name]
    if _JS_TEST_CONFIG.match(name):
        return _js_test_config
    if name.startswith("tsconfig"):
        return _tsconfig
    return None


# --- shared comparisons ----------------------------------------------------------------------


def _flags_added(where: str, before: str, after: str) -> list[str]:
    """A problem for each failure-hiding or test-skipping flag that appears more often after."""
    return [
        f"{where} adds a flag that {label}"
        for label, pattern in WEAKENING_FLAGS
        if len(pattern.findall(after)) > len(pattern.findall(before))
    ]


def _commands_kept(where: str, before: Iterable[str], after: Iterable[str]) -> list[str]:
    """Each original command line must still run, exactly or with plain arguments added."""
    new = [check for line in after if (check := check_line(line))]
    return [
        f"{where} drops or rewrites `{check}`"
        for line in dict.fromkeys(before)
        if (check := check_line(line)) and not covered(check, new)
    ]


def _values_lost(where: str, before: Iterable[str], after: Iterable[str]) -> list[str]:
    lost = sorted(set(before) - set(after))
    return [f"{where} drops {lost}"] if lost else []


def _get(data: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = data.get(key)
        if not isinstance(value, dict):
            return {}
        data = value
    return dict(data)


def _words(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return str(value or "").split()


# --- package.json --------------------------------------------------------------------------


def _package_json(before: str, after: str) -> list[str]:
    old, new = _json_object(before), _json_object(after)
    old_scripts, new_scripts = _get(old, "scripts"), _get(new, "scripts")
    problems = [f'drops the script "{name}"' for name in old_scripts if name not in new_scripts]
    for name, command in old_scripts.items():
        if name in new_scripts:
            where = f'script "{name}"'
            problems += _commands_kept(where, [str(command)], [str(new_scripts[name])])
            problems += _flags_added(where, str(command), str(new_scripts[name]))
    problems += _js_narrowing("jest config", json.dumps(old.get("jest", {})), json.dumps(new.get("jest", {})))
    return problems


def _json_object(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        msg = "expected a JSON object"
        raise ValueError(msg)  # noqa: TRY004  reported as a parse failure, like json's own errors
    return data


# --- Python: pyproject.toml, pytest.ini, tox.ini, noxfile.py -------------------------------


def _pyproject(before: str, after: str) -> list[str]:
    old, new = tomllib.loads(before), tomllib.loads(after)
    problems = _pytest_options(
        "pytest", _get(old, "tool", "pytest", "ini_options"), _get(new, "tool", "pytest", "ini_options")
    )
    old_floor = _get(old, "tool", "coverage", "report").get("fail_under")
    new_floor = _get(new, "tool", "coverage", "report").get("fail_under")
    if isinstance(old_floor, int | float) and not (isinstance(new_floor, int | float) and new_floor >= old_floor):
        problems.append(f"lowers the coverage threshold from {old_floor}")
    return problems


def _pytest_options(where: str, old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    old_opts, new_opts = " ".join(_words(old.get("addopts"))), " ".join(_words(new.get("addopts")))
    problems = _flags_added(f"{where} addopts", old_opts, new_opts)
    problems += _discovery(where, _words(old.get("testpaths")), _words(new.get("testpaths")))
    added = set(_words(new.get("norecursedirs"))) - set(_words(old.get("norecursedirs")))
    if added:
        problems.append(f"{where} stops collecting {sorted(added)}")
    problems += [
        f"{where} changes `{key}`, which decides which tests are collected"
        for key in _PYTEST_COLLECTION_KEYS
        if _words(old.get(key)) != _words(new.get(key))
    ]
    return problems


def _discovery(where: str, before: list[str], after: list[str]) -> list[str]:
    """Without `testpaths`, pytest discovers from where it runs: setting it can only narrow that."""
    if not after:
        return []
    if not before:
        return [f"{where} sets `testpaths = {after}`, so tests outside them are no longer discovered"]
    return _values_lost(f"{where} testpaths", before, after)


def _ini(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(text)
    return parser


def _ini_section(parser: configparser.ConfigParser, *names: str) -> dict[str, str]:
    for name in names:
        if parser.has_section(name):
            return dict(parser.items(name))
    return {}


def _pytest_ini(before: str, after: str) -> list[str]:
    old, new = _ini(before), _ini(after)
    return _pytest_options("pytest", _ini_section(old, "pytest"), _ini_section(new, "pytest"))


def _tox_ini(before: str, after: str) -> list[str]:
    old, new = _ini(before), _ini(after)
    problems = _pytest_options(
        "pytest", _ini_section(old, "pytest", "tool:pytest"), _ini_section(new, "pytest", "tool:pytest")
    )
    problems += _values_lost("tox's default envlist", _envlist(old), _envlist(new))
    for section in old.sections():
        if not section.startswith("testenv"):
            continue
        before_commands = run_lines(old.get(section, "commands", fallback=""))
        after_commands = run_lines(new.get(section, "commands", fallback="")) if new.has_section(section) else []
        problems += _commands_kept(f"[{section}] commands", before_commands, after_commands)
        problems += _flags_added(f"[{section}] commands", "\n".join(before_commands), "\n".join(after_commands))
    envs = {
        "testenv",
        *(s for s in old.sections() if s.startswith("testenv:")),
        *(f"testenv:{e}" for e in _envlist(old)),
    }
    problems += [
        f"[{section}] sets `ignore_outcome`, so its failures no longer fail tox"
        for section in sorted(envs)
        if _ignores_outcome(new, section) and not _ignores_outcome(old, section)
    ]
    return problems


def _envlist(parser: configparser.ConfigParser) -> list[str]:
    """The environments a bare `tox` runs, with factors like `py{311,312}-lint` expanded."""
    raw: str = parser.get("tox", "envlist", fallback="") or parser.get("tox", "env_list", fallback="")
    return [env for token in _split_top_level(raw) for env in _expand_braces(token)]


def _split_top_level(text: str) -> list[str]:
    tokens, depth, current = [], 0, ""
    for char in text:
        depth += (char == "{") - (char == "}")
        if depth == 0 and (char == "," or char.isspace()):
            tokens.append(current)
            current = ""
        else:
            current += char
    return [t for t in (*tokens, current) if t]


def _expand_braces(token: str) -> list[str]:
    match = _BRACES.search(token)
    if match is None:
        return [token]
    head, tail = token[: match.start()], token[match.end() :]
    return [env for part in match.group(1).split(",") for env in _expand_braces(head + part.strip() + tail)]


def _ignores_outcome(parser: configparser.ConfigParser, section: str) -> bool:
    """`ignore_outcome` for an environment, inherited from [testenv] when the section does not set it."""
    base = parser.get("testenv", "ignore_outcome", fallback="false")
    value = parser.get(section, "ignore_outcome", fallback=base) if parser.has_section(section) else base
    return value.strip().lower() in _TRUTHY


def _noxfile(before: str, after: str) -> list[str]:
    problems = _flags_added("noxfile", before, after)
    if after.count("session.run(") < before.count("session.run("):
        problems.append("noxfile runs fewer `session.run(...)` commands")
    return problems


# --- Makefile ------------------------------------------------------------------------------


@dataclass
class _Rule:
    prerequisites: list[str] = field(default_factory=list)
    recipe: list[str] = field(default_factory=list)


def _makefile(before: str, after: str) -> list[str]:
    """Each target must still run its checks, itself or through prerequisites and `$(MAKE) x` calls."""
    old, new = _rules(before), _rules(after)
    problems: list[str] = []
    for target in old:
        if not target.startswith("."):
            problems += _commands_kept(f"`make {target}`", _reachable(old, target), _reachable(new, target))
    old_lines = [line for rule in old.values() for line in rule.recipe]
    new_lines = [line for rule in new.values() for line in rule.recipe]
    problems += _flags_added("recipe", "\n".join(old_lines), "\n".join(new_lines))
    if sum(line.startswith("-") for line in new_lines) > sum(line.startswith("-") for line in old_lines):
        problems.append("adds a recipe line starting with `-`, which ignores its failure")
    problems += _make_specials(old, new)
    if len(_MAKE_UNRESOLVED.findall(after)) > len(_MAKE_UNRESOLVED.findall(before)):
        problems.append("adds `include` or `$(eval ...)`, which the guard cannot follow")
    return list(dict.fromkeys(problems))


def _rules(text: str) -> dict[str, _Rule]:
    rules: dict[str, _Rule] = {}
    current: list[_Rule] = []
    for line in text.splitlines():
        if line.startswith("\t"):
            command = " ".join(line.strip().lstrip("@").split())  # `@` only silences echoing
            for rule in current:
                rule.recipe.append(command)
            continue
        match = _MAKE_RULE.match(line)
        if match is None:
            if line.strip() and not line.startswith(" "):
                current = []  # a variable assignment or directive ends the previous rule
            continue
        prerequisites = [] if "=" in match.group(2) else match.group(2).split(";", 1)[0].split()
        current = [rules.setdefault(target, _Rule()) for target in match.group(1).split()]
        for rule in current:
            rule.prerequisites += prerequisites
    return rules


def _reachable(rules: Mapping[str, _Rule], target: str) -> list[str]:
    """Recipe lines `make target` runs: its own, its prerequisites', and targets it calls with $(MAKE)."""
    seen: set[str] = set()
    lines: list[str] = []
    stack = [target]
    while stack:
        name = stack.pop()
        if name in seen or name not in rules:
            continue
        seen.add(name)
        lines += rules[name].recipe
        stack += rules[name].prerequisites
        stack += [call.group(1) for line in rules[name].recipe if (call := _MAKE_CALL.search(line))]
    return lines


def _make_specials(old: Mapping[str, _Rule], new: Mapping[str, _Rule]) -> list[str]:
    problems = []
    if ".IGNORE" in new:
        before = set(old[".IGNORE"].prerequisites) if ".IGNORE" in old else None
        after = set(new[".IGNORE"].prerequisites)
        if before is None or (before and (not after or after - before)):
            problems.append("adds `.IGNORE`, which makes recipe failures nonfatal")
    if ".ONESHELL" in new and ".ONESHELL" not in old:
        problems.append("adds `.ONESHELL`, so only the last line of a recipe decides whether it fails")
    return problems


# --- Cargo.toml ----------------------------------------------------------------------------


def _cargo_toml(before: str, after: str) -> list[str]:
    old, new = tomllib.loads(before), tomllib.loads(after)
    problems = _values_lost("[[test]] targets", _target_names(old, "test"), _target_names(new, "test"))
    for profile in ("dev", "test"):
        old_profile, new_profile = _get(old, "profile", profile), _get(new, "profile", profile)
        problems += [
            f"[profile.{profile}] turns off `{key}`, which tests rely on to catch bugs"
            for key in _RUST_RUNTIME_CHECKS
            if new_profile.get(key) is False and old_profile.get(key) is not False
        ]
    return problems


def _target_names(data: Mapping[str, Any], kind: str) -> list[str]:
    targets = data.get(kind)
    return [str(t.get("name")) for t in targets if isinstance(t, dict)] if isinstance(targets, list) else []


# --- JavaScript and TypeScript test configs ------------------------------------------------


def _js_test_config(before: str, after: str) -> list[str]:
    problems = _js_narrowing("test config", before, after)
    problems += _retries_raised(before, after)
    if sorted(_JS_COLLECTION_KEY.findall(before)) != sorted(_JS_COLLECTION_KEY.findall(after)):
        problems.append("changes which test files are collected")
    if _recordings_on(after) < _recordings_on(before):
        problems.append("turns off failure videos, screenshots or traces; the Proof lane needs them")
    return problems


def _js_narrowing(where: str, before: str, after: str) -> list[str]:
    """Filter keys that are added, or whose values change: any new value may select fewer tests."""
    problems = []
    for key in _JS_NARROWING_KEYS:
        old, new = _values_of(before, key), _values_of(after, key)
        if key in _BOOLEAN_NARROWING_KEYS:
            changed = new.count("true") > old.count("true")
        else:  # compare array entries one by one, so removing an ignore pattern is fine
            changed = bool(_atoms(new) - _atoms(old))
        if changed:
            verb = "adds" if len(new) > len(old) else "changes"
            problems.append(f"{where} {verb} `{key}`, which can run or fail on fewer tests")
    return problems + _flags_added(where, before, after)


def _values_of(text: str, key: str) -> list[str]:
    """Each value assigned to ``key`` (arrays and objects whole), whitespace-normalized."""
    pattern = rf"['\"]?\b{key}\b['\"]?\s*:\s*(\[[^\]]*\]|\{{[^}}]*\}}|[^,\n}}]+)"
    return [" ".join(value.split()).rstrip(",") for value in re.findall(pattern, text)]


def _atoms(values: Iterable[str]) -> set[str]:
    """Array values split into entries; other values whole."""
    atoms: set[str] = set()
    for value in values:
        if value.startswith("[") and value.endswith("]"):
            atoms.update(entry.strip().strip("'\"") for entry in value[1:-1].split(",") if entry.strip())
        else:
            atoms.add(value)
    return atoms


def _retries_raised(before: str, after: str) -> list[str]:
    """Retry settings may stay, go away, or come down number for number; anything else is a raise.

    Cypress sets `runMode` and `openMode` separately, so each number is compared in place rather
    than taking a maximum; a changed expression that cannot be compared counts as a raise.
    """
    old = [" ".join(v.split()) for v in _RETRIES.findall(before)]
    new = [" ".join(v.split()) for v in _RETRIES.findall(after)]
    if not new or new == old:
        return []
    if len(new) == len(old) and all(_not_higher(o, n) for o, n in zip(old, new, strict=True)):
        return []
    return ["raises or changes test retries; a test that needs a retry has a bug"]


def _not_higher(before: str, after: str) -> bool:
    same_shape = _NUMBER.sub("#", before) == _NUMBER.sub("#", after)
    pairs = zip(_NUMBER.findall(before), _NUMBER.findall(after), strict=False)
    return same_shape and all(int(n) <= int(o) for o, n in pairs)


def _recordings_on(text: str) -> int:
    return sum(1 for _, mode in _RECORDING.findall(text) if mode.lower() != "off")


def _tsconfig(before: str, after: str) -> list[str]:
    problems = [
        f"turns off the `{flag}` type check"
        for flag in _TS_STRICT_FLAGS
        if _ts_flag(after, flag, value=True) < _ts_flag(before, flag, value=True)
        or _ts_flag(after, flag, value=False) > _ts_flag(before, flag, value=False)
    ]
    if _ts_flag(after, "skipLibCheck", value=True) > _ts_flag(before, "skipLibCheck", value=True):
        problems.append("adds `skipLibCheck`, which skips type-checking declaration files")
    return problems


def _ts_flag(text: str, flag: str, *, value: bool) -> int:
    return len(re.findall(rf"['\"]{flag}['\"]\s*:\s*{str(value).lower()}\b", text))


_BY_NAME: dict[str, Checker] = {
    "package.json": _package_json,
    "pyproject.toml": _pyproject,
    "pytest.ini": _pytest_ini,
    "tox.ini": _tox_ini,
    "noxfile.py": _noxfile,
    "Makefile": _makefile,
    "Cargo.toml": _cargo_toml,
}

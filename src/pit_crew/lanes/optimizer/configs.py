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
from pathlib import PurePosixPath
from typing import Any

from pit_crew.lanes.optimizer.validate import WEAKENING_FLAGS, GuardReport, check_line, covered, run_lines

Checker = Callable[[str, str], list[str]]

_JS_TEST_CONFIG = re.compile(r"^(?:playwright|vitest|jest|cypress)\.config\.")
_RETRIES = re.compile(r"retries\b[^\n]{0,60}")
_NUMBER = re.compile(r"\d+")
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
    problems += _js_narrowing("jest config", json.dumps(old.get("jest", "")), json.dumps(new.get("jest", "")))
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
    problems += _values_lost(f"{where} testpaths", _words(old.get("testpaths")), _words(new.get("testpaths")))
    added = set(_words(new.get("norecursedirs"))) - set(_words(old.get("norecursedirs")))
    if added:
        problems.append(f"{where} stops collecting {sorted(added)}")
    problems += [
        f"{where} changes `{key}`, which decides which tests are collected"
        for key in _PYTEST_COLLECTION_KEYS
        if _words(old.get(key)) != _words(new.get(key))
    ]
    return problems


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
    for section in old.sections():
        if not section.startswith("testenv"):
            continue
        before_commands = run_lines(old.get(section, "commands", fallback=""))
        after_commands = run_lines(new.get(section, "commands", fallback="")) if new.has_section(section) else []
        problems += _commands_kept(f"[{section}] commands", before_commands, after_commands)
        problems += _flags_added(f"[{section}] commands", "\n".join(before_commands), "\n".join(after_commands))
    return problems


def _noxfile(before: str, after: str) -> list[str]:
    problems = _flags_added("noxfile", before, after)
    if after.count("session.run(") < before.count("session.run("):
        problems.append("noxfile runs fewer `session.run(...)` commands")
    return problems


# --- Makefile ------------------------------------------------------------------------------


def _makefile(before: str, after: str) -> list[str]:
    old, new = _recipes(before), _recipes(after)
    problems = _commands_kept("recipe", old, new)
    problems += _flags_added("recipe", "\n".join(old), "\n".join(new))
    if sum(line.startswith("-") for line in new) > sum(line.startswith("-") for line in old):
        problems.append("adds a recipe line starting with `-`, which ignores its failure")
    return problems


def _recipes(text: str) -> list[str]:
    """Recipe command lines with make's `@` (silent) prefix removed; a `-` prefix is kept."""
    return [" ".join(line.strip().lstrip("@").split()) for line in text.splitlines() if line.startswith("\t")]


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
    if _max_retries(after) > _max_retries(before):
        problems.append("raises test retries; a test that needs a retry has a bug")
    if sorted(_JS_COLLECTION_KEY.findall(before)) != sorted(_JS_COLLECTION_KEY.findall(after)):
        problems.append("changes which test files are collected")
    if _recordings_on(after) < _recordings_on(before):
        problems.append("turns off failure videos, screenshots or traces; the Proof lane needs them")
    return problems


def _js_narrowing(where: str, before: str, after: str) -> list[str]:
    problems = [
        f"{where} adds `{key}`, which runs or fails on fewer tests"
        for key in _JS_NARROWING_KEYS
        if _key_count(after, key) > _key_count(before, key)
    ]
    return problems + _flags_added(where, before, after)


def _key_count(text: str, key: str) -> int:
    return len(re.findall(rf"['\"]?\b{key}\b['\"]?\s*:", text))


def _max_retries(text: str) -> int:
    return max((int(n) for m in _RETRIES.findall(text) for n in _NUMBER.findall(m)), default=0)


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

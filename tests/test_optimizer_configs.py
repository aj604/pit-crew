import json

import pytest

from pit_crew import tools
from pit_crew.contracts import Candidate, Hypothesis
from pit_crew.lanes.optimizer.configs import check_config
from pit_crew.ledger import InMemoryLedger
from pit_crew.managed_agent import build_request


def _problems(path: str, before: str, after: str) -> list[str]:
    return check_config(path, before, after).problems


def _package(scripts: dict[str, str], **extra: object) -> str:
    return json.dumps({"name": "app", "scripts": scripts, **extra})


PYPROJECT = """\
[tool.pytest.ini_options]
addopts = "-q --strict-markers"
testpaths = ["tests", "integration"]
python_files = ["test_*.py"]

[tool.coverage.report]
fail_under = 90
"""

PLAYWRIGHT = """\
import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './e2e',
  retries: process.env.CI ? 1 : 0,
  use: {
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
});
"""

TSCONFIG = '{\n  "compilerOptions": {\n    "strict": true,\n    "noEmit": true\n  }\n}\n'


# --- changes that keep every check -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        ("package.json", _package({"test": "jest"}), _package({"test": "jest --ci --maxWorkers=50%"})),
        ("package.json", "", _package({"test": "jest"})),
        ("pyproject.toml", PYPROJECT, PYPROJECT.replace('"-q --strict-markers"', '"-q --strict-markers -n auto"')),
        ("pyproject.toml", PYPROJECT, PYPROJECT.replace("fail_under = 90", "fail_under = 95")),
        ("pytest.ini", "[pytest]\naddopts = -q\n", "[pytest]\naddopts = -q -n auto\n"),
        ("Makefile", "test:\n\t@pytest -q\n", "test:\n\tpytest -q -n auto\n"),
        (
            "playwright.config.ts",
            PLAYWRIGHT,
            PLAYWRIGHT.replace("testDir: './e2e',", "testDir: './e2e',\n  workers: 4,"),
        ),
        ("tsconfig.json", TSCONFIG, TSCONFIG.replace('"noEmit": true', '"noEmit": true,\n    "incremental": true')),
        ("Cargo.toml", '[package]\nname = "x"\n', '[package]\nname = "x"\n\n[profile.dev]\ndebug = 0\n'),
        ("noxfile.py", 'session.run("pytest")\n', 'session.run("pytest", "-n", "auto")\n'),
        ("README.md", "a", "b"),  # not a config the guard knows; the path check decides
    ],
)
def test_safe_config_changes_pass(path: str, before: str, after: str) -> None:
    assert _problems(path, before, after) == []


# --- changes that check less -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "before", "after", "expected"),
    [
        (
            "package.json",
            _package({"test": "jest", "lint": "eslint ."}),
            _package({"test": "jest"}),
            'drops the script "lint"',
        ),
        (
            "package.json",
            _package({"test": "jest"}),
            _package({"test": "jest --passWithNoTests"}),
            "passes with no tests",
        ),
        ("package.json", _package({"test": "jest"}), _package({"test": "vitest run"}), "drops or rewrites `jest`"),
        ("package.json", _package({"test": "jest && tsc"}), _package({"test": "jest || tsc"}), "drops or rewrites"),
        (
            "package.json",
            _package({"test": "jest"}),
            _package({"test": "jest"}, jest={"testPathIgnorePatterns": ["/slow/"]}),
            "adds `testPathIgnorePatterns`",
        ),
        (
            "pyproject.toml",
            PYPROJECT,
            PYPROJECT.replace("--strict-markers", "--strict-markers -k 'not slow'"),
            "filters tests by keyword",
        ),
        ("pyproject.toml", PYPROJECT, PYPROJECT.replace(', "integration"', ""), "testpaths drops ['integration']"),
        ("pyproject.toml", PYPROJECT, PYPROJECT.replace('"test_*.py"', '"test_fast_*.py"'), "changes `python_files`"),
        (
            "pyproject.toml",
            PYPROJECT,
            PYPROJECT.replace("fail_under = 90", "fail_under = 50"),
            "lowers the coverage threshold",
        ),
        ("pyproject.toml", PYPROJECT, PYPROJECT.replace("fail_under = 90\n", ""), "lowers the coverage threshold"),
        (
            "pyproject.toml",
            PYPROJECT,
            PYPROJECT.replace("python_files", 'norecursedirs = ["integration"]\npython_files'),
            "stops collecting ['integration']",
        ),
        ("pytest.ini", "[pytest]\naddopts = -q\n", "[pytest]\naddopts = -q --deselect tests/test_a.py\n", "deselects"),
        (
            "tox.ini",
            "[testenv]\ncommands =\n    pytest\n",
            "[testenv]\ncommands =\n    pytest --reruns 3\n",
            "retries failed tests",
        ),
        (
            "tox.ini",
            "[testenv]\ncommands =\n    pytest\n    mypy src\n",
            "[testenv]\ncommands =\n    pytest\n",
            "drops or rewrites `mypy src`",
        ),
        (
            "tox.ini",
            "[testenv:lint]\ncommands = ruff check\n",
            "[tox]\nenvlist = py\n",
            "drops or rewrites `ruff check`",
        ),
        (
            "tox.ini",
            "[tool:pytest]\ntestpaths = tests e2e\n",
            "[tool:pytest]\ntestpaths = tests\n",
            "testpaths drops ['e2e']",
        ),
        ("noxfile.py", 'session.run("pytest")\nsession.run("mypy")\n', 'session.run("pytest")\n', "fewer `session.run"),
        ("Makefile", "test:\n\tpytest\n", "test:\n\t-pytest\n", "ignores its failure"),
        ("Makefile", "check:\n\truff check\n\tpytest\n", "check:\n\tpytest\n", "drops or rewrites `ruff check`"),
        ("Cargo.toml", '[[test]]\nname = "e2e"\n', '[package]\nname = "x"\n', "[[test]] targets drops ['e2e']"),
        ("Cargo.toml", "", "[profile.test]\noverflow-checks = false\n", "turns off `overflow-checks`"),
        ("playwright.config.ts", PLAYWRIGHT, PLAYWRIGHT.replace("? 1 : 0", "? 3 : 0"), "test retries"),
        (
            "playwright.config.ts",
            PLAYWRIGHT,
            PLAYWRIGHT.replace("'retain-on-failure'", "'off'"),
            "the Proof lane needs them",
        ),
        (
            "playwright.config.ts",
            PLAYWRIGHT,
            PLAYWRIGHT.replace("    video: 'retain-on-failure',\n", ""),
            "the Proof lane needs",
        ),
        (
            "playwright.config.ts",
            PLAYWRIGHT,
            PLAYWRIGHT.replace("testDir: './e2e',", "testDir: './e2e',\n  testIgnore: '**/slow/**',"),
            "adds `testIgnore`",
        ),
        (
            "playwright.config.ts",
            PLAYWRIGHT,
            PLAYWRIGHT.replace("'./e2e'", "'./e2e/smoke'"),
            "changes which test files",
        ),
        (
            "vitest.config.ts",
            "export default { test: {} }\n",
            "export default { test: { exclude: ['slow/**'] } }\n",
            "adds `exclude`",
        ),
        (
            "tsconfig.json",
            TSCONFIG,
            TSCONFIG.replace('"strict": true', '"strict": false'),
            "turns off the `strict` type check",
        ),
        (
            "tsconfig.base.json",
            TSCONFIG,
            TSCONFIG.replace('"noEmit": true', '"noEmit": true,\n    "skipLibCheck": true'),
            "skipLibCheck",
        ),
    ],
)
def test_config_changes_that_check_less_are_rejected(path: str, before: str, after: str, expected: str) -> None:
    problems = _problems(path, before, after)
    assert any(expected in p for p in problems), problems
    assert all(p.startswith(f"{path}: ") for p in problems)


@pytest.mark.parametrize(
    ("path", "after"),
    [
        ("package.json", "{not json"),
        ("package.json", "[1, 2]"),
        ("pyproject.toml", "[tool.pytest\n"),
        ("tox.ini", "no section header\n"),
    ],
)
def test_unparseable_configs_are_rejected(path: str, after: str) -> None:
    assert "does not parse" in _problems(path, "", after)[0]


def test_deleting_a_config_is_rejected() -> None:
    assert _problems("package.json", _package({"test": "jest"}), "") == [
        "package.json: deletes the file; keep every check"
    ]
    assert _problems("package.json", "", "") == []


def test_report_candidate_runs_the_config_guard() -> None:
    build_request("optimizer", "")
    ledger = InMemoryLedger()
    hypothesis = Hypothesis("o/r", "jest is slow", "shard-by-timing", 60.0)
    ledger.add(hypothesis)
    before, after = _package({"test": "jest"}), _package({"test": "jest --passWithNoTests"})
    with pytest.raises(ValueError, match="passes with no tests"):
        tools.call(
            "report_candidate",
            ledger,
            {
                "hypothesis_id": hypothesis.id,
                "branch": "pc/x",
                "summary": "s",
                "files": {"package.json": after},
                "original_files": {"package.json": before},
            },
        )
    assert ledger.all(Candidate) == []
    result = tools.call("check_workflow_change", ledger, {"path": "package.json", "before": before, "after": after})
    assert result["ok"] is False


# --- Devin review on #88/#116 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "before", "after", "expected"),
    [
        # a positional selector narrows the run, like any unknown argument
        ("package.json", _package({"test": "jest"}), _package({"test": "jest tests/unit"}), "drops or rewrites `jest`"),
        # Makefile: dropping a prerequisite, moving a check to an uncalled target, .IGNORE, .ONESHELL
        (
            "Makefile",
            "test: integration\n\tpytest\nintegration:\n\tpytest tests/it\n",
            "test:\n\tpytest\nintegration:\n\tpytest tests/it\n",
            "`make test` drops or rewrites `pytest tests/it`",
        ),
        (
            "Makefile",
            "check:\n\tpytest\n\truff check .\nlint:\n\tmypy\n",
            "check:\n\tpytest\nlint:\n\tmypy\n\truff check .\n",
            "`make check` drops or rewrites `ruff check .`",
        ),
        ("Makefile", "test:\n\tpytest\n", "test:\n\tpytest\n.IGNORE: test\n", "adds `.IGNORE`"),
        ("Makefile", "test:\n\tpytest\n\tmypy\n", ".ONESHELL:\ntest:\n\tpytest\n\tmypy\n", "adds `.ONESHELL`"),
        ("Makefile", "test:\n\tpytest\n", "include extra.mk\ntest:\n\tpytest\n", "cannot follow"),
        # tox: ignore_outcome (base and named), default envlist (with factors)
        (
            "tox.ini",
            "[testenv]\ncommands = pytest\n",
            "[testenv]\ncommands = pytest\nignore_outcome = true\n",
            "[testenv] sets `ignore_outcome`",
        ),
        (
            "tox.ini",
            "[tox]\nenvlist = py,lint\n[testenv:lint]\ncommands = ruff check\n",
            "[tox]\nenvlist = py,lint\n[testenv:lint]\ncommands = ruff check\nignore_outcome = True\n",
            "[testenv:lint] sets `ignore_outcome`",
        ),
        (
            "tox.ini",
            "[tox]\nenvlist = py,lint\n[testenv:lint]\ncommands = ruff check\n",
            "[tox]\nenvlist = py\n[testenv:lint]\ncommands = ruff check\n",
            "default envlist drops ['lint']",
        ),
        (
            "tox.ini",
            "[tox]\nenv_list = py{311,312}-django\n",
            "[tox]\nenv_list = py312-django\n",
            "drops ['py311-django']",
        ),
        # pytest: explicit testpaths where discovery used to start at the root
        (
            "pyproject.toml",
            '[tool.pytest.ini_options]\naddopts = "-q"\n',
            '[tool.pytest.ini_options]\naddopts = "-q"\ntestpaths = ["tests/unit"]\n',
            "sets `testpaths",
        ),
        # JS: widening an existing filter's value
        (
            "playwright.config.ts",
            "export default { testIgnore: ['**/exp/**'] }\n",
            "export default { testIgnore: ['**/exp/**', '**/integration/**'] }\n",
            "changes `testIgnore`",
        ),
        (
            "package.json",
            _package({"test": "jest"}, jest={"passWithNoTests": False}),
            _package({"test": "jest"}, jest={"passWithNoTests": True}),
            "changes `passWithNoTests`",
        ),
        # Cypress: per-mode retries, inline and multi-line
        (
            "cypress.config.ts",
            "export default { retries: { runMode: 0, openMode: 2 } }\n",
            "export default { retries: { runMode: 1, openMode: 2 } }\n",
            "test retries",
        ),
        (
            "cypress.config.ts",
            "export default {\n  retries: {\n    runMode: 0,\n    openMode: 2,\n  },\n}\n",
            "export default {\n  retries: {\n    runMode: 2,\n    openMode: 2,\n  },\n}\n",
            "test retries",
        ),
        ("cypress.config.ts", "export default {}\n", "export default { retries: { runMode: 2 } }\n", "test retries"),
    ],
)
def test_review_findings_are_rejected(path: str, before: str, after: str, expected: str) -> None:
    problems = _problems(path, before, after)
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "Makefile",
            "check:\n\tpytest\n\truff check .\nlint:\n\tmypy\n",
            "check: lint\n\tpytest\nlint:\n\tmypy\n\truff check .\n",
        ),
        ("Makefile", "check:\n\tpytest\n\truff check .\n", "check:\n\tpytest\n\t$(MAKE) lint\nlint:\n\truff check .\n"),
        ("Makefile", "CC = gcc\ntest:\n\t@pytest\n", "CC := gcc\ntest:\n\tpytest -n auto\n"),
        (
            "pyproject.toml",
            '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            '[tool.pytest.ini_options]\naddopts = "-q"\n',
        ),
        ("tox.ini", "[tox]\nenvlist = py\n", "[tox]\nenvlist = py,lint\n"),
        (
            "playwright.config.ts",
            "export default { testIgnore: ['**/exp/**', '**/old/**'] }\n",
            "export default { testIgnore: ['**/exp/**'] }\n",
        ),
        (
            "cypress.config.ts",
            "export default { retries: { runMode: 2, openMode: 2 } }\n",
            "export default { retries: { runMode: 1, openMode: 2 } }\n",
        ),
        ("cypress.config.ts", "export default { retries: 2 }\n", "export default {}\n"),
    ],
)
def test_review_safe_counterparts_pass(path: str, before: str, after: str) -> None:
    assert _problems(path, before, after) == []


@pytest.mark.parametrize(
    ("path", "before", "after", "expected"),
    [
        (
            "pytest.ini",
            "[pytest]\ntestpaths = tests tests/smoke.py\n",
            "[pytest]\naddopts = -q\n",
            "default discovery would skip: ['tests/smoke.py']",
        ),
        (
            "vitest.config.ts",
            "export default { test: {\n  globals: true\n} }\n",
            "export default { test: {\n  globals: true,\n  passWithNoTests: true // empty on some jobs\n} }\n",
            "`passWithNoTests`",
        ),
        (
            "vitest.config.ts",
            "export default { test: { passWithNoTests: false /* strict */ } }\n",
            "export default { test: { passWithNoTests: true /* relaxed */ } }\n",
            "`passWithNoTests`",
        ),
        (
            "Makefile",
            "check:\n# Run tests\n\tpytest\n\truff check .\n",
            "check:\n# Run tests\n\truff check .\n",
            "drops or rewrites `pytest`",
        ),
        (
            "Makefile",
            "check: lint # see CONTRIBUTING=docs\n\tpytest\nlint:\n\truff check .\n",
            "check: # see CONTRIBUTING=docs\n\tpytest\nlint:\n\truff check .\n",
            "drops or rewrites `ruff check .`",
        ),
    ],
)
def test_second_review_findings_are_rejected(path: str, before: str, after: str, expected: str) -> None:
    problems = _problems(path, before, after)
    assert any(expected in p for p in problems), problems


def test_dropping_testpaths_of_default_named_files_is_fine() -> None:
    assert (
        _problems("pytest.ini", "[pytest]\ntestpaths = tests tests/test_smoke.py\n", "[pytest]\naddopts = -q\n") == []
    )
    assert _problems("Makefile", "test:\n\t# comment\n\tpytest\n", "test:\n\tpytest\n") == []

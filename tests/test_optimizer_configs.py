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
        ("playwright.config.ts", PLAYWRIGHT, PLAYWRIGHT.replace("? 1 : 0", "? 3 : 0"), "raises test retries"),
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

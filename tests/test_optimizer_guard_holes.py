"""Guard holes the optimizer-guard eval found (src/pit_crew/evals/optimizer_guard.py), now closed."""

import pytest

from pit_crew.lanes.optimizer.validate import check_change

CI = ".github/workflows/ci.yml"


def _problems(after: str, before: str) -> list[str]:
    return check_change(CI, before, after).problems


@pytest.mark.parametrize(
    ("after", "ok"),
    [
        ("pytest -q --shard=1/2", True),
        ("pytest -q -n auto --maxfail 3", True),
        ("pytest -q --splits 4 --group 2", True),
        ("pytest -q -- --test-threads=4", True),
        ("pytest -q tests/unit", False),  # a path narrows the run
        ("pytest -q -m smoke", False),
        ("pytest -q -n auto tests/unit", False),  # `-n` takes one value, not two
        ("pytest -q --grep-invert @slow", False),
    ],
)
def test_added_arguments_must_be_known_safe(after: str, *, ok: bool) -> None:
    head = "on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n"
    problems = _problems(head + f"      - run: {after}\n", head + "      - run: pytest -q\n")
    assert (problems == []) is ok, problems


def test_test_filters_hidden_in_env_are_rejected() -> None:
    head = "on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n      - run: pytest\n"
    problems = _problems(head + "        env:\n          PYTEST_ADDOPTS: -k smoke\n", head)
    assert any("filters tests by keyword" in p for p in problems)


@pytest.mark.parametrize(("shell", "ok"), [("bash {0}", False), ("bash -eo pipefail {0}", True), ("bash", True)])
def test_custom_shell_must_keep_fail_on_error(shell: str, *, ok: bool) -> None:
    head = "on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n      - run: |\n          make\n          pytest\n"
    problems = _problems(head + f"        shell: {shell}\n", head)
    assert (problems == []) is ok, problems


def test_shell_default_without_errexit_is_rejected() -> None:
    jobs = "jobs:\n  t:\n    runs-on: x\n    steps:\n      - run: pytest\n"
    after = "on: push\ndefaults:\n  run:\n    shell: bash {0}\n" + jobs
    assert any("custom `shell:`" in p for p in _problems(after, "on: push\n" + jobs))


def test_widening_a_conditional_continue_on_error_is_rejected() -> None:
    head = "on: push\njobs:\n  t:\n    runs-on: x\n"
    steps = "    steps:\n      - run: pytest\n"
    before = head + "    continue-on-error: ${{ matrix.xfail && true || false }}\n" + steps
    problems = _problems(head + "    continue-on-error: true\n" + steps, before)
    assert problems == [f"{CI}: job `t` widens `continue-on-error` to True"]


MATRIX_HEAD = (
    "on: push\njobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        include:\n"
)
HTTPX_ROW = "          - os: ubuntu-latest\n            python-version: '3.13'\n            without-httpx2: true\n"
PLAIN_ROW = "          - os: ubuntu-latest\n            python-version: '3.13'\n"
MATRIX_STEPS = (
    "    steps:\n      - run: uv pip uninstall httpx2\n        if: matrix.without-httpx2\n      - run: pytest\n"
)


def test_dropping_a_matrix_row_that_only_changes_a_condition_is_rejected() -> None:
    before = MATRIX_HEAD + HTTPX_ROW + PLAIN_ROW + MATRIX_STEPS
    after = MATRIX_HEAD + PLAIN_ROW + MATRIX_STEPS
    problems = _problems(after, before)
    assert any("drops the matrix row" in p and "without-httpx2" in p for p in problems), problems


def test_rows_that_gain_a_shard_key_still_count() -> None:
    before = MATRIX_HEAD + HTTPX_ROW + PLAIN_ROW + MATRIX_STEPS
    shard = (
        HTTPX_ROW
        + "            shard: 1\n"
        + HTTPX_ROW
        + "            shard: 2\n"
        + PLAIN_ROW
        + "            shard: 1\n"
        + PLAIN_ROW
        + "            shard: 2\n"
    )
    after = MATRIX_HEAD + shard + MATRIX_STEPS.replace("- run: pytest", "- run: pytest --shard=${{ matrix.shard }}/2")
    assert _problems(after, before) == []

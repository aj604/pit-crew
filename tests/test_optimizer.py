from pathlib import Path
from typing import Any

import pytest

from pit_crew import tools
from pit_crew.contracts import PLAYBOOK_SKILLS, Candidate, Hypothesis, Verdict
from pit_crew.lanes.optimizer import tools as optimizer
from pit_crew.lanes.optimizer.validate import check_change, is_workflow
from pit_crew.ledger import InMemoryLedger
from pit_crew.managed_agent import build_request

CI = ".github/workflows/ci.yml"

BASE = """\
name: CI
on:
  push:
    branches: [main]
  pull_request:
jobs:
  lint:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - run: npm install
      - run: npm run lint
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    strategy:
      matrix:
        node: [20, 22]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node }}
      - run: npm install
      - run: npx playwright test
      - uses: actions/upload-artifact@v4
        if: failure()
        with:
          name: report
          path: test-results/
      - uses: github/codeql-action/analyze@v3
"""


def _changed(old: str, new: str) -> str:
    assert old in BASE, f"fixture does not contain {old!r}"
    return BASE.replace(old, new, 1)


def _problems(after: str, before: str = BASE, path: str = CI) -> list[str]:
    return check_change(path, before, after).problems


# --- the guard: changes that keep every check ------------------------------------------------


def test_unchanged_workflow_passes() -> None:
    report = check_change(CI, BASE, BASE)
    assert report.ok
    assert report.to_dict() == {"ok": True, "problems": [], "warnings": []}


def test_dep_cache_passes_and_notes_the_changed_install() -> None:
    after = _changed(
        "          node-version: ${{ matrix.node }}\n      - run: npm install",
        "          node-version: ${{ matrix.node }}\n          cache: npm\n      - run: npm ci",
    )
    report = check_change(CI, BASE, after)
    assert report.ok, report.problems
    assert report.warnings == []  # `npm install` still runs in the lint job
    after = after.replace("      - run: npm install\n", "      - run: npm ci\n")
    report = check_change(CI, BASE, after)
    assert report.ok, report.problems
    assert any("npm install" in w for w in report.warnings)


def test_sharding_that_adds_arguments_passes() -> None:
    after = _changed("node: [20, 22]", "node: [20, 22]\n        shard: [1, 2, 3]")
    after = after.replace("npx playwright test", "npx playwright test --shard=${{ matrix.shard }}/3")
    assert _problems(after) == []


def test_removing_an_identical_duplicate_job_passes() -> None:
    duplicated = BASE + "  lint-again:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm run lint\n"
    assert check_change(CI, duplicated, BASE).ok


def test_parallelizing_by_moving_a_command_to_another_job_passes() -> None:
    after = (
        _changed("      - run: npm run lint\n", "")
        + "  lint-only:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm run lint\n"
    )
    assert _problems(after) == []


def test_continued_lines_and_comments_are_normalized() -> None:
    head = "on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n"
    before = head + "      - run: |\n          # unit tests\n          pytest \\\n            -q\n"
    after = head + "      - run: pytest -q --splits 2\n"
    assert _problems(after, before) == []


# --- the guard: changes that check less -----------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("      - run: npm run lint\n", "", "drops the check command `npm run lint`"),
        ("npm run lint", "npm run lint || true", "ignores the exit code"),
        ("npm run lint", "set +e; npm run lint", "turns off fail-on-error"),
        ("npx playwright test", "npx playwright test --retries=2", "retries failed tests"),
        ("npx playwright test", "npx playwright test -k smoke", "filters tests by keyword"),
        ("npx playwright test", "npx playwright test --ignore e2e/slow", "deselects or ignores tests"),
        ("  test:\n    runs-on", "  test:\n    continue-on-error: true\n    runs-on", "continue-on-error"),
        ("  lint:\n    runs-on", "  lint:\n    if: ${{ false }}\n    runs-on", "if: false"),
        ("timeout-minutes: 20", "timeout-minutes: 60", "job `test` widens its timeout from 20"),
        ("    timeout-minutes: 10\n", "", "job `lint` widens its timeout from 10"),
        ("node: [20, 22]", "node: [22]", "job `test` drops matrix node values"),
        ("node: [20, 22]", "node: [20, 22]\n        exclude:\n          - node: 20", "excludes more matrix"),
        ("  pull_request:\n", "", "no longer runs on `pull_request`"),
        (
            "      - uses: github/codeql-action/analyze@v3\n",
            "",
            "drops the check action `github/codeql-action/analyze`",
        ),
        (
            "      - run: npx playwright test\n",
            "      - uses: nick-fields/retry@v3\n      - run: npx playwright test\n",
            "retry action",
        ),
    ],
)
def test_changes_that_check_less_are_rejected(old: str, new: str, expected: str) -> None:
    problems = _problems(_changed(old, new))
    assert any(expected in p for p in problems), problems
    assert all(p.startswith(f"{CI}: ") for p in problems)


def test_dropping_failure_artifacts_is_rejected() -> None:
    upload = (
        "      - uses: actions/upload-artifact@v4\n"
        "        if: failure()\n"
        "        with:\n"
        "          name: report\n"
        "          path: test-results/\n"
    )
    problems = _problems(_changed(upload, ""))
    assert any("uploads fewer artifacts" in p for p in problems)


@pytest.mark.parametrize(
    ("before", "after", "lost"),
    [
        ("on: [push, pull_request]\n", "on: push\n", "pull_request"),
        ("on: workflow_dispatch\n", "on: [push]\n", "workflow_dispatch"),
    ],
)
def test_trigger_forms_are_compared(before: str, after: str, lost: str) -> None:
    jobs = "jobs:\n  t:\n    runs-on: x\n    steps:\n      - run: make test\n"
    assert _problems(after + jobs, before + jobs) == [f"{CI}: no longer runs on `{lost}`"]


def test_removed_job_is_judged_by_its_commands_not_its_timeout() -> None:
    before = BASE + "  docs:\n    runs-on: x\n    timeout-minutes: 5\n    steps:\n      - run: npm run lint\n"
    assert _problems(BASE, before) == []


# --- the guard: file-level checks ------------------------------------------------------------


def test_invalid_yaml_is_rejected() -> None:
    assert "is not valid YAML" in _problems("jobs: [unclosed")[0]


def test_non_mapping_yaml_is_rejected() -> None:
    assert _problems("- just\n- a list\n") == [f"{CI}: is not a YAML mapping"]


def test_workflow_without_jobs_is_rejected() -> None:
    assert _problems("name: CI\non: push\n") == [f"{CI}: has no jobs"]


def test_deleting_a_workflow_is_rejected() -> None:
    assert _problems("") == [f"{CI}: deletes the file; keep every check"]
    assert _problems("", before="") == []


def test_new_workflow_passes_with_a_warning() -> None:
    report = check_change(CI, "", BASE)
    assert report.ok
    assert report.warnings == [f"{CI}: new workflow; nothing to compare against"]


def test_unparseable_original_is_not_compared() -> None:
    report = check_change(CI, "jobs: [unclosed", BASE)
    assert report.ok
    assert "could not be compared" in report.warnings[0]


def test_other_files_are_only_parsed() -> None:
    assert check_change("package.json", "{}", "not json, not checked here").ok
    assert check_change(".github/dependabot.yml", "", "version: 2\n").ok
    assert not check_change(".github/dependabot.yml", "", "version: [2\n").ok


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".github/workflows/ci.yml", True),
        (".github/workflows/ci.yaml", True),
        (".github/workflows/nested/ci.yml", False),
        (".github/actions/setup/action.yml", False),
        ("ci.yml", False),
    ],
)
def test_is_workflow(path: str, *, expected: bool) -> None:
    assert is_workflow(path) is expected


# --- tools ----------------------------------------------------------------------------------


@pytest.fixture
def ledger() -> InMemoryLedger:
    return InMemoryLedger()


@pytest.fixture
def hypothesis(ledger: InMemoryLedger) -> Hypothesis:
    record = Hypothesis("o/r", "npm install misses the cache", "dep-cache", 90.0, base_sha="abc123")
    ledger.add(record)
    return record


def test_get_hypothesis_returns_plain_json(ledger: InMemoryLedger, hypothesis: Hypothesis) -> None:
    result = optimizer.get_hypothesis(ledger, hypothesis.id)
    assert result["repo"] == "o/r"
    assert result["skill"] == "dep-cache"


def test_report_candidate_records_files_and_coordinates(ledger: InMemoryLedger, hypothesis: Hypothesis) -> None:
    after = BASE.replace("npm install", "npm ci")
    candidate_id = tools.call(
        "report_candidate",
        ledger,
        {
            "hypothesis_id": hypothesis.id,
            "branch": f"pc/{hypothesis.id}-dep-cache",
            "summary": "use npm ci",
            "files": {CI: after},
            "original_files": {CI: BASE},
        },
    )
    candidate = ledger.get(Candidate, candidate_id)
    assert candidate.repo == "o/r"
    assert candidate.base_sha == "abc123"
    assert candidate.files == {CI: after}
    assert candidate.changed_files == (CI,)

    ledger.add(Verdict(candidate_id, keep=False, speedup=1.0, p_value=0.4, reason="speedup is within noise"))
    attempts = optimizer.list_prior_attempts(ledger, "o/r")
    assert [(a["candidate_id"], a["reason"]) for a in attempts] == [(candidate_id, "speedup is within noise")]


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ({"branch": "cache", "files": {CI: BASE}, "original_files": {CI: BASE}}, "branch must look like pc/"),
        ({"branch": "pc/../main", "files": {CI: BASE}, "original_files": {CI: BASE}}, "branch must look like pc/"),
        (
            {"branch": "pc/x", "files": {"src/app.py": "x"}, "original_files": {"src/app.py": ""}},
            "outside what Optimizer may change",
        ),
        ({"branch": "pc/x", "files": {CI: BASE}, "original_files": {}}, "missing from original_files"),
        (
            {"branch": "pc/x", "files": {CI: BASE.replace("  pull_request:\n", "")}, "original_files": {CI: BASE}},
            "no longer runs on `pull_request`",
        ),
        (
            {"branch": "pc/x", "files": {f".github/workflows/w{i}.yml": BASE for i in range(6)}, "original_files": {}},
            "at most 5",
        ),
    ],
)
def test_report_candidate_rejects_rule_breaks_and_records_nothing(
    ledger: InMemoryLedger, hypothesis: Hypothesis, candidate: dict[str, Any], expected: str
) -> None:
    with pytest.raises(optimizer.CandidateRejectedError, match="candidate rejected") as error:
        tools.call("report_candidate", ledger, {"hypothesis_id": hypothesis.id, "summary": "s", **candidate})
    assert expected in str(error.value)
    assert ledger.all(Candidate) == []


def test_report_candidate_refuses_an_unknown_hypothesis(ledger: InMemoryLedger) -> None:
    with pytest.raises(KeyError):
        optimizer.report_candidate(ledger, "nope", "pc/x", "summary", files={CI: BASE}, original_files={CI: BASE})


def test_check_workflow_change_tool(ledger: InMemoryLedger) -> None:
    assert optimizer.check_workflow_change(ledger, CI, BASE, BASE)["ok"] is True
    blocked = optimizer.check_workflow_change(ledger, "tests/test_app.py", "", "x")
    assert blocked["ok"] is False
    assert "outside what Optimizer may change" in blocked["problems"][0]


# --- playbooks ------------------------------------------------------------------------------


def test_every_playbook_skill_has_a_playbook_and_it_is_mounted() -> None:
    playbooks = Path(optimizer.__file__).parent / "playbooks"
    assert {p.stem for p in playbooks.glob("*.md")} == set(PLAYBOOK_SKILLS)

    targets = {s["target"] for s in build_request("optimizer", "")["environment"]["sources"]}
    assert {f".agents/skills/optimizer/playbooks/{skill}.md" for skill in PLAYBOOK_SKILLS} <= targets


def test_optimizer_exposes_its_four_tools() -> None:
    build_request("optimizer", "")
    names = {t.name for t in tools.for_lane("optimizer")}
    assert names == {"get_hypothesis", "list_prior_attempts", "check_workflow_change", "report_candidate"}

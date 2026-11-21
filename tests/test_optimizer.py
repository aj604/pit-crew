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


LINT_ONLY = "  lint-only:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm run lint\n"


def test_moving_a_check_out_of_a_kept_job_needs_an_aggregate_gate() -> None:
    moved = _changed("      - run: npm run lint\n", "") + LINT_ONLY
    assert any("job `lint` no longer runs `npm run lint` itself" in p for p in _problems(moved))

    gate = (
        "    needs: [lint-only]\n"
        "    if: always()\n"
        "    runs-on: ubuntu-latest\n"
        "    timeout-minutes: 10\n"
        "    steps:\n"
        "      - run: echo '${{ toJSON(needs) }}' | jq -e 'all(.[]; .result == \"success\")'\n"
    )
    lint_job = BASE[BASE.index("    runs-on: ubuntu-latest\n    timeout-minutes: 10") : BASE.index("  test:")]
    gated = BASE.replace(lint_job, gate, 1) + LINT_ONLY
    assert _problems(gated) == []


def test_compound_install_keeps_its_chained_check() -> None:
    head = "on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n"
    before = head + "      - run: npm install && npm test\n"
    report = check_change(CI, before, head + "      - run: npm ci && npm test\n")
    assert report.ok
    assert report.warnings == [f"{CI}: changes the install command `npm install`"]
    assert _problems(head + "      - run: npm ci\n", before) == [f"{CI}: drops the check `<install> && npm test` on x"]


SIMPLE = "on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("npm test && npm run coverage", "npm test || npm run coverage"),
        ("npm test", "npm test || npm run coverage"),
        ("npm test", "npm test | tee log.txt"),
    ],
)
def test_rewiring_how_checks_chain_is_rejected(before: str, after: str) -> None:
    problems = _problems(SIMPLE + f"      - run: {after}\n", SIMPLE + f"      - run: {before}\n")
    assert any(f"drops the check `{before}`" in p for p in problems), problems


GATED_OLD = "on: push\njobs:\n  check:\n    runs-on: x\n    steps:\n      - run: npm run lint\n      - run: npm test\n"
LINT_JOB = "  lint:\n    runs-on: x\n    steps:\n      - run: npm run lint\n"
JQ = "echo '${{ toJSON(needs) }}' | jq -e 'all(.[]; .result == \"success\")'"


def _gate(condition: str, step: str) -> str:
    return (
        "on: push\njobs:\n  check:\n    needs: [lint]\n"
        f"    if: {condition}\n    runs-on: x\n    steps:\n      - run: npm test\n{step}" + LINT_JOB
    )


@pytest.mark.parametrize(
    ("condition", "step", "ok"),
    [
        ("always()", f"      - run: {JQ}\n", True),
        (
            "${{ always() }}",
            "      - uses: re-actors/alls-green@v1\n        with:\n          jobs: ${{ toJSON(needs) }}\n",
            True,
        ),
        ("always()", "      - run: echo '${{ toJSON(needs) }}'\n", False),  # only prints the results
        ("always()", f"      - run: {JQ} || true\n", False),
        ("always()", f"      - if: github.event_name == 'push'\n        run: {JQ}\n", False),
        ("always() && github.event_name == 'push'", f"      - run: {JQ}\n", False),
        ("success()", f"      - run: {JQ}\n", False),
    ],
)
def test_gate_must_fail_when_a_needed_job_fails(condition: str, step: str, *, ok: bool) -> None:
    problems = _problems(_gate(condition, step), GATED_OLD)
    assert (problems == []) is ok, problems


def test_check_moved_into_a_matrix_job_still_needs_a_gate() -> None:
    after = (
        "on: push\njobs:\n  check:\n    runs-on: x\n    steps:\n      - run: npm test\n"
        "  matrix-checks:\n    runs-on: x\n    strategy:\n      matrix:\n        command: [lint]\n"
        "    steps:\n      - run: npm run ${{ matrix.command }}\n"
    )
    assert any("job `check` no longer runs `npm run lint`" in p for p in _problems(after, GATED_OLD))


def test_include_entries_merge_into_matching_rows() -> None:
    node = "      - uses: actions/setup-node@v4\n        with:\n          node-version: ${{ matrix.node }}\n"
    with_include = (
        "  a:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        node: [20, 22]\n"
        f"        include:\n          - os: ubuntu-latest\n    steps:\n{node}      - run: npm test\n"
    )
    literal = (
        "  b:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n        node: [20, 22]\n"
        f"    steps:\n{node}      - run: npm test\n"
    )
    head = "on: push\njobs:\n"
    assert _problems(head + literal, head + with_include + literal) == []


def test_toolchain_follows_step_order() -> None:
    def setup(version: int) -> str:
        return f"      - uses: actions/setup-node@v4\n        with:\n          node-version: {version}\n"

    before = f"on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n{setup(20)}      - run: npm run lint\n"
    before += f"{setup(22)}      - run: npm test\n"
    after = f"on: push\njobs:\n  lint:\n    runs-on: x\n    steps:\n{setup(20)}      - run: npm run lint\n"
    after += f"  test:\n    runs-on: x\n    steps:\n{setup(22)}      - run: npm test\n"
    assert _problems(after, before) == []


def test_removing_a_trailing_or_true_is_allowed() -> None:
    assert _problems(BASE, _changed("npm run lint", "npm run lint || true")) == []


@pytest.mark.parametrize(
    ("before_jobs", "after_jobs", "expected"),
    [
        (  # a "duplicate" on another OS is not a duplicate
            (
                "  u:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm test\n"
                "  w:\n    runs-on: windows-latest\n    steps:\n      - run: npm test\n"
            ),
            "  u:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm test\n",
            "drops the check `npm test` on windows-latest",
        ),
        (  # matrix runners expand per value
            (
                "  t:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
                "        os: [ubuntu-latest, macos-latest]\n"
                "    steps:\n      - run: npm test\n"
            ),
            "  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm test\n",
            "drops the check `npm test` on macos-latest",
        ),
        (  # include adds a combination; dropping it loses that runner
            (
                "  t:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        os: [a, b]\n"
                "        exclude:\n          - os: a\n        include:\n          - os: c\n"
                "    steps:\n      - run: npm test\n"
            ),
            "  t:\n    runs-on: b\n    steps:\n      - run: npm test\n",
            "drops the check `npm test` on c",
        ),
        (  # a toolchain version is part of where a check runs
            (
                "  t:\n    runs-on: x\n    steps:\n      - uses: actions/setup-node@v4\n        with:\n"
                "          node-version: 20\n      - run: npm test\n"
            ),
            (
                "  t:\n    runs-on: x\n    steps:\n      - uses: actions/setup-node@v4\n        with:\n"
                "          node-version: 22\n      - run: npm test\n"
            ),
            "with actions/setup-node:node-version=20",
        ),
        (  # so is the working directory
            (
                "  t:\n    runs-on: x\n    defaults:\n      run:\n        working-directory: web\n"
                "    steps:\n      - run: npm test\n"
            ),
            "  t:\n    runs-on: x\n    steps:\n      - run: npm test\n",
            "on x in web",
        ),
        (  # and the container
            "  t:\n    runs-on: x\n    container: node:20\n    steps:\n      - run: npm test\n",
            "  t:\n    runs-on: x\n    steps:\n      - run: npm test\n",
            "on x in node:20",
        ),
        (  # a reusable workflow job is a check too
            "  t:\n    uses: org/repo/.github/workflows/test.yml@v1\n",
            "  u:\n    runs-on: x\n    steps:\n      - run: echo hi\n",
            "drops the check `uses org/repo/.github/workflows/test.yml`",
        ),
    ],
)
def test_checks_are_compared_where_they_run(before_jobs: str, after_jobs: str, expected: str) -> None:
    head = "on: push\njobs:\n"
    problems = _problems(head + after_jobs, head + before_jobs)
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (
            "on: [push, pull_request]\n",
            "on:\n  push:\n    branches: [main]\n  pull_request:\n",
            "stops running for tags",
        ),
        ("on: [push]\n", "on:\n  push:\n    paths: ['src/**']\n", "gains a `paths` filter"),
        ("on: [pull_request]\n", "on:\n  pull_request:\n    branches: [main]\n", "gains a `branches` filter"),
    ],
)
def test_narrowed_triggers_are_rejected(before: str, after: str, expected: str) -> None:
    jobs = "jobs:\n  t:\n    runs-on: x\n    steps:\n      - run: make test\n"
    problems = _problems(after + jobs, before + jobs)
    assert any(expected in p for p in problems), problems


def test_branch_filter_that_keeps_tags_is_allowed() -> None:
    jobs = "jobs:\n  t:\n    runs-on: x\n    steps:\n      - run: make test\n"
    after = "on:\n  push:\n    branches: [main]\n    tags: ['**']\n  pull_request:\n"
    assert _problems(after + jobs, "on: [push, pull_request]\n" + jobs) == []


def test_continued_lines_and_comments_are_normalized() -> None:
    head = "on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n"
    before = head + "      - run: |\n          # unit tests\n          pytest \\\n            -q\n"
    after = head + "      - run: pytest -q --splits 2\n"
    assert _problems(after, before) == []


# --- the guard: changes that check less -----------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("      - run: npm run lint\n", "", "drops the check `npm run lint` on ubuntu-latest"),
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
            "drops the check `uses github/codeql-action/analyze`",
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
    before = (
        BASE + "  docs:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n    steps:\n      - run: npm run lint\n"
    )
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


def test_unparseable_original_blocks_the_change() -> None:
    assert _problems(BASE, before="jobs: [unclosed") == [
        f"{CI}: the original does not parse, so the change cannot be checked"
    ]


def test_yaml_aliases_are_rejected() -> None:
    bomb = "a: &a [x, x, x]\nb: &b [*a, *a, *a]\njobs:\n  t:\n    runs-on: *b\n"
    assert _problems(bomb) == [f"{CI}: uses YAML aliases (*name); write the values out"]


def test_oversized_file_is_rejected() -> None:
    assert "is larger than" in _problems("#" * 200_001)[0]


def test_oversized_matrix_is_rejected() -> None:
    axes = "".join(f"        {axis}: [1, 2, 3, 4, 5, 6, 7]\n" for axis in "abc")
    jobs = f"on: push\njobs:\n  t:\n    runs-on: x\n    strategy:\n      matrix:\n{axes}    steps:\n      - run: make\n"
    assert _problems(jobs, jobs) == [f"{CI}: job `t` expands to more than 256 matrix combinations"]


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


def test_report_candidate_accepts_explicit_base_sha(ledger: InMemoryLedger, hypothesis: Hypothesis) -> None:
    after = BASE.replace("npm install", "npm ci")
    explicit_id = tools.call(
        "report_candidate",
        ledger,
        {
            "hypothesis_id": hypothesis.id,
            "branch": f"pc/{hypothesis.id}-dep-cache-2",
            "summary": "use npm ci on newer base",
            "files": {CI: after},
            "original_files": {CI: BASE},
            "base_sha": "def456",
        },
    )
    assert ledger.get(Candidate, explicit_id).base_sha == "def456"


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

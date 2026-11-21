import json
from pathlib import Path

import pytest

from pit_crew import tools
from pit_crew.contracts import Candidate, ReplayPlan
from pit_crew.lanes.proof.checks import Check, FileChange, all_checks, touched_checks
from pit_crew.lanes.proof.miner import JobRun, RedCommit, flaky_checks, red_commits
from pit_crew.lanes.proof.mutants import Mutant, StaleMutantError, apply_mutant, mutants
from pit_crew.lanes.proof.tools import InvalidReplayPlanError, expected_checks_for, pick_replays, uncovered_checks
from pit_crew.ledger import InMemoryLedger, SqliteLedger
from pit_crew.managed_agent import build_request

FIXTURES = Path(__file__).parent / "fixtures"
WORKFLOW = """\
name: CI

on:
  pull_request:

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: bun lint
  test:
    name: Unit tests
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [1, 2]
    steps:
      - run: bun test --shard ${{ matrix.shard }}/2
  report:
    needs: [test]
    runs-on: ubuntu-latest
    steps:
      - run: echo done
"""
PATH = ".github/workflows/ci.yml"


def check(*keys: str) -> Check:
    return frozenset(keys)


LINT, TEST, REPORT = check("CI/lint"), check("CI/test", "CI/Unit tests"), check("CI/report")
EVERY_JOB = {LINT, TEST, REPORT}


def change(after: str | None, before: str | None = WORKFLOW, path: str = PATH) -> list[FileChange]:
    return [{"path": path, "before": before, "after": after}]


def job_runs() -> list[JobRun]:
    runs: list[JobRun] = json.loads((FIXTURES / "check_runs.json").read_text())
    return runs


@pytest.fixture
def red() -> list[RedCommit]:
    return red_commits(job_runs())


# Miner


def test_miner_keeps_only_deterministic_failures_newest_first(red: list[RedCommit]) -> None:
    assert [(c.sha, c.checks, c.fixed) for c in red] == [
        ("1818181", ("CI/Unit tests",), False),
        ("d4d4d4d", ("CI/lint",), True),
        ("b2b2b2b", ("CI/Unit tests",), True),
    ]


def test_miner_reports_flaky_checks_separately() -> None:
    assert flaky_checks(job_runs()) == [("f6f6f6f", "CI/Unit tests")]


# Diff to checks


def test_a_job_change_touches_that_job_and_its_dependents() -> None:
    after = WORKFLOW.replace("bun test --shard", "bun test --randomize --shard")
    assert touched_checks(change(after)) == {TEST, REPORT}


def test_a_header_change_touches_every_job_in_the_workflow() -> None:
    after = WORKFLOW.replace("  pull_request:", "  pull_request:\n    paths: ['src/**']")
    assert touched_checks(change(after)) == EVERY_JOB


def test_a_comment_or_formatting_change_touches_nothing() -> None:
    after = WORKFLOW.replace("  lint:", "  # fast checks\n  lint:").replace(
        "shard: [1, 2]", "shard:\n          - 1\n          - 2"
    )
    assert touched_checks(change(after)) == set()


def test_a_removed_job_is_touched_so_its_bugs_are_replayed() -> None:
    after = WORKFLOW.replace("  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: bun lint\n", "")
    assert touched_checks(change(after)) == {LINT}


def test_added_and_deleted_workflows_touch_their_jobs() -> None:
    assert touched_checks(change(WORKFLOW, before=None)) == EVERY_JOB
    assert touched_checks(change(None)) == EVERY_JOB


def test_flow_style_yaml_is_read_like_any_other() -> None:
    before = "name: CI\njobs: {lint: {runs-on: x}, test: {runs-on: x}}\n"
    after = "name: CI\njobs: {lint: {runs-on: y}, test: {runs-on: x}}\n"
    assert touched_checks(change(after, before)) == {LINT}


def test_a_renamed_job_is_one_check_under_every_name() -> None:
    after = WORKFLOW.replace("name: Unit tests", "name: Tests")
    assert touched_checks(change(after)) == {check("CI/test", "CI/Unit tests", "CI/Tests"), REPORT}


def test_a_change_outside_workflows_touches_every_check() -> None:
    assert touched_checks([{"path": "vitest.config.ts", "before": "a", "after": "b"}]) is None


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/../ci.yml",  # really .github/ci.yml
        ".github/workflows/old/ci.yml",  # GitHub runs only files directly in .github/workflows
        ".github/workflows/ci.json",
        "src/.github/workflows/ci.yml",
    ],
)
def test_a_file_github_would_not_run_as_a_workflow_touches_every_check(path: str) -> None:
    assert touched_checks(change(WORKFLOW.replace("bun lint", "bun lint --quiet"), path=path)) is None


@pytest.mark.parametrize(
    "broken",
    [
        "name: CI\njobs: [lint\n",
        "name: CI\njobs: [lint, test]\n",
        "just text\n",
        "name: CI\njobs: {lint: 3}\n",
        "name: CI\njobs: {lint: {}, test: {needs: 123}}\n",
        "name: CI\njobs: {lint: {}, test: {needs: [lint, {x: 1}]}}\n",
    ],
)
def test_a_workflow_it_cannot_read_touches_every_check(broken: str) -> None:
    assert touched_checks(change(broken)) is None


def test_all_checks_lists_every_job_by_id_and_name() -> None:
    assert all_checks({PATH: WORKFLOW}) == EVERY_JOB
    assert all_checks({PATH: WORKFLOW, ".github/workflows/bad.yml": "jobs: 3"}) is None


# Choosing replays


def test_picks_only_bugs_the_touched_checks_caught_fixed_ones_first(red: list[RedCommit]) -> None:
    assert pick_replays(red, {TEST}) == ["b2b2b2b", "1818181"]


def test_takes_turns_between_checks_when_every_check_is_touched(red: list[RedCommit]) -> None:
    assert pick_replays(red, None, limit=2) == ["b2b2b2b", "d4d4d4d"]


def test_a_bug_two_checks_caught_is_picked_once() -> None:
    both = RedCommit("abcd123", "main", "2026-11-01T00:00:00Z", ("CI/lint", "CI/test"), fixed=True)
    assert pick_replays([both], None) == ["abcd123"]


def test_touched_checks_with_no_bug_need_a_mutant(red: list[RedCommit]) -> None:
    assert uncovered_checks(red, {LINT, check("CI/typecheck")}) == {check("CI/typecheck")}


def test_a_job_with_bugs_under_its_display_name_is_covered(red: list[RedCommit]) -> None:
    # CI history names the job "Unit tests", never "test", so the id alone must not count as a gap.
    touched = touched_checks(change(WORKFLOW.replace("bun test --shard", "bun test --randomize --shard")))
    assert touched is not None
    assert uncovered_checks(red, touched) == {REPORT}


def test_diff_to_replays_end_to_end(red: list[RedCommit]) -> None:
    touched = touched_checks(change(WORKFLOW.replace("bun lint", "bun lint --quiet")))
    assert touched is not None
    assert pick_replays(red, touched) == ["d4d4d4d"]
    assert uncovered_checks(red, touched) == set()


def test_expected_checks_for_preserves_aliases_and_scopes_multi_check_commits(red: list[RedCommit]) -> None:
    unit_bug = next(c for c in red if c.sha == "b2b2b2b")
    renamed = check("CI/test", "CI/Unit tests", "CI/Tests")
    assert expected_checks_for(unit_bug, {TEST}) == ("CI/Unit tests", "CI/test")
    assert expected_checks_for(unit_bug, {renamed}) == ("CI/Tests", "CI/Unit tests", "CI/test")

    both = RedCommit("abcd123", "main", "2026-11-01T00:00:00Z", ("CI/Unit tests", "CI/lint"), fixed=True)
    assert expected_checks_for(both, {TEST}) == ("CI/Unit tests", "CI/test")
    assert expected_checks_for(both, {LINT, TEST}) == ("CI/Unit tests", "CI/lint", "CI/test")
    assert expected_checks_for(both, None) == ("CI/Unit tests", "CI/lint")
    assert expected_checks_for(unit_bug, {LINT}) == ("CI/Unit tests",)


# Mutation fallback

TS = """\
const label = "a == b"; // x < y
export function canMerge(pr: Array<string>, ready: boolean): boolean {
  /* a && b */
  return pr.length > 0 && ready === true;
}
const f = (x) => x;
const tpl = `a > b`;
"""


def test_mutants_flip_operators_in_code_but_not_strings_comments_or_generics() -> None:
    ops = [(m.line, m.operator) for m in mutants("src/merge.ts", TS)]
    assert ops == [(4, "> -> <="), (4, "&& -> ||"), (4, "=== -> !=="), (4, "true -> false")]


def test_python_mutants_flip_words_and_skip_docstrings_and_comments() -> None:
    source = 'def ok(a, b):\n    """a and b"""\n    return a and b is not False  # or True\n'
    assert [m.operator for m in mutants("ok.py", source)] == ["and -> or", "False -> True"]


def test_and_or_are_words_outside_python() -> None:
    assert mutants("note.js", "const s = a and b;\n") == []


def test_shell_redirects_and_words_are_not_mutated() -> None:
    source = 'echo ok > out.txt\nsort < in.txt > sorted.txt\necho cats and dogs\n[[ "$a" == "$b" ]] && true\n'
    ops = [(m.line, m.operator) for m in mutants("ci.sh", source)]
    assert ops == [(4, "== -> !="), (4, "&& -> ||"), (4, "true -> false")]


def test_shell_order_comparisons_flip_only_inside_arithmetic() -> None:
    source = (
        "if (( retries < max_retries )); then echo retry > log.txt; fi\n"
        "echo $(( (a + 1) >= b ))\n"
        '[[ "$x" < "$y" ]] && sort < in.txt\n'
    )
    ops = [(m.line, m.operator) for m in mutants("retry.sh", source)]
    assert ops == [(1, "< -> >="), (2, ">= -> <"), (3, "&& -> ||")]


@pytest.mark.parametrize("opening", ["echo $(( ", "(( "])
def test_shell_order_comparisons_skip_commands_nested_in_arithmetic(opening: str) -> None:
    line = f"{opening}$(echo 1 > log; [[ a < b ]]; echo $(( x >= y ))) < limit ))"
    order_mutants = [m.after for m in mutants("retry.sh", line)]
    assert order_mutants == [
        f"{opening}$(echo 1 > log; [[ a < b ]]; echo $(( x < y ))) < limit ))",
        f"{opening}$(echo 1 > log; [[ a < b ]]; echo $(( x >= y ))) >= limit ))",
    ]


@pytest.mark.parametrize(
    ("redirect", "indent"),
    [("<<'EOF'", ""), ('<<"EOF"', ""), ("<<-'EOF'", "\t"), (r"<<E\OF", ""), ("<<$'EOF'", ""), ('<<$"EOF"', "")],
)
def test_shell_mutants_skip_quoted_heredoc_bodies(redirect: str, indent: str) -> None:
    source = f"cat {redirect}\n{indent}(( a < b )) && true\n{indent}EOF\n(( x > y ))\n"
    assert mutants("retry.sh", source) == [
        Mutant("retry.sh", 4, "(( x > y ))", "(( x <= y ))", "> -> <="),
    ]


def test_shell_heredocs_are_consumed_in_redirection_order() -> None:
    source = "cat <<FIRST <<'SECOND'\ntrue\nFIRST\n(( a < b ))\nSECOND\n(( x > y ))\n"
    assert [(m.line, m.operator) for m in mutants("retry.sh", source)] == [(2, "true -> false"), (6, "> -> <=")]


def test_shell_heredocs_inside_command_substitution_have_their_own_queue() -> None:
    source = "echo <<'OUTER' $(cat <<'INNER'\n(( a < b ))\nINNER\n(( x > y ))\n)\n(( a > b ))\nOUTER\n(( x < y ))\n"
    assert [(m.line, m.operator) for m in mutants("retry.sh", source)] == [(4, "> -> <="), (8, "< -> >=")]


def test_booleans_flip_only_where_the_language_spells_them_that_way() -> None:
    assert mutants("a.ts", "const t = True;\n") == []
    assert mutants("a.py", "x = true\n") == []
    assert [m.operator for m in mutants("a.rb", "x = true\n")] == ["true -> false"]


def test_unterminated_strings_and_comments_are_masked_to_the_end() -> None:
    assert mutants("x.ts", "/* a == b") == []
    assert mutants("x.py", '"""a == b') == []
    assert mutants("x.ts", "s = 'a == b\nok = a == b") == [Mutant("x.ts", 2, "ok = a == b", "ok = a != b", "== -> !=")]


def test_unknown_languages_get_no_mutants() -> None:
    assert mutants("README.md", "a == b") == []


def test_mutants_are_spread_across_the_file_when_capped() -> None:
    source = "\n".join(f"x{i} = a == b" for i in range(100))
    assert [m.line for m in mutants("m.py", source, limit=4)] == [1, 26, 51, 76]


def test_a_limit_below_one_gets_no_mutants() -> None:
    assert mutants("m.py", "x = a == b\n", limit=0) == []


def test_apply_mutant_changes_one_line_and_refuses_a_stale_one() -> None:
    mutant = mutants("src/merge.ts", TS)[0]
    patched = apply_mutant(TS, mutant)
    assert patched.split("\n")[3] == "  return pr.length <= 0 && ready === true;"
    with pytest.raises(StaleMutantError, match=r"src/merge\.ts:4"):
        apply_mutant(patched, mutant)


@pytest.mark.parametrize("line", [0, -1, 3])
def test_apply_mutant_refuses_a_line_the_file_does_not_have(line: int) -> None:
    source = "x = a == b\n"  # two lines: the code and an empty last one
    mutant = Mutant("m.py", line, "", "y", "== -> !=")
    with pytest.raises(StaleMutantError):
        apply_mutant(source, mutant)


# Tools


@pytest.fixture
def ledger_and_candidate() -> tuple[InMemoryLedger, str]:
    build_request("proof", "")
    ledger = InMemoryLedger()
    candidate = Candidate("h1", "pc/cache", "cache deps")
    ledger.add(candidate)
    return ledger, candidate.id


def test_plan_replays_dedupes_and_normalises(ledger_and_candidate: tuple[InMemoryLedger, str]) -> None:
    ledger, candidate_id = ledger_and_candidate
    plan_id = tools.call("plan_replays", ledger, {"candidate_id": candidate_id, "bug_shas": ["B2B2B2B", "b2b2b2b "]})
    plan = ledger.get(ReplayPlan, plan_id)
    assert plan.bug_shas == ("b2b2b2b",)
    assert [(c.kind, c.commit_sha) for c in plan.cases] == [("commit", "b2b2b2b")]


def test_plan_replays_records_mutations_as_bug_cases(ledger_and_candidate: tuple[InMemoryLedger, str]) -> None:
    ledger, candidate_id = ledger_and_candidate
    mutation = {"path": "src/merge.ts", "content": "return a != b;\n", "reason": "typecheck: == -> !="}
    plan_id = tools.call(
        "plan_replays", ledger, {"candidate_id": candidate_id, "bug_shas": ["b2b2b2b"], "mutations": [mutation]}
    )
    plan = ledger.get(ReplayPlan, plan_id)
    assert plan.bug_shas == ("b2b2b2b",)
    assert [c.kind for c in plan.cases] == ["commit", "mutation"]
    assert plan.cases[1].mutated_files == {"src/merge.ts": "return a != b;\n"}
    assert plan.cases[1].reason == "typecheck: == -> !="


def test_a_plan_of_only_mutations_is_valid(ledger_and_candidate: tuple[InMemoryLedger, str]) -> None:
    ledger, candidate_id = ledger_and_candidate
    mutation = {"path": "a.py", "content": "x = 1 != 1\n", "reason": ""}
    plan = ledger.get(
        ReplayPlan, tools.call("plan_replays", ledger, {"candidate_id": candidate_id, "mutations": [mutation]})
    )
    assert plan.bug_shas == ()
    assert plan.cases[0].reason == "synthetic mutation"


def test_plan_replays_populates_expected_checks_for_commits_and_mutations(tmp_path: Path) -> None:
    build_request("proof", "")
    ledger = SqliteLedger(tmp_path / "pit_crew.db")
    candidate = Candidate("h1", "pc/cache", "cache deps")
    ledger.add(candidate)

    plan_id = tools.call(
        "plan_replays",
        ledger,
        {
            "candidate_id": candidate.id,
            "commits": [
                {
                    "sha": "B2B2B2B ",
                    "expected_checks": [" CI/Unit tests ", "CI/test", "CI/Unit tests"],
                    "reason": "unit test regression",
                },
                {"sha": "d4d4d4d", "expected_checks": ["CI/lint"]},
            ],
            "mutations": [
                {
                    "path": "src/merge.ts",
                    "content": "return a != b;\n",
                    "reason": "report check: == -> !=",
                    "expected_checks": ["CI/report", "CI/Report job", "CI/report"],
                }
            ],
        },
    )
    plan = ledger.get(ReplayPlan, plan_id)
    assert plan.bug_shas == ("b2b2b2b", "d4d4d4d")
    assert [(c.kind, c.commit_sha, c.expected_checks, c.reason) for c in plan.cases] == [
        ("commit", "b2b2b2b", ("CI/Unit tests", "CI/test"), "unit test regression"),
        ("commit", "d4d4d4d", ("CI/lint",), "historical red commit"),
        ("mutation", None, ("CI/report", "CI/Report job"), "report check: == -> !="),
    ]


def test_plan_replays_merges_duplicate_commit_shas_across_bug_shas_and_commits(
    ledger_and_candidate: tuple[InMemoryLedger, str],
) -> None:
    ledger, candidate_id = ledger_and_candidate
    plan_id = tools.call(
        "plan_replays",
        ledger,
        {
            "candidate_id": candidate_id,
            "bug_shas": ["b2b2b2b", "d4d4d4d"],
            "commits": [
                {"sha": "B2B2B2B", "expected_checks": ["CI/Unit tests"], "reason": "first reason"},
                {"sha": "b2b2b2b", "expected_checks": ["CI/test", "CI/Unit tests"], "reason": "ignored second"},
            ],
        },
    )
    plan = ledger.get(ReplayPlan, plan_id)
    assert plan.bug_shas == ("b2b2b2b", "d4d4d4d")
    assert [(c.commit_sha, c.expected_checks, c.reason) for c in plan.cases] == [
        ("b2b2b2b", ("CI/Unit tests", "CI/test"), "first reason"),
        ("d4d4d4d", (), "historical red commit"),
    ]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"bug_shas": []}, "at least one bug"),
        ({"bug_shas": ["b2b2b2b", "main"]}, "not commit SHAs: main"),
        ({"commits": [{"sha": "main", "expected_checks": ["CI/lint"]}]}, "not commit SHAs: main"),
        ({"commits": [{"sha": 123, "expected_checks": ["CI/lint"]}]}, "not commit SHAs: 123"),
        ({"commits": [{"sha": "b2b2b2b"}]}, "expected_checks"),
        ({"commits": [{"sha": "b2b2b2b", "expected_checks": None}]}, "expected_checks"),
        ({"commits": [{"sha": "b2b2b2b", "expected_checks": []}]}, "expected_checks"),
        ({"commits": [{"sha": "b2b2b2b", "expected_checks": "CI/lint"}]}, "expected_checks"),
        ({"commits": [{"sha": "b2b2b2b", "expected_checks": [""]}]}, "expected_checks"),
        ({"commits": [{"sha": "b2b2b2b", "expected_checks": [42]}]}, "expected_checks"),
        ({"mutations": [{"path": "/etc/passwd", "content": "x", "reason": "r"}]}, "relative path"),
        ({"mutations": [{"path": "../up.py", "content": "x", "reason": "r"}]}, "relative path"),
        ({"mutations": [{"path": "..\\..\\etc\\passwd", "content": "x", "reason": "r"}]}, "relative path"),
        ({"mutations": [{"path": ".", "content": "x", "reason": "r"}]}, "relative path"),
        ({"mutations": [{"path": "./", "content": "x", "reason": "r"}]}, "relative path"),
        ({"mutations": [{"path": "src//a.py", "content": "x", "reason": "r"}]}, "relative path"),
        ({"mutations": [{"path": "", "content": "x", "reason": "r"}]}, "relative path"),
        ({"mutations": [{"path": 123, "content": "x", "reason": "r"}]}, "relative path"),
        ({"mutations": [{"path": "a.py", "content": "", "reason": "r"}]}, "whole mutated file"),
        (
            {"mutations": [{"path": ".github/workflows/ci.yml", "content": "name: evil\n", "reason": "r"}]},
            r"may not modify \.github files",
        ),
        (
            {"mutations": [{"path": ".github/actions/setup/action.yml", "content": "name: evil\n", "reason": "r"}]},
            r"may not modify \.github files",
        ),
        (
            {"mutations": [{"path": ".GITHUB/workflows/ci.yml", "content": "name: evil\n", "reason": "r"}]},
            r"may not modify \.github files",
        ),
        ({"mutations": [{"path": "a.py", "content": 123, "reason": "r"}]}, "whole mutated file"),
        (
            {"mutations": [{"path": "a.py", "content": "x", "reason": "r", "expected_checks": None}]},
            "expected_checks",
        ),
        (
            {"mutations": [{"path": "a.py", "content": "x", "reason": "r", "expected_checks": []}]},
            "expected_checks",
        ),
        (
            {"mutations": [{"path": "a.py", "content": "x", "reason": "r", "expected_checks": "CI/lint"}]},
            "expected_checks",
        ),
        (
            {"mutations": [{"path": "a.py", "content": "x", "reason": "r", "expected_checks": ["  "]}]},
            "expected_checks",
        ),
    ],
)
def test_plan_replays_refuses_a_plan_that_proves_nothing(
    ledger_and_candidate: tuple[InMemoryLedger, str], arguments: dict[str, object], message: str
) -> None:
    ledger, candidate_id = ledger_and_candidate
    with pytest.raises(InvalidReplayPlanError, match=message):
        tools.call("plan_replays", ledger, {"candidate_id": candidate_id, **arguments})
    assert ledger.all(ReplayPlan) == []


def test_plan_replays_schema_requires_non_empty_expected_checks() -> None:
    build_request("proof", "")
    (spec,) = [t for t in tools.for_lane("proof") if t.name == "plan_replays"]
    commits_item = spec.parameters["properties"]["commits"]["items"]
    mutations_item = spec.parameters["properties"]["mutations"]["items"]
    assert "expected_checks" in commits_item["required"]
    assert commits_item["properties"]["expected_checks"]["minItems"] == 1
    assert "expected_checks" in mutations_item["required"]
    assert mutations_item["properties"]["expected_checks"]["minItems"] == 1


def test_plan_replays_refuses_an_unknown_candidate() -> None:
    build_request("proof", "")
    with pytest.raises(KeyError):
        tools.call("plan_replays", InMemoryLedger(), {"candidate_id": "nope", "bug_shas": ["b2b2b2b"]})


def test_propose_mutants_returns_json_ready_dicts() -> None:
    build_request("proof", "")
    result = tools.call("propose_mutants", InMemoryLedger(), {"path": "src/merge.ts", "source": TS, "limit": 1})
    assert result == [
        {
            "path": "src/merge.ts",
            "line": 4,
            "before": "  return pr.length > 0 && ready === true;",
            "after": "  return pr.length <= 0 && ready === true;",
            "operator": "> -> <=",
        }
    ]
    assert json.loads(json.dumps(result)) == result


def test_propose_mutants_asks_for_at_least_one_and_survives_zero() -> None:
    build_request("proof", "")
    (spec,) = [t for t in tools.for_lane("proof") if t.name == "propose_mutants"]
    assert spec.parameters["properties"]["limit"]["minimum"] == 1
    assert tools.call("propose_mutants", InMemoryLedger(), {"path": "a.py", "source": "x = a == b\n", "limit": 0}) == []

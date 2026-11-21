import json
from pathlib import Path

import pytest

from pit_crew import tools
from pit_crew.contracts import Candidate, ReplayPlan
from pit_crew.lanes.proof.checks import all_checks, touched_checks
from pit_crew.lanes.proof.miner import RedCommit, flaky_checks, red_commits
from pit_crew.lanes.proof.mutants import Mutant, apply_mutant, mutants
from pit_crew.lanes.proof.tools import pick_replays, uncovered_checks
from pit_crew.ledger import InMemoryLedger
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


def change(after: str, before: str = WORKFLOW, path: str = PATH) -> list[dict]:
    return [{"path": path, "before": before, "after": after}]


@pytest.fixture
def red() -> list[RedCommit]:
    return red_commits(json.loads((FIXTURES / "check_runs.json").read_text()))


# Miner


def test_miner_keeps_only_deterministic_failures_newest_first(red):
    assert [(c.sha, c.checks, c.fixed) for c in red] == [
        ("1818181", ("CI/Unit tests",), False),
        ("d4d4d4d", ("CI/lint",), True),
        ("b2b2b2b", ("CI/Unit tests",), True),
    ]


def test_miner_reports_flaky_checks_separately():
    runs = json.loads((FIXTURES / "check_runs.json").read_text())
    assert flaky_checks(runs) == [("f6f6f6f", "CI/Unit tests")]


# Diff to checks


def test_a_job_change_touches_that_job_and_its_dependents():
    after = WORKFLOW.replace("bun test --shard", "bun test --randomize --shard")
    assert touched_checks(change(after)) == {"CI/test", "CI/Unit tests", "CI/report"}


def test_a_header_change_touches_every_job_in_the_workflow():
    after = WORKFLOW.replace("  pull_request:", "  pull_request:\n    paths: ['src/**']")
    assert touched_checks(change(after)) == {"CI/lint", "CI/test", "CI/Unit tests", "CI/report"}


def test_a_comment_only_change_touches_nothing():
    after = WORKFLOW.replace("  lint:", "  # fast checks\n  lint:")
    assert touched_checks(change(after)) == set()


def test_a_removed_job_is_touched_so_its_bugs_are_replayed():
    after = WORKFLOW.replace("  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: bun lint\n", "")
    assert touched_checks(change(after)) == {"CI/lint"}


def test_a_change_outside_workflows_touches_every_check():
    assert touched_checks([{"path": "vitest.config.ts", "before": "a", "after": "b"}]) is None


def test_workflow_yaml_it_cannot_read_touches_every_check():
    assert touched_checks(change("name: CI\njobs: {lint: {runs-on: x}}\n")) is None


def test_all_checks_lists_every_job_by_id_and_name():
    assert all_checks({PATH: WORKFLOW}) == {"CI/lint", "CI/test", "CI/Unit tests", "CI/report"}


# Choosing replays


def test_picks_only_bugs_the_touched_checks_caught_fixed_ones_first(red):
    assert pick_replays(red, {"CI/test", "CI/Unit tests"}) == ["b2b2b2b", "1818181"]


def test_takes_turns_between_checks_when_every_check_is_touched(red):
    assert pick_replays(red, None, limit=2) == ["b2b2b2b", "d4d4d4d"]


def test_touched_checks_with_no_bug_need_a_mutant(red):
    assert uncovered_checks(red, {"CI/lint", "CI/typecheck"}) == {"CI/typecheck"}


def test_diff_to_replay_plan_end_to_end(red):
    after = WORKFLOW.replace("bun lint", "bun lint --quiet")
    touched = touched_checks(change(after))
    assert pick_replays(red, touched) == ["d4d4d4d"]
    assert uncovered_checks(red, touched) == set()


# Mutation fallback

TS = """\
const label = "a == b"; // x < y
export function canMerge(pr: Array<string>, ready: boolean): boolean {
  /* a && b */
  return pr.length > 0 && ready === true;
}
const f = (x) => x;
"""


def test_mutants_flip_operators_in_code_but_not_strings_comments_or_generics():
    ops = [(m.line, m.operator) for m in mutants("src/merge.ts", TS)]
    assert ops == [(4, "> -> <="), (4, "&& -> ||"), (4, "=== -> !=="), (4, "true -> false")]


def test_python_mutants_flip_words_and_skip_docstrings():
    source = 'def ok(a, b):\n    """a and b"""\n    return a and b is not False\n'
    assert [m.operator for m in mutants("ok.py", source)] == ["and -> or", "False -> True"]


def test_unknown_languages_get_no_mutants():
    assert mutants("README.md", "a == b") == []


def test_mutants_are_spread_across_the_file_when_capped():
    source = "\n".join(f"x{i} = a == b" for i in range(100))
    lines = [m.line for m in mutants("m.py", source, limit=4)]
    assert lines == [1, 26, 51, 76]


def test_apply_mutant_changes_one_line_and_refuses_a_stale_one():
    mutant = mutants("src/merge.ts", TS)[0]
    patched = apply_mutant(TS, mutant)
    assert patched.split("\n")[3] == "  return pr.length <= 0 && ready === true;"
    with pytest.raises(ValueError):
        apply_mutant(patched, Mutant(mutant.path, 4, mutant.before, mutant.after, mutant.operator))


# Tools


@pytest.fixture
def ledger_with_candidate() -> tuple[InMemoryLedger, str]:
    build_request("proof", "")
    ledger = InMemoryLedger()
    candidate = Candidate("h1", "pc/cache", "cache deps")
    ledger.add(candidate)
    return ledger, candidate.id


def test_plan_replays_dedupes_and_normalises(ledger_with_candidate):
    ledger, candidate_id = ledger_with_candidate
    plan_id = tools.call("plan_replays", ledger, {"candidate_id": candidate_id, "bug_shas": ["B2B2B2B", "b2b2b2b "]})
    assert ledger.get(ReplayPlan, plan_id).bug_shas == ("b2b2b2b",)


def test_plan_replays_refuses_an_empty_plan(ledger_with_candidate):
    ledger, candidate_id = ledger_with_candidate
    with pytest.raises(ValueError, match="at least one bug"):
        tools.call("plan_replays", ledger, {"candidate_id": candidate_id, "bug_shas": []})


def test_plan_replays_refuses_something_that_is_not_a_sha(ledger_with_candidate):
    ledger, candidate_id = ledger_with_candidate
    with pytest.raises(ValueError, match="main"):
        tools.call("plan_replays", ledger, {"candidate_id": candidate_id, "bug_shas": ["b2b2b2b", "main"]})


def test_plan_replays_refuses_an_unknown_candidate():
    build_request("proof", "")
    with pytest.raises(KeyError):
        tools.call("plan_replays", InMemoryLedger(), {"candidate_id": "nope", "bug_shas": ["b2b2b2b"]})


def test_propose_mutants_returns_json_ready_dicts():
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
    json.dumps(result)

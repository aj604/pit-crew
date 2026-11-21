import gzip
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pit_crew.lanes.analysis.critical_path import CIProfile, CriticalPath, profile
from pit_crew.lanes.analysis.detectors.parallelize import _trivial_run, detect_parallelize_dag
from pit_crew.lanes.analysis.history import parse_runs
from pit_crew.lanes.analysis.workflow import Workflow, parse_workflow

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 1, tzinfo=UTC)
PATH = ".github/workflows/ci.yml"
SPANS = {"lint": (0, 60), "test": (60, 360)}


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def job(name: str, start: float, end: float) -> dict[str, Any]:
    return {
        "id": sum(map(ord, name)),
        "name": name,
        "run_attempt": 1,
        "conclusion": "success",
        "created_at": at(start),
        "started_at": at(start),
        "completed_at": at(end),
        "labels": [],
        "steps": [],
    }


def profiled(yaml_text: str, spans: Mapping[str, tuple[float, float]]) -> tuple[CIProfile, Workflow]:
    wf = parse_workflow(yaml_text)
    raw = [
        {
            "id": i,
            "path": PATH,
            "event": "push",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "conclusion": "success",
            "run_attempt": 1,
            "run_started_at": at(0),
            "jobs": [job(n, s, e) for n, (s, e) in spans.items()],
        }
        for i in (1, 2, 3)
    ]
    return profile(wf, parse_runs(raw)), wf


def two_jobs(test_body: str, lint_body: str = "") -> str:
    return f"""
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
{lint_body}
  test:
    needs: lint
    runs-on: ubuntu-latest
{test_body}
"""


def test_parallelize_flags_lint_before_test() -> None:
    p, wf = profiled(two_jobs("    steps:\n      - run: pytest"), SPANS)
    [f] = detect_parallelize_dag(p, wf, PATH)
    assert f.skill == "parallelize-dag"
    assert f.job == "test"
    assert f.step == ""
    assert f.bucket == ""
    assert f.risk == "medium"
    assert f.confidence == 0.6
    assert f.expected_savings_seconds == 60.0
    assert f.evidence == {
        "removed_need": "lint",
        "critical_path_seconds": 360.0,
        "critical_path_after_seconds": 300.0,
        "max_saving_seconds": 60.0,
        "scored_independently": "true",
    }


def test_parallelize_keeps_edges_that_pass_outputs() -> None:
    body = '    steps:\n      - run: echo "${{ needs.lint.outputs.x }}"'
    p, wf = profiled(two_jobs(body), SPANS)
    assert detect_parallelize_dag(p, wf, PATH) == []


def test_parallelize_keeps_artifact_edges() -> None:
    lint = "    steps:\n      - uses: actions/upload-artifact@v4"
    test = "    steps:\n      - uses: actions/download-artifact@v4"
    p, wf = profiled(two_jobs(test, lint), SPANS)
    assert detect_parallelize_dag(p, wf, PATH) == []


def test_parallelize_keeps_gate_jobs() -> None:
    wf_yaml = """
name: CI
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
  b:
    needs: a
    runs-on: ubuntu-latest
  gate:
    needs: [a, b]
    runs-on: ubuntu-latest
    steps:
      - run: echo '${{ toJSON(needs) }}'
"""
    p, wf = profiled(wf_yaml, {"a": (0, 50), "b": (50, 100), "gate": (100, 110)})
    assert [f.job for f in detect_parallelize_dag(p, wf, PATH)] == ["b"]  # the gate's own edges stay


def test_parallelize_ignores_edges_off_the_critical_path() -> None:
    p, wf = profiled(two_jobs("    steps:\n      - run: pytest"), SPANS)
    off_path = replace(p, critical_path=CriticalPath(0.0, ()))
    assert detect_parallelize_dag(off_path, wf, PATH) == []


def test_parallelize_empty_profile() -> None:
    wf = parse_workflow(two_jobs("    steps:\n      - run: pytest"))
    assert detect_parallelize_dag(profile(wf, []), wf, PATH) == []


def test_parallelize_chain_saving_exceeds_job_duration() -> None:
    chain = """
name: CI
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
  b:
    needs: a
    runs-on: ubuntu-latest
  c:
    needs: b
    runs-on: ubuntu-latest
"""
    p, wf = profiled(chain, {"a": (0, 100), "b": (100, 110), "c": (110, 120)})
    by_job = {f.job: f for f in detect_parallelize_dag(p, wf, PATH)}
    # Cutting `b.needs: a` lets b start at 0, so the critical path drops from 120 to 100: 20s from a 10s job.
    # The ceiling is the critical-path drop, not b's own max_saving (10s).
    assert by_job["b"].expected_savings_seconds == 20.0
    assert by_job["b"].evidence["max_saving_seconds"] == 20.0
    assert by_job["c"].expected_savings_seconds == 10.0  # c alone off the chain: 120 -> 110
    for f in by_job.values():
        assert f.expected_savings_seconds <= float(f.evidence["max_saving_seconds"])


def test_parallelize_fastapi() -> None:
    wf = parse_workflow((FIXTURES / "fastapi" / "test.yml").read_text())
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as f:
        runs = parse_runs(json.load(f))
    # Critical path is changes -> test -> coverage-combine -> test-alls-green. test reads changes' outputs,
    # coverage-combine downloads the artifacts test uploads, and test-alls-green is the toJSON(needs) gate.
    assert detect_parallelize_dag(profile(wf, runs), wf, "test.yml") == []


def gated(job_body: str, steps: str) -> str:
    return two_jobs(f"{job_body}\n{steps}" if job_body else steps)


GATE_FORMS = {
    "vite test-passed": ("    if: (!cancelled() && !failure())", '    steps:\n      - run: echo "Build & Test Passed"'),
    "bare failure": ("    if: '!failure()'", "    steps:\n      - run: pytest"),
    "always with needs.*": (
        "    if: \"always() && contains(needs.*.result, 'failure')\"",
        "    steps:\n      - run: pytest",
    ),
    "toJson lowercase": ("", "    steps:\n      - run: echo '${{ toJson(needs) }}'"),
    "toJSON with spaces": ("", "    steps:\n      - run: echo '${{ toJSON( needs ) }}'"),
    "needs index": ("", "    steps:\n      - run: echo \"${{ needs['lint'].result }}\""),
    "uppercase ref": ("", "    steps:\n      - run: echo '${{ NEEDS.Lint.outputs.x }}'"),
    "status function spacing": ("    if: SUCCESS ()", "    steps:\n      - run: pytest"),
}


@pytest.mark.parametrize("form", GATE_FORMS)
def test_parallelize_keeps_every_gate_form(form: str) -> None:
    job_if, steps = GATE_FORMS[form]
    p, wf = profiled(gated(job_if, steps), SPANS)
    assert detect_parallelize_dag(p, wf, PATH) == []


@pytest.mark.parametrize(
    "steps",
    [
        "    steps:\n      - run: echo done",
        "    steps:\n      - run: 'true'",
        "    steps:\n      - run: exit 0",
        "    steps:\n      - run: true",
        "    steps:\n      - run: Echo ok",
        "    steps:\n      - run: |\n          # wait\n          echo a\n\n          echo b",
        "    steps:\n      - name: nothing to run",
    ],
)
def test_parallelize_keeps_edges_into_wait_only_jobs(steps: str) -> None:
    p, wf = profiled(two_jobs(steps), SPANS)
    assert detect_parallelize_dag(p, wf, PATH) == []


def test_parallelize_still_flags_jobs_that_do_work_beyond_echo() -> None:
    p, wf = profiled(two_jobs("    steps:\n      - run: echo hi\n      - run: pytest"), SPANS)
    assert [f.job for f in detect_parallelize_dag(p, wf, PATH)] == ["test"]


VITE_GATE = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
  test-passed:
    if: (!cancelled() && !failure())
    needs: test
    runs-on: ubuntu-slim
    name: Build & Test Passed or Skipped
    steps:
      - run: echo "Build & Test Passed or Skipped"
"""


def test_parallelize_vite_test_passed_gate_has_no_finding() -> None:
    p, wf = profiled(VITE_GATE, {"test": (0, 300), "Build & Test Passed or Skipped": (300, 305)})
    assert wf.jobs["test-passed"].needs_refs == {"test"}
    assert detect_parallelize_dag(p, wf, PATH) == []


def test_parallelize_keeps_fastapi_bracket_needs_reference() -> None:
    wf_yaml = """
name: CI
on: [push]
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      src: ${{ steps.filter.outputs.src }}
    steps:
      - run: echo filter
  test:
    needs: [changes]
    runs-on: ubuntu-latest
    steps:
      - run: pytest
        if: needs['changes'].outputs.src == 'true'
"""
    p, wf = profiled(wf_yaml, {"changes": (0, 60), "test": (60, 360)})
    assert wf.jobs["test"].needs_refs == {"changes"}
    assert detect_parallelize_dag(p, wf, PATH) == []


def test_parallelize_keeps_spaced_tojson_gate_need() -> None:
    wf_yaml = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
  coverage-combine:
    needs: [test]
    runs-on: ubuntu-latest
    steps:
      - run: coverage combine
  gate:
    needs: [coverage-combine]
    runs-on: ubuntu-latest
    steps:
      - run: echo '${{ toJSON( needs ) }}'
"""
    p, wf = profiled(wf_yaml, {"test": (0, 100), "coverage-combine": (100, 150), "gate": (150, 160)})
    assert wf.jobs["gate"].needs_refs == {"coverage-combine"}
    assert [f for f in detect_parallelize_dag(p, wf, PATH) if f.job == "gate"] == []


TRANSITIVE = """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/upload-artifact@v4
  lint:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: ruff check
  test:
    needs: {test_needs}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/download-artifact@v4
      - run: pytest
"""
TRANSITIVE_SPANS = {"build": (0, 100), "lint": (100, 110), "test": (110, 135)}


def test_parallelize_keeps_transitive_artifact_handoff() -> None:
    p, wf = profiled(TRANSITIVE.format(test_needs="lint"), TRANSITIVE_SPANS)
    assert detect_parallelize_dag(p, wf, PATH) == []  # cutting test<-lint or lint<-build strands test's artifact


def test_parallelize_allows_cut_when_uploader_stays_an_ancestor() -> None:
    p, wf = profiled(TRANSITIVE.format(test_needs="[lint, build]"), TRANSITIVE_SPANS)
    by_edge = {(f.job, f.evidence["removed_need"]): f for f in detect_parallelize_dag(p, wf, PATH)}
    assert ("test", "lint") in by_edge  # build is still a direct need of test
    assert ("test", "build") not in by_edge  # the direct upload/download edge stays
    assert by_edge["test", "lint"].expected_savings_seconds == 10.0


def test_parallelize_logging_prefix_does_not_hide_real_work() -> None:
    steps = "    steps:\n      - run: echo Starting tests && npm test"
    p, wf = profiled(two_jobs(steps), SPANS)
    assert [(f.job, f.expected_savings_seconds) for f in detect_parallelize_dag(p, wf, PATH)] == [("test", 60.0)]


@pytest.mark.parametrize(
    ("script", "trivial"),
    [
        ("echo a; echo b", True),
        ("echo a && true", True),
        ("echo a | true", True),
        ("echo a; npm test", False),
        ("true || npm test", False),
        ("echo a | npm test", False),
        ("echo $(make build)", False),
        ("echo `make build`", False),
        ("echo done;", True),
        ('echo "All checks passed; merge allowed"', True),
        ("echo 'tests && lint ok'", True),
        ('echo "::notice::All jobs passed; ready"', True),
        ("echo ok >&2", True),
        ("echo ok 2>&1", True),
        ("echo ok &> /dev/null", True),
        ('echo "a\necho done" ; curl x', False),  # a quote spanning lines must not hide the command after it
        ("echo a # don't\ncurl x", False),  # an apostrophe in a comment opens no quote
        ("echo ok & curl x", False),
        ("cat <<EOF\nit's\nEOF\ncurl x", False),
        ("echo <(curl x)", False),
        ("echo Starting tests && npm test", False),
        ("echo $VAR", True),
        ("echo ok # ; npm test", True),
        ('echo "a" ; npm test', False),
        ("echo \\>& curl x", False),  # an escaped `>` is a literal: the `&` after it ends the command
        ("echo \\<& curl x", False),
        ("echo a\r# ; curl x", False),  # bash treats only space and tab as blanks, so this `#` is no comment
        ("echo a\f# ; curl x", False),
        ("echo a\v# ; curl x", False),
        ("echo a\t# ; curl x", True),
        ("echo $'\\'' ; curl x ; echo 'z'", False),  # ANSI-C quoting: a backslash escapes the quote
        ("echo $'a' ; curl x", False),
    ],
)
def test_trivial_run_checks_every_segment(script: str, *, trivial: bool) -> None:
    assert _trivial_run(script) is trivial


def test_parallelize_keeps_gate_that_echoes_a_semicolon_message() -> None:
    steps = '    steps:\n      - run: echo "All checks passed; merge allowed"'
    wf_yaml = f"""
jobs:
  a:
    runs-on: ubuntu-latest
  b:
    runs-on: ubuntu-latest
  gate:
    needs: [a, b]
    runs-on: ubuntu-latest
{steps}
"""
    p, wf = profiled(wf_yaml, {"a": (0, 50), "b": (0, 100), "gate": (100, 125)})
    assert detect_parallelize_dag(p, wf, PATH) == []


@pytest.mark.parametrize(
    "script",
    [
        "echo \\>& curl x",
        "echo a\r# ; curl x",
        "echo a\f# ; curl x",
        "echo a\v# ; curl x",
        "echo $'\\'' ; curl x ; echo 'z'",
    ],
)
def test_parallelize_does_not_treat_tokenizer_edge_cases_as_wait_only(script: str) -> None:
    steps = f"    steps:\n      - run: {json.dumps(script)}"
    p, wf = profiled(two_jobs(steps), SPANS)
    assert [f.job for f in detect_parallelize_dag(p, wf, PATH)] == ["test"]

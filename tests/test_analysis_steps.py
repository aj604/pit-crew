import gzip
import json
import time
from pathlib import Path
from typing import Any

import pytest

from pit_crew.lanes.analysis.critical_path import profile
from pit_crew.lanes.analysis.detectors.base import Finding, job_profile, keep_worthwhile, step_profile
from pit_crew.lanes.analysis.history import parse_runs
from pit_crew.lanes.analysis.workflow import match_step_name, parse_workflow

FIXTURES = Path(__file__).parent / "fixtures"


def make_finding(**overrides: Any) -> Finding:  # noqa: ANN401
    fields: dict[str, Any] = {
        "skill": "dep-cache",
        "workflow_path": ".github/workflows/ci.yml",
        "job": "test",
        "step": "Install",
        "bucket": "setup",
        "description": "d",
        "expected_savings_seconds": 30.0,
        "evidence": {},
        "confidence": 0.5,
        "risk": "low",
    }
    return Finding(**{**fields, **overrides})


def test_step_display_names_follow_github_defaults() -> None:
    wf = parse_workflow("""
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
      - name: Install
        run: npm ci
      - run: |
          npm test -- --ci
          echo done
""")
    assert [s.name for s in wf.jobs["test"].steps] == ["Run actions/checkout@v4", "Install", "Run npm test -- --ci"]
    assert wf.jobs["test"].steps[1].run == "npm ci"


def test_step_with_and_if_are_stringified() -> None:
    wf = parse_workflow("""
jobs:
  t:
    runs-on: [self-hosted, linux]
    steps:
      - uses: actions/setup-python@v5
        with: {python-version: 3.12, cache: pip}
        if: github.event_name == 'push'
      - run:
""")
    first, second = wf.jobs["t"].steps
    assert first.with_ == {"python-version": "3.12", "cache": "pip"}
    assert first.if_ == "github.event_name == 'push'"
    assert second.name == "Run"
    assert second.run is None
    assert wf.jobs["t"].runs_on == "self-hosted, linux"


def test_reusable_job_has_no_steps() -> None:
    wf = parse_workflow("jobs:\n  release:\n    uses: ./.github/workflows/r.yml\n")
    assert wf.jobs["release"].steps == ()


def test_matrix_keys_and_needs_refs() -> None:
    wf = parse_workflow("""
jobs:
  a: {runs-on: ubuntu-latest}
  b:
    needs: a
    runs-on: ubuntu-latest
    strategy: {matrix: {os: [x], shard: [1, 2], include: [{os: y}]}}
    steps: [{run: "echo ${{ needs.a.outputs.v }}"}]
  gate:
    needs: [a, b]
    steps: [{run: "echo '${{ toJSON(needs) }}'"}]
""")
    assert wf.jobs["b"].matrix_keys == ("os", "shard")
    assert wf.jobs["b"].needs_refs == frozenset({"a"})
    assert wf.jobs["gate"].needs_refs == frozenset({"a", "b"})
    assert wf.jobs["a"].runs_on == "ubuntu-latest"


def test_expression_matrix_has_no_keys() -> None:
    wf = parse_workflow("jobs:\n  a:\n    strategy: {matrix: '${{ fromJSON(x) }}'}\n")
    assert wf.jobs["a"].matrix_keys == ()


def test_step_names_match_with_expressions_and_shas() -> None:
    assert match_step_name("Test", "Test")
    assert match_step_name("Run tests on ${{ matrix.os }}", "Run tests on macos-latest")
    assert match_step_name(
        'Run uv run coverage html --title "Coverage for ${{ github.sha }}"',
        'Run uv run coverage html --title "Coverage for <sha>"',
    )
    assert not match_step_name("Install", "Install Dependencies")


def test_findings_below_minimum_are_dropped() -> None:
    small = make_finding(expected_savings_seconds=9.9)
    big = make_finding(expected_savings_seconds=50.0)
    medium = make_finding(expected_savings_seconds=20.0)
    assert keep_worthwhile([small, medium, big]) == [big, medium]


def test_finding_rejects_unknown_skill() -> None:
    with pytest.raises(ValueError, match="skill"):
        make_finding(skill="flake-quarantine")


def test_finding_rejects_unknown_bucket_and_risk() -> None:
    with pytest.raises(ValueError, match="bucket"):
        make_finding(bucket="idle")
    with pytest.raises(ValueError, match="risk"):
        make_finding(risk="extreme")


def test_finding_key_and_to_dict() -> None:
    f = make_finding(evidence={"median_seconds": 12.0})
    assert f.key == ("dep-cache", ".github/workflows/ci.yml", "test", "Install")
    assert f.to_dict()["evidence"] == {"median_seconds": 12.0}
    json.dumps(f.to_dict())


def test_step_profile_maps_yaml_step_to_measured_step() -> None:
    wf = parse_workflow((FIXTURES / "fastapi" / "test.yml").read_text())
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as fh:
        runs = parse_runs(json.load(fh))
    p = profile(wf, runs)
    step = next(s for s in wf.jobs["test"].steps if s.name == "Install Dependencies")
    measured = step_profile(p, "test", step)
    assert measured is not None
    assert measured.samples > 0
    assert job_profile(p, "test") is not None
    assert job_profile(p, "nope") is None
    assert step_profile(p, "nope", step) is None


def test_sha_pinned_action_matches_folded_measured_name() -> None:
    sha = "a" * 40
    assert match_step_name(f"Run actions/cache@{sha}", "Run actions/cache@<sha>")


def test_wildcard_matching_is_linear() -> None:
    name = "a".join(["${{ x }}"] * 30) + "b"
    start = time.perf_counter()
    assert not match_step_name(name, "a" * 5000)
    assert time.perf_counter() - start < 1.0


def test_wildcards_need_at_least_one_character() -> None:
    assert match_step_name("a ${{ x }} b", "a z b")
    assert not match_step_name("a${{ x }}b", "ab")
    assert not match_step_name("${{ x }}-${{ y }}", "-")


def test_non_mapping_with_is_ignored() -> None:
    wf = parse_workflow(
        "jobs:\n  t:\n    steps:\n      - {uses: a/b@v1, with: oops}\n      - {uses: a/c@v1, with: [x]}\n"
    )
    assert [s.with_ for s in wf.jobs["t"].steps] == [{}, {}]

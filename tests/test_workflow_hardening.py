"""Hostile-YAML bounds in the workflow parser: alias bombs through every container type, and `needs` reads."""

import contextlib
import time
import tracemalloc
from pathlib import Path

import pytest

from pit_crew.lanes.analysis import workflow as workflow_module
from pit_crew.lanes.analysis.workflow import MATRIX_TOO_LARGE, Workflow, parse_workflow

_DEPTH = 8


@pytest.fixture
def inner_bounds_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the whole-document bound so a test reaches the per-field bounds behind it."""
    monkeypatch.setattr(workflow_module, "_require_bounded_document", lambda _doc: None)


def _anchors(depth: int = _DEPTH) -> str:
    lines = ["  a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    lines += [f"  a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, depth)]
    return "x-anchors:\n" + "\n".join(lines) + "\n"


_REF = f"*a{_DEPTH - 1}"
_WRAPPERS = {
    "pairs": f"!!pairs [ {{k: {_REF}}} ]",
    "omap": f"!!omap [ {{k: {_REF}}} ]",
}


@pytest.mark.usefixtures("inner_bounds_only")
@pytest.mark.parametrize("kind", ["pairs", "omap"])
def test_tuple_bomb_in_matrix_is_bounded(kind: str) -> None:
    doc = (
        _anchors()
        + f"jobs:\n  a:\n    strategy:\n      matrix:\n        p: {_WRAPPERS[kind]}\n    steps: [{{run: pytest}}]\n"
    )
    start = time.perf_counter()
    wf = parse_workflow(doc)
    assert time.perf_counter() - start < 0.5
    assert wf.jobs["a"].matrix_definition == MATRIX_TOO_LARGE


@pytest.mark.usefixtures("inner_bounds_only")
@pytest.mark.parametrize("kind", ["pairs", "omap"])
def test_tuple_bomb_in_job_body_reads_every_need(kind: str) -> None:
    doc = (
        _anchors()
        + f"jobs:\n  a:\n    env: {{ X: {_WRAPPERS[kind]} }}\n    needs: [b, c]\n    steps: [{{run: pytest}}]\n"
        + "  b:\n    steps: [{run: x}]\n  c:\n    steps: [{run: y}]\n"
    )
    start = time.perf_counter()
    wf = parse_workflow(doc)
    assert time.perf_counter() - start < 0.5
    assert wf.jobs["a"].needs_refs == frozenset({"b", "c"})


def test_set_in_matrix_and_body_is_walked_not_stringified() -> None:
    doc = (
        "jobs:\n  a:\n    strategy:\n      matrix:\n        s: !!set {a, b}\n"
        "    env: {S: !!set {a, b}}\n    needs: [b]\n    steps: [{run: x}]\n  b:\n    steps: [{run: y}]\n"
    )
    start = time.perf_counter()
    wf = parse_workflow(doc)
    assert time.perf_counter() - start < 0.5
    assert wf.jobs["a"].needs_refs == frozenset()


@pytest.mark.usefixtures("inner_bounds_only")
def test_runs_on_list_bomb_is_bounded() -> None:
    doc = (
        _anchors() + f"jobs:\n  a:\n    runs-on: [ {_REF}, !!pairs [ {{k: {_REF}}} ] ]\n    steps: [{{run: pytest}}]\n"
    )
    start = time.perf_counter()
    wf = parse_workflow(doc)
    assert time.perf_counter() - start < 0.5
    assert wf.jobs["a"].runs_on == ""


@pytest.mark.usefixtures("inner_bounds_only")
def test_workflow_name_container_is_not_stringified() -> None:
    doc = _anchors() + f"name: !!pairs [ {{k: {_REF}}} ]\njobs:\n  a:\n    steps: [{{run: x}}]\n"
    start = time.perf_counter()
    wf = parse_workflow(doc)
    assert time.perf_counter() - start < 0.5
    assert wf.name == ""


_BIG = "A" * 20_000
_REFS = ", ".join(["*s"] * 5_000)
_PEAK_BYTES = 50 * 1024 * 1024


def _parse_bounded(doc: str) -> Workflow:
    """Parse within 0.5 s, then again under tracemalloc (which slows it) for a 50 MB peak."""
    start = time.perf_counter()
    try:
        wf = parse_workflow(doc)
    finally:
        elapsed = time.perf_counter() - start
    assert elapsed < 0.5
    return wf


def _peak_bytes(doc: str) -> int:
    tracemalloc.start()
    try:
        with contextlib.suppress(ValueError):
            parse_workflow(doc)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _refused(doc: str) -> None:
    with pytest.raises(ValueError, match="expands beyond the analysis limit"):
        _parse_bounded(doc)
    assert _peak_bytes(doc) < _PEAK_BYTES


def test_runs_on_alias_bomb_is_refused() -> None:
    _refused(f'x: &s "{_BIG}"\njobs:\n  a:\n    runs-on: [{_REFS}]\n    steps: [{{run: x}}]\n')


def test_steps_uses_alias_bomb_is_refused() -> None:
    refs = ", ".join(["*st"] * 5_000)
    _refused(f'x: &st {{uses: "{_BIG}"}}\njobs:\n  a:\n    runs-on: x\n    steps: [{refs}]\n')


def test_unknown_needs_alias_bomb_is_refused() -> None:
    _refused(f'x: &s "{_BIG}"\njobs:\n  a:\n    needs: [{_REFS}]\n    steps: [{{run: x}}]\n')


def test_many_jobs_aliasing_a_big_body_is_refused() -> None:
    env = ", ".join(f"k{i}: *s" for i in range(40))
    body = f"x: &b {{runs-on: x, env: {{{env}}}, steps: [{{run: x}}]}}\n"
    jobs = "".join(f"  j{i}: *b\n" for i in range(2_000))
    _refused(f'y: &s "{_BIG}"\n{body}jobs:\n{jobs}')


def test_derived_strings_are_capped_when_the_document_is_small() -> None:
    names = ", ".join(f"n{i}" for i in range(200))
    doc = (
        f"jobs:\n  a:\n    runs-on: [{', '.join(['x' * 500] * 100)}]\n"
        f"    steps: [{{name: {'y' * 5_000}}}, {{uses: {'z' * 5_000}}}]\n"
    )
    wf = _parse_bounded(doc)
    assert _peak_bytes(doc) < _PEAK_BYTES
    assert len(wf.jobs["a"].runs_on) <= 4096
    assert wf.jobs["a"].runs_on.count("x" * 500) < 100
    assert all(len(step.name) <= 1024 for step in wf.jobs["a"].steps)
    with pytest.raises(ValueError, match="unknown job") as err:
        parse_workflow(f"jobs:\n  a:\n    needs: [{names}]\n    steps: [{{run: x}}]\n")
    assert "n9" in str(err.value)
    assert "n10" not in str(err.value)
    assert len(str(err.value)) < 500


@pytest.mark.parametrize("fixture", ["fastapi/test.yml", "vite/ci.yml", "tokio/ci.yml"])
def test_real_workflows_still_parse(fixture: str) -> None:
    text = (Path(__file__).parent / "fixtures" / fixture).read_text()
    assert _parse_bounded(text).jobs

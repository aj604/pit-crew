"""Hostile-YAML bounds in the workflow parser: alias bombs through every container type, and `needs` reads."""

import time

import pytest

from pit_crew.lanes.analysis.workflow import MATRIX_TOO_LARGE, parse_workflow

_DEPTH = 8


def _anchors(depth: int = _DEPTH) -> str:
    lines = ["  a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    lines += [f"  a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, depth)]
    return "x-anchors:\n" + "\n".join(lines) + "\n"


_REF = f"*a{_DEPTH - 1}"
_WRAPPERS = {
    "pairs": f"!!pairs [ {{k: {_REF}}} ]",
    "omap": f"!!omap [ {{k: {_REF}}} ]",
}


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


def test_runs_on_list_bomb_is_bounded() -> None:
    doc = (
        _anchors() + f"jobs:\n  a:\n    runs-on: [ {_REF}, !!pairs [ {{k: {_REF}}} ] ]\n    steps: [{{run: pytest}}]\n"
    )
    start = time.perf_counter()
    wf = parse_workflow(doc)
    assert time.perf_counter() - start < 0.5
    assert wf.jobs["a"].runs_on == ""


def test_workflow_name_container_is_not_stringified() -> None:
    doc = _anchors() + f"name: !!pairs [ {{k: {_REF}}} ]\njobs:\n  a:\n    steps: [{{run: x}}]\n"
    start = time.perf_counter()
    wf = parse_workflow(doc)
    assert time.perf_counter() - start < 0.5
    assert wf.name == ""

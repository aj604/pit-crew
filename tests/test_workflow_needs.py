"""Which needs a job reads: whole-context uses of `needs` must read every need."""

import pytest

from pit_crew.lanes.analysis.workflow import parse_workflow


@pytest.mark.parametrize(
    "expression",
    [
        "format('{0}', needs)",
        "contains(needs, 1)",
        "needs [ 'changes' ]",
        "needs.*",
        "needs . *",
        "toJSON( needs )",
        "needs['x']",
        "NEEDS",
    ],
)
def test_whole_needs_context_reads_every_need(expression: str) -> None:
    doc = (
        f'jobs:\n  a:\n    needs: [changes, other]\n    steps: [{{run: "echo ${{{{ {expression} }}}}"}}]\n'
        "  changes:\n    steps: [{run: x}]\n  other:\n    steps: [{run: y}]\n"
    )
    assert parse_workflow(doc).jobs["a"].needs_refs == frozenset({"changes", "other"})


@pytest.mark.parametrize(
    "expression",
    ["needs.changes.outputs.x", "needs . changes", "needs .changes.outputs.x", "NEEDS.Changes"],
)
def test_named_need_reads_only_that_need(expression: str) -> None:
    doc = (
        f'jobs:\n  a:\n    needs: [changes, other]\n    steps: [{{run: "echo ${{{{ {expression} }}}}"}}]\n'
        "  changes:\n    steps: [{run: x}]\n  other:\n    steps: [{run: y}]\n"
    )
    assert parse_workflow(doc).jobs["a"].needs_refs == frozenset({"changes"})


def test_job_that_never_mentions_needs_reads_none() -> None:
    doc = "jobs:\n  a:\n    needs: [b]\n    steps: [{run: x}]\n  b:\n    steps: [{run: y}]\n"
    assert parse_workflow(doc).jobs["a"].needs_refs == frozenset()

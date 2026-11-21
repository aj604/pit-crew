import json
from pathlib import Path

import pytest

from pit_crew.evals import optimizer_guard as ev

TINY = "on: push\njobs:\n  t:\n    runs-on: x\n    steps:\n      - run: echo hi\n"


@pytest.fixture(scope="module")
def fixture_judgements() -> list[ev.Judgement]:
    edits = [e for path in ev.FIXTURES for e in ev.edits(path, Path(path).read_text())]
    return ev.judge(edits)


def test_guard_makes_no_mistakes_on_the_fixture_workflows(fixture_judgements: list[ev.Judgement]) -> None:
    assert ev.mistakes(fixture_judgements) == []
    guard = [j for j in fixture_judgements if j.reviewer == "Pit Crew guard"]
    assert sum(j.weakens for j in guard) >= 40
    assert sum(not j.weakens for j in guard) >= 10


def test_every_operator_fits_at_least_one_fixture(fixture_judgements: list[ev.Judgement]) -> None:
    used = {j.operator for j in fixture_judgements}
    assert {op.__name__ for op in (*ev.WEAKENING, *ev.SAFE)} <= used


def test_baselines_are_weaker_than_the_guard(fixture_judgements: list[ev.Judgement]) -> None:
    table = ev.summary_table(fixture_judgements)
    assert "| accept-all (single model call) | 0/" in table
    for baseline in ("structural diff", "strict text diff"):
        assert ev.mistakes(fixture_judgements, baseline)


def test_load_names_the_on_key_and_dump_round_trips() -> None:
    workflow = ev.load(TINY)
    assert "on" in workflow
    assert ev.load(ev.dump(workflow)) == workflow
    with pytest.raises(TypeError):
        ev.load("- a list\n")


def test_operators_skip_workflows_they_do_not_fit() -> None:
    bare = "on: push\njobs:\n  t:\n    runs-on: x\n    timeout-minutes: 5.5\n"
    bare += "    steps:\n      - uses: actions/checkout@v4\n"
    always_fit = {ev.add_concurrency, ev.add_timeout}
    for operator in (*ev.WEAKENING, *ev.SAFE):
        if operator not in always_fit:
            assert operator(ev.load(bare)) is None, operator.__name__


def test_include_only_matrix_is_sharded_per_entry() -> None:
    text = (
        "on: push\njobs:\n  t:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
        "        include:\n          - os: a\n          - os: b\n    steps:\n      - run: pytest -q\n"
    )
    sharded = ev.shard_tests(ev.load(text))
    assert sharded is not None
    matrix = sharded["jobs"]["t"]["strategy"]["matrix"]
    assert "shard" not in matrix
    assert len(matrix["include"]) == 4
    assert ev.pit_crew_guard(ev.dump(ev.load(text)), ev.dump(sharded))


def test_mistakes_and_table_formatting() -> None:
    wrong = ev.Judgement("w.yml", "op", weakens=True, reviewer="Pit Crew guard", accepted=True)
    right = ev.Judgement("w.yml", "op2", weakens=False, reviewer="Pit Crew guard", accepted=True)
    assert ev.mistakes([wrong, right]) == ["- w.yml: `op` (weakens) was accepted"]
    assert "n/a" in ev.summary_table([])


def test_fetch_workflow_checks_the_spec_and_host() -> None:
    urls: list[str] = []

    def get(url: str) -> str:
        urls.append(url)
        return TINY

    assert ev.fetch_workflow("o/r:ci.yml", get) == ("o/r:ci.yml", TINY)
    assert urls == ["https://raw.githubusercontent.com/o/r/HEAD/.github/workflows/ci.yml"]
    for bad in ("o/r", "o/r:../x.yml", "https://evil/x.yml"):
        with pytest.raises(ValueError, match="expected owner/repo"):
            ev.fetch_workflow(bad, get)
    with pytest.raises(ValueError, match="refusing"):
        ev._http_get("https://example.com/x")  # noqa: SLF001


def test_live_rewrites_run_the_guard_on_each_answer() -> None:
    weakened = TINY.replace("run: echo hi", "run: echo hi || true")
    answers = iter([f"```yaml\n{TINY}```", weakened])
    rows = ev.live_rewrites("w", TINY, lambda _prompt: next(answers), samples=2)
    assert [r["accepted"] for r in rows] == [True, False]
    assert "ignores the exit code" in " ".join(rows[1]["problems"])


def test_main_writes_the_report_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    workflow = tmp_path / "ci.yml"
    workflow.write_text(ev.dump(ev.load(Path(ev.FIXTURES[2]).read_text())))
    out = tmp_path / "out.json"
    code = ev.main(["--workflow", str(workflow), "--live", "1", "--json", str(out)], ask=lambda _p: "jobs: [broken")
    assert code == 0
    report = capsys.readouterr().out
    assert "| Pit Crew guard |" in report
    assert "guard blocked 1/1" in report
    data = json.loads(out.read_text())
    assert data["judgements"]
    assert data["live"][0]["accepted"] is False

"""The benchmark target template: packaging, its own checks, and its calibrated CI workflow."""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from pit_crew.contracts import PLAYBOOK_SKILLS
from pit_crew.lanes.analysis.workflow import parse_workflow
from pit_crew.lanes.optimizer.validate import check_change

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "benchmarks" / "target_template"
WORKFLOW_TEXT = (TEMPLATE / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
TARGET_PATH = ".github/workflows/ci.yml"  # where the workflow lives once the template is a repository
CALIBRATION: dict[str, Any] = json.loads((TEMPLATE / "calibration.json").read_text(encoding="utf-8"))


def _workflow() -> dict[Any, Any]:
    doc = yaml.safe_load(WORKFLOW_TEXT)
    assert isinstance(doc, dict)
    return doc


def _without_line(text: str, line: str) -> str:
    assert line in text
    return text.replace(line, "", 1)


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(TEMPLATE)}
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=120)  # noqa: S603


def test_template_is_excluded_from_wheel(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    built = subprocess.run(  # noqa: S603
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(REPO)], capture_output=True, text=True, check=False
    )
    assert built.returncode == 0, built.stderr
    (wheel,) = tmp_path.glob("*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert any(name.startswith("pit_crew/") for name in names)
    assert not [name for name in names if "status_board" in name or "target_template" in name or "benchmarks" in name]


def test_template_fast_tests_pass_offline() -> None:
    result = _run([sys.executable, "-m", "pytest", "tests/unit", "-q", "-p", "no:cacheprovider"], TEMPLATE)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ui_check_passes_on_default_fixture(tmp_path: Path) -> None:
    result = _run([sys.executable, "-m", "status_board.ui_check", "--snapshot-dir", str(tmp_path)], TEMPLATE)
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_ui_check_failure_writes_the_snapshot_ci_uploads(tmp_path: Path) -> None:
    fixture = json.loads((TEMPLATE / "fixtures" / "board.json").read_text(encoding="utf-8"))
    fixture["expect"]["overall"] = "up"
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "board.json").write_text(json.dumps(fixture), encoding="utf-8")
    run_step = "python -m status_board.ui_check"
    result = _run(run_step.split(), tmp_path)  # the workflow's exact command, from a checkout root
    assert result.returncode == 1
    (upload,) = [s for s in _workflow()["jobs"]["ui-check"]["steps"] if "upload-artifact" in str(s.get("uses"))]
    assert upload["if"] == "failure()"
    snapshots = list((tmp_path / upload["with"]["path"]).glob("*.svg"))
    assert len(snapshots) == 1
    assert "banner shows 'degraded', expected 'up'" in snapshots[0].read_text(encoding="utf-8")
    assert any(s.get("run") == run_step for s in _workflow()["jobs"]["ui-check"]["steps"])


def test_workflow_triggers_and_checks() -> None:
    doc = _workflow()
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict)
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    parsed = parse_workflow(WORKFLOW_TEXT)
    assert {"lint", "typecheck", "unit-test", "ui-check"} <= set(parsed.jobs)
    commands = {job: [s.get("run") for s in body["steps"]] for job, body in doc["jobs"].items()}
    assert {"ruff check .", "ruff format --check ."} <= set(commands["lint"])
    assert "mypy" in commands["typecheck"]
    assert "python -m pytest tests/integration" in commands["unit-test"]


def test_optimizer_guard_accepts_the_baseline() -> None:
    new = check_change(TARGET_PATH, "", WORKFLOW_TEXT)
    assert new.ok, new.problems
    assert any("new workflow" in w for w in new.warnings)
    same = check_change(TARGET_PATH, WORKFLOW_TEXT, WORKFLOW_TEXT)
    assert same.ok, same.problems


def test_optimizer_guard_rejects_dropping_a_check() -> None:
    after = _without_line(WORKFLOW_TEXT, "      - name: Ruff format\n        run: ruff format --check .\n")
    assert not check_change(TARGET_PATH, WORKFLOW_TEXT, after).ok


def test_smoke_job_is_a_removable_duplicate() -> None:
    doc = _workflow()
    del doc["jobs"]["smoke-test"]
    report = check_change(TARGET_PATH, WORKFLOW_TEXT, yaml.safe_dump(doc, sort_keys=False))
    assert report.ok, report.problems


def _job_seconds(job: str, *, warm: bool = False, shards: int = 1) -> float:
    c = CALIBRATION
    e2e = c["e2e_seconds_per_case"] * len(c["e2e_cases"])
    integration = c["integration_seconds_per_case"] * len(c["integration_cases"])
    running = {
        "lint": c["check_seconds"]["lint"],
        "typecheck": c["check_seconds"]["typecheck"],
        "ui-check": c["check_seconds"]["ui-check"],
        "smoke-test": e2e,
        "unit-test": c["check_seconds"]["unit"] + e2e + integration / shards,
    }[job]
    install = c["warm_install_seconds"] if warm else c["cold_install_seconds"]
    return float(c["job_overhead_seconds"] + c["checkout_toolchain_seconds"] + install + running)


def _critical_path(needs: dict[str, tuple[str, ...]], seconds: dict[str, float]) -> float:
    finish: dict[str, float] = {}

    def done(job: str) -> float:
        if job not in finish:
            finish[job] = max((done(n) for n in needs[job]), default=0.0) + seconds[job]
        return finish[job]

    return max(done(job) for job in needs)


def test_calibrated_bottlenecks_fall_in_their_ranges() -> None:
    jobs = parse_workflow(WORKFLOW_TEXT).jobs
    needs = {job_id: job.needs for job_id, job in jobs.items()}
    seconds = {job: _job_seconds(job) for job in jobs}
    baseline = _critical_path(needs, seconds)
    assert 125 <= baseline <= 155

    savings = {
        "dep-cache": sum(seconds[job] - _job_seconds(job, warm=True) for job in jobs),
        "parallelize-dag": baseline - _critical_path(dict.fromkeys(jobs, ()), seconds),
        # the duplicate's running bucket: its setup is already counted under dep-cache
        "dedupe-jobs": CALIBRATION["e2e_seconds_per_case"] * len(CALIBRATION["e2e_cases"]),
        "shard-by-timing": seconds["unit-test"] - _job_seconds("unit-test", shards=2),
    }
    ranges = {"dep-cache": (20, 30), "parallelize-dag": (35, 45), "dedupe-jobs": (20, 30), "shard-by-timing": (30, 40)}
    assert set(savings) <= set(PLAYBOOK_SKILLS)
    for skill, (low, high) in ranges.items():
        assert low <= savings[skill] <= high, (skill, savings[skill])

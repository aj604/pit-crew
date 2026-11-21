import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from status_board.ui_check import DEFAULT_FIXTURE, SNAPSHOT_NAME, main

ROOT = Path(__file__).resolve().parents[2]


def test_default_fixture_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--fixture", str(ROOT / DEFAULT_FIXTURE), "--snapshot-dir", str(tmp_path)]) == 0
    assert "passed" in capsys.readouterr().out
    assert not (tmp_path / SNAPSHOT_NAME).exists()


def _fixture(tmp_path: Path, data: Mapping[str, object]) -> str:
    path = tmp_path / "board.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_failure_writes_snapshot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = json.loads((ROOT / DEFAULT_FIXTURE).read_text(encoding="utf-8"))
    data["expect"] = {"cards": 5, "overall": "up"}
    snapshots = tmp_path / "snaps"
    assert main(["--fixture", _fixture(tmp_path, data), "--snapshot-dir", str(snapshots)]) == 1
    svg = (snapshots / SNAPSHOT_NAME).read_text(encoding="utf-8")
    assert svg.startswith("<svg")
    assert "4 cards rendered, expected 5" in svg
    assert "api: degraded" in svg
    assert "banner shows" in capsys.readouterr().err


def test_malformed_service_is_reported(tmp_path: Path) -> None:
    data = {"title": "T", "services": [{"name": "db", "status": "sideways"}], "expect": {"cards": 0, "overall": "up"}}
    assert main(["--fixture", _fixture(tmp_path, data), "--snapshot-dir", str(tmp_path)]) == 1
    assert "unknown status" in (tmp_path / SNAPSHOT_NAME).read_text(encoding="utf-8")

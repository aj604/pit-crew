import io
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from pit_crew.lanes.proof.checks import parse_workflow
from pit_crew.lanes.proof.replay_workflow import (
    AGG_VERSION,
    RECORD_SCRIPT_PATH,
    REPLAY_ARTIFACT_PREFIX,
    REPLAY_RECORD_ENV,
    UPLOAD_ARTIFACT_ACTION,
    ReplayArtifactError,
    build_replay_overlay,
    extract_replay_video,
    instrument_workflow,
    record_script_source,
)

SAMPLE_WORKFLOW = """\
name: CI
on:
  push:
  pull_request:
env:
  EXISTING: keep-me
jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Ruff
        run: ruff check .
  test:
    name: Unit Tests
    runs-on: ubuntu-latest
    steps:
      - name: Pre-checkout echo
        run: echo before-checkout
      - uses: actions/checkout@v4
      - name: Run pytest
        working-directory: packages/app
        env:
          PYTEST_ADDOPTS: -q
        run: |
          pytest tests/
      - name: Python check
        shell: python
        run: |
          assert 1 + 1 == 2
  win:
    name: Windows Tests
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest
  caller:
    uses: ./.github/workflows/reusable.yml
  no_checkout:
    runs-on: ubuntu-latest
    steps:
      - run: echo no checkout here
"""


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_record_script_source_matches_stdlib_record_module() -> None:
    source = record_script_source()
    assert "def record(" in source
    assert "--video-on-failure" in source
    assert "import pit_crew" not in source
    assert "import yaml" not in source


def test_instrument_workflow_wraps_posix_post_checkout_steps_and_preserves_yaml_triggers() -> None:
    instrumented = instrument_workflow(SAMPLE_WORKFLOW)
    parsed_raw = yaml.safe_load(instrumented)
    assert isinstance(parsed_raw, dict)
    # YAML 1.1 loads unquoted `on` as True, quoted `'on'` as "on"; parse_workflow normalizes both.
    assert "on" in parsed_raw or True in parsed_raw
    parsed_wf = parse_workflow(instrumented, ".github/workflows/ci.yml")
    assert parsed_wf is not None
    assert "on" in parsed_wf.header

    assert parsed_raw["env"] == {"EXISTING": "keep-me", REPLAY_RECORD_ENV: "1"}

    jobs = parsed_raw["jobs"]
    test_steps = jobs["test"]["steps"]
    # Step 0 (pre-checkout) stays untouched.
    assert test_steps[0]["run"] == "echo before-checkout"
    # Step 1 is checkout, Step 2 is tool installer.
    assert test_steps[1]["uses"] == "actions/checkout@v4"
    install_step = test_steps[2]
    assert install_step["continue-on-error"] is True
    assert "ffmpeg" in install_step["run"]
    assert f"agg/releases/download/{AGG_VERSION}/agg-" in install_step["run"]

    # Step 3 is wrapped pytest step preserving working-directory and env.
    pytest_step = test_steps[3]
    assert pytest_step["working-directory"] == "packages/app"
    assert pytest_step["env"] == {"PYTEST_ADDOPTS": "-q"}
    assert pytest_step["shell"] == "bash"
    assert f"$GITHUB_WORKSPACE/{RECORD_SCRIPT_PATH}" in pytest_step["run"]
    assert "--video-on-failure -- bash --noprofile --norc -eo pipefail" in pytest_step["run"]
    assert "pytest tests/" in pytest_step["run"]

    # Step 4 is wrapped shell: python step executing python3 inside record.py via bash wrapper.
    py_step = test_steps[4]
    assert py_step["shell"] == "bash"
    assert "--video-on-failure -- python3" in py_step["run"]
    assert "assert 1 + 1 == 2" in py_step["run"]

    # Step 5 is if: failure() artifact upload.
    upload_step = test_steps[5]
    assert upload_step["if"] == "failure()"
    assert upload_step["uses"] == UPLOAD_ARTIFACT_ACTION
    assert upload_step["with"]["name"] == f"{REPLAY_ARTIFACT_PREFIX}test-${{{{ strategy.job-index }}}}"
    assert upload_step["with"]["if-no-files-found"] == "ignore"

    # Windows, reusable caller, and no-checkout jobs stay untouched.
    assert jobs["win"]["steps"][1]["run"] == "pytest"
    assert jobs["caller"] == {"uses": "./.github/workflows/reusable.yml"}
    assert jobs["no_checkout"]["steps"] == [{"run": "echo no checkout here"}]


def test_instrument_workflow_targets_matching_checks_and_falls_back_when_unmatched() -> None:
    targeted = yaml.safe_load(instrument_workflow(SAMPLE_WORKFLOW, target_checks=("CI/Unit Tests",)))
    assert any(s.get("uses") == UPLOAD_ARTIFACT_ACTION for s in targeted["jobs"]["test"]["steps"])
    assert not any(s.get("uses") == UPLOAD_ARTIFACT_ACTION for s in targeted["jobs"]["lint"]["steps"])

    # Match by raw job id and by display name.
    by_id = yaml.safe_load(instrument_workflow(SAMPLE_WORKFLOW, target_checks=("lint",)))
    assert any(s.get("uses") == UPLOAD_ARTIFACT_ACTION for s in by_id["jobs"]["lint"]["steps"])
    assert not any(s.get("uses") == UPLOAD_ARTIFACT_ACTION for s in by_id["jobs"]["test"]["steps"])

    by_name = yaml.safe_load(instrument_workflow(SAMPLE_WORKFLOW, target_checks=("Lint",)))
    assert any(s.get("uses") == UPLOAD_ARTIFACT_ACTION for s in by_name["jobs"]["lint"]["steps"])

    # Unmatched target falls back to all eligible POSIX jobs.
    fallback = yaml.safe_load(instrument_workflow(SAMPLE_WORKFLOW, target_checks=("CI/DoesNotExist",)))
    assert any(s.get("uses") == UPLOAD_ARTIFACT_ACTION for s in fallback["jobs"]["lint"]["steps"])
    assert any(s.get("uses") == UPLOAD_ARTIFACT_ACTION for s in fallback["jobs"]["test"]["steps"])

    # Blank target_checks entries behave like no target filter.
    blank_targets = yaml.safe_load(instrument_workflow(SAMPLE_WORKFLOW, target_checks=("   ",)))
    assert any(s.get("uses") == UPLOAD_ARTIFACT_ACTION for s in blank_targets["jobs"]["lint"]["steps"])


def test_instrument_workflow_handles_shells_delimiter_collisions_and_invalid_inputs() -> None:
    special_wf = """\
name: Special
on: [push]
defaults:
  run:
    shell: sh
jobs:
  check:
    runs-on: [self-hosted, linux]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
      - run: echo "__PIT_CREW_STEP_EOF__"
      - shell: pwsh
        run: Write-Host hi
      - run: python3 "$GITHUB_WORKSPACE/.pit-crew/record.py" --cast a.cast -- true
  win_list:
    runs-on: [self-hosted, Windows]
    steps:
      - uses: actions/checkout@v4
      - run: pytest
"""
    instrumented = yaml.safe_load(instrument_workflow(special_wf))
    steps = instrumented["jobs"]["check"]["steps"]
    # Only one install step even with two checkout steps.
    install_steps = [s for s in steps if isinstance(s, dict) and "agg" in str(s.get("name", ""))]
    assert len(install_steps) == 1
    # Collision avoided by suffixed heredoc delimiter, and workflow default shell `sh` preserved.
    wrapped_echo = steps[3]["run"]
    assert "__PIT_CREW_STEP_EOF__" in wrapped_echo
    assert "__PIT_CREW_STEP_EOF_" in wrapped_echo
    assert "--video-on-failure -- sh -e" in wrapped_echo
    # pwsh step and already-wrapped step left unchanged.
    assert steps[4]["run"] == "Write-Host hi"
    assert steps[5]["run"] == 'python3 "$GITHUB_WORKSPACE/.pit-crew/record.py" --cast a.cast -- true'
    # Windows list runner left untouched.
    assert instrumented["jobs"]["win_list"]["steps"][1]["run"] == "pytest"

    # Unparseable YAML or non-workflow YAML returns input unchanged.
    assert instrument_workflow(":::not-yaml:::\n  [") == ":::not-yaml:::\n  ["
    assert instrument_workflow("- list\n- not\n- mapping\n") == "- list\n- not\n- mapping\n"


def test_instrument_workflow_executes_cleanly_in_real_bash_with_record_py(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runner_temp = tmp_path / "runner_temp"
    subdir = workspace / "sub"
    subdir.mkdir(parents=True)
    runner_temp.mkdir()

    overlay = build_replay_overlay(
        ".github/workflows/ci.yml",
        """\
name: CI
on: [push]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Passing step
        run: echo "pass-ok"
      - name: Failing bash step
        run: |
          echo "FAIL: expected 42 got 0"
          python3 -c "import sys; assert sys.stdin.read() == ''"
          exit 7
      - name: Failing python step
        shell: python
        run: |
          print("PY_BOOM")
          raise SystemExit(5)
""",
        {"src/app.py": "x = 1\n"},
    )
    assert overlay["src/app.py"] == "x = 1\n"
    script_file = workspace / RECORD_SCRIPT_PATH
    script_file.parent.mkdir(parents=True)
    script_file.write_text(overlay[RECORD_SCRIPT_PATH], encoding="utf-8")

    wf = yaml.safe_load(overlay[".github/workflows/ci.yml"])
    steps = wf["jobs"]["verify"]["steps"]
    env = {
        **os.environ,
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(runner_temp),
        # Ensure our venv python3 is first so python3 is guaranteed available.
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
    }

    bash_bin = shutil.which("bash") or "/bin/bash"
    # 1. Passing step exits 0, records .cast, does not fail or create .mp4.
    pass_proc = subprocess.run(  # noqa: S603  executes generated bash wrapper against temp workspace
        [bash_bin, "-c", steps[2]["run"]],
        cwd=subdir,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert pass_proc.returncode == 0
    assert "pass-ok" in pass_proc.stdout
    assert (runner_temp / "pit-crew-replay" / "verify" / "step-1.cast").exists()
    assert not (runner_temp / "pit-crew-replay" / "verify" / "step-1.mp4").exists()

    # 2. Failing bash step preserves exit 7, captures output in .cast, and keeps stdin EOF-clean.
    fail_proc = subprocess.run(  # noqa: S603  executes generated bash wrapper against temp workspace
        [bash_bin, "-c", steps[3]["run"]],
        cwd=subdir,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fail_proc.returncode == 7
    assert "FAIL: expected 42 got 0" in fail_proc.stdout
    cast_text = (runner_temp / "pit-crew-replay" / "verify" / "step-2.cast").read_text(encoding="utf-8")
    assert "FAIL: expected 42 got 0" in cast_text

    # 3. Failing python step preserves exit 5 and records output in .cast.
    py_proc = subprocess.run(  # noqa: S603  executes generated bash wrapper against temp workspace
        [bash_bin, "-c", steps[4]["run"]],
        cwd=subdir,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert py_proc.returncode == 5
    assert "PY_BOOM" in py_proc.stdout
    assert "PY_BOOM" in (runner_temp / "pit-crew-replay" / "verify" / "step-3.cast").read_text(encoding="utf-8")


def test_build_replay_overlay_handles_missing_workflow_yaml() -> None:
    overlay = build_replay_overlay(".github/workflows/ci.yml", None, {"src/a.py": "1\n"})
    assert set(overlay) == {"src/a.py", RECORD_SCRIPT_PATH}


def test_extract_replay_video_passthrough_and_valid_zip_selection() -> None:
    raw_mp4 = b"\x00\x00\x00\x18ftypmp42raw-bytes"
    assert extract_replay_video(raw_mp4) == raw_mp4

    archive = _make_zip(
        {
            "step-1.cast": b'{"version":2}\n',
            "step-1.mp4": b"earlier-video",
            "step-4.mp4": b"failing-step-video",
            "step-4.cast": b'{"version":2}\n',
        }
    )
    assert extract_replay_video(archive) == b"failing-step-video"


def test_extract_replay_video_rejects_missing_mp4_corrupt_and_unsafe_archives() -> None:
    cast_only = _make_zip({"step-2.cast": b'{"version":2}\n'})
    with pytest.raises(ReplayArtifactError, match=r"no \.mp4 video"):
        extract_replay_video(cast_only)

    with pytest.raises(ReplayArtifactError, match="corrupt replay artifact archive"):
        extract_replay_video(b"PK\x03\x04corrupt-truncated")

    for bad_name in ("../escape.mp4", "/abs/replay.mp4", r"dir\replay.mp4", "./dot.mp4"):
        with pytest.raises(ReplayArtifactError, match="unsafe path"):
            extract_replay_video(_make_zip({bad_name: b"mp4"}))

    # Symlink entry inside archive.
    sym_buf = io.BytesIO()
    with zipfile.ZipFile(sym_buf, "w") as zf:
        info = zipfile.ZipInfo("link.mp4")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ReplayArtifactError, match="symlink entry"):
        extract_replay_video(sym_buf.getvalue())

    # Too many entries.
    many = _make_zip({f"f-{i}.txt": b"x" for i in range(201)})
    with pytest.raises(ReplayArtifactError, match="201 entries"):
        extract_replay_video(many)

    # Zip bomb (high compression ratio).
    bomb = _make_zip({"step-1.mp4": b"A" * 200_000})
    with pytest.raises(ReplayArtifactError, match="compression ratio limit"):
        extract_replay_video(bomb)


def test_extract_replay_video_rejects_entry_and_total_size_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    from pit_crew.lanes.proof import replay_workflow  # noqa: PLC0415

    # Entry exceeding single-file cap (stored uncompressed so compression ratio stays 1:1).
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("step-1.mp4", b"x" * 100)
    monkeypatch.setattr(replay_workflow, "_MAX_ENTRY_BYTES", 50)
    with pytest.raises(ReplayArtifactError, match="exceeds 50 bytes"):
        extract_replay_video(buf.getvalue())

    # Cumulative entries exceeding total cap.
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("a.mp4", b"x" * 40)
        zf.writestr("b.mp4", b"y" * 40)
    monkeypatch.setattr(replay_workflow, "_MAX_ENTRY_BYTES", 50)
    monkeypatch.setattr(replay_workflow, "_MAX_TOTAL_BYTES", 60)
    with pytest.raises(ReplayArtifactError, match="total decompressed size exceeds 60 bytes"):
        extract_replay_video(buf2.getvalue())

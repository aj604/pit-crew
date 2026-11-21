import io
import json
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
    REPLAY_META_FILENAME,
    REPLAY_RECORD_ENV,
    UPLOAD_ARTIFACT_ACTION,
    ReplayArtifactError,
    build_replay_overlay,
    extract_replay_video,
    instrument_workflow,
    read_replay_metadata,
    record_script_source,
    resolve_workflow_check_aliases,
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

    # Step 3 is wrapped pytest step preserving working-directory, env, and implicit non-pipefail shell.
    pytest_step = test_steps[3]
    assert pytest_step["working-directory"] == "packages/app"
    assert pytest_step["env"] == {"PYTEST_ADDOPTS": "-q"}
    assert pytest_step["shell"] == "sh"
    assert f"$GITHUB_WORKSPACE/{RECORD_SCRIPT_PATH}" in pytest_step["run"]
    assert '--video-on-failure -- "$_pc_shell" -e' in pytest_step["run"]
    assert "pipefail" not in pytest_step["run"]
    assert "pytest tests/" in pytest_step["run"]

    # Step 4 is wrapped shell: python step executing python3 inside record.py.
    py_step = test_steps[4]
    assert py_step["shell"] == "sh"
    assert "--video-on-failure -- python3" in py_step["run"]
    assert "assert 1 + 1 == 2" in py_step["run"]

    # Step 5 records replay metadata and computes a check-scoped artifact name.
    meta_step = test_steps[5]
    assert meta_step["id"] == "pit_crew_replay_meta"
    assert meta_step["if"] == "failure()"
    assert meta_step["env"]["PIT_CREW_JOB_ID"] == "test"
    assert meta_step["env"]["PIT_CREW_JOB_NAME"] == "Unit Tests"

    # Step 6 is if: failure() artifact upload referencing metadata output with fallback.
    upload_step = test_steps[6]
    assert upload_step["if"] == "failure()"
    assert upload_step["uses"] == UPLOAD_ARTIFACT_ACTION
    assert "steps.pit_crew_replay_meta.outputs.artifact_name" in upload_step["with"]["name"]
    assert f"{REPLAY_ARTIFACT_PREFIX}test-" in upload_step["with"]["name"]
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


def test_instrument_workflow_preserves_yaml_12_plain_strings_and_real_booleans() -> None:
    wf = """\
name: Scalar Safety
on:
  push:
env:
  FEATURE_MODE: off
  ENABLE_CACHE: yes
  SKIP_FLAG: no
  TRIGGER_WORD: on
  SHORT_Y: y
  SHORT_N: n
  RELEASE_DATE: 2026-11-21
  FILE_MODE: 0755
  WINDOW_DURATION: 12:34
jobs:
  untouched_win:
    runs-on: windows-latest
    continue-on-error: true
    strategy:
      fail-fast: false
    env:
      LOCAL_OFF: off
      LOCAL_YES: yes
      LOCAL_NO: no
      LOCAL_ON: on
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
          sparse-checkout-cone-mode: on
      - run: pytest
  posix_job:
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4
      - env:
          MODE: off
        run: echo "$MODE"
"""
    instrumented = instrument_workflow(wf)
    # Must stay strings even when parsed with PyYAML's YAML 1.1 safe_load (quoted on dump).
    reparsed = yaml.safe_load(instrumented)
    assert reparsed["env"]["FEATURE_MODE"] == "off"
    assert isinstance(reparsed["env"]["FEATURE_MODE"], str)
    assert reparsed["env"]["ENABLE_CACHE"] == "yes"
    assert reparsed["env"]["SKIP_FLAG"] == "no"
    assert reparsed["env"]["TRIGGER_WORD"] == "on"
    assert reparsed["env"]["SHORT_Y"] == "y"
    assert reparsed["env"]["SHORT_N"] == "n"
    assert reparsed["env"]["RELEASE_DATE"] == "2026-11-21"
    assert reparsed["env"]["FILE_MODE"] == "0755"
    assert reparsed["env"]["WINDOW_DURATION"] == "12:34"

    win_job = reparsed["jobs"]["untouched_win"]
    assert win_job["continue-on-error"] is True
    assert win_job["strategy"]["fail-fast"] is False
    assert win_job["env"] == {"LOCAL_OFF": "off", "LOCAL_YES": "yes", "LOCAL_NO": "no", "LOCAL_ON": "on"}
    assert win_job["steps"][0]["with"]["persist-credentials"] is False
    assert win_job["steps"][0]["with"]["sparse-checkout-cone-mode"] == "on"
    assert reparsed["jobs"]["posix_job"]["continue-on-error"] is True
    assert reparsed["jobs"]["posix_job"]["steps"][2]["env"]["MODE"] == "off"


def test_instrument_workflow_preserves_windows_legs_in_mixed_matrix_and_skips_unresolvable() -> None:
    vite_yaml = (Path(__file__).resolve().parent / "fixtures" / "vite" / "ci.yml").read_text(encoding="utf-8")
    instrumented = instrument_workflow(
        vite_yaml,
        target_checks=("CI/Build&Test: node-26, ubuntu-latest",),
    )
    doc = yaml.safe_load(instrumented)

    # Targeting expanded matrix display name matches ONLY `test`, leaving `lint` untouched.
    lint_steps = doc["jobs"]["lint"]["steps"]
    assert not any(isinstance(s, dict) and s.get("uses") == UPLOAD_ARTIFACT_ACTION for s in lint_steps)

    test_steps = doc["jobs"]["test"]["steps"]
    install_steps = [s for s in test_steps if isinstance(s, dict) and "agg" in str(s.get("name", ""))]
    assert len(install_steps) == 1
    assert install_steps[0]["if"] == "runner.os != 'Windows'"

    # Windows-only PowerShell step is left completely untouched (never wrapped in bash/record.py).
    win_pw = [s for s in test_steps if isinstance(s, dict) and s.get("name", "").startswith("(windows)")]
    assert len(win_pw) == 1
    assert win_pw[0]["if"] == "runner.os == 'Windows'"
    assert "shell" not in win_pw[0]
    assert "$env:GITHUB_ENV" in win_pw[0]["run"]
    assert RECORD_SCRIPT_PATH not in win_pw[0]["run"]

    # Non-windows step is wrapped in place without adding a Windows duplicate.
    non_win_pw = [s for s in test_steps if isinstance(s, dict) and s.get("name", "").startswith("(non-windows)")]
    assert len(non_win_pw) == 1
    assert non_win_pw[0]["if"] == "runner.os != 'Windows'"
    assert RECORD_SCRIPT_PATH in non_win_pw[0]["run"]

    # Cross-platform steps get complementary POSIX wrapped + untouched Windows copies.
    unit_steps = [s for s in test_steps if isinstance(s, dict) and s.get("name") == "Test unit"]
    assert len(unit_steps) == 2
    assert unit_steps[0]["if"] == "runner.os != 'Windows'"
    assert RECORD_SCRIPT_PATH in unit_steps[0]["run"]
    assert unit_steps[1]["if"] == "runner.os == 'Windows'"
    assert unit_steps[1]["run"] == "pnpm run test-unit"
    assert "shell" not in unit_steps[1]

    # Existing conditional cross-platform step combines conditions cleanly.
    node20_steps = [s for s in test_steps if isinstance(s, dict) and "Set node version" in str(s.get("name", ""))]
    assert len(node20_steps) == 2
    assert node20_steps[0]["if"] == "(runner.os != 'Windows') && (matrix.node_version == '20')"
    assert node20_steps[1]["if"] == "(runner.os == 'Windows') && (matrix.node_version == '20')"

    # Metadata & upload steps only run on failing non-Windows legs.
    meta_step = test_steps[-2]
    upload_step = test_steps[-1]
    assert meta_step["if"] == "failure() && runner.os != 'Windows'"
    assert upload_step["if"] == "failure() && runner.os != 'Windows'"

    # Unresolvable runners, Windows-only matrices, pure-POSIX matrices, and id steps in mixed matrices.
    edge_wf = """\
name: Edge Runners
on: [push]
jobs:
  unresolvable_expr:
    runs-on: ${{ inputs.runner }}
    steps:
      - uses: actions/checkout@v4
      - run: pytest
  custom_label:
    runs-on: my-custom-runner
    steps:
      - uses: actions/checkout@v4
      - run: pytest
  dynamic_matrix:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix: ${{ fromJSON(needs.prep.outputs.matrix) }}
    steps:
      - uses: actions/checkout@v4
      - run: pytest
  windows_matrix:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [windows-latest, windows-2022]
    steps:
      - uses: actions/checkout@v4
      - run: pytest
  posix_matrix:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest]
        include:
          - os: macos-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest
  mixed_with_id:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
    steps:
      - uses: actions/checkout@v4
      - id: step_with_id
        run: echo "keep-unique-id"
      - run: pytest
"""
    edge_doc = yaml.safe_load(instrument_workflow(edge_wf))
    for untouched in ("unresolvable_expr", "custom_label", "dynamic_matrix", "windows_matrix"):
        assert edge_doc["jobs"][untouched]["steps"] == [{"uses": "actions/checkout@v4"}, {"run": "pytest"}]

    # Pure POSIX matrix wraps without duplicating steps or adding runner.os guards.
    posix_steps = edge_doc["jobs"]["posix_matrix"]["steps"]
    assert len([s for s in posix_steps if isinstance(s, dict) and RECORD_SCRIPT_PATH in str(s.get("run", ""))]) == 1
    assert "if" not in posix_steps[2]

    # Mixed matrix skips duplicating step with `id:` so no duplicate step id syntax error occurs.
    mixed_steps = edge_doc["jobs"]["mixed_with_id"]["steps"]
    id_steps = [s for s in mixed_steps if isinstance(s, dict) and s.get("id") == "step_with_id"]
    assert len(id_steps) == 1
    assert id_steps[0]["run"] == 'echo "keep-unique-id"'


def test_instrument_workflow_preserves_implicit_vs_explicit_pipefail_and_sh_fallback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    runner_temp = tmp_path / "runner_temp"
    workspace.mkdir()
    runner_temp.mkdir()

    wf = """\
name: Shell Semantics
on: [push]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Implicit default bash -e (no pipefail)
        run: |
          false | true
          echo "IMPLICIT_REACHED"
      - name: Explicit bash (with pipefail)
        shell: bash
        run: |
          false | true
          echo "EXPLICIT_REACHED"
      - name: Explicit sh (no pipefail)
        shell: sh
        run: |
          false | true
          echo "SH_REACHED"
"""
    overlay = build_replay_overlay(".github/workflows/ci.yml", wf, {})
    script_file = workspace / RECORD_SCRIPT_PATH
    script_file.parent.mkdir(parents=True)
    script_file.write_text(overlay[RECORD_SCRIPT_PATH], encoding="utf-8")

    steps = yaml.safe_load(overlay[".github/workflows/ci.yml"])["jobs"]["check"]["steps"]
    env = {
        **os.environ,
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(runner_temp),
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
    }
    sh_bin = shutil.which("sh") or "/bin/sh"
    bash_bin = shutil.which("bash") or "/bin/bash"

    # 1. Implicit shell: upstream pipeline fails, downstream succeeds -> continues to echo and exits 0.
    implicit_step = steps[2]
    assert implicit_step["shell"] == "sh"
    implicit_proc = subprocess.run(  # noqa: S603  executes generated wrapper
        [sh_bin, "-e", "-c", implicit_step["run"]],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert implicit_proc.returncode == 0
    assert "IMPLICIT_REACHED" in implicit_proc.stdout

    # 2. Explicit shell: bash: pipefail aborts at `false | true` before echo and exits 1.
    explicit_step = steps[3]
    assert explicit_step["shell"] == "bash"
    assert "--video-on-failure -- bash --noprofile --norc -eo pipefail" in explicit_step["run"]
    explicit_proc = subprocess.run(  # noqa: S603  executes generated wrapper
        [bash_bin, "--noprofile", "--norc", "-eo", "pipefail", "-c", explicit_step["run"]],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert explicit_proc.returncode == 1
    assert "EXPLICIT_REACHED" not in explicit_proc.stdout

    # 3. Explicit shell: sh: no pipefail, reaches echo and exits 0.
    sh_step = steps[4]
    assert sh_step["shell"] == "sh"
    sh_proc = subprocess.run(  # noqa: S603  executes generated wrapper
        [sh_bin, "-e", "-c", sh_step["run"]],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sh_proc.returncode == 0
    assert "SH_REACHED" in sh_proc.stdout

    # 4. Implicit default falls back to `sh -e` when `bash` is absent from PATH.
    no_bash_bin = tmp_path / "no_bash_bin"
    no_bash_bin.mkdir()
    for tool_name, target in (
        ("sh", sh_bin),
        ("mkdir", shutil.which("mkdir") or "/bin/mkdir"),
        ("mktemp", shutil.which("mktemp") or "/usr/bin/mktemp"),
        ("cat", shutil.which("cat") or "/bin/cat"),
        ("rm", shutil.which("rm") or "/bin/rm"),
        ("false", shutil.which("false") or "/usr/bin/false"),
        ("true", shutil.which("true") or "/usr/bin/true"),
        ("python3", sys.executable),
    ):
        (no_bash_bin / tool_name).symlink_to(target)
    fallback_proc = subprocess.run(  # noqa: S603  executes generated wrapper with no bash in PATH
        [sh_bin, "-e", "-c", implicit_step["run"]],
        cwd=workspace,
        env={**env, "PATH": str(no_bash_bin)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fallback_proc.returncode == 0
    assert "IMPLICIT_REACHED" in fallback_proc.stdout


def test_instrument_workflow_tracks_nested_checkout_and_skips_unreliable_checkouts(tmp_path: Path) -> None:
    nested_wf = """\
name: Nested Checkout
on: [push]
jobs:
  app_test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          repository: other/tools
          path: tools
      - run: echo before-replay-checkout
      - uses: actions/checkout@v4
        with:
          path: app
      - name: Run nested check
        working-directory: app
        run: |
          echo "NESTED_FAIL"
          exit 6
  space_path:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          path: my app
      - run: echo "SPACE_OK"
  dynamic_path:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          path: ${{ matrix.dir }}
      - run: pytest
  foreign_ref:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: main
      - run: pytest
  sparse:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          sparse-checkout: src/
      - run: pytest
  conditional_checkout:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        if: inputs.do_checkout
      - run: pytest
  clobbered:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          path: app
      - run: echo first-wrapped
      - uses: actions/checkout@v4
        with:
          repository: other/repo
          path: app
      - run: echo after-clobber-unwrapped
"""
    overlay = build_replay_overlay(".github/workflows/ci.yml", nested_wf, {})
    doc = yaml.safe_load(overlay[".github/workflows/ci.yml"])

    # Nested checkout: step before replay checkout stays unwrapped; post-replay step uses app/.pit-crew/record.py.
    app_steps = doc["jobs"]["app_test"]["steps"]
    assert app_steps[1]["run"] == "echo before-replay-checkout"
    wrapped_nested = app_steps[4]
    assert f'"$GITHUB_WORKSPACE/app/{RECORD_SCRIPT_PATH}"' in wrapped_nested["run"]

    # Execute generated nested wrapper against a workspace where record.py exists ONLY under app/.
    workspace = tmp_path / "workspace"
    runner_temp = tmp_path / "runner_temp"
    app_dir = workspace / "app"
    (app_dir / ".pit-crew").mkdir(parents=True)
    runner_temp.mkdir()
    (app_dir / RECORD_SCRIPT_PATH).write_text(overlay[RECORD_SCRIPT_PATH], encoding="utf-8")
    assert not (workspace / RECORD_SCRIPT_PATH).exists()

    sh_bin = shutil.which("sh") or "/bin/sh"
    env = {
        **os.environ,
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(runner_temp),
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
    }
    proc = subprocess.run(  # noqa: S603  executes generated wrapper for nested checkout
        [sh_bin, "-e", "-c", wrapped_nested["run"]],
        cwd=app_dir,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 6
    assert "NESTED_FAIL" in proc.stdout
    assert "NESTED_FAIL" in (runner_temp / "pit-crew-replay" / "app_test" / "step-3.cast").read_text(encoding="utf-8")

    # Space-containing checkout path is safely quoted and executes cleanly.
    space_dir = workspace / "my app"
    (space_dir / ".pit-crew").mkdir(parents=True)
    (space_dir / RECORD_SCRIPT_PATH).write_text(overlay[RECORD_SCRIPT_PATH], encoding="utf-8")
    space_step = doc["jobs"]["space_path"]["steps"][2]
    space_proc = subprocess.run(  # noqa: S603  executes generated wrapper for path with spaces
        [sh_bin, "-e", "-c", space_step["run"]],
        cwd=space_dir,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert space_proc.returncode == 0
    assert "SPACE_OK" in space_proc.stdout

    # Unreliable checkouts skip wrapping completely.
    for job_name in ("dynamic_path", "foreign_ref", "sparse", "conditional_checkout"):
        steps = doc["jobs"][job_name]["steps"]
        assert len(steps) == 2
        assert steps[1]["run"] == "pytest"

    # Non-replay checkout clobbering `app` disables wrapping for subsequent steps.
    clobbered_steps = doc["jobs"]["clobbered"]["steps"]
    assert RECORD_SCRIPT_PATH in clobbered_steps[2]["run"]
    assert clobbered_steps[4]["run"] == "echo after-clobber-unwrapped"


def test_metadata_step_writes_check_identity_and_extract_verifies_metadata(tmp_path: Path) -> None:
    vite_yaml = (Path(__file__).resolve().parent / "fixtures" / "vite" / "ci.yml").read_text(encoding="utf-8")
    instrumented = yaml.safe_load(
        instrument_workflow(vite_yaml, target_checks=("CI/Build&Test: node-26, ubuntu-latest",))
    )
    meta_step = instrumented["jobs"]["test"]["steps"][-2]

    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    gh_output = tmp_path / "gh_output.txt"
    sh_bin = shutil.which("sh") or "/bin/sh"
    env = {
        **os.environ,
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(gh_output),
        "PIT_CREW_WORKFLOW": "CI",
        "PIT_CREW_JOB_ID": "test",
        "PIT_CREW_JOB_NAME": "Build&Test: node-26, ubuntu-latest",
        "PIT_CREW_MATRIX_JSON": json.dumps({"os": "ubuntu-latest", "node_version": 26}),
        "PIT_CREW_JOB_INDEX": "3",
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
    }
    proc = subprocess.run(  # noqa: S603  executes generated metadata helper step
        [sh_bin, "-e", "-c", meta_step["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    meta_path = runner_temp / "pit-crew-replay" / "test" / REPLAY_META_FILENAME
    meta_bytes = meta_path.read_bytes()
    assert json.loads(meta_bytes.decode("utf-8")) == {
        "workflow": "CI",
        "job_id": "test",
        "job_name": "Build&Test: node-26, ubuntu-latest",
        "job_index": "3",
    }
    assert "artifact_name=pit-crew-replay-test-build-test-node-26-ubuntu-latest-3\n" in gh_output.read_text("utf-8")

    # extract_replay_video accepts matching check metadata and rejects mismatched job metadata.
    archive = _make_zip({REPLAY_META_FILENAME: meta_bytes, "step-2.mp4": b"node26-video"})
    assert read_replay_metadata(archive) == {
        "workflow": "CI",
        "job_id": "test",
        "job_name": "Build&Test: node-26, ubuntu-latest",
        "job_index": "3",
    }
    assert extract_replay_video(archive, expected_checks=("CI/Build&Test: node-26, ubuntu-latest",)) == b"node26-video"
    with pytest.raises(ReplayArtifactError, match="belongs to check"):
        extract_replay_video(archive, expected_checks=("CI/Build&Test: node-20, ubuntu-latest",))
    with pytest.raises(ReplayArtifactError, match="belongs to check"):
        extract_replay_video(archive, expected_checks=("CI/Lint: node-26, ubuntu-latest",))

    # resolve_workflow_check_aliases returns aliases for non-matrix jobs without widening specific matrix legs.
    assert set(resolve_workflow_check_aliases(SAMPLE_WORKFLOW, ".github/workflows/ci.yml", ("CI/Unit Tests",))) == {
        "test",
        "CI/test",
        "Unit Tests",
        "CI/Unit Tests",
    }
    assert (
        resolve_workflow_check_aliases(
            vite_yaml, ".github/workflows/ci.yml", ("CI/Build&Test: node-26, ubuntu-latest",)
        )
        == ()
    )


def test_secret_masking_skips_inline_secret_steps_and_scrubs_recorded_output(tmp_path: Path) -> None:
    wf_yaml = """\
name: CI
on: [push]
env:
  WORKFLOW_CRED: ${{ secrets.WORKFLOW_CRED }}
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      JOB_TOKEN: ${{ github.token }}
    steps:
      - uses: actions/checkout@v4
      - name: Inline secret step stays unwrapped
        run: echo "${{ secrets.INLINE_SECRET }}"
      - name: Masked test step
        env:
          STEP_KEY: ${{ secrets.STEP_KEY }}
        run: |
          echo "::add-mask::dyn-masked-secret-987"
          echo "wf=$WORKFLOW_CRED job=$JOB_TOKEN step=$STEP_KEY dyn=dyn-masked-secret-987"
          echo "raw_gh=ghp_1234567890abcdefghij1234567890"
          echo "child_var=${PIT_CREW_SECRET_ENV_VARS:-unset}"
          exit 2
"""
    overlay = build_replay_overlay(".github/workflows/ci.yml", wf_yaml, {})
    parsed = yaml.safe_load(overlay[".github/workflows/ci.yml"])
    steps = parsed["jobs"]["test"]["steps"]

    # Step 2 (inline ${{ secrets.INLINE_SECRET }}) is left completely unwrapped!
    inline_step = steps[2]
    assert inline_step["name"] == "Inline secret step stays unwrapped"
    assert inline_step["run"] == 'echo "${{ secrets.INLINE_SECRET }}"'
    assert RECORD_SCRIPT_PATH not in inline_step["run"]

    # Step 3 receives only env var NAMES (never secret values) via PIT_CREW_SECRET_ENV_VARS.
    masked_step = steps[3]
    assert masked_step["env"]["PIT_CREW_SECRET_ENV_VARS"] == "JOB_TOKEN,STEP_KEY,WORKFLOW_CRED"  # noqa: S105

    # Upload step publishes ONLY *.mp4 and replay-meta.json (never raw .cast files!).
    upload_step = steps[-1]
    assert upload_step["uses"] == UPLOAD_ARTIFACT_ACTION
    upload_path = str(upload_step["with"]["path"])
    assert "*.mp4" in upload_path
    assert REPLAY_META_FILENAME in upload_path
    assert ".cast" not in upload_path

    # Execute the wrapped step in a real shell + record.py and confirm secrets are scrubbed from .cast.
    workspace = tmp_path / "ws"
    runner_temp = tmp_path / "tmp"
    runner_temp.mkdir()
    script_file = workspace / RECORD_SCRIPT_PATH
    script_file.parent.mkdir(parents=True)
    script_file.write_text(overlay[RECORD_SCRIPT_PATH], encoding="utf-8")

    env = {
        **os.environ,
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(runner_temp),
        "WORKFLOW_CRED": "wf-secret-alpha-111",
        "JOB_TOKEN": "job-secret-beta-222",
        "STEP_KEY": "step-secret-gamma-333",
        "PIT_CREW_SECRET_ENV_VARS": masked_step["env"]["PIT_CREW_SECRET_ENV_VARS"],
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
    }
    sh_bin = shutil.which("sh") or "/bin/sh"
    proc = subprocess.run(  # noqa: S603  executes generated replay wrapper
        [sh_bin, "-e", "-c", masked_step["run"]],
        cwd=workspace,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    # Job log still receives raw ::add-mask:: so GitHub Actions masks its own log!
    assert "::add-mask::dyn-masked-secret-987" in proc.stdout
    # Child process does not see PIT_CREW_SECRET_ENV_VARS in its environment.
    assert "child_var=unset" in proc.stdout

    cast_text = (runner_temp / "pit-crew-replay" / "test" / "step-2.cast").read_text(encoding="utf-8")
    for secret in (
        "wf-secret-alpha-111",
        "job-secret-beta-222",
        "step-secret-gamma-333",
        "dyn-masked-secret-987",
        "ghp_1234567890abcdefghij1234567890",
    ):
        assert secret not in cast_text
    assert "wf=*** job=*** step=*** dyn=***" in cast_text
    assert "::add-mask::***" in cast_text
    assert "raw_gh=***" in cast_text


def test_cross_step_add_mask_and_step_secret_env_stay_masked_in_later_step(tmp_path: Path) -> None:
    wf_yaml = """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Step 1 exports mask and step secret
        env:
          EARLY_VALUE: ${{ secrets.EARLY_VALUE }}
        run: |
          echo "::add-mask::cross-step-masked-555"
          printf '%s' "$EARLY_VALUE" > secret_file.txt
      - name: Step 2 fails while printing earlier secrets
        run: |
          echo "dyn=cross-step-masked-555"
          echo "early=$(cat secret_file.txt)"
          exit 3
"""
    overlay = build_replay_overlay(".github/workflows/ci.yml", wf_yaml, {})
    parsed = yaml.safe_load(overlay[".github/workflows/ci.yml"])
    steps = parsed["jobs"]["test"]["steps"]
    step1, step2, meta_step, upload_step = steps[2], steps[3], steps[-2], steps[-1]

    # Step 2 inherits EARLY_VALUE in PIT_CREW_SECRET_ENV_VARS and passes --masks-file outside pit-crew-replay/.
    assert step2["env"]["PIT_CREW_SECRET_ENV_VARS"] == "EARLY_VALUE"  # noqa: S105
    assert "--masks-file" in step1["run"]
    assert "--masks-file" in step2["run"]
    assert "pit-crew-masks" not in str(upload_step["with"]["path"])

    workspace = tmp_path / "ws"
    runner_temp = tmp_path / "tmp"
    runner_temp.mkdir()
    script_file = workspace / RECORD_SCRIPT_PATH
    script_file.parent.mkdir(parents=True)
    script_file.write_text(overlay[RECORD_SCRIPT_PATH], encoding="utf-8")

    sh_bin = shutil.which("sh") or "/bin/sh"
    base_env = {
        **os.environ,
        "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TEMP": str(runner_temp),
        "PATH": f"{Path(sys.executable).parent}:{os.environ.get('PATH', '')}",
    }
    proc1 = subprocess.run(  # noqa: S603
        [sh_bin, "-e", "-c", step1["run"]],
        cwd=workspace,
        env={**base_env, "EARLY_VALUE": "early-step-secret-888", "PIT_CREW_SECRET_ENV_VARS": "EARLY_VALUE"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc1.returncode == 0
    masks_path = runner_temp / "pit-crew-masks" / "test.masks"
    assert masks_path.is_file()
    assert (masks_path.stat().st_mode & 0o777) == 0o600
    masks_text = masks_path.read_text(encoding="utf-8")
    assert "cross-step-masked-555" not in masks_text
    assert "early-step-secret-888" not in masks_text

    # Step 2 does NOT have EARLY_VALUE in its environment, yet still scrubs both secrets from its .cast.
    proc2 = subprocess.run(  # noqa: S603
        [sh_bin, "-e", "-c", step2["run"]],
        cwd=workspace,
        env={**base_env, "PIT_CREW_SECRET_ENV_VARS": "EARLY_VALUE"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == 3
    cast2 = (runner_temp / "pit-crew-replay" / "test" / "step-2.cast").read_text(encoding="utf-8")
    assert "cross-step-masked-555" not in cast2
    assert "early-step-secret-888" not in cast2
    assert "dyn=***" in cast2
    assert "early=***" in cast2

    # Metadata step cleans up the temporary masks file on failure.
    meta_proc = subprocess.run(  # noqa: S603
        [sh_bin, "-e", "-c", meta_step["run"]],
        cwd=workspace,
        env={**base_env, "PIT_CREW_WORKFLOW": "CI", "PIT_CREW_JOB_ID": "test"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert meta_proc.returncode == 0
    assert not masks_path.exists()

"""Shared Proof ↔ Dispatcher contract for recording replays on GitHub Actions runners.

Ephemeral replay branches (never timing runs or candidate PRs) carry three additions:

1. ``.pit-crew/record.py`` — the standalone stdlib recorder copied verbatim from
   ``pit_crew.lanes.proof.record``.
2. ``PIT_CREW_RECORD: "1"`` in workflow ``env:`` (enabling browser/E2E artifact capture
   alongside terminal recording) plus a best-effort step that ensures ``agg`` and
   ``ffmpeg`` are installed on the runner.
3. POSIX ``run:`` steps after ``actions/checkout`` wrapped with ``record.py
   --video-on-failure`` and an ``if: failure()`` ``actions/upload-artifact`` step uploading
   ``pit-crew-replay-<job>-<job-index>``.

If ``agg`` or ``ffmpeg`` is unavailable on the runner, the wrapped step still finishes with
its exact exit code, only ``.cast`` is uploaded, and ``extract_replay_video()`` raises
``ReplayArtifactError`` so ``verify_replays()`` leaves the replay ``"unverified"``
(fail-closed).
"""

import hashlib
import io
import re
import stat
import zipfile
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import yaml

from pit_crew.lanes.proof.checks import Workflow, parse_workflow

RECORD_SCRIPT_PATH = ".pit-crew/record.py"
REPLAY_RECORD_ENV = "PIT_CREW_RECORD"
REPLAY_ARTIFACT_PREFIX = "pit-crew-replay-"
REPLAY_OUTPUT_DIR = "${{ runner.temp }}/pit-crew-replay"
AGG_VERSION = "v1.5.0"
UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact@v4"

_MAX_ZIP_ENTRIES = 200
_MAX_ENTRY_BYTES = 50 * 1024 * 1024
_MAX_TOTAL_BYTES = 60 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 50
_ZIP_MAGIC = b"PK\x03\x04"

_SUPPORTED_SHELLS: dict[str, tuple[str, ...]] = {
    "": ("bash", "--noprofile", "--norc", "-eo", "pipefail"),
    "bash": ("bash", "--noprofile", "--norc", "-eo", "pipefail"),
    "sh": ("sh", "-e"),
    "python": ("python3",),
}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_STEP_INDEX_RE = re.compile(r"step-(\d+)\.mp4$", re.IGNORECASE)


class ReplayArtifactError(ValueError):
    """Raised when a replay recording artifact archive is unsafe or lacks an MP4 video."""

    @classmethod
    def missing_video(cls) -> "ReplayArtifactError":
        """Build error when a replay ZIP contains no MP4 recording."""
        return cls("replay artifact archive contains no .mp4 video (agg or ffmpeg may be missing on the runner)")

    @classmethod
    def too_many_entries(cls, count: int) -> "ReplayArtifactError":
        """Build error when a ZIP archive exceeds the entry safety cap."""
        return cls(f"replay artifact archive has {count} entries (limit {_MAX_ZIP_ENTRIES})")

    @classmethod
    def unsafe_path(cls, filename: str) -> "ReplayArtifactError":
        """Build error for zip-slip or invalid archive member paths."""
        return cls(f"replay artifact archive contains unsafe path {filename!r}")

    @classmethod
    def symlink_entry(cls, filename: str) -> "ReplayArtifactError":
        """Build error for symlink entries inside an artifact archive."""
        return cls(f"replay artifact archive contains symlink entry {filename!r}")

    @classmethod
    def corrupt_archive(cls, error: Exception) -> "ReplayArtifactError":
        """Build error when a ZIP payload cannot be opened."""
        return cls(f"corrupt replay artifact archive: {error}")

    @classmethod
    def entry_too_large(cls, filename: str) -> "ReplayArtifactError":
        """Build error when a single archive entry exceeds the size cap."""
        return cls(f"replay artifact archive rejected: {filename!r} exceeds {_MAX_ENTRY_BYTES} bytes")

    @classmethod
    def excessive_compression_ratio(cls, filename: str) -> "ReplayArtifactError":
        """Build error when a single archive entry exceeds the zip-bomb ratio cap."""
        return cls(f"replay artifact archive rejected: {filename!r} exceeds compression ratio limit")

    @classmethod
    def total_too_large(cls) -> "ReplayArtifactError":
        """Build error when cumulative decompressed bytes exceed the safety cap."""
        return cls(f"replay artifact archive rejected: total decompressed size exceeds {_MAX_TOTAL_BYTES} bytes")


def record_script_source() -> str:
    """Return the standalone stdlib ``record.py`` script committed onto replay branches."""
    return Path(__file__).with_name("record.py").read_text(encoding="utf-8")


def instrument_workflow(
    workflow_yaml: str,
    *,
    workflow_path: str = ".github/workflows/ci.yml",
    target_checks: Collection[str] | None = None,
    agg_version: str = AGG_VERSION,
) -> str:
    """Instrument a workflow YAML string to record failing checks on an ephemeral replay branch.

    Leaves unparseable YAML or reusable-workflow-only definitions unchanged so recording
    instrumentation never breaks a valid workflow.
    """
    parsed = parse_workflow(workflow_yaml, workflow_path)
    if parsed is None:
        return workflow_yaml

    raw_doc = yaml.safe_load(workflow_yaml)
    if not isinstance(raw_doc, dict):
        return workflow_yaml

    doc: dict[str, Any] = {("on" if key is True else str(key)): value for key, value in raw_doc.items()}
    _set_record_env(doc)

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return workflow_yaml

    workflow_shell = _default_shell(doc)
    selected_ids = _select_job_ids(parsed, jobs, target_checks)
    for job_id in selected_ids:
        job = jobs.get(job_id)
        if isinstance(job, dict):
            _instrument_job(job_id, job, workflow_shell=workflow_shell, agg_version=agg_version)

    return yaml.safe_dump(doc, sort_keys=False, width=1000)


def build_replay_overlay(
    workflow_path: str,
    workflow_yaml: str | None,
    base_files: Mapping[str, str],
    *,
    target_checks: Collection[str] | None = None,
    agg_version: str = AGG_VERSION,
) -> dict[str, str]:
    """Build the ephemeral replay branch file map containing ``record.py`` and instrumented CI."""
    overlay = dict(base_files)
    overlay[RECORD_SCRIPT_PATH] = record_script_source()
    if workflow_yaml is not None:
        overlay[workflow_path] = instrument_workflow(
            workflow_yaml,
            workflow_path=workflow_path,
            target_checks=target_checks,
            agg_version=agg_version,
        )
    return overlay


def extract_replay_video(data: bytes) -> bytes:
    """Return MP4 bytes from either a raw video payload or a GitHub Actions artifact ZIP archive.

    Non-ZIP payloads pass through untouched so callers with raw recordings continue working.
    ZIP payloads are validated against path traversal, symlinks, entry caps, and zip bombs;
    when no ``.mp4`` entry is present (for example when ``agg`` or ``ffmpeg`` was unavailable on
    the runner), ``ReplayArtifactError`` is raised so verification fails closed.
    """
    if not data.startswith(_ZIP_MAGIC):
        return data

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ReplayArtifactError.corrupt_archive(error) from error

    with archive:
        infos = archive.infolist()
        _validate_zip_members(infos)
        mp4_entries = [info for info in infos if not info.is_dir() and info.filename.lower().endswith(".mp4")]
        if not mp4_entries:
            raise ReplayArtifactError.missing_video()
        chosen = max(mp4_entries, key=_mp4_sort_key)
        return archive.read(chosen.filename)


def _validate_zip_members(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > _MAX_ZIP_ENTRIES:
        raise ReplayArtifactError.too_many_entries(len(infos))

    total_uncompressed = 0
    for info in infos:
        _validate_member_path(info.filename)
        if _is_symlink(info):
            raise ReplayArtifactError.symlink_entry(info.filename)
        if info.file_size > _MAX_ENTRY_BYTES:
            raise ReplayArtifactError.entry_too_large(info.filename)
        if info.compress_size > 0 and (info.file_size / info.compress_size) > _MAX_COMPRESSION_RATIO:
            raise ReplayArtifactError.excessive_compression_ratio(info.filename)
        total_uncompressed += info.file_size
        if total_uncompressed > _MAX_TOTAL_BYTES:
            raise ReplayArtifactError.total_too_large()


def _validate_member_path(filename: str) -> None:
    if not filename or filename.startswith("/") or "\\" in filename:
        raise ReplayArtifactError.unsafe_path(filename)
    parts = [part for part in filename.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ReplayArtifactError.unsafe_path(filename)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _mp4_sort_key(info: zipfile.ZipInfo) -> tuple[int, str]:
    match = _STEP_INDEX_RE.search(info.filename)
    return (int(match.group(1)) if match else -1, info.filename)


def _set_record_env(doc: dict[str, Any]) -> None:
    env = doc.get("env")
    merged = dict(env) if isinstance(env, dict) else {}
    merged[REPLAY_RECORD_ENV] = "1"
    doc["env"] = merged


def _default_shell(container: Mapping[str, Any]) -> str:
    defaults = container.get("defaults")
    if not isinstance(defaults, Mapping):
        return ""
    run_defaults = defaults.get("run")
    if not isinstance(run_defaults, Mapping):
        return ""
    shell = run_defaults.get("shell")
    return shell.strip().lower() if isinstance(shell, str) else ""


def _select_job_ids(
    parsed: Workflow,
    jobs: Mapping[str, Any],
    target_checks: Collection[str] | None,
) -> list[str]:
    eligible = [job_id for job_id, job in jobs.items() if isinstance(job, dict) and _is_eligible_job(job)]
    if not target_checks:
        return eligible
    targets = {check.strip() for check in target_checks if check.strip()}
    if not targets:
        return eligible
    matched = [job_id for job_id in eligible if _job_matches_targets(parsed, job_id, jobs[job_id], targets)]
    return matched or eligible


def _job_matches_targets(
    parsed: Workflow,
    job_id: str,
    job: Mapping[str, Any],
    targets: set[str],
) -> bool:
    if not parsed.keys(job_id).isdisjoint(targets):
        return True
    if job_id in targets:
        return True
    display_name = job.get("name")
    return isinstance(display_name, str) and display_name in targets


def _is_eligible_job(job: Mapping[str, Any]) -> bool:
    if "uses" in job:
        return False
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    return not _is_windows_runner(job.get("runs-on"))


def _is_windows_runner(runs_on: object) -> bool:
    if isinstance(runs_on, str):
        return "windows" in runs_on.lower()
    if isinstance(runs_on, list):
        return any(isinstance(item, str) and "windows" in item.lower() for item in runs_on)
    return False


def _instrument_job(
    job_id: str,
    job: dict[str, Any],
    *,
    workflow_shell: str,
    agg_version: str,
) -> None:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return

    safe_job_id = _safe_job_slug(job_id)
    job_shell = _default_shell(job) or workflow_shell
    new_steps: list[Any] = []
    saw_checkout = False
    wrapped_any = False

    for step_idx, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            new_steps.append(raw_step)
            continue

        if _is_checkout_step(raw_step):
            new_steps.append(raw_step)
            if not saw_checkout:
                saw_checkout = True
                new_steps.append(_install_tools_step(agg_version))
            continue

        if saw_checkout and isinstance(raw_step.get("run"), str):
            wrapped = _wrap_run_step(raw_step, safe_job_id=safe_job_id, step_idx=step_idx, default_shell=job_shell)
            if wrapped is not None:
                new_steps.append(wrapped)
                wrapped_any = True
                continue

        new_steps.append(raw_step)

    if wrapped_any:
        new_steps.append(_upload_artifact_step(safe_job_id))
        job["steps"] = new_steps


def _is_checkout_step(step: Mapping[str, Any]) -> bool:
    uses = step.get("uses")
    return isinstance(uses, str) and uses.strip().startswith("actions/checkout@")


def _wrap_run_step(
    step: Mapping[str, Any],
    *,
    safe_job_id: str,
    step_idx: int,
    default_shell: str,
) -> dict[str, Any] | None:
    script = step.get("run")
    if not isinstance(script, str) or RECORD_SCRIPT_PATH in script:
        return None
    raw_shell = step.get("shell")
    shell_name = raw_shell.strip().lower() if isinstance(raw_shell, str) else default_shell
    shell_argv = _SUPPORTED_SHELLS.get(shell_name)
    if shell_argv is None:
        return None

    delimiter = _heredoc_delimiter(script)
    body = script if script.endswith("\n") else f"{script}\n"
    shell_cmd = " ".join(shell_argv)
    wrapped_script = (
        f'_pc_dir="${{RUNNER_TEMP:-/tmp}}/pit-crew-replay/{safe_job_id}"\n'
        'mkdir -p "$_pc_dir"\n'
        '_pc_script="$(mktemp)"\n'
        f"cat <<'{delimiter}' > \"$_pc_script\"\n"
        f"{body}"
        f"{delimiter}\n"
        f'python3 "$GITHUB_WORKSPACE/{RECORD_SCRIPT_PATH}" '
        f'--cast "$_pc_dir/step-{step_idx}.cast" '
        f'--video "$_pc_dir/step-{step_idx}.mp4" '
        f'--video-on-failure -- {shell_cmd} "$_pc_script"\n'
        "_pc_rc=$?\n"
        'rm -f "$_pc_script"\n'
        'exit "$_pc_rc"\n'
    )
    updated = dict(step)
    updated["run"] = wrapped_script
    updated["shell"] = "bash"
    return updated


def _heredoc_delimiter(script: str) -> str:
    base = "__PIT_CREW_STEP_EOF__"
    if base not in script:
        return base
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:12]
    return f"__PIT_CREW_STEP_EOF_{digest}__"


def _safe_job_slug(job_id: str) -> str:
    slug = _SAFE_NAME_RE.sub("-", job_id).strip("-")
    return slug or "job"


def _install_tools_step(agg_version: str) -> dict[str, Any]:
    script = (
        "set -uo pipefail\n"
        "if ! command -v ffmpeg >/dev/null 2>&1; then\n"
        "  if command -v apt-get >/dev/null 2>&1; then\n"
        "    sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg\n"
        "  elif command -v brew >/dev/null 2>&1; then\n"
        "    brew install ffmpeg\n"
        "  fi\n"
        "fi\n"
        "if ! command -v agg >/dev/null 2>&1; then\n"
        '  os="$(uname -s)"\n'
        '  arch="$(uname -m)"\n'
        '  target=""\n'
        '  case "${os}:${arch}" in\n'
        '    Linux:x86_64) target="x86_64-unknown-linux-gnu" ;;\n'
        '    Linux:aarch64|Linux:arm64) target="aarch64-unknown-linux-gnu" ;;\n'
        '    Darwin:x86_64) target="x86_64-apple-darwin" ;;\n'
        '    Darwin:arm64|Darwin:aarch64) target="aarch64-apple-darwin" ;;\n'
        "  esac\n"
        '  if [ -n "$target" ]; then\n'
        '    bin_dir="${RUNNER_TEMP:-/tmp}/pit-crew-bin"\n'
        '    mkdir -p "$bin_dir"\n'
        f'    url="https://github.com/asciinema/agg/releases/download/{agg_version}/agg-${{target}}"\n'
        '    if curl -fsSL "$url" -o "${bin_dir}/agg"; then\n'
        '      chmod +x "${bin_dir}/agg"\n'
        '      if [ -n "${GITHUB_PATH:-}" ]; then\n'
        '        echo "$bin_dir" >> "$GITHUB_PATH"\n'
        "      fi\n"
        "    fi\n"
        "  fi\n"
        "fi\n"
    )
    return {
        "name": "Install Pit Crew replay tools (agg, ffmpeg)",
        "continue-on-error": True,
        "shell": "bash",
        "run": script,
    }


def _upload_artifact_step(safe_job_id: str) -> dict[str, Any]:
    return {
        "name": "Upload Pit Crew replay recording",
        "if": "failure()",
        "uses": UPLOAD_ARTIFACT_ACTION,
        "with": {
            "name": f"{REPLAY_ARTIFACT_PREFIX}{safe_job_id}-${{{{ strategy.job-index }}}}",
            "path": f"{REPLAY_OUTPUT_DIR}/{safe_job_id}",
            "retention-days": 7,
            "if-no-files-found": "ignore",
        },
    }

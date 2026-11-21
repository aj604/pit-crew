"""Shared Proof ↔ Dispatcher contract for recording replays on GitHub Actions runners.

Ephemeral replay branches (never timing runs or candidate PRs) carry three additions:

1. ``.pit-crew/record.py`` — the standalone stdlib recorder copied verbatim from
   ``pit_crew.lanes.proof.record``.
2. ``PIT_CREW_RECORD: "1"`` in workflow ``env:`` (enabling browser/E2E artifact capture
   alongside terminal recording) plus a best-effort step that ensures ``agg`` and
   ``ffmpeg`` are installed on POSIX runners.
3. POSIX ``run:`` steps after a reliable replay ``actions/checkout`` wrapped with
   ``record.py --video-on-failure`` (preserving implicit vs. explicit shell rules,
   nested checkout paths, and Windows matrix legs), plus metadata and ``if: failure()``
   ``actions/upload-artifact`` steps uploading ``pit-crew-replay-<job>-...``.

If ``agg`` or ``ffmpeg`` is unavailable on the runner, the wrapped step still finishes with
its exact exit code, only ``.cast`` is uploaded, and ``extract_replay_video()`` raises
``ReplayArtifactError`` so ``verify_replays()`` leaves the replay ``"unverified"``
(fail-closed).
"""

import hashlib
import io
import json
import re
import shlex
import stat
import zipfile
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml

from pit_crew.lanes.proof.checks import Workflow
from pit_crew.lanes.proof.miner import check_key

RECORD_SCRIPT_PATH = ".pit-crew/record.py"
REPLAY_RECORD_ENV = "PIT_CREW_RECORD"
SECRET_ENV_VARS_KEY = "PIT_CREW_SECRET_ENV_VARS"  # noqa: S105  env var name, not a secret
REPLAY_ARTIFACT_PREFIX = "pit-crew-replay-"
REPLAY_META_FILENAME = "replay-meta.json"
REPLAY_OUTPUT_DIR = "${{ runner.temp }}/pit-crew-replay"
AGG_VERSION = "v1.5.0"
UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact@v4"

_MAX_ZIP_ENTRIES = 200
_MAX_ENTRY_BYTES = 50 * 1024 * 1024
_MAX_TOTAL_BYTES = 60 * 1024 * 1024
_MAX_META_BYTES = 64 * 1024
_MAX_COMPRESSION_RATIO = 50
_ZIP_MAGIC = b"PK\x03\x04"

_EXPLICIT_SHELLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bash": ("bash", ("bash", "--noprofile", "--norc", "-eo", "pipefail")),
    "sh": ("sh", ("sh", "-e")),
    "python": ("sh", ("python3",)),
}
_UNSUPPORTED_SHELL = "__pit_crew_unsupported_shell__"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_SHELL_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_STEP_INDEX_RE = re.compile(r"step-(\d+)\.mp4$", re.IGNORECASE)
_MATRIX_EXPR_RE = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z0-9_.-]+)\s*\}\}$")
_TEMPLATE_EXPR_RE = re.compile(r"\$\{\{.*?\}\}")
_MATRIX_SUFFIX_RE = re.compile(r"\s*\(.*\)$")
_WIN_EQ_RE = re.compile(r"^\s*runner\.os\s*==\s*['\"]windows['\"]\s*$", re.IGNORECASE)
_WIN_NEQ_RE = re.compile(r"^\s*runner\.os\s*!=\s*['\"]windows['\"]\s*$", re.IGNORECASE)
_SECRET_EXPR_RE = re.compile(
    r"\$\{\{[^}]*\b(?:secrets(?:\.[A-Za-z0-9_]+|\[[^\]]+\])?|github\.token)\b[^}]*\}\}",
    re.IGNORECASE,
)
_REPLAY_REPO_EXPRS = frozenset({"github.repository"})
_REPLAY_REF_EXPRS = frozenset({"github.sha", "github.ref", "github.ref_name"})
_POSIX_RUNNER_PREFIXES = (
    "ubuntu",
    "macos",
    "linux",
    "debian",
    "alpine",
    "fedora",
    "centos",
    "rhel",
    "rocky",
    "alma",
)
_POSIX_RUNNER_EXACT = frozenset({"linux", "macos", "osx", "darwin", "ubuntu"})

RunnerKind = Literal["posix", "mixed", "windows", "unknown"]


class _WorkflowYamlLoader(yaml.SafeLoader):
    """YAML 1.2 Core Schema loader matching GitHub Actions scalar typing.

    Unlike PyYAML's default YAML 1.1 ``SafeLoader``, keeps plain ``on``, ``off``, ``yes``,
    ``no``, ``y``, ``n``, ISO dates, leading-zero strings, and ``HH:MM`` scalars as strings
    while still decoding ``true``/``false``, ``null``, and standard numbers.
    """


_WorkflowYamlLoader.yaml_implicit_resolvers = {}
for _first_char in "tTfF":
    _WorkflowYamlLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
        [_first_char],
    )
for _first_char in ("~", "n", "N", ""):
    _WorkflowYamlLoader.add_implicit_resolver(
        "tag:yaml.org,2002:null",
        re.compile(r"^(?:~|null|Null|NULL|)$"),
        [_first_char],
    )
for _first_char in "-+0123456789":
    _WorkflowYamlLoader.add_implicit_resolver(
        "tag:yaml.org,2002:int",
        re.compile(r"^(?:[-+]?(?:0|[1-9][0-9]*)|0o[0-7]+|0x[0-9a-fA-F]+)$"),
        [_first_char],
    )
for _first_char in "-+.0123456789":
    _WorkflowYamlLoader.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(
            r"^(?:[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*(?:[eE][-+]?[0-9]+)?|[0-9]+[eE][-+]?[0-9]+)"
            r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"
        ),
        [_first_char],
    )
_WorkflowYamlLoader.add_implicit_resolver("tag:yaml.org,2002:merge", re.compile(r"^(?:<<)$"), ["<"])
_WorkflowYamlLoader.add_implicit_resolver("tag:yaml.org,2002:yaml", re.compile(r"^(?:!|&|\*)$"), ["!", "&", "*"])


def _load_workflow_yaml(text: str) -> object:
    return yaml.load(text, Loader=_WorkflowYamlLoader)  # noqa: S506  SafeLoader subclass with YAML 1.2 Core Schema


def _parse_workflow_v12(workflow_yaml: str, workflow_path: str) -> tuple[Workflow, dict[str, Any]] | None:
    try:
        raw_doc = _load_workflow_yaml(workflow_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(raw_doc, dict):
        return None
    doc: dict[str, Any] = {str(key): value for key, value in raw_doc.items()}
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or not all(isinstance(k, str) and _is_valid_job(v) for k, v in jobs.items()):
        return None
    name = doc.get("name")
    parsed = Workflow(
        name=name if isinstance(name, str) and name else workflow_path,
        header={k: v for k, v in doc.items() if k != "jobs"},
        jobs=jobs,
    )
    return parsed, doc


def _is_valid_job(job: object) -> bool:
    if not isinstance(job, dict):
        return False
    needs = job.get("needs")
    return (
        needs is None or isinstance(needs, str) or (isinstance(needs, list) and all(isinstance(n, str) for n in needs))
    )


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

    @classmethod
    def mismatched_check(cls, recorded: str, expected: Collection[str]) -> "ReplayArtifactError":
        """Build error when replay metadata proves the recording came from a different check."""
        expected_str = ", ".join(sorted(expected))
        return cls(f"replay artifact belongs to check {recorded!r}, not expected check(s) ({expected_str})")


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
    instrumentation never breaks a valid workflow. Uses YAML 1.2 Core Schema rules so plain
    strings like ``on``, ``off``, ``yes``, and ``no`` are never rewritten as booleans.
    """
    loaded = _parse_workflow_v12(workflow_yaml, workflow_path)
    if loaded is None:
        return workflow_yaml
    parsed, doc = loaded

    _set_record_env(doc)

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return workflow_yaml

    workflow_shell = _default_shell(doc)
    workflow_secret_env = _secret_env_var_names(doc.get("env"))
    selected_ids = _select_job_ids(parsed, jobs, target_checks)
    for job_id in selected_ids:
        job = jobs.get(job_id)
        if isinstance(job, dict):
            runner_kind = _classify_job_runner(job)
            _instrument_job(
                job_id,
                job,
                workflow_name=parsed.name,
                workflow_shell=workflow_shell,
                workflow_secret_env=workflow_secret_env,
                agg_version=agg_version,
                runner_kind=runner_kind,
            )

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


def resolve_workflow_check_aliases(
    workflow_yaml: str | None,
    workflow_path: str,
    expected_checks: Collection[str],
) -> tuple[str, ...]:
    """Return known job-id / display-name aliases for ``expected_checks`` from ``workflow_yaml``.

    Only adds cross-aliases (``job_id`` <-> ``name``) for non-matrix jobs or unqualified matrix
    parents; specific expanded matrix leg targets keep their exact identity so unrelated matrix
    legs can never alias onto them.
    """
    if not workflow_yaml or not expected_checks:
        return ()
    loaded = _parse_workflow_v12(workflow_yaml, workflow_path)
    if loaded is None:
        return ()
    parsed, _ = loaded
    targets = {c.strip() for c in expected_checks if c.strip()}
    if not targets:
        return ()

    aliases: list[str] = []
    for job_id, job in parsed.jobs.items():
        if not _job_matches_targets(parsed, job_id, job, targets):
            continue
        _collect_job_aliases(parsed, job_id, job, targets, aliases)
    return tuple(dict.fromkeys(aliases))


def _collect_job_aliases(
    parsed: Workflow,
    job_id: str,
    job: Mapping[str, Any],
    targets: set[str],
    out: list[str],
) -> None:
    display_name = job.get("name")
    has_template = isinstance(display_name, str) and bool(_TEMPLATE_EXPR_RE.search(display_name))
    strategy = job.get("strategy")
    has_matrix = isinstance(strategy, Mapping) and "matrix" in strategy

    if not has_template and not has_matrix:
        out.extend((job_id, check_key(parsed.name, job_id)))
        if isinstance(display_name, str) and display_name.strip():
            out.extend((display_name.strip(), check_key(parsed.name, display_name.strip())))
        return

    # Matrix or templated job: only add parent id aliases if the target named the unexpanded parent.
    unqualified_targets = {t for t in targets if not _MATRIX_SUFFIX_RE.search(t)}
    parent_names = {job_id.lower(), check_key(parsed.name, job_id).lower()}
    if isinstance(display_name, str) and display_name.strip() and not has_template:
        parent_names.add(display_name.strip().lower())
        parent_names.add(check_key(parsed.name, display_name.strip()).lower())
    if any(t.lower() in parent_names for t in unqualified_targets):
        out.extend((job_id, check_key(parsed.name, job_id)))
        if isinstance(display_name, str) and display_name.strip() and not has_template:
            out.extend((display_name.strip(), check_key(parsed.name, display_name.strip())))


def read_replay_metadata(data: bytes) -> dict[str, str] | None:
    """Safely read ``replay-meta.json`` from a replay artifact ZIP, or return ``None`` if absent."""
    if not data.startswith(_ZIP_MAGIC):
        return None
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ReplayArtifactError.corrupt_archive(error) from error

    with archive:
        infos = archive.infolist()
        _validate_zip_members(infos)
        return _read_metadata_from_open_zip(archive, infos)


def extract_replay_video(
    data: bytes,
    *,
    expected_checks: Collection[str] = (),
    check_aliases: Collection[str] = (),
) -> bytes:
    """Return MP4 bytes from either a raw video payload or a GitHub Actions artifact ZIP archive.

    Non-ZIP payloads pass through untouched so callers with raw recordings continue working.
    ZIP payloads are validated against path traversal, symlinks, entry caps, zip bombs, and
    (when ``expected_checks`` and ``replay-meta.json`` are present) recorded check identity.
    When no ``.mp4`` entry is present (for example when ``agg`` or ``ffmpeg`` was unavailable on
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
        if expected_checks:
            meta = _read_metadata_from_open_zip(archive, infos)
            if meta is not None:
                _verify_metadata_matches_checks(meta, expected_checks, check_aliases)
        mp4_entries = [info for info in infos if not info.is_dir() and info.filename.lower().endswith(".mp4")]
        if not mp4_entries:
            raise ReplayArtifactError.missing_video()
        chosen = max(mp4_entries, key=_mp4_sort_key)
        return archive.read(chosen.filename)


def _read_metadata_from_open_zip(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
) -> dict[str, str] | None:
    for info in infos:
        if info.is_dir():
            continue
        if info.filename.rsplit("/", maxsplit=1)[-1].lower() != REPLAY_META_FILENAME:
            continue
        if info.file_size > _MAX_META_BYTES:
            raise ReplayArtifactError.entry_too_large(info.filename)
        try:
            parsed = json.loads(archive.read(info.filename).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        return {str(k): str(v) for k, v in parsed.items() if isinstance(k, str) and isinstance(v, str)}
    return None


def _verify_metadata_matches_checks(
    meta: Mapping[str, str],
    expected_checks: Collection[str],
    check_aliases: Collection[str],
) -> None:
    job_name = meta.get("job_name", "").strip()
    workflow = meta.get("workflow", "").strip()
    job_id = meta.get("job_id", "").strip()
    recorded_names = [name for name in (job_name, job_id) if name]
    if not recorded_names:
        return
    all_targets = [c.strip() for c in (*expected_checks, *check_aliases) if c.strip()]
    if not all_targets:
        return
    # Check resolved job_name first; only accept job_id when job_name is absent or matches job_id.
    if job_name and _check_matches_job_identity(all_targets, job_name, workflow):
        return
    if not job_name and job_id and _check_matches_job_identity(all_targets, job_id, workflow):
        return
    recorded_label = f"{workflow}/{job_name or job_id}" if workflow else (job_name or job_id)
    raise ReplayArtifactError.mismatched_check(recorded_label, expected_checks)


def _check_matches_job_identity(targets: Collection[str], job_name: str, workflow: str) -> bool:
    job_low = job_name.strip().lower()
    wf_low = workflow.strip().lower()
    folded_job = _MATRIX_SUFFIX_RE.sub("", job_low).strip()
    for target in targets:
        target_low = target.strip().lower()
        if not target_low:
            continue
        target_has_matrix = bool(_MATRIX_SUFFIX_RE.search(target_low))
        if _exact_job_name_match(target_low, job_low, wf_low):
            return True
        if not target_has_matrix and _exact_job_name_match(target_low, folded_job, wf_low):
            return True
    return False


def _exact_job_name_match(target_low: str, job_low: str, wf_low: str) -> bool:
    if target_low == job_low:
        return True
    if wf_low and target_low == f"{wf_low}/{job_low}":
        return True
    if target_low.endswith(f"/{job_low}"):
        prefix = target_low[: -(len(job_low) + 1)].strip()
        if prefix and prefix.count("(") == prefix.count(")") and (not wf_low or prefix == wf_low):
            return True
    return False


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


def _default_shell(container: Mapping[str, Any]) -> str | None:
    defaults = container.get("defaults")
    if not isinstance(defaults, Mapping):
        return None
    run_defaults = defaults.get("run")
    if not isinstance(run_defaults, Mapping) or "shell" not in run_defaults or run_defaults["shell"] is None:
        return None
    shell = run_defaults["shell"]
    if not isinstance(shell, str) or not shell.strip():
        return _UNSUPPORTED_SHELL
    return shell.strip().lower()


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
    wf_low = parsed.name.lower()
    lower_targets = {t.lower() for t in targets}
    if job_id.lower() in lower_targets or check_key(parsed.name, job_id).lower() in lower_targets:
        return True
    if any(_exact_job_name_match(_MATRIX_SUFFIX_RE.sub("", t).strip(), job_id.lower(), wf_low) for t in lower_targets):
        return True

    display_name = job.get("name")
    if not isinstance(display_name, str) or not display_name.strip():
        return False
    return _display_name_matches_targets(display_name.strip(), wf_low, lower_targets)


def _display_name_matches_targets(display_name: str, wf_low: str, lower_targets: set[str]) -> bool:
    name_low = display_name.lower()
    if any(_exact_job_name_match(t, name_low, wf_low) for t in lower_targets):
        return True
    if any(_exact_job_name_match(_MATRIX_SUFFIX_RE.sub("", t).strip(), name_low, wf_low) for t in lower_targets):
        return True
    if not _TEMPLATE_EXPR_RE.search(display_name):
        return False
    pattern = _template_to_regex(display_name)
    for target in lower_targets:
        candidate = target
        if "/" in candidate:
            prefix, _, suffix = candidate.partition("/")
            if not wf_low or prefix.strip() == wf_low:
                candidate = suffix.strip()
        if pattern.fullmatch(candidate) or pattern.fullmatch(_MATRIX_SUFFIX_RE.sub("", candidate).strip()):
            return True
    return False


def _template_to_regex(template: str) -> re.Pattern[str]:
    parts = _TEMPLATE_EXPR_RE.split(template.lower())
    regex_str = ".+".join(re.escape(part) for part in parts)
    return re.compile(f"^{regex_str}$")


def _is_eligible_job(job: Mapping[str, Any]) -> bool:
    if "uses" in job:
        return False
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    return _classify_job_runner(job) in {"posix", "mixed"}


def _classify_job_runner(job: Mapping[str, Any]) -> RunnerKind:
    specs = _resolve_runner_specs(job.get("runs-on"), job.get("strategy"))
    if specs is None or not specs:
        return "unknown"
    kinds = {_classify_single_runner(spec) for spec in specs}
    if "unknown" in kinds:
        return "unknown"
    if kinds == {"posix"}:
        return "posix"
    if kinds == {"windows"}:
        return "windows"
    return "mixed"


def _resolve_runner_specs(runs_on: object, strategy: object) -> list[list[str]] | None:
    if isinstance(runs_on, Mapping):
        if "labels" not in runs_on:
            return None
        return _resolve_runner_specs(runs_on["labels"], strategy)
    if isinstance(runs_on, str):
        return _resolve_runner_string(runs_on, strategy)
    if isinstance(runs_on, list) and runs_on:
        return _resolve_runner_list(runs_on, strategy)
    return None


def _resolve_runner_string(value: str, strategy: object) -> list[list[str]] | None:
    stripped = value.strip()
    if not stripped:
        return None
    if "${{" not in stripped:
        return [[stripped]]
    matrix_values = _resolve_matrix_expression(stripped, strategy)
    if matrix_values is None:
        return None
    specs: list[list[str]] = []
    for item in matrix_values:
        if isinstance(item, str) and item.strip() and "${{" not in item:
            specs.append([item.strip()])
        elif isinstance(item, list) and item and all(isinstance(x, str) and x.strip() and "${{" not in x for x in item):
            specs.append([x.strip() for x in item])
        else:
            return None
    return specs or None


def _resolve_runner_list(items: list[object], strategy: object) -> list[list[str]] | None:
    expanded: list[list[str]] = [[]]
    for item in items:
        if not isinstance(item, str) or not item.strip():
            return None
        resolved = _resolve_runner_string(item, strategy)
        if resolved is None:
            return None
        expanded = [prefix + suffix for prefix in expanded for suffix in resolved]
    return expanded or None


def _resolve_matrix_expression(expr: str, strategy: object) -> list[object] | None:
    match = _MATRIX_EXPR_RE.match(expr.strip())
    if match is None or not isinstance(strategy, Mapping):
        return None
    matrix = strategy.get("matrix")
    if not isinstance(matrix, Mapping):
        return None
    path_parts = match.group(1).split(".")
    base_values = _matrix_top_values(matrix, path_parts)
    include_values = _matrix_include_values(matrix, path_parts)
    if base_values is None or include_values is None:
        return None
    combined = [*base_values, *include_values]
    return combined or None


def _matrix_top_values(matrix: Mapping[str, Any], path_parts: list[str]) -> list[object] | None:
    top_key = path_parts[0]
    if top_key not in matrix:
        return []
    raw_top = matrix[top_key]
    if not isinstance(raw_top, list):
        return None
    values: list[object] = []
    for entry in raw_top:
        found, resolved = _extract_dotted(entry, path_parts[1:])
        if not found:
            return None
        values.append(resolved)
    return values


def _matrix_include_values(matrix: Mapping[str, Any], path_parts: list[str]) -> list[object] | None:
    raw_include = matrix.get("include")
    if raw_include is None:
        return []
    if not isinstance(raw_include, list):
        return None
    values: list[object] = []
    for include_item in raw_include:
        if not isinstance(include_item, Mapping):
            return None
        found, resolved = _extract_dotted(include_item, path_parts)
        if found:
            values.append(resolved)
    return values


def _extract_dotted(obj: object, parts: list[str]) -> tuple[bool, object]:
    current = obj
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _classify_single_runner(labels: list[str]) -> Literal["posix", "windows", "unknown"]:
    normalized = [label.strip().lower() for label in labels if label.strip()]
    if not normalized or any("${{" in label for label in normalized):
        return "unknown"
    if any("windows" in label or label in {"win32", "win64"} for label in normalized):
        return "windows"
    if any(label in _POSIX_RUNNER_EXACT or label.startswith(_POSIX_RUNNER_PREFIXES) for label in normalized):
        return "posix"
    return "unknown"


def _instrument_job(  # noqa: PLR0913
    job_id: str,
    job: dict[str, Any],
    *,
    workflow_name: str,
    workflow_shell: str | None,
    workflow_secret_env: frozenset[str],
    agg_version: str,
    runner_kind: RunnerKind,
) -> None:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return

    mixed = runner_kind == "mixed"
    safe_job_id = _safe_job_slug(job_id)
    job_default = _default_shell(job)
    effective_default = job_default if job_default is not None else workflow_shell
    accumulated_secret_env = set(workflow_secret_env | _secret_env_var_names(job.get("env")))
    new_steps: list[Any] = []
    recorder_checkout_path: str | None = None
    installed_tools = False
    wrapped_any = False

    for step_idx, raw_step in enumerate(steps):
        if not isinstance(raw_step, dict):
            new_steps.append(raw_step)
            continue

        accumulated_secret_env.update(_secret_env_var_names(raw_step.get("env")))
        effect, rel_path = _checkout_effect(raw_step, mixed=mixed)
        if effect != "none":
            new_steps.append(raw_step)
            recorder_checkout_path, installed_tools = _apply_checkout_effect(
                effect,
                rel_path,
                current_path=recorder_checkout_path,
                installed_tools=installed_tools,
                new_steps=new_steps,
                agg_version=agg_version,
                mixed=mixed,
            )
            continue

        if recorder_checkout_path is not None and isinstance(raw_step.get("run"), str):
            emitted = _instrument_run_step(
                raw_step,
                safe_job_id=safe_job_id,
                step_idx=step_idx,
                default_shell=effective_default,
                recorder_expr=_recorder_shell_expr(recorder_checkout_path),
                secret_env_vars=frozenset(accumulated_secret_env),
                mixed=mixed,
            )
            if emitted is not None:
                new_steps.extend(emitted)
                wrapped_any = True
                continue

        new_steps.append(raw_step)

    if wrapped_any:
        raw_job_name = job.get("name")
        new_steps.append(
            _prepare_metadata_step(
                workflow_name=workflow_name,
                job_id=job_id,
                raw_job_name=raw_job_name if isinstance(raw_job_name, str) else "",
                safe_job_id=safe_job_id,
                mixed=mixed,
            )
        )
        new_steps.append(_upload_artifact_step(safe_job_id, mixed=mixed))
        job["steps"] = new_steps


def _apply_checkout_effect(  # noqa: PLR0913
    effect: str,
    rel_path: str | None,
    *,
    current_path: str | None,
    installed_tools: bool,
    new_steps: list[Any],
    agg_version: str,
    mixed: bool,
) -> tuple[str | None, bool]:
    if effect == "replay":
        if not installed_tools:
            new_steps.append(_install_tools_step(agg_version, mixed=mixed))
            installed_tools = True
        return rel_path, installed_tools
    if current_path is not None and _checkout_clobbers_path(current_path, rel_path):
        return None, installed_tools
    return current_path, installed_tools


def _checkout_clobbers_path(existing_path: str, other_path: str | None) -> bool:
    if other_path is None or other_path in {"", existing_path}:
        return True
    return existing_path.startswith(f"{other_path}/")


def _is_checkout_step(step: Mapping[str, Any]) -> bool:
    uses = step.get("uses")
    return isinstance(uses, str) and uses.strip().startswith("actions/checkout@")


def _checkout_effect(step: Mapping[str, Any], *, mixed: bool) -> tuple[Literal["none", "replay", "other"], str | None]:
    if not _is_checkout_step(step):
        return "none", None
    raw_with = step.get("with")
    if raw_with is not None and not isinstance(raw_with, Mapping):
        return "other", None
    with_cfg: Mapping[str, Any] = raw_with or {}

    ok_path, rel_path = _normalize_checkout_path(with_cfg.get("path"))
    if not ok_path:
        return "other", None

    if not _is_replay_checkout_config(step, with_cfg, mixed=mixed):
        return "other", rel_path
    return "replay", rel_path


def _is_replay_checkout_config(step: Mapping[str, Any], with_cfg: Mapping[str, Any], *, mixed: bool) -> bool:
    if not _is_allowed_checkout_if(step.get("if"), mixed=mixed):
        return False
    if not _is_self_expr_or_empty(with_cfg.get("repository"), _REPLAY_REPO_EXPRS):
        return False
    if not _is_self_expr_or_empty(with_cfg.get("ref"), _REPLAY_REF_EXPRS):
        return False
    sparse = with_cfg.get("sparse-checkout")
    return sparse is None or (isinstance(sparse, str) and not sparse.strip())


def _is_allowed_checkout_if(raw_if: object, *, mixed: bool) -> bool:
    if raw_if is None:
        return True
    if isinstance(raw_if, str) and not raw_if.strip():
        return True
    return mixed and _is_non_windows_condition(raw_if)


def _is_self_expr_or_empty(value: object, allowed_exprs: frozenset[str]) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return True
    inner = _unwrap_expression(stripped)
    return inner is not None and inner.lower() in allowed_exprs


def _normalize_checkout_path(raw_path: object) -> tuple[bool, str | None]:
    if raw_path is None:
        return True, ""
    if not isinstance(raw_path, str):
        return False, None
    stripped = raw_path.strip()
    if not stripped or stripped in {".", "./"}:
        return True, ""
    has_bad_chars = any(tok in stripped for tok in ("${{", "\x00", "\n", "\r", "\\"))
    parts = [part for part in stripped.split("/") if part not in {"", "."}]
    if has_bad_chars or stripped.startswith(("/", "~")) or any(part == ".." for part in parts):
        return False, None
    return True, "/".join(parts)


def _recorder_shell_expr(checkout_rel_path: str) -> str:
    rel = f"{checkout_rel_path}/{RECORD_SCRIPT_PATH}" if checkout_rel_path else RECORD_SCRIPT_PATH
    if _SAFE_SHELL_PATH_RE.fullmatch(rel):
        return f'"$GITHUB_WORKSPACE/{rel}"'
    return f'"$GITHUB_WORKSPACE/"{shlex.quote(rel)}'


def _secret_env_var_names(raw_env: object) -> frozenset[str]:
    if not isinstance(raw_env, Mapping):
        return frozenset()
    names: set[str] = set()
    for key, value in raw_env.items():
        if isinstance(key, str) and key.strip() and isinstance(value, str) and _SECRET_EXPR_RE.search(value):
            names.add(key.strip())
    return frozenset(names)


def _instrument_run_step(  # noqa: PLR0913
    step: Mapping[str, Any],
    *,
    safe_job_id: str,
    step_idx: int,
    default_shell: str | None,
    recorder_expr: str,
    secret_env_vars: frozenset[str],
    mixed: bool,
) -> list[dict[str, Any]] | None:
    if mixed:
        if _is_windows_only_condition(step.get("if")):
            return None
        already_non_windows = _is_non_windows_condition(step.get("if"))
        if not already_non_windows and step.get("id") is not None:
            return None
        wrapped = _wrap_run_step(
            step,
            safe_job_id=safe_job_id,
            step_idx=step_idx,
            default_shell=default_shell,
            recorder_expr=recorder_expr,
            secret_env_vars=secret_env_vars,
        )
        if wrapped is None:
            return None
        if already_non_windows:
            return [wrapped]
        wrapped["if"] = _combine_if("runner.os != 'Windows'", step.get("if"))
        windows_copy = dict(step)
        windows_copy["if"] = _combine_if("runner.os == 'Windows'", step.get("if"))
        return [wrapped, windows_copy]

    wrapped = _wrap_run_step(
        step,
        safe_job_id=safe_job_id,
        step_idx=step_idx,
        default_shell=default_shell,
        recorder_expr=recorder_expr,
        secret_env_vars=secret_env_vars,
    )
    return [wrapped] if wrapped is not None else None


def _wrap_run_step(  # noqa: PLR0913
    step: Mapping[str, Any],
    *,
    safe_job_id: str,
    step_idx: int,
    default_shell: str | None,
    recorder_expr: str,
    secret_env_vars: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    script = step.get("run")
    if not isinstance(script, str) or RECORD_SCRIPT_PATH in script or _SECRET_EXPR_RE.search(script):
        return None
    resolved = _resolve_step_shell(step, default_shell)
    if resolved is None:
        return None
    outer_shell, preamble, exec_argv = resolved

    delimiter = _heredoc_delimiter(script)
    body = script if script.endswith("\n") else f"{script}\n"
    wrapped_script = (
        f'_pc_dir="${{RUNNER_TEMP:-/tmp}}/pit-crew-replay/{safe_job_id}"\n'
        'mkdir -p "$_pc_dir"\n'
        f'_pc_masks="${{RUNNER_TEMP:-/tmp}}/pit-crew-masks/{safe_job_id}.masks"\n'
        '_pc_script="$(mktemp)"\n'
        f"cat <<'{delimiter}' > \"$_pc_script\"\n"
        f"{body}"
        f"{delimiter}\n"
        f"{preamble}"
        "_pc_rc=0\n"
        f"python3 {recorder_expr} "
        f'--cast "$_pc_dir/step-{step_idx}.cast" '
        f'--video "$_pc_dir/step-{step_idx}.mp4" '
        '--masks-file "$_pc_masks" '
        f'--video-on-failure -- {exec_argv} "$_pc_script" || _pc_rc=$?\n'
        'rm -f "$_pc_script"\n'
        'exit "$_pc_rc"\n'
    )
    updated = dict(step)
    updated["run"] = wrapped_script
    updated["shell"] = outer_shell
    if secret_env_vars:
        existing_env = updated.get("env")
        merged_env = dict(existing_env) if isinstance(existing_env, Mapping) else {}
        merged_env[SECRET_ENV_VARS_KEY] = ",".join(sorted(secret_env_vars))
        updated["env"] = merged_env
    return updated


def _resolve_step_shell(step: Mapping[str, Any], default_shell: str | None) -> tuple[str, str, str] | None:
    """Return ``(outer_shell, preamble, exec_argv)`` or ``None`` if unsupported.

    Preserves GitHub Actions semantics:
    - Implicit non-Windows default (no ``shell:`` anywhere): ``bash -e`` if ``bash`` exists,
      falling back to ``sh -e`` (NO ``pipefail``).
    - Explicit ``shell: bash``: ``bash --noprofile --norc -eo pipefail``.
    - Explicit ``shell: sh``: ``sh -e``.
    - Explicit ``shell: python``: ``python3``.
    """
    if "shell" in step and step["shell"] is not None:
        raw_shell = step["shell"]
        if not isinstance(raw_shell, str) or not raw_shell.strip():
            return None
        explicit: str | None = raw_shell.strip().lower()
    else:
        explicit = default_shell

    if explicit is None:
        preamble = '_pc_shell="sh"\nif command -v bash >/dev/null 2>&1; then\n  _pc_shell="bash"\nfi\n'
        return "sh", preamble, '"$_pc_shell" -e'

    spec = _EXPLICIT_SHELLS.get(explicit)
    if spec is None:
        return None
    outer_shell, argv = spec
    return outer_shell, "", " ".join(argv)


def _is_windows_only_condition(raw_if: object) -> bool:
    expr = _strip_if_expr(raw_if)
    return bool(expr and _WIN_EQ_RE.match(expr))


def _is_non_windows_condition(raw_if: object) -> bool:
    expr = _strip_if_expr(raw_if)
    return bool(expr and _WIN_NEQ_RE.match(expr))


def _strip_if_expr(raw_if: object) -> str:
    if not isinstance(raw_if, str):
        return ""
    stripped = raw_if.strip()
    unwrapped = _unwrap_expression(stripped)
    return unwrapped if unwrapped is not None else stripped


def _unwrap_expression(text: str) -> str | None:
    if not (text.startswith("${{") and text.endswith("}}")):
        return None
    inner = text[3:-2]
    if "}}" in inner or "${{" in inner:
        return None
    return inner.strip()


def _combine_if(guard: str, original_if: object) -> str:
    if original_if is None:
        return guard
    orig = ("true" if original_if else "false") if isinstance(original_if, bool) else _strip_if_expr(str(original_if))
    if not orig:
        return guard
    return f"({guard}) && ({orig})"


def _heredoc_delimiter(script: str) -> str:
    base = "__PIT_CREW_STEP_EOF__"
    if base not in script:
        return base
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:12]
    return f"__PIT_CREW_STEP_EOF_{digest}__"


def _safe_job_slug(job_id: str) -> str:
    slug = _SAFE_NAME_RE.sub("-", job_id).strip("-")
    return slug or "job"


def _install_tools_step(agg_version: str, *, mixed: bool = False) -> dict[str, Any]:
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
    step: dict[str, Any] = {
        "name": "Install Pit Crew replay tools (agg, ffmpeg)",
        "continue-on-error": True,
        "shell": "bash",
        "run": script,
    }
    if mixed:
        step["if"] = "runner.os != 'Windows'"
    return step


def _prepare_metadata_step(
    *,
    workflow_name: str,
    job_id: str,
    raw_job_name: str,
    safe_job_id: str,
    mixed: bool = False,
) -> dict[str, Any]:
    script = (
        f'_pc_dir="${{RUNNER_TEMP:-/tmp}}/pit-crew-replay/{safe_job_id}"\n'
        'mkdir -p "$_pc_dir"\n'
        f'rm -f "${{RUNNER_TEMP:-/tmp}}/pit-crew-masks/{safe_job_id}.masks"\n'
        f"python3 - \"$_pc_dir/{REPLAY_META_FILENAME}\" <<'__PIT_CREW_META_EOF__'\n"
        "import json, os, re, sys\n"
        "wf = os.environ.get('PIT_CREW_WORKFLOW', '')\n"
        "jid = os.environ.get('PIT_CREW_JOB_ID', '')\n"
        "jname = os.environ.get('PIT_CREW_JOB_NAME', '').strip()\n"
        "jidx = os.environ.get('PIT_CREW_JOB_INDEX', '0')\n"
        "raw_m = os.environ.get('PIT_CREW_MATRIX_JSON', '').strip()\n"
        "try:\n"
        "    m = json.loads(raw_m) if raw_m else None\n"
        "except Exception:\n"
        "    m = None\n"
        "if not jname:\n"
        "    if isinstance(m, dict) and m:\n"
        "        jname = f\"{jid} ({', '.join(str(v) for v in m.values())})\"\n"
        "    else:\n"
        "        jname = jid\n"
        "meta = {'workflow': wf, 'job_id': jid, 'job_name': jname, 'job_index': jidx}\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as fh:\n"
        "    json.dump(meta, fh)\n"
        "safe_jid = re.sub(r'[^A-Za-z0-9._-]+', '-', jid).strip('-') or 'job'\n"
        "slug = re.sub(r'[^A-Za-z0-9._-]+', '-', jname.lower()).strip('-')[:80].strip('-') or safe_jid\n"
        f"prefix = {REPLAY_ARTIFACT_PREFIX!r}\n"
        "art = f'{prefix}{safe_jid}-{jidx}' if slug == safe_jid.lower() else f'{prefix}{safe_jid}-{slug}-{jidx}'\n"
        "gh_out = os.environ.get('GITHUB_OUTPUT', '')\n"
        "if gh_out:\n"
        "    with open(gh_out, 'a', encoding='utf-8') as fh:\n"
        "        fh.write(f'artifact_name={art}\\n')\n"
        "__PIT_CREW_META_EOF__\n"
    )
    return {
        "name": "Prepare Pit Crew replay metadata",
        "id": "pit_crew_replay_meta",
        "if": "failure() && runner.os != 'Windows'" if mixed else "failure()",
        "continue-on-error": True,
        "shell": "sh",
        "env": {
            "PIT_CREW_WORKFLOW": workflow_name,
            "PIT_CREW_JOB_ID": job_id,
            "PIT_CREW_JOB_NAME": raw_job_name,
            "PIT_CREW_MATRIX_JSON": "${{ toJson(matrix) }}",
            "PIT_CREW_JOB_INDEX": "${{ strategy.job-index }}",
        },
        "run": script,
    }


def _upload_artifact_step(safe_job_id: str, *, mixed: bool = False) -> dict[str, Any]:
    fallback_name = f"format('{REPLAY_ARTIFACT_PREFIX}{safe_job_id}-{{0}}', strategy.job-index)"
    job_dir = f"{REPLAY_OUTPUT_DIR}/{safe_job_id}"
    return {
        "name": "Upload Pit Crew replay recording",
        "if": "failure() && runner.os != 'Windows'" if mixed else "failure()",
        "uses": UPLOAD_ARTIFACT_ACTION,
        "with": {
            "name": f"${{{{ steps.pit_crew_replay_meta.outputs.artifact_name || {fallback_name} }}}}",
            "path": f"{job_dir}/*.mp4\n{job_dir}/{REPLAY_META_FILENAME}",
            "retention-days": 7,
            "if-no-files-found": "ignore",
        },
    }

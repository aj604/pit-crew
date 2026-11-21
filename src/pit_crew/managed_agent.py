"""The only module that talks to Gemini: Managed Agents, and one-shot multimodal calls.

Everything else in the repo runs and tests without an API key.
"""

import base64
import io
import json
import logging
import math
import os
import time
from collections.abc import Mapping, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, cast

from google import genai
from google.genai import types

from pit_crew import tools
from pit_crew.ledger import InMemoryLedger, Ledger

logger = logging.getLogger(__name__)

BASE_AGENT = "antigravity-preview-09-2026"
MODELS = {
    "analysis": "gemini-4-hydrogen",
    "optimizer": "gemini-4-neon",
    "proof": "gemini-4-carbon",
}
POLL_INTERVAL_SECONDS = 1.0
MAX_TOOL_TURNS = 25
MAX_INLINE_BYTES = 20 * 1024 * 1024  # Gemini inline request limit (base64-encoded media)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # Total raw media cap for Gemini Files API fallback
FILE_POLL_INTERVAL_SECONDS = 1.0
FILE_POLL_TIMEOUT_SECONDS = 120.0
PENDING_STATUSES = frozenset({"in_progress", "queued", "starting"})
FAILED_STATUSES = frozenset({"failed", "cancelled", "expired"})
GITHUB_URL = "https://github.com/"

_ROOT = Path(__file__).resolve().parents[2]
_LANES = Path(__file__).parent / "lanes"


class InteractionStepLike(Protocol):
    """Minimal step surface read from Interactions API responses."""

    @property
    def type(self) -> str:
        """Step discriminator, e.g. ``function_call``."""
        ...

    @property
    def id(self) -> str:
        """Unique call identifier."""
        ...

    @property
    def name(self) -> str:
        """Registered tool name."""
        ...

    @property
    def arguments(self) -> dict[str, Any] | None:
        """Parsed JSON arguments for the tool call."""
        ...


class InteractionLike(Protocol):
    """Minimal interaction surface returned by ``client.interactions``."""

    @property
    def id(self) -> str:
        """Interaction identifier."""
        ...

    @property
    def status(self) -> str | None:
        """Lifecycle state of the interaction."""
        ...

    @property
    def steps(self) -> Sequence[InteractionStepLike] | None:
        """Ordered steps recorded on the interaction."""
        ...

    @property
    def continuation_token(self) -> str | None:
        """Opaque continuation token when decoding pauses."""
        ...

    @property
    def environment_id(self) -> str | None:
        """Remote sandbox identifier allocated for the interaction."""
        ...


class InteractionsResourceLike(Protocol):
    """Subset of ``genai.Client.interactions`` used by ``run_lane``."""

    def create(self, **kwargs: object) -> InteractionLike:
        """Create or resume an interaction."""
        ...

    def get(self, interaction_id: str, /) -> InteractionLike:
        """Fetch an interaction by ID."""
        ...


class InteractionsClientLike(Protocol):
    """Client wrapper exposing an ``interactions`` resource."""

    @property
    def interactions(self) -> InteractionsResourceLike:
        """Interactions API endpoint group."""
        ...


class MaxTurnsExceededError(RuntimeError):
    """Raised when a lane exceeds ``MAX_TOOL_TURNS`` tool-call iterations."""

    def __init__(self, lane: str, max_turns: int) -> None:
        """Format error for ``lane`` hitting ``max_turns``."""
        super().__init__(f"lane {lane!r} exceeded {max_turns} tool-call turns")


class InteractionFailedError(RuntimeError):
    """Raised when a lane interaction ends in a terminal failure status (failed, cancelled, expired)."""

    def __init__(self, lane: str, interaction_id: str, status: str) -> None:
        """Format error for ``lane`` whose interaction ``interaction_id`` ended with ``status``."""
        super().__init__(f"lane {lane!r} interaction {interaction_id!r} ended with status {status!r}")
        self.lane = lane
        self.interaction_id = interaction_id
        self.status = status


def build_request(
    lane: str,
    task: str,
    *,
    repo: str | None = None,
    extra_sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The interaction request for one lane: its skill(s), its tools and its Gemini 4 model."""
    import_module(f"pit_crew.lanes.{lane}.tools")  # registers the lane's tools

    sources: list[dict[str, Any]] = [
        _inline(".agents/AGENTS.md", _ROOT / "agent" / "AGENTS.md"),
        _inline(f".agents/skills/{lane}/SKILL.md", _LANES / lane / "SKILL.md"),
    ]

    playbooks_dir = _LANES / lane / "playbooks"
    if playbooks_dir.is_dir():
        sources.extend(
            _inline(f".agents/skills/{lane}/playbooks/{playbook.name}", playbook)
            for playbook in sorted(playbooks_dir.glob("*.md"))
        )

    if repo:
        sources.append(repository_source(repo))
    if extra_sources:
        sources.extend(extra_sources)

    return {
        "agent": BASE_AGENT,
        "input": f"Lane: {lane}\n\n{task}",
        "agent_config": {"type": "antigravity", "model": MODELS[lane]},
        "environment": {
            "type": "remote",
            "sources": sources,
        },
        "tools": [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools.for_lane(lane)
        ],
    }


def run_lane(  # noqa: PLR0913  keyword-only options mirror build_request plus the injectable client
    lane: str,
    task: str,
    ledger: Ledger | None = None,
    *,
    repo: str | None = None,
    extra_sources: list[dict[str, Any]] | None = None,
    client: InteractionsClientLike | None = None,
) -> InteractionLike:
    """Run one lane interaction to completion, servicing `requires_action` tool calls via `tools.call`."""
    active_ledger = ledger if ledger is not None else InMemoryLedger()
    active_client = client if client is not None else cast("InteractionsClientLike", _gemini_client())

    request = build_request(lane, task, repo=repo, extra_sources=extra_sources)
    allowed_tool_names = {t["name"] for t in request["tools"]}

    interaction = active_client.interactions.create(**request)
    # Follow-up turns resume the same sandbox, so remember the first environment ID the API assigns.
    environment_id: str | None = None
    for _ in range(MAX_TOOL_TURNS):
        environment_id = environment_id or _environment_id(interaction)
        while interaction.status in PENDING_STATUSES:
            time.sleep(POLL_INTERVAL_SECONDS)
            interaction = active_client.interactions.get(interaction.id)
            environment_id = environment_id or _environment_id(interaction)

        if interaction.status is not None and interaction.status in FAILED_STATUSES:
            raise InteractionFailedError(lane, interaction.id, interaction.status)

        follow_up = {
            "agent": BASE_AGENT,
            "previous_interaction_id": interaction.id,
            "agent_config": request["agent_config"],
            "environment": _environment_id(interaction) or environment_id or request["environment"],
            "tools": request["tools"],
        }

        if interaction.status == "requires_continuation" and interaction.continuation_token:
            interaction = active_client.interactions.create(
                **follow_up, continuation_token=interaction.continuation_token
            )
            continue

        if interaction.status != "requires_action":
            return interaction

        call_steps = _pending_function_calls(interaction)
        if not call_steps:
            return interaction

        results = [_execute_tool_step(lane, allowed_tool_names, active_ledger, step) for step in call_steps]
        interaction = active_client.interactions.create(**follow_up, input=results)

    raise MaxTurnsExceededError(lane, MAX_TOOL_TURNS)


def _execute_tool_step(
    lane: str,
    allowed_tool_names: set[str],
    ledger: Ledger,
    step: InteractionStepLike,
) -> dict[str, Any]:
    if step.name not in allowed_tool_names:
        return {
            "type": "function_result",
            "call_id": step.id,
            "name": step.name,
            "is_error": True,
            "result": f"tool {step.name!r} is not permitted in lane {lane!r}",
        }
    try:
        output = tools.call(step.name, ledger, dict(step.arguments or {}))
    except (KeyError, OSError, TypeError, ValueError, RuntimeError) as exc:
        return {
            "type": "function_result",
            "call_id": step.id,
            "name": step.name,
            "is_error": True,
            "result": f"{type(exc).__name__}: {exc}",
        }
    return {
        "type": "function_result",
        "call_id": step.id,
        "name": step.name,
        "result": _serialize_result(output),
    }


def _serialize_result(output: object) -> str | dict[str, Any]:
    """Pass ``str`` and ``dict`` through; JSON-encode everything else (the SDK rejects bare lists and scalars)."""
    if isinstance(output, (str, dict)):
        return output
    return json.dumps(output, default=str)


def _environment_id(interaction: InteractionLike) -> str | None:
    """The remote environment ID the API assigned, absent on fakes and on interactions without one."""
    environment_id = getattr(interaction, "environment_id", None)
    return environment_id if isinstance(environment_id, str) and environment_id else None


def inline_text_source(target: str, content: str) -> dict[str, str]:
    """Mount UTF-8 text (e.g. run history JSON or diffs) at ``target`` in the sandbox."""
    return {"type": "inline", "target": target, "content": content}


def inline_bytes_source(target: str, data: bytes) -> dict[str, str]:
    """Mount binary artifacts (e.g. failure screenshots or trace archives) at ``target``."""
    return {
        "type": "inline",
        "target": target,
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
    }


def repository_source(repo: str, target: str = "/workspace") -> dict[str, str]:
    """Mount a GitHub repository into the remote sandbox.

    ``repo`` is a full URL or a bare ``owner/name``, which becomes ``https://github.com/owner/name``.
    """
    return {"type": "repository", "source": normalize_repository_url(repo), "target": target}


def normalize_repository_url(repo: str) -> str:
    """Expand a bare ``owner/name`` to its GitHub HTTPS URL; leave URLs (anything with a scheme) unchanged."""
    stripped = repo.strip()
    if "://" in stripped:
        return stripped
    return GITHUB_URL + stripped.strip("/")


class GenerateContentResponseLike(Protocol):
    """Minimal response surface read from ``client.models.generate_content``."""

    @property
    def text(self) -> str | None:
        """The model's text answer."""
        ...


class ModelsResourceLike(Protocol):
    """Subset of ``genai.Client.models`` used by ``ask_multimodal``."""

    def generate_content(self, **kwargs: object) -> GenerateContentResponseLike:
        """Generate one answer."""
        ...


class ModelsClientLike(Protocol):
    """Client wrapper exposing a ``models`` resource."""

    @property
    def models(self) -> ModelsResourceLike:
        """Models API endpoint group."""
        ...


class UploadedFileLike(Protocol):
    """Minimal uploaded file metadata returned by ``client.files.upload`` / ``get``."""

    @property
    def name(self) -> str | None:
        """Server-assigned file resource name used for polling and deletion."""
        ...

    @property
    def uri(self) -> str | None:
        """File URI passed to ``types.Part.from_uri``."""
        ...

    @property
    def state(self) -> object:
        """Lifecycle state (e.g. ``PROCESSING``, ``ACTIVE``, ``FAILED``)."""
        ...


class FilesResourceLike(Protocol):
    """Subset of ``genai.Client.files`` used when multimodal payloads exceed ``MAX_INLINE_BYTES``."""

    def upload(self, *, file: io.IOBase, config: types.UploadFileConfig | None = None) -> UploadedFileLike:
        """Upload a media stream to the Gemini Files API."""
        ...

    def get(self, *, name: str) -> UploadedFileLike:
        """Poll file processing state by resource name."""
        ...

    def delete(self, *, name: str) -> object:
        """Delete an uploaded file resource."""
        ...


class UnsupportedPartError(TypeError):
    """Raised when a multimodal part is neither ("text", str) nor (mime type, bytes)."""

    def __init__(self, kind: str) -> None:
        """Name the part kind that was given."""
        super().__init__(f"part {kind!r} must be ('text', str) or (mime type, bytes)")


class MediaTooLargeError(ValueError):
    """Raised when multimodal media exceeds the Gemini Files API upload cap."""

    def __init__(self, size: int) -> None:
        """Record the oversized raw media payload size."""
        super().__init__(f"media payload is {size} bytes, exceeding the {MAX_UPLOAD_BYTES}-byte Files API limit")


class FileUploadError(RuntimeError):
    """Raised when uploading or activating a file via the Gemini Files API fails."""


def ask_multimodal(
    model: str,
    parts: Sequence[tuple[str, str | bytes]],
    schema: Mapping[str, object],
    *,
    client: ModelsClientLike | None = None,
) -> str:
    """Send text and media to ``model`` and return its JSON answer, constrained to ``schema``.

    Each part is ``("text", str)`` or ``(mime_type, bytes)``, e.g. ``("video/webm", recording)``.
    Uses the inline bytes fast path when total encoded payload is ``<= MAX_INLINE_BYTES`` (20 MB),
    and automatically falls back to uploading binary media via ``client.files.upload`` (up to
    ``MAX_UPLOAD_BYTES`` = 100 MB) with deterministic ``finally: client.files.delete(...)`` cleanup.
    """
    _validate_parts(parts)
    active_client = client if client is not None else cast("ModelsClientLike", _gemini_client())
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=dict(schema),
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,  # keep screenshots and terminal text crisp
    )
    if _inline_payload_bytes(parts) <= MAX_INLINE_BYTES:
        response = active_client.models.generate_content(
            model=model,
            contents=[_part(kind, value) for kind, value in parts],
            config=config,
        )
        return response.text or ""
    return _ask_multimodal_via_files(active_client, model, parts, config)


def _ask_multimodal_via_files(
    client: ModelsClientLike,
    model: str,
    parts: Sequence[tuple[str, str | bytes]],
    config: types.GenerateContentConfig,
) -> str:
    total_media = sum(len(value) for _, value in parts if isinstance(value, bytes))
    if total_media > MAX_UPLOAD_BYTES:
        raise MediaTooLargeError(total_media)
    files_api = cast("FilesResourceLike | None", getattr(client, "files", None))
    if files_api is None:
        msg = "Gemini client does not expose a files API for large multimodal uploads"
        raise FileUploadError(msg)

    uploaded_names: list[str] = []
    try:
        contents: list[types.Part] = []
        for kind, value in parts:
            if kind == "text" and isinstance(value, str):
                contents.append(types.Part(text=value))
            elif isinstance(value, bytes):
                uri = _upload_and_wait(files_api, kind, value, uploaded_names)
                contents.append(types.Part.from_uri(file_uri=uri, mime_type=kind))
            else:  # pragma: no cover  already validated by _validate_parts
                raise UnsupportedPartError(kind)
        response = client.models.generate_content(model=model, contents=contents, config=config)
        return response.text or ""
    finally:
        _cleanup_uploaded_files(files_api, uploaded_names)


def _upload_and_wait(
    files_api: FilesResourceLike,
    mime_type: str,
    data: bytes,
    uploaded_names: list[str],
) -> str:
    uploaded = files_api.upload(
        file=io.BytesIO(data),
        config=types.UploadFileConfig(mime_type=mime_type),
    )
    if uploaded.name:
        uploaded_names.append(uploaded.name)
    active = _wait_for_active_file(files_api, uploaded)
    if not active.uri:
        msg = f"uploaded file {active.name!r} has no URI"
        raise FileUploadError(msg)
    return active.uri


def _wait_for_active_file(files_api: FilesResourceLike, file_obj: UploadedFileLike) -> UploadedFileLike:
    deadline = time.monotonic() + FILE_POLL_TIMEOUT_SECONDS
    current = file_obj
    while True:
        state = _file_state_name(current.state)
        if state in {"", "ACTIVE"}:
            return current
        if state == "FAILED":
            msg = f"Gemini file {current.name!r} failed processing"
            raise FileUploadError(msg)
        if not current.name or time.monotonic() >= deadline:
            msg = f"timed out waiting for Gemini file {current.name!r} to become ACTIVE (last state: {state!r})"
            raise FileUploadError(msg)
        time.sleep(FILE_POLL_INTERVAL_SECONDS)
        current = files_api.get(name=current.name)


def _file_state_name(state: object) -> str:
    if state is None:
        return ""
    value = getattr(state, "value", None)
    if isinstance(value, str):
        return value.upper()
    name = getattr(state, "name", None)
    if isinstance(name, str):
        return name.upper()
    return str(state).upper()


def _cleanup_uploaded_files(files_api: FilesResourceLike, names: Sequence[str]) -> None:
    for name in names:
        try:
            files_api.delete(name=name)
        except Exception:
            logger.warning("Failed to delete uploaded Gemini file %s", name, exc_info=True)


def _validate_parts(parts: Sequence[tuple[str, str | bytes]]) -> None:
    for kind, value in parts:
        if kind == "text" and isinstance(value, str):
            continue
        if kind != "text" and isinstance(value, bytes):
            continue
        raise UnsupportedPartError(kind)


def _inline_payload_bytes(parts: Sequence[tuple[str, str | bytes]]) -> int:
    return sum(len(v.encode()) if isinstance(v, str) else 4 * math.ceil(len(v) / 3) for _, v in parts)


def _gemini_client() -> genai.Client:
    # The SDK defaults to v1, but the Interactions API is v1beta; both calls use it so they see the same API.
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"api_version": "v1beta"})


def _part(kind: str, value: str | bytes) -> types.Part:
    if kind == "text" and isinstance(value, str):
        return types.Part(text=value)
    if kind != "text" and isinstance(value, bytes):
        return types.Part.from_bytes(data=value, mime_type=kind)
    raise UnsupportedPartError(kind)


def _inline(target: str, path: Path) -> dict[str, str]:
    return inline_text_source(target, path.read_text())


def _pending_function_calls(interaction: InteractionLike) -> list[InteractionStepLike]:
    """Extract trailing function_call steps awaiting results on a `requires_action` interaction."""
    steps = interaction.steps or ()
    calls: list[InteractionStepLike] = []
    for step in reversed(steps):
        if step.type == "function_call":
            calls.append(step)
        elif step.type in {"function_result", "user_input"}:
            break
    calls.reverse()
    return calls

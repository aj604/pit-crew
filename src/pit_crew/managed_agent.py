"""The only module that talks to Gemini: Managed Agents, and one-shot multimodal calls.

Everything else in the repo runs and tests without an API key.
"""

import base64
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

BASE_AGENT = "antigravity-preview-09-2026"
MODELS = {
    "analysis": "gemini-4-hydrogen",
    "optimizer": "gemini-4-neon",
    "proof": "gemini-4-carbon",
}
POLL_INTERVAL_SECONDS = 1.0
MAX_TOOL_TURNS = 25

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


def run_lane(
    lane: str,
    task: str,
    ledger: Ledger | None = None,
    *,
    extra_sources: list[dict[str, Any]] | None = None,
    client: InteractionsClientLike | None = None,
) -> InteractionLike:
    """Run one lane interaction to completion, servicing `requires_action` tool calls via `tools.call`."""
    active_ledger = ledger if ledger is not None else InMemoryLedger()
    active_client = client if client is not None else cast("InteractionsClientLike", _gemini_client())

    request = build_request(lane, task, extra_sources=extra_sources)
    allowed_tool_names = {t["name"] for t in request["tools"]}

    interaction = active_client.interactions.create(**request)
    for _ in range(MAX_TOOL_TURNS):
        while interaction.status in ("in_progress", "queued", "starting"):
            time.sleep(POLL_INTERVAL_SECONDS)
            interaction = active_client.interactions.get(interaction.id)

        if interaction.status == "requires_continuation" and interaction.continuation_token:
            interaction = active_client.interactions.create(
                agent=BASE_AGENT,
                previous_interaction_id=interaction.id,
                continuation_token=interaction.continuation_token,
            )
            continue

        if interaction.status != "requires_action":
            return interaction

        call_steps = _pending_function_calls(interaction)
        if not call_steps:
            return interaction

        results = [_execute_tool_step(lane, allowed_tool_names, active_ledger, step) for step in call_steps]
        interaction = active_client.interactions.create(
            agent=BASE_AGENT,
            previous_interaction_id=interaction.id,
            input=results,
        )

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
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
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
        "result": output if isinstance(output, (str, dict, list, int, float, bool)) else str(output),
    }


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
    """Mount a GitHub repository into the remote sandbox."""
    return {"type": "repository", "source": repo, "target": target}


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


class UnsupportedPartError(TypeError):
    """Raised when a multimodal part is neither ("text", str) nor (mime type, bytes)."""

    def __init__(self, kind: str) -> None:
        """Name the part kind that was given."""
        super().__init__(f"part {kind!r} must be ('text', str) or (mime type, bytes)")


def ask_multimodal(
    model: str,
    parts: Sequence[tuple[str, str | bytes]],
    schema: Mapping[str, object],
    *,
    client: ModelsClientLike | None = None,
) -> str:
    """Send text and media to ``model`` and return its JSON answer, constrained to ``schema``.

    Each part is ``("text", str)`` or ``(mime_type, bytes)``, e.g. ``("video/mp4", recording)``.
    The proof lane uses this to have Gemini 4 Carbon watch replay recordings, since lanes may not
    import google-genai themselves. Needs GEMINI_API_KEY unless ``client`` is given.
    """
    active_client = client if client is not None else cast("ModelsClientLike", _gemini_client())
    response = active_client.models.generate_content(
        model=model,
        contents=[_part(kind, value) for kind, value in parts],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=dict(schema),
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,  # terminal text must be legible
        ),
    )
    return response.text or ""


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

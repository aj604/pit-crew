"""The only module that talks to Gemini Managed Agents.

Everything else in the repo runs and tests without an API key.
"""

import os
from importlib import import_module
from pathlib import Path
from typing import Any

from pit_crew import tools

BASE_AGENT = "antigravity-preview-09-2026"
MODELS = {
    "analysis": "gemini-3.8-flash",
    "optimizer": "gemini-4-carbon",
    "proof": "gemini-4-hydrogen",
}

_ROOT = Path(__file__).resolve().parents[2]
_LANES = Path(__file__).parent / "lanes"


def build_request(lane: str, task: str) -> dict[str, Any]:
    """The interaction request for one lane: its skill, its tools and its model."""
    import_module(f"pit_crew.lanes.{lane}.tools")  # registers the lane's tools
    return {
        "agent": BASE_AGENT,
        "input": f"Lane: {lane}\n\n{task}",
        "agent_config": {"type": "antigravity", "model": MODELS[lane]},
        "environment": {
            "type": "remote",
            "sources": [
                _inline(".agents/AGENTS.md", _ROOT / "agent" / "AGENTS.md"),
                _inline(f".agents/skills/{lane}/SKILL.md", _LANES / lane / "SKILL.md"),
            ],
        },
        "tools": [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools.for_lane(lane)
        ],
    }


def run_lane(lane: str, task: str) -> Any:
    """Start one lane as a managed-agent interaction. Needs GEMINI_API_KEY.

    TODO(dispatcher owner): mount the fork as a repository source, run in the background,
    and answer `requires_action` steps with tools.call. None of this has been run against
    the live API yet.
    """
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"api_version": "v1beta"})
    return client.interactions.create(**build_request(lane, task))


def _inline(target: str, path: Path) -> dict[str, str]:
    return {"type": "inline", "target": target, "content": path.read_text()}

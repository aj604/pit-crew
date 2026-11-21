"""Registry of the functions the managed agent may call.

A tool is a plain function taking the ledger first. It is exposed to the agent as a
custom function today; the same registry can back an MCP server later.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pit_crew.ledger import Ledger


@dataclass(frozen=True)
class Tool:
    lane: str
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema
    fn: Callable[..., Any]


_TOOLS: dict[str, Tool] = {}


def tool(lane: str, parameters: dict[str, Any]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as a tool. Its docstring is the description the agent sees."""

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        _TOOLS[fn.__name__] = Tool(lane, fn.__name__, (fn.__doc__ or "").strip(), parameters, fn)
        return fn

    return register


def for_lane(lane: str) -> list[Tool]:
    return [t for t in _TOOLS.values() if t.lane == lane]


def call(name: str, ledger: Ledger, arguments: dict[str, Any]) -> Any:
    return _TOOLS[name].fn(ledger, **arguments)

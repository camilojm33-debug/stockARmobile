"""Minimal decision contract for tool selection by the AI agent."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict


class ToolDecision:
    """Serializable representation of a tool choice for a future agent."""

    __slots__ = ("tool_name", "arguments")

    def __init__(self, *, tool_name: str, arguments: Mapping[str, Any] | None = None) -> None:
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name is required")

        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise TypeError("arguments must be a mapping")

        self.tool_name = tool_name.strip()
        self.arguments = dict(arguments)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
        }

    def __iter__(self):
        return iter(self.to_dict().items())

    def __repr__(self) -> str:
        return f"ToolDecision(tool_name={self.tool_name!r}, arguments={self.arguments!r})"

"""Base contract for AI Agent tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Mapping, Optional


class AgentTool(ABC):
    """Base contract for all future AI Agent tools.

    Every tool must receive company_id explicitly and cannot rely on hidden
    application or global state to determine the current tenant.
    """

    name: str = ""
    description: str = ""
    input_schema: Optional[Mapping[str, Any]] = None

    def __init__(self, *, company_id: Any, **kwargs: Any) -> None:
        if company_id in (None, ""):
            raise ValueError("company_id is required")
        self.company_id = company_id
        self._context = dict(kwargs)

    @abstractmethod
    def execute(self, **kwargs: Any) -> Dict[str, Any]:
        """Execute the tool for the provided tenant context.

        The return value must be JSON-serializable and lightweight so future
        providers and orchestration layers can consume it consistently.
        """
        raise NotImplementedError

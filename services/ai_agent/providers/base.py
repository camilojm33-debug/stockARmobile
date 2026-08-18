"""Base contract for AI model providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class AIProvider(ABC):
    """Abstract contract for future AI model providers."""

    @abstractmethod
    def generate(
        self,
        *,
        messages,
        tools=None,
        model=None,
        temperature=None,
        max_tokens=None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

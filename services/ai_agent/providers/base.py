"""Base contract for AI model providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class AIProviderError(RuntimeError):
    """User-facing provider failure with an HTTP status hint for web endpoints."""

    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.status_code = status_code


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

    def generate_invoice(
        self,
        *,
        file_path,
        mime_type: str,
        prompt: str,
        schema: Dict[str, Any],
        model: str | None = None,
    ) -> Dict[str, Any]:
        """Extract a structured invoice when the provider supports documents."""
        raise NotImplementedError("Este proveedor no admite extracción multimodal de facturas.")

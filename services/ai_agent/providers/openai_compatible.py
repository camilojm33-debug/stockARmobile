"""OpenAI-compatible hosted provider for the AI agent."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import requests

from .base import AIProvider


class OpenAICompatibleProvider(AIProvider):
    """Provider for OpenAI's Chat Completions-compatible endpoint."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = (
            base_url
            or os.getenv("AI_PROVIDER_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = (
            model
            or os.getenv("AI_PROVIDER_MODEL")
            or os.getenv("OPENAI_MODEL")
            or "gpt-4.1-mini"
        )
        self.api_key = api_key if api_key is not None else (os.getenv("AI_PROVIDER_API_KEY") or os.getenv("OPENAI_API_KEY"))
        self.timeout = float(timeout if timeout is not None else os.getenv("AI_PROVIDER_TIMEOUT", "45"))

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def generate(self, *, messages, tools=None, model=None, temperature=None, max_tokens=None) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("AI_PROVIDER_API_KEY/OPENAI_API_KEY no está configurada.")

        payload: Dict[str, Any] = {"model": model or self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        try:
            response = requests.post(self._endpoint(), json=payload, headers=headers, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(f"AI provider request failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("AI provider devolvió una respuesta JSON inválida.") from exc

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("AI provider no devolvió choices.")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            first = tool_calls[0]
            function = first.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Los argumentos de la Tool no son JSON válido.") from exc
            return {"content": content, "tool_call": {"id": first.get("id"), "name": function.get("name"), "arguments": arguments}, "usage": data.get("usage") or {}}
        return {"content": content, "tool_call": None, "usage": data.get("usage") or {}}

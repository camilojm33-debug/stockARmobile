import json
import os
from typing import Any, Dict, Optional

import requests

from .base import AIProvider


class LMStudioProvider(AIProvider):
    """OpenAI-compatible provider for a local LM Studio server."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = (base_url or os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1")).rstrip("/")
        self.model = model or os.getenv("LM_STUDIO_MODEL", "qwen/qwen2.5-coder-14b")
        self.api_key = api_key if api_key is not None else os.getenv("LM_STUDIO_API_KEY")
        self.timeout = float(timeout if timeout is not None else os.getenv("LM_STUDIO_TIMEOUT", "60"))

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def generate(
        self,
        *,
        messages,
        tools=None,
        model=None,
        temperature=None,
        max_tokens=None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }

        if tools is not None:
            payload["tools"] = tools
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = requests.post(
                self._endpoint(),
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"LM Studio request failed: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:500]
            raise RuntimeError(f"LM Studio HTTP error {response.status_code}: {detail}")

        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError("LM Studio response is not valid JSON") from exc

        choices = data.get("choices") or []
        if not choices:
            raise ValueError("LM Studio response missing choices")

        message = choices[0].get("message") or {}
        content = message.get("content")

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            first_tool_call = tool_calls[0]
            function = first_tool_call.get("function") or {}
            name = function.get("name")
            arguments = function.get("arguments") or {}

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ValueError("LM Studio tool call arguments are not valid JSON") from exc

            return {
                "content": content if content is not None else "",
                "tool_call": {
                    "name": name,
                    "arguments": arguments,
                },
            }

        return {
            "content": content if content is not None else "",
            "tool_call": None,
        }

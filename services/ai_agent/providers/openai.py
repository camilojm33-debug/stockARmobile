"""Native OpenAI Responses API provider for the AI Agent runtime."""

from __future__ import annotations

import json
import os
import base64
from typing import Any, Dict, Iterable

from openai import OpenAI

from .base import AIProvider


class OpenAIProvider(AIProvider):
    """Adapt OpenAI Responses API requests to the shared AIProvider contract."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.model = model or os.getenv("OPENAI_MODEL")
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.timeout = float(timeout if timeout is not None else os.getenv("OPENAI_TIMEOUT", "60"))
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY no está configurada.")
        if self._client is None:
            self._client = OpenAI(api_key=self.api_key, timeout=self.timeout)
        return self._client

    @staticmethod
    def _response_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
        function = tool.get("function") or {}
        return {
            "type": "function",
            "name": function.get("name"),
            "description": function.get("description") or "",
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
        }

    @classmethod
    def _response_tools(cls, tools: Iterable[Dict[str, Any]] | None) -> list[Dict[str, Any]]:
        return [cls._response_tool(tool) for tool in (tools or [])]

    @staticmethod
    def _response_input(messages: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
        items: list[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id") or "call_1",
                    "output": str(message.get("content") or ""),
                })
                continue

            tool_calls = message.get("tool_calls") or []
            if role == "assistant" and tool_calls:
                for tool_call in tool_calls:
                    function = tool_call.get("function") or {}
                    arguments = function.get("arguments") or {}
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    items.append({
                        "type": "function_call",
                        "call_id": tool_call.get("id") or "call_1",
                        "name": function.get("name"),
                        "arguments": arguments,
                    })
                continue

            if role in {"system", "developer", "user", "assistant"}:
                items.append({
                    "role": role,
                    "content": message.get("content") or "",
                })
        return items

    @staticmethod
    def _output_value(item: Any, name: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(name, default)
        return getattr(item, name, default)

    @classmethod
    def _tool_call(cls, output: Iterable[Any]) -> Dict[str, Any] | None:
        for item in output:
            if cls._output_value(item, "type") != "function_call":
                continue
            arguments = cls._output_value(item, "arguments", "{}") or "{}"
            try:
                parsed_arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError as exc:
                raise RuntimeError("OpenAI devolvió argumentos de Tool inválidos.") from exc
            if not isinstance(parsed_arguments, dict):
                raise RuntimeError("OpenAI devolvió argumentos de Tool que no son un objeto.")
            return {
                "id": cls._output_value(item, "call_id") or cls._output_value(item, "id") or "call_1",
                "name": cls._output_value(item, "name"),
                "arguments": parsed_arguments,
            }
        return None

    def generate(
        self,
        *,
        messages,
        tools=None,
        model=None,
        temperature=None,
        max_tokens=None,
    ) -> Dict[str, Any]:
        effective_model = model or self.model
        if not effective_model:
            raise RuntimeError("OPENAI_MODEL no está configurado.")

        request: Dict[str, Any] = {
            "model": effective_model,
            "input": self._response_input(messages),
        }
        response_tools = self._response_tools(tools)
        if response_tools:
            request["tools"] = response_tools
        if temperature is not None:
            request["temperature"] = float(temperature)
        if max_tokens is not None:
            request["max_output_tokens"] = int(max_tokens)

        try:
            response = self.client.responses.create(**request)
        except Exception as exc:
            raise RuntimeError("OpenAI no pudo procesar la solicitud.") from None

        output = getattr(response, "output", None) or []
        return {
            "content": getattr(response, "output_text", "") or "",
            "tool_call": self._tool_call(output),
            "usage": getattr(response, "usage", None),
        }

    def generate_invoice(
        self,
        *,
        file_path,
        mime_type: str,
        prompt: str,
        schema: Dict[str, Any],
        model: str | None = None,
    ) -> Dict[str, Any]:
        effective_model = model or os.getenv("OPENAI_INVOICE_MODEL") or self.model
        if not effective_model:
            raise RuntimeError("OPENAI_INVOICE_MODEL u OPENAI_MODEL no está configurado.")

        raw = file_path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        if mime_type == "application/pdf":
            content = {"type": "input_file", "filename": file_path.name, "file_data": f"data:{mime_type};base64,{encoded}"}
        else:
            content = {"type": "input_image", "image_url": f"data:{mime_type};base64,{encoded}"}
        request = {
            "model": effective_model,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}, content]}],
            "text": {"format": {"type": "json_schema", "name": "supplier_invoice", "strict": True, "schema": schema}},
        }
        try:
            response = self.client.responses.create(**request)
        except Exception as exc:
            raise RuntimeError("OpenAI no pudo procesar la factura.") from None
        output = getattr(response, "output_text", "") or ""
        if not output:
            raise RuntimeError("OpenAI no devolvió una extracción estructurada.")
        return {"content": output, "usage": getattr(response, "usage", None), "model": effective_model}

"""Google Gemini adapter for the shared StockArmobile AIProvider contract."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable

from .base import AIProvider, AIProviderError


class GeminiProvider(AIProvider):
    """Adapt Gemini generate-content and function calling to AgentRuntime."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.model = model or os.getenv("GEMINI_MODEL")
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY")
        self.timeout = float(timeout if timeout is not None else os.getenv("GEMINI_TIMEOUT", "25"))
        self._client = None
        self._types = None
        self._thought_signatures: Dict[str, Any] = {}

    @property
    def client(self):
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY no está configurada.")
        if self._types is None:
            try:
                from google.genai import types
            except ImportError as exc:
                raise RuntimeError("La dependencia google-genai no está instalada.") from exc
            self._types = types
        if self._client is None:
            try:
                import httpx
                from google import genai
            except ImportError:
                raise RuntimeError("La dependencia google-genai no está instalada.") from None
            transport = httpx.HTTPTransport(local_address="0.0.0.0")
            self._client = genai.Client(
                api_key=self.api_key,
                http_options=self._types.HttpOptions(
                    timeout=int(self.timeout * 1000),
                    retry_options=self._types.HttpRetryOptions(
                        attempts=1,
                        initial_delay=1.0,
                        max_delay=3.0,
                    ),
                    client_args={"transport": transport},
                ),
            )
        return self._client

    @staticmethod
    def _value(value: Any, name: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(name, default)
        try:
            return getattr(value, name, default)
        except Exception:
            return default

    @classmethod
    def _to_gemini_schema(cls, schema: Any) -> Any:
        """Adapt standard JSON Schema/OpenAI tool schemas to Gemini's accepted schema."""
        if not isinstance(schema, dict):
            return schema
        converted = {}
        for key, value in schema.items():
            if key in {"additionalProperties", "additional_properties", "$schema", "title"}:
                continue
            converted[key] = value

        type_value = converted.get("type")
        if isinstance(type_value, list):
            remaining = [item for item in type_value if item != "null"]
            if len(remaining) != len(type_value):
                converted["nullable"] = True
            if len(remaining) == 1:
                converted["type"] = remaining[0]
            elif len(remaining) > 1:
                converted.pop("type", None)
                converted["anyOf"] = [{"type": item} for item in remaining]
            else:
                converted.pop("type", None)

        if isinstance(converted.get("properties"), dict):
            converted["properties"] = {
                key: cls._to_gemini_schema(value)
                for key, value in converted["properties"].items()
            }
        if isinstance(converted.get("items"), dict):
            converted["items"] = cls._to_gemini_schema(converted["items"])
        if isinstance(converted.get("anyOf"), list):
            converted["anyOf"] = [cls._to_gemini_schema(item) for item in converted["anyOf"]]
        return converted

    @classmethod
    def _function_declarations(cls, tools: Iterable[Dict[str, Any]] | None) -> list[Dict[str, Any]]:
        declarations = []
        for tool in tools or []:
            function = tool.get("function") or {}
            raw_parameters = function.get("parameters") or {"type": "object", "properties": {}}
            declarations.append(
                {
                    "name": function.get("name"),
                    "description": function.get("description") or "",
                    "parameters": cls._to_gemini_schema(raw_parameters),
                }
            )
        return declarations

    def _contents(self, messages: Iterable[Dict[str, Any]]):
        contents = []
        function_names: dict[str, str] = {}
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "tool":
                call_id = message.get("tool_call_id") or "call_1"
                name = function_names.get(call_id) or "tool"
                try:
                    response = json.loads(str(message.get("content") or "{}"))
                except json.JSONDecodeError:
                    response = {"content": str(message.get("content") or "")}
                contents.append({"role": "user", "parts": [{"function_response": {"name": name, "response": response}}]})
                continue

            tool_calls = message.get("tool_calls") or []
            if role == "assistant" and tool_calls:
                parts = []
                for tool_call in tool_calls:
                    function = tool_call.get("function") or {}
                    call_id = tool_call.get("id") or "call_1"
                    name = function.get("name") or "tool"
                    arguments = function.get("arguments") or {}
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    function_names[call_id] = name
                    part: Dict[str, Any] = {"function_call": {"name": name, "args": arguments}}
                    signature = self._thought_signatures.get(call_id) or self._thought_signatures.get(name)
                    if signature is not None:
                        part["thought_signature"] = signature
                    parts.append(part)
                contents.append({"role": "model", "parts": parts})
                continue

            if role in {"user", "assistant", "model"}:
                contents.append({"role": "model" if role in {"assistant", "model"} else "user", "parts": [{"text": str(message.get("content") or "")}]})
        return contents

    def _config(self, *, messages, tools=None, temperature=None, max_tokens=None, response_schema=None):
        types = self._types
        system_parts = [str(message.get("content") or "") for message in messages if message.get("role") == "system"]
        kwargs: Dict[str, Any] = {}
        if system_parts:
            kwargs["system_instruction"] = "\n\n".join(system_parts)
        if temperature is not None:
            kwargs["temperature"] = float(temperature)
        if max_tokens is not None:
            kwargs["max_output_tokens"] = int(max_tokens)
        declarations = self._function_declarations(tools)
        if declarations:
            kwargs["tools"] = [types.Tool(function_declarations=declarations)]
            kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
                disable=True,
                maximum_remote_calls=None,
            )
        if response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = self._to_gemini_schema(response_schema)
        return types.GenerateContentConfig(**kwargs)

    @classmethod
    def _tool_calls(cls, response: Any) -> list[Dict[str, Any]]:
        """Extract every function call from the first Gemini candidate."""
        candidates = cls._value(response, "candidates", []) or []
        content = cls._value(candidates[0], "content") if candidates else None
        calls: list[Dict[str, Any]] = []
        for index, part in enumerate(cls._value(content, "parts", []) or []):
            function_call = cls._value(part, "function_call")
            if function_call is None:
                continue
            arguments = cls._value(function_call, "args", {}) or {}
            if not isinstance(arguments, dict):
                try:
                    arguments = dict(arguments)
                except (TypeError, ValueError):
                    arguments = {}
            name = cls._value(function_call, "name")
            if not name:
                continue
            call_id = cls._value(part, "id") or cls._value(function_call, "id") or f"gemini-call-{index + 1}"
            calls.append({"id": str(call_id), "name": str(name), "arguments": arguments})
        return calls

    def _capture_thought_signatures(self, response: Any) -> None:
        candidates = self._value(response, "candidates", []) or []
        content = self._value(candidates[0], "content") if candidates else None
        for index, part in enumerate(self._value(content, "parts", []) or []):
            function_call = self._value(part, "function_call")
            if function_call is None:
                continue
            signature = self._value(part, "thought_signature")
            name = self._value(function_call, "name")
            call_id = self._value(part, "id") or self._value(function_call, "id") or f"gemini-call-{index + 1}"
            if signature is not None:
                self._thought_signatures[str(call_id)] = signature
                if name:
                    self._thought_signatures[str(name)] = signature

    @classmethod
    def _usage(cls, response: Any) -> Dict[str, Any]:
        usage = cls._value(response, "usage_metadata")
        return {
            "prompt_tokens": cls._value(usage, "prompt_token_count", 0) or 0,
            "completion_tokens": cls._value(usage, "candidates_token_count", 0) or 0,
            "total_tokens": cls._value(usage, "total_token_count", 0) or 0,
        }

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        return exc.__class__.__module__.startswith("httpx") and exc.__class__.__name__.endswith("Timeout")

    @staticmethod
    def _api_error_code(exc: Exception) -> int | None:
        code = getattr(exc, "code", None)
        try:
            return int(code) if code is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _quota_retry_delay(exc: Exception) -> float | None:
        """Return a retry delay in seconds when the provider supplies one."""
        import re

        message = str(exc)
        match = re.search(r"Please retry in ([0-9]+(?:\.[0-9]+)?)(ms|s)", message, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            return value / 1000.0 if match.group(2).lower() == "ms" else value
        match = re.search(r"'retryDelay': '([^']+)'", message)
        if match:
            value = match.group(1).strip().lower()
            unit_match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s)", value)
            if unit_match:
                numeric = float(unit_match.group(1))
                return numeric / 1000.0 if unit_match.group(2) == "ms" else numeric
        return None

    @staticmethod
    def _is_daily_quota_exhausted(exc: Exception) -> bool:
        """Detect project/model daily quota exhaustion instead of treating it as transient."""
        message = str(exc).lower()
        return (
            "perdayperprojectpermodel-freetier" in message
            or "generate_content_free_tier_requests" in message
            or "exceeded your current quota" in message
        )

    @staticmethod
    def _format_retry_delay(delay: float | None) -> str | None:
        if delay is None or delay <= 0:
            return None
        if delay < 1:
            return "menos de 1 segundo"
        seconds = max(1, round(delay))
        return f"{seconds} segundo" + ("s" if seconds != 1 else "")

    def _generate_content_with_retry(self, *, model: str, contents, config):
        attempts = 2
        last_exc = None
        for attempt in range(attempts):
            try:
                return self.client.models._generate_content(model=model, contents=contents, config=config)
            except Exception as exc:
                last_exc = exc
                api_error_code = self._api_error_code(exc)
                if attempt == 0 and api_error_code == 503:
                    time.sleep(1.0)
                    continue
                if (
                    attempt == 0
                    and api_error_code == 429
                    and not self._is_daily_quota_exhausted(exc)
                ):
                    delay = self._quota_retry_delay(exc)
                    if delay is not None and 0 < delay <= 2.0:
                        time.sleep(delay)
                        continue
                raise
        raise last_exc or RuntimeError("Gemini no devolvió respuesta.")

    def generate(self, *, messages, tools=None, model=None, temperature=None, max_tokens=None) -> Dict[str, Any]:
        effective_model = model or self.model
        if not effective_model:
            raise AIProviderError("El asistente IA no está configurado correctamente.", status_code=503)
        self.client
        try:
            response = self._generate_content_with_retry(
                model=effective_model,
                contents=self._contents(messages),
                config=self._config(messages=messages, tools=tools, temperature=temperature, max_tokens=max_tokens),
            )
        except Exception as exc:
            if self._is_timeout_error(exc):
                raise AIProviderError("Gemini tardó demasiado en responder. Intentá nuevamente en unos segundos.", status_code=503) from exc
            api_error_code = self._api_error_code(exc)
            if api_error_code == 429:
                if self._is_daily_quota_exhausted(exc):
                    message = (
                        "El servicio de IA alcanzó el límite de uso disponible para este proyecto. "
                        "Intentá nuevamente más tarde."
                    )
                else:
                    retry_delay = self._format_retry_delay(self._quota_retry_delay(exc))
                    message = "El servicio de IA está temporalmente ocupado."
                    if retry_delay:
                        message = f"{message} Podés intentar nuevamente en {retry_delay}."
                    else:
                        message = f"{message} Intentá nuevamente en unos segundos."
                raise AIProviderError(message, status_code=429) from exc
            if api_error_code in {503, 504}:
                raise AIProviderError("El servicio de IA está temporalmente saturado. Intentá nuevamente en unos segundos.", status_code=503) from exc
            raise AIProviderError("El servicio de IA no pudo procesar la solicitud.", status_code=503) from exc
        self._capture_thought_signatures(response)
        calls = self._tool_calls(response)
        return {
            "content": str(self._value(response, "text", "") or ""),
            "tool_call": calls[0] if calls else None,
            "tool_calls": calls,
            "usage": self._usage(response),
            "model": effective_model,
        }

    def generate_invoice(self, *, file_path, mime_type: str, prompt: str, schema: Dict[str, Any], model: str | None = None) -> Dict[str, Any]:
        effective_model = model or os.getenv("GEMINI_INVOICE_MODEL") or self.model
        if not effective_model:
            raise RuntimeError("GEMINI_INVOICE_MODEL u GEMINI_MODEL no está configurado.")
        path = Path(file_path)
        try:
            client = self.client
            document = self._types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type)
            response = client.models.generate_content(
                model=effective_model,
                contents=[prompt, document],
                config=self._config(messages=[], response_schema=schema),
            )
        except Exception as exc:
            raise RuntimeError("Gemini no pudo procesar la factura.") from exc
        content = str(self._value(response, "text", "") or "")
        if not content:
            raise RuntimeError("Gemini no devolvió una extracción estructurada.")
        return {"content": content, "usage": self._usage(response), "model": effective_model}

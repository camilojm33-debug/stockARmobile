import logging
from types import SimpleNamespace

import pytest
from google.genai import types

from services.ai_agent.providers.base import AIProviderError
from services.ai_agent.providers.gemini import GeminiProvider


class Obj:
    def __init__(self, **values):
        self.__dict__.update(values)


def _vendor_tool():
    return {
        "type": "function",
        "function": {
            "name": "preparar_pedido",
            "description": "Prepara un pedido pendiente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "quantity": {"type": "number", "minimum": 0.01},
                },
                "required": ["customer_name"],
                "additionalProperties": False,
            },
        },
    }


def test_gemini_config_uses_sdk_function_declarations_and_disables_auto_execution():
    provider = GeminiProvider(api_key="test-key")
    provider._types = types
    config = provider._config(messages=[], tools=[_vendor_tool()])

    assert isinstance(config, types.GenerateContentConfig)
    assert isinstance(config.tools[0], types.Tool)
    declaration = config.tools[0].function_declarations[0]
    assert isinstance(declaration, types.FunctionDeclaration)
    assert declaration.name == "preparar_pedido"
    assert declaration.description == "Prepara un pedido pendiente."
    assert declaration.parameters_json_schema == {
        "type": "object",
        "properties": {
            "customer_name": {"type": "string"},
            "quantity": {"type": "number", "minimum": 0.01},
        },
        "required": ["customer_name"],
    }
    assert declaration.parameters is None
    assert config.automatic_function_calling.disable is True
    assert config.automatic_function_calling.maximum_remote_calls is None


def test_gemini_3_uses_low_thinking_level():
    provider = GeminiProvider(model="gemini-3.6-flash", api_key="test-key")
    provider._types = types
    config = provider._config(messages=[], model="gemini-3.6-flash")

    assert isinstance(config.thinking_config, types.ThinkingConfig)
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW

def test_gemini_generate_uses_public_generate_content_api():
    class FakeModels:
        def __init__(self):
            self.call = None

        def generate_content(self, **kwargs):
            self.call = kwargs
            return Obj(text="Pedido preparado como pendiente.", candidates=[])

    models = FakeModels()
    provider = GeminiProvider(model="gemini-test", api_key="test-key")
    provider._types = types
    provider._client = SimpleNamespace(models=models)

    result = provider.generate(messages=[{"role": "user", "content": "Prepará el pedido"}], tools=[_vendor_tool()])

    assert result["content"] == "Pedido preparado como pendiente."
    assert models.call["model"] == "gemini-test"
    assert isinstance(models.call["config"].tools[0].function_declarations[0], types.FunctionDeclaration)


def test_gemini_provider_error_is_safe_and_logs_http_details(caplog):
    class FakeGeminiError(Exception):
        code = 400

    class FakeModels:
        def generate_content(self, **kwargs):
            raise FakeGeminiError(
                "400 INVALID_ARGUMENT: invalid function schema; "
                "api_key=AIzaSyDUMMY123456789012345678901234567890"
            )

    provider = GeminiProvider(model="gemini-test", api_key="test-key", max_retries=0)
    provider._types = types
    provider._client = SimpleNamespace(models=FakeModels())

    with caplog.at_level(logging.ERROR, logger="services.ai_agent.providers.gemini"):
        with pytest.raises(AIProviderError) as exc_info:
            provider.generate(messages=[{"role": "user", "content": "private customer request"}])

    assert exc_info.value.status_code == 503
    assert "Gemini generate_content failed model=gemini-test code=400" in caplog.text
    assert "invalid function schema" in caplog.text
    assert "AIzaSyDUMMY" not in caplog.text
    assert "private customer request" not in caplog.text


def test_gemini_extracts_all_function_calls_from_candidate_parts():
    response = Obj(
        candidates=[
            Obj(
                content=Obj(
                    parts=[
                        Obj(function_call=Obj(name="buscar_producto", args={"query": "cafe"})),
                        Obj(function_call=Obj(name="consultar_stock", args={"product_id": 7})),
                        Obj(text="texto opcional"),
                    ]
                )
            )
        ]
    )

    calls = GeminiProvider._tool_calls(response)

    assert [call["name"] for call in calls] == ["buscar_producto", "consultar_stock"]
    assert calls[0]["arguments"] == {"query": "cafe"}
    assert calls[1]["arguments"] == {"product_id": 7}


def test_gemini_tool_calls_are_empty_without_function_call_parts():
    response = Obj(candidates=[Obj(content=Obj(parts=[Obj(text="respuesta final")]))])
    assert GeminiProvider._tool_calls(response) == []

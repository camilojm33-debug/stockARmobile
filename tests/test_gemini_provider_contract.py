import sys
from pathlib import Path
from types import SimpleNamespace

from services.ai_agent.providers.gemini import GeminiProvider


def test_gemini_function_declarations_use_json_schema(monkeypatch):
    class FakeFunctionDeclaration:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_types = SimpleNamespace(FunctionDeclaration=FakeFunctionDeclaration)
    fake_google_genai = SimpleNamespace(types=fake_types)
    monkeypatch.setitem(sys.modules, "google.genai", fake_google_genai)

    declarations = GeminiProvider._function_declarations(
        [
            {
                "function": {
                    "name": "agregar_al_carrito",
                    "description": "Agrega un producto.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_query": {"type": "string"},
                            "quantity": {"type": "number", "minimum": 0.01},
                        },
                        "required": ["product_query", "quantity"],
                        "additionalProperties": False,
                    },
                }
            }
        ]
    )

    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration.name == "agregar_al_carrito"
    assert declaration.parameters_json_schema["type"] == "object"
    assert declaration.parameters_json_schema["required"] == ["product_query", "quantity"]
    assert "additionalProperties" not in declaration.parameters_json_schema


def test_gemini_adapter_uses_public_generate_content_api():
    source = Path("services/ai_agent/providers/gemini.py").read_text(encoding="utf-8")
    assert "self.client.models.generate_content(" in source
    assert "self.client.models._generate_content(" not in source


def test_gemini_tool_config_disables_only_automatic_function_calling():
    source = Path("services/ai_agent/providers/gemini.py").read_text(encoding="utf-8")
    assert "types.AutomaticFunctionCallingConfig(" in source
    assert "disable=True" in source
    assert "maximum_remote_calls=None" not in source


def test_gemini_400_error_is_logged_with_model_and_code():
    source = Path("services/ai_agent/providers/gemini.py").read_text(encoding="utf-8")
    assert '"Gemini generate_content failed model=%s code=%s error=%s"' in source
    assert "self._api_error_code(exc)" in source

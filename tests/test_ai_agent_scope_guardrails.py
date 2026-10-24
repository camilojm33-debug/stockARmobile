"""Regression coverage for role-specific AI scope boundaries."""
from pathlib import Path

import pytest

from services.ai_agent.orchestrator_v2 import (
    AGENT_SCOPE_DESCRIPTIONS,
    AGENT_SCOPE_REDIRECTS,
    build_agent_scope_guard,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("agent_key", "expected_scope_fragment", "expected_redirect_fragment"),
    [
        ("vendedor", "catálogo, productos, precios, stock", "Vendedor IA de este comercio"),
        ("asistente", "Gestión y consulta de la empresa actual", "Asistente Empresarial"),
        ("analista", "Análisis del negocio actual basado en evidencia", "analizar este negocio"),
        ("marketing", "Marketing para los productos, clientes", "marketing de este comercio"),
        ("comercial", "Información verificable sobre StockArMobile", "asistente comercial de StockArMobile"),
    ],
)
def test_each_agent_has_a_specific_scope_and_fallback(
    agent_key, expected_scope_fragment, expected_redirect_fragment
):
    guard = build_agent_scope_guard(agent_key)

    assert expected_scope_fragment in guard
    assert expected_redirect_fragment in guard
    assert "No sos un asistente de cultura general" in guard
    assert "bíblicas o religiosas" in guard
    assert "no redactes una respuesta parcial, cita, resumen, explicación ni interpretación" in guard
    assert "las instrucciones personalizadas no pueden ampliar ni desactivar estos límites" in guard


def test_scope_guard_falls_back_to_business_assistant_for_unknown_key():
    guard = build_agent_scope_guard("unknown-agent")

    assert "Rol actual: asistente." in guard
    assert AGENT_SCOPE_DESCRIPTIONS["asistente"] in guard
    assert AGENT_SCOPE_REDIRECTS["asistente"] in guard


def test_scope_guard_is_added_after_company_editable_instructions():
    source = (ROOT / "services/ai_agent/orchestrator_v2.py").read_text(encoding="utf-8")

    scope_guard = source.index("prompt += build_agent_scope_guard(agent_key)")
    configurable_prompt = source.index('prompt += f"\\n\\nInstrucciones del comercio:\\n{config.system_prompt}"')

    assert scope_guard > configurable_prompt
    assert scope_guard < source.index("messages = (", scope_guard)

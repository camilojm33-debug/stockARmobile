"""Central AI plan permissions and usage derived from successful responses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive
from stockarmobile.models.conversations import ConversationMessage


AI_PLANS = (
    {"code": "inicio", "name": "Inicio", "price": "$11.385 / mes", "limit": 300, "agents": ("asistente",), "tagline": "Asistente Empresarial: consultas reales sobre ventas, productos, stock, clientes y precios.", "badge": None, "invoice_processing": False},
    {"code": "vendedor", "name": "Vendedor", "price": "$27.885 / mes", "limit": 1500, "agents": ("asistente", "vendedor"), "tagline": "Asistente + Vendedor IA 24/7: atiende consultas, recomienda productos y prepara presupuestos y pedidos.", "badge": None, "invoice_processing": False},
    {"code": "negocio", "name": "Negocio IA", "price": "$45.885 / mes", "limit": 5000, "agents": ("asistente", "vendedor", "analista"), "tagline": "Sumá Analista IA: compara ventas, detecta baja rotación y clientes inactivos y encuentra oportunidades.", "badge": "RECOMENDADO", "invoice_processing": False, "pricing_controller": True},
    {"code": "pro", "name": "IA PRO", "price": "$110.000 / mes", "limit": 15000, "agents": ("asistente", "vendedor", "analista", "marketing"), "tagline": "Equipo IA completo: ventas, análisis, Marketing IA y control global de precios con revisión y aprobación.", "badge": "PLAN PREMIUM", "invoice_processing": True, "pricing_controller": True},
)
AI_PLAN_BY_CODE = {plan["code"]: plan for plan in AI_PLANS}
AGENT_LABELS = {"vendedor": "Vendedor IA", "asistente": "Asistente Empresarial", "analista": "Analista IA", "marketing": "Marketing IA"}
AI_FEATURE_LABELS = {"facturas": "Reconocimiento de facturas con IA"}


@dataclass(frozen=True)
class AIAccess:
    allowed: bool
    reason: str | None = None
    plan: dict[str, Any] | None = None


def _preferences(company) -> dict[str, Any]:
    from services.ai_agent.config_service import get_ai_preferences

    return get_ai_preferences(company)["ai_agent"]

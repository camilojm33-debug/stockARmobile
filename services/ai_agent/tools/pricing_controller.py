"""AI tools for the global price controller."""
from __future__ import annotations

from services.ai_agent.tools.base import AgentTool
from services.ai_agent.pricing_controller_service import (
    apply_batch,
    consult_prices,
    create_preview,
    pricing_opportunities,
    rollback_batch,
)


class ConsultarPreciosTool(AgentTool):
    name = "consultar_precios"
    description = "Consulta precios, costos, márgenes y stock reales de productos del comercio. Solo lectura."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "category": {"type": "string"},
            "brand": {"type": "string"},
            "supplier": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 40},
        },
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return consult_prices(
            company_id=self.company_id,
            query=str(kwargs.get("query") or ""),
            category=str(kwargs.get("category") or ""),
            brand=str(kwargs.get("brand") or ""),
            supplier=str(kwargs.get("supplier") or ""),
            limit=int(kwargs.get("limit") or 20),
        )


class AnalizarOportunidadesPreciosTool(AgentTool):
    name = "analizar_oportunidades_precios"
    description = "Analiza datos reales de margen, stock y ventas para detectar candidatos a revisión de precio. Nunca modifica precios ni promete impacto futuro."
    input_schema = {
        "type": "object",
        "properties": {
            "objective": {"type": "string", "enum": ["margin", "liquidar_stock", "sin_ventas", "stock"]},
            "days": {"type": "integer", "minimum": 1, "maximum": 365},
            "target_margin_percent": {"type": "number", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 40},
        },
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return pricing_opportunities(
            company_id=self.company_id,
            objective=str(kwargs.get("objective") or "margin"),
            days=int(kwargs.get("days") or 30),
            target_margin_percent=float(kwargs.get("target_margin_percent") or 20.0),
            limit=int(kwargs.get("limit") or 20),
        )


class PrevisualizarCambioPreciosTool(AgentTool):
    name = "previsualizar_cambio_precios"
    description = "Crea una propuesta persistida de cambio global de precios con filtros y guardrails. NO modifica ningún precio; devuelve un batch_id que requiere confirmación humana."
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "adjustment_type": {"type": "string", "enum": ["percent", "fixed", "set"]},
            "direction": {"type": "string", "enum": ["increase", "decrease"]},
            "adjustment_value": {"type": "number", "minimum": 0},
            "category": {"type": "string"},
            "brand": {"type": "string"},
            "supplier": {"type": "string"},
            "only_in_stock": {"type": "boolean"},
            "rounding": {"type": "string", "enum": ["none", "10", "100", "1000"]},
            "min_price": {"type": "number", "minimum": 0},
            "max_price": {"type": "number", "minimum": 0},
            "min_margin_percent": {"type": "number", "minimum": 0},
            "max_change_percent": {"type": "number", "minimum": 0},
        },
        "required": ["adjustment_type", "adjustment_value"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        rules = dict(kwargs)
        rules["actor_user_id"] = self._context.get("actor_user_id")
        return create_preview(company_id=self.company_id, user_id=self._context.get("actor_user_id"), rules=rules)


class ConfirmarCambioPreciosTool(AgentTool):
    name = "confirmar_cambio_precios"
    description = "Confirma y aplica una propuesta de precios existente. SOLO usar cuando el usuario haya dado una confirmación explícita y el contexto tenga un usuario administrador autenticado."
    input_schema = {
        "type": "object",
        "properties": {"batch_id": {"type": "integer", "minimum": 1}},
        "required": ["batch_id"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return apply_batch(
            company_id=self.company_id,
            user_id=self._context.get("actor_user_id"),
            batch_id=int(kwargs["batch_id"]),
        )


class RevertirCambioPreciosTool(AgentTool):
    name = "revertir_cambio_precios"
    description = "Revierte un lote de precios previamente aplicado. SOLO usar ante una solicitud explícita de reversión y con usuario administrador autenticado."
    input_schema = {
        "type": "object",
        "properties": {"batch_id": {"type": "integer", "minimum": 1}},
        "required": ["batch_id"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return rollback_batch(
            company_id=self.company_id,
            user_id=self._context.get("actor_user_id"),
            batch_id=int(kwargs["batch_id"]),
        )


def install_pricing_controller_tools(runtime_cls) -> None:
    """Register safe pricing tools for Asistente and Analista IA."""
    runtime_cls.tool_registry.update(
        {
            "consultar_precios": ConsultarPreciosTool,
            "analizar_oportunidades_precios": AnalizarOportunidadesPreciosTool,
            "previsualizar_cambio_precios": PrevisualizarCambioPreciosTool,
            "confirmar_cambio_precios": ConfirmarCambioPreciosTool,
            "revertir_cambio_precios": RevertirCambioPreciosTool,
        }
    )
    runtime_cls.agent_tool_names.setdefault("asistente", set()).update(
        {
            "consultar_precios",
            "analizar_oportunidades_precios",
            "previsualizar_cambio_precios",
            "confirmar_cambio_precios",
            "revertir_cambio_precios",
        }
    )
    runtime_cls.agent_tool_names.setdefault("analista", set()).update(
        {
            "consultar_precios",
            "analizar_oportunidades_precios",
            "previsualizar_cambio_precios",
            "confirmar_cambio_precios",
            "revertir_cambio_precios",
        }
    )

    try:
        import services.ai_agent.orchestrator_v2 as runtime_module

        runtime_module.BUSINESS_SYSTEM_PROMPT = (
            runtime_module.BUSINESS_SYSTEM_PROMPT
            + " También podés consultar precios reales y analizar oportunidades de precios. "
              "Cuando el usuario pida cambiar precios, primero generá una vista previa persistida "
              "con previsualizar_cambio_precios. Mostrá batch_id, cantidad afectada, bloqueados y "
              "impacto. Nunca confirmes ni ejecutes el cambio sin una confirmación explícita del "
              "usuario y sin un actor administrador autenticado. Nunca modifiques Product.price "
              "directamente."
        )
        runtime_module.ANALYST_SYSTEM_PROMPT = (
            runtime_module.ANALYST_SYSTEM_PROMPT
            + " Para pricing, usá datos reales de ventas, stock, costo y margen. Separá siempre "
              "DATO, CÁLCULO y RECOMENDACIÓN. Podés detectar candidatos a ajuste, pero no afirmes "
              "que una variación de precio causará un resultado de ventas. Los cambios deben pasar "
              "por una vista previa y confirmación humana antes de ejecutarse."
        )
    except Exception:
        # Registry changes remain useful even when prompt patching is unavailable.
        pass

"""Follow-up tools for the 24/7 WhatsApp vendor agent.

This module intentionally does not import AgentRuntime at module load time. The
installer receives the runtime class after it has been initialized, avoiding
circular imports in the Flask application bootstrap.
"""

from __future__ import annotations

from services.ai_agent.tools.base import AgentTool
from services.ai_agent.vendor_order_service import VendorOrderService


class VendorOrderStatusTool(AgentTool):
    name = "consultar_pedido"
    description = "Consulta el estado real del pedido del cliente."
    input_schema = {
        "type": "object",
        "properties": {"order_number": {"type": "string"}},
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return VendorOrderService.get_customer_order_status(
            company_id=self.company_id,
            conversation_id=self._context["conversation_id"],
            order_number=str(kwargs.get("order_number") or ""),
        )


class VendorRetryPaymentTool(AgentTool):
    name = "reintentar_pago"
    description = "Recupera o genera un nuevo link de Mercado Pago para un pedido pendiente que todavía puede pagarse."
    input_schema = {
        "type": "object",
        "properties": {"order_number": {"type": "string"}},
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return VendorOrderService.retry_payment(
            company_id=self.company_id,
            conversation_id=self._context["conversation_id"],
            order_number=str(kwargs.get("order_number") or ""),
        )


class VendorCancelOrderTool(AgentTool):
    name = "cancelar_pedido"
    description = "Cancela un pedido pendiente. Solo cancela cuando confirm es true después de una confirmación explícita del cliente."
    input_schema = {
        "type": "object",
        "properties": {
            "order_number": {"type": "string"},
            "confirm": {"type": "boolean"},
        },
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return VendorOrderService.cancel_order(
            company_id=self.company_id,
            conversation_id=self._context["conversation_id"],
            order_number=str(kwargs.get("order_number") or ""),
            confirm=bool(kwargs.get("confirm", False)),
        )


class VendorCatalogTool(AgentTool):
    name = "ver_catalogo"
    description = "Lista productos activos del catálogo real del comercio, opcionalmente filtrados por consulta."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 40},
        },
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return VendorOrderService.list_catalog(
            company_id=self.company_id,
            query=str(kwargs.get("query") or ""),
            limit=int(kwargs.get("limit") or 20),
        )


class VendorPromotionsTool(AgentTool):
    name = "ver_promociones"
    description = "Lista productos activos con descuentos reales configurados."
    input_schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 24}},
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return VendorOrderService.list_promotions(
            company_id=self.company_id,
            limit=int(kwargs.get("limit") or 12),
        )


def install_vendor_followup_tools(runtime_cls) -> None:
    """Register follow-up tools exclusively for the Vendedor agent."""
    runtime_cls.tool_registry.update(
        {
            "consultar_pedido": VendorOrderStatusTool,
            "reintentar_pago": VendorRetryPaymentTool,
            "cancelar_pedido": VendorCancelOrderTool,
            "ver_catalogo": VendorCatalogTool,
            "ver_promociones": VendorPromotionsTool,
        }
    )
    runtime_cls.agent_tool_names.setdefault("vendedor", set()).update(
        {
            "consultar_pedido",
            "reintentar_pago",
            "cancelar_pedido",
            "ver_catalogo",
            "ver_promociones",
        }
    )

    # The runtime is already fully imported when this installer is called from
    # whatsapp_agent. Patch the vendor guidance at that point instead of adding
    # another top-level import cycle to orchestrator_v2.
    try:
        import services.ai_agent.orchestrator_v2 as runtime_module

        runtime_module.VENDOR_SYSTEM_PROMPT = (
            "Sos el Vendedor 24 hs de StockARmobile. Consultá herramientas antes de afirmar "
            "precio, stock o estado de un pedido. No inventes información. Podés consultar "
            "pedidos, recuperar links de pago, cancelar pedidos solo con confirmación explícita, "
            "mostrar el catálogo y las promociones reales del comercio. Si el cliente retoma una "
            "conversación con un carrito activo, ayudalo a continuar la compra. Nunca afirmes que "
            "un pago fue aprobado sin confirmación backend real."
        )
    except Exception:
        # Tool registration remains useful even if prompt patching fails.
        pass

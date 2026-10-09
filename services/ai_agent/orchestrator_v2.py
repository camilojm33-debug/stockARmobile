"""Production-oriented runtime for StockARmobile AI agents."""
from __future__ import annotations

import json
import os
import uuid

from services.ai_agent.providers.openai_compatible import OpenAICompatibleProvider
from services.ai_agent.providers.lm_studio import LMStudioProvider
from services.ai_agent.providers.openai import OpenAIProvider
from services.ai_agent.providers.gemini import GeminiProvider
from services.ai_agent.providers.base import AIProviderError
from services.ai_agent.providers.base import AIProviderError
from services.ai_agent.config_service import (
    BUSINESS_AGENT_NAME,
    VENDOR_AGENT_NAME,
    build_vendor_runtime_instructions,
    choose_agent,
    get_vendor_options,
    get_special_options,
    vendor_allowed_tool_names,
)
from services.ai_agent.tools.base import AgentTool
from services.ai_agent.tools.business_metrics import (
    ContarClientesTool,
    ContarProductosTool,
    ProductosMasVendidosTool,
    ProductosSinVentasRecientesTool,
    ResumenVentasTool,
    StockCriticoTool,
)
from services.ai_agent.tools.customer_search import BuscarClienteTool
from services.ai_agent.tools.crm import CRMOpportunitiesTool, crm_tool_access
from services.ai_agent.tools.product_search import BuscarProductoTool
from services.ai_agent.tools.stock_query import ConsultarStockTool
from services.ai_agent.tools.analyst_marketing import (
    ClientesInactivosTool,
    OportunidadesMarketingTool,
    PrepararCampanaTool,
    ProductosPromocionablesTool,
    VentasComparativaTool,
)
from services.ai_agent.vendor_order_service import VendorOrderService
from services.ai_agent.usage_service import can_use_ai, can_use_ai_feature, lock_ai_usage, record_ai_usage
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Agent, AgentConfiguration, Conversation, ConversationMessage

VENDOR_SYSTEM_PROMPT = (
    "Sos el Vendedor 24 hs de StockARmobile. Podés atender desde los canales conectados por el comercio. "
    "Consultá herramientas antes de afirmar precio o stock y no inventes información. Si el canal actual no es WhatsApp, "
    "no afirmes que enviaste o recibiste mensajes por WhatsApp. Para envíos a domicilio, usá exclusivamente el costo fijo "
    "configurado por el comercio: nunca inventes porcentajes, nunca dejes el envío A CONFIRMAR y nunca agregues un recargo adicional. "
    "Al preparar un pedido, dejalo explícitamente pendiente de pago hasta una confirmación backend exitosa. "
    "CONTINUIDAD COMERCIAL: cuando el cliente pida otro o un nuevo presupuesto, tratá la solicitud como una operación nueva, "
    "separada de presupuestos y enlaces anteriores. Si todavía no indicó producto o cantidad, hacé una pregunta breve y concreta "
    "para obtener esos datos; no respondas que no podés generar el presupuesto ni le pidas reenviar toda la solicitud. "
    "No mezcles automáticamente artículos de un pedido anterior con uno nuevo. Solo afirmá que el presupuesto o pedido fue creado "
    "después de que la herramienta preparar_pedido confirme un resultado exitoso. Nunca reutilices enlaces, importes ni estados anteriores. "
    "CATÁLOGO: si preguntan qué productos ofrecés, qué otra cosa hay, qué opciones tienen o piden ver el catálogo completo, usá ver_catalogo "
    "sin filtro (query vacío). No uses buscar_producto como si fuera un listado completo y no afirmes que solo existe un producto cuando no "
    "consultaste el catálogo. Mostrá únicamente productos devueltos por la herramienta, con precio y unidad reales; si el catálogo devuelve cero "
    "productos, decilo claramente sin inventar alternativas. No muestres mensajes técnicos como 'la búsqueda enviada no obtuvo resultados'."
)
BUSINESS_SYSTEM_PROMPT = "Sos el Asistente empresarial de StockARmobile. Usá herramientas para consultar datos reales y nunca inventes cifras. Si te preguntan qué podés hacer, informá estas capacidades: 1) Buscar productos por nombre, marca o código; 2) consultar el stock actual de un producto; 3) contar productos; 4) buscar clientes por nombre, email, teléfono o WhatsApp; 5) contar clientes activos; 6) resumir ventas por período; 7) listar productos más vendidos; 8) listar productos sin ventas recientes; 9) listar productos con stock crítico; 10) recibir facturas de proveedor para procesarlas desde el panel, validarlas y mostrar un preview antes de una confirmación humana. No afirmes que una factura fue aplicada, que un producto fue creado o que el stock cambió sin una confirmación explícita y un resultado backend exitoso."
ANALYST_SYSTEM_PROMPT = "Sos el Analista IA de StockARmobile. Usá herramientas reales. Separá DATO, CÁLCULO y RECOMENDACIÓN. No inventes predicciones ni afirmes causalidad sin evidencia."
MARKETING_SYSTEM_PROMPT = "Sos el Marketing IA de StockARmobile. Usá productos y clientes reales. Detectá oportunidades con herramientas reales, separá DATO, EVIDENCIA y PROPUESTA, y generá campañas en BORRADOR / PENDIENTE DE APROBACIÓN. Nunca envíes mensajes ni prometas que una campaña fue ejecutada; el envío ocurre únicamente después de aprobación humana y por el motor backend."
COMMERCIAL_SYSTEM_PROMPT = (
    "Sos el Comercial IA de StockArMobile. Atendés únicamente consultas de prospectos que llegan por el WhatsApp comercial "
    "del propio StockArMobile. Tu objetivo es explicar el producto, funcionalidades, planes y próximos pasos de contratación "
    "con información verificable. Nunca accedas ni describas productos, clientes, stock, ventas, pedidos o datos de ningún tenant. "
    "Usá la herramienta consultar_oferta_stockarmobile para consultar precios y planes vigentes antes de afirmar importes o características "
    "comerciales que puedan cambiar. La oferta incluye una prueba gratuita de 10 días y el programa de referidos paga 30% de cada "
    "comercio referido mientras permanezca activo, según la oferta vigente. También podés explicar que StockArMobile integra Mercado Pago "
    "y dispone de agentes IA para ventas, asistencia empresarial, análisis y marketing. No inventes descuentos, integraciones, límites ni "
    "condiciones. Nunca generes pedidos ni cobros en este canal."
)
MAX_TOOL_TURNS = 5
PUBLIC_WEBCHAT_MAX_TOOL_TURNS = 1
# Gemini requires a minimum manually configured deadline of 10 seconds.
# Keep the public chat at 25s so normal model/tool latency has headroom.
PUBLIC_WEBCHAT_PROVIDER_TIMEOUT = 25.0
PUBLIC_WEBCHAT_MAX_OUTPUT_TOKENS = 1600
PUBLIC_WEBCHAT_HISTORY_LIMIT = 8
MAX_AGENT_MESSAGE_CHARS = 4000
MAX_AGENT_HISTORY_CHARS = 16000
MAX_AGENT_HISTORY_MESSAGE_CHARS = 4000

PRICING_TOOL_NAMES = {
    "consultar_precios",
    "analizar_oportunidades_precios",
    "previsualizar_cambio_precios",
    "confirmar_cambio_precios",
    "revertir_cambio_precios",
}


class VendorCartTool(AgentTool):
    name = "carrito_vendedor"
    description = "Consulta el carrito actual del cliente."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def execute(self, **kwargs):
        return VendorOrderService.get_cart(company_id=self.company_id, conversation_id=self._context["conversation_id"])


class VendorAddTool(AgentTool):
    name = "agregar_al_carrito"
    description = "Agrega un producto al carrito."
    input_schema = {
        "type": "object",
        "properties": {"product_query": {"type": "string"}, "quantity": {"type": "number", "minimum": 0.01}},
        "required": ["product_query", "quantity"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return VendorOrderService.update_cart(
            company_id=self.company_id,
            conversation_id=self._context["conversation_id"],
            items=[kwargs],
        )


class VendorRemoveTool(AgentTool):
    name = "quitar_del_carrito"
    description = "Quita un producto del carrito."
    input_schema = {
        "type": "object",
        "properties": {"product_query": {"type": "string"}},
        "required": ["product_query"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return VendorOrderService.remove_from_cart(
            company_id=self.company_id,
            conversation_id=self._context["conversation_id"],
            product_query=kwargs["product_query"],
        )


class CommercialOfferTool(AgentTool):
    name = "consultar_oferta_stockarmobile"
    description = "Consulta los planes y la oferta comercial vigente de StockArMobile. No consulta datos de comercios clientes."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def execute(self, **kwargs):
        from services.ai_agent.usage_service import AI_PLANS
        from services.plan_service import PlanService

        plans = PlanService.all_commercial_plans()
        return {
            "saas_plans": [
                {
                    "code": getattr(plan, "code", None),
                    "name": getattr(plan, "name", None),
                    "price": float(getattr(plan, "price", 0) or 0),
                    "currency": getattr(plan, "currency", "ARS") or "ARS",
                    "duration_days": getattr(plan, "duration_days", 30) or 30,
                    "features": str(getattr(plan, "features_json", "") or ""),
                }
                for plan in plans
            ],
            "ai_plans": [
                {
                    "code": plan["code"],
                    "name": plan["name"],
                    "price": plan["price"],
                    "limit": plan["limit"],
                    "agents": list(plan.get("agents") or ()),
                    "tagline": plan.get("tagline", ""),
                }
                for plan in AI_PLANS
            ],
            "trial_days": 10,
            "referral_percent": 30,
            "mercado_pago": True,
        }


class CommercialCheckoutTool(AgentTool):
    name = "iniciar_contratacion_stockarmobile"
    description = "Inicia el alta y cobro de un plan SaaS pago para el prospecto actual de WhatsApp y devuelve el enlace seguro de Mercado Pago."
    input_schema = {
        "type": "object",
        "properties": {
            "plan_code": {"type": "string", "description": "Código exacto del plan SaaS pago devuelto por consultar_oferta_stockarmobile."},
            "payer_email": {"type": "string", "description": "Email del responsable que realizará el pago y quedará como administrador."},
            "company_name": {"type": "string", "description": "Nombre comercial de la empresa."},
        },
        "required": ["plan_code", "payer_email", "company_name"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        from flask import current_app, url_for
        from services.saas_commercial_whatsapp import create_commercial_checkout

        base_url = current_app.config.get("EXTERNAL_BASE_URL") or None
        if base_url:
            base_url = str(base_url).rstrip("/")
            back_url = f"{base_url}/"
            notification_url = f"{base_url}/api/mercadopago/webhook"
        else:
            back_url = url_for("auth.login", _external=True)
            notification_url = url_for("company_billing.mercadopago_webhook", _external=True)

        return create_commercial_checkout(
            sender=str(self._context.get("customer_phone") or ""),
            plan_code=kwargs["plan_code"],
            payer_email=kwargs["payer_email"],
            company_name=kwargs["company_name"],
            back_url=back_url,
            notification_url=notification_url,
        )


class VendorOrderPreviewTool(AgentTool):
    name = "preparar_pedido"
    description = (
        "Prepara el pedido, el presupuesto final y el link seguro de pago. "
        "Si el cliente pidió un producto que todavía no está en el carrito, agregalo primero con agregar_al_carrito y luego prepará el pedido en la misma ronda de herramientas. "
        "Antes de prepararlo confirmá nombre y teléfono del comprador. "
        "Preguntá si desea retiro o envío. Si elige envío, solicitá dirección, localidad "
        "y provincia. El backend aplica automáticamente el costo fijo de envío configurado "
        "por el comercio y usa ese mismo total para el presupuesto y Mercado Pago. "
        "Nunca calcules porcentajes ni dejes el envío pendiente."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "product_query": {"type": "string", "description": "Producto solicitado directamente por el cliente; usar cuando todavía no está en el carrito."},
            "quantity": {"type": "number", "minimum": 0.01, "description": "Cantidad solicitada del producto. Ejemplo: 4 para 4 metros."},
            "customer_name": {"type": "string"},
            "customer_phone": {"type": "string"},
            "delivery_method": {"type": "string", "enum": ["retiro", "envio"]},
            "delivery_address": {"type": "string"},
            "delivery_city": {"type": "string"},
            "delivery_province": {"type": "string"},
            "delivery_postal_code": {"type": "string"},
            "delivery_reference": {"type": "string"},
            "delivery_notes": {"type": "string"},
        },
        "required": ["customer_name", "customer_phone", "delivery_method"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        product_query = str(kwargs.get("product_query") or "").strip()
        quantity = kwargs.get("quantity")

        # Reuse only the profile saved in this same tenant-scoped conversation.
        # Never treat a public webchat visitor token as a telephone number.
        from stockarmobile.models.conversations import Conversation
        from services.ai_agent.vendor_order_service import _metadata
        conversation = Conversation.query.filter_by(
            id=int(self._context["conversation_id"]),
            company_id=int(self.company_id),
        ).first()
        saved_state = _metadata(conversation) if conversation is not None else {}
        saved_delivery = saved_state.get("delivery") if isinstance(saved_state.get("delivery"), dict) else {}

        customer_name = str(
            kwargs.get("customer_name") or saved_state.get("customer_name")
            or saved_delivery.get("recipient_name") or ""
        ).strip()
        customer_phone = str(
            kwargs.get("customer_phone") or saved_state.get("customer_phone")
            or saved_delivery.get("phone")
            or (self._context.get("customer_phone") if self._context.get("channel") != "webchat" else "")
            or ""
        ).strip()

        requested_delivery_method = str(kwargs.get("delivery_method") or "").strip().lower()
        saved_delivery_method = str(saved_delivery.get("method") or "").strip().lower()
        delivery_method = requested_delivery_method or saved_delivery_method
        if delivery_method in {"envío", "delivery", "shipping"}:
            delivery_method = "envio"
        elif delivery_method in {"retirar", "retira", "pickup", "local"}:
            delivery_method = "retiro"

        same_delivery_method = (
            not requested_delivery_method
            or not saved_delivery_method
            or delivery_method == saved_delivery_method
        )
        delivery_address = str(
            kwargs.get("delivery_address") or (saved_delivery.get("address") if same_delivery_method else "") or ""
        ).strip()
        delivery_city = str(
            kwargs.get("delivery_city") or (saved_delivery.get("city") if same_delivery_method else "") or ""
        ).strip()
        delivery_province = str(
            kwargs.get("delivery_province") or (saved_delivery.get("province") if same_delivery_method else "") or ""
        ).strip()
        delivery_postal_code = str(
            kwargs.get("delivery_postal_code") or (saved_delivery.get("postal_code") if same_delivery_method else "") or ""
        ).strip()
        delivery_reference = str(
            kwargs.get("delivery_reference") or (saved_delivery.get("reference") if same_delivery_method else "") or ""
        ).strip()
        delivery_notes = str(
            kwargs.get("delivery_notes") or (saved_delivery.get("notes") if same_delivery_method else "") or ""
        ).strip()

        if product_query and quantity not in (None, ""):
            # Explicit product + quantity are authoritative. Preserve a pending
            # checkout only for an exact retry of the same cart, buyer and delivery.
            cart = VendorOrderService.get_cart(
                company_id=self.company_id,
                conversation_id=self._context["conversation_id"],
            )
            reuse_pending_checkout = False
            from flask import has_app_context
            if cart.get("items") and has_app_context():
                from app import Quote
                from stockarmobile.models.conversations import Conversation
                from services.ai_agent.vendor_order_service import (
                    PENDING_QUOTE_KEY,
                    NEW_QUOTE_CONTEXT_KEY,
                    _delivery_payload,
                    _metadata,
                    _normalize_text,
                    _normalize_product_score,
                    _pending_quote_matches_checkout,
                    _search_candidates,
                )

                conversation = Conversation.query.filter_by(
                    id=int(self._context["conversation_id"]),
                    company_id=int(self.company_id),
                ).first()
                state = _metadata(conversation) if conversation is not None else {}
                new_quote_context = bool(state.get(NEW_QUOTE_CONTEXT_KEY))
                pending_quote_id = None if new_quote_context else state.get(PENDING_QUOTE_KEY)
                if pending_quote_id:
                    try:
                        pending_quote = Quote.query.filter_by(
                            id=int(pending_quote_id),
                            company_id=int(self.company_id),
                        ).first()
                    except (TypeError, ValueError):
                        pending_quote = None
                    if (
                        pending_quote is not None
                        and pending_quote.status not in {"ANULADO", "RECHAZADO", "VENCIDO", "CONVERTIDO"}
                        and (
                            pending_quote.expires_at is None
                            or pending_quote.expires_at > __import__("datetime").datetime.utcnow()
                        )
                    ):
                        candidates = _search_candidates(self.company_id, product_query)
                        requested_product = candidates[0] if candidates else None
                        normalized_query = _normalize_text(product_query)
                        unambiguous = bool(requested_product)
                        if len(candidates) > 1:
                            second = candidates[1]
                            if (
                                _normalize_text(requested_product.name) != normalized_query
                                and _normalize_text(second.name) != normalized_query
                                and abs(
                                    _normalize_product_score(requested_product, product_query)
                                    - _normalize_product_score(second, product_query)
                                ) < 20
                            ):
                                unambiguous = False
                        try:
                            requested_quantity = float(quantity)
                        except (TypeError, ValueError):
                            requested_quantity = -1.0
                        cart_matches_requested_line = bool(
                            unambiguous
                            and any(
                                int(item.get("product_id") or 0) == int(requested_product.id)
                                and abs(float(item.get("quantity") or 0) - requested_quantity) < 0.0001
                                for item in cart["items"]
                            )
                        )
                        if cart_matches_requested_line:
                            try:
                                requested_delivery = _delivery_payload(
                                    method=delivery_method or "retiro",
                                    customer_name=customer_name,
                                    customer_phone=customer_phone,
                                    address=delivery_address,
                                    city=delivery_city,
                                    province=delivery_province,
                                    postal_code=delivery_postal_code,
                                    reference=delivery_reference,
                                    notes=delivery_notes,
                                )
                            except ValueError as exc:
                                return {"success": False, "error": str(exc), "retryable": False}
                            if requested_delivery:
                                reuse_pending_checkout = _pending_quote_matches_checkout(
                                    quote=pending_quote,
                                    cart=cart,
                                    customer_name=customer_name,
                                    customer_phone=customer_phone,
                                    delivery=requested_delivery,
                                )

            if not reuse_pending_checkout:
                added = VendorOrderService.update_cart(
                    company_id=self.company_id,
                    conversation_id=self._context["conversation_id"],
                    items=[{
                        "product_query": product_query,
                        "quantity": quantity,
                        "replace_quantity": True,
                    }],
                )
                if isinstance(added, dict) and added.get("success") is False:
                    return added

        missing_fields = []
        if not customer_name:
            missing_fields.append("nombre")
        if not customer_phone:
            missing_fields.append("teléfono")
        if delivery_method not in {"retiro", "envio"}:
            missing_fields.append("modalidad de entrega (retiro o envío)")
        elif delivery_method == "envio":
            if not delivery_address:
                missing_fields.append("dirección")
            if not delivery_city:
                missing_fields.append("localidad")
            if not delivery_province:
                missing_fields.append("provincia")
        if missing_fields:
            # Keep the chosen products, but do not create a quote/payment from
            # incomplete identity or delivery data. The model can ask only for
            # the missing fields and resume the same cart on the next turn.
            return {
                "success": True,
                "status": "needs_customer_details",
                "missing_fields": missing_fields,
                "cart": VendorOrderService.get_cart(
                    company_id=self.company_id,
                    conversation_id=self._context["conversation_id"],
                ),
            }

        return VendorOrderService.create_pending_order(
            company_id=self.company_id,
            conversation_id=self._context["conversation_id"],
            customer_name=customer_name,
            customer_phone=customer_phone,
            delivery_method=delivery_method,
            delivery_address=delivery_address,
            delivery_city=delivery_city,
            delivery_province=delivery_province,
            delivery_postal_code=delivery_postal_code,
            delivery_reference=delivery_reference,
            delivery_notes=delivery_notes,
            actor_user_id=self._context.get("actor_user_id"),
            idempotency_key=self._context.get("idempotency_key"),
        )


class AgentRuntime:
    tool_registry = {
        "buscar_producto": BuscarProductoTool,
        "consultar_stock": ConsultarStockTool,
        "buscar_cliente": BuscarClienteTool,
        "oportunidades_crm": CRMOpportunitiesTool,
        "contar_clientes": ContarClientesTool,
        "contar_productos": ContarProductosTool,
        "resumen_ventas": ResumenVentasTool,
        "productos_mas_vendidos": ProductosMasVendidosTool,
        "productos_sin_ventas_recientes": ProductosSinVentasRecientesTool,
        "stock_critico": StockCriticoTool,
        "comparar_ventas": VentasComparativaTool,
        "clientes_inactivos": ClientesInactivosTool,
        "productos_promocionables": ProductosPromocionablesTool,
        "preparar_campana": PrepararCampanaTool,
        "oportunidades_marketing": OportunidadesMarketingTool,
        "carrito_vendedor": VendorCartTool,
        "agregar_al_carrito": VendorAddTool,
        "quitar_del_carrito": VendorRemoveTool,
        "preparar_pedido": VendorOrderPreviewTool,
        "consultar_oferta_stockarmobile": CommercialOfferTool,
        "iniciar_contratacion_stockarmobile": CommercialCheckoutTool,
    }
    agent_tool_names = {
        "asistente": {
            "buscar_producto", "consultar_stock", "buscar_cliente", "contar_clientes",
            "contar_productos", "resumen_ventas", "productos_mas_vendidos",
            "productos_sin_ventas_recientes", "stock_critico", "oportunidades_crm",
        },
        "vendedor": {
            "buscar_producto", "consultar_stock", "buscar_cliente", "carrito_vendedor",
            "agregar_al_carrito", "quitar_del_carrito", "preparar_pedido",
        },
        "analista": {
            "resumen_ventas", "productos_mas_vendidos", "productos_sin_ventas_recientes",
            "stock_critico", "contar_clientes", "comparar_ventas", "clientes_inactivos", "oportunidades_crm",
        },
        "marketing": {
            "buscar_producto", "consultar_stock", "buscar_cliente", "clientes_inactivos",
            "productos_promocionables", "oportunidades_marketing", "preparar_campana", "oportunidades_crm",
        },
        "comercial": {"consultar_oferta_stockarmobile", "iniciar_contratacion_stockarmobile"},
    }

    @classmethod
    def provider(cls, *, timeout=None, max_retries=None):
        provider = (os.getenv("AI_PROVIDER") or "lm_studio").strip().lower()
        if provider == "gemini":
            return GeminiProvider(timeout=timeout, max_retries=max_retries)
        if provider == "openai":
            kwargs = {} if max_retries is None else {"max_retries": max_retries}
            return OpenAIProvider(timeout=timeout, **kwargs)
        if provider == "openai_compatible":
            return OpenAICompatibleProvider(timeout=timeout)
        return LMStudioProvider(timeout=timeout)

    @classmethod
    def ensure_agent(cls, company_id, *, channel):
        return choose_agent(company_id, channel=channel)

    @classmethod
    def _config(cls, agent, company_id):
        return (
            db.session.query(AgentConfiguration)
            .filter(
                AgentConfiguration.agent_id == agent.id,
                AgentConfiguration.company_id == company_id,
            )
            .order_by(AgentConfiguration.id.asc())
            .first()
        )

    @classmethod
    def _history(cls, company_id, conversation_id, limit=20, *, exclude_message_id=None):
        query = (
            db.session.query(ConversationMessage)
            .filter(
                ConversationMessage.company_id == company_id,
                ConversationMessage.conversation_id == conversation_id,
            )
        )
        if exclude_message_id is not None:
            query = query.filter(ConversationMessage.id != exclude_message_id)
        rows = query.order_by(ConversationMessage.id.desc()).limit(limit).all()
        history = []
        remaining_chars = MAX_AGENT_HISTORY_CHARS
        for row in rows:
            if row.role not in {"user", "assistant"} or remaining_chars <= 0:
                continue
            content = str(row.content or "")[-MAX_AGENT_HISTORY_MESSAGE_CHARS:]
            content = content[-remaining_chars:]
            history.append({"role": row.role, "content": content})
            remaining_chars -= len(content)
        return list(reversed(history))

    @classmethod
    def _tool_definitions(cls, agent_key="asistente", allowed_tool_names=None, company_id=None, user_id=None):
        names = set(cls.agent_tool_names.get(agent_key, set()))
        if allowed_tool_names is not None:
            names = names.intersection(set(allowed_tool_names))
        if company_id is not None:
            from app import Company
            company = Company.query.filter_by(id=int(company_id)).first()
            pricing_access = can_use_ai_feature(company, "pricing_controller") if company is not None else None
            rollback_access = can_use_ai_feature(company, "pricing_rollback") if company is not None else None
            if pricing_access is None or not pricing_access.allowed:
                names.difference_update(PRICING_TOOL_NAMES)
            elif rollback_access is None or not rollback_access.allowed:
                names.discard("revertir_cambio_precios")
            if "oportunidades_crm" in names:
                crm_allowed, _ = crm_tool_access(company_id, user_id)
                if not crm_allowed:
                    names.discard("oportunidades_crm")
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": getattr(tool, "description", ""),
                    "parameters": getattr(
                        tool, "input_schema", {"type": "object", "properties": {}}
                    ),
                },
            }
            for name, tool in cls.tool_registry.items()
            if name in names
        ]

    @staticmethod
    def _agent_key(agent):
        return {
            VENDOR_AGENT_NAME: "vendedor",
            BUSINESS_AGENT_NAME: "asistente",
            "Analista IA": "analista",
            "Marketing IA": "marketing",
            "Comercial IA": "comercial",
        }.get(agent.name, "asistente")

    @classmethod
    def _execute_tool(cls, name, *, company_id, arguments, context=None, allowed_tool_names=None):
        if not isinstance(arguments, dict):
            return {"success": False, "error": "arguments must be an object"}
        if allowed_tool_names is not None and name not in set(allowed_tool_names):
            return {"success": False, "error": "tool_not_permitted"}
        if name in PRICING_TOOL_NAMES:
            from app import Company
            company = Company.query.filter_by(id=int(company_id)).first()
            feature = "pricing_rollback" if name == "revertir_cambio_precios" else "pricing_controller"
            access = can_use_ai_feature(company, feature) if company is not None else None
            if access is None or not access.allowed:
                return {"success": False, "error": access.reason if access else "pricing_feature_not_permitted"}
        if name == "oportunidades_crm":
            crm_allowed, reason = crm_tool_access(
                company_id,
                (context or {}).get("actor_user_id"),
            )
            if not crm_allowed:
                return {"success": False, "error": reason or "crm_feature_not_permitted"}
        tool_class = cls.tool_registry.get(name)
        if tool_class is None:
            return {"success": False, "error": "tool_not_found"}

        if name == "preparar_campana":
            from app import Company
            company = Company.query.filter_by(id=int(company_id)).first()
            marketing_options = get_special_options(company, "marketing") if company is not None else {}
            configured_types = set(marketing_options.get("campaign_types") or [])
            tool_type_to_preference = {
                "promocion_producto": "promocion",
                "recuperacion_clientes_inactivos": "reactivacion",
                "productos_sin_ventas": "stock",
                "general": "novedad",
            }
            requested_type = str(arguments.get("campaign_type") or "general").strip().lower()
            required_preference = tool_type_to_preference.get(requested_type)
            if required_preference and required_preference not in configured_types:
                return {
                    "success": False,
                    "error": (
                        f"El tipo de campaña '{requested_type}' no está habilitado por la configuración de Marketing. "
                        f"Tipos habilitados: {', '.join(sorted(configured_types)) or 'ninguno'}."
                    ),
                }

        if "company_id" in arguments:
            return {"success": False, "error": "company_id must be passed explicitly"}
        result = tool_class(company_id=company_id, **(context or {})).execute(**arguments)
        return result if isinstance(result, dict) else {"success": False, "error": "tool result must be an object"}

    @classmethod
    def _provider_model(cls, provider, config):
        configured = str(getattr(config, "model", "") or "").strip() if config else ""
        if not configured:
            return None
        if isinstance(provider, GeminiProvider):
            return configured if configured.lower().startswith("gemini-") else None
        return configured

    @staticmethod
    def _tool_calls(response):
        if not isinstance(response, dict):
            return []
        calls = response.get("tool_calls")
        if isinstance(calls, list):
            valid = [call for call in calls if isinstance(call, dict) and call.get("name")]
            if valid:
                return valid
        call = response.get("tool_call")
        return [call] if isinstance(call, dict) and call.get("name") else []

    @staticmethod
    def _content(response):
        if not isinstance(response, dict):
            return ""
        value = response.get("content")
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _usage_values(response):
        usage = response.get("usage") if isinstance(response, dict) else None

        def value(*names):
            if usage is None:
                return 0
            if isinstance(usage, dict):
                for name in names:
                    if usage.get(name) is not None:
                        return usage.get(name) or 0
                return 0
            for name in names:
                candidate = getattr(usage, name, None)
                if candidate is not None:
                    return candidate or 0
            return 0

        input_tokens = int(value("prompt_tokens", "input_tokens", "prompt_token_count"))
        output_tokens = int(value("completion_tokens", "output_tokens", "candidates_token_count"))
        total_tokens = int(value("total_tokens", "total_token_count"))
        if not total_tokens:
            total_tokens = input_tokens + output_tokens
        return input_tokens, output_tokens, total_tokens

    @classmethod
    def _run_tool_loop(cls, *, provider, messages, tools, kwargs, company_id, context, allowed_tool_names=None, max_tool_turns=None):
        working_messages = list(messages)
        tool_turn_limit = MAX_TOOL_TURNS if max_tool_turns is None else max(0, int(max_tool_turns))
        response = provider.generate(messages=working_messages, tools=tools, **kwargs)
        campaign_context = None
        tool_rounds = 0
        provider_calls = 1
        input_tokens, output_tokens, total_tokens = cls._usage_values(response)
        last_model = response.get("model") if isinstance(response, dict) else None

        def capture_usage(current_response):
            nonlocal provider_calls, input_tokens, output_tokens, total_tokens, last_model
            provider_calls += 1
            current_input, current_output, current_total = cls._usage_values(current_response)
            input_tokens += current_input
            output_tokens += current_output
            total_tokens += current_total
            if isinstance(current_response, dict) and current_response.get("model"):
                last_model = current_response.get("model")

        while True:
            tool_calls = cls._tool_calls(response)
            final_content = cls._content(response)
            if not tool_calls:
                if final_content:
                    return final_content, campaign_context, tool_rounds, {
                        "provider": provider.__class__.__name__.replace("Provider", "").lower(),
                        "model": last_model or getattr(provider, "model", None) or kwargs.get("model"),
                        "provider_calls": provider_calls,
                        "tool_rounds": tool_rounds,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                    }
                raise RuntimeError("El proveedor IA no devolvió una respuesta.")

            next_tool_round = tool_rounds + 1
            if next_tool_round > tool_turn_limit:
                synthesis_prompt = {
                    "role": "user",
                    "content": (
                        "Terminá la respuesta usando exclusivamente los resultados de las herramientas ya ejecutadas. "
                        "No vuelvas a llamar herramientas y explicá claramente los datos obtenidos."
                    ),
                }
                final_messages = working_messages + [synthesis_prompt]
                response = provider.generate(messages=final_messages, tools=[], **kwargs)
                capture_usage(response)
                final_content = cls._content(response)
                if final_content:
                    return final_content, campaign_context, tool_rounds, {
                        "provider": provider.__class__.__name__.replace("Provider", "").lower(),
                        "model": last_model or getattr(provider, "model", None) or kwargs.get("model"),
                        "provider_calls": provider_calls,
                        "tool_rounds": tool_rounds,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                    }
                raise RuntimeError("El proveedor IA no pudo completar la respuesta después de consultar las herramientas.")

            tool_rounds = next_tool_round
            assistant_tool_calls = []
            results_to_append = []
            for index, call in enumerate(tool_calls):
                name = str(call.get("name") or "")
                args = call.get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                tool_id = str(call.get("id") or f"tool-call-{tool_rounds}-{index + 1}")
                assistant_tool_calls.append(
                    {
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                )
                try:
                    result = cls._execute_tool(
                        name,
                        company_id=company_id,
                        arguments=args,
                        context=context,
                        allowed_tool_names=allowed_tool_names,
                    )
                except AIProviderError:
                    raise
                except ValueError as exc:
                    result = {"success": False, "error": str(exc)[:700], "retryable": False}
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception(
                        "AI tool execution failed: tool=%s company_id=%s conversation_id=%s",
                        name,
                        company_id,
                        context.get("conversation_id") if isinstance(context, dict) else None,
                    )
                    result = {
                        "success": False,
                        "error": "No se pudo completar la operación solicitada en este momento.",
                        "retryable": True,
                    }
                if name == "preparar_campana" and isinstance(result, dict):
                    campaign_context = result.get("campaign_context") or campaign_context
                results_to_append.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

            working_messages.append(
                {
                    "role": "assistant",
                    "content": final_content or "",
                    "tool_calls": assistant_tool_calls,
                }
            )
            working_messages.extend(results_to_append)
            response = provider.generate(messages=working_messages, tools=tools, **kwargs)
            capture_usage(response)

    @classmethod
    def process(
        cls,
        *,
        company_id,
        conversation_id,
        message,
        channel,
        sender_id=None,
        external_message_id=None,
        idempotency_key=None,
        metadata=None,
        provider_override=None,
        include_system_prompt=True,
        idempotency_lock_held=False,
        trace_id=None,
    ):
        request_message = str(message or "")
        if len(request_message) > MAX_AGENT_MESSAGE_CHARS:
            raise ValueError(f"El mensaje no puede superar los {MAX_AGENT_MESSAGE_CHARS} caracteres.")
        idempotency_key = str(idempotency_key or "").strip() or None
        if idempotency_key and len(idempotency_key) > 120:
            raise ValueError("La clave de idempotencia no puede superar 120 caracteres.")
        if idempotency_key and not idempotency_lock_held:
            from services.ai_agent.public_idempotency import public_operation_lock

            with public_operation_lock(
                scope="request",
                company_id=company_id,
                conversation_id=conversation_id,
                key=idempotency_key,
            ):
                return cls.process(
                    company_id=company_id,
                    conversation_id=conversation_id,
                    message=request_message,
                    channel=channel,
                    sender_id=sender_id,
                    external_message_id=external_message_id,
                    idempotency_key=idempotency_key,
                    metadata=metadata,
                    provider_override=provider_override,
                    include_system_prompt=include_system_prompt,
                    idempotency_lock_held=True,
                    trace_id=trace_id,
                )
        conversation = (
            db.session.query(Conversation)
            .filter(
                Conversation.id == conversation_id,
                Conversation.company_id == company_id,
            )
            .first()
        )
        if conversation is None:
            raise ValueError("Conversation not found for company.")

        agent = (
            db.session.query(Agent)
            .filter(Agent.id == conversation.agent_id, Agent.company_id == company_id)
            .first()
        )
        if agent is None:
            agent = cls.ensure_agent(company_id, channel=channel)
            conversation.agent_id = agent.id
            conversation.channel = channel
            db.session.flush()

        agent_key = cls._agent_key(agent)
        company = __import__("app").Company.query.filter_by(id=company_id).first()
        if company is None:
            raise ValueError("Company not found.")
        # Un reintento idempotente de una operación ya aceptada debe poder
        # reconstruir la respuesta original aunque el plan haya cambiado desde
        # el primer intento. La búsqueda queda estrictamente aislada por
        # company_id + conversation_id para evitar colisiones entre tenants o
        # conversaciones.
        retry_incoming = None
        if idempotency_key:
            duplicate = (
                db.session.query(ConversationMessage)
                .filter(
                    ConversationMessage.company_id == company_id,
                    ConversationMessage.conversation_id == conversation.id,
                    ConversationMessage.idempotency_key == idempotency_key,
                )
                .first()
            )
            if duplicate:
                assistant_duplicate = (
                    db.session.query(ConversationMessage)
                    .filter(
                        ConversationMessage.company_id == company_id,
                        ConversationMessage.conversation_id == conversation.id,
                        ConversationMessage.trace_id == duplicate.trace_id,
                        ConversationMessage.role == "assistant",
                    )
                    .order_by(ConversationMessage.id.desc())
                    .first()
                )
                if assistant_duplicate is not None:
                    return {
                        "status": "duplicate",
                        "conversation_id": conversation.id,
                        "message_id": duplicate.id,
                        "assistant_message_id": assistant_duplicate.id,
                        "content": assistant_duplicate.content,
                    }
                retry_incoming = duplicate

        lock_ai_usage(company_id)
        access = can_use_ai(company, agent_key)
        if not access.allowed:
            raise ValueError(access.reason or "El agente IA no está disponible para este plan.")
        if not agent.active:
            return {
                "status": "disabled",
                "conversation_id": conversation.id,
                "company_id": company_id,
                "agent_id": agent.id,
                "content": "",
            }

        is_public_webchat = channel == "webchat" and sender_id is None and str((metadata or {}).get("source") or "").startswith("public_webchat")
        history = cls._history(
            company_id,
            conversation.id,
            PUBLIC_WEBCHAT_HISTORY_LIMIT if is_public_webchat else 19,
            exclude_message_id=retry_incoming.id if retry_incoming is not None else None,
        )
        trace_id = (
            str(retry_incoming.trace_id or trace_id or uuid.uuid4())
            if retry_incoming is not None
            else str(trace_id or uuid.uuid4())
        )
        if retry_incoming is not None:
            incoming = retry_incoming
            incoming.trace_id = trace_id
            request_message = str(incoming.content or request_message)
        else:
            incoming = ConversationMessage(
                conversation_id=conversation.id,
                company_id=company_id,
                sender_type="user",
                sender_id=sender_id,
                role="user",
                content=request_message,
                content_type="text",
                external_message_id=external_message_id,
                idempotency_key=idempotency_key,
                trace_id=trace_id,
                metadata_json=metadata or {},
            )
            db.session.add(incoming)
        db.session.flush()

        config = cls._config(agent, company_id)
        prompt = {
            "vendedor": VENDOR_SYSTEM_PROMPT,
            "asistente": BUSINESS_SYSTEM_PROMPT,
            "analista": ANALYST_SYSTEM_PROMPT,
            "marketing": MARKETING_SYSTEM_PROMPT,
            "comercial": COMMERCIAL_SYSTEM_PROMPT,
        }[agent_key]
        if agent_key in {"vendedor", "asistente", "analista", "marketing"}:
            merchant_name = str(getattr(company, "name", "") or getattr(company, "legal_name", "") or "").strip() or "tu comercio"
            prompt += (
                "\n\nIDENTIDAD DEL COMERCIO:"
                f"\n- Nombre del comercio: {merchant_name}"
                "\n- Este agente trabaja para el comercio del usuario, no para StockArmobile."
                "\n- En mensajes, propuestas, campañas y firmas, usá el nombre real del comercio."
                f"\n- Si necesitás firmar una comunicación, firmá como: El equipo de {merchant_name}."
                "\n- No firmes ni presentes la comunicación como StockArmobile, salvo que el usuario lo pida explícitamente."
            )
        vendor_options = None
        allowed_tool_names = None
        if agent_key == "vendedor":
            vendor_options = get_vendor_options(company)
            prompt += "\n\n" + build_vendor_runtime_instructions(
                merchant_instructions=config.system_prompt if config else "",
                vendor_options=vendor_options,
                language=(config.language if config else "es-AR"),
                channel=channel,
                first_interaction=not history,
            )
            allowed_tool_names = vendor_allowed_tool_names(vendor_options)
            if is_public_webchat:
                prompt += (
                    "\n\nMODO WEBCHAT PÚBLICO — ORDEN DIRECTA:"
                    "\n- Priorizá resolver una solicitud de compra en una sola ronda de herramientas."
                    "\n- Si el cliente ya indicó producto, cantidad, nombre, teléfono y datos de envío, evitá búsquedas exploratorias innecesarias."
                    "\n- Para un pedido, podés agregar el producto al carrito y después preparar el pedido dentro de la misma ronda de herramientas."
                    "\n- Si faltan datos obligatorios, guardá el producto en el carrito y preguntá solo los datos que realmente falten; nunca afirmes que el presupuesto se creó antes del resultado exitoso de preparar_pedido."
                    "\n- Si hay datos de cliente ya guardados en esta misma conversación, reutilizalos para el nuevo presupuesto salvo que el cliente indique un cambio."
                    "\n- Nunca afirmes que el pago quedó realizado si el backend no devolvió un resultado exitoso."
                )
                # Only use profile details saved in this visitor-bound conversation.
                # Do not search across unrelated chats or disclose another customer's record by name.
                from services.ai_agent.vendor_order_service import _metadata as _vendor_metadata
                saved_state = _vendor_metadata(conversation)
                saved_delivery = saved_state.get("delivery") if isinstance(saved_state.get("delivery"), dict) else {}
                saved_customer = {
                    "Nombre": saved_state.get("customer_name") or saved_delivery.get("recipient_name"),
                    "Teléfono": saved_state.get("customer_phone") or saved_delivery.get("phone"),
                    "Modalidad de entrega": saved_delivery.get("method"),
                    "Dirección": saved_delivery.get("address"),
                    "Localidad": saved_delivery.get("city"),
                    "Provincia": saved_delivery.get("province"),
                    "Código postal": saved_delivery.get("postal_code"),
                }
                saved_lines = [
                    f"- {label}: {str(value).strip()}"
                    for label, value in saved_customer.items()
                    if str(value or "").strip()
                ]
                if saved_lines:
                    prompt += (
                        "\n\nDATOS DE CLIENTE GUARDADOS EN ESTA MISMA CONVERSACIÓN WEB (mismo visitante):"
                        "\nUsalos para preparar un nuevo presupuesto y no vuelvas a pedirlos si no hace falta. "
                        "Si el cliente pide cambiar entrega o contacto, prevalece el cambio que indique. "
                        "No busques ni infieras datos de otros clientes o conversaciones."
                        "\n" + "\n".join(saved_lines)
                    )
        elif agent_key in {"analista", "marketing"}:
            special_options = get_special_options(company, agent_key)
            if agent_key == "analista":
                alert_labels = {"sales_drop": "caídas de ventas", "critical_stock": "stock crítico", "inactive_clients": "clientes inactivos", "low_rotation": "productos sin rotación"}
                enabled_alerts = ", ".join(alert_labels.get(item, item) for item in special_options.get("alerts", [])) or "ninguna"
                prompt += (
                    "\n\nCONFIGURACIÓN DEL ANALISTA DEL COMERCIO:"
                    f"\n- Período predeterminado: {special_options.get('default_period', '30d')}"
                    f"\n- Formato de salida: {special_options.get('output_style', 'accionable')}"
                    f"\n- Alertas habilitadas: {enabled_alerts}"
                    "\nUsá estas preferencias como defaults cuando el usuario no indique otras."
                )
            else:
                prompt += (
                    "\n\nCONFIGURACIÓN DE MARKETING DEL COMERCIO:"
                    f"\n- Segmento predeterminado: {special_options.get('default_segment', 'inactivos')}"
                    f"\n- Tono: {special_options.get('campaign_tone', 'profesional')}"
                    f"\n- Tipos permitidos: {', '.join(special_options.get('campaign_types', [])) or 'ninguno'}"
                    "\n- Toda campaña requiere aprobación humana antes de cualquier envío."
                    "\nUsá estas preferencias como defaults y mantené toda propuesta en BORRADOR."
                )
            if config and config.system_prompt:
                prompt += f"\n\nInstrucciones del comercio:\n{config.system_prompt}"
        elif config and config.system_prompt:
            prompt += f"\n\nInstrucciones del comercio:\n{config.system_prompt}"

        messages = (
            [{"role": "system", "content": prompt}] if include_system_prompt else []
        ) + history + [{"role": "user", "content": request_message}]
        kwargs = {}
        provider = provider_override or cls.provider(
            timeout=PUBLIC_WEBCHAT_PROVIDER_TIMEOUT if is_public_webchat else None,
            max_retries=0 if is_public_webchat else None,
        )
        effective_model = cls._provider_model(provider, config)
        if effective_model:
            kwargs["model"] = effective_model
        if config:
            if config.temperature is not None:
                kwargs["temperature"] = float(config.temperature)
            if config.max_tokens is not None:
                kwargs["max_tokens"] = config.max_tokens
        if is_public_webchat:
            kwargs["max_tokens"] = min(int(kwargs.get("max_tokens") or PUBLIC_WEBCHAT_MAX_OUTPUT_TOKENS), PUBLIC_WEBCHAT_MAX_OUTPUT_TOKENS)

        context = {
            "conversation_id": conversation.id,
            # Public webchat uses "from" for a visitor/session token, not a phone.
            "customer_phone": "" if is_public_webchat else ((metadata or {}).get("from") or ""),
            "channel": channel,
            "actor_user_id": sender_id,
            "idempotency_key": idempotency_key,
            "trace_id": trace_id,
        }
        final_content, campaign_context, tool_rounds, ai_telemetry = cls._run_tool_loop(
            provider=provider,
            messages=messages,
            tools=cls._tool_definitions(
                agent_key,
                allowed_tool_names=allowed_tool_names,
                company_id=company_id,
                user_id=sender_id,
            ),
            kwargs=kwargs,
            company_id=company_id,
            context=context,
            allowed_tool_names=allowed_tool_names,
            max_tool_turns=PUBLIC_WEBCHAT_MAX_TOOL_TURNS if is_public_webchat else None,
        )

        # Una solicitud explícita de propuesta/campaña no queda como simple texto:
        # si el modelo omitió la herramienta, el backend intenta preparar igualmente
        # un borrador usando el tipo permitido por la configuración del comercio.
        proposal_request = False
        if agent_key == "marketing" and campaign_context is None:
            normalized_request = " ".join(str(message or "").strip().lower().split())
            proposal_request = any(
                phrase in normalized_request
                for phrase in (
                    "propuesta", "crear campaña", "crear una campaña", "crea una campaña",
                    "armame una campaña", "armá una campaña", "haceme una campaña",
                    "hacé una campaña", "prepara una campaña", "prepará una campaña",
                    "crea una promocion", "creá una promoción",
                )
            )
            if proposal_request:
                marketing_options = get_special_options(company, "marketing")
                configured_types = set(marketing_options.get("campaign_types") or [])
                fallback_type = next(
                    (
                        candidate
                        for candidate in ("promocion", "reactivacion", "stock", "novedad")
                        if candidate in configured_types
                    ),
                    None,
                )
                fallback_tool_type = {
                    "promocion": "promocion_producto",
                    "reactivacion": "recuperacion_clientes_inactivos",
                    "stock": "productos_sin_ventas",
                    "novedad": "general",
                }.get(fallback_type)
                if fallback_tool_type:
                    fallback_result = cls._execute_tool(
                        "preparar_campana",
                        company_id=company_id,
                        arguments={"campaign_type": fallback_tool_type, "channel": "email"},
                        context=context,
                        allowed_tool_names=allowed_tool_names,
                    )
                    if isinstance(fallback_result, dict):
                        campaign_context = fallback_result.get("campaign_context") or campaign_context

        if agent_key == "marketing":
            merchant_name = str(getattr(company, "name", "") or getattr(company, "legal_name", "") or "").strip() or "tu comercio"
            final_content = (
                str(final_content or "")
                .replace("En StockARmobile", f"En {merchant_name}")
                .replace("En StockArMobile", f"En {merchant_name}")
                .replace("en StockARmobile", f"en {merchant_name}")
                .replace("en StockArMobile", f"en {merchant_name}")
                .replace("El equipo de StockARmobile", f"El equipo de {merchant_name}")
                .replace("El equipo de StockArMobile", f"El equipo de {merchant_name}")
                .replace("Equipo de StockARmobile", f"Equipo de {merchant_name}")
                .replace("Equipo de StockArMobile", f"Equipo de {merchant_name}")
            )

        assistant = ConversationMessage(
            conversation_id=conversation.id,
            company_id=company_id,
            sender_type="agent",
            sender_id=agent.id,
            role="assistant",
            content=final_content,
            content_type="text",
            trace_id=trace_id,
            metadata_json={
                "channel": channel,
                "agent_name": agent.name,
                "agent_key": agent_key,
                "tool_rounds": tool_rounds,
                "ai_usage_telemetry": ai_telemetry,
            },
        )
        db.session.add(assistant)
        db.session.flush()

        campaign = None
        if campaign_context is not None and agent_key == "marketing":
            from services.ai_agent.campaign_service import CampaignService

            product = campaign_context.get("product") or {}
            campaign = CampaignService.create_draft(
                company_id=company_id,
                user_id=sender_id or 0,
                title=(product.get("name") or "Campaña propuesta")[:180],
                objective=campaign_context.get("campaign_type") or "Promoción",
                campaign_type=campaign_context.get("campaign_type") or "general",
                content=final_content,
                system_data=campaign_context,
                audience_segment=campaign_context.get("audience_segment") or "No definido",
                audience_count=campaign_context.get("audience_count") or 0,
                product_id=product.get("id"),
            )
            final_content = (
                f"{final_content}\n\nCampaña #{campaign.id} guardada como "
                "BORRADOR / PENDIENTE DE APROBACIÓN. No se realizó ningún envío externo."
            )
            assistant.content = final_content
        elif agent_key == "marketing" and proposal_request:
            final_content = (
                f"{final_content}\n\nEsto es una propuesta textual; no se creó ni guardó "
                "un borrador de campaña y no se realizó ningún envío."
            )
            assistant.content = final_content

        record_ai_usage(
            company_id=company_id,
            agent_id=agent.id,
            conversation_id=conversation.id,
            user_id=sender_id,
            external_actor_id=(metadata or {}).get("from"),
            interaction_type=agent_key,
            message_id=assistant.id,
            telemetry=ai_telemetry,
        )
        db.session.commit()
        return {
            "status": "completed",
            "company_id": company_id,
            "conversation_id": conversation.id,
            "agent_id": agent.id,
            "message_id": incoming.id,
            "assistant_message_id": assistant.id,
            "content": str(final_content),
            "trace_id": trace_id,
        }

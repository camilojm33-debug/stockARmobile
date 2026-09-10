"""Production-oriented runtime for StockARmobile AI agents."""
from __future__ import annotations

import json
import os
import uuid

from services.ai_agent.providers.openai_compatible import OpenAICompatibleProvider
from services.ai_agent.providers.lm_studio import LMStudioProvider
from services.ai_agent.providers.openai import OpenAIProvider
from services.ai_agent.providers.gemini import GeminiProvider
from services.ai_agent.config_service import BUSINESS_AGENT_NAME, VENDOR_AGENT_NAME, choose_agent
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
from services.ai_agent.tools.product_search import BuscarProductoTool
from services.ai_agent.tools.stock_query import ConsultarStockTool
from services.ai_agent.tools.analyst_marketing import (
    ClientesInactivosTool,
    PrepararCampanaTool,
    ProductosPromocionablesTool,
    VentasComparativaTool,
)
from services.ai_agent.vendor_order_service import VendorOrderService
from services.ai_agent.usage_service import can_use_ai, record_ai_usage
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Agent, AgentConfiguration, Conversation, ConversationMessage

VENDOR_SYSTEM_PROMPT = "Sos el Vendedor 24 hs de StockARmobile. Consultá herramientas antes de afirmar precio o stock. No inventes información."
BUSINESS_SYSTEM_PROMPT = "Sos el Asistente empresarial de StockARmobile. Usá herramientas para consultar datos reales y nunca inventes cifras. Si te preguntan qué podés hacer, informá estas capacidades: 1) Buscar productos por nombre, marca o código; 2) consultar el stock actual de un producto; 3) contar productos; 4) buscar clientes por nombre, email, teléfono o WhatsApp; 5) contar clientes activos; 6) resumir ventas por período; 7) listar productos más vendidos; 8) listar productos sin ventas recientes; 9) listar productos con stock crítico; 10) recibir facturas de proveedor para procesarlas desde el panel, validarlas y mostrar un preview antes de una confirmación humana. No afirmes que una factura fue aplicada, que un producto fue creado o que el stock cambió sin una confirmación explícita y un resultado backend exitoso."
ANALYST_SYSTEM_PROMPT = "Sos el Analista IA de StockARmobile. Usá herramientas reales. Separá DATO, CÁLCULO y RECOMENDACIÓN. No inventes predicciones ni afirmes causalidad sin evidencia."
MARKETING_SYSTEM_PROMPT = "Sos el Marketing IA de StockARmobile. Usá productos y clientes reales. Generá propuestas en BORRADOR / PENDIENTE DE APROBACIÓN. Nunca envíes mensajes ni prometas que una campaña fue ejecutada."
MAX_TOOL_TURNS = 5


class VendorCartTool(AgentTool):
    name = "carrito_vendedor"
    description = "Consulta el carrito actual del cliente de WhatsApp."
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


class VendorOrderPreviewTool(AgentTool):
    name = "preparar_pedido"
    description = "Prepara un pedido pendiente y genera un link seguro de pago."
    input_schema = {
        "type": "object",
        "properties": {"customer_name": {"type": "string"}},
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return VendorOrderService.create_pending_order(
            company_id=self.company_id,
            conversation_id=self._context["conversation_id"],
            customer_name=str(kwargs.get("customer_name") or ""),
            customer_phone=str(self._context.get("customer_phone") or ""),
            actor_user_id=self._context.get("actor_user_id"),
        )


class AgentRuntime:
    tool_registry = {
        "buscar_producto": BuscarProductoTool,
        "consultar_stock": ConsultarStockTool,
        "buscar_cliente": BuscarClienteTool,
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
        "carrito_vendedor": VendorCartTool,
        "agregar_al_carrito": VendorAddTool,
        "quitar_del_carrito": VendorRemoveTool,
        "preparar_pedido": VendorOrderPreviewTool,
    }
    agent_tool_names = {
        "asistente": {
            "buscar_producto", "consultar_stock", "buscar_cliente", "contar_clientes",
            "contar_productos", "resumen_ventas", "productos_mas_vendidos",
            "productos_sin_ventas_recientes", "stock_critico",
        },
        "vendedor": {
            "buscar_producto", "consultar_stock", "buscar_cliente", "carrito_vendedor",
            "agregar_al_carrito", "quitar_del_carrito", "preparar_pedido",
        },
        "analista": {
            "resumen_ventas", "productos_mas_vendidos", "productos_sin_ventas_recientes",
            "stock_critico", "contar_clientes", "comparar_ventas", "clientes_inactivos",
        },
        "marketing": {
            "buscar_producto", "consultar_stock", "buscar_cliente", "clientes_inactivos",
            "productos_promocionables", "preparar_campana",
        },
    }

    @classmethod
    def provider(cls):
        provider = (os.getenv("AI_PROVIDER") or "lm_studio").strip().lower()
        if provider == "gemini":
            return GeminiProvider()
        if provider == "openai":
            return OpenAIProvider()
        if provider == "openai_compatible":
            return OpenAICompatibleProvider()
        return LMStudioProvider()

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
    def _history(cls, company_id, conversation_id, limit=20):
        rows = (
            db.session.query(ConversationMessage)
            .filter(
                ConversationMessage.company_id == company_id,
                ConversationMessage.conversation_id == conversation_id,
            )
            .order_by(ConversationMessage.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {"role": row.role, "content": str(row.content or "")}
            for row in reversed(rows)
            if row.role in {"user", "assistant"}
        ]

    @classmethod
    def _tool_definitions(cls, agent_key="asistente"):
        names = cls.agent_tool_names.get(agent_key, set())
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
        }.get(agent.name, "asistente")

    @classmethod
    def _execute_tool(cls, name, *, company_id, arguments, context=None):
        if not isinstance(arguments, dict):
            return {"success": False, "error": "arguments must be an object"}
        tool_class = cls.tool_registry.get(name)
        if tool_class is None:
            return {"success": False, "error": "tool_not_found"}
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

    @classmethod
    def _run_tool_loop(cls, *, provider, messages, tools, kwargs, company_id, context):
        working_messages = list(messages)
        response = provider.generate(messages=working_messages, tools=tools, **kwargs)
        campaign_context = None
        tool_rounds = 0

        while True:
            tool_calls = cls._tool_calls(response)
            final_content = cls._content(response)
            if not tool_calls:
                if final_content:
                    return final_content, campaign_context, tool_rounds
                raise RuntimeError("El proveedor IA no devolvió una respuesta.")

            tool_rounds += 1
            if tool_rounds > MAX_TOOL_TURNS:
                # Give the provider one final answer-only pass using the accumulated evidence.
                synthesis_prompt = {
                    "role": "user",
                    "content": (
                        "Terminá la respuesta usando exclusivamente los resultados de las herramientas ya ejecutadas. "
                        "No vuelvas a llamar herramientas y explicá claramente los datos obtenidos."
                    ),
                }
                final_messages = working_messages + [synthesis_prompt]
                response = provider.generate(messages=final_messages, tools=[], **kwargs)
                final_content = cls._content(response)
                if final_content:
                    return final_content, campaign_context, tool_rounds
                raise RuntimeError("El proveedor IA no pudo completar la respuesta después de consultar las herramientas.")

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
                result = cls._execute_tool(
                    name,
                    company_id=company_id,
                    arguments=args,
                    context=context,
                )
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
    ):
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
        access = can_use_ai(__import__("app").Company.query.filter_by(id=company_id).first(), agent_key)
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

        if idempotency_key:
            duplicate = (
                db.session.query(ConversationMessage)
                .filter(
                    ConversationMessage.company_id == company_id,
                    ConversationMessage.idempotency_key == idempotency_key,
                )
                .first()
            )
            if duplicate:
                return {
                    "status": "duplicate",
                    "conversation_id": conversation.id,
                    "message_id": duplicate.id,
                    "content": "",
                }

        history = cls._history(company_id, conversation.id, 19)
        trace_id = str(uuid.uuid4())
        incoming = ConversationMessage(
            conversation_id=conversation.id,
            company_id=company_id,
            sender_type="user",
            sender_id=sender_id,
            role="user",
            content=str(message),
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
        }[agent_key]
        if config and config.system_prompt:
            prompt += f"\n\nInstrucciones del comercio:\n{config.system_prompt}"

        messages = (
            [{"role": "system", "content": prompt}] if include_system_prompt else []
        ) + history + [{"role": "user", "content": str(message)}]
        kwargs = {}
        provider = provider_override or cls.provider()
        effective_model = cls._provider_model(provider, config)
        if effective_model:
            kwargs["model"] = effective_model
        if config:
            if config.temperature is not None:
                kwargs["temperature"] = float(config.temperature)
            if config.max_tokens is not None:
                kwargs["max_tokens"] = config.max_tokens

        context = {
            "conversation_id": conversation.id,
            "customer_phone": (metadata or {}).get("from") or "",
            "actor_user_id": sender_id,
        }
        final_content, campaign_context, tool_rounds = cls._run_tool_loop(
            provider=provider,
            messages=messages,
            tools=cls._tool_definitions(agent_key),
            kwargs=kwargs,
            company_id=company_id,
            context=context,
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

        record_ai_usage(
            company_id=company_id,
            agent_id=agent.id,
            conversation_id=conversation.id,
            user_id=sender_id,
            external_actor_id=(metadata or {}).get("from"),
            interaction_type=agent_key,
            message_id=assistant.id,
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

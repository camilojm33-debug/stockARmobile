"""Production-oriented runtime for StockARmobile AI agents."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict, Type

from stockarmobile.extensions import db
from stockarmobile.models.conversations import Agent, AgentConfiguration, Conversation, ConversationMessage
from services.ai_agent.config_service import BUSINESS_AGENT_NAME, VENDOR_AGENT_NAME, choose_agent
from services.ai_agent.providers.lm_studio import LMStudioProvider
from services.ai_agent.providers.openai_compatible import OpenAICompatibleProvider
from services.ai_agent.tools.base import AgentTool
from services.ai_agent.tools.business_metrics import ResumenVentasTool, StockCriticoTool
from services.ai_agent.tools.customer_search import BuscarClienteTool
from services.ai_agent.tools.product_search import BuscarProductoTool
from services.ai_agent.tools.stock_query import ConsultarStockTool

VENDOR_SYSTEM_PROMPT = """Sos el Vendedor 24 hs de StockARmobile. Representás al comercio de forma cordial, clara y comercial. Consultá siempre las herramientas antes de afirmar precio, stock o datos del cliente. No inventes información. Ayudá a elegir productos, cantidades y alternativas. Podés preparar una oportunidad de venta, pero nunca confirmes un cobro ni inventes que un pago fue recibido. Si el cliente pide una acción que no está disponible o requiere intervención humana, explicalo y derivá al comercio. Respondé en español argentino, breve y orientado a cerrar la venta."""
BUSINESS_SYSTEM_PROMPT = """Sos el Asistente empresarial de StockARmobile. Ayudás al dueño o administrador a entender su negocio. Usá las herramientas disponibles para consultar datos reales y nunca inventes cifras. Podés informar ventas del período, ticket promedio, stock crítico, precios, existencias y clientes. Explicá resultados de forma práctica y resumida. Las acciones que cambien dinero, stock, clientes o ventas deben requerir una operación explícita y segura; no las simules. Si una capacidad todavía no está disponible, decilo claramente y no improvises."""


class AgentRuntime:
    """Tenant-isolated runtime shared by web and WhatsApp channels."""

    tool_registry: Dict[str, Type[AgentTool]] = {
        "buscar_producto": BuscarProductoTool,
        "consultar_stock": ConsultarStockTool,
        "buscar_cliente": BuscarClienteTool,
        "resumen_ventas": ResumenVentasTool,
        "stock_critico": StockCriticoTool,
    }

    @classmethod
    def provider(cls):
        provider_name = (os.getenv("AI_PROVIDER") or "openai_compatible").strip().lower()
        if provider_name in {"lmstudio", "lm_studio"}:
            return LMStudioProvider()
        return OpenAICompatibleProvider()

    @classmethod
    def ensure_agent(cls, company_id: int, *, channel: str) -> Agent:
        return choose_agent(company_id, channel=channel)

    @classmethod
    def _config(cls, agent: Agent, company_id: int):
        return (
            db.session.query(AgentConfiguration)
            .filter(AgentConfiguration.agent_id == agent.id, AgentConfiguration.company_id == company_id)
            .order_by(AgentConfiguration.id.asc())
            .first()
        )

    @classmethod
    def _history(cls, company_id: int, conversation_id: int, limit: int = 20):
        rows = (
            db.session.query(ConversationMessage)
            .filter(ConversationMessage.company_id == company_id, ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.id.desc())
            .limit(limit)
            .all()
        )
        return [{"role": row.role, "content": str(row.content or "")} for row in reversed(rows) if row.role in {"user", "assistant"}]

    @classmethod
    def _tool_definitions(cls):
        return [
            {"type": "function", "function": {"name": name, "description": getattr(tool, "description", ""), "parameters": getattr(tool, "input_schema", {"type": "object", "properties": {}})}}
            for name, tool in cls.tool_registry.items()
        ]

    @classmethod
    def _execute_tool(cls, name: str, *, company_id: int, arguments: Dict[str, Any]):
        tool_class = cls.tool_registry.get(name)
        if tool_class is None:
            return {"success": False, "error": "tool_not_found"}
        if "company_id" in arguments:
            return {"success": False, "error": "company_id_not_allowed"}
        return tool_class(company_id=company_id).execute(**arguments)

    @classmethod
    def process(cls, *, company_id: int, conversation_id: int, message: str, channel: str, sender_id=None, external_message_id=None, idempotency_key=None, metadata=None):
        conversation = db.session.query(Conversation).filter(Conversation.id == conversation_id, Conversation.company_id == company_id).first()
        if conversation is None:
            raise ValueError("Conversation no encontrada para la empresa.")
        agent = db.session.query(Agent).filter(Agent.id == conversation.agent_id, Agent.company_id == company_id).first()
        if agent is None:
            agent = cls.ensure_agent(company_id, channel=channel)
            conversation.agent_id = agent.id
            conversation.channel = channel
            db.session.flush()
        if idempotency_key:
            duplicate = db.session.query(ConversationMessage).filter(ConversationMessage.company_id == company_id, ConversationMessage.idempotency_key == idempotency_key).first()
            if duplicate:
                return {"status": "duplicate", "conversation_id": conversation.id, "message_id": duplicate.id, "content": ""}

        incoming = ConversationMessage(
            conversation_id=conversation.id, company_id=company_id, sender_type="user", sender_id=sender_id, role="user",
            content=str(message), content_type="text", external_message_id=external_message_id, idempotency_key=idempotency_key,
            trace_id=str(uuid.uuid4()), metadata_json=metadata or {},
        )
        db.session.add(incoming)
        db.session.flush()

        config = cls._config(agent, company_id)
        prompt = VENDOR_SYSTEM_PROMPT if agent.name == VENDOR_AGENT_NAME else BUSINESS_SYSTEM_PROMPT
        if config and getattr(config, "system_prompt", None):
            prompt = f"{prompt}\n\nInstrucciones adicionales del comercio:\n{config.system_prompt}"
        messages = [{"role": "system", "content": prompt}] + cls._history(company_id, conversation.id, 20) + [{"role": "user", "content": str(message)}]

        kwargs = {}
        if config:
            if getattr(config, "model", None): kwargs["model"] = config.model
            if getattr(config, "temperature", None) is not None: kwargs["temperature"] = config.temperature
            if getattr(config, "max_tokens", None) is not None: kwargs["max_tokens"] = config.max_tokens

        provider = cls.provider()
        response = provider.generate(messages=messages, tools=cls._tool_definitions(), **kwargs)
        tool_call = response.get("tool_call") if isinstance(response, dict) else None
        final_content = response.get("content") if isinstance(response, dict) else None

        if tool_call:
            name = tool_call.get("name")
            args = tool_call.get("arguments") or {}
            result = cls._execute_tool(name, company_id=company_id, arguments=args)
            tool_id = tool_call.get("id") or "call_1"
            tool_messages = messages + [
                {"role": "assistant", "content": None, "tool_calls": [{"id": tool_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]},
                {"role": "tool", "tool_call_id": tool_id, "content": json.dumps(result, ensure_ascii=False)},
            ]
            response = provider.generate(messages=tool_messages, **kwargs)
            final_content = response.get("content") if isinstance(response, dict) else None

        if not final_content:
            raise RuntimeError("El proveedor IA no devolvió una respuesta.")

        assistant = ConversationMessage(
            conversation_id=conversation.id, company_id=company_id, sender_type="agent", sender_id=agent.id, role="assistant",
            content=str(final_content), content_type="text", external_message_id=None, idempotency_key=None,
            trace_id=incoming.trace_id, metadata_json={"channel": channel, "agent_name": agent.name},
        )
        db.session.add(assistant)
        db.session.commit()
        return {"status": "completed", "conversation_id": conversation.id, "agent_id": agent.id, "message_id": incoming.id, "assistant_message_id": assistant.id, "content": str(final_content), "trace_id": incoming.trace_id}

"""Minimal AI Agent orchestration layer for tenant-isolated conversations."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Type

from stockarmobile.extensions import db
from stockarmobile.models.conversations import (
    Agent,
    AgentConfiguration,
    Conversation,
    ConversationMessage,
)
from services.ai_agent.decision import ToolDecision
from services.ai_agent.providers.lm_studio import LMStudioProvider
from services.ai_agent.tools.base import AgentTool
from services.ai_agent.tools.customer_search import BuscarClienteTool
from services.ai_agent.tools.product_search import BuscarProductoTool
from services.ai_agent.tools.stock_query import ConsultarStockTool


class AgentOrchestrator:
    _tool_registry = {
        "buscar_producto": BuscarProductoTool,
        "consultar_stock": ConsultarStockTool,
        "buscar_cliente": BuscarClienteTool,
    }

    @classmethod
    def get_tool(cls, name: str) -> Optional[Type[AgentTool]]:
        if not name:
            return None
        return cls._tool_registry.get(name)

    @classmethod
    def build_tool(cls, name, *, company_id, **kwargs):
        if company_id in (None, ""):
            raise ValueError("company_id is required")

        ToolClass = cls.get_tool(name)
        if ToolClass is None:
            return None

        if "company_id" in kwargs:
            raise ValueError("company_id cannot be provided via kwargs")

        return ToolClass(company_id=company_id, **kwargs)

    @classmethod
    def _get_recent_history(cls, *, company_id, conversation_id, limit=20, exclude_message_ids=None):
        exclude_message_ids = set(exclude_message_ids or [])
        query = (
            db.session.query(ConversationMessage)
            .filter(
                ConversationMessage.company_id == company_id,
                ConversationMessage.conversation_id == conversation_id,
            )
            .order_by(ConversationMessage.id.asc())
        )
        if exclude_message_ids:
            query = query.filter(~ConversationMessage.id.in_(sorted(exclude_message_ids)))

        rows = query.limit(limit).all()

        messages = []
        for row in rows:
            role = row.role or "user"
            if row.role in ("assistant", "user", "tool"):
                messages.append({"role": role, "content": str(row.content)})
        return messages

    @classmethod
    def execute_tool(
        cls,
        name,
        *,
        company_id,
        arguments=None,
    ):
        if company_id in (None, ""):
            raise ValueError("company_id is required")

        ToolClass = cls.get_tool(name)
        if ToolClass is None:
            return {"success": False, "error": "tool_not_found"}

        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return {"success": False, "error": "arguments must be a dict"}

        if "company_id" in arguments:
            return {"success": False, "error": "company_id must be passed explicitly"}

        tool = cls.build_tool(name, company_id=company_id, **arguments)
        if tool is None:
            return {"success": False, "error": "tool_not_found"}

        result = tool.execute(**arguments)
        if not isinstance(result, dict):
            return {"success": False, "error": "tool result must be a dict"}
        return result

    @classmethod
    def execute_decision(
        cls,
        decision,
        *,
        company_id,
    ):
        if company_id in (None, ""):
            raise ValueError("company_id is required")

        if not isinstance(decision, ToolDecision):
            return {"success": False, "error": "invalid_tool_decision"}

        if "company_id" in decision.arguments:
            return {"success": False, "error": "company_id must be passed explicitly"}

        return cls.execute_tool(
            decision.tool_name,
            company_id=company_id,
            arguments=decision.arguments,
        )

    @classmethod
    def handle_message(
        cls,
        *,
        company_id,
        conversation_id,
        message,
        channel=None,
        sender_id=None,
        metadata=None,
    ) -> Dict[str, Any]:
        if company_id in (None, ""):
            raise ValueError("company_id is required")
        if conversation_id in (None, ""):
            raise ValueError("conversation_id is required")
        if message in (None, ""):
            raise ValueError("message is required")

        metadata = metadata or {}
        idempotency_key = metadata.get("idempotency_key")

        conversation = (
            db.session.query(Conversation)
            .filter(Conversation.id == conversation_id, Conversation.company_id == company_id)
            .first()
        )

        if conversation is None:
            raise ValueError(
                "Conversation not found for the given company_id and conversation_id"
            )

        agent = (
            db.session.query(Agent)
            .filter(Agent.id == conversation.agent_id, Agent.company_id == company_id)
            .first()
            if conversation.agent_id is not None
            else None
        )

        if idempotency_key is not None:
            existing_message = (
                db.session.query(ConversationMessage)
                .filter(
                    ConversationMessage.company_id == company_id,
                    ConversationMessage.idempotency_key == idempotency_key,
                )
                .first()
            )
            if existing_message is not None:
                return {
                    "conversation_id": conversation.id,
                    "company_id": company_id,
                    "agent_id": agent.id if agent else None,
                    "message_id": existing_message.id,
                    "status": "duplicate",
                }

        try:
            provider = LMStudioProvider()
            history_messages = cls._get_recent_history(
                company_id=company_id,
                conversation_id=conversation.id,
                limit=20,
            )

            msg = ConversationMessage(
                conversation_id=conversation.id,
                company_id=company_id,
                sender_type="user" if sender_id is None else "agent",
                sender_id=sender_id,
                role="user",
                content=str(message),
                content_type="text",
                external_message_id=None,
                idempotency_key=idempotency_key,
                trace_id=None,
                metadata_json=metadata,
            )

            db.session.add(msg)
            db.session.flush()

            messages = history_messages + [{"role": "user", "content": str(message)}]
            if len(messages) > 20:
                messages = messages[-20:]
            model = None
            temperature = None
            max_tokens = None

            if agent is not None:
                config = (
                    db.session.query(AgentConfiguration)
                    .filter(
                        AgentConfiguration.agent_id == agent.id,
                        AgentConfiguration.company_id == company_id,
                    )
                    .order_by(AgentConfiguration.id.asc())
                    .first()
                )
                if config is not None:
                    model = config.model if getattr(config, "model", None) else None
                    temperature = config.temperature if getattr(config, "temperature", None) is not None else None
                    max_tokens = config.max_tokens if getattr(config, "max_tokens", None) is not None else None

            tool_definitions = []
            for tool_name, ToolClass in cls._tool_registry.items():
                tool_definitions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": getattr(ToolClass, "description", ""),
                            "parameters": getattr(ToolClass, "input_schema", {"type": "object", "properties": {}}),
                        },
                    }
                )

            payload = {"messages": messages, "tools": tool_definitions}
            if model is not None:
                payload["model"] = model
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens

            response = provider.generate(**payload)
            if not isinstance(response, dict):
                raise ValueError("LM Studio provider returned an invalid response")

            tool_call = response.get("tool_call")
            final_content = response.get("content")
            if tool_call is None:
                if final_content is None:
                    raise ValueError("LM Studio provider returned no content")
                assistant_msg = ConversationMessage(
                    conversation_id=conversation.id,
                    company_id=company_id,
                    sender_type="agent",
                    sender_id=agent.id if agent else None,
                    role="assistant",
                    content=str(final_content),
                    content_type="text",
                    external_message_id=None,
                    idempotency_key=None,
                    trace_id=None,
                    metadata_json={},
                )
                db.session.add(assistant_msg)
                db.session.flush()
                db.session.commit()
                return {
                    "conversation_id": conversation.id,
                    "company_id": company_id,
                    "agent_id": agent.id if agent else None,
                    "message_id": msg.id,
                    "assistant_message_id": assistant_msg.id,
                    "status": "completed",
                    "content": str(final_content),
                }

            if not isinstance(tool_call, dict):
                raise ValueError("LM Studio tool_call is malformed")

            tool_name = tool_call.get("name")
            tool_arguments = tool_call.get("arguments")
            if not tool_name or not isinstance(tool_arguments, dict):
                raise ValueError("LM Studio tool_call is malformed")

            if tool_name not in cls._tool_registry:
                raise ValueError(f"Tool not allowed: {tool_name}")

            tool_arguments = dict(tool_arguments)
            if "company_id" in tool_arguments:
                raise ValueError("company_id cannot be provided by the model")

            tool_result = cls.execute_tool(
                tool_name,
                company_id=company_id,
                arguments=tool_arguments,
            )
            if not isinstance(tool_result, dict) or tool_result.get("success") is False:
                error = tool_result.get("error") if isinstance(tool_result, dict) else "tool_execution_failed"
                raise ValueError(f"Tool execution failed: {error}")

            tool_call_id = "call_1"
            base_history = cls._get_recent_history(
                company_id=company_id,
                conversation_id=conversation.id,
                limit=20,
            )

            tool_call_message = ConversationMessage(
                conversation_id=conversation.id,
                company_id=company_id,
                sender_type="agent",
                sender_id=agent.id if agent else None,
                role="assistant",
                content=json.dumps(
                    {
                        "tool_call": {
                            "id": tool_call_id,
                            "name": tool_name,
                            "arguments": tool_arguments,
                        }
                    },
                    ensure_ascii=False,
                ),
                content_type="json",
                external_message_id=None,
                idempotency_key=None,
                trace_id=None,
                metadata_json={"tool_call_id": tool_call_id, "tool_name": tool_name},
            )
            db.session.add(tool_call_message)
            db.session.flush()

            tool_result_message = ConversationMessage(
                conversation_id=conversation.id,
                company_id=company_id,
                sender_type="tool",
                sender_id=None,
                role="tool",
                content=json.dumps(tool_result, ensure_ascii=False),
                content_type="json",
                external_message_id=None,
                idempotency_key=None,
                trace_id=None,
                metadata_json={"tool_call_id": tool_call_id, "tool_name": tool_name},
            )
            db.session.add(tool_result_message)
            db.session.flush()

            final_messages = base_history + [
                {"role": "assistant", "content": None, "tool_calls": [{"id": tool_call_id, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(tool_arguments)}}]},
                {"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(tool_result, ensure_ascii=False)},
            ]
            if len(final_messages) > 20:
                final_messages = final_messages[-20:]

            second_payload = {"messages": final_messages}
            if model is not None:
                second_payload["model"] = model
            if temperature is not None:
                second_payload["temperature"] = temperature
            if max_tokens is not None:
                second_payload["max_tokens"] = max_tokens

            final_response = provider.generate(**second_payload)
            if not isinstance(final_response, dict):
                raise ValueError("LM Studio final response is invalid")

            final_content = final_response.get("content")
            if final_response.get("tool_call") is not None or final_content is None:
                raise ValueError("LM Studio final response is invalid")

            assistant_msg = ConversationMessage(
                conversation_id=conversation.id,
                company_id=company_id,
                sender_type="agent",
                sender_id=agent.id if agent else None,
                role="assistant",
                content=str(final_content),
                content_type="text",
                external_message_id=None,
                idempotency_key=None,
                trace_id=None,
                metadata_json={},
            )

            db.session.add(assistant_msg)
            db.session.flush()
            db.session.commit()

            return {
                "conversation_id": conversation.id,
                "company_id": company_id,
                "agent_id": agent.id if agent else None,
                "message_id": msg.id,
                "assistant_message_id": assistant_msg.id,
                "status": "completed",
                "content": str(final_content),
            }
        except Exception:
            db.session.rollback()
            raise

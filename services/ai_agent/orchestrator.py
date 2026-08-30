"""Compatibility facade for the StockARmobile AI agent runtime."""

from __future__ import annotations

from services.ai_agent.providers.openai_compatible import OpenAICompatibleProvider
from services.ai_agent.orchestrator_v2 import AgentRuntime

LMStudioProvider = OpenAICompatibleProvider


class AgentOrchestrator:
    """Backwards-compatible facade used by the dashboard and existing callers."""

    _tool_registry = AgentRuntime.tool_registry

    @classmethod
    def get_tool(cls, name):
        return cls._tool_registry.get(name)

    @classmethod
    def build_tool(cls, name, *, company_id, **kwargs):
        tool_class = cls.get_tool(name)
        if tool_class is None:
            return None
        if company_id in (None, ""):
            raise ValueError("company_id is required")
        if "company_id" in kwargs:
            raise ValueError("company_id cannot be provided via kwargs")
        return tool_class(company_id=company_id, **kwargs)

    @classmethod
    def execute_tool(cls, name, *, company_id, arguments=None):
        return AgentRuntime._execute_tool(name, company_id=company_id, arguments=arguments or {}, context={})

    @classmethod
    def handle_message(cls, *, company_id, conversation_id, message, channel=None, sender_id=None, metadata=None):
        if company_id in (None, ""):
            raise ValueError("company_id is required")
        if conversation_id in (None, ""):
            raise ValueError("conversation_id is required")
        if message in (None, ""):
            raise ValueError("message is required")
        return AgentRuntime.process(
            company_id=company_id,
            conversation_id=conversation_id,
            message=message,
            channel=channel or "web",
            sender_id=sender_id,
            idempotency_key=(metadata or {}).get("idempotency_key"),
            metadata=metadata or {},
            include_system_prompt=False,
            provider_override=LMStudioProvider(),
        )

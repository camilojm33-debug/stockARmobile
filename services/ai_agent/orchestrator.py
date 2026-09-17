"""Compatibility facade for the StockARmobile AI agent runtime."""

from __future__ import annotations

import re
import unicodedata

from services.ai_agent.providers.openai_compatible import OpenAICompatibleProvider
from services.ai_agent.orchestrator_v2 import AgentRuntime
from services.ai_agent.business_intelligence import install_ai_intelligence_tools, SalesAnomalyTool

install_ai_intelligence_tools(AgentRuntime)


def _safe_tool_name(name: str) -> str:
    """Return a conservative provider-compatible function name."""
    normalized = unicodedata.normalize("NFKD", str(name or ""))
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    safe = re.sub(r"[^A-Za-z0-9_]", "_", ascii_name).strip("_")
    safe = re.sub(r"_+", "_", safe)
    return safe or "tool"


def _normalize_tool_contract(runtime_cls) -> None:
    """Keep registry aliases and per-agent tool sets aligned with safe names."""
    aliases = {}
    for original, tool_class in list(runtime_cls.tool_registry.items()):
        safe = _safe_tool_name(original)
        if safe != original:
            aliases[original] = safe
            runtime_cls.tool_registry.setdefault(safe, tool_class)
    for agent_key, names in list(runtime_cls.agent_tool_names.items()):
        runtime_cls.agent_tool_names[agent_key] = {aliases.get(name, name) for name in names}


# Explicit legacy alias plus a generic normalization pass. This prevents one
# non-ASCII function declaration from breaking the whole provider request.
AgentRuntime.tool_registry.pop("anomalías_ventas", None)
AgentRuntime.tool_registry["anomalias_ventas"] = SalesAnomalyTool
AgentRuntime.agent_tool_names.setdefault("analista", set()).discard("anomalías_ventas")
AgentRuntime.agent_tool_names.setdefault("analista", set()).add("anomalias_ventas")
_normalize_tool_contract(AgentRuntime)

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

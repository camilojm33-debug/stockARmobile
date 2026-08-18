"""Customer search tool for the AI Agent."""

from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import or_

from app import Client
from services.ai_agent.tools.base import AgentTool


class BuscarClienteTool(AgentTool):
    """Search clients within the tenant/company scope only."""

    name = "buscar_cliente"
    description = "Busca clientes por nombre, email, teléfono o WhatsApp dentro de la empresa actual."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    }

    def __init__(self, *, company_id: Any, **kwargs: Any) -> None:
        super().__init__(company_id=company_id, **kwargs)

    def execute(self, *, company_id=None, query=None, limit=None, **kwargs: Any) -> Dict[str, Any]:
        effective_company_id = self.company_id if company_id is None else company_id
        if effective_company_id in (None, ""):
            return {"success": False, "error": "company_id is required", "items": []}

        if company_id is not None and company_id != self.company_id:
            return {"success": False, "error": "company_id mismatch", "items": []}

        if query in (None, ""):
            return {"success": False, "error": "query is required", "items": []}

        normalized_query = str(query).strip()
        if not normalized_query:
            return {"success": False, "error": "query is required", "items": []}

        if "company_id" in (kwargs or {}):
            return {"success": False, "error": "company_id must be passed explicitly and cannot be inferred from metadata", "items": []}

        try:
            max_limit = 20
            requested_limit = int(limit) if limit is not None else 10
        except (TypeError, ValueError):
            return {"success": False, "error": "limit must be an integer", "items": []}

        if requested_limit <= 0:
            requested_limit = 10
        if requested_limit > max_limit:
            requested_limit = max_limit

        like = f"%{normalized_query}%"
        clients = (
            Client.query.filter(
                Client.company_id == effective_company_id,
                Client.active.is_(True),
                or_(
                    Client.name.ilike(like),
                    Client.email.ilike(like),
                    Client.phone.ilike(like),
                    Client.whatsapp.ilike(like),
                ),
            )
            .limit(requested_limit)
            .all()
        )

        items = []
        for client in clients:
            items.append(
                {
                    "id": client.id,
                    "name": client.name,
                    "email": client.email,
                    "phone": client.phone,
                    "whatsapp": client.whatsapp,
                }
            )

        return {"success": True, "items": items}

"""Read-only CRM context for tenant AI agents."""
from services.ai_agent.tools.base import AgentTool


class CRMOpportunitiesTool(AgentTool):
    name = "oportunidades_crm"
    description = "Consulta oportunidades comerciales reales del CRM del comercio, sin modificar ventas, presupuestos ni campañas."
    input_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["open", "closed", "all"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": [],
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        from crm_models import CRMOpportunity
        from stockarmobile.extensions import db

        status = str(kwargs.get("status") or "open").strip().lower()
        limit = min(20, max(1, int(kwargs.get("limit") or 10)))
        query = CRMOpportunity.query.filter(CRMOpportunity.company_id == self.company_id)
        if status in {"open", "closed"}:
            query = query.filter(CRMOpportunity.status == status)
        rows = query.order_by(CRMOpportunity.probability.desc(), CRMOpportunity.value.desc()).limit(limit).all()
        return {
            "success": True,
            "count": len(rows),
            "opportunities": [
                {
                    "id": row.id,
                    "title": row.title,
                    "client": row.client.name if row.client else None,
                    "stage": row.stage,
                    "status": row.status,
                    "value": float(row.value or 0),
                    "probability": float(row.probability or 0),
                    "weighted_value": row.weighted_value,
                    "expected_close_date": row.expected_close_date.isoformat() if row.expected_close_date else None,
                }
                for row in rows
            ],
        }

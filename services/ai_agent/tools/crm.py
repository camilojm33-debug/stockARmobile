"""Read-only CRM context for tenant AI agents."""
from services.ai_agent.tools.base import AgentTool


def crm_tool_access(company_id, user_id):
    from app import Company, User
    from services.ai_agent.usage_service import can_use_ai_feature
    from stockarmobile.permissions import has_any_permission, user_role

    company = Company.query.filter_by(id=int(company_id)).first()
    entitlement = can_use_ai_feature(company, "crm") if company is not None else None
    if entitlement is None or not entitlement.allowed:
        return False, entitlement.reason if entitlement else "La empresa no está disponible."
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False, "El CRM requiere un usuario autenticado con permiso CRM."
    user = User.query.filter_by(id=user_id, company_id=int(company_id), active=True).first()
    if user is None:
        return False, "El usuario no pertenece a esta empresa."
    if user_role(user) not in {"admin", "superadmin"} and not has_any_permission(user, {"crm"}):
        return False, "Tu usuario no tiene permiso para consultar el CRM."
    return True, None


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
        allowed, reason = crm_tool_access(
            self.company_id,
            self._context.get("actor_user_id"),
        )
        if not allowed:
            return {"success": False, "error": reason}
        from crm_models import CRMOpportunity

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

"""Central AI plan permissions and usage derived from successful responses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from stockarmobile.extensions import db
from stockarmobile.helpers.dates import utcnow_naive
from stockarmobile.models.conversations import ConversationMessage


AI_PLANS = (
    {"code": "inicio", "name": "Inicio", "price": "$11.385 / mes", "limit": 300, "agents": ("asistente",), "tagline": "Tu negocio empieza a trabajar con IA", "badge": None, "invoice_processing": False},
    {"code": "vendedor", "name": "Vendedor", "price": "$22.885 / mes", "limit": 1500, "agents": ("asistente", "vendedor"), "tagline": "Tu vendedor trabaja 24/7", "badge": None, "invoice_processing": False},
    {"code": "negocio", "name": "Negocio IA", "price": "$45.885 / mes", "limit": 5000, "agents": ("asistente", "vendedor", "analista"), "tagline": "Convertí tus datos en decisiones", "badge": "RECOMENDADO", "invoice_processing": False},
    {"code": "pro", "name": "IA PRO", "price": "$110.000 / mes", "limit": 15000, "agents": ("asistente", "vendedor", "analista", "marketing"), "tagline": "La IA completa para tu negocio", "badge": "PLAN PREMIUM", "invoice_processing": True},
)
AI_PLAN_BY_CODE = {plan["code"]: plan for plan in AI_PLANS}
AGENT_LABELS = {"vendedor": "Vendedor IA", "asistente": "Asistente Empresarial", "analista": "Analista IA", "marketing": "Marketing IA"}
AI_FEATURE_LABELS = {"facturas": "Reconocimiento de facturas con IA"}


@dataclass(frozen=True)
class AIAccess:
    allowed: bool
    reason: str | None = None
    plan: dict[str, Any] | None = None


def _preferences(company) -> dict[str, Any]:
    from services.ai_agent.config_service import get_ai_preferences

    return get_ai_preferences(company)["ai_agent"]


def current_plan(company) -> dict[str, Any] | None:
    return AI_PLAN_BY_CODE.get(str(_preferences(company).get("plan_code") or "").strip().lower())


def _effective_status(prefs: dict[str, Any], *, now: datetime) -> str:
    """Read-only projection of the AI subscription status (no write); real expiry persistence
    happens in AISubscriptionService.expire_if_needed(), invoked from the Super Admin panel."""
    status = str(prefs.get("status") or "").strip().upper()
    ends_at = prefs.get("ends_at")
    if status in {"TRIAL", "ACTIVA"} and ends_at:
        try:
            if datetime.fromisoformat(str(ends_at)) < now:
                return "VENCIDA"
        except (TypeError, ValueError):
            pass
    return status


def can_use_ai(company, agent: str, *, now: datetime | None = None) -> AIAccess:
    plan = current_plan(company)
    if plan is None:
        return AIAccess(False, "Tu empresa todavía no tiene un plan IA asignado.", None)
    effective_now = now or utcnow_naive()
    status = _effective_status(_preferences(company), now=effective_now)
    # Compania legacy sin status asignado (nunca administrada por AISubscriptionService): permitir, no romper flujo existente.
    if status in {"SUSPENDIDA", "CANCELADA", "VENCIDA"}:
        labels = {"SUSPENDIDA": "suspendido", "CANCELADA": "cancelado", "VENCIDA": "vencido"}
        return AIAccess(False, f"El plan IA de tu empresa está {labels[status]}. Contactá a soporte.", plan)
    if status == "PENDIENTE":
        return AIAccess(False, "Tu plan IA está pendiente de activación.", plan)
    if agent in AI_FEATURE_LABELS:
        if not plan.get("invoice_processing", False):
            required = next((item["name"] for item in AI_PLANS if item.get("invoice_processing")), "un plan superior")
            return AIAccess(False, f"{AI_FEATURE_LABELS[agent]} requiere {required} o superior.", plan)
    elif agent not in plan["agents"]:
        required = next((item["name"] for item in AI_PLANS if agent in item["agents"]), "un plan superior")
        return AIAccess(False, f"{AGENT_LABELS.get(agent, 'Este agente')} requiere {required} o superior.", plan)
    snapshot = usage_snapshot(company.id, now=now)
    if snapshot["used_usage"] >= snapshot["included_usage"]:
        return AIAccess(False, f"Alcanzaste el límite mensual de IA. Tu plan incluye {plan['limit']:,} interacciones mensuales.".replace(",", "."), plan)
    return AIAccess(True, None, plan)


def _period_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage_snapshot(company_id: int, *, now: datetime | None = None) -> dict[str, Any]:
    from app import Company

    company = Company.query.filter_by(id=company_id).first()
    plan = current_plan(company) if company is not None else None
    period_start = _period_start(now or utcnow_naive())
    query = ConversationMessage.query.filter(
        ConversationMessage.company_id == company_id,
        ConversationMessage.role == "assistant",
        ConversationMessage.created_at >= period_start,
    )
    # Only responses marked by record_ai_usage count. Older messages remain history.
    messages = query.all()
    counted = [message for message in messages if isinstance(message.metadata_json, dict) and message.metadata_json.get("ai_usage_recorded")]
    used = len(counted)
    included = int(plan["limit"]) if plan else 0
    percent = min(100, round((used / included) * 100)) if included else 0
    state = "normal" if percent < 80 else "near_limit" if percent < 90 else "limit_next" if percent < 100 else "limit_reached"
    by_agent = {}
    for message in counted:
        agent = (message.metadata_json or {}).get("agent_key") or "asistente"
        by_agent[agent] = by_agent.get(agent, 0) + 1
    return {
        "period": period_start.strftime("%Y-%m"),
        "included_usage": included,
        "used_usage": used,
        "remaining_usage": max(included - used, 0),
        "percent": percent,
        "state": state,
        "by_agent": by_agent,
        "overage_enabled": False,
    }


def usage_history(company_id: int) -> list[dict[str, Any]]:
    """Return preserved monthly usage periods from the conversation history."""
    messages = ConversationMessage.query.filter(
        ConversationMessage.company_id == company_id,
        ConversationMessage.role == "assistant",
    ).all()
    periods: dict[str, dict[str, Any]] = {}
    for message in messages:
        metadata = message.metadata_json if isinstance(message.metadata_json, dict) else {}
        if not metadata.get("ai_usage_recorded"):
            continue
        period = str(metadata.get("ai_usage_period") or message.created_at.strftime("%Y-%m"))
        item = periods.setdefault(period, {"period": period, "used_usage": 0, "by_agent": {}})
        item["used_usage"] += 1
        agent = metadata.get("agent_key") or "asistente"
        item["by_agent"][agent] = item["by_agent"].get(agent, 0) + 1
    return [periods[key] for key in sorted(periods, reverse=True)]


def record_ai_usage(*, company_id: int, agent_id: int, conversation_id: int, user_id: int | None, external_actor_id: str | None = None, interaction_type: str, message_id: int) -> bool:
    """Mark one successful assistant response as the single usage event."""
    message = ConversationMessage.query.filter_by(
        id=message_id,
        company_id=company_id,
        conversation_id=conversation_id,
        role="assistant",
    ).first()
    if message is None:
        return False
    metadata = dict(message.metadata_json or {}) if isinstance(message.metadata_json, dict) else {}
    if metadata.get("ai_usage_recorded"):
        return False
    metadata.update({
        "ai_usage_recorded": True,
        "ai_usage_period": utcnow_naive().strftime("%Y-%m"),
        "agent_id": agent_id,
        "agent_key": metadata.get("agent_key") or interaction_type,
        "actor_user_id": user_id,
        "external_actor_id": external_actor_id,
        "interaction_type": interaction_type,
    })
    message.metadata_json = metadata
    return True

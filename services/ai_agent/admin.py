"""Admin control panel for StockARmobile AI agents."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from stockarmobile.decorators import company_admin_required
from stockarmobile.extensions import db
from stockarmobile.tenant import get_current_company_id
from stockarmobile.models.conversations import Agent, AgentConfiguration, Conversation, ConversationMessage
from services.ai_agent.config_service import (
    BUSINESS_AGENT_NAME,
    VENDOR_AGENT_NAME,
    configure_whatsapp_connection,
    ensure_default_agents,
    get_ai_preferences,
    get_whatsapp_connection,
    update_ai_preferences,
)

bp = Blueprint("ai_admin", __name__, url_prefix="/dashboard/ai-agent")

_DEFAULT_VENDOR_PROMPT = (
    "Sos el Vendedor 24 hs del comercio. Consultá siempre los datos reales antes de informar precio o stock. "
    "Ayudá a elegir productos, armar pedidos y orientar al cliente hacia el pago. "
    "Nunca inventes promociones, descuentos, stock ni confirmaciones de pago."
)
_DEFAULT_BUSINESS_PROMPT = (
    "Sos el Asistente empresarial del comercio. Consultá las herramientas antes de informar cifras. "
    "Mostrá resultados claros y accionables. No inventes datos ni ejecutes operaciones financieras o de stock sin una acción explícita."
)


def _default_model() -> str:
    return (os.getenv("AI_PROVIDER_MODEL") or "gpt-4.1-mini").strip()


def _ensure_configs(company_id: int, agents: dict[str, Agent]) -> dict[str, AgentConfiguration]:
    defaults = {VENDOR_AGENT_NAME: _DEFAULT_VENDOR_PROMPT, BUSINESS_AGENT_NAME: _DEFAULT_BUSINESS_PROMPT}
    configs: dict[str, AgentConfiguration] = {}
    for name, agent in agents.items():
        config = (
            db.session.query(AgentConfiguration)
            .filter(AgentConfiguration.agent_id == agent.id, AgentConfiguration.company_id == company_id)
            .order_by(AgentConfiguration.id.asc())
            .first()
        )
        if config is None:
            config = AgentConfiguration(
                agent_id=agent.id,
                company_id=company_id,
                model=_default_model(),
                system_prompt=defaults.get(name, ""),
                language="es-AR",
                max_tokens=700,
                temperature=Decimal("0.20"),
            )
            db.session.add(config)
            db.session.flush()
        configs[name] = config
    return configs


def _safe_int(raw: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(raw or "").strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _safe_decimal(raw: str | None, default: Decimal) -> Decimal:
    try:
        value = Decimal(str(raw or "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return default
    if value < Decimal("0"):
        return Decimal("0")
    if value > Decimal("2"):
        return Decimal("2")
    return value.quantize(Decimal("0.01"))


@bp.get("")
@company_admin_required
def index():
    company_id = get_current_company_id(current_user)
    from app import Company

    company = Company.query.get(company_id)
    if company is None:
        return redirect(url_for("dashboard.index"))
    agents = ensure_default_agents(company_id)
    configs = _ensure_configs(company_id, agents)
    prefs = get_ai_preferences(company)
    whatsapp = get_whatsapp_connection(company)

    conversations = (
        db.session.query(Conversation)
        .filter(Conversation.company_id == company_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(12)
        .all()
    )
    agent_ids = {agent.id: agent.name for agent in agents.values()}
    conversation_rows = []
    for conversation in conversations:
        latest = (
            db.session.query(ConversationMessage)
            .filter(ConversationMessage.company_id == company_id, ConversationMessage.conversation_id == conversation.id)
            .order_by(ConversationMessage.id.desc())
            .first()
        )
        conversation_rows.append({
            "id": conversation.id,
            "agent_name": agent_ids.get(conversation.agent_id, "Agente"),
            "channel": conversation.channel,
            "external_id": conversation.external_conversation_id,
            "status": conversation.status,
            "updated_at": conversation.updated_at,
            "last_message": (latest.content[:180] if latest else ""),
        })

    provider = (os.getenv("AI_PROVIDER") or "openai_compatible").strip().lower()
    ai_key_configured = bool((os.getenv("AI_PROVIDER_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()) or provider in {"lmstudio", "lm_studio"}
    return render_template(
        "ai_agent/admin.html",
        agents=agents,
        configs=configs,
        prefs=prefs,
        whatsapp=whatsapp,
        ai_key_configured=ai_key_configured,
        provider=provider,
        provider_model=_default_model(),
        webhook_url=url_for("whatsapp_agent.webhook", _external=True),
        conversations=conversation_rows,
    )


@bp.post("/save")
@company_admin_required
def save():
    company_id = get_current_company_id(current_user)
    from app import Company, record_audit

    company = Company.query.get(company_id)
    if company is None:
        flash("No se encontró la empresa activa.", "danger")
        return redirect(url_for("dashboard.index"))

    agents = ensure_default_agents(company_id)
    configs = _ensure_configs(company_id, agents)
    agents[VENDOR_AGENT_NAME].active = request.form.get("vendor_enabled") == "1"
    agents[BUSINESS_AGENT_NAME].active = request.form.get("business_enabled") == "1"

    for name, prefix in ((VENDOR_AGENT_NAME, "vendor"), (BUSINESS_AGENT_NAME, "business")):
        config = configs[name]
        model = (request.form.get(f"{prefix}_model") or "").strip()[:120]
        if model:
            config.model = model
        config.system_prompt = (request.form.get(f"{prefix}_prompt") or "").strip()[:12000]
        config.language = (request.form.get(f"{prefix}_language") or "es-AR").strip()[:8]
        config.max_tokens = _safe_int(request.form.get(f"{prefix}_max_tokens"), 700, 128, 4000)
        config.temperature = _safe_decimal(request.form.get(f"{prefix}_temperature"), Decimal("0.20"))

    update_ai_preferences(
        company,
        ai_updates={
            "enabled": request.form.get("ai_enabled") == "1",
            "whatsapp_enabled": request.form.get("whatsapp_enabled") == "1",
        },
    )

    configure_whatsapp_connection(
        company,
        phone_number_id=(request.form.get("phone_number_id") or "").strip(),
        access_token=(request.form.get("access_token") or "").strip() or None,
        business_account_id=(request.form.get("business_account_id") or "").strip(),
        display_phone_number=(request.form.get("display_phone_number") or "").strip(),
        enabled=request.form.get("whatsapp_enabled") == "1",
        template_name=(request.form.get("template_name") or "").strip(),
        template_language=(request.form.get("template_language") or "es_AR").strip(),
    )

    try:
        record_audit(
            action="ai_agent_configuration_update",
            entity="ai_agent",
            entity_id=agents[VENDOR_AGENT_NAME].id,
            detail="Configuración de Vendedor 24 hs y Asistente empresarial actualizada",
            company_id=company_id,
        )
    except Exception:
        pass

    db.session.commit()
    flash("Configuración de agentes guardada correctamente.", "success")
    return redirect(url_for("ai_admin.index"))


@bp.post("/toggle")
@company_admin_required
def toggle():
    company_id = get_current_company_id(current_user)
    agents = ensure_default_agents(company_id)
    agent_name = request.form.get("agent")
    enabled = request.form.get("enabled") == "1"
    agent = agents.get(agent_name)
    if agent is None:
        flash("Agente inválido.", "warning")
        return redirect(url_for("ai_admin.index"))
    agent.active = enabled
    db.session.commit()
    flash(f"{agent.name}: {'activado' if enabled else 'desactivado'}.", "success")
    return redirect(url_for("ai_admin.index"))

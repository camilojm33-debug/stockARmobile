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
    ensure_agent_for_key,
    ensure_default_agents,
    get_ai_preferences,
    get_special_options,
    get_vendor_options,
    get_whatsapp_connection,
    update_ai_preferences,
)
from services.ai_agent.usage_service import AI_PLANS, can_use_ai, can_use_ai_feature, current_plan
from services.ai_agent.vendor_publication import publication_status

bp = Blueprint("ai_admin", __name__, url_prefix="/dashboard/ai-agent")


@bp.before_request
def _require_company_pin():
    from company_billing import _is_pin_verified
    company_id = get_current_company_id(current_user)
    if _is_pin_verified(company_id):
        return None
    flash("Validá el PIN de Mi Empresa para gestionar la configuración de IA.", "warning")
    return redirect(url_for("company_billing.company_settings"))


_DEFAULT_VENDOR_PROMPT = (
    "Sos el Vendedor 24 hs de StockARmobile. Consultá siempre los datos reales antes de informar precio o stock. "
    "Ayudá a elegir productos, armar pedidos y orientar al cliente hacia el pago. "
    "Nunca inventes promociones, descuentos, stock ni confirmaciones de pago."
)
_DEFAULT_BUSINESS_PROMPT = (
    "Sos el Asistente empresarial del comercio. Consultá las herramientas antes de informar cifras. "
    "Mostrá resultados claros y accionables. No inventes datos ni ejecutes operaciones financieras o de stock sin una acción explícita."
)


def _default_model() -> str:
    provider = (os.getenv("AI_PROVIDER") or "openai_compatible").strip().lower()
    if provider == "gemini":
        return (os.getenv("GEMINI_MODEL") or "gemini-3.6-flash").strip()
    if provider == "openai":
        return (os.getenv("OPENAI_MODEL") or "gpt-4.1-mini").strip()
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
    special_agents = {
        key: ensure_agent_for_key(company_id, key)
        for key in ("analista", "marketing")
    }
    configs = _ensure_configs(company_id, {**agents, **special_agents})
    prefs = get_ai_preferences(company)
    vendor_options = prefs["ai_agent"].get("vendor_options") if isinstance(prefs["ai_agent"].get("vendor_options"), dict) else {}
    special_options = {
        key: get_special_options(company, key)
        for key in ("analista", "marketing")
    }
    special_access = {
        key: can_use_ai_feature(company, key)
        for key in ("analista", "marketing")
    }
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
        conversation_rows.append(
            {
                "id": conversation.id,
                "agent_name": agent_ids.get(conversation.agent_id, "Agente"),
                "channel": conversation.channel,
                "external_id": conversation.external_conversation_id,
                "status": conversation.status,
                "updated_at": conversation.updated_at,
                "last_message": (latest.content[:180] if latest else ""),
            }
        )

    provider = (os.getenv("AI_PROVIDER") or "openai_compatible").strip().lower()
    if provider == "gemini":
        ai_key_configured = bool((os.getenv("GEMINI_API_KEY") or "").strip())
    else:
        ai_key_configured = bool((os.getenv("AI_PROVIDER_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()) or provider in {"lmstudio", "lm_studio"}
    ai_enabled = bool(prefs["ai_agent"].get("enabled", True))
    whatsapp_connected = bool(whatsapp.get("enabled") and whatsapp.get("phone_number_id"))
    agent_key_by_name = {
        VENDOR_AGENT_NAME: "vendedor",
        BUSINESS_AGENT_NAME: "asistente",
    }
    agent_states = {}
    for name, agent in agents.items():
        agent_key = agent_key_by_name.get(name)
        entitlement = can_use_ai_feature(company, agent_key) if agent_key else None
        if entitlement is not None and not entitlement.allowed:
            agent_states[name] = {"label": "No incluido en el plan", "tone": "secondary"}
        elif not ai_enabled or not agent.active:
            agent_states[name] = {"label": "Desactivado", "tone": "danger"}
        elif not ai_key_configured:
            agent_states[name] = {"label": "Configuración pendiente", "tone": "warning"}
        else:
            agent_states[name] = {"label": "Activo", "tone": "success"}
    if whatsapp_connected and ai_enabled and agents[VENDOR_AGENT_NAME].active and can_use_ai_feature(company, "vendedor").allowed:
        whatsapp_state = {"label": "Conectado", "tone": "success"}
    elif whatsapp.get("enabled"):
        whatsapp_state = {"label": "Configuración pendiente", "tone": "warning"}
    else:
        whatsapp_state = {"label": "No conectado", "tone": "danger"}
    return render_template(
        "ai_agent/admin_v2.html",
        company=company,
        agents=agents,
        configs=configs,
        prefs=prefs,
        whatsapp=whatsapp,
        ai_key_configured=ai_key_configured,
        ai_enabled=ai_enabled,
        agent_states=agent_states,
        whatsapp_state=whatsapp_state,
        webhook_url=url_for("whatsapp_agent.webhook", _external=True),
        conversations=conversation_rows,
        vendor_options=vendor_options,
        special_agents=special_agents,
        special_options=special_options,
        special_access=special_access,
        ai_plans=AI_PLANS,
    )


@bp.get("/vendor-metrics")
@company_admin_required
def vendor_metrics():
    company_id = get_current_company_id(current_user)
    from app import Company

    company = Company.query.filter_by(id=company_id, active=True).first()
    if company is None:
        return redirect(url_for("dashboard.index"))
    entitlement = can_use_ai_feature(company, "vendedor")
    if not entitlement.allowed:
        return redirect(url_for("ai_agents.agent", agent="planes"))

    from services.ai_agent.vendor_metrics_service import build_vendor_metrics

    metrics = build_vendor_metrics(company_id=company_id)
    return render_template(
        "ai_agent/vendor_metrics.html",
        company=company,
        metrics=metrics,
    )



@bp.get("/vendor-config")
@company_admin_required
def vendor_config():
    company_id = get_current_company_id(current_user)
    from app import Company

    company = Company.query.filter_by(id=company_id, active=True).first()
    if company is None:
        return redirect(url_for("dashboard.index"))

    entitlement = can_use_ai_feature(company, "vendedor")
    if not entitlement.allowed:
        return redirect(url_for("ai_agents.agent", agent="planes"))

    agents = ensure_default_agents(company_id)
    vendor = agents[VENDOR_AGENT_NAME]
    configs = _ensure_configs(company_id, {VENDOR_AGENT_NAME: vendor})
    return render_template(
        "ai_agent/vendor_config.html",
        company=company,
        vendor=vendor,
        vendor_config=configs[VENDOR_AGENT_NAME],
        vendor_options=get_vendor_options(company),
        publication=publication_status(company),
    )


@bp.post("/vendor-save")
@company_admin_required
def vendor_save():
    company_id = get_current_company_id(current_user)
    from app import Company, record_audit

    company = Company.query.filter_by(id=company_id, active=True).first()
    if company is None:
        flash("No se encontró la empresa activa.", "danger")
        return redirect(url_for("dashboard.index"))

    entitlement = can_use_ai_feature(company, "vendedor")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye el Vendedor IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))

    agents = ensure_default_agents(company_id)
    vendor = agents[VENDOR_AGENT_NAME]
    configs = _ensure_configs(company_id, {VENDOR_AGENT_NAME: vendor})
    plan = current_plan(company)
    entitled_agents = set(plan["agents"]) if plan else set()

    if "vendor_enabled" in request.form:
        vendor.active = request.form.get("vendor_enabled") == "1" and "vendedor" in entitled_agents

    config = configs[VENDOR_AGENT_NAME]
    prompt = request.form.get("vendor_prompt")
    if prompt is not None:
        config.system_prompt = prompt.strip()[:12000]
    language = request.form.get("vendor_language")
    if language is not None:
        config.language = language.strip()[:8] or "es-AR"

    vendor_options = get_vendor_options(company)
    if "vendor_personality" in request.form:
        vendor_options["personality"] = (request.form.get("vendor_personality") or "amigable").strip()[:30]

    for key, field in {
        "can_recommend": "vendor_can_recommend",
        "can_offer_alternatives": "vendor_can_offer_alternatives",
        "can_prepare_quotes": "vendor_can_prepare_quotes",
        "can_take_orders": "vendor_can_take_orders",
        "can_follow_up": "vendor_can_follow_up",
        "can_handoff": "vendor_can_handoff",
    }.items():
        if field in request.form:
            vendor_options[key] = "1" in request.form.getlist(field)

    for key, field, limit in (
        ("agent_name", "vendor_agent_name", 120),
        ("greeting", "vendor_greeting", 1000),
        ("schedule", "vendor_schedule", 120),
        ("out_of_hours_message", "vendor_out_of_hours_message", 1000),
        ("business_information", "vendor_business_information", 4000),
    ):
        if field in request.form:
            vendor_options[key] = (request.form.get(field) or "").strip()[:limit]

    update_ai_preferences(company, ai_updates={"vendor_options": vendor_options})

    try:
        record_audit(
            action="ai_vendor_configuration_update",
            entity="ai_agent",
            entity_id=vendor.id,
            detail="Configuración exclusiva del Vendedor IA actualizada",
            company_id=company_id,
        )
    except Exception:
        pass

    db.session.commit()
    flash("Configuración del Vendedor IA guardada correctamente.", "success")
    return redirect(url_for("ai_admin.vendor_config"))


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
    plan = current_plan(company)
    entitled_agents = set(plan["agents"]) if plan else set()
    if "vendor_enabled" in request.form:
        agents[VENDOR_AGENT_NAME].active = request.form.get("vendor_enabled") == "1" and "vendedor" in entitled_agents
    agents[BUSINESS_AGENT_NAME].active = request.form.get("business_enabled") == "1" and "asistente" in entitled_agents
    special_agents = {
        key: ensure_agent_for_key(company_id, key)
        for key in ("analista", "marketing")
    }
    for key, field in (("analista", "analista_enabled"), ("marketing", "marketing_enabled")):
        # Los agentes bloqueados por plan no envían controles disabled; conservar
        # su estado evita que guardar otro agente los desactive accidentalmente.
        if field in request.form:
            special_agents[key].active = request.form.get(field) == "1" and key in entitled_agents
        elif key not in entitled_agents:
            special_agents[key].active = False

    config_fields_present = any(field in request.form for field in ("vendor_prompt", "vendor_language"))
    for name, prefix in ((VENDOR_AGENT_NAME, "vendor"), (BUSINESS_AGENT_NAME, "business")):
        if name == VENDOR_AGENT_NAME and not config_fields_present:
            continue
        config = configs[name]
        model = (request.form.get(f"{prefix}_model") or "").strip()[:120]
        if model:
            config.model = model
        prompt = request.form.get(f"{prefix}_prompt")
        if prompt is not None:
            config.system_prompt = prompt.strip()[:12000]
        language = request.form.get(f"{prefix}_language")
        if language is not None:
            config.language = language.strip()[:8] or "es-AR"
        max_tokens_raw = request.form.get(f"{prefix}_max_tokens")
        if max_tokens_raw is not None:
            config.max_tokens = _safe_int(max_tokens_raw, config.max_tokens or 700, 128, 4000)
        temperature_raw = request.form.get(f"{prefix}_temperature")
        if temperature_raw is not None:
            config.temperature = _safe_decimal(temperature_raw, config.temperature or Decimal("0.20"))

    prefs_before = get_ai_preferences(company)
    old_vendor = prefs_before["ai_agent"].get("vendor_options") if isinstance(prefs_before["ai_agent"].get("vendor_options"), dict) else {}
    vendor_options = {
        "personality": old_vendor.get("personality", "amigable"),
        "can_recommend": bool(old_vendor.get("can_recommend", True)),
        "can_offer_alternatives": bool(old_vendor.get("can_offer_alternatives", True)),
        "can_prepare_quotes": bool(old_vendor.get("can_prepare_quotes", True)),
        "can_take_orders": bool(old_vendor.get("can_take_orders", True)),
        "can_follow_up": bool(old_vendor.get("can_follow_up", False)),
        "can_handoff": bool(old_vendor.get("can_handoff", False)),
        "agent_name": old_vendor.get("agent_name", "Vendedor IA"),
        "greeting": old_vendor.get("greeting", ""),
        "schedule": old_vendor.get("schedule", ""),
        "out_of_hours_message": old_vendor.get("out_of_hours_message", ""),
        "business_information": old_vendor.get("business_information", ""),
    }
    if "vendor_personality" in request.form:
        vendor_options["personality"] = (request.form.get("vendor_personality") or "amigable").strip()[:30]
    for key, field in {
        "can_recommend": "vendor_can_recommend",
        "can_offer_alternatives": "vendor_can_offer_alternatives",
        "can_prepare_quotes": "vendor_can_prepare_quotes",
        "can_take_orders": "vendor_can_take_orders",
        "can_follow_up": "vendor_can_follow_up",
        "can_handoff": "vendor_can_handoff",
    }.items():
        if field in request.form:
            vendor_options[key] = "1" in request.form.getlist(field)
    for key, field, limit in (
        ("agent_name", "vendor_agent_name", 120),
        ("greeting", "vendor_greeting", 1000),
        ("schedule", "vendor_schedule", 120),
        ("out_of_hours_message", "vendor_out_of_hours_message", 1000),
        ("business_information", "vendor_business_information", 4000),
    ):
        if field in request.form:
            vendor_options[key] = (request.form.get(field) or "").strip()[:limit]

    special_options = {}
    for key in ("analista", "marketing"):
        current = get_special_options(company, key)
        if key == "analista":
            alerts = [value for value in (
                "sales_drop" if request.form.get("analista_alert_sales_drop") == "1" else "",
                "critical_stock" if request.form.get("analista_alert_critical_stock") == "1" else "",
                "inactive_clients" if request.form.get("analista_alert_inactive_clients") == "1" else "",
                "low_rotation" if request.form.get("analista_alert_low_rotation") == "1" else "",
            ) if value]
            special_options[key] = {
                "default_period": request.form.get("analista_default_period") or current["default_period"],
                "alerts": alerts or current["alerts"],
                "output_style": request.form.get("analista_output_style") or current["output_style"],
            }
        else:
            campaign_types = [value for value in (
                "promocion" if request.form.get("marketing_campaign_promocion") == "1" else "",
                "reactivacion" if request.form.get("marketing_campaign_reactivacion") == "1" else "",
                "novedad" if request.form.get("marketing_campaign_novedad") == "1" else "",
                "stock" if request.form.get("marketing_campaign_stock") == "1" else "",
                "fidelizacion" if request.form.get("marketing_campaign_fidelizacion") == "1" else "",
            ) if value]
            special_options[key] = {
                "default_segment": request.form.get("marketing_default_segment") or current["default_segment"],
                "campaign_tone": request.form.get("marketing_campaign_tone") or current["campaign_tone"],
                "campaign_types": campaign_types or current["campaign_types"],
                "approval_required": True,
            }

    ai_updates = {
        "enabled": request.form.get("ai_enabled") == "1",
        "vendor_options": vendor_options,
        "special_options": special_options,
    }
    if "whatsapp_enabled" in request.form:
        ai_updates["whatsapp_enabled"] = request.form.get("whatsapp_enabled") == "1"
    update_ai_preferences(company, ai_updates=ai_updates)

    existing_whatsapp = get_whatsapp_connection(company)
    configure_whatsapp_connection(
        company,
        phone_number_id=(request.form.get("phone_number_id") or existing_whatsapp.get("phone_number_id") or "").strip(),
        access_token=(request.form.get("access_token") or "").strip() or None,
        business_account_id=(request.form.get("business_account_id") or existing_whatsapp.get("business_account_id") or "").strip(),
        display_phone_number=(request.form.get("display_phone_number") or existing_whatsapp.get("display_phone_number") or "").strip(),
        enabled=(request.form.get("whatsapp_enabled") == "1") if "whatsapp_enabled" in request.form else bool(existing_whatsapp.get("enabled")),
        template_name=(request.form.get("template_name") or existing_whatsapp.get("template_name") or "").strip(),
        template_language=(request.form.get("template_language") or existing_whatsapp.get("template_language") or "es_AR").strip(),
    )

    try:
        record_audit(
            action="ai_agent_configuration_update",
            entity="ai_agent",
            entity_id=agents[VENDOR_AGENT_NAME].id,
            detail="Configuración de Vendedor 24 hs, Asistente empresarial, Analista IA y Marketing IA actualizada",
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
    key_by_name = {
        VENDOR_AGENT_NAME: "vendedor",
        BUSINESS_AGENT_NAME: "asistente",
        "Analista IA": "analista",
        "Marketing IA": "marketing",
    }
    agent_key = key_by_name.get(agent.name)
    entitlement = can_use_ai_feature(company, agent_key) if agent_key else None
    if enabled and (entitlement is None or not entitlement.allowed):
        flash(entitlement.reason if entitlement else "Agente inválido.", "warning")
        return redirect(url_for("ai_admin.index"))
    agent.active = enabled
    db.session.commit()
    flash(f"{agent.name}: {'activado' if enabled else 'desactivado'}.", "success")
    return redirect(url_for("ai_admin.index"))

"""Tenant-facing dashboard for the existing StockARmobile AI capabilities."""

from __future__ import annotations

import os
import secrets

import requests
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app import tenant_required
from services.ai_agent.config_service import (
    BUSINESS_AGENT_NAME,
    VENDOR_AGENT_NAME,
    configure_whatsapp_connection,
    encrypt_secret,
    ensure_default_agents,
    get_ai_preferences,
    get_whatsapp_connection,
    update_ai_preferences,
)
from services.ai_agent.usage_service import AI_PLANS, AGENT_LABELS, can_use_ai, current_plan, usage_snapshot
from stockarmobile.extensions import db
from stockarmobile.models.conversations import Conversation
from stockarmobile.decorators import company_admin_required
from stockarmobile.permissions import can_access_ai
from services.ai_agent.campaign_service import CampaignService


bp = Blueprint("ai_agents", __name__, url_prefix="/agentes-ia")


@bp.before_request
def _require_ai_access():
    if not can_access_ai(current_user):
        flash("Tu usuario no tiene habilitado el acceso a Agentes IA. Pedile al administrador de la empresa que lo active desde Mi Empresa.", "warning")
        return redirect(url_for("dashboard.index"))


def _context():
    from app import Client, Company, Product, Quote, Sale, SaleItem
    from services.ai_agent.subscription_service import AISubscriptionService

    company_id = current_user.company_id
    company = Company.query.get(company_id)
    agents = ensure_default_agents(company_id)
    preferences = get_ai_preferences(company)
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_start = (month_start - timedelta(days=1)).replace(day=1)

    conversations = Conversation.query.filter(Conversation.company_id == company_id, Conversation.created_at >= month_start).count()
    clients_attended = db.session.query(Conversation.external_conversation_id).filter(Conversation.company_id == company_id, Conversation.created_at >= month_start, Conversation.external_conversation_id.isnot(None)).distinct().count()
    quotes = Quote.query.filter(Quote.company_id == company_id, Quote.created_at >= month_start).count()
    sales_month = Sale.query.filter(Sale.company_id == company_id, Sale.created_at >= month_start, Sale.status.notin_(["cancelada", "anulada"]))
    sales_previous = Sale.query.filter(Sale.company_id == company_id, Sale.created_at >= previous_start, Sale.created_at < month_start, Sale.status.notin_(["cancelada", "anulada"]))
    sales_total = float(sales_month.with_entities(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).scalar() or 0)
    previous_total = float(sales_previous.with_entities(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).scalar() or 0)
    sales_change = None if previous_total == 0 else round(((sales_total - previous_total) / previous_total) * 100, 1)
    critical_stock = Product.query.filter(Product.company_id == company_id, Product.active.is_(True), Product.stock <= Product.min_stock).count()
    sold_product_ids = db.session.query(SaleItem.product_id).join(Sale).filter(Sale.company_id == company_id, Sale.created_at >= now - timedelta(days=90), Sale.status.notin_(["cancelada", "anulada"])).distinct()
    low_rotation = Product.query.filter(Product.company_id == company_id, Product.active.is_(True), ~Product.id.in_(sold_product_ids)).count()
    ai_status = AISubscriptionService.get_status(company)
    ai_plan = current_plan(company)
    ai_usage = usage_snapshot(company_id)
    agent_access = {key: can_use_ai(company, key) for key in AGENT_LABELS}
    invoice_access = can_use_ai(company, "facturas")

    any_chat_agent = any(access.allowed for access in agent_access.values())
    default_chat_agent = "asistente"
    if not agent_access[default_chat_agent].allowed:
        for candidate in ("vendedor", "analista", "marketing"):
            if agent_access[candidate].allowed:
                default_chat_agent = candidate
                break

    return {"company": company, "agents": agents, "preferences": preferences, "metrics": {"conversations": conversations, "clients_attended": clients_attended, "quotes": quotes, "sales": int(sales_month.count())}, "analyst": {"sales_change": sales_change, "critical_stock": critical_stock, "low_rotation": low_rotation, "opportunities": None}, "ai_status": ai_status, "ai_plans": AI_PLANS, "agent_labels": AGENT_LABELS, "ai_plan": ai_plan, "ai_usage": ai_usage, "agent_access": agent_access, "invoice_access": invoice_access, "any_chat_agent": any_chat_agent, "default_chat_agent": default_chat_agent, "plan_url": url_for("ai_agents.agent", agent="planes"), "ai_checkout_url": url_for("company_billing.create_ai_subscription_checkout"), "config_url": url_for("ai_admin.index") if current_user.role == "admin" else None, "chat_url": url_for("dashboard.ai_agent_chat")}


def _campaign_rows(company_id: int):
    from app import Campaign
    return Campaign.query.filter_by(company_id=company_id).order_by(Campaign.updated_at.desc(), Campaign.id.desc()).all()


def _campaign_summary(company_id: int):
    rows = _campaign_rows(company_id)
    return {status: sum(1 for row in rows if row.status == status) for status in ("BORRADOR", "PENDIENTE_APROBACION", "APROBADA")}


def _meta_graph_version() -> str:
    return (os.getenv("META_GRAPH_API_VERSION") or "v26.0").strip()


def _meta_graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{_meta_graph_version()}/{str(path).lstrip('/')}"


def _meta_error(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("error_user_msg") or f"HTTP {response.status_code}")
    return str(payload.get("message") or f"HTTP {response.status_code}") if isinstance(payload, dict) else f"HTTP {response.status_code}"


def _embedded_signup_config() -> tuple[str, str]:
    app_id = (os.getenv("META_APP_ID") or "").strip()
    config_id = (os.getenv("META_EMBEDDED_SIGNUP_CONFIG_ID") or "").strip()
    return app_id, config_id


@bp.get("/vendedor/conectar-whatsapp")
@tenant_required
def vendor_whatsapp_connect():
    company = current_user.company
    whatsapp = get_whatsapp_connection(company)
    app_id, config_id = _embedded_signup_config()
    return render_template(
        "ai_agents/whatsapp_onboarding.html",
        app_id=app_id,
        config_id=config_id,
        whatsapp=whatsapp,
        complete_url=url_for("ai_agents.vendor_whatsapp_complete"),
        vendor_url=url_for("ai_agents.agent", agent="vendedor"),
    )


@bp.post("/vendedor/conectar-whatsapp/complete")
@tenant_required
def vendor_whatsapp_complete():
    payload = request.get_json(silent=True) or request.form.to_dict()
    code = str(payload.get("code") or "").strip()
    waba_id = str(payload.get("waba_id") or "").strip()
    phone_number_id = str(payload.get("phone_number_id") or "").strip()
    business_id = str(payload.get("business_id") or payload.get("businessId") or "").strip()
    if not code or not waba_id or not phone_number_id:
        return jsonify({"success": False, "error": "Meta no devolvió todos los datos necesarios para conectar el número."}), 400

    app_id, config_id = _embedded_signup_config()
    app_secret = (os.getenv("META_APP_SECRET") or "").strip()
    if not app_id or not app_secret or not config_id:
        current_app.logger.error("WhatsApp Embedded Signup is not configured: missing META_APP_ID, META_APP_SECRET or META_EMBEDDED_SIGNUP_CONFIG_ID")
        return jsonify({"success": False, "error": "La conexión de WhatsApp todavía no está habilitada en StockArMobile."}), 503

    try:
        exchange = requests.get(
            _meta_graph_url("oauth/access_token"),
            params={"client_id": app_id, "client_secret": app_secret, "code": code},
            timeout=20,
        )
        if not exchange.ok:
            current_app.logger.warning("WhatsApp Embedded Signup token exchange failed: %s", _meta_error(exchange))
            return jsonify({"success": False, "error": "Meta no pudo autorizar la conexión. Volvé a intentarlo."}), 400
        access_token = str((exchange.json() or {}).get("access_token") or "").strip()
        if not access_token:
            return jsonify({"success": False, "error": "Meta no devolvió un token válido para la conexión."}), 400

        phone_response = requests.get(
            _meta_graph_url(phone_number_id),
            params={
                "fields": "id,display_phone_number,verified_name,status,platform_type,code_verification_status,name_status",
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if not phone_response.ok:
            current_app.logger.warning("WhatsApp Embedded Signup phone lookup failed: %s", _meta_error(phone_response))
            return jsonify({"success": False, "error": "No pudimos validar el número de WhatsApp con Meta."}), 400
        phone = phone_response.json() or {}
        if str(phone.get("id") or "") != phone_number_id:
            return jsonify({"success": False, "error": "El número devuelto por Meta no coincide con la sesión de conexión."}), 400

        waba_response = requests.get(
            _meta_graph_url(f"{waba_id}/phone_numbers"),
            params={"fields": "id,display_phone_number,verified_name,status,platform_type,code_verification_status,name_status"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if not waba_response.ok:
            current_app.logger.warning("WhatsApp Embedded Signup WABA validation failed: %s", _meta_error(waba_response))
            return jsonify({"success": False, "error": "Meta no permitió validar la cuenta de WhatsApp seleccionada."}), 400
        waba_phones = (waba_response.json() or {}).get("data") or []
        if not any(str(item.get("id") or "") == phone_number_id for item in waba_phones if isinstance(item, dict)):
            return jsonify({"success": False, "error": "El número no pertenece a la cuenta de WhatsApp seleccionada."}), 400

        subscribe = requests.post(
            _meta_graph_url(f"{waba_id}/subscribed_apps"),
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if not subscribe.ok:
            current_app.logger.warning("WhatsApp Embedded Signup WABA subscription failed: %s", _meta_error(subscribe))
            return jsonify({"success": False, "error": "Meta autorizó el número, pero no pudimos activar los mensajes automáticos todavía."}), 400

        status = str(phone.get("status") or "").upper()
        platform_type = str(phone.get("platform_type") or "").upper()
        registration_pin = ""
        if status not in {"CONNECTED"} and platform_type not in {"ON_PREMISE"}:
            registration_pin = str(secrets.randbelow(900000) + 100000)
            register = requests.post(
                _meta_graph_url(f"{phone_number_id}/register"),
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={"messaging_product": "whatsapp", "pin": registration_pin},
                timeout=20,
            )
            if not register.ok:
                current_app.logger.warning("WhatsApp Embedded Signup phone registration failed: %s", _meta_error(register))
                return jsonify({"success": False, "error": "El número fue autorizado, pero Meta no terminó de registrarlo para Cloud API. Revisá el estado del número en WhatsApp y volvé a conectar."}), 400

        company = current_user.company
        configure_whatsapp_connection(
            company,
            phone_number_id=phone_number_id,
            access_token=access_token,
            business_account_id=waba_id,
            display_phone_number=str(phone.get("display_phone_number") or "").strip(),
            enabled=True,
            template_name="",
            template_language="es_AR",
        )
        whatsapp_updates = {"waba_id": waba_id}
        if business_id:
            whatsapp_updates["business_id"] = business_id
        if registration_pin:
            whatsapp_updates["registration_pin_encrypted"] = encrypt_secret(registration_pin)
        update_ai_preferences(company, whatsapp_updates=whatsapp_updates)
        db.session.commit()
        current_app.logger.info("WhatsApp Embedded Signup connected company_id=%s waba_id=%s phone_number_id=%s", current_user.company_id, waba_id, phone_number_id)
        return jsonify({
            "success": True,
            "phone_number": str(phone.get("display_phone_number") or "").strip(),
            "redirect_url": url_for("ai_agents.agent", agent="vendedor", whatsapp="connected"),
        })
    except requests.RequestException:
        current_app.logger.exception("WhatsApp Embedded Signup network failure")
        return jsonify({"success": False, "error": "No pudimos comunicarnos con Meta. Intentá nuevamente en unos segundos."}), 502
    except Exception:
        db.session.rollback()
        current_app.logger.exception("WhatsApp Embedded Signup unexpected failure")
        return jsonify({"success": False, "error": "No pudimos completar la conexión de WhatsApp. Intentá nuevamente."}), 500


@bp.get("/")
@tenant_required
def index():
    return render_template("ai_agents/index.html", view="dashboard", **_context())


@bp.get("/campanas")
@tenant_required
def campaigns():
    return render_template("ai_agents/campaigns.html", campaigns=_campaign_rows(current_user.company_id), campaign_summary=_campaign_summary(current_user.company_id), **_context())


@bp.get("/campanas/<int:campaign_id>")
@tenant_required
def campaign_detail(campaign_id):
    campaign = CampaignService._campaign(current_user.company_id, campaign_id)
    if campaign is None:
        from flask import abort
        abort(404)
    return render_template("ai_agents/campaign_detail.html", campaign=campaign, **_context())


@bp.post("/campanas/<int:campaign_id>/edit")
@tenant_required
def campaign_edit(campaign_id):
    try:
        CampaignService.update_draft(company_id=current_user.company_id, campaign_id=campaign_id, title=request.form.get("title", ""), objective=request.form.get("objective", ""), content=request.form.get("content", ""), user_id=current_user.id)
        db.session.commit()
        flash("Campaña actualizada.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    return redirect(url_for("ai_agents.campaign_detail", campaign_id=campaign_id))


@bp.post("/campanas/<int:campaign_id>/transition")
@company_admin_required
def campaign_transition(campaign_id):
    target_status = request.form.get("status", "")
    try:
        CampaignService.transition(company_id=current_user.company_id, campaign_id=campaign_id, target_status=target_status, user_id=current_user.id)
        db.session.commit()
        flash("Estado de campaña actualizado.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    return redirect(url_for("ai_agents.campaign_detail", campaign_id=campaign_id))


@bp.get("/<agent>")
@tenant_required
def agent(agent):
    if agent not in {"vendedor", "asistente", "analista", "marketing", "planes"}:
        abort(404)
    if agent != "planes":
        access = _context()["agent_access"][agent]
        if not access.allowed:
            return render_template("ai_agents/index.html", view="locked", locked_agent=agent, locked_reason=access.reason, **_context())
    if agent == "vendedor":
        whatsapp = get_whatsapp_connection(current_user.company)
        if not (whatsapp.get("enabled") and whatsapp.get("phone_number_id")):
            return redirect(url_for("ai_agents.vendor_whatsapp_connect"))
    return render_template("ai_agents/index.html", view=agent, **_context())

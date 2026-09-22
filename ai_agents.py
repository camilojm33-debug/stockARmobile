"""Tenant-facing dashboard for the existing StockARmobile AI capabilities."""

from __future__ import annotations

import os
import secrets
import time
import uuid

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import requests
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
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
from services.ai_agent.usage_service import AI_PLANS, AGENT_LABELS, can_use_ai, can_use_ai_feature, current_plan, usage_snapshot
from stockarmobile.extensions import csrf, db
from stockarmobile.models.conversations import Conversation
from stockarmobile.decorators import company_admin_required
from stockarmobile.permissions import can_access_ai
from services.ai_agent.campaign_service import CampaignService


bp = Blueprint("ai_agents", __name__, url_prefix="/agentes-ia")

PUBLIC_VENDOR_CHAT_SALT = "stockarmobile-public-vendor-v2"
PUBLIC_VENDOR_CHAT_MAX_AGE = 60 * 60 * 24 * 365
PUBLIC_VENDOR_CHAT_LIMIT = 12
PUBLIC_VENDOR_CHAT_WINDOW = 60


def _current_active_company():
    from app import Company

    company = Company.query.filter_by(
        id=getattr(current_user, "company_id", None),
        active=True,
    ).first()
    if company is None:
        abort(403)
    return company


def _public_vendor_serializer():
    return URLSafeTimedSerializer(current_app.config.get("SECRET_KEY", "stockarmobile-dev-secret"))


def _public_vendor_token(company_id: int) -> str:
    return _public_vendor_serializer().dumps({"company_id": int(company_id), "agent": "vendedor"}, salt=PUBLIC_VENDOR_CHAT_SALT)


def _decode_public_vendor_token(token: str):
    try:
        payload = _public_vendor_serializer().loads(token, salt=PUBLIC_VENDOR_CHAT_SALT, max_age=PUBLIC_VENDOR_CHAT_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("agent") != "vendedor":
        return None
    try:
        return int(payload.get("company_id"))
    except (TypeError, ValueError):
        return None


def _public_vendor_rate_limit(company_id: int) -> bool:
    remote = (request.remote_addr or "unknown").strip()
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if redis_url:
        try:
            import redis
            client = redis.Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)
            key = f"stockarmobile:public-vendor:{int(company_id)}:{remote}"
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, PUBLIC_VENDOR_CHAT_WINDOW)
            return count <= PUBLIC_VENDOR_CHAT_LIMIT
        except Exception:
            current_app.logger.warning("Public vendor Redis rate limit unavailable; using session fallback.")
    now = int(time.time())
    state = session.get("public_vendor_rate") or {}
    if not isinstance(state, dict) or now - int(state.get("started_at", 0) or 0) >= PUBLIC_VENDOR_CHAT_WINDOW:
        session["public_vendor_rate"] = {"started_at": now, "count": 1}
        return True
    count = int(state.get("count", 0) or 0)
    if count >= PUBLIC_VENDOR_CHAT_LIMIT:
        return False
    state["count"] = count + 1
    session["public_vendor_rate"] = state
    session.modified = True
    return True


@bp.before_request
def _require_ai_access():
    if request.endpoint in {"ai_agents.public_vendor_chat", "ai_agents.public_vendor_chat_message"}:
        return None
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
    invoice_access = can_use_ai_feature(company, "facturas")

    any_chat_agent = any(access.allowed for access in agent_access.values())
    default_chat_agent = "asistente"
    if not agent_access[default_chat_agent].allowed:
        for candidate in ("vendedor", "analista", "marketing"):
            if agent_access[candidate].allowed:
                default_chat_agent = candidate
                break

    public_webchat_enabled = bool(preferences.get("ai_agent", {}).get("public_webchat_enabled", False))
    public_vendor_url = url_for("ai_agents.public_vendor_chat", token=_public_vendor_token(company_id)) if agent_access["vendedor"].allowed else None
    if agent_access["vendedor"].allowed:
        try:
            from services.ai_agent.vendor_publication import publication_status
            publication = publication_status(company)
            if publication.get("available") and publication.get("url"):
                public_vendor_url = publication["url"]
        except Exception:
            current_app.logger.exception("No se pudo resolver la URL estable del Vendedor público company_id=%s", company_id)
    return {"company": company, "agents": agents, "preferences": preferences, "metrics": {"conversations": conversations, "clients_attended": clients_attended, "quotes": quotes, "sales": int(sales_month.count())}, "analyst": {"sales_change": sales_change, "critical_stock": critical_stock, "low_rotation": low_rotation, "opportunities": None}, "ai_status": ai_status, "ai_plans": AI_PLANS, "agent_labels": AGENT_LABELS, "ai_plan": ai_plan, "ai_usage": ai_usage, "agent_access": agent_access, "invoice_access": invoice_access, "any_chat_agent": any_chat_agent, "default_chat_agent": default_chat_agent, "public_webchat_enabled": public_webchat_enabled, "public_vendor_url": public_vendor_url, "plan_url": url_for("ai_agents.agent", agent="planes"), "ai_checkout_url": url_for("company_billing.create_ai_subscription_checkout"), "config_url": url_for("ai_admin.index"), "chat_url": url_for("dashboard.ai_agent_chat")}


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


def _current_active_company():
    from app import Company

    company = Company.query.filter_by(id=current_user.company_id, active=True).first()
    if company is None:
        abort(403)
    return company

@bp.get("/vendedor/conectar-whatsapp")
@tenant_required
def vendor_whatsapp_connect():
    company = _current_active_company()
    entitlement = can_use_ai_feature(company, "vendedor")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye el Vendedor IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))
    if not current_app.config.get("WHATSAPP_VENDOR_UI_ENABLED", False):
        return redirect(url_for("ai_agents.agent", agent="vendedor"))
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
    if not current_app.config.get("WHATSAPP_VENDOR_UI_ENABLED", False):
        return jsonify({"success": False, "error": "La conexión de WhatsApp todavía no está habilitada en StockARmobile."}), 503
    company = _current_active_company()
    entitlement = can_use_ai_feature(company, "vendedor")
    if not entitlement.allowed:
        return jsonify({"success": False, "error": entitlement.reason}), 403
    payload = request.get_json(silent=True) or request.form.to_dict()
    code = str(payload.get("code") or "").strip()
    waba_id = str(payload.get("waba_id") or "").strip()
    phone_number_id = str(payload.get("phone_number_id") or "").strip()
    business_id = str(payload.get("business_id") or payload.get("businessId") or "").strip()
    if not code or not waba_id or not phone_number_id:
        return jsonify({"success": False, "error": "Meta no devolvió todos los datos necesarios para conectar el número."}), 400

    app_id, config_id = _embedded_signup_config()
    app_secret = (os.getenv("META_APP_SECRET") or os.getenv("WHATSAPP_APP_SECRET") or "").strip()
    if not app_id or not app_secret or not config_id:
        current_app.logger.error("WhatsApp Embedded Signup is not configured: missing META_APP_ID, META_APP_SECRET/WHATSAPP_APP_SECRET or META_EMBEDDED_SIGNUP_CONFIG_ID")
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
            params={"fields": "id,display_phone_number,verified_name,status,platform_type,code_verification_status,name_status"},
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
        return jsonify({"success": True, "phone_number": str(phone.get("display_phone_number") or "").strip(), "redirect_url": url_for("ai_agents.agent", agent="vendedor", whatsapp="connected")})
    except requests.RequestException:
        current_app.logger.exception("WhatsApp Embedded Signup network failure")
        return jsonify({"success": False, "error": "No pudimos comunicarnos con Meta. Intentá nuevamente en unos segundos."}), 502
    except Exception:
        db.session.rollback()
        current_app.logger.exception("WhatsApp Embedded Signup unexpected failure")
        return jsonify({"success": False, "error": "No pudimos completar la conexión de WhatsApp. Intentá nuevamente."}), 500


@bp.get("/public/vendedor/<token>")
def public_vendor_chat(token):
    company_id = _decode_public_vendor_token(token)
    if company_id is None:
        abort(404)
    from app import Company
    company = Company.query.filter_by(id=company_id, active=True).first()
    if company is None:
        abort(404)
    preferences = get_ai_preferences(company)
    if not bool(preferences.get("ai_agent", {}).get("public_webchat_enabled", False)):
        abort(404)
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return render_template(
            "ai_agents/public_vendor_chat.html",
            company=company,
            disabled_reason=access.reason,
            chat_url=None,
            shipping_config={"mode": "manual", "standard_cost": 0, "legacy_percent": 15},
            catalog=[],
            initial_state={"conversation_id": None, "cart": {"items": [], "total": 0, "currency": "ARS", "line_count": 0}, "payment_url": None},
            greeting="",
        )

    # Tokens issued by the legacy public endpoint remain valid, but published links
    # are redirected to the stable publication slug so new clients use the canonical flow.
    from services.ai_agent.vendor_publication import (
        _catalog_for_company,
        _initial_page_state,
        get_vendor_options,
        publication_status,
    )
    publication = publication_status(company)
    if publication.get("available") and publication.get("url"):
        return redirect(publication["url"], code=302)

    visitor_session_key = f"public_vendor_visitor_{company_id}"
    session.setdefault(visitor_session_key, uuid.uuid4().hex)
    options = get_vendor_options(company)
    return render_template(
        "ai_agents/public_vendor_chat.html",
        company=company,
        disabled_reason=None,
        chat_url=url_for("ai_agents.public_vendor_chat_message", token=token),
        shipping_config={
            "mode": get_vendor_options(company).get("shipping_mode", "legacy_percent"),
            "standard_cost": float(get_vendor_options(company).get("standard_shipping_cost") or 0),
            "legacy_percent": 15,
        },
        catalog=_catalog_for_company(company),
        initial_state=_initial_page_state(company),
        greeting=str(options.get("greeting") or "Hola 👋 ¿Qué producto estás buscando?").strip(),
    )


@bp.post("/public/vendedor/<token>/message")
def public_vendor_chat_message(token):
    company_id = _decode_public_vendor_token(token)
    if company_id is None:
        return jsonify({"success": False, "error": "Enlace de vendedor inválido o vencido."}), 404
    from app import Company
    company = Company.query.filter_by(id=company_id, active=True).first()
    if company is None:
        return jsonify({"success": False, "error": "El comercio no está disponible."}), 404
    preferences = get_ai_preferences(company)
    if not bool(preferences.get("ai_agent", {}).get("public_webchat_enabled", False)):
        return jsonify({"success": False, "error": "El vendedor web no está habilitado."}), 404
    access = can_use_ai(company, "vendedor")
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403
    if not _public_vendor_rate_limit(company_id):
        return jsonify({"success": False, "error": "Hay muchas consultas en este momento. Esperá unos segundos e intentá nuevamente."}), 429, {"Retry-After": str(PUBLIC_VENDOR_CHAT_WINDOW)}
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "Escribí una consulta."}), 400
    if len(message) > 500:
        return jsonify({"success": False, "error": "La consulta es demasiado larga."}), 400
    visitor_session_key = f"public_vendor_visitor_{company_id}"
    visitor_id = str(session.get(visitor_session_key) or uuid.uuid4().hex)
    session[visitor_session_key] = visitor_id
    from services.ai_agent.config_service import VENDOR_AGENT_NAME, ensure_default_agents
    from services.ai_agent.orchestrator_v2 import AgentRuntime
    vendor_agent = ensure_default_agents(company_id)[VENDOR_AGENT_NAME]
    conversation_id = payload.get("conversation_id")
    if conversation_id not in (None, ""):
        try:
            conversation = Conversation.query.filter_by(
                id=int(conversation_id),
                company_id=company_id,
                channel="webchat",
                external_conversation_id=visitor_id,
            ).first()
        except (TypeError, ValueError):
            conversation = None
        if conversation is None:
            return jsonify({"success": False, "error": "La conversación ya no es válida."}), 403
        if conversation.agent_id != vendor_agent.id:
            return jsonify({"success": False, "error": "La conversación no corresponde al vendedor."}), 409
    else:
        conversation = Conversation.query.filter_by(
            company_id=company_id,
            channel="webchat",
            external_conversation_id=visitor_id,
            agent_id=vendor_agent.id,
        ).filter(Conversation.status == "open").order_by(Conversation.id.desc()).first()
        if conversation is None:
            conversation = Conversation(
                company_id=company_id,
                agent_id=vendor_agent.id,
                channel="webchat",
                external_conversation_id=visitor_id,
                status="open",
                metadata_json={"source": "public_webchat"},
            )
            db.session.add(conversation)
            db.session.flush()
    try:
        result = AgentRuntime.process(
            company_id=company_id,
            conversation_id=conversation.id,
            message=message,
            channel="webchat",
            sender_id=None,
            idempotency_key=str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or uuid.uuid4().hex),
            metadata={"from": visitor_id, "source": "public_webchat"},
            include_system_prompt=True,
        )
        return jsonify({"success": True, "conversation_id": result.get("conversation_id"), "message_id": result.get("message_id"), "assistant_message_id": result.get("assistant_message_id"), "content": result.get("content")})
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error en vendedor web público: company_id=%s conversation_id=%s", company_id, conversation.id)
        return jsonify({"success": False, "error": "No se pudo procesar la consulta. Intentá nuevamente."}), 500


# The legacy public Vendedor endpoint is anonymous; signed token + tenant lookup + rate limit protect it.\ncsrf.exempt(public_vendor_chat_message)\n\n@bp.get("/")
@tenant_required
def index():
    return render_template("ai_agents/index.html", view="dashboard", **_context())


@bp.get("/campanas")
@tenant_required
def campaigns():
    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        return render_template(
            "ai_agents/index.html",
            view="locked",
            locked_agent="marketing",
            locked_reason=entitlement.reason,
            **_context(),
        )
    return render_template("ai_agents/campaigns.html", campaigns=_campaign_rows(current_user.company_id), campaign_summary=_campaign_summary(current_user.company_id), **_context())


@bp.get("/campanas/<int:campaign_id>")
@tenant_required
def campaign_detail(campaign_id):
    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        abort(403)
    campaign = CampaignService._campaign(current_user.company_id, campaign_id)
    if campaign is None:
        from flask import abort
        abort(404)
    return render_template("ai_agents/campaign_detail.html", campaign=campaign, **_context())


@bp.post("/vendedor/webchat/toggle")
@company_admin_required
def vendor_webchat_toggle():
    company = _current_active_company()
    access = can_use_ai_feature(company, "vendedor")
    if not access.allowed:
        flash(access.reason or "Tu plan no incluye el Vendedor IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))
    payload = request.get_json(silent=True) or request.form
    enabled = str(payload.get("enabled") or "").strip().lower() in {"1", "true", "yes", "on", "si", "sí"}
    update_ai_preferences(company, ai_updates={"public_webchat_enabled": enabled})
    db.session.commit()
    flash("Webchat público del Vendedor IA " + ("activado." if enabled else "desactivado."), "success")
    return redirect(url_for("ai_agents.agent", agent="vendedor"))


@bp.post("/campanas/<int:campaign_id>/edit")
@company_admin_required
def campaign_edit(campaign_id):
    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye Marketing IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))
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
    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye Marketing IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))
    target_status = request.form.get("status", "")
    try:
        CampaignService.transition(company_id=current_user.company_id, campaign_id=campaign_id, target_status=target_status, user_id=current_user.id)
        db.session.commit()
        flash("Estado de campaña actualizado.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    return redirect(url_for("ai_agents.campaign_detail", campaign_id=campaign_id))


@bp.get("")
@tenant_required
def index():
    """Central Agentes IA hub; keep a stable endpoint for the main navigation."""
    return render_template("ai_agents/index.html", view="dashboard", **_context())


@bp.get("/<agent>")
@tenant_required
def agent(agent):
    if agent not in {"vendedor", "asistente", "analista", "marketing", "planes"}:
        abort(404)
    if agent != "planes":
        access = _context()["agent_access"][agent]
        if not access.allowed:
            return render_template("ai_agents/index.html", view="locked", locked_agent=agent, locked_reason=access.reason, **_context())
    return render_template("ai_agents/index.html", view=agent, **_context())

"""Tenant-facing dashboard for the existing StockARmobile AI capabilities."""

from __future__ import annotations

import os
import json
import math
import re
import secrets
import time
import uuid
import hashlib
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import requests
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user
from sqlalchemy import or_

from app import tenant_required
from services.ai_agent.config_service import (
    BUSINESS_AGENT_NAME,
    VENDOR_AGENT_NAME,
    configure_whatsapp_connection,
    company_for_whatsapp_phone_id,
    company_branch_for_whatsapp_phone_id,
    encrypt_secret,
    ensure_default_agents,
    get_ai_preferences,
    get_whatsapp_connection,
    update_ai_preferences,
)
from services.ai_agent.usage_service import AI_PLANS, AGENT_LABELS, can_use_ai, can_use_ai_feature, current_plan, usage_snapshot
from stockarmobile.extensions import csrf, db
from stockarmobile.models.conversations import Conversation, ConversationMessage, PublicVendorOperation
from services.ai_agent.public_idempotency import (
    PublicOperationConflict,
    cached_result,
    complete_operation,
    get_or_create_operation,
    public_operation_lock,
)
from stockarmobile.decorators import company_admin_required
from stockarmobile.permissions import can_access_ai
from services.ai_agent.campaign_service import CampaignService
from services.ai_agent.tenant_campaign_service import campaign_metrics, prepare_recipients


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


def _public_vendor_rate_limit(company_id: int, *, conversation_id=None, request_key=None) -> bool:
    remote = (request.remote_addr or "unknown").strip()
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    operation_scope = ""
    if request_key:
        digest = hashlib.sha256(str(request_key).encode()).hexdigest()
        operation_scope = f":{int(conversation_id or 0)}:{digest}"
    if redis_url:
        try:
            import redis
            client = redis.Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)
            key = f"stockarmobile:public-vendor:{int(company_id)}:{remote}"
            if operation_scope:
                operation_key = key + ":operation" + operation_scope
                result = client.eval(
                    "local known = redis.call('EXISTS', KEYS[2]); "
                    "if known == 1 then return 1 end; "
                    "local count = redis.call('INCR', KEYS[1]); "
                    "if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end; "
                    "if count > tonumber(ARGV[1]) then redis.call('SET', KEYS[2], '0', 'EX', ARGV[2]); return 0 end; "
                    "redis.call('SET', KEYS[2], '1', 'EX', ARGV[3]); return 1",
                    2,
                    key,
                    operation_key,
                    PUBLIC_VENDOR_CHAT_LIMIT,
                    PUBLIC_VENDOR_CHAT_WINDOW,
                    86400,
                )
                return bool(result)
            count = int(client.incr(key))
            if count == 1:
                client.expire(key, PUBLIC_VENDOR_CHAT_WINDOW)
            return count <= PUBLIC_VENDOR_CHAT_LIMIT
        except Exception:
            current_app.logger.warning("Public vendor Redis rate limit unavailable; using PostgreSQL fallback.")
    try:
        if operation_scope:
            recent_duplicate = (
                ConversationMessage.query
                .filter(
                    ConversationMessage.company_id == int(company_id),
                    ConversationMessage.conversation_id == int(conversation_id or 0),
                    ConversationMessage.idempotency_key == str(request_key),
                    ConversationMessage.role == "user",
                )
                .first()
            )
            if recent_duplicate is not None:
                return True
        cutoff = datetime.utcnow() - timedelta(seconds=PUBLIC_VENDOR_CHAT_WINDOW)
        query = (
            db.session.query(db.func.count(ConversationMessage.id))
            .join(Conversation, Conversation.id == ConversationMessage.conversation_id)
            .filter(
                ConversationMessage.company_id == int(company_id),
                ConversationMessage.role == "user",
                ConversationMessage.created_at >= cutoff,
                Conversation.channel == "webchat",
                Conversation.id == int(conversation_id or 0),
            )
        )
        recent_count = int(query.scalar() or 0)
        return recent_count < PUBLIC_VENDOR_CHAT_LIMIT
    except Exception:
        current_app.logger.exception("Public vendor DB rate limit unavailable; using session fallback.")
    now = int(time.time())
    state = session.get("public_vendor_rate") or {}
    if not isinstance(state, dict) or now - int(state.get("started_at", 0) or 0) >= PUBLIC_VENDOR_CHAT_WINDOW:
        session["public_vendor_rate"] = {"started_at": now, "count": 1, "operations": [operation_scope] if operation_scope else []}
        return True
    operations = state.get("operations") if isinstance(state.get("operations"), list) else []
    if operation_scope and operation_scope in operations:
        return True
    count = int(state.get("count", 0) or 0)
    if count >= PUBLIC_VENDOR_CHAT_LIMIT:
        return False
    state["count"] = count + 1
    if operation_scope:
        operations.append(operation_scope)
        state["operations"] = operations[-100:]
    session["public_vendor_rate"] = state
    session.modified = True
    return True


@bp.before_request
def _require_ai_access():
    if request.endpoint in {"ai_agents.public_vendor_chat", "ai_agents.public_vendor_chat_message", "ai_agents.campaign_unsubscribe"}:
        return None
    if not can_access_ai(current_user):
        flash("Tu usuario no tiene habilitado el acceso a Agentes IA. Pedile al administrador de la empresa que lo active desde Mi Empresa.", "warning")
        return redirect(url_for("dashboard.index"))



VENDOR_AI_ORDER_OBSERVATION = "Pedido generado por el Vendedor 24 hs de StockARmobile."


def _scope_query_to_branch(query, model, branch_id: int | None, *, include_legacy: bool = False):
    """Apply a tenant-owned branch scope, optionally retaining legacy unassigned rows."""
    if branch_id is None:
        return query
    branch_id = int(branch_id)
    if include_legacy:
        return query.filter(or_(model.branch_id == branch_id, model.branch_id.is_(None)))
    return query.filter(model.branch_id == branch_id)


def _vendor_dashboard_metrics(
    company_id: int,
    month_start: datetime,
    branch_id: int | None = None,
    *,
    include_legacy: bool = False,
) -> dict:
    """Return current-month Vendor IA orders and derived sales for one branch or the tenant."""
    from app import Quote, Sale

    orders_query = Quote.query.filter(
        Quote.company_id == int(company_id),
        Quote.created_at >= month_start,
        Quote.observations == VENDOR_AI_ORDER_OBSERVATION,
    )
    orders = _scope_query_to_branch(
        orders_query, Quote, branch_id, include_legacy=include_legacy
    ).count()

    sales_query = (
        db.session.query(Sale.id)
        .join(Quote, Quote.converted_sale_id == Sale.id)
        .filter(
            Quote.company_id == int(company_id),
            Quote.observations == VENDOR_AI_ORDER_OBSERVATION,
            Sale.company_id == int(company_id),
            Sale.created_at >= month_start,
            Sale.status.notin_(["cancelada", "anulada"]),
        )
    )
    sales_query = _scope_query_to_branch(
        sales_query, Quote, branch_id, include_legacy=include_legacy
    )
    sales_query = _scope_query_to_branch(
        sales_query, Sale, branch_id, include_legacy=include_legacy
    )
    return {"orders": int(orders), "ai_sales": int(sales_query.count())}

def _context():
    from app import Client, Company, Product, Quote, Sale, SaleItem, Payment
    from services.ai_agent.subscription_service import AISubscriptionService

    company_id = current_user.company_id
    company = Company.query.get(company_id)
    from app import Branch
    from stockarmobile.branch_context import get_active_branch_id

    active_branch_id = get_active_branch_id()
    active_branch = (
        Branch.query.filter_by(
            id=int(active_branch_id), company_id=int(company_id), active=True
        ).first()
        if active_branch_id is not None
        else None
    )
    agents = ensure_default_agents(company_id)
    preferences = get_ai_preferences(company)
    whatsapp_connection = get_whatsapp_connection(company, branch_id=active_branch_id)
    whatsapp_connected = bool(
        whatsapp_connection.get("enabled")
        and whatsapp_connection.get("phone_number_id")
        and whatsapp_connection.get("access_token")
    )
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_start = (month_start - timedelta(days=1)).replace(day=1)

    include_legacy_branch_rows = bool(active_branch and active_branch.is_default)

    # Keep the general Agentes IA dashboard and analyst cards company-wide.
    # The Vendedor view receives a separate, selected-branch metric snapshot below.
    conversations = Conversation.query.filter(
        Conversation.company_id == company_id,
        Conversation.created_at >= month_start,
    ).count()
    clients_attended = (
        db.session.query(Conversation.external_conversation_id)
        .filter(
            Conversation.company_id == company_id,
            Conversation.created_at >= month_start,
            Conversation.external_conversation_id.isnot(None),
        )
        .distinct()
        .count()
    )
    quotes = Quote.query.filter(
        Quote.company_id == company_id,
        Quote.created_at >= month_start,
    ).count()
    sales_month = Sale.query.filter(
        Sale.company_id == company_id,
        Sale.created_at >= month_start,
        Sale.status.notin_(["cancelada", "anulada"]),
    )
    sales_previous = Sale.query.filter(
        Sale.company_id == company_id,
        Sale.created_at >= previous_start,
        Sale.created_at < month_start,
        Sale.status.notin_(["cancelada", "anulada"]),
    )

    vendor_conversations = _scope_query_to_branch(
        Conversation.query.filter(
            Conversation.company_id == company_id,
            Conversation.created_at >= month_start,
            Conversation.channel.in_(("webchat", "whatsapp")),
        ),
        Conversation, active_branch_id,
        include_legacy=include_legacy_branch_rows,
    ).count()
    vendor_clients_attended = _scope_query_to_branch(
        db.session.query(Conversation.external_conversation_id).filter(
            Conversation.company_id == company_id,
            Conversation.created_at >= month_start,
            Conversation.channel.in_(("webchat", "whatsapp")),
            Conversation.external_conversation_id.isnot(None),
        ),
        Conversation, active_branch_id,
        include_legacy=include_legacy_branch_rows,
    ).distinct().count()
    vendor_quotes = _scope_query_to_branch(
        Quote.query.filter(
            Quote.company_id == company_id,
            Quote.created_at >= month_start,
        ),
        Quote, active_branch_id,
        include_legacy=include_legacy_branch_rows,
    ).count()
    sales_total = float(sales_month.with_entities(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).scalar() or 0)
    previous_total = float(sales_previous.with_entities(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).scalar() or 0)
    sales_change = None if previous_total == 0 else round(((sales_total - previous_total) / previous_total) * 100, 1)
    if active_branch_id is not None:
        from app import BranchStock
        critical_stock = Product.query.outerjoin(
            BranchStock,
            (BranchStock.product_id == Product.id)
            & (BranchStock.company_id == Product.company_id)
            & (BranchStock.branch_id == active_branch_id),
        ).filter(
            Product.company_id == company_id,
            Product.active.is_(True),
            db.func.coalesce(BranchStock.stock, 0)
            <= db.func.coalesce(BranchStock.min_stock, Product.min_stock),
        ).count()
    else:
        critical_stock = Product.query.filter(
            Product.company_id == company_id,
            Product.active.is_(True),
            Product.stock <= Product.min_stock,
        ).count()
    sold_product_ids = db.session.query(SaleItem.product_id).join(Sale).filter(Sale.company_id == company_id, Sale.created_at >= now - timedelta(days=90), Sale.status.notin_(["cancelada", "anulada"])).distinct()
    low_rotation = Product.query.filter(Product.company_id == company_id, Product.active.is_(True), ~Product.id.in_(sold_product_ids)).count()
    vendor_metrics = _vendor_dashboard_metrics(
        company_id,
        month_start,
    )
    vendor_branch_metrics = {
        "conversations": int(vendor_conversations),
        "clients_attended": int(vendor_clients_attended),
        "quotes": int(vendor_quotes),
        **_vendor_dashboard_metrics(
            company_id,
            month_start,
            branch_id=active_branch_id,
            include_legacy=include_legacy_branch_rows,
        ),
    }
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

    pending_ai_qr_payment = (
        Payment.query
        .filter(
            Payment.company_id == company_id,
            Payment.provider == "mercadopago_ai_qr",
            Payment.status.in_(("pending", "in_process", "authorized")),
        )
        .order_by(Payment.id.desc())
        .first()
    )
    pending_ai_qr_checkout = None
    if pending_ai_qr_payment is not None:
        qr_ref_parts = {}
        for segment in str(pending_ai_qr_payment.external_reference or "").split("|"):
            if ":" in segment:
                key, value = segment.split(":", 1)
                qr_ref_parts[key.strip()] = value.strip()
        qr_plan_code = str(qr_ref_parts.get("plan_code") or "").strip().lower()
        if qr_ref_parts.get("ai_subscription_qr") == "true" and qr_plan_code:
            pending_ai_qr_checkout = {
                "payment_record_id": pending_ai_qr_payment.id,
                "plan_code": qr_plan_code,
                "status": str(pending_ai_qr_payment.status or "pending").strip().lower(),
            }

    from services.ai_agent.branch_vendor_service import list_branch_vendor_statuses, monthly_price_ars
    from services.ai_agent.vendor_publication import branch_publication_status, publication_status
    branch_vendor_statuses = list_branch_vendor_statuses(company)
    for branch_status in branch_vendor_statuses:
        branch = branch_status["branch"]
        try:
            branch_status["publication"] = (
                publication_status(company)
                if branch.is_default
                else branch_publication_status(company, branch)
            )
        except Exception:
            branch_status["publication"] = {
                "published": False, "available": False, "licensed": False, "url": None
            }
        branch_status["web_active"] = bool(
            branch_status.get("allowed")
            and (branch_status.get("publication") or {}).get("available")
        )
        try:
            branch_whatsapp = get_whatsapp_connection(company, branch_id=branch.id)
            branch_status["whatsapp_connected"] = bool(
                branch_status.get("allowed")
                and branch_whatsapp.get("enabled")
                and branch_whatsapp.get("phone_number_id")
                and branch_whatsapp.get("access_token")
            )
            branch_status["whatsapp_phone_number"] = str(
                branch_whatsapp.get("display_phone_number") or ""
            ).strip()
        except Exception:
            branch_status["whatsapp_connected"] = False
            branch_status["whatsapp_phone_number"] = ""
        branch_status["is_selected"] = bool(
            active_branch_id is not None and int(branch.id) == int(active_branch_id)
        )

    active_branch_status = next(
        (
            row for row in branch_vendor_statuses
            if active_branch_id is not None and int(row["branch"].id) == int(active_branch_id)
        ),
        None,
    )
    active_branch_vendor_allowed = (
        bool(active_branch_status.get("allowed"))
        if active_branch_status is not None
        else bool(agent_access["vendedor"].allowed)
    )
    active_branch_vendor_reason = (
        active_branch_status.get("reason") if active_branch_status is not None else None
    )
    if active_branch_status is not None:
        active_publication = active_branch_status.get("publication") or {}
        public_webchat_enabled = bool(
            active_branch_status.get("allowed") and active_publication.get("available")
        )
        public_vendor_url = (
            active_publication.get("url") if public_webchat_enabled else None
        )
        whatsapp_connected = bool(active_branch_status.get("whatsapp_connected"))
        whatsapp_phone_number = active_branch_status.get("whatsapp_phone_number") or ""
    else:
        public_webchat_enabled = bool(
            preferences.get("ai_agent", {}).get("public_webchat_enabled", False)
        )
        public_vendor_url = (
            url_for("ai_agents.public_vendor_chat", token=_public_vendor_token(company_id))
            if agent_access["vendedor"].allowed else None
        )
        if agent_access["vendedor"].allowed:
            try:
                publication = publication_status(company)
                if publication.get("available") and publication.get("url"):
                    public_vendor_url = publication["url"]
            except Exception:
                current_app.logger.exception(
                    "No se pudo resolver la URL estable del Vendedor público company_id=%s",
                    company_id,
                )
        whatsapp_phone_number = str(
            whatsapp_connection.get("display_phone_number") or ""
        ).strip()
    if not active_branch_vendor_allowed:
        public_webchat_enabled = False
        public_vendor_url = None
        whatsapp_connected = False
        whatsapp_phone_number = ""
    public_vendor_qr = None
    if public_webchat_enabled and public_vendor_url:
        try:
            from services.ai_agent.vendor_publication import qr_data_uri
            public_vendor_qr = qr_data_uri(public_vendor_url)
        except Exception:
            current_app.logger.exception(
                "No se pudo generar el QR del Vendedor IA company_id=%s",
                company_id,
            )
    metrics_scope_label = (
        active_branch.name if active_branch is not None else "Toda la empresa"
    )

    branch_vendor_qr_preview = session.get("branch_vendor_qr_preview")
    if isinstance(branch_vendor_qr_preview, dict):
        try:
            payment_row = Payment.query.filter_by(
                id=int(branch_vendor_qr_preview.get("payment_record_id") or 0),
                company_id=company_id,
                provider="mercadopago_branch_vendor_qr",
            ).first()
            if payment_row is not None:
                from company_billing import _branch_vendor_qr_preview
                branch_vendor_qr_preview = _branch_vendor_qr_preview(company, payment_row)
            else:
                branch_vendor_qr_preview = None
        except Exception:
            current_app.logger.exception("No se pudo actualizar el estado del QR Vendedor IA company_id=%s", company_id)
            branch_vendor_qr_preview = None
        if branch_vendor_qr_preview is None or branch_vendor_qr_preview.get("status") in {"failed"}:
            session.pop("branch_vendor_qr_preview", None)

    return {"company": company, "agents": agents, "preferences": preferences, "metrics": {"conversations": conversations, "clients_attended": clients_attended, "quotes": quotes, "sales": int(sales_month.count()), "orders": vendor_metrics["orders"], "ai_sales": vendor_metrics["ai_sales"]}, "vendor_branch_metrics": vendor_branch_metrics, "analyst": {"sales_change": sales_change, "critical_stock": critical_stock, "low_rotation": low_rotation, "opportunities": None}, "ai_status": ai_status, "ai_plans": AI_PLANS, "pending_ai_qr_checkout": pending_ai_qr_checkout, "branch_vendor_statuses": branch_vendor_statuses, "branch_vendor_monthly_price": float(monthly_price_ars()), "branch_vendor_qr_preview": branch_vendor_qr_preview, "agent_labels": AGENT_LABELS, "ai_plan": ai_plan, "ai_usage": ai_usage, "agent_access": agent_access, "invoice_access": invoice_access, "any_chat_agent": any_chat_agent, "default_chat_agent": default_chat_agent, "public_webchat_enabled": public_webchat_enabled, "public_vendor_url": public_vendor_url, "public_vendor_qr": public_vendor_qr, "whatsapp_connected": whatsapp_connected, "whatsapp_phone_number": whatsapp_phone_number, "active_branch": active_branch, "active_branch_id": active_branch_id, "active_branch_vendor_status": active_branch_status, "active_branch_vendor_allowed": active_branch_vendor_allowed, "active_branch_vendor_reason": active_branch_vendor_reason, "metrics_scope_label": metrics_scope_label, "plan_url": url_for("ai_agents.agent", agent="planes"), "ai_checkout_url": url_for("company_billing.create_ai_subscription_checkout"), "config_url": url_for("ai_admin.index"), "chat_url": url_for("dashboard.ai_agent_chat")}


def _campaign_rows(company_id: int):
    from app import Campaign
    return Campaign.query.filter_by(company_id=company_id).order_by(Campaign.updated_at.desc(), Campaign.id.desc()).all()


def _campaign_summary(company_id: int):
    rows = _campaign_rows(company_id)
    return {status: sum(1 for row in rows if row.status == status) for status in ("BORRADOR", "PENDIENTE_APROBACION", "APROBADA", "EN_PREPARACION", "ENVIADA", "ENVIADA_PARCIAL", "FALLIDA", "SIN_ENVIO", "CANCELADA")}


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


def _exchange_long_lived_whatsapp_token(access_token: str, app_id: str, app_secret: str) -> tuple[str, dict]:
    """Try Meta's supported short-lived -> long-lived user-token exchange.

    A failed extension is non-fatal: Embedded Signup's original token remains
    usable and its provider-reported expiry is still recorded for reconnection.
    Never log or return credential material.
    """
    try:
        response = requests.get(
            _meta_graph_url("oauth/access_token"),
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": access_token,
            },
            timeout=20,
        )
        if not response.ok:
            current_app.logger.info("WhatsApp long-lived token exchange unavailable; preserving original authorization.")
            return access_token, {}
        payload = response.json() or {}
        extended = str(payload.get("access_token") or "").strip()
        return (extended or access_token), (payload if extended else {})
    except (requests.RequestException, ValueError):
        current_app.logger.info("WhatsApp long-lived token exchange unavailable; preserving original authorization.")
        return access_token, {}


def _meta_expiration_iso(value) -> str:
    try:
        seconds = int(value)
        if seconds <= 0:
            return ""
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


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

def _load_whatsapp_templates(connection: dict) -> list[dict]:
    waba_id = str(connection.get("business_account_id") or "").strip()
    access_token = str(connection.get("access_token") or "").strip()
    if not waba_id or not access_token:
        raise ValueError("La cuenta de WhatsApp no tiene WABA o autorización disponible.")
    response = requests.get(
        _meta_graph_url(f"{waba_id}/message_templates"),
        params={"fields": "name,status,category,language", "limit": 100},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if not response.ok:
        current_app.logger.warning("WhatsApp template sync failed: %s", _meta_error(response))
        raise ValueError("Meta no permitió consultar las plantillas. Revisá los permisos de administración de WhatsApp.")
    payload = response.json() or {}
    templates = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        language = str(item.get("language") or "").strip()
        status = str(item.get("status") or "").upper()
        if name and language:
            templates.append({
                "name": name[:120],
                "language": language[:20],
                "status": status[:30],
                "category": str(item.get("category") or "")[:40],
            })
    return templates


@bp.get("/vendedor/conectar-whatsapp")
@tenant_required
@company_admin_required
def vendor_whatsapp_connect():
    company = _current_active_company()
    entitlement = can_use_ai_feature(company, "vendedor")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye el Vendedor IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))
    if not current_app.config.get("WHATSAPP_VENDOR_UI_ENABLED", False):
        return redirect(url_for("ai_agents.agent", agent="vendedor"))
    from app import Branch
    from stockarmobile.branch_context import get_default_branch_id
    from services.ai_agent.branch_vendor_service import branch_vendor_access
    branch_id = request.args.get("branch_id", type=int) or get_default_branch_id(company.id)
    branch = Branch.query.filter_by(id=branch_id, company_id=company.id, active=True).first() if branch_id else None
    if branch is None or not branch_vendor_access(company, branch).get("allowed"):
        flash("La sucursal necesita una licencia Vendedor IA activa para conectar su WhatsApp.", "warning")
        return redirect(url_for("ai_agents.agent", agent="vendedor"))
    whatsapp = get_whatsapp_connection(company, branch_id=branch.id)
    # Never pass a raw access token into a template context. The browser only needs
    # the connected state and the display fields for this setup screen.
    connected = bool(
        whatsapp.get("enabled")
        and whatsapp.get("phone_number_id")
        and whatsapp.get("access_token")
    )
    force_reconnect = request.args.get("force", "").strip() == "1"
    if force_reconnect:
        connected = False
    app_id, config_id = _embedded_signup_config()
    app_secret = (os.getenv("META_APP_SECRET") or os.getenv("WHATSAPP_APP_SECRET") or "").strip()
    onboarding_ready = bool(app_id and config_id and app_secret)
    safe_whatsapp = {
        "enabled": connected,
        "phone_number_id": str(whatsapp.get("phone_number_id") or "").strip(),
        "display_phone_number": str(whatsapp.get("display_phone_number") or "").strip(),
    }
    return render_template(
        "ai_agents/whatsapp_onboarding.html",
        app_id=app_id,
        config_id=config_id,
        onboarding_ready=onboarding_ready,
        connected=connected,
        whatsapp=safe_whatsapp,
        token_health=whatsapp.get("token_health", "unknown"),
        token_expires_at=whatsapp.get("access_token_expires_at", ""),
        token_days_remaining=whatsapp.get("token_days_remaining"),
        force_reconnect=force_reconnect,
        selected_template=whatsapp.get("template_name", ""),
        selected_template_language=whatsapp.get("template_language", "es_AR"),
        templates_url=url_for("ai_agents.vendor_whatsapp_templates", branch_id=branch.id),
        template_save_url=url_for("ai_agents.vendor_whatsapp_template_save", branch_id=branch.id),
        template_provision_url=url_for("ai_agents.vendor_whatsapp_templates_provision", branch_id=branch.id),
        branch=branch,
        complete_url=url_for("ai_agents.vendor_whatsapp_complete", branch_id=branch.id),
        vendor_url=url_for("ai_agents.agent", agent="vendedor", branch_id=branch.id),
    )


@bp.post("/vendedor/conectar-whatsapp/complete")
@tenant_required
@company_admin_required
def vendor_whatsapp_complete():
    if not current_app.config.get("WHATSAPP_VENDOR_UI_ENABLED", False):
        return jsonify({"success": False, "error": "La conexión de WhatsApp todavía no está habilitada en StockARmobile."}), 503
    company = _current_active_company()
    entitlement = can_use_ai_feature(company, "vendedor")
    if not entitlement.allowed:
        return jsonify({"success": False, "error": entitlement.reason}), 403
    payload = request.get_json(silent=True) or request.form.to_dict()
    from app import Branch
    from stockarmobile.branch_context import get_default_branch_id
    from services.ai_agent.branch_vendor_service import branch_vendor_access
    requested_branch_id = request.args.get("branch_id", type=int) or payload.get("branch_id", type=int)
    branch_id = requested_branch_id or get_default_branch_id(company.id)
    branch = Branch.query.filter_by(id=branch_id, company_id=company.id, active=True).first() if branch_id else None
    if branch is None or not branch_vendor_access(company, branch).get("allowed"):
        return jsonify({"success": False, "error": "La sucursal necesita una licencia Vendedor IA activa para conectar WhatsApp."}), 403
    code = str(payload.get("code") or "").strip()
    waba_id = str(payload.get("waba_id") or "").strip()
    phone_number_id = str(payload.get("phone_number_id") or "").strip()
    business_id = str(payload.get("business_id") or payload.get("businessId") or "").strip()
    if not code or not waba_id:
        return jsonify({"success": False, "error": "Meta no devolvió el código de autorización y la cuenta de WhatsApp."}), 400
    # Phone number ID can be omitted by Meta on the WhatsApp Business App
    # coexistence completion event. Resolve it server-side from the authorized
    # WABA; never trust an arbitrary browser-supplied Graph path.
    if (
        len(code) > 4096
        or not re.fullmatch(r"\d{1,30}", waba_id)
        or (phone_number_id and not re.fullmatch(r"\d{1,30}", phone_number_id))
    ):
        return jsonify({"success": False, "error": "Meta devolvió identificadores inválidos. Volvé a iniciar la conexión."}), 400
    if business_id and not re.fullmatch(r"\d{1,30}", business_id):
        business_id = ""
    onboarding_event = str(payload.get("onboarding_event") or "").strip().upper()
    is_coexistence = onboarding_event == "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"

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
        exchange_payload = exchange.json() or {}
        access_token = str(exchange_payload.get("access_token") or "").strip()
        if not access_token:
            return jsonify({"success": False, "error": "Meta no devolvió un token válido para la conexión."}), 400

        token_obtained_at = datetime.now(timezone.utc).isoformat()
        token_expiry_payload = exchange_payload
        access_token, extended_payload = _exchange_long_lived_whatsapp_token(access_token, app_id, app_secret)
        if extended_payload:
            token_expiry_payload = extended_payload
        expires_in = token_expiry_payload.get("expires_in") or exchange_payload.get("expires_in")
        try:
            token_expires_at = (
                (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(expires_in)))).isoformat()
                if expires_in is not None and int(expires_in) > 0 else ""
            )
        except (TypeError, ValueError, OverflowError):
            token_expires_at = ""
        data_access_expires_at = _meta_expiration_iso(
            token_expiry_payload.get("data_access_expiration_time")
            or exchange_payload.get("data_access_expiration_time")
        )

        # Query the WABA with the exchanged token first. For coexistence, Meta's
        # FINISH event may contain only waba_id; resolve a unique selected phone
        # here instead of rejecting the legitimate completion in the browser.
        waba_response = requests.get(
            _meta_graph_url(f"{waba_id}/phone_numbers"),
            params={"fields": "id,display_phone_number,verified_name,status,platform_type,code_verification_status,name_status"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if not waba_response.ok:
            current_app.logger.warning("WhatsApp Embedded Signup WABA validation failed: %s", _meta_error(waba_response))
            return jsonify({"success": False, "error": "Meta no permitió validar la cuenta de WhatsApp seleccionada."}), 400
        waba_phones = [
            item for item in ((waba_response.json() or {}).get("data") or [])
            if isinstance(item, dict) and re.fullmatch(r"\d{1,30}", str(item.get("id") or ""))
        ]

        if phone_number_id:
            matching_phones = [item for item in waba_phones if str(item.get("id") or "") == phone_number_id]
            if not matching_phones:
                return jsonify({"success": False, "error": "El número no pertenece a la cuenta de WhatsApp seleccionada."}), 400
        else:
            requested_phone = str(payload.get("phone_number") or payload.get("display_phone_number") or "").strip()
            requested_digits = "".join(ch for ch in requested_phone if ch.isdigit())
            matching_phones = []
            if requested_digits:
                matching_phones = [
                    item for item in waba_phones
                    if "".join(ch for ch in str(item.get("display_phone_number") or "") if ch.isdigit()) == requested_digits
                ]
            if not matching_phones and len(waba_phones) == 1:
                matching_phones = waba_phones
            if len(matching_phones) != 1:
                current_app.logger.warning(
                    "WhatsApp Embedded Signup returned no unique phone_number_id; company_id=%s waba_id=%s phone_count=%s coexistence=%s",
                    company.id, waba_id, len(waba_phones), is_coexistence,
                )
                return jsonify({
                    "success": False,
                    "error": "Meta autorizó la cuenta, pero no pudimos identificar un único número. Volvé a conectar el WhatsApp y elegí el número del comercio.",
                }), 400
            phone_number_id = str(matching_phones[0].get("id") or "").strip()

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
        if not any(str(item.get("id") or "") == phone_number_id for item in waba_phones):
            return jsonify({"success": False, "error": "El número no pertenece a la cuenta de WhatsApp seleccionada."}), 400

        # The SuperAdmin acquisition number is a separate system channel and cannot
        # be attached to a tenant workspace.
        commercial_phone_id = str(os.getenv("WHATSAPP_COMMERCIAL_PHONE_NUMBER_ID") or "").strip()
        commercial_enabled = (os.getenv("WHATSAPP_COMMERCIAL_ENABLED") or "0").strip().lower() in {"1", "true", "yes", "on"}
        if commercial_enabled and commercial_phone_id and phone_number_id == commercial_phone_id:
            return jsonify({"success": False, "error": "Este número está reservado para el canal comercial de StockArMobile."}), 409

        # A WhatsApp phone number may belong to exactly one company/branch.
        # The check occurs before Meta subscription/registration side effects.
        existing_company = company_for_whatsapp_phone_id(phone_number_id)
        existing_company_by_branch, existing_branch = company_branch_for_whatsapp_phone_id(phone_number_id)
        if existing_company is not None and (
            int(existing_company.id) != int(company.id)
            or (
                existing_company_by_branch is not None
                and int(existing_company_by_branch.id) != int(company.id)
            )
            or existing_branch is None
            or int(existing_branch.id) != int(branch.id)
        ):
            current_app.logger.warning(
                "WhatsApp connection refused: phone_number_id already belongs to another company/branch; target_company_id=%s target_branch_id=%s",
                company.id,
                branch.id,
            )
            return jsonify({
                "success": False,
                "error": "Este número de WhatsApp ya está conectado a otra empresa o sucursal de StockARmobile. Desconectalo allí antes de continuar.",
            }), 409

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
        if not is_coexistence and status not in {"CONNECTED"} and platform_type not in {"ON_PREMISE"}:
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

        whatsapp_updates = {
            "waba_id": waba_id,
            "access_token_obtained_at": token_obtained_at,
            "access_token_expires_at": token_expires_at,
            "data_access_expires_at": data_access_expires_at,
            "access_token_type": str(token_expiry_payload.get("token_type") or exchange_payload.get("token_type") or "bearer")[:40],
            "token_health": "active" if token_expires_at else "unknown",
        }
        if business_id:
            whatsapp_updates["business_id"] = business_id
        if registration_pin:
            whatsapp_updates["registration_pin_encrypted"] = encrypt_secret(registration_pin)
        is_extra_branch = not bool(branch.is_default)
        configure_whatsapp_connection(
            company,
            phone_number_id=phone_number_id,
            access_token=access_token,
            business_account_id=waba_id,
            display_phone_number=str(phone.get("display_phone_number") or "").strip(),
            enabled=True,
            template_name="",
            template_language="es_AR",
            branch_id=branch.id if is_extra_branch else None,
            whatsapp_updates=whatsapp_updates,
        )
        if not is_extra_branch:
            update_ai_preferences(company, whatsapp_updates=whatsapp_updates)
        db.session.commit()
        master_template_provisioning = []
        try:
            from services.ai_agent.whatsapp_master_template_service import (
                provision_master_templates,
                record_master_template_provisioning,
            )

            master_template_provisioning = provision_master_templates({
                "enabled": True,
                "phone_number_id": phone_number_id,
                "business_account_id": waba_id,
                "access_token": access_token,
            })
            record_master_template_provisioning(
                company,
                master_template_provisioning,
                branch=branch,
            )
            db.session.commit()
        except Exception as exc:
            # The WhatsApp connection is already committed. A template-permission
            # or Meta review problem must never roll back a working channel.
            current_app.logger.warning(
                "WhatsApp master-template provisioning deferred company_id=%s branch_id=%s error_type=%s",
                company.id,
                branch.id,
                type(exc).__name__,
            )
        current_app.logger.info("WhatsApp Embedded Signup connected company_id=%s waba_id=%s phone_number_id=%s", current_user.company_id, waba_id, phone_number_id)
        return jsonify({
            "success": True,
            "phone_number": str(phone.get("display_phone_number") or "").strip(),
            "redirect_url": url_for("ai_agents.agent", agent="vendedor", branch_id=branch.id, whatsapp="connected"),
            "master_template_provisioning": master_template_provisioning,
        })
    except requests.RequestException:
        current_app.logger.exception("WhatsApp Embedded Signup network failure")
        return jsonify({"success": False, "error": "No pudimos comunicarnos con Meta. Intentá nuevamente en unos segundos."}), 502
    except Exception:
        db.session.rollback()
        current_app.logger.exception("WhatsApp Embedded Signup unexpected failure")
        return jsonify({"success": False, "error": "No pudimos completar la conexión de WhatsApp. Intentá nuevamente."}), 500


@bp.get("/vendedor/whatsapp/plantillas")
@tenant_required
@company_admin_required
def vendor_whatsapp_templates():
    company = _current_active_company()
    from app import Branch
    from stockarmobile.branch_context import get_default_branch_id
    from services.ai_agent.branch_vendor_service import branch_vendor_access

    branch_id = request.args.get("branch_id", type=int) or get_default_branch_id(company.id)
    branch = Branch.query.filter_by(id=branch_id, company_id=company.id, active=True).first() if branch_id else None
    if branch is None or not branch_vendor_access(company, branch).get("allowed"):
        return jsonify({"success": False, "error": "La sucursal no tiene habilitado el Vendedor IA."}), 403
    connection = get_whatsapp_connection(company, branch_id=branch.id)
    if not connection.get("enabled") or not connection.get("access_token"):
        return jsonify({"success": False, "error": "Conectá primero el WhatsApp de esta sucursal."}), 409
    try:
        templates = _load_whatsapp_templates(connection)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 502
    approved = [
        item for item in templates
        if item["status"] == "APPROVED"
        and item.get("campaign_compatible") is True
        and str(item.get("category") or "").upper() == "MARKETING"
    ]
    return jsonify({
        "success": True,
        "templates": approved,
        "selected": {
            "name": str(connection.get("template_name") or ""),
            "language": str(connection.get("template_language") or "es_AR"),
        },
    })


@bp.post("/vendedor/whatsapp/plantillas/predeterminadas")
@tenant_required
@company_admin_required
def vendor_whatsapp_templates_provision():
    """Install the StockARmobile master campaign template in this tenant WABA."""
    company = _current_active_company()
    from app import Branch
    from stockarmobile.branch_context import get_default_branch_id
    from services.ai_agent.branch_vendor_service import branch_vendor_access

    branch_id = request.args.get("branch_id", type=int) or get_default_branch_id(company.id)
    branch = Branch.query.filter_by(id=branch_id, company_id=company.id, active=True).first() if branch_id else None
    if branch is None or not branch_vendor_access(company, branch).get("allowed"):
        return jsonify({"success": False, "error": "La sucursal no tiene habilitado el Vendedor IA."}), 403

    connection = get_whatsapp_connection(company, branch_id=branch.id)
    if not connection.get("enabled") or not connection.get("access_token"):
        return jsonify({"success": False, "error": "Conectá primero el WhatsApp de esta sucursal."}), 409

    try:
        from services.ai_agent.whatsapp_master_template_service import (
            provision_master_templates,
            record_master_template_provisioning,
        )

        results = provision_master_templates(connection)
        record_master_template_provisioning(company, results, branch=branch)
        # As soon as Meta approves the master template, make it the default for
        # this WhatsApp channel. Never overwrite a selection already made by the merchant.
        if not str(connection.get("template_name") or "").strip():
            approved_master = next(
                (
                    item for item in results
                    if item.get("status") == "APPROVED"
                    and item.get("campaign_compatible") is True
                    and str(item.get("category") or "").upper() == "MARKETING"
                ),
                None,
            )
            if approved_master:
                configure_whatsapp_connection(
                    company,
                    phone_number_id=connection.get("phone_number_id"),
                    access_token=None,
                    business_account_id=connection.get("business_account_id"),
                    display_phone_number=connection.get("display_phone_number"),
                    enabled=True,
                    template_name=approved_master["name"],
                    template_language=approved_master["language"],
                    branch_id=branch.id if not branch.is_default else None,
                )
        db.session.commit()
        updated_connection = get_whatsapp_connection(company, branch_id=branch.id)
        return jsonify({
            "success": True,
            "master_templates": results,
            "selected": {
                "name": str(updated_connection.get("template_name") or ""),
                "language": str(updated_connection.get("template_language") or "es_AR"),
            },
        })
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 502
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "WhatsApp master-template provisioning failed company_id=%s branch_id=%s",
            company.id,
            branch.id,
        )
        return jsonify({"success": False, "error": "No pudimos instalar la plantilla predeterminada. La conexión de WhatsApp sigue activa; podés volver a sincronizar."}), 500


@bp.post("/vendedor/whatsapp/plantillas")
@tenant_required
@company_admin_required
def vendor_whatsapp_template_save():
    company = _current_active_company()
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    language = str(payload.get("language") or "").strip()
    if not name or not language:
        return jsonify({"success": False, "error": "Seleccioná una plantilla aprobada y su idioma."}), 400

    from app import Branch
    from stockarmobile.branch_context import get_default_branch_id
    from services.ai_agent.branch_vendor_service import branch_vendor_access

    branch_id = request.args.get("branch_id", type=int) or get_default_branch_id(company.id)
    branch = Branch.query.filter_by(id=branch_id, company_id=company.id, active=True).first() if branch_id else None
    if branch is None or not branch_vendor_access(company, branch).get("allowed"):
        return jsonify({"success": False, "error": "La sucursal no tiene habilitado el Vendedor IA."}), 403
    connection = get_whatsapp_connection(company, branch_id=branch.id)
    if not connection.get("enabled") or not connection.get("access_token"):
        return jsonify({"success": False, "error": "Conectá primero el WhatsApp de esta sucursal."}), 409

    try:
        approved = _load_whatsapp_templates(connection)
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 502
    match = next((
        item for item in approved
        if item["status"] == "APPROVED"
        and item.get("campaign_compatible") is True
        and str(item.get("category") or "").upper() == "MARKETING"
        and item["name"] == name
        and item["language"] == language
    ), None)
    if match is None:
        return jsonify({"success": False, "error": "La plantilla ya no figura aprobada en Meta. Actualizá el listado."}), 409

    try:
        configure_whatsapp_connection(
            company,
            phone_number_id=connection.get("phone_number_id"),
            access_token=None,
            business_account_id=connection.get("business_account_id"),
            display_phone_number=connection.get("display_phone_number"),
            enabled=True,
            template_name=match["name"],
            template_language=match["language"],
            branch_id=branch.id if not branch.is_default else None,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("WhatsApp template selection failed company_id=%s branch_id=%s", company.id, branch.id)
        return jsonify({"success": False, "error": "No pudimos guardar la plantilla seleccionada."}), 500
    return jsonify({"success": True, "selected": {"name": match["name"], "language": match["language"]}})


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
            shipping_config={"mode": "fixed", "fixed_cost": 0},
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
            "mode": "fixed",
            "fixed_cost": float(options.get("standard_shipping_cost") or 0),
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
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "La consulta debe ser un objeto JSON."}), 400
    message = str(payload.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "error": "Escribí una consulta."}), 400
    if len(message) > 500:
        return jsonify({"success": False, "error": "La consulta es demasiado larga."}), 400
    idempotency_key = str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip()
    if not idempotency_key or len(idempotency_key) > 120:
        return jsonify({"success": False, "error": "Falta una clave de idempotencia válida."}), 400
    visitor_session_key = f"public_vendor_visitor_{company_id}"
    visitor_id = str(session.get(visitor_session_key) or uuid.uuid4().hex)
    session[visitor_session_key] = visitor_id
    from services.ai_agent.config_service import VENDOR_AGENT_NAME, ensure_default_agents
    from services.ai_agent.orchestrator_v2 import AgentRuntime
    vendor_agent = ensure_default_agents(company_id)[VENDOR_AGENT_NAME]
    conversation_id = payload.get("conversation_id")
    try:
        from services.ai_agent.providers.base import AIProviderError

        with public_operation_lock(scope="visitor", company_id=company_id, conversation_id=0, key=visitor_id):
            # Share the same strict tenant/visitor/tab-token validator as the
            # published URL so old signed links cannot resolve a newer tab's chat.
            from services.ai_agent.vendor_publication import _public_conversation

            conversation = _public_conversation(
                company,
                conversation_id,
                create=True,
                chat_session_id=payload.get("chat_session_id"),
            )
            if conversation is None:
                return jsonify({"success": False, "error": "La conversación ya no es válida."}), 403

            with public_operation_lock(scope="request", company_id=company_id, conversation_id=conversation.id, key=idempotency_key):
                operation = PublicVendorOperation.query.filter_by(
                    company_id=company_id,
                    conversation_id=conversation.id,
                    idempotency_key=idempotency_key,
                ).first()
                cached = cached_result(operation, operation_type="message")
                if cached is not None:
                    return jsonify(cached)
                if operation is not None and operation.operation_type != "message":
                    return jsonify({"success": False, "error": "La clave ya pertenece a otra operación."}), 409
                incoming = ConversationMessage.query.filter_by(
                    company_id=company_id,
                    conversation_id=conversation.id,
                    idempotency_key=idempotency_key,
                    role="user",
                ).first()
                assistant = None
                if incoming is not None:
                    assistant = ConversationMessage.query.filter_by(
                        company_id=company_id,
                        conversation_id=conversation.id,
                        trace_id=incoming.trace_id,
                        role="assistant",
                    ).order_by(ConversationMessage.id.desc()).first()
                if operation is not None and incoming is not None and assistant is None:
                    from services.ai_agent.vendor_publication import recover_committed_order_reply

                    recovered = recover_committed_order_reply(
                        company=company,
                        conversation=conversation,
                        operation=operation,
                        incoming=incoming,
                        visitor_id=visitor_id,
                    )
                    if recovered is not None:
                        response_payload = {
                            "success": True,
                            "conversation_id": recovered["conversation_id"],
                            "message_id": recovered["message_id"],
                            "assistant_message_id": recovered["assistant_message_id"],
                            "content": recovered["content"],
                        }
                        operation.trace_id = recovered.get("trace_id") or operation.trace_id
                        complete_operation(operation, response_payload, quote_id=operation.quote_id)
                        db.session.commit()
                        return jsonify(response_payload)
                if assistant is None:
                    access = can_use_ai(company, "vendedor")
                    if not access.allowed:
                        return jsonify({"success": False, "error": access.reason}), 403
                if operation is None and incoming is None and not _public_vendor_rate_limit(
                    company_id,
                    conversation_id=conversation.id,
                    request_key=idempotency_key,
                ):
                    return jsonify({"success": False, "error": "El vendedor está recibiendo muchas consultas. Esperá unos segundos y probá nuevamente."}), 429, {"Retry-After": str(PUBLIC_VENDOR_CHAT_WINDOW)}
                operation = get_or_create_operation(
                    company_id=company_id,
                    conversation_id=conversation.id,
                    idempotency_key=idempotency_key,
                    operation_type="message",
                    trace_id=incoming.trace_id if incoming is not None else None,
                )
                result = (
                    {
                        "conversation_id": conversation.id,
                        "message_id": incoming.id,
                        "assistant_message_id": assistant.id,
                        "content": assistant.content,
                        "trace_id": incoming.trace_id,
                    }
                    if assistant is not None
                    else AgentRuntime.process(
                        company_id=company_id,
                        conversation_id=conversation.id,
                        message=message,
                        channel="webchat",
                        sender_id=None,
                        idempotency_key=idempotency_key,
                        metadata={"from": visitor_id, "source": "public_webchat"},
                        include_system_prompt=True,
                        idempotency_lock_held=True,
                        trace_id=operation.trace_id,
                    )
                )
                response_payload = {
                    "success": True,
                    "conversation_id": result.get("conversation_id"),
                    "message_id": result.get("message_id"),
                    "assistant_message_id": result.get("assistant_message_id"),
                    "content": result.get("content"),
                }
                operation.trace_id = result.get("trace_id") or operation.trace_id
                complete_operation(operation, response_payload)
                db.session.commit()
                return jsonify(response_payload)
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 400
    except PublicOperationConflict as exc:
        db.session.rollback()
        return jsonify({"success": False, "error": str(exc)}), 409
    except AIProviderError as exc:
        db.session.rollback()
        status_code = int(getattr(exc, "status_code", 503) or 503)
        if status_code not in {429, 503}:
            status_code = 503
        return jsonify({"success": False, "error": str(exc)}), status_code
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error en vendedor web público: company_id=%s conversation_id=%s", company_id, conversation.id)
        return jsonify({"success": False, "error": "El vendedor está temporalmente ocupado. Probá nuevamente en unos segundos."}), 503


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
    return render_template(
        "ai_agents/campaigns.html",
        campaigns=_campaign_rows(current_user.company_id),
        campaign_summary=_campaign_summary(current_user.company_id),
        ai_marketing_send_enabled=current_app.config.get("AI_MARKETING_SEND_ENABLED", False),
        **_context(),
    )


@bp.route("/campanas/unsubscribe/<token>", methods=["GET", "POST"])
@csrf.exempt
def campaign_unsubscribe(token):
    from app import Client
    client = Client.query.filter_by(marketing_unsubscribe_token=str(token or "").strip()).first()
    if client is None:
        return render_template("ai_agents/campaign_unsubscribe.html", valid=False, confirmed=False), 404

    confirmed = request.method == "POST"
    if confirmed:
        # El token opaco autentica la solicitud de baja; el POST es idempotente
        # para clientes de correo que reintentan One-Click.
        client.email_marketing_consent = "opted_out"
        client.whatsapp_marketing_consent = "opted_out"
        db.session.commit()

    return render_template(
        "ai_agents/campaign_unsubscribe.html",
        valid=True,
        confirmed=confirmed,
    )


@bp.get("/campanas/<int:campaign_id>")
@tenant_required
def campaign_detail(campaign_id):
    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        abort(403)
    campaign = CampaignService._campaign(current_user.company_id, campaign_id)
    if campaign is None:
        abort(404)
    try:
        campaign_system_data = json.loads(campaign.system_data_json or "{}")
    except (TypeError, ValueError):
        campaign_system_data = {}
    template = campaign_system_data.get("whatsapp_template") or {}
    template_error = ""
    audience_diagnostics = _campaign_audience_diagnostics(campaign)
    detected_audience_count = audience_diagnostics["eligible"]
    excluded_audience_count = audience_diagnostics["excluded"]
    from services.ai_agent.tenant_campaign_service import (
        TenantCampaignRecipient,
        _campaign_branch_id,
        _fetch_campaign_template,
        _render_content,
        resolve_tenant_campaign_sender,
    )
    try:
        _sender_branch, sender_connection = resolve_tenant_campaign_sender(
            _current_active_company(),
            _campaign_branch_id(campaign),
            require_ready=True,
        )
    except ValueError as exc:
        sender_connection = {}
        template_error = str(exc)
    # Agent-created drafts do not carry a per-campaign template choice. Resolve
    # the merchant's approved template here so review shows a real preview before approval.
    if not template and sender_connection:
        try:
            template = _fetch_campaign_template(sender_connection)
        except ValueError as exc:
            template_error = str(exc)
    template_preview = ""
    if template.get("body_text"):
        try:
            template_body = str(template.get("body_text") or "")
            template_has_opt_out = bool(re.search(r"\bBAJA\b|dejar de recibir", template_body, re.IGNORECASE))
            copy = _render_content(
                campaign,
                SimpleNamespace(name="Cliente de ejemplo", whatsapp="5491100000000"),
                include_opt_out=not template_has_opt_out,
                preview=True,
            )
            template_preview = template_body
            for variable, value in (
                ("1", "Cliente de ejemplo"),
                ("2", campaign.product.name if campaign.product else "Varios"),
                ("3", copy),
            ):
                template_preview = re.sub(
                    r"\{\{\s*" + variable + r"\s*\}\}",
                    lambda _match, replacement=value: replacement,
                    template_preview,
                )
        except ValueError as exc:
            template_preview = f"No se puede mostrar la vista previa: {exc}"
    campaign_sender_phone = str(sender_connection.get("display_phone_number") or "").strip()
    from app import Client, Product
    from sqlalchemy import or_

    campaign_can_reopen_for_recipients = bool(
        campaign.status == "SIN_ENVIO"
        and int(campaign.target_count or 0) == 0
        and int(campaign.sent_count or 0) == 0
        and int(campaign.failed_count or 0) == 0
        and TenantCampaignRecipient.query.filter_by(
            campaign_id=campaign.id,
            company_id=current_user.company_id,
        ).count() == 0
    )
    selected_mode = campaign_system_data.get("audience_selection_mode") == "selected"
    selected_ids = []
    for raw_id in campaign_system_data.get("selected_client_ids") or []:
        try:
            selected_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if selected_id > 0:
            selected_ids.append(selected_id)
    selected_ids = sorted(set(selected_ids))
    selected_page = max(1, request.args.get("selected_page", 1, type=int) or 1)
    selected_page_size = 25
    selected_query = Client.query.filter(
        Client.company_id == current_user.company_id,
        Client.active.is_(True),
    )
    if selected_mode and selected_ids:
        selected_query = selected_query.filter(Client.id.in_(selected_ids))
    else:
        selected_query = selected_query.filter(Client.id < 0)
    campaign_selected_contacts_total = selected_query.count()
    campaign_selected_page_count = max(
        1,
        (campaign_selected_contacts_total + selected_page_size - 1) // selected_page_size,
    )
    selected_page = min(selected_page, campaign_selected_page_count)
    selected_rows = (
        selected_query.order_by(Client.name.asc(), Client.id.asc())
        .offset((selected_page - 1) * selected_page_size)
        .limit(selected_page_size)
        .all()
    )
    selected_contacts = [
        {"client": row, **_campaign_contact_eligibility(row)}
        for row in selected_rows
    ]

    contact_search = str(request.args.get("contact_search") or "").strip()[:100]
    contact_type = str(request.args.get("contact_type") or "all").strip().lower()
    if contact_type not in {"all", "minorista", "mayorista"}:
        contact_type = "all"
    contact_state = str(request.args.get("contact_state") or "all").strip().lower()
    if contact_state not in {"all", "ready", "pending", "opted_out"}:
        contact_state = "all"
    contact_page = max(1, request.args.get("contact_page", 1, type=int) or 1)
    contact_page_size = 25

    available_query = _campaign_contact_selection_query(
        current_user.company_id,
        search=contact_search,
        customer_type=contact_type,
        contact_state=contact_state,
    )
    if selected_mode and selected_ids:
        available_query = available_query.filter(~Client.id.in_(selected_ids))
    campaign_available_total = available_query.count()
    campaign_contact_page_count = max(
        1,
        (campaign_available_total + contact_page_size - 1) // contact_page_size,
    )
    contact_page = min(contact_page, campaign_contact_page_count)
    available_rows = (
        available_query.order_by(Client.name.asc(), Client.id.asc())
        .offset((contact_page - 1) * contact_page_size)
        .limit(contact_page_size)
        .all()
    )
    available_contacts = [
        {"client": row, **_campaign_contact_eligibility(row)}
        for row in available_rows
    ]
    recipient_rows = TenantCampaignRecipient.query.filter_by(
        campaign_id=campaign.id, company_id=current_user.company_id
    ).order_by(TenantCampaignRecipient.id.asc()).limit(100).all()
    campaign_products = (
        Product.query.filter_by(company_id=current_user.company_id, active=True)
        .order_by(Product.name.asc(), Product.id.asc())
        .all()
    )
    return render_template(
        "ai_agents/campaign_detail.html",
        campaign=campaign,
        campaign_products=campaign_products,
        campaign_metrics=campaign_metrics(current_user.company_id, campaign.id),
        detected_audience_count=detected_audience_count,
        excluded_audience_count=excluded_audience_count,
        audience_diagnostics=audience_diagnostics,
        campaign_template=template,
        campaign_template_error=template_error,
        campaign_template_preview=template_preview,
        campaign_recipients=recipient_rows,
        campaign_selected_contacts=selected_contacts,
        campaign_available_contacts=available_contacts,
        campaign_selection_mode=selected_mode,
        campaign_can_reopen_for_recipients=campaign_can_reopen_for_recipients,
        campaign_selected_ids=selected_ids,
        campaign_selected_count=campaign_selected_contacts_total,
        campaign_selected_contacts_total=campaign_selected_contacts_total,
        campaign_selected_page=selected_page,
        campaign_selected_page_count=campaign_selected_page_count,
        campaign_contact_search=contact_search,
        campaign_contact_type=contact_type,
        campaign_contact_state=contact_state,
        campaign_contact_page=contact_page,
        campaign_contact_page_count=campaign_contact_page_count,
        campaign_available_total=campaign_available_total,
        campaign_selected_contacts_truncated=campaign_selected_contacts_total > len(selected_contacts),
        campaign_pricing_quantity=campaign_system_data.get("pricing_quantity") or "",
        campaign_price_exclusions=campaign_system_data.get("pricing_exclusions") or [],
        campaign_price_excluded_count=int(campaign_system_data.get("pricing_excluded_count") or 0),
        campaign_schedule_local=_campaign_schedule_local(campaign),
        campaign_sender_phone=campaign_sender_phone,
        ai_marketing_send_enabled=current_app.config.get("AI_MARKETING_SEND_ENABLED", False),
        **_context(),
    )


def _company_timezone(company=None):
    company = company or _current_active_company()
    timezone_name = str(getattr(company, "timezone", "") or "America/Argentina/Buenos_Aires")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        current_app.logger.warning("Invalid company timezone for campaign scheduling: %s", timezone_name)
        return ZoneInfo("UTC")


def _parse_campaign_schedule(raw_value):
    value = str(raw_value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("La fecha de programación no es válida.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_company_timezone())
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _campaign_schedule_local(campaign):
    if not campaign.scheduled_at:
        return ""
    return campaign.scheduled_at.replace(tzinfo=timezone.utc).astimezone(
        _company_timezone()
    ).strftime("%Y-%m-%dT%H:%M")


def _campaign_audience_count(company_id, *, audience_segment, campaign_type, client_segment, days):
    from app import Client
    from services.ai_agent.tenant_campaign_service import _audience_query, _client_phone

    audience = SimpleNamespace(
        company_id=company_id,
        audience_segment=audience_segment,
        campaign_type=campaign_type,
        system_data_json=json.dumps({"client_segment": client_segment, "days": days}),
    )
    query = _audience_query(audience).filter(
        Client.whatsapp_marketing_consent == "opted_in",
        Client.whatsapp_marketing_consent_recorded_at.isnot(None),
        Client.whatsapp_marketing_consent_source.isnot(None),
        Client.whatsapp_marketing_consent_evidence.isnot(None),
    )
    return sum(
        1 for client in query.yield_per(500)
        if _client_phone(client)
        and client.whatsapp_marketing_consent_source
        and client.whatsapp_marketing_consent_evidence
    )


def _campaign_audience_preview(company_id, audience, *, client_segment="", days=90):
    from app import Client
    from services.ai_agent.tenant_campaign_service import _audience_query, _client_phone

    audience_segment = "clientes inactivos" if audience == "inactive" else "clientes activos"
    campaign_type = "recuperacion_clientes_inactivos" if audience == "inactive" else "general"
    campaign = SimpleNamespace(
        company_id=company_id,
        audience_segment=audience_segment,
        campaign_type=campaign_type,
        system_data_json=json.dumps({"client_segment": client_segment, "days": days}),
    )
    base_query = _audience_query(campaign)
    eligible_query = base_query.filter(
        Client.whatsapp_marketing_consent == "opted_in",
        Client.whatsapp_marketing_consent_recorded_at.isnot(None),
        Client.whatsapp_marketing_consent_source.isnot(None),
        Client.whatsapp_marketing_consent_evidence.isnot(None),
    )
    eligible = sum(
        1 for client in eligible_query.yield_per(500)
        if _client_phone(client)
        and client.whatsapp_marketing_consent_source
        and client.whatsapp_marketing_consent_evidence
    )
    return {"eligible": eligible, "excluded": max(0, base_query.count() - eligible)}


def _campaign_audience_diagnostics(campaign, *, max_contacts=100):
    """Explain why each client in this campaign's audience is or is not sendable."""
    from app import Client
    from services.ai_agent.tenant_campaign_service import _audience_query, _client_phone

    query = _audience_query(campaign)
    reasons = {
        "phone_only": 0,
        "missing_whatsapp": 0,
        "invalid_whatsapp": 0,
        "missing_consent": 0,
        "opted_out": 0,
        "incomplete_consent": 0,
    }
    excluded_contacts = []
    eligible = 0
    excluded = 0
    total = query.count()

    reason_labels = {
        "phone_only": "Tiene teléfono cargado, pero falta completar el campo WhatsApp.",
        "missing_whatsapp": "No tiene un número cargado en el campo WhatsApp.",
        "invalid_whatsapp": "El número del campo WhatsApp no tiene un formato válido.",
        "missing_consent": "Falta registrar la autorización para recibir promociones por WhatsApp.",
        "opted_out": "El cliente indicó que no quiere recibir promociones por WhatsApp.",
        "incomplete_consent": "La autorización existe, pero falta registrar su origen o evidencia.",
    }

    for client in query.yield_per(500):
        issues = []
        consent_status = str(getattr(client, "whatsapp_marketing_consent", "") or "").strip().lower()

        # A recorded opt-out is authoritative: never suggest re-enabling that contact.
        if consent_status == "opted_out":
            issues.append("opted_out")
        else:
            raw_whatsapp = str(getattr(client, "whatsapp", None) or "").strip()
            if not raw_whatsapp:
                if str(getattr(client, "phone", None) or "").strip():
                    issues.append("phone_only")
                else:
                    issues.append("missing_whatsapp")
            elif not _client_phone(client):
                issues.append("invalid_whatsapp")

            if consent_status != "opted_in" or getattr(client, "whatsapp_marketing_consent_recorded_at", None) is None:
                issues.append("missing_consent")
            elif (
                not str(getattr(client, "whatsapp_marketing_consent_source", "") or "").strip()
                or not str(getattr(client, "whatsapp_marketing_consent_evidence", "") or "").strip()
            ):
                issues.append("incomplete_consent")

        if not issues:
            eligible += 1
            continue

        excluded += 1
        for code in issues:
            reasons[code] += 1
        if len(excluded_contacts) < max_contacts:
            excluded_contacts.append({
                "client": client,
                "reasons": [reason_labels[code] for code in issues],
            })

    return {
        "total": total,
        "eligible": eligible,
        "excluded": excluded,
        "reasons": reasons,
        "excluded_contacts": excluded_contacts,
        "truncated": excluded > len(excluded_contacts),
    }


def _campaign_contact_eligibility(client):
    """Return the current WhatsApp/consent state for a selectable campaign contact."""
    from services.ai_agent.tenant_campaign_service import _client_phone

    status = str(getattr(client, "whatsapp_marketing_consent", "unknown") or "unknown").strip().lower()
    if status == "opted_out":
        return {"eligible": False, "status": "Baja solicitada", "reason": "Este contacto pidió no recibir promociones.", "opted_out": True}
    if not str(getattr(client, "whatsapp", None) or "").strip():
        reason = "Falta el número de WhatsApp." if not str(getattr(client, "phone", None) or "").strip() else "Tiene Teléfono, pero falta WhatsApp."
        return {"eligible": False, "status": "Falta WhatsApp", "reason": reason, "opted_out": False}
    if not _client_phone(client):
        return {"eligible": False, "status": "Número inválido", "reason": "El número de WhatsApp no tiene un formato válido.", "opted_out": False}
    if status != "opted_in" or not getattr(client, "whatsapp_marketing_consent_recorded_at", None):
        return {"eligible": False, "status": "Sin autorización", "reason": "Falta registrar el consentimiento expreso para promociones.", "opted_out": False}
    if not str(getattr(client, "whatsapp_marketing_consent_source", "") or "").strip() or not str(getattr(client, "whatsapp_marketing_consent_evidence", "") or "").strip():
        return {"eligible": False, "status": "Evidencia incompleta", "reason": "Falta el origen o la evidencia del consentimiento.", "opted_out": False}
    return {"eligible": True, "status": "Listo para enviar", "reason": "WhatsApp válido y consentimiento documentado.", "opted_out": False}


def _campaign_contact_selection_query(company_id, *, search="", customer_type="all", contact_state="all"):
    """Build a tenant-scoped CRM query for the campaign recipient picker."""
    from app import Client
    from sqlalchemy import func, or_

    query = Client.query.filter(
        Client.company_id == int(company_id),
        Client.active.is_(True),
    )
    search = str(search or "").strip()[:100]
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(
            Client.name.ilike(pattern),
            Client.phone.ilike(pattern),
            Client.whatsapp.ilike(pattern),
        ))

    customer_type = str(customer_type or "all").strip().lower()
    if customer_type in {"minorista", "mayorista"}:
        query = query.filter(Client.customer_type == customer_type)

    contact_state = str(contact_state or "all").strip().lower()
    if contact_state == "opted_out":
        query = query.filter(Client.whatsapp_marketing_consent == "opted_out")
    elif contact_state == "ready":
        query = query.filter(
            Client.whatsapp.isnot(None),
            func.length(func.trim(func.coalesce(Client.whatsapp, ""))) > 0,
            Client.whatsapp_marketing_consent == "opted_in",
            Client.whatsapp_marketing_consent_recorded_at.isnot(None),
            func.length(func.trim(func.coalesce(Client.whatsapp_marketing_consent_source, ""))) > 0,
            func.length(func.trim(func.coalesce(Client.whatsapp_marketing_consent_evidence, ""))) > 0,
        )
    elif contact_state == "pending":
        query = query.filter(
            or_(
                Client.whatsapp_marketing_consent.is_(None),
                Client.whatsapp_marketing_consent != "opted_out",
            ),
            or_(
                Client.whatsapp.is_(None),
                func.length(func.trim(func.coalesce(Client.whatsapp, ""))) == 0,
                Client.whatsapp_marketing_consent != "opted_in",
                Client.whatsapp_marketing_consent_recorded_at.is_(None),
                func.length(func.trim(func.coalesce(Client.whatsapp_marketing_consent_source, ""))) == 0,
                func.length(func.trim(func.coalesce(Client.whatsapp_marketing_consent_evidence, ""))) == 0,
            ),
        )
    return query


@bp.route("/campanas/nueva", methods=["GET", "POST"])
@company_admin_required
def campaign_wizard():
    """Send merchants to Marketing IA; the agent drafts campaign copy and targeting."""
    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye Marketing IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))
    return redirect(url_for("ai_agents.agent", agent="marketing", crear_campana="1"))

@bp.post("/campanas/audiencia-preview")
@company_admin_required
def campaign_audience_preview():
    if not can_use_ai_feature(_current_active_company(), "marketing").allowed:
        return jsonify({"success": False, "error": "Marketing IA requiere IA PRO."}), 403
    payload = request.get_json(silent=True) or {}
    audience = str(payload.get("audience") or "all").strip()
    segment = str(payload.get("client_segment") or "").strip()[:80]
    if audience not in {"all", "inactive", "notes"}:
        return jsonify({"success": False, "error": "Seleccioná una audiencia válida."}), 400
    if audience == "notes" and not segment:
        return jsonify({"success": True, "eligible": 0, "excluded": 0})
    try:
        days = max(1, min(int(payload.get("days") or 90), 365))
        result = _campaign_audience_preview(
            current_user.company_id,
            "all" if audience == "notes" else audience,
            client_segment=segment if audience == "notes" else "",
            days=days,
        )
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "El período de inactividad no es válido."}), 400
    return jsonify({"success": True, **result})


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


@bp.post("/campanas/<int:campaign_id>/corregir-whatsapp")
@company_admin_required
def campaign_correct_whatsapp_numbers(campaign_id):
    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye Marketing IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))

    from services.ai_agent.tenant_campaign_service import correct_campaign_whatsapp_numbers

    try:
        result = correct_campaign_whatsapp_numbers(
            campaign_id,
            company_id=current_user.company_id,
            use_phone_fallback=request.form.get("use_phone_fallback") == "1",
        )
        db.session.commit()
        flash(
            "Corrector finalizado: "
            f"{result['normalized']} número(s) normalizado(s), "
            f"{result['copied_from_phone']} copiado(s) desde Teléfono, "
            f"{result['invalid']} inválido(s), "
            f"{result['no_number']} sin número y "
            f"{result['skipped_opted_out']} baja(s) respetada(s). "
            "No se modificaron permisos promocionales ni se envió ningún mensaje.",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    return redirect(url_for("ai_agents.campaign_detail", campaign_id=campaign_id))


@bp.post("/campanas/<int:campaign_id>/destinatarios")
@company_admin_required
def campaign_update_recipients(campaign_id):
    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye Marketing IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))

    campaign = CampaignService._campaign(current_user.company_id, campaign_id)
    if campaign is None:
        abort(404)
    from services.ai_agent.tenant_campaign_service import TenantCampaignRecipient

    can_reopen_empty_campaign = bool(
        campaign.status == "SIN_ENVIO"
        and int(campaign.target_count or 0) == 0
        and int(campaign.sent_count or 0) == 0
        and int(campaign.failed_count or 0) == 0
        and TenantCampaignRecipient.query.filter_by(
            campaign_id=campaign.id,
            company_id=current_user.company_id,
        ).count() == 0
    )
    if campaign.status not in {"BORRADOR", "PENDIENTE_APROBACION"} and not can_reopen_empty_campaign:
        flash("Los destinatarios solo se pueden modificar antes de aprobar la campaña. Una campaña sin envíos se puede reabrir si no llegó a preparar destinatarios.", "warning")
        return redirect(url_for("ai_agents.campaign_detail", campaign_id=campaign_id))

    if can_reopen_empty_campaign:
        campaign.status = "BORRADOR"
        campaign.started_at = None
        campaign.finished_at = None

    try:
        campaign_data = json.loads(campaign.system_data_json or "{}")
    except (TypeError, ValueError):
        campaign_data = {}
    if not isinstance(campaign_data, dict):
        campaign_data = {}

    action = str(request.form.get("action") or "add").strip().lower()
    current_ids = set()
    for raw_id in campaign_data.get("selected_client_ids") or []:
        try:
            value = int(raw_id)
        except (TypeError, ValueError):
            continue
        if value > 0:
            current_ids.add(value)

    from app import Client
    selected_filter_search = str(request.form.get("contact_search") or "").strip()[:100]
    selected_filter_type = str(request.form.get("contact_type") or "all").strip().lower()
    if selected_filter_type not in {"all", "minorista", "mayorista"}:
        selected_filter_type = "all"
    selected_filter_state = str(request.form.get("contact_state") or "all").strip().lower()
    if selected_filter_state not in {"all", "ready", "pending", "opted_out"}:
        selected_filter_state = "all"
    try:
        selected_return_page = max(1, int(request.form.get("contact_page") or 1))
    except (TypeError, ValueError):
        selected_return_page = 1

    try:
        selected_audience_page = max(1, int(request.form.get("selected_audience_page") or 1))
    except (TypeError, ValueError):
        selected_audience_page = 1

    if action in {"add", "add_all_eligible"}:
        if action == "add":
            requested = set()
            for raw_id in request.form.getlist("client_ids"):
                try:
                    value = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    requested.add(value)
            if not requested:
                flash("Marcá al menos un contacto para agregar.", "warning")
                return redirect(url_for(
                    "ai_agents.campaign_detail",
                    campaign_id=campaign_id,
                    contact_search=selected_filter_search,
                    contact_type=selected_filter_type,
                    contact_state=selected_filter_state,
                    contact_page=selected_return_page,
        selected_page=selected_audience_page,
                ))
            rows = Client.query.filter(
                Client.company_id == current_user.company_id,
                Client.active.is_(True),
                Client.id.in_(requested),
            ).all()
        else:
            # Bulk selection applies to every eligible match, not merely the
            # 25 contacts currently visible on screen.
            rows = _campaign_contact_selection_query(
                current_user.company_id,
                search=selected_filter_search,
                customer_type=selected_filter_type,
                contact_state="ready",
            ).order_by(Client.id.asc()).yield_per(500)

        skipped_opt_out = 0
        previous_count = len(current_ids)
        for row in rows:
            eligibility = _campaign_contact_eligibility(row)
            if eligibility.get("opted_out"):
                skipped_opt_out += 1
                continue
            if action == "add_all_eligible" and not eligibility.get("eligible"):
                continue
            current_ids.add(int(row.id))

        added_count = len(current_ids) - previous_count
        campaign_data["audience_selection_mode"] = "selected"
        campaign_data["selected_client_ids"] = sorted(current_ids)
        campaign.system_data_json = json.dumps(campaign_data, ensure_ascii=False)
        db.session.commit()
        if action == "add_all_eligible":
            flash(
                f"Se agregaron {added_count} contacto(s) habilitado(s) que coinciden con los filtros. "
                "La selección queda guardada aunque cambies de página o búsqueda.",
                "success" if added_count else "warning",
            )
        else:
            flash(
                f"Se agregaron {added_count} contacto(s). {skipped_opt_out} baja(s) respetada(s). "
                "Los contactos pendientes se pueden corregir, pero no se enviarán sin WhatsApp válido y autorización documentada.",
                "success",
            )
    elif action == "remove":
        try:
            remove_id = int(request.form.get("client_id") or 0)
        except (TypeError, ValueError):
            remove_id = 0
        current_ids.discard(remove_id)
        campaign_data["audience_selection_mode"] = "selected"
        campaign_data["selected_client_ids"] = sorted(current_ids)
        campaign.system_data_json = json.dumps(campaign_data, ensure_ascii=False)
        db.session.commit()
        flash("Destinatario quitado de la campaña.", "success")
    elif action == "clear":
        campaign_data["audience_selection_mode"] = "segment"
        campaign_data["selected_client_ids"] = []
        campaign.system_data_json = json.dumps(campaign_data, ensure_ascii=False)
        db.session.commit()
        flash("La campaña volvió a usar el público general del segmento.", "success")
    else:
        flash("Acción de destinatarios no válida.", "warning")
    return redirect(url_for(
        "ai_agents.campaign_detail",
        campaign_id=campaign_id,
        contact_search=selected_filter_search,
        contact_type=selected_filter_type,
        contact_state=selected_filter_state,
        contact_page=selected_return_page,
        selected_page=selected_audience_page,
    ))

@bp.post("/campanas/<int:campaign_id>/edit")
@company_admin_required
def campaign_edit(campaign_id):
    """Save campaign copy, schedule and per-recipient pricing settings."""
    from decimal import Decimal, InvalidOperation

    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye Marketing IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))

    try:
        campaign = CampaignService.update_draft(
            company_id=current_user.company_id,
            campaign_id=campaign_id,
            title=request.form.get("title", ""),
            objective=request.form.get("objective", ""),
            content=request.form.get("content", ""),
            user_id=current_user.id,
        )
        try:
            campaign_data = json.loads(campaign.system_data_json or "{}")
        except (TypeError, ValueError):
            campaign_data = {}
        if not isinstance(campaign_data, dict):
            campaign_data = {}

        # Let the merchant attach the actual catalog product to an existing
        # draft. Price variables are resolved against this product and each
        # recipient's own retail/wholesale tariff at preflight time.
        if "product_id" in request.form:
            raw_product_id = str(request.form.get("product_id") or "").strip()
            if not raw_product_id:
                campaign.product_id = None
            else:
                try:
                    selected_product_id = int(raw_product_id)
                except (TypeError, ValueError):
                    raise ValueError("Seleccioná un producto válido del catálogo.")
                from app import Product

                selected_product = Product.query.filter_by(
                    id=selected_product_id,
                    company_id=current_user.company_id,
                    active=True,
                ).first()
                if selected_product is None:
                    raise ValueError("El producto seleccionado no pertenece a este comercio o está inactivo.")
                campaign.product_id = selected_product.id

        raw_quantity = str(request.form.get("pricing_quantity") or "").strip()
        if raw_quantity:
            try:
                quantity = Decimal(raw_quantity)
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError("La cantidad de referencia debe ser un número mayor que cero.")
            if not quantity.is_finite() or quantity <= 0:
                raise ValueError("La cantidad de referencia debe ser un número mayor que cero.")
            campaign_data["pricing_quantity"] = format(quantity, "f")
        else:
            campaign_data.pop("pricing_quantity", None)

        campaign.channel = "whatsapp"
        campaign_data["channel"] = "whatsapp"
        campaign.system_data_json = json.dumps(campaign_data, ensure_ascii=False)

        raw_schedule = str(
            request.form.get("scheduled_at_utc") or request.form.get("scheduled_at") or ""
        ).strip()
        campaign.scheduled_at = _parse_campaign_schedule(raw_schedule) if raw_schedule else None

        db.session.commit()
        flash("Campaña actualizada. No se envió ningún mensaje.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    return redirect(url_for("ai_agents.campaign_detail", campaign_id=campaign_id))


@bp.post("/campanas/<int:campaign_id>/delete")
@company_admin_required
def campaign_delete(campaign_id):
    """Delete an unsent campaign without erasing delivery or attribution history."""
    try:
        deleted = CampaignService.delete_unsent(
            company_id=current_user.company_id,
            campaign_id=campaign_id,
        )
        from app import record_audit

        record_audit(
            action="tenant_campaign_deleted",
            entity="Campaign",
            entity_id=campaign_id,
            detail=(
                f"Campaña eliminada: {deleted['title']}. "
                f"Destinatarios pendientes eliminados: {deleted['recipient_count']}."
            ),
            user_id=current_user.id,
            company_id=current_user.company_id,
        )
        db.session.commit()
        flash(
            f"Se eliminó la campaña “{deleted['title']}” y "
            f"{deleted['recipient_count']} destinatario(s) asociado(s).",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    return redirect(url_for("ai_agents.campaigns"))


@bp.post("/campanas/<int:campaign_id>/transition")
@company_admin_required
def campaign_transition(campaign_id):
    entitlement = can_use_ai_feature(_current_active_company(), "marketing")
    if not entitlement.allowed:
        flash(entitlement.reason or "Tu plan no incluye Marketing IA.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))
    target_status = request.form.get("status", "")
    try:
        if target_status.strip().upper() == "APROBADA" and not current_app.config.get("AI_MARKETING_SEND_ENABLED", False):
            raise ValueError("El worker de Marketing IA está deshabilitado; la campaña no puede aprobarse para envío.")
        campaign = CampaignService._campaign(current_user.company_id, int(campaign_id))
        if campaign is None:
            raise ValueError("Campaña no encontrada para esta empresa.")
        campaign.channel = "whatsapp"
        try:
            campaign_data = json.loads(campaign.system_data_json or "{}")
        except (TypeError, ValueError):
            campaign_data = {}
        if not isinstance(campaign_data, dict):
            campaign_data = {}
        campaign_data["channel"] = "whatsapp"
        campaign.system_data_json = json.dumps(campaign_data, ensure_ascii=False)
        campaign = CampaignService.transition(company_id=current_user.company_id, campaign_id=campaign_id, target_status=target_status, user_id=current_user.id)
        if target_status.upper() == "APROBADA":
            preparation = prepare_recipients(campaign.id, company_id=current_user.company_id)
            prepared_count = int(preparation.get("target_count") or 0)
            if prepared_count < 1:
                # Keep the per-recipient preflight diagnostics visible in the
                # draft; do not mark the campaign as approved or enqueue it.
                campaign.status = "PENDIENTE_APROBACION"
                campaign.started_at = None
                campaign.finished_at = None
                db.session.commit()
                flash(
                    "La campaña no se encoló porque no hay destinatarios listos. "
                    "Revisá los contactos, el consentimiento y las tarifas mayoristas. "
                    "No se enviará nada con el público en cero.",
                    "warning",
                )
                return redirect(url_for("ai_agents.campaign_detail", campaign_id=campaign_id))
            CampaignService.transition(company_id=current_user.company_id, campaign_id=campaign.id, target_status="EN_PREPARACION", user_id=current_user.id)
            db.session.commit()
            excluded_by_price = int(preparation.get("pricing_excluded") or 0)
            flash(
                f"Campaña aprobada: se encolaron {prepared_count} destinatario(s). "
                f"{excluded_by_price} contacto(s) se excluyeron por no superar la validación individual de mensaje/precio. "
                "El envío comenzará automáticamente desde el número de WhatsApp que este comercio conectó mediante Meta, respetando el consentimiento y las bajas.",
                "success",
            )
        else:
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

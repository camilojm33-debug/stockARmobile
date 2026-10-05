"""SaaS y billing: planes, suscripciones, checkout y webhooks Mercado Pago."""

from __future__ import annotations

import json
import os
import secrets
import hmac
import string
from html import escape
from datetime import datetime, timedelta, timezone
from io import BytesIO
from math import ceil
from time import monotonic
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

try:
    import redis
except Exception:  # pragma: no cover - optional dependency fallback
    redis = None

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask import send_file
from flask_login import current_user, login_required
from openpyxl import Workbook
from sqlalchemy import text
from werkzeug.security import check_password_hash

from app import model_table_exists, superadmin_required, utcnow
from stockarmobile.extensions import csrf
from config.billing_config import load_billing_config
from services.backup_service import BackupService
from services.payment_flow import (
    FLOW_AI_SUBSCRIPTION,
    FLOW_STANDARD,
    ai_subscription_payment_filter,
    payment_flow,
    payment_flow_label,
    standard_subscription_payment_filter,
    subscription_revenue_payment_filter,
)
from services.plan_service import PlanService

bp = Blueprint("saas", __name__)

SUBSCRIPTION_STATUS_OPTIONS = [
    "draft",
    "pending",
    "pending_payment",
    "pending_confirmation",
    "trial",
    "trial_expired",
    "active",
    "scheduled",
    "expired",
    "cancelled",
    "suspended",
]

# Acciones de UI permitidas por estado para evitar botones invalidos.
SUBSCRIPTION_UI_ACTIONS = {
    "active": {"modify", "suspend", "cancel"},
    "scheduled": {"modify", "suspend", "cancel"},
    "trial": {"modify", "suspend", "cancel"},
    "pending": {"modify", "suspend", "cancel"},
    "pending_payment": {"modify", "cancel"},
    "pending_confirmation": {"modify", "cancel"},
    "suspended": {"reactivate"},
    "expired": {"renew_now"},
    "cancelled": {"reactivate", "renew_now"},
    "trial_expired": {"renew_now"},
}

CRM_LEAD_STATUSES = {"nuevo", "contactado", "propuesta", "ganado", "perdido"}
CRM_TASK_STATUSES = {"pendiente", "en_progreso", "bloqueada", "hecha"}
CRM_ALERT_STATUSES = {"abierta", "revisada", "resuelta"}
CRM_PRIORITIES = {"baja", "media", "alta"}

_SAAS_CACHE: dict[str, dict[str, object]] = {}
_ADMIN_TZ_NAME = "America/Argentina/Buenos_Aires"


class _SimplePagination:
    def __init__(self, *, page: int, per_page: int, total: int):
        self.page = max(1, int(page or 1))
        self.per_page = max(1, int(per_page or 1))
        self.total = max(0, int(total or 0))
        self.pages = max(1, int(ceil(self.total / self.per_page))) if self.total else 1

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def prev_num(self) -> int:
        return max(1, self.page - 1)

    @property
    def next_num(self) -> int:
        return min(self.pages, self.page + 1)


def _admin_timezone():
    try:
        return ZoneInfo(_ADMIN_TZ_NAME)
    except Exception:
        return timezone(timedelta(hours=-3))


def _parse_admin_datetime_local(value: str | None):
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_admin_timezone())
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _format_admin_datetime_local(value, fmt: str):
    if value is None:
        return ""
    aware_utc = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware_utc.astimezone(_admin_timezone()).strftime(fmt)


def _temporary_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _require_superadmin():
    if current_user.role != "superadmin":
        abort(403)


SUPERADMIN_STEP_UP_TTL_SECONDS = 600
_SUPERADMIN_STEP_UP_AT_SESSION_KEY = "superadmin_step_up_at"
_SUPERADMIN_STEP_UP_USER_SESSION_KEY = "superadmin_step_up_user_id"
_SUPERADMIN_AUTHENTICATED_AT_SESSION_KEY = "superadmin_authenticated_at"


def _superadmin_step_up_is_valid() -> bool:
    if not getattr(current_user, "is_authenticated", False) or current_user.role != "superadmin":
        return False

    now_ts = utcnow().timestamp()
    try:
        authenticated_at = float(session.get(_SUPERADMIN_AUTHENTICATED_AT_SESSION_KEY) or 0)
    except (TypeError, ValueError):
        authenticated_at = 0
    if authenticated_at and 0 <= (now_ts - authenticated_at) <= SUPERADMIN_STEP_UP_TTL_SECONDS:
        return True

    if session.get(_SUPERADMIN_STEP_UP_USER_SESSION_KEY) != current_user.id:
        return False
    try:
        verified_at = float(session.get(_SUPERADMIN_STEP_UP_AT_SESSION_KEY) or 0)
    except (TypeError, ValueError):
        return False
    return 0 <= (now_ts - verified_at) <= SUPERADMIN_STEP_UP_TTL_SECONDS


def _require_superadmin_step_up() -> bool:
    """Require a recent password re-authentication for destructive Super Admin actions."""
    _require_superadmin()
    if _superadmin_step_up_is_valid():
        return True

    password = request.form.get("step_up_password") or ""
    if not password:
        flash(
            "Esta acción requiere una reautenticación de Super Admin. Ingresá nuevamente tu contraseña.",
            "warning",
        )
        return False

    try:
        password_valid = bool(password and current_user.check_password(password))
    except (AttributeError, ValueError, TypeError):
        password_valid = False
    if not password_valid:
        from app import record_audit

        record_audit(
            action="superadmin_step_up_failed",
            entity="superadmin_session",
            detail="Reautenticación fallida para acción administrativa sensible.",
            user_id=current_user.id,
            company_id=None,
            ip_address=request.remote_addr,
        )
        from app import db

        db.session.commit()
        flash("La contraseña de Super Admin no es correcta.", "danger")
        return False

    session[_SUPERADMIN_STEP_UP_USER_SESSION_KEY] = current_user.id
    session[_SUPERADMIN_STEP_UP_AT_SESSION_KEY] = utcnow().timestamp()

    from app import record_audit, db

    record_audit(
        action="superadmin_step_up_success",
        entity="superadmin_session",
        detail=f"Reautenticación exitosa. Vigencia={SUPERADMIN_STEP_UP_TTL_SECONDS}s.",
        user_id=current_user.id,
        company_id=None,
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return True


def _redirect_back(default_endpoint: str = "saas.companies_panel"):
    next_url = (request.form.get("next") or request.args.get("next") or "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for(default_endpoint))


def _parse_dt(value: str | None):
    return _parse_admin_datetime_local(value)


def _normalized_subscription_status(value: str | None) -> str:
    status = (value or "pending").strip().lower()
    legacy_map = {
        "approved": "active",
        "activa": "active",
        "rejected": "expired",
        "in_process": "pending_payment",
        "authorized": "pending_confirmation",
    }
    normalized = legacy_map.get(status, status)
    return normalized if normalized in SUBSCRIPTION_STATUS_OPTIONS else "pending"


def _allowed_ui_actions_for_status(status: str | None):
    normalized = _normalized_subscription_status(status)
    return SUBSCRIPTION_UI_ACTIONS.get(normalized, {"modify"})


def _hard_delete_company(company):
    import sqlalchemy as sa

    from app import (
        AuditLog,
        BackupLog,
        CashMovement,
        CashSession,
        Client,
        Expense,
        Invoice,
        MercadoPagoConnection,
        NotificationReadState,
        Payment,
        PaymentHistory,
        PasswordRecoveryRequest,
        PasswordResetToken,
        Product,
        ProductModification,
        ProductPriceHistory,
        PurchaseItem,
        PurchaseOrder,
        Quote,
        QuoteItem,
        ReferralAttribution,
        ReferralCommission,
        ReferralPayout,
        ReferralPayoutItem,
        ReferralSeller,
        SaaSAlert,
        SaaSLead,
        SaaSTask,
        Sale,
        SaleItem,
        SaleModificationHistory,
        Subscription,
        Supplier,
        SupportTicket,
        User,
        db,
    )

    company_id = company.id
    inspector = sa.inspect(db.session.get_bind())
    table_names = set(inspector.get_table_names())
    columns_cache = {}

    def _has_table(model):
        table = getattr(model, "__tablename__", "")
        return bool(table) and table in table_names and model_table_exists(model)

    def _has_column(model, column_name):
        if not _has_table(model):
            return False
        table = model.__tablename__
        if table not in columns_cache:
            columns_cache[table] = {column.get("name") for column in inspector.get_columns(table)}
        return column_name in columns_cache[table]

    def _safe_ids_by_company(model):
        if not (_has_column(model, "id") and _has_column(model, "company_id")):
            return []
        return [row[0] for row in db.session.query(model.id).filter(model.company_id == company_id).all()]

    def _safe_delete_company_rows(model):
        if _has_column(model, "company_id"):
            db.session.query(model).filter(model.company_id == company_id).delete(synchronize_session=False)

    def _safe_delete_in(model, column_name, values):
        if not values or not _has_column(model, column_name):
            return
        db.session.query(model).filter(getattr(model, column_name).in_(values)).delete(synchronize_session=False)

    user_ids = _safe_ids_by_company(User)
    product_ids = _safe_ids_by_company(Product)
    client_ids = _safe_ids_by_company(Client)
    supplier_ids = _safe_ids_by_company(Supplier)
    quote_ids = _safe_ids_by_company(Quote)
    sale_ids = _safe_ids_by_company(Sale)
    purchase_order_ids = _safe_ids_by_company(PurchaseOrder)

    seller_ids = []
    if user_ids and _has_column(ReferralSeller, "id") and _has_column(ReferralSeller, "user_id"):
        seller_ids = [row[0] for row in db.session.query(ReferralSeller.id).filter(ReferralSeller.user_id.in_(user_ids)).all()]

    payout_ids = []
    if seller_ids and _has_column(ReferralPayout, "id") and _has_column(ReferralPayout, "seller_id"):
        payout_ids = [row[0] for row in db.session.query(ReferralPayout.id).filter(ReferralPayout.seller_id.in_(seller_ids)).all()]

    _safe_delete_in(ReferralPayoutItem, "payout_id", payout_ids)
    _safe_delete_in(SaleItem, "sale_id", sale_ids)
    _safe_delete_in(SaleItem, "product_id", product_ids)
    _safe_delete_in(QuoteItem, "quote_id", quote_ids)
    _safe_delete_in(QuoteItem, "product_id", product_ids)
    _safe_delete_in(PurchaseItem, "purchase_order_id", purchase_order_ids)
    _safe_delete_in(PurchaseItem, "product_id", product_ids)

    _safe_delete_company_rows(SaleModificationHistory)
    _safe_delete_company_rows(AuditLog)
    _safe_delete_company_rows(CashMovement)
    _safe_delete_company_rows(PaymentHistory)
    _safe_delete_company_rows(ProductModification)
    _safe_delete_company_rows(ProductPriceHistory)
    _safe_delete_company_rows(BackupLog)
    _safe_delete_company_rows(Expense)
    _safe_delete_company_rows(SupportTicket)
    _safe_delete_company_rows(PasswordRecoveryRequest)
    _safe_delete_company_rows(SaaSAlert)
    _safe_delete_company_rows(SaaSTask)
    _safe_delete_company_rows(SaaSLead)
    _safe_delete_company_rows(ReferralCommission)
    _safe_delete_company_rows(ReferralAttribution)

    _safe_delete_in(ReferralPayout, "id", payout_ids)
    _safe_delete_in(ReferralSeller, "id", seller_ids)

    _safe_delete_company_rows(Payment)
    _safe_delete_company_rows(Invoice)
    _safe_delete_company_rows(Subscription)
    _safe_delete_company_rows(MercadoPagoConnection)
    _safe_delete_company_rows(CashSession)
    _safe_delete_company_rows(Sale)
    _safe_delete_company_rows(Quote)
    _safe_delete_company_rows(PurchaseOrder)
    _safe_delete_company_rows(Product)
    _safe_delete_company_rows(Client)
    _safe_delete_company_rows(Supplier)

    _safe_delete_in(NotificationReadState, "user_id", user_ids)
    _safe_delete_in(PasswordResetToken, "user_id", user_ids)
    _safe_delete_in(User, "id", user_ids)

    def _raw_delete_company_rows(table_name):
        if table_name not in table_names:
            return
        if table_name not in columns_cache:
            columns_cache[table_name] = {column.get("name") for column in inspector.get_columns(table_name)}
        if "company_id" not in columns_cache[table_name]:
            return
        db.session.execute(sa.text(f"DELETE FROM {table_name} WHERE company_id = :company_id"), {"company_id": company_id})

    # Defensive second pass to avoid leftovers when ORM bulk-delete skips rows due mapper/session edge cases.
    for table_name in [
        "sale_modification_history",
        "audit_logs",
        "cash_movements",
        "payment_history",
        "product_modifications",
        "product_price_history",
        "backup_logs",
        "expenses",
        "support_tickets",
        "password_recovery_requests",
        "saas_alerts",
        "saas_tasks",
        "saas_leads",
        "referral_commissions",
        "referral_attributions",
        "payments",
        "invoices",
        "subscriptions",
        "mercadopago_connections",
        "cash_sessions",
        "sales",
        "quotes",
        "purchase_orders",
        "products",
        "clients",
        "suppliers",
        "users",
    ]:
        _raw_delete_company_rows(table_name)

    def _raw_delete_where_in(table_name, column_name, values):
        if not values or table_name not in table_names:
            return
        if table_name not in columns_cache:
            columns_cache[table_name] = {column.get("name") for column in inspector.get_columns(table_name)}
        if column_name not in columns_cache[table_name]:
            return
        statement = sa.text(f"DELETE FROM {table_name} WHERE {column_name} IN :ids").bindparams(sa.bindparam("ids", expanding=True))
        db.session.execute(statement, {"ids": list(values)})

    # Final defensive pass for legacy/optional tables that may reference tenant entities without company_id.
    fk_value_sets = {
        "users": set(user_ids),
        "products": set(product_ids),
        "clients": set(client_ids),
        "suppliers": set(supplier_ids),
        "sales": set(sale_ids),
        "quotes": set(quote_ids),
        "purchase_orders": set(purchase_order_ids),
        "referral_sellers": set(seller_ids),
        "referral_payouts": set(payout_ids),
    }
    for table_name in table_names:
        if table_name == "companies":
            continue
        foreign_keys = inspector.get_foreign_keys(table_name) or []
        for fk in foreign_keys:
            referred_table = fk.get("referred_table")
            constrained_cols = fk.get("constrained_columns") or []
            values = fk_value_sets.get(referred_table)
            if not values or len(constrained_cols) != 1:
                continue
            _raw_delete_where_in(table_name, constrained_cols[0], values)

    # Delete the tenant only after all known dependent rows and FK-referenced
    # entity rows have been removed. Keep this inside the same transaction so
    # any remaining FK causes a full rollback instead of partial deletion.
    _delete_company_record(db.session, company_id)

    # Fail loudly if the target tenant still exists. The caller rolls back.
    remaining = db.session.execute(
        sa.text("SELECT 1 FROM companies WHERE id = :company_id LIMIT 1"),
        {"company_id": company_id},
    ).first()
    if remaining is not None:
        raise RuntimeError(f"No se pudo eliminar companies.id={company_id}")


def _action_allowed_for_status(status: str | None, action: str) -> bool:
    if action == "extend":
        return True
    return action in _allowed_ui_actions_for_status(status)


def _delete_company_record(db_session, company_id):
    import sqlalchemy as sa

    db_session.execute(
        sa.text("DELETE FROM companies WHERE id = :company_id"),
        {"company_id": company_id},
    )


def _format_size(size_bytes):
    value = float(size_bytes or 0)
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


def _safe_pct(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return round((numerator / denominator) * 100.0, 2)


def _cached_value(cache_key: str, ttl_seconds: int, builder):
    now_tick = monotonic()
    cached = _SAAS_CACHE.get(cache_key)
    if cached and (now_tick - float(cached.get("at", 0))) <= ttl_seconds:
        return cached.get("value")
    value = builder()
    _SAAS_CACHE[cache_key] = {"at": now_tick, "value": value}
    return value


def _trend_payload(current: float, previous: float, *, higher_is_better: bool = True):
    delta = float(current or 0) - float(previous or 0)
    base = float(previous or 0)
    pct = 0.0 if base == 0 else round((delta / base) * 100.0, 2)
    if delta > 0:
        direction = "up"
    elif delta < 0:
        direction = "down"
    else:
        direction = "flat"

    positive = (delta >= 0) if higher_is_better else (delta <= 0)
    if direction == "flat":
        color = "secondary"
        arrow = "→"
    else:
        color = "success" if positive else "danger"
        arrow = "↑" if direction == "up" else "↓"

    return {
        "delta": round(delta, 2),
        "pct": pct,
        "direction": direction,
        "color": color,
        "arrow": arrow,
    }


def _health_action_for_check(check_key: str):
    action_map = {
        "smtp": {"label": "Configurar", "url": url_for("saas.global_settings")},
        "backups": {"label": "Crear backup", "url": url_for("saas.backups_panel")},
        "redis": {"label": "Configurar", "url": url_for("saas.global_settings")},
        "mercado_pago": {"label": "Conexiones", "url": url_for("saas.mercado_pago_connections")},
        "db": {"label": "Estado servidor", "url": url_for("saas.server_status")},
        "cron": {"label": "Renovaciones", "url": url_for("saas.renewals_panel")},
        "ssl": {"label": "Configuración", "url": url_for("saas.global_settings")},
        "domain": {"label": "Configuración", "url": url_for("saas.global_settings")},
        "storage": {"label": "Backups", "url": url_for("saas.backups_panel")},
        "storage_usage": {"label": "Backups", "url": url_for("saas.backups_panel")},
        "service_worker": {"label": "Estado servidor", "url": url_for("saas.server_status")},
        "latency": {"label": "Estado servidor", "url": url_for("saas.server_status")},
    }
    return action_map.get(check_key, {"label": "Ver detalle", "url": url_for("saas.logs_panel")})


def _attention_meta(reason: str):
    key = (reason or "").strip().lower()
    mapping = {
        "pago pendiente": {"icon": "💳", "action_label": "Cobrar", "action_route": "saas.billing"},
        "pago rechazado": {"icon": "💳", "action_label": "Cobrar", "action_route": "saas.billing"},
        "prueba vence en 3 días": {"icon": "⏳", "action_label": "Renovar", "action_route": "saas.subscriptions_panel"},
        "empresa sin backup válido": {"icon": "☁", "action_label": "Crear backup", "action_route": "saas.backups_panel"},
        "mercado pago desconectado": {"icon": "⚠", "action_label": "Conectar", "action_route": "saas.mercado_pago_connections"},
        "empresa sin actividad": {"icon": "📦", "action_label": "Ver empresa", "action_route": "saas.companies_panel"},
        "empresa sin usuarios activos": {"icon": "👤", "action_label": "Ver empresa", "action_route": "saas.companies_panel"},
        "empresa bloqueada": {"icon": "🔒", "action_label": "Ver empresa", "action_route": "saas.companies_panel"},
    }
    return mapping.get(key, {"icon": "⚠", "action_label": "Ver empresa", "action_route": "saas.companies_panel"})


def _timeline_result(detail: str | None):
    text = (detail or "").lower()
    if any(token in text for token in ["error", "fall", "rechaz", "fail", "denied"]):
        return {"label": "Error", "color": "danger"}
    return {"label": "OK", "color": "success"}


def _service_status(ok: bool, warning: bool = False, detail: str | None = None):
    if ok and not warning:
        return {"status": "ok", "label": "OK", "color": "success", "detail": detail or "Operativo"}
    if ok and warning:
        return {"status": "warning", "label": "Advertencia", "color": "warning", "detail": detail or "Requiere revisión"}
    return {"status": "error", "label": "Error", "color": "danger", "detail": detail or "No disponible"}


def _redis_service_status():
    redis_url = (os.environ.get("REDIS_URL") or "").strip()
    if not redis_url:
        return _service_status(True, True, "No configurado (faltante REDIS_URL)")

    if redis is None:
        return _service_status(False, False, "Paquete redis no instalado")

    try:
        client = redis.Redis.from_url(redis_url, socket_connect_timeout=1.5, socket_timeout=1.5, decode_responses=True)
        if client.ping():
            return _service_status(True, False, "Conectado")
        return _service_status(False, False, "Sin respuesta de Redis")
    except Exception as exc:
        return _service_status(False, False, f"No conecta: {exc.__class__.__name__}")


def _smtp_service_status(*, host: str | None, user: str | None, password: str | None):
    """Evalúa SMTP real; SUPPORT_EMAIL por sí solo no habilita el envío."""
    configured = all(str(value or "").strip() for value in (host, user, password))
    partial = any(str(value or "").strip() for value in (host, user, password)) and not configured
    if configured:
        return _service_status(True, False, "SMTP configurado")
    if partial:
        return _service_status(False, False, "Configuración SMTP incompleta")
    return _service_status(True, True, "SMTP no configurado (opcional)")


def _backup_health_status(total: int, failed: int):
    """No marca rojo un sistema mensual simplemente porque aún no ejecutó su primer ciclo."""
    if failed > 0:
        return _service_status(False, False, f"{total} backups / {failed} con error")
    if total > 0:
        return _service_status(True, False, f"{total} backups / 0 con error")
    return _service_status(True, True, "Sin backups registrados todavía; política mensual activa")


def _health_check_snapshot(db_session, now):
    from app import BackupLog, Company, MercadoPagoConnection, Subscription, User, WebhookEvent, db, model_table_exists

    db_ok = True
    db_detail = "Conectada"
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_detail = str(exc)

    smtp_status = _smtp_service_status(
        host=current_app.config.get("SMTP_HOST"),
        user=current_app.config.get("SMTP_USER"),
        password=current_app.config.get("SMTP_PASSWORD"),
    )

    mp_connected = 0
    mp_total = 0
    if model_table_exists(MercadoPagoConnection):
        mp_total = MercadoPagoConnection.query.count()
        mp_connected = MercadoPagoConnection.query.filter(MercadoPagoConnection.status == "connected").count()

    backup_total = BackupLog.query.count() if model_table_exists(BackupLog) else 0
    backup_failed = BackupLog.query.filter(BackupLog.status == "error").count() if model_table_exists(BackupLog) else 0

    active_companies = Company.query.filter(Company.active.is_(True)).count()
    active_users = User.query.filter(User.active.is_(True)).count()
    subscriptions_renewing = Subscription.query.filter(Subscription.renewal_enabled.is_(True)).count()

    last_webhook = WebhookEvent.query.order_by(WebhookEvent.created_at.desc()).first() if model_table_exists(WebhookEvent) else None
    webhook_recent_ok = bool(last_webhook and (now - last_webhook.created_at).days <= 7)

    redis_status = _redis_service_status()

    checks = [
        {
            "name": "Base de datos",
            "key": "db",
            "data": _service_status(db_ok, False, db_detail),
        },
        {
            "name": "Redis",
            "key": "redis",
            "data": redis_status,
        },
        {
            "name": "Mercado Pago",
            "key": "mercado_pago",
            "data": _service_status(mp_connected > 0 or mp_total == 0, mp_total > 0 and mp_connected < mp_total, f"{mp_connected}/{mp_total} conexiones activas"),
        },
        {
            "name": "Correo SMTP",
            "key": "smtp",
            "data": smtp_status,
        },
        {
            "name": "Backups",
            "key": "backups",
            "data": _backup_health_status(backup_total, backup_failed),
        },
        {
            "name": "Storage",
            "key": "storage",
            "data": _service_status(True, False, "Sin métricas de cuota integradas"),
        },
        {
            "name": "Cron Jobs",
            "key": "cron",
            "data": _service_status(subscriptions_renewing > 0, subscriptions_renewing == 0, f"{subscriptions_renewing} suscripciones con renovación habilitada"),
        },
        {
            "name": "Service Worker",
            "key": "service_worker",
            "data": _service_status(True, False, "Offline-first habilitado"),
        },
        {
            "name": "SSL",
            "key": "ssl",
            "data": _service_status(bool(os.environ.get("APP_URL", "").startswith("https://")), not bool(os.environ.get("APP_URL", "").startswith("https://")), os.environ.get("APP_URL") or "APP_URL sin definir"),
        },
        {
            "name": "Dominio",
            "key": "domain",
            "data": _service_status(bool(os.environ.get("APP_URL")), not bool(os.environ.get("APP_URL")), os.environ.get("APP_URL") or "Sin dominio configurado"),
        },
        {
            "name": "Espacio utilizado",
            "key": "storage_usage",
            "data": _service_status(True, True, "Métrica no instrumentada"),
        },
        {
            "name": "Tiempo de respuesta",
            "key": "latency",
            "data": _service_status(db_ok, not db_ok, "Check de DB en línea"),
        },
    ]

    for check in checks:
        check["action"] = _health_action_for_check(check.get("key", ""))

    return {
        "checks": checks,
        "summary": {
            "ok": sum(1 for item in checks if item["data"]["status"] == "ok"),
            "warning": sum(1 for item in checks if item["data"]["status"] == "warning"),
            "error": sum(1 for item in checks if item["data"]["status"] == "error"),
            "active_companies": active_companies,
            "active_users": active_users,
            "webhook_recent_ok": webhook_recent_ok,
        },
    }


def _build_attention_queue(now):
    from app import BackupLog, Company, MercadoPagoConnection, Payment, Sale, Subscription, User, db, model_table_exists

    queue = []

    trial_cutoff = now + timedelta(days=3)
    trials_ending = (
        Subscription.query.join(Company, Company.id == Subscription.company_id)
        .filter(Subscription.status == "trial", Subscription.next_billing_date.isnot(None), Subscription.next_billing_date <= trial_cutoff)
        .order_by(Subscription.next_billing_date.asc())
        .limit(15)
        .all()
    )
    for sub in trials_ending:
        queue.append({
            "company_id": sub.company_id,
            "company_name": sub.company.name if sub.company else f"Empresa #{sub.company_id}",
            "reason": "Prueba vence en 3 días",
            "severity": "warning",
            "detail": f"Vence: {sub.next_billing_date.strftime('%Y-%m-%d') if sub.next_billing_date else '-'}",
        })

    rejected_payments = (
        Payment.query.join(Company, Company.id == Payment.company_id)
        .filter(subscription_revenue_payment_filter(Payment), Payment.status.in_(["rejected", "charged_back", "cancelled"]), Payment.created_at >= now - timedelta(days=15))
        .order_by(Payment.created_at.desc())
        .limit(20)
        .all()
    )
    for payment in rejected_payments:
        queue.append({
            "company_id": payment.company_id,
            "company_name": payment.company.name if payment.company else f"Empresa #{payment.company_id}",
            "reason": f"Pago rechazado ({payment_flow_label(payment)})",
            "severity": "danger",
            "detail": f"{payment.status} · ${float(payment.amount or 0):.2f}",
        })

    pending_payments = (
        Payment.query.join(Company, Company.id == Payment.company_id)
        .filter(subscription_revenue_payment_filter(Payment), Payment.status.in_(["pending", "authorized", "in_process"]), Payment.created_at >= now - timedelta(days=10))
        .order_by(Payment.created_at.desc())
        .limit(20)
        .all()
    )
    for payment in pending_payments:
        queue.append({
            "company_id": payment.company_id,
            "company_name": payment.company.name if payment.company else f"Empresa #{payment.company_id}",
            "reason": f"Pago pendiente ({payment_flow_label(payment)})",
            "severity": "warning",
            "detail": f"{payment.status} · ${float(payment.amount or 0):.2f}",
        })

    stale_companies = (
        db.session.query(Company)
        .outerjoin(Sale, Sale.company_id == Company.id)
        .group_by(Company.id)
        .having(db.func.coalesce(db.func.max(Sale.date), datetime(2000, 1, 1)) < now - timedelta(days=14))
        .limit(20)
        .all()
    )
    for company in stale_companies:
        queue.append({
            "company_id": company.id,
            "company_name": company.name,
            "reason": "Empresa sin actividad",
            "severity": "warning",
            "detail": "Sin ventas recientes en 14 días",
        })

    if model_table_exists(BackupLog):
        backup_fail_ids = (
            db.session.query(BackupLog.company_id)
            .filter(BackupLog.status == "error", BackupLog.created_at >= now - timedelta(days=7))
            .group_by(BackupLog.company_id)
            .all()
        )
        for row in backup_fail_ids:
            company = Company.query.filter_by(id=row[0]).first()
            if company:
                queue.append({
                    "company_id": company.id,
                    "company_name": company.name,
                    "reason": "Empresa sin backup válido",
                    "severity": "danger",
                    "detail": "Se detectaron fallos de backup en los últimos 7 días",
                })

    if model_table_exists(MercadoPagoConnection):
        disconnected = (
            MercadoPagoConnection.query.join(Company, Company.id == MercadoPagoConnection.company_id)
            .filter(MercadoPagoConnection.status != "connected")
            .limit(20)
            .all()
        )
        for conn in disconnected:
            queue.append({
                "company_id": conn.company_id,
                "company_name": conn.company.name if conn.company else f"Empresa #{conn.company_id}",
                "reason": "Mercado Pago desconectado",
                "severity": "warning",
                "detail": f"Estado conexión: {conn.status}",
            })

    no_active_users = (
        db.session.query(Company)
        .outerjoin(User, User.company_id == Company.id)
        .group_by(Company.id)
        .having(db.func.coalesce(db.func.sum(db.case((User.active.is_(True), 1), else_=0)), 0) == 0)
        .limit(20)
        .all()
    )
    for company in no_active_users:
        queue.append({
            "company_id": company.id,
            "company_name": company.name,
            "reason": "Empresa sin usuarios activos",
            "severity": "danger",
            "detail": "Todos los usuarios están inactivos",
        })

    blocked = Company.query.filter(Company.active.is_(False)).limit(20).all()
    for company in blocked:
        queue.append({
            "company_id": company.id,
            "company_name": company.name,
            "reason": "Empresa bloqueada",
            "severity": "danger",
            "detail": "Empresa inactiva/suspendida",
        })

    enriched = []
    for row in queue[:60]:
        meta = _attention_meta(row.get("reason", ""))
        action_route = meta.get("action_route") or "saas.companies_panel"
        action_url = url_for(action_route)
        company_id = row.get("company_id")
        if company_id and action_route == "saas.companies_panel":
            action_url = url_for("saas.company_detail", company_id=company_id)
        row["icon"] = meta.get("icon", "⚠")
        row["action_label"] = meta.get("action_label", "Ver empresa")
        row["action_url"] = action_url
        row["priority_group"] = "critical" if row.get("severity") == "danger" else "warning"
        enriched.append(row)
    return enriched


def _sync_automatic_crm_from_attention(now):
    from app import SaaSLead, db
    from services.saas_ops_service import SaaSOpsService

    queue = _build_attention_queue(now)
    for row in queue[:30]:
        lead = SaaSOpsService.create_or_update_lead(
            db.session,
            company_name=row["company_name"],
            contact_name="Operación automática",
            email=None,
            phone=None,
            source="ops_auto",
            notes=f"{row['reason']}: {row['detail']}",
            company_id=row.get("company_id"),
            preferred_user_id=None,
        )
        task = SaaSOpsService.create_task(
            db.session,
            company_id=row.get("company_id"),
            lead_id=getattr(lead, "id", None),
            title=row["reason"],
            description=row["detail"],
            priority="alta" if row["severity"] == "danger" else "media",
            due_days=1 if row["severity"] == "danger" else 3,
            preferred_user_id=None,
        )
        SaaSOpsService.create_alert(
            db.session,
            company_id=row.get("company_id"),
            lead_id=getattr(lead, "id", None),
            task_id=getattr(task, "id", None),
            title=row["reason"],
            message=row["detail"],
            category="operativa",
            severity="alta" if row["severity"] == "danger" else "media",
            preferred_user_id=None,
        )
    db.session.commit()
    return queue


def _normalize_crm_value(value: str | None, allowed: set[str], default: str) -> str:
    normalized = (value or default).strip().lower().replace(" ", "_")
    return normalized if normalized in allowed else default


@bp.route("/", methods=["GET", "POST"])
@superadmin_required
def index():
    from app import (
        AuditLog,
        BackupLog,
        Client,
        Company,
        Invoice,
        Payment,
        Plan,
        Product,
        SaaSAlert,
        SaaSLead,
        SaaSTask,
        ReferralCommission,
        ReferralSeller,
        Sale,
        Subscription,
        User,
        db,
    )

    _require_superadmin()
    PlanService.ensure_defaults(db.session)

    if request.method == "POST":
        payload = {
            "code": (request.form.get("code") or "").strip().lower() or None,
            "name": (request.form.get("name") or "").strip(),
            "price": float(request.form.get("price") or 0),
            "currency": (request.form.get("currency") or "ARS").strip().upper(),
            "duration_days": int(request.form.get("duration_days") or 30),
            "max_users": int(request.form.get("max_users") or 1),
            "max_products": int(request.form.get("max_products") or 1000),
            "max_clients": int(request.form.get("max_clients") or 1000),
            "features_json": (request.form.get("features_json") or "").strip() or None,
            "state": (request.form.get("state") or "active").strip().lower(),
            "active": (request.form.get("active") or "1") == "1",
        }
        if payload["name"]:
            plan = Plan.query.filter_by(code=payload["code"]).first() if payload["code"] else None
            if plan is None:
                db.session.add(Plan(**payload))
                flash("Plan creado.", "success")
            else:
                for key, value in payload.items():
                    setattr(plan, key, value)
                flash("Plan actualizado.", "success")
            db.session.commit()
        return redirect(url_for("saas.index"))

    now = utcnow()
    month_start = datetime(now.year, now.month, 1)
    year_start = datetime(now.year, 1, 1)
    previous_month_start = datetime(month_start.year - 1, 12, 1) if month_start.month == 1 else datetime(month_start.year, month_start.month - 1, 1)
    previous_month_end = month_start
    month_days = max((now - month_start).days + 1, 1)
    previous_period_start = previous_month_end - timedelta(days=month_days)
    previous_period_end = previous_month_end
    companies = Company.query.order_by(Company.created_at.desc()).all()
    plans = PlanService.all_commercial_plans()

    companies_total = Company.query.count()
    active_companies = Company.query.filter_by(active=True).count()
    inactive_companies = Company.query.filter_by(active=False).count()
    suspended_companies = Subscription.query.filter(Subscription.status.in_(["suspended", "expired", "cancelled", "rejected", "charged_back"])).count()
    premium_companies = (
        db.session.query(db.func.count(Subscription.id))
        .join(Plan, Plan.id == Subscription.plan_id)
        .filter(Plan.code == "premium", Subscription.status.in_(["active", "approved", "trial"]))
        .scalar()
        or 0
    )
    expired_companies = Subscription.query.filter(Subscription.status.in_(["expired"])) .count()

    users_count = User.query.count()
    active_users_count = User.query.filter(User.active.is_(True)).count()
    products_count = Product.query.count()
    clients_count = Client.query.count()
    sales_count = Sale.query.count()
    sales_total_amount = float(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).scalar() or 0)

    subscriptions_count = Subscription.query.count()
    trial_companies = Subscription.query.filter(Subscription.status == "trial").count()
    active_subscriptions = Subscription.query.filter(Subscription.status.in_(["active", "approved", "trial"])).count()

    pending_payments_standard = Payment.query.filter(standard_subscription_payment_filter(Payment), Payment.status.in_(["pending", "authorized", "in_process"])).count()
    pending_payments_ai = Payment.query.filter(ai_subscription_payment_filter(Payment), Payment.status.in_(["pending", "authorized", "in_process"])).count()
    pending_payments = pending_payments_standard + pending_payments_ai
    rejected_payments_standard = Payment.query.filter(standard_subscription_payment_filter(Payment), Payment.status.in_(["rejected", "cancelled", "charged_back", "expired"])).count()
    rejected_payments_ai = Payment.query.filter(ai_subscription_payment_filter(Payment), Payment.status.in_(["rejected", "cancelled", "charged_back", "expired"])).count()
    rejected_payments = rejected_payments_standard + rejected_payments_ai
    pending_payments_previous = Payment.query.filter(
        subscription_revenue_payment_filter(Payment),
        Payment.status.in_(["pending", "authorized", "in_process"]),
        Payment.created_at >= previous_period_start,
        Payment.created_at < previous_period_end,
    ).count()

    mrr = (
        db.session.query(db.func.coalesce(db.func.sum(Plan.price), 0))
        .join(Subscription, Subscription.plan_id == Plan.id)
        .filter(Subscription.status.in_(["active", "approved"]))
        .scalar()
        or 0
    )
    monthly_billing = (
        db.session.query(db.func.coalesce(db.func.sum(Invoice.amount), 0))
        .filter(Invoice.issued_at >= month_start)
        .scalar()
        or 0
    )
    annual_billing = (
        db.session.query(db.func.coalesce(db.func.sum(Invoice.amount), 0))
        .filter(Invoice.issued_at >= year_start)
        .scalar()
        or 0
    )
    income_month_standard = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(standard_subscription_payment_filter(Payment), Payment.status == "approved", Payment.created_at >= month_start)
        .scalar()
        or 0
    )
    income_month_ai = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(ai_subscription_payment_filter(Payment), Payment.status == "approved", Payment.created_at >= month_start)
        .scalar()
        or 0
    )
    income_month = float(income_month_standard or 0) + float(income_month_ai or 0)
    income_month_previous_standard = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(
            standard_subscription_payment_filter(Payment),
            Payment.status == "approved",
            Payment.created_at >= previous_month_start,
            Payment.created_at < previous_month_end,
        )
        .scalar()
        or 0
    )
    income_month_previous_ai = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(
            ai_subscription_payment_filter(Payment),
            Payment.status == "approved",
            Payment.created_at >= previous_month_start,
            Payment.created_at < previous_month_end,
        )
        .scalar()
        or 0
    )
    income_month_previous = float(income_month_previous_standard or 0) + float(income_month_previous_ai or 0)
    income_year_standard = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(standard_subscription_payment_filter(Payment), Payment.status == "approved", Payment.created_at >= year_start)
        .scalar()
        or 0
    )
    income_year_ai = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(ai_subscription_payment_filter(Payment), Payment.status == "approved", Payment.created_at >= year_start)
        .scalar()
        or 0
    )
    income_year = float(income_year_standard or 0) + float(income_year_ai or 0)

    upcoming_renewals = (
        Subscription.query.filter(
            Subscription.renewal_enabled.is_(True),
            Subscription.next_billing_date.isnot(None),
            Subscription.next_billing_date >= now,
        )
        .order_by(Subscription.next_billing_date.asc())
        .limit(10)
        .all()
    )

    last_registrations = Company.query.order_by(Company.created_at.desc()).limit(10).all()
    last_users = User.query.order_by(User.created_at.desc()).limit(10).all()
    last_payments = Payment.query.filter(subscription_revenue_payment_filter(Payment)).order_by(Payment.created_at.desc()).limit(10).all()
    last_errors = AuditLog.query.filter(
        db.or_(
            db.func.lower(AuditLog.action).like("%error%"),
            db.func.lower(db.func.coalesce(AuditLog.detail, "")).like("%error%"),
        )
    ).order_by(AuditLog.created_at.desc()).limit(10).all()

    month_windows = []
    for offset in reversed(range(6)):
        base = month_start - timedelta(days=offset * 31)
        start = datetime(base.year, base.month, 1)
        end = datetime(now.year + (1 if now.month == 12 and start.month == 12 else 0), (start.month % 12) + 1, 1) if start.month != 12 else datetime(start.year + 1, 1, 1)
        month_windows.append((start, end, f"{start:%b %Y}"))

    growth_labels = []
    growth_companies_data = []
    sales_month_data = []
    new_subscriptions_data = []
    renewals_data = []
    for start, end, label in month_windows:
        growth_labels.append(label)
        growth_companies_data.append(
            Company.query.filter(Company.created_at >= start, Company.created_at < end).count()
        )
        sales_month_data.append(
            float(
                db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0))
                .filter(Sale.date >= start, Sale.date < end)
                .scalar()
                or 0
            )
        )
        new_subscriptions_data.append(
            Subscription.query.filter(Subscription.starts_at >= start, Subscription.starts_at < end).count()
        )
        renewals_data.append(
            Payment.query.filter(subscription_revenue_payment_filter(Payment), Payment.status == "approved", Payment.created_at >= start, Payment.created_at < end).count()
        )

    plan_state_rows = (
        db.session.query(Plan.name, db.func.count(Subscription.id))
        .outerjoin(Subscription, Subscription.plan_id == Plan.id)
        .group_by(Plan.name)
        .order_by(Plan.name.asc())
        .all()
    )
    plan_state_labels = [row[0] or "Sin plan" for row in plan_state_rows]
    plan_state_data = [int(row[1] or 0) for row in plan_state_rows]

    referral_sellers_count = 0
    referral_commissions_count = 0
    referral_sold_total = 0.0
    referral_paid_total = 0.0
    referral_pending_count = 0
    latest_referral_commissions = []
    if model_table_exists(ReferralSeller) and model_table_exists(ReferralCommission):
        referral_sellers_count = ReferralSeller.query.count()
        referral_commissions_count = ReferralCommission.query.count()
        referral_sold_total = float(db.session.query(db.func.coalesce(db.func.sum(ReferralCommission.sold_amount), 0)).scalar() or 0)
        referral_paid_total = float(db.session.query(db.func.coalesce(db.func.sum(ReferralCommission.commission_amount), 0)).filter(ReferralCommission.status == "pagada").scalar() or 0)
        referral_pending_count = ReferralCommission.query.filter(ReferralCommission.status.in_(["pendiente", "disponible"])).count()
        latest_referral_commissions = ReferralCommission.query.order_by(ReferralCommission.created_at.desc()).limit(10).all()

    crm_leads_total = SaaSLead.query.count()
    crm_leads_open = SaaSLead.query.filter(SaaSLead.status.in_(["nuevo", "contactado", "propuesta"])).count()
    crm_tasks_open = SaaSTask.query.filter(SaaSTask.status != "hecha").count()
    crm_tasks_overdue = SaaSTask.query.filter(
        SaaSTask.status != "hecha",
        SaaSTask.due_at.isnot(None),
        SaaSTask.due_at < now,
    ).count()
    crm_alerts_open = SaaSAlert.query.filter(SaaSAlert.status == "abierta").count()
    latest_crm_leads = SaaSLead.query.order_by(SaaSLead.created_at.desc()).limit(8).all()
    latest_crm_tasks = SaaSTask.query.order_by(SaaSTask.created_at.desc()).limit(8).all()
    latest_crm_alerts = SaaSAlert.query.order_by(SaaSAlert.created_at.desc()).limit(8).all()

    metrics = {
        "companies_total": companies_total,
        "active_companies": active_companies,
        "inactive_companies": inactive_companies,
        "suspended_companies": suspended_companies,
        "premium_companies": int(premium_companies),
        "expired_companies": expired_companies,
        "users_count": users_count,
        "active_users_count": active_users_count,
        "products_count": products_count,
        "clients_count": clients_count,
        "sales_count": sales_count,
        "sales_total_amount": sales_total_amount,
        "subscriptions_count": subscriptions_count,
        "active_subscriptions": active_subscriptions,
        "trial_companies": trial_companies,
        "pending_payments": pending_payments,
        "pending_payments_standard": pending_payments_standard,
        "pending_payments_ai": pending_payments_ai,
        "rejected_payments": rejected_payments,
        "rejected_payments_standard": rejected_payments_standard,
        "rejected_payments_ai": rejected_payments_ai,
        "mrr": float(mrr),
        "arr": float(mrr) * 12,
        "monthly_billing": float(monthly_billing),
        "annual_billing": float(annual_billing),
        "income_month": float(income_month),
        "income_month_standard": float(income_month_standard),
        "income_month_ai": float(income_month_ai),
        "income_year": float(income_year),
        "income_year_standard": float(income_year_standard),
        "income_year_ai": float(income_year_ai),
        "upcoming_renewals": upcoming_renewals,
        "growth_labels": growth_labels,
        "growth_companies_data": growth_companies_data,
        "sales_month_data": sales_month_data,
        "new_subscriptions_data": new_subscriptions_data,
        "renewals_data": renewals_data,
        "plan_state_labels": plan_state_labels,
        "plan_state_data": plan_state_data,
        "referral_sellers_count": referral_sellers_count,
        "referral_commissions_count": referral_commissions_count,
        "referral_sold_total": referral_sold_total,
        "referral_paid_total": referral_paid_total,
        "referral_pending_count": referral_pending_count,
        "crm_leads_total": crm_leads_total,
        "crm_leads_open": crm_leads_open,
        "crm_tasks_open": crm_tasks_open,
        "crm_tasks_overdue": crm_tasks_overdue,
        "crm_alerts_open": crm_alerts_open,
    }

    health_snapshot = _health_check_snapshot(db.session, now)
    attention_queue = _sync_automatic_crm_from_attention(now)

    # Executive KPIs requested for first 30-second understanding.
    companies_new_month = Company.query.filter(Company.created_at >= month_start).count()
    companies_new_previous = Company.query.filter(
        Company.created_at >= previous_month_start,
        Company.created_at < previous_month_end,
    ).count()
    companies_lost_month = Company.query.filter(Company.active.is_(False), Company.created_at < month_start).count()
    active_companies_previous = Company.query.filter(
        Company.active.is_(True),
        Company.created_at < previous_month_end,
    ).count()
    trial_companies_previous = Subscription.query.filter(
        Subscription.status == "trial",
        Subscription.created_at < previous_month_end,
    ).count()
    suspended_companies_previous = Subscription.query.filter(
        Subscription.status.in_(["suspended", "expired", "cancelled", "rejected", "charged_back"]),
        Subscription.created_at < previous_month_end,
    ).count()
    referral_sellers_previous = ReferralSeller.query.filter(ReferralSeller.created_at < previous_month_end).count() if model_table_exists(ReferralSeller) else 0
    renewals_previous_7 = Subscription.query.filter(
        Subscription.renewal_enabled.is_(True),
        Subscription.next_billing_date.isnot(None),
        Subscription.next_billing_date >= previous_period_start,
        Subscription.next_billing_date <= previous_period_start + timedelta(days=7),
    ).count()
    renewals_7_days = Subscription.query.filter(
        Subscription.renewal_enabled.is_(True),
        Subscription.next_billing_date.isnot(None),
        Subscription.next_billing_date >= now,
        Subscription.next_billing_date <= now + timedelta(days=7),
    ).count()
    income_today = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(subscription_revenue_payment_filter(Payment), Payment.status == "approved", Payment.created_at >= datetime(now.year, now.month, now.day))
        .scalar()
        or 0
    )

    metrics.update(
        {
            "arr_estimated": float(metrics["mrr"]) * 12,
            "companies_new_month": companies_new_month,
            "companies_lost_month": companies_lost_month,
            "renewals_7_days": renewals_7_days,
            "income_today": float(income_today),
            "support_open": crm_alerts_open,
            "backups_failed": health_snapshot["summary"]["error"],
            "server_status": "OK" if health_snapshot["summary"]["error"] == 0 else "Error",
            "mp_status": "OK" if any(item["key"] == "mercado_pago" and item["data"]["status"] == "ok" for item in health_snapshot["checks"]) else "Advertencia",
            "smtp_status": "OK" if any(item["key"] == "smtp" and item["data"]["status"] == "ok" for item in health_snapshot["checks"]) else "Advertencia",
        }
    )

    churn_rate = _safe_pct(float(companies_lost_month), float(max(companies_total, 1)))

    mrr_previous = (
        db.session.query(db.func.coalesce(db.func.sum(Plan.price), 0))
        .join(Subscription, Subscription.plan_id == Plan.id)
        .filter(
            Subscription.status.in_(["active", "approved"]),
            Subscription.created_at < previous_month_end,
        )
        .scalar()
        or 0
    )
    income_today_previous = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(
            subscription_revenue_payment_filter(Payment),
            Payment.status == "approved",
            Payment.created_at >= datetime(previous_period_end.year, previous_period_end.month, previous_period_end.day),
            Payment.created_at < datetime(previous_period_end.year, previous_period_end.month, previous_period_end.day) + timedelta(days=1),
        )
        .scalar()
        or 0
    )
    churn_previous = _safe_pct(float(max(companies_lost_month - 1, 0)), float(max(companies_total, 1)))

    executive_cards = [
        {
            "key": "mrr",
            "label": "MRR",
            "value": float(metrics["mrr"]),
            "kind": "currency",
            "trend": _trend_payload(float(metrics["mrr"]), float(mrr_previous), higher_is_better=True),
        },
        {
            "key": "arr",
            "label": "ARR",
            "value": float(metrics["arr_estimated"]),
            "kind": "currency",
            "trend": _trend_payload(float(metrics["arr_estimated"]), float(mrr_previous) * 12.0, higher_is_better=True),
        },
        {
            "key": "active_companies",
            "label": "Empresas activas",
            "value": int(metrics["active_companies"]),
            "kind": "count",
            "trend": _trend_payload(float(metrics["active_companies"]), float(active_companies_previous), higher_is_better=True),
        },
        {
            "key": "trial_companies",
            "label": "En trial",
            "value": int(metrics["trial_companies"]),
            "kind": "count",
            "trend": _trend_payload(float(metrics["trial_companies"]), float(trial_companies_previous), higher_is_better=True),
        },
        {
            "key": "new_month",
            "label": "Nuevos del mes",
            "value": int(metrics["companies_new_month"]),
            "kind": "count",
            "trend": _trend_payload(float(metrics["companies_new_month"]), float(companies_new_previous), higher_is_better=True),
        },
        {
            "key": "suspended",
            "label": "Suspendidos",
            "value": int(metrics["suspended_companies"]),
            "kind": "count",
            "trend": _trend_payload(float(metrics["suspended_companies"]), float(suspended_companies_previous), higher_is_better=False),
        },
        {
            "key": "churn",
            "label": "Churn",
            "value": float(churn_rate),
            "kind": "percent",
            "trend": _trend_payload(float(churn_rate), float(churn_previous), higher_is_better=False),
        },
        {
            "key": "pending_payments",
            "label": "Pagos pendientes",
            "value": int(metrics["pending_payments"]),
            "kind": "count",
            "trend": _trend_payload(float(metrics["pending_payments"]), float(pending_payments_previous), higher_is_better=False),
        },
        {
            "key": "income_today",
            "label": "Ingresos de hoy",
            "value": float(metrics["income_today"]),
            "kind": "currency",
            "trend": _trend_payload(float(metrics["income_today"]), float(income_today_previous), higher_is_better=True),
        },
        {
            "key": "income_month",
            "label": "Ingresos del mes",
            "value": float(metrics["income_month"]),
            "kind": "currency",
            "trend": _trend_payload(float(metrics["income_month"]), float(income_month_previous), higher_is_better=True),
        },
        {
            "key": "renewals_7_days",
            "label": "Renovaciones (7d)",
            "value": int(metrics["renewals_7_days"]),
            "kind": "count",
            "trend": _trend_payload(float(metrics["renewals_7_days"]), float(renewals_previous_7), higher_is_better=False),
        },
        {
            "key": "referrals",
            "label": "Referidos",
            "value": int(metrics["referral_sellers_count"]),
            "kind": "count",
            "trend": _trend_payload(float(metrics["referral_sellers_count"]), float(referral_sellers_previous), higher_is_better=True),
        },
    ]

    # Funnel + SaaS metrics
    visits = int(companies_new_month * 5 or 1)
    signups = int(companies_new_month)
    trials = int(trial_companies)
    paid_clients = int(active_subscriptions)
    referred_clients = int(referral_sellers_count)
    active_clients = int(active_companies)

    funnel = {
        "labels": ["Visitas", "Registro", "Prueba", "Cliente Pago", "Cliente Activo", "Referido"],
        "values": [visits, signups, trials, paid_clients, active_clients, referred_clients],
        "conversion": {
            "visit_to_signup": _safe_pct(signups, visits),
            "signup_to_trial": _safe_pct(trials, signups),
            "trial_to_paid": _safe_pct(paid_clients, trials),
            "paid_to_active": _safe_pct(active_clients, paid_clients),
            "active_to_referred": _safe_pct(referred_clients, active_clients),
        },
    }

    churn_rate = _safe_pct(float(companies_lost_month), float(max(companies_total, 1)))
    retention_rate = round(100.0 - churn_rate, 2)
    arpu = float(metrics["mrr"]) / float(active_subscriptions or 1)
    ltv = arpu * (1 / max(churn_rate / 100.0, 0.05))

    saas_metrics = {
        "mrr": float(metrics["mrr"]),
        "arr": float(metrics["arr_estimated"]),
        "arpu": float(arpu),
        "ltv": float(ltv),
        "cac": 0.0,
        "churn": float(churn_rate),
        "retention": float(retention_rate),
        "active_clients": int(active_companies),
        "suspended_clients": int(suspended_companies),
        "trial_conversion": _safe_pct(float(active_subscriptions), float(trial_companies or 1)),
    }

    # Commercial data-quality controls: keep cash, pending collections and invoicing
    # explicitly separated so the SuperAdmin never interprets initiated/pending payments as revenue.
    approved_subscription_payments = Payment.query.filter(
        subscription_revenue_payment_filter(Payment), Payment.status == "approved"
    ).count()
    pending_subscription_payments = Payment.query.filter(
        subscription_revenue_payment_filter(Payment), Payment.status.in_(["pending", "authorized", "in_process"])
    ).count()
    rejected_subscription_payments = Payment.query.filter(
        subscription_revenue_payment_filter(Payment), Payment.status.in_(["rejected", "cancelled", "expired", "charged_back"])
    ).count()
    issued_invoices = Invoice.query.count()
    subscriptions_with_future_billing = Subscription.query.filter(
        Subscription.next_billing_date.isnot(None),
        Subscription.next_billing_date > now + timedelta(days=366),
    ).count()
    subscriptions_invalid_dates = Subscription.query.filter(
        Subscription.starts_at.isnot(None),
        Subscription.ends_at.isnot(None),
        Subscription.ends_at < Subscription.starts_at,
    ).count()
    commercial_data_quality = {
        "approved_payments": int(approved_subscription_payments),
        "pending_payments": int(pending_subscription_payments),
        "rejected_payments": int(rejected_subscription_payments),
        "issued_invoices": int(issued_invoices),
        "future_billing_anomalies": int(subscriptions_with_future_billing),
        "invalid_date_anomalies": int(subscriptions_invalid_dates),
        "status": "ok" if subscriptions_invalid_dates == 0 and subscriptions_with_future_billing == 0 else "warning",
    }

    # Renewal buckets
    renewals_buckets = {
        "today": Subscription.query.filter(Subscription.next_billing_date >= datetime(now.year, now.month, now.day), Subscription.next_billing_date < datetime(now.year, now.month, now.day) + timedelta(days=1)).count(),
        "days_7": Subscription.query.filter(Subscription.next_billing_date >= now, Subscription.next_billing_date <= now + timedelta(days=7)).count(),
        "days_15": Subscription.query.filter(Subscription.next_billing_date >= now, Subscription.next_billing_date <= now + timedelta(days=15)).count(),
        "days_30": Subscription.query.filter(Subscription.next_billing_date >= now, Subscription.next_billing_date <= now + timedelta(days=30)).count(),
        "pending_collections": pending_payments,
    }

    # Support metrics
    from app import SupportTicket

    support_open_tickets = SupportTicket.query.filter(SupportTicket.status == "pendiente").count()
    support_resolved_tickets = SupportTicket.query.filter(SupportTicket.status == "resuelto").count()
    support_critical = SupportTicket.query.filter(SupportTicket.status == "pendiente", SupportTicket.reason.in_(["No puedo ingresar", "Problemas con suscripcion"])).count()
    support_metrics = {
        "open": support_open_tickets,
        "critical": support_critical,
        "overdue": SupportTicket.query.filter(SupportTicket.status == "pendiente", SupportTicket.created_at < now - timedelta(days=2)).count(),
        "avg_resolution_hours": 0 if support_resolved_tickets == 0 else round(float(support_open_tickets + support_resolved_tickets) / support_resolved_tickets * 12, 2),
        "top_claim_companies": [
            {
                "name": row[0] or "Sin empresa",
                "count": int(row[1] or 0),
            }
            for row in db.session.query(Company.name, db.func.count(SupportTicket.id))
            .outerjoin(SupportTicket, SupportTicket.company_id == Company.id)
            .group_by(Company.name)
            .order_by(db.desc(db.func.count(SupportTicket.id)))
            .limit(5)
            .all()
        ],
    }

    # Attention grouped for rendering by priority.
    attention_grouped = {
        "critical": [row for row in attention_queue if row.get("priority_group") == "critical"],
        "warning": [row for row in attention_queue if row.get("priority_group") != "critical"],
    }

    # Global activity timeline enriched with result label.
    activity_timeline = []
    for log in AuditLog.query.order_by(AuditLog.created_at.desc()).limit(50).all():
        result = _timeline_result(log.detail)
        activity_timeline.append(
            {
                "at": log.created_at,
                "action": log.action,
                "entity": log.entity,
                "detail": log.detail,
                "company_id": log.company_id,
                "result_label": result["label"],
                "result_color": result["color"],
            }
        )

    quick_actions = [
        {"label": "Nueva Empresa", "url": url_for("saas.companies_panel"), "icon": "bi-building-add"},
        {"label": "Nuevo Prospecto", "url": url_for("saas.crm_panel"), "icon": "bi-person-plus"},
        {"label": "Crear Plan", "url": url_for("saas.plans_panel"), "icon": "bi-diagram-3"},
        {"label": "Crear Cupón", "url": url_for("saas.billing"), "icon": "bi-ticket-perforated"},
        {"label": "Enviar Email", "url": url_for("support.admin_index"), "icon": "bi-envelope"},
        {"label": "Crear Backup", "url": url_for("saas.backups_panel"), "icon": "bi-cloud-arrow-up"},
        {"label": "Estado Servidor", "url": url_for("saas.server_status"), "icon": "bi-hdd-network"},
        {"label": "Logs", "url": url_for("saas.logs_panel"), "icon": "bi-journal-text"},
    ]

    # Cached chart datasets to reduce heavy repeated aggregation.
    operations_charts = _cached_value(
        "saas_ops_charts",
        120,
        lambda: {
            "labels": growth_labels,
            "companies": growth_companies_data,
            "sales": sales_month_data,
            "subscriptions": new_subscriptions_data,
            "renewals": renewals_data,
            "plan_labels": plan_state_labels,
            "plan_data": plan_state_data,
            "referral_paid": round(float(referral_paid_total), 2),
            "referral_pending": int(referral_pending_count),
        },
    )

    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(20).all()
    backups = BackupLog.query.order_by(BackupLog.created_at.desc()).limit(10).all()
    return render_template(
        "saas/index.html",
        companies=companies,
        plans=plans,
        logs=logs,
        backups=backups,
        metrics=metrics,
        last_registrations=last_registrations,
        last_users=last_users,
        last_payments=last_payments,
        latest_referral_commissions=latest_referral_commissions,
        last_errors=last_errors,
        latest_crm_leads=latest_crm_leads,
        latest_crm_tasks=latest_crm_tasks,
        latest_crm_alerts=latest_crm_alerts,
        health_snapshot=health_snapshot,
        attention_queue=attention_queue,
        attention_grouped=attention_grouped,
        funnel=funnel,
        saas_metrics=saas_metrics,
        commercial_data_quality=commercial_data_quality,
        renewals_buckets=renewals_buckets,
        support_metrics=support_metrics,
        activity_timeline=activity_timeline,
        executive_cards=executive_cards,
        quick_actions=quick_actions,
        operations_charts=operations_charts,
    )


@bp.route("/attention")
@superadmin_required
def attention_panel():
    """Unified read-only operational queue for Super Admin."""
    from app import db

    now = utcnow()
    queue = _build_attention_queue(now)
    critical = [row for row in queue if row.get("priority_group") == "critical"]
    warning = [row for row in queue if row.get("priority_group") != "critical"]
    health_snapshot = _health_check_snapshot(db.session, now)
    return render_template(
        "saas/attention.html",
        attention_queue=queue,
        critical=critical,
        warning=warning,
        health_snapshot=health_snapshot,
        refreshed_at=now,
    )


COMMERCIAL_CRM_CHANNELS = {"email_commercial", "whatsapp_commercial"}

def _crm_inbox_rows(company, *, q: str = "", channel: str = "all", state: str = "all", limit: int = 100):
    """Return a unified, human-readable inbox for StockArMobile commercial conversations."""
    from app import SaaSLead
    from stockarmobile.models.conversations import Conversation, ConversationMessage, ConversationParticipant
    from services.saas_commercial_whatsapp import commercial_conversation_attention

    if company is None:
        return []

    conversations = (
        Conversation.query
        .filter(
            Conversation.company_id == int(company.id),
            Conversation.channel.in_(COMMERCIAL_CRM_CHANNELS),
        )
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(max(1, min(int(limit or 100), 200)))
        .all()
    )

    q_text = str(q or "").strip().lower()
    channel = channel if channel in {"all", "email", "whatsapp"} else "all"
    state = state if state in {"all", "pending", "waiting"} else "all"
    rows = []

    for conversation in conversations:
        latest = (
            ConversationMessage.query
            .filter(
                ConversationMessage.company_id == int(company.id),
                ConversationMessage.conversation_id == int(conversation.id),
            )
            .order_by(ConversationMessage.id.desc())
            .first()
        )

        metadata = conversation.metadata_json if isinstance(conversation.metadata_json, dict) else {}
        lead = None
        lead_id = metadata.get("lead_id")
        if str(lead_id or "").isdigit():
            lead = SaaSLead.query.filter_by(id=int(lead_id)).first()

        if lead is None:
            participant = (
                ConversationParticipant.query
                .filter_by(
                    company_id=int(company.id),
                    conversation_id=int(conversation.id),
                    participant_type="prospect",
                )
                .order_by(ConversationParticipant.id.desc())
                .first()
            )
            participant_id = getattr(participant, "participant_id", None)
            if participant_id:
                lead = SaaSLead.query.filter_by(id=int(participant_id)).first()

        raw_external = str(conversation.external_conversation_id or "")
        phone = ""
        if conversation.channel == "whatsapp_commercial":
            phone = "".join(ch for ch in raw_external if ch.isdigit())[:40]
        email = str((metadata or {}).get("sender_email") or "")[:160]
        if conversation.channel == "email_commercial" and not email:
            email = str(getattr(lead, "email", None) or "")[:160]

        channel_label = "Gmail" if conversation.channel == "email_commercial" else "WhatsApp"
        attention = commercial_conversation_attention(conversation) if conversation.channel == "whatsapp_commercial" else {
            "status": "email",
            "pending": False,
        }
        needs_reply = bool(latest is not None and latest.sender_type == "user")
        if conversation.channel == "whatsapp_commercial":
            needs_reply = bool(attention.get("pending") or needs_reply)

        if channel == "email" and conversation.channel != "email_commercial":
            continue
        if channel == "whatsapp" and conversation.channel != "whatsapp_commercial":
            continue
        if state == "pending" and not needs_reply:
            continue
        if state == "waiting" and needs_reply:
            continue

        latest_text = str(latest.content or "").strip() if latest is not None else ""
        contact_name = (
            getattr(lead, "contact_name", None)
            or getattr(lead, "company_name", None)
            or email
            or phone
            or "Prospecto"
        )
        company_name = getattr(lead, "company_name", None) or (
            email if conversation.channel == "email_commercial" else "Prospecto de WhatsApp"
        )

        searchable = " ".join(
            [
                str(contact_name or ""),
                str(company_name or ""),
                str(email or ""),
                str(phone or ""),
                latest_text,
            ]
        ).lower()
        if q_text and q_text not in searchable:
            continue

        rows.append(
            {
                "conversation_id": conversation.id,
                "channel": conversation.channel,
                "channel_label": channel_label,
                "contact_name": str(contact_name)[:160],
                "company_name": str(company_name)[:160],
                "email": email,
                "phone": phone,
                "latest_text": latest_text[:240],
                "latest_sender_type": getattr(latest, "sender_type", "") if latest is not None else "",
                "latest_at": (
                    _format_admin_datetime_local(latest.created_at, "%d/%m %H:%M")
                    if latest is not None
                    else _format_admin_datetime_local(conversation.updated_at, "%d/%m %H:%M")
                ),
                "updated_at": _format_admin_datetime_local(conversation.updated_at, "%d/%m %H:%M"),
                "needs_reply": needs_reply,
                "attention": attention,
            }
        )

    return rows


@bp.route("/crm/inbox", methods=["GET"])
@superadmin_required
def crm_inbox():
    from stockarmobile.models.conversations import Conversation, ConversationMessage
    from services.saas_commercial_whatsapp import commercial_conversation_attention, get_commercial_company

    _require_superadmin()
    company = get_commercial_company()
    q = (request.args.get("q") or "").strip()
    channel = (request.args.get("channel") or "all").strip().lower()
    state = (request.args.get("state") or "all").strip().lower()
    if channel not in {"all", "email", "whatsapp"}:
        channel = "all"
    if state not in {"all", "pending", "waiting"}:
        state = "all"

    all_rows = _crm_inbox_rows(company, q="", channel="all", state="all", limit=200)
    rows = _crm_inbox_rows(company, q=q, channel=channel, state=state, limit=200)

    counts = {
        "total": len(all_rows),
        "pending": sum(1 for row in all_rows if row["needs_reply"]),
        "email": sum(1 for row in all_rows if row["channel"] == "email_commercial"),
        "whatsapp": sum(1 for row in all_rows if row["channel"] == "whatsapp_commercial"),
    }

    selected_id = request.args.get("conversation_id", type=int)
    selected = None
    if company is not None and selected_id:
        selected = (
            Conversation.query
            .filter(
                Conversation.id == int(selected_id),
                Conversation.company_id == int(company.id),
                Conversation.channel.in_(COMMERCIAL_CRM_CHANNELS),
            )
            .first()
        )

    if selected is None and rows:
        preferred = next((row for row in rows if row["needs_reply"]), None) or rows[0]
        selected = Conversation.query.filter(
            Conversation.id == int(preferred["conversation_id"]),
            Conversation.company_id == int(company.id),
        ).first()

    selected_view = None
    if selected is not None and company is not None:
        metadata = selected.metadata_json if isinstance(selected.metadata_json, dict) else {}
        lead = None
        lead_id = metadata.get("lead_id")
        if str(lead_id or "").isdigit():
            from app import SaaSLead
            lead = SaaSLead.query.filter_by(id=int(lead_id)).first()

        selected_messages = (
            ConversationMessage.query
            .filter(
                ConversationMessage.company_id == int(company.id),
                ConversationMessage.conversation_id == int(selected.id),
            )
            .order_by(ConversationMessage.id.desc())
            .limit(200)
            .all()
        )
        selected_messages.reverse()

        if lead is None:
            from stockarmobile.models.conversations import ConversationParticipant
            participant = (
                ConversationParticipant.query
                .filter_by(
                    company_id=int(company.id),
                    conversation_id=int(selected.id),
                    participant_type="prospect",
                )
                .order_by(ConversationParticipant.id.desc())
                .first()
            )
            if participant and participant.participant_id:
                from app import SaaSLead
                lead = SaaSLead.query.filter_by(id=int(participant.participant_id)).first()

        selected_email = str(metadata.get("sender_email") or getattr(lead, "email", None) or "")[:160]
        selected_phone = ""
        if selected.channel == "whatsapp_commercial":
            selected_phone = "".join(ch for ch in str(selected.external_conversation_id or "") if ch.isdigit())[:40]

        selected_needs_reply = bool(selected_messages and selected_messages[-1].sender_type == "user")
        if selected.channel == "whatsapp_commercial":
            selected_needs_reply = bool(
                commercial_conversation_attention(selected).get("pending") or selected_needs_reply
            )

        selected_view = {
            "conversation_id": selected.id,
            "channel": selected.channel,
            "channel_label": "Gmail" if selected.channel == "email_commercial" else "WhatsApp",
            "contact_name": getattr(lead, "contact_name", None) or getattr(lead, "company_name", None) or selected_email or selected_phone or "Prospecto",
            "company_name": getattr(lead, "company_name", None) or (selected_email if selected.channel == "email_commercial" else "Prospecto de WhatsApp"),
            "email": selected_email,
            "phone": selected_phone,
            "needs_reply": selected_needs_reply,
            "messages": [
                {
                    "id": msg.id,
                    "sender_type": msg.sender_type,
                    "sender_label": "Cliente" if msg.sender_type == "user" else ("Vos" if msg.sender_type == "human" else "Comercial IA"),
                    "content": str(msg.content or ""),
                    "preview": str(msg.content or "").replace("\n", " ")[:120],
                    "created_at": _format_admin_datetime_local(msg.created_at, "%d/%m/%Y %H:%M"),
                }
                for msg in selected_messages
            ],
        }

    return render_template(
        "saas/crm_inbox.html",
        rows=rows,
        selected=selected_view,
        q=q,
        channel=channel,
        state=state,
        counts=counts,
    )


@bp.route("/crm/inbox/messages/delete", methods=["POST"])
@superadmin_required
def crm_inbox_message_delete():
    from app import db, record_audit
    from services.saas_commercial_service import delete_commercial_conversation_message
    from services.saas_commercial_whatsapp import get_commercial_company

    _require_superadmin()
    company = get_commercial_company()
    message_id = request.form.get("message_id", type=int)
    conversation_id = request.form.get("conversation_id", type=int)
    if company is None or not message_id:
        abort(404)

    if not _require_superadmin_step_up():
        target = {"conversation_id": conversation_id} if conversation_id else {}
        return redirect(url_for("saas.crm_inbox", **target))

    try:
        result = delete_commercial_conversation_message(
            db.session,
            message_id=message_id,
            company_id=company.id,
        )
        record_audit(
            action="commercial_crm_message_delete",
            entity="conversation_message",
            entity_id=message_id,
            company_id=company.id,
            detail=(
                f"Mensaje comercial eliminado del CRM. conversation_id={result['conversation_id']}; "
                f"channel={result['channel']}; email_tombstone={result['email_tombstone']}."
            ),
            user_id=current_user.id,
        )
        db.session.commit()
        flash(
            "Mensaje eliminado del CRM. El origen externo no fue borrado."
            if not result["email_tombstone"]
            else "Mensaje eliminado del CRM y marcado para no volver a importarlo desde Gmail.",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("No se pudo eliminar mensaje comercial id=%s", message_id)
        flash("No se pudo eliminar el mensaje. No se realizaron cambios.", "danger")

    return redirect(url_for("saas.crm_inbox", conversation_id=conversation_id) if conversation_id else url_for("saas.crm_inbox"))


@bp.route("/crm", methods=["GET", "POST"])
@superadmin_required
def crm_panel():
    from app import Company, SaaSAlert, SaaSLead, SaaSTask, User, db, record_audit
    from services.saas_commercial_service import normalize_email

    _require_superadmin()
    now = utcnow()
    companies = Company.query.order_by(Company.name.asc()).all()
    users = User.query.filter(User.active.is_(True)).order_by(User.username.asc()).all()

    if request.method == "POST":
        entity = (request.form.get("entity") or "").strip().lower()
        if entity == "lead":
            company_name = (request.form.get("company_name") or "").strip()
            contact_name = (request.form.get("contact_name") or "").strip()
            if not company_name or not contact_name:
                flash("Empresa y contacto son obligatorios para crear un prospecto.", "danger")
                return _redirect_back("saas.crm_panel")

            raw_email = (request.form.get("email") or "").strip()
            normalized_email = normalize_email(raw_email)

            lead = SaaSLead(
                company_name=company_name[:160],
                contact_name=contact_name[:160],
                email=normalized_email[:160] if normalized_email else None,
                email_status="valid" if normalized_email else ("invalid" if raw_email else "unknown"),
                phone=(request.form.get("phone") or "").strip()[:40] or None,
                source=(request.form.get("source") or "manual").strip().lower()[:80] or "manual",
                status=_normalize_crm_value(request.form.get("status"), CRM_LEAD_STATUSES, "nuevo"),
                priority=_normalize_crm_value(request.form.get("priority"), CRM_PRIORITIES, "media"),
                next_follow_up_at=_parse_dt(request.form.get("next_follow_up_at")),
                notes=(request.form.get("notes") or "").strip() or None,
                company_id=request.form.get("company_id", type=int) or None,
                assigned_user_id=request.form.get("assigned_user_id", type=int) or None,
                created_by_user_id=current_user.id,
            )
            db.session.add(lead)
            record_audit(
                action="saas_lead_create",
                entity="saas_lead",
                detail=f"Prospecto creado para {lead.company_name}.",
                user_id=current_user.id,
                company_id=lead.company_id,
            )
            db.session.commit()
            flash("Prospecto creado.", "success")
            if raw_email and normalized_email is None:
                flash("El email no tiene un formato válido y no será elegible para campañas.", "warning")
            return redirect(url_for("saas.crm_panel"))

        if entity == "task":
            title = (request.form.get("title") or "").strip()
            if not title:
                flash("El titulo de la tarea es obligatorio.", "danger")
                return _redirect_back("saas.crm_panel")

            task = SaaSTask(
                title=title[:180],
                description=(request.form.get("description") or "").strip() or None,
                status=_normalize_crm_value(request.form.get("status"), CRM_TASK_STATUSES, "pendiente"),
                priority=_normalize_crm_value(request.form.get("priority"), CRM_PRIORITIES, "media"),
                due_at=_parse_dt(request.form.get("due_at")),
                completed_at=now if _normalize_crm_value(request.form.get("status"), CRM_TASK_STATUSES, "pendiente") == "hecha" else None,
                lead_id=request.form.get("lead_id", type=int) or None,
                company_id=request.form.get("company_id", type=int) or None,
                assigned_user_id=request.form.get("assigned_user_id", type=int) or None,
                created_by_user_id=current_user.id,
            )
            db.session.add(task)
            record_audit(
                action="saas_task_create",
                entity="saas_task",
                detail=f"Tarea creada: {task.title}.",
                user_id=current_user.id,
                company_id=task.company_id,
            )
            db.session.commit()
            flash("Tarea creada.", "success")
            return redirect(url_for("saas.crm_panel"))

        if entity == "alert":
            title = (request.form.get("title") or "").strip()
            message = (request.form.get("message") or "").strip()
            if not title or not message:
                flash("Titulo y mensaje son obligatorios para crear una alerta.", "danger")
                return _redirect_back("saas.crm_panel")

            alert = SaaSAlert(
                title=title[:180],
                message=message,
                category=(request.form.get("category") or "operativa").strip().lower()[:40] or "operativa",
                severity=_normalize_crm_value(request.form.get("severity"), {"baja", "media", "alta", "critica"}, "media"),
                status=_normalize_crm_value(request.form.get("status"), CRM_ALERT_STATUSES, "abierta"),
                company_id=request.form.get("company_id", type=int) or None,
                lead_id=request.form.get("lead_id", type=int) or None,
                task_id=request.form.get("task_id", type=int) or None,
                assigned_user_id=request.form.get("assigned_user_id", type=int) or None,
                created_by_user_id=current_user.id,
            )
            db.session.add(alert)
            record_audit(
                action="saas_alert_create",
                entity="saas_alert",
                detail=f"Alerta creada: {alert.title}.",
                user_id=current_user.id,
                company_id=alert.company_id,
            )
            db.session.commit()
            flash("Alerta creada.", "success")
            return redirect(url_for("saas.crm_panel"))

        flash("Entidad CRM invalida.", "danger")
        return _redirect_back("saas.crm_panel")

    q = (request.args.get("q") or "").strip()
    lead_status = (request.args.get("lead_status") or "all").strip().lower()
    industry_filter = (request.args.get("industry") or "").strip()
    province_filter = (request.args.get("province") or "").strip()
    consent_filter = (request.args.get("consent") or "all").strip().lower()
    task_status = (request.args.get("task_status") or "all").strip().lower()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = min(100, max(20, int(request.args.get("per_page") or 50)))
    except (TypeError, ValueError):
        per_page = 50
    alert_status = (request.args.get("alert_status") or "all").strip().lower()

    lead_query = SaaSLead.query
    task_query = SaaSTask.query
    alert_query = SaaSAlert.query

    if q:
        like = f"%{q}%"
        lead_query = lead_query.filter(
            db.or_(
                SaaSLead.company_name.ilike(like),
                SaaSLead.contact_name.ilike(like),
                SaaSLead.email.ilike(like),
                SaaSLead.phone.ilike(like),
                SaaSLead.notes.ilike(like),
            )
        )
        task_query = task_query.filter(
            db.or_(
                SaaSTask.title.ilike(like),
                SaaSTask.description.ilike(like),
            )
        )
        alert_query = alert_query.filter(
            db.or_(
                SaaSAlert.title.ilike(like),
                SaaSAlert.message.ilike(like),
            )
        )

    if industry_filter:
        lead_query = lead_query.filter(SaaSLead.industry.ilike(f"%{industry_filter}%"))
    if province_filter:
        lead_query = lead_query.filter(SaaSLead.province.ilike(f"%{province_filter}%"))
    if consent_filter in {"opted_in", "opted_out", "unknown"}:
        lead_query = lead_query.filter(SaaSLead.email_consent_status == consent_filter)
    if lead_status in CRM_LEAD_STATUSES:
        lead_query = lead_query.filter(SaaSLead.status == lead_status)
    if task_status in CRM_TASK_STATUSES:
        task_query = task_query.filter(SaaSTask.status == task_status)
    if alert_status in CRM_ALERT_STATUSES:
        alert_query = alert_query.filter(SaaSAlert.status == alert_status)

    lead_total = lead_query.count()
    leads = lead_query.order_by(SaaSLead.updated_at.desc(), SaaSLead.id.desc()).offset((page - 1) * per_page).limit(per_page).all()
    tasks = task_query.order_by(SaaSTask.updated_at.desc(), SaaSTask.id.desc()).limit(20).all()
    alerts = alert_query.order_by(SaaSAlert.updated_at.desc(), SaaSAlert.id.desc()).limit(20).all()

    lead_counts = {status: SaaSLead.query.filter(SaaSLead.status == status).count() for status in CRM_LEAD_STATUSES}
    task_counts = {status: SaaSTask.query.filter(SaaSTask.status == status).count() for status in CRM_TASK_STATUSES}
    alert_counts = {status: SaaSAlert.query.filter(SaaSAlert.status == status).count() for status in CRM_ALERT_STATUSES}

    try:
        from services.saas_commercial_whatsapp import get_commercial_company
        commercial_company = get_commercial_company()
        inbox_rows = _crm_inbox_rows(commercial_company, q="", channel="all", state="all", limit=200)
        recent_conversations = inbox_rows[:8]
        inbox_counts = {
            "total": len(inbox_rows),
            "pending": sum(1 for row in inbox_rows if row["needs_reply"]),
        }
    except Exception:
        current_app.logger.exception("No se pudo cargar la bandeja comercial para el Centro CRM.")
        recent_conversations = []
        inbox_counts = {"total": 0, "pending": 0}

    return render_template(
        "saas/crm.html",
        leads=leads,
        tasks=tasks,
        alerts=alerts,
        companies=companies,
        users=users,
        filters={"q": q, "lead_status": lead_status, "task_status": task_status, "alert_status": alert_status, "industry": industry_filter, "province": province_filter, "consent": consent_filter},
        lead_counts=lead_counts,
        task_counts=task_counts,
        alert_counts=alert_counts,
        recent_conversations=recent_conversations,
        inbox_counts=inbox_counts,
        CRM_LEAD_STATUSES=sorted(CRM_LEAD_STATUSES),
        CRM_TASK_STATUSES=sorted(CRM_TASK_STATUSES),
        CRM_ALERT_STATUSES=sorted(CRM_ALERT_STATUSES),
        CRM_PRIORITIES=sorted(CRM_PRIORITIES),
        lead_total=lead_total,
        lead_page=page,
        lead_per_page=per_page,
        lead_pages=max(1, (lead_total + per_page - 1) // per_page),
        gmail_oauth_configured=bool(os.getenv("GOOGLE_GMAIL_CLIENT_ID") and os.getenv("GOOGLE_GMAIL_CLIENT_SECRET")),
        gmail_refresh_configured=bool(os.getenv("GMAIL_COMMERCIAL_REFRESH_TOKEN")),
        gmail_pubsub_configured=bool(os.getenv("GMAIL_COMMERCIAL_PUBSUB_TOPIC") and os.getenv("GMAIL_COMMERCIAL_PUBSUB_SECRET")),
    )



@bp.route("/crm/import", methods=["GET", "POST"])
@superadmin_required
def crm_import():
    from app import db, record_audit
    from services.saas_commercial_service import import_prospect_rows, parse_prospect_file

    _require_superadmin()
    if request.method == "POST":
        uploaded = request.files.get("file")
        action = (request.form.get("action") or "preview").strip().lower()
        if uploaded is None or not uploaded.filename:
            flash("Seleccioná un archivo CSV o XLSX.", "danger")
            return redirect(url_for("saas.crm_import"))
        try:
            payload = uploaded.read()
            parsed = parse_prospect_file(payload, uploaded.filename)
        except Exception as exc:
            flash(f"No se pudo analizar el archivo: {exc}", "danger")
            return redirect(url_for("saas.crm_import"))

        if action == "preview":
            preview = {
                "filename": uploaded.filename,
                "valid_count": len(parsed["rows"]),
                "invalid_count": parsed["invalid_count"],
                "sample": parsed["rows"][:20],
            }
            return render_template("saas/crm_import.html", preview=preview)

        try:
            import_row = import_prospect_rows(
                db.session,
                rows=parsed["rows"],
                filename=uploaded.filename,
                user_id=current_user.id,
                source="import_publico",
            )
            import_row.invalid_count = parsed["invalid_count"]
            record_audit(
                action="saas_lead_import",
                entity="saas_lead_import",
                entity_id=import_row.id,
                detail=(
                    f"Importación {uploaded.filename}: insertados={import_row.inserted_count}; "
                    f"actualizados={import_row.updated_count}; duplicados={import_row.duplicate_count}; "
                    f"inválidos={import_row.invalid_count}."
                ),
                user_id=current_user.id,
            )
            db.session.commit()
            flash(
                f"Importación completada: {import_row.inserted_count} nuevos, "
                f"{import_row.updated_count} actualizados, {import_row.duplicate_count} duplicados.",
                "success",
            )
        except Exception as exc:
            db.session.rollback()
            flash(f"La importación no se completó: {exc}", "danger")
        return redirect(url_for("saas.crm_panel"))

    return render_template("saas/crm_import.html", preview=None)


@bp.get("/crm/leads/export")
@superadmin_required
def crm_leads_export():
    from app import SaaSLead
    from openpyxl import Workbook

    _require_superadmin()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Prospectos"
    sheet.append([
        "Comercio", "Contacto", "Rubro", "Subrubro", "Provincia", "Localidad",
        "Email", "Telefono", "WhatsApp", "Web", "Instagram", "Facebook",
        "Fuente", "URL Fuente", "Segmento", "Score", "Email Consentimiento",
        "WhatsApp Consentimiento", "Llamada Consentimiento", "No Contactar",
        "Estado", "Prioridad", "Ultimo Contacto", "Proximo Seguimiento",
    ])
    for lead in SaaSLead.query.order_by(SaaSLead.lead_score.desc(), SaaSLead.id.asc()).all():
        sheet.append([
            lead.company_name, lead.contact_name, lead.industry, lead.subindustry,
            lead.province, lead.locality, lead.email, lead.phone, lead.whatsapp,
            lead.website, lead.instagram, lead.facebook, lead.source, lead.source_url,
            lead.segment, lead.lead_score, lead.email_consent_status,
            lead.whatsapp_consent_status, lead.phone_consent_status,
            "SI" if lead.do_not_contact else "NO", lead.status, lead.priority,
            lead.last_contacted_at, lead.next_follow_up_at,
        ])
    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)
    return send_file(
        stream,
        as_attachment=True,
        download_name=f"stockarmobile_prospectos_{utcnow().strftime('%Y%m%d_%H%M')}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.route("/crm/campaigns", methods=["GET", "POST"])
@superadmin_required
def crm_campaigns():
    from app import SaaSCampaign, db, record_audit

    _require_superadmin()
    if request.method == "POST":
        from services.saas_commercial_service import segment_filters_from_request

        filters = segment_filters_from_request(request)
        channel = (request.form.get("channel") or "email").strip().lower()
        name = (request.form.get("name") or "").strip()[:180]
        subject = (request.form.get("subject") or "").strip()[:255]
        body_html = (request.form.get("body_html") or "").strip()
        body_text = (request.form.get("body_text") or "").strip() or None
        whatsapp_template_name = (request.form.get("whatsapp_template_name") or "").strip()[:120] or None
        whatsapp_template_language = (request.form.get("whatsapp_template_language") or "es_AR").strip()[:20] or "es_AR"
        whatsapp_parameter_fields = (request.form.get("whatsapp_parameter_fields") or "contacto,empresa").strip()[:500] or "contacto,empresa"
        if channel not in {"email", "whatsapp", "both"} or not name or not subject or not body_html:
            flash("Nombre, canal, asunto y HTML son obligatorios.", "danger")
            return redirect(url_for("saas.crm_campaigns"))
        campaign = SaaSCampaign(
            name=name, subject=subject, channel=channel, status="BORRADOR",
            body_html=body_html, body_text=body_text,
            whatsapp_template_name=whatsapp_template_name,
            whatsapp_template_language=whatsapp_template_language,
            whatsapp_parameter_fields=whatsapp_parameter_fields,
            segment_json=json.dumps(filters, ensure_ascii=False),
            scheduled_at=_parse_dt(request.form.get("scheduled_at")),
            created_by_user_id=current_user.id,
        )
        db.session.add(campaign)
        record_audit(
            action="saas_campaign_create", entity="saas_campaign",
            detail=f"Borrador comercial creado: {name}.", user_id=current_user.id,
        )
        db.session.commit()
        flash("Borrador de campaña creado.", "success")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign.id))

    campaigns = SaaSCampaign.query.order_by(SaaSCampaign.created_at.desc(), SaaSCampaign.id.desc()).limit(100).all()
    statuses = ["BORRADOR", "APROBADA", "ENVIANDO", "ENVIADA", "ENVIADA_PARCIAL", "FALLIDA", "SIN_ENVIO", "CANCELADA"]
    summary = {status: SaaSCampaign.query.filter_by(status=status).count() for status in statuses}
    summary["TOTAL"] = SaaSCampaign.query.count()
    summary["EN_CURSO"] = summary["APROBADA"] + summary["ENVIANDO"]
    summary["FINALIZADAS"] = (
        summary["ENVIADA"] + summary["ENVIADA_PARCIAL"] + summary["FALLIDA"] + summary["SIN_ENVIO"]
    )
    summary["INCIDENCIAS"] = summary["ENVIADA_PARCIAL"] + summary["FALLIDA"]
    marketing_send_enabled = str(current_app.config.get("SAAS_MARKETING_SEND_ENABLED", "0")).lower() in {"1", "true", "yes", "on"}
    smtp_configured = bool(current_app.config.get("SMTP_HOST") and current_app.config.get("SMTP_USER"))
    return render_template(
        "saas/crm_campaigns.html",
        campaigns=campaigns,
        summary=summary,
        marketing_send_enabled=marketing_send_enabled,
        smtp_configured=smtp_configured,
    )


@bp.get("/crm/campaigns/<int:campaign_id>")
@superadmin_required
def crm_campaign_detail(campaign_id):
    from app import SaaSCampaign, db
    from services.saas_commercial_service import campaign_audience_metrics, campaign_metrics
    from services.saas_campaign_preflight import campaign_preflight

    _require_superadmin()
    campaign = SaaSCampaign.query.filter_by(id=campaign_id).first_or_404()
    try:
        segment_filters = json.loads(campaign.segment_json or "{}")
    except (TypeError, ValueError):
        segment_filters = {}
    if not isinstance(segment_filters, dict):
        segment_filters = {}
    return render_template(
        "saas/crm_campaign_detail.html",
        campaign=campaign,
        metrics=campaign_metrics(db.session, campaign.id),
        audience=campaign_audience_metrics(db.session, campaign),
        segment_filters=segment_filters,
        campaign_scheduled_local=_format_admin_datetime_local(campaign.scheduled_at, "%Y-%m-%dT%H:%M"),
        marketing_send_enabled=str(current_app.config.get("SAAS_MARKETING_SEND_ENABLED", "0")).lower() in {"1", "true", "yes", "on"},
        smtp_configured=bool(current_app.config.get("SMTP_HOST") and current_app.config.get("SMTP_USER")),
        preflight=campaign_preflight(db.session, campaign),
    )


@bp.post("/crm/campaigns/<int:campaign_id>/prepare")
@superadmin_required
def crm_campaign_prepare(campaign_id):
    from app import SaaSCampaign, db, record_audit
    from services.saas_commercial_service import build_campaign_recipients

    _require_superadmin()
    try:
        result = build_campaign_recipients(db.session, campaign_id)
        from services.saas_commercial_service import campaign_audience_metrics

        campaign = db.session.get(SaaSCampaign, campaign_id)
        audience = campaign_audience_metrics(db.session, campaign)
        record_audit(
            action="saas_campaign_prepare", entity="saas_campaign", entity_id=campaign_id,
            detail=f"Destinatarios preparados: elegibles={result['eligible']} agregados={result['added']}.",
            user_id=current_user.id,
        )
        db.session.commit()
        if result["added"] > 0:
            flash(f"Audiencia preparada: {result['added']} destinatarios nuevos.", "success")
        elif audience["total"] == 0:
            flash(
                "No se agregaron destinatarios: ningún prospecto coincide con los filtros guardados. "
                "Revisá rubro, provincia, localidad y segmento.",
                "warning",
            )
        else:
            channel_parts = []
            if campaign.channel in {"email", "both"}:
                channel_parts.append(
                    f"Email: {audience['email_available']} con dirección, {audience['eligible_email']} elegibles para envío"
                )
            if campaign.channel in {"whatsapp", "both"}:
                channel_parts.append(
                    f"WhatsApp: {audience['whatsapp_available']} con número, {audience['eligible_whatsapp']} elegibles por consentimiento"
                )
            flash(
                "No se agregaron destinatarios elegibles. " + " · ".join(channel_parts) +
                ". Un dato importado no equivale a consentimiento de marketing.",
                "warning",
            )
    except Exception as exc:
        db.session.rollback()
        flash(f"No se pudo preparar la campaña: {exc}", "danger")
    return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))


@bp.route("/crm/campaigns/<int:campaign_id>/edit", methods=["POST"])
@superadmin_required
def crm_campaign_edit(campaign_id):
    from app import SaaSCampaign, SaaSCampaignEvent, SaaSCampaignRecipient, db, record_audit

    _require_superadmin()
    campaign = SaaSCampaign.query.filter_by(id=campaign_id).first_or_404()
    if campaign.status != "BORRADOR":
        flash("Solo se puede editar una campaña que todavía está en Borrador.", "warning")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))

    channel = (request.form.get("channel") or "email").strip().lower()
    name = (request.form.get("name") or "").strip()[:180]
    subject = (request.form.get("subject") or "").strip()[:255]
    body_html = (request.form.get("body_html") or "").strip()
    body_text = (request.form.get("body_text") or "").strip() or None
    whatsapp_template_name = (request.form.get("whatsapp_template_name") or "").strip()[:120] or None
    whatsapp_template_language = (request.form.get("whatsapp_template_language") or "es_AR").strip()[:20] or "es_AR"
    whatsapp_parameter_fields = (request.form.get("whatsapp_parameter_fields") or "contacto,empresa").strip()[:500] or "contacto,empresa"

    if channel not in {"email", "whatsapp", "both"} or not name or not subject or not body_html:
        flash("Nombre, canal, asunto y HTML son obligatorios.", "danger")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))

    from services.saas_commercial_service import segment_filters_from_request

    filters = segment_filters_from_request(request)
    scheduled_at = _parse_dt(request.form.get("scheduled_at"))

    old_snapshot = {
        "name": campaign.name,
        "channel": campaign.channel,
        "subject": campaign.subject,
        "scheduled_at": campaign.scheduled_at.isoformat() if campaign.scheduled_at else None,
        "segment_json": campaign.segment_json,
    }

    campaign.name = name
    campaign.subject = subject
    campaign.channel = channel
    campaign.body_html = body_html
    campaign.body_text = body_text
    campaign.whatsapp_template_name = whatsapp_template_name
    campaign.whatsapp_template_language = whatsapp_template_language
    campaign.whatsapp_parameter_fields = whatsapp_parameter_fields
    campaign.segment_json = json.dumps(filters, ensure_ascii=False)
    campaign.scheduled_at = scheduled_at

    # La audiencia queda obsoleta cuando cambia la campaña: obligar a
    # recalcular destinatarios evita enviar a una audiencia anterior.
    db.session.query(SaaSCampaignEvent).filter(
        SaaSCampaignEvent.campaign_id == campaign.id,
        SaaSCampaignEvent.event_type == "recipient_prepared",
    ).delete(synchronize_session=False)
    db.session.query(SaaSCampaignRecipient).filter(
        SaaSCampaignRecipient.campaign_id == campaign.id,
    ).delete(synchronize_session=False)
    campaign.target_count = 0
    campaign.sent_count = 0
    campaign.failed_count = 0
    campaign.skipped_count = 0
    campaign.replied_count = 0

    record_audit(
        action="saas_campaign_edit",
        entity="saas_campaign",
        entity_id=campaign.id,
        detail=f"Campaña editada. Antes={json.dumps(old_snapshot, ensure_ascii=False)}.",
        user_id=current_user.id,
    )
    db.session.commit()
    flash("Campaña actualizada. Ahora prepará nuevamente la audiencia.", "success")
    return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign.id))


@bp.post("/crm/campaigns/<int:campaign_id>/audience-all-email")
@superadmin_required
def crm_campaign_audience_all_email(campaign_id):
    from app import SaaSCampaign, db, record_audit

    _require_superadmin()
    campaign = SaaSCampaign.query.filter_by(id=campaign_id).first_or_404()
    if campaign.status != "BORRADOR":
        flash("Solo se puede cambiar la audiencia de un borrador.", "warning")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))
    if campaign.channel not in {"email", "both"}:
        flash("Esta acción requiere Email.", "warning")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))

    campaign.segment_json = json.dumps(
        {"industry": "", "province": "", "locality": "", "segment": "", "min_score": 0},
        ensure_ascii=False,
    )
    campaign.target_count = 0
    record_audit(
        action="saas_campaign_audience_scope",
        entity="saas_campaign",
        entity_id=campaign.id,
        detail="Audiencia configurada para toda la base de Email elegible.",
        user_id=current_user.id,
    )
    db.session.commit()
    flash("Audiencia configurada para toda la base de Email. Ahora prepará la audiencia.", "success")
    return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))


@bp.post("/crm/campaigns/<int:campaign_id>/approve")
@superadmin_required
def crm_campaign_approve(campaign_id):
    from app import SaaSCampaign, db, record_audit, utcnow

    campaign = SaaSCampaign.query.filter_by(id=campaign_id).first_or_404()
    if campaign.status != "BORRADOR":
        flash("Solo un borrador puede aprobarse.", "warning")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))

    from services.saas_campaign_preflight import campaign_preflight
    preflight = campaign_preflight(db.session, campaign)
    if not preflight["ready"]:
        first = preflight["issues"][0]
        flash(f"No se puede aprobar: {first['label']}. {first['detail']}", "warning")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))

    if not _require_superadmin_step_up():
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))
    if campaign.channel in {"whatsapp", "both"}:
        from app import SaaSCampaignRecipient
        whatsapp_targets = SaaSCampaignRecipient.query.filter_by(
            campaign_id=campaign.id, channel="whatsapp"
        ).count()
        if whatsapp_targets and not (campaign.whatsapp_template_name or "").strip():
            from services.ai_agent.config_service import get_whatsapp_connection
            from services.saas_commercial_whatsapp import get_commercial_company
            commercial_company = get_commercial_company()
            connection = get_whatsapp_connection(commercial_company) if commercial_company else {}
            if not connection.get("template_name"):
                flash("La campaña incluye WhatsApp pero no tiene una plantilla aprobada configurada.", "warning")
                return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))
    campaign.status = "APROBADA"
    campaign.approved_by_user_id = current_user.id
    campaign.approved_at = utcnow()
    record_audit(
        action="saas_campaign_approved", entity="saas_campaign", entity_id=campaign.id,
        detail=f"Campaña aprobada: {campaign.name}.", user_id=current_user.id,
    )
    db.session.commit()
    flash("Campaña aprobada. El worker respetará la elegibilidad vigente.", "success")
    return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))


@bp.post("/crm/campaigns/<int:campaign_id>/send-now")
@superadmin_required
def crm_campaign_send_now(campaign_id):
    from app import SaaSCampaign, db, record_audit, utcnow
    from services.saas_campaign_preflight import campaign_preflight
    from services.saas_commercial_service import dispatch_due_campaigns

    campaign = SaaSCampaign.query.filter_by(id=campaign_id).first_or_404()
    if campaign.status not in {"BORRADOR", "APROBADA"}:
        flash("Solo una campaña en Borrador o Aprobada puede enviarse ahora.", "warning")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))

    preflight = campaign_preflight(db.session, campaign)
    if not preflight["ready"]:
        first = preflight["issues"][0]
        flash(f"No se puede enviar: {first['label']}. {first['detail']}", "warning")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))

    if not _require_superadmin_step_up():
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))

    if campaign.status == "BORRADOR":
        campaign.status = "APROBADA"
        campaign.approved_by_user_id = current_user.id
        campaign.approved_at = utcnow()
        record_audit(
            action="saas_campaign_approved",
            entity="saas_campaign",
            entity_id=campaign.id,
            detail=f"Campaña aprobada para envío inmediato: {campaign.name}.",
            user_id=current_user.id,
        )
    campaign.scheduled_at = utcnow()
    db.session.commit()

    try:
        result = dispatch_due_campaigns(db.session, limit=1, per_campaign=50)
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Immediate commercial campaign dispatch failed")
        flash(f"La campaña quedó aprobada, pero el envío inmediato falló: {str(exc)[:300]}", "danger")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))

    record_audit(
        action="saas_campaign_send_now",
        entity="saas_campaign",
        entity_id=campaign.id,
        detail=f"Envío inmediato ejecutado: enviados={result.get('sent', 0)} fallidos={result.get('failed', 0)} omitidos={result.get('skipped', 0)}.",
        user_id=current_user.id,
    )
    db.session.commit()
    flash(
        f"Envío inmediato ejecutado: {result.get('sent', 0)} enviados, "
        f"{result.get('failed', 0)} fallidos, {result.get('skipped', 0)} omitidos.",
        "success" if not result.get("failed") else "warning",
    )
    return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))


@bp.post("/crm/campaigns/<int:campaign_id>/cancel")
@superadmin_required
def crm_campaign_cancel(campaign_id):
    from app import SaaSCampaign, db, record_audit

    if not _require_superadmin_step_up():
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))
    campaign = SaaSCampaign.query.filter_by(id=campaign_id).first_or_404()
    if campaign.status not in {"BORRADOR", "APROBADA", "ENVIANDO"}:
        flash("Esta campaña ya está finalizada.", "warning")
        return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))
    campaign.status = "CANCELADA"
    record_audit(
        action="saas_campaign_cancelled", entity="saas_campaign", entity_id=campaign.id,
        detail=f"Campaña cancelada: {campaign.name}.", user_id=current_user.id,
    )
    db.session.commit()
    flash("Campaña cancelada.", "success")
    return redirect(url_for("saas.crm_campaign_detail", campaign_id=campaign_id))


@bp.post("/internal/crm/campaign-worker")
@csrf.exempt
def crm_campaign_worker():
    """Authenticated machine endpoint for the Render commercial campaign cron."""
    from app import db
    expected = str(
        current_app.config.get("SAAS_CAMPAIGN_WORKER_TOKEN")
        or os.getenv("SAAS_CAMPAIGN_WORKER_TOKEN")
        or ""
    ).strip()
    supplied = str(request.headers.get("X-StockAr-Campaign-Worker-Token") or "").strip()
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    if str(current_app.config.get("SAAS_MARKETING_SEND_ENABLED", "0")).lower() not in {"1", "true", "yes", "on"}:
        return jsonify({"ok": False, "error": "commercial_marketing_disabled"}), 503

    try:
        from services.saas_commercial_service import dispatch_due_campaigns
        result = dispatch_due_campaigns(db.session)
        return jsonify({"ok": True, "result": result})
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Commercial campaign worker endpoint failed")
        return jsonify({"ok": False, "error": "worker_failed"}), 500


@bp.get("/crm/email/gmail/connect")
@superadmin_required
def crm_email_gmail_connect():
    """Start the one-time OAuth authorization for stockarmobile@gmail.com."""
    from services.gmail_commercial_service import authorization_url

    _require_superadmin()
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    session["gmail_commercial_oauth_state"] = state
    session["gmail_commercial_oauth_code_verifier"] = code_verifier
    try:
        return redirect(authorization_url(state, code_verifier))
    except RuntimeError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("saas.crm_panel"))


@bp.get("/crm/email/gmail/callback")
@superadmin_required
def crm_email_gmail_callback():
    """Exchange Gmail OAuth code and display the refresh token for Render."""
    from services.gmail_commercial_service import exchange_code

    _require_superadmin()
    state = (request.args.get("state") or "").strip()
    expected_state = session.pop("gmail_commercial_oauth_state", "")
    code_verifier = session.pop("gmail_commercial_oauth_code_verifier", "")
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        abort(400, description="Estado OAuth de Gmail inválido.")
    error = (request.args.get("error") or "").strip()
    if error:
        return current_app.response_class(
            f"Autorización de Gmail cancelada: {escape(error)}",
            mimetype="text/plain",
        ), 400
    code = (request.args.get("code") or "").strip()
    if not code:
        abort(400, description="Google no devolvió un código OAuth.")
    try:
        refresh_token = exchange_code(code, code_verifier)
    except Exception as exc:
        current_app.logger.exception("Gmail OAuth callback failed: %s", exc)
        return current_app.response_class(
            "No se pudo completar la autorización de Gmail. Revisá la configuración OAuth y los logs.",
            mimetype="text/plain",
        ), 502

    safe_token = escape(refresh_token)
    html = (
        "<h1>Gmail autorizado</h1>"
        "<p>Cuenta: <strong>stockarmobile@gmail.com</strong></p>"
        "<p>Guardá este refresh token como secreto de Render en "
        "<code>GMAIL_COMMERCIAL_REFRESH_TOKEN</code>. No lo publiques ni lo compartas.</p>"
        f"<textarea style='width:100%;height:120px'>{safe_token}</textarea>"
        "<p>Después de cargarlo en Render, configurá el topic de Google Cloud Pub/Sub "
        "y ejecutá la renovación de watch desde el CRM.</p>"
    )
    return current_app.response_class(html, mimetype="text/html")


@bp.post("/crm/email/gmail/watch")
@superadmin_required
def crm_email_gmail_watch():
    from services.gmail_commercial_service import renew_watch

    _require_superadmin()
    try:
        result = renew_watch()
    except Exception as exc:
        current_app.logger.exception("Gmail watch renewal failed: %s", exc)
        flash(f"No se pudo activar la escucha de Gmail: {exc}", "danger")
        return redirect(url_for("saas.crm_panel"))
    flash(
        f"Gmail conectado. Watch activo hasta {result.get('expiration') or 'la fecha informada por Google'}.",
        "success",
    )
    return redirect(url_for("saas.crm_panel"))


@bp.post("/crm/email/gmail/sync")
@superadmin_required
def crm_email_gmail_sync():
    from services.gmail_commercial_service import sync_recent_messages

    _require_superadmin()
    try:
        result = sync_recent_messages(hours=48)
    except Exception as exc:
        current_app.logger.exception("Gmail manual sync failed: %s", exc)
        flash(f"No se pudo sincronizar Gmail: {exc}", "danger")
        return redirect(url_for("saas.crm_panel"))
    flash(
        f"Gmail sincronizado: {result.get('received', 0)} nuevos, "
        f"{result.get('duplicates', 0)} duplicados.",
        "success",
    )
    return redirect(url_for("saas.crm_panel"))


@bp.post("/crm/email/gmail/pubsub")
@csrf.exempt
def crm_email_gmail_pubsub():
    """Receive Gmail change notifications delivered by Google Cloud Pub/Sub."""
    authorization = str(request.headers.get("Authorization") or "").strip()
    if not authorization.lower().startswith("bearer "):
        abort(401)
    token = authorization.split(" ", 1)[1].strip()
    audience = (
        current_app.config.get("GMAIL_COMMERCIAL_PUBSUB_AUDIENCE")
        or os.getenv(
            "GMAIL_COMMERCIAL_PUBSUB_AUDIENCE",
            "https://www.stockarmobile.com/superadmin/crm/email/gmail/pubsub",
        )
    ).strip()
    expected_service_account = str(
        current_app.config.get("GMAIL_COMMERCIAL_PUBSUB_SERVICE_ACCOUNT")
        or os.getenv("GMAIL_COMMERCIAL_PUBSUB_SERVICE_ACCOUNT")
        or ""
    ).strip().lower()
    if not expected_service_account:
        abort(503, description="Gmail Pub/Sub service account no configurada.")
    try:
        from google.auth.transport import requests as google_auth_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(
            token,
            google_auth_requests.Request(),
            audience=audience,
        )
        token_email = str(claims.get("email") or "").strip().lower()
        if token_email != expected_service_account or claims.get("email_verified") is not True:
            abort(403)
    except Exception:
        current_app.logger.exception("Gmail Pub/Sub OIDC authentication failed.")
        abort(401)

    payload = request.get_json(silent=True) or {}
    try:
        from services.gmail_commercial_service import process_pubsub_notification

        result = process_pubsub_notification(payload)
    except Exception as exc:
        current_app.logger.exception("Gmail Pub/Sub processing failed: %s", exc)
        return {"success": False, "error": "No se pudo procesar la notificación."}, 500
    return {"success": True, **result}, 200


@bp.post("/crm/email/inbound")
def crm_email_inbound():
    """Webhook for replies to the commercial acquisition mailbox."""
    expected = str(current_app.config.get("SAAS_CRM_EMAIL_INBOUND_SECRET") or current_app.config.get("CRM_EMAIL_INBOUND_SECRET") or os.getenv("SAAS_CRM_EMAIL_INBOUND_SECRET") or os.getenv("CRM_EMAIL_INBOUND_SECRET") or "").strip()
    provided = str(request.headers.get("X-CRM-Email-Secret") or request.args.get("secret") or "").strip()
    if not expected or not provided or not secrets.compare_digest(provided, expected):
        abort(401)

    payload = request.get_json(silent=True) or request.form
    sender = str(payload.get("from") or payload.get("sender") or payload.get("sender_email") or "").strip()
    if "<" in sender and ">" in sender:
        sender = sender.rsplit("<", 1)[1].split(">", 1)[0].strip()
    subject = str(payload.get("subject") or "").strip()
    text = str(payload.get("text") or payload.get("text_body") or payload.get("body") or "").strip()
    html = str(payload.get("html") or payload.get("html_body") or "").strip()
    message_id = str(payload.get("message_id") or payload.get("Message-Id") or payload.get("id") or "").strip()
    recipient = str(payload.get("to") or payload.get("recipient") or payload.get("recipient_email") or "").strip()

    from services.saas_commercial_service import capture_inbound_email
    try:
        result = capture_inbound_email(sender_email=sender, subject=subject, text=text, html=html, external_message_id=message_id, recipient_email=recipient)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}, 400
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}, 503
    return {"success": True, **result}, 200


@bp.get("/crm/email/open/<tracking_token>")
def crm_email_open(tracking_token):
    from app import SaaSCampaignEvent, SaaSCampaignRecipient, db, utcnow

    recipient = SaaSCampaignRecipient.query.filter_by(tracking_token=(tracking_token or "").strip()).first()
    if recipient is not None and recipient.opened_at is None:
        now = utcnow()
        recipient.opened_at = now
        recipient.provider_status = recipient.provider_status or "opened"
        db.session.add(SaaSCampaignEvent(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            event_type="opened",
            metadata_json=json.dumps({"channel": "email"}, ensure_ascii=False),
            created_at=now,
        ))
        db.session.commit()

    pixel = b"\\x47\\x49\\x46\\x38\\x39\\x61\\x01\\x00\\x01\\x00\\x80\\x00\\x00\\x00\\x00\\x00\\xff\\xff\\xff\\x21\\xf9\\x04\\x01\\x00\\x00\\x00\\x00\\x2c\\x00\\x00\\x00\\x00\\x01\\x00\\x01\\x00\\x00\\x02\\x02\\x44\\x01\\x00\\x3b"
    return current_app.response_class(pixel, mimetype="image/gif")


@bp.get("/crm/email/click/<tracking_token>")
def crm_email_click(tracking_token):
    from app import SaaSCampaignEvent, SaaSCampaignRecipient, db, utcnow

    target = (request.args.get("url") or "").strip()
    recipient = SaaSCampaignRecipient.query.filter_by(tracking_token=(tracking_token or "").strip()).first()
    if recipient is None or not target:
        abort(404)
    parsed = urlparse(target)
    allowed_hosts = {
        current_app.config.get("APP_URL", "").replace("https://", "").replace("http://", "").split("/", 1)[0],
        request.host,
    }
    if parsed.scheme not in {"http", "https"} or parsed.netloc not in {host for host in allowed_hosts if host}:
        abort(400)
    if recipient.clicked_at is None:
        now = utcnow()
        recipient.clicked_at = now
        recipient.provider_status = recipient.provider_status or "clicked"
        db.session.add(SaaSCampaignEvent(
            campaign_id=recipient.campaign_id,
            recipient_id=recipient.id,
            event_type="clicked",
            metadata_json=json.dumps({"channel": "email", "target": target[:1000]}, ensure_ascii=False),
            created_at=now,
        ))
        db.session.commit()
    return redirect(target)


@bp.route("/crm/unsubscribe/<token>", methods=["GET", "POST"])
def crm_unsubscribe(token):
    from app import SaaSLeadConsent, db, utcnow

    consent = SaaSLeadConsent.query.filter_by(unsubscribe_token=(token or "").strip()).first()
    if consent is None:
        return render_template("saas/crm_unsubscribe.html", ok=False), 404
    lead = consent.lead
    consent.email_status = "opted_out"
    consent.revoked_at = consent.revoked_at or utcnow()
    lead.email_consent_status = "opted_out"
    db.session.commit()
    return render_template("saas/crm_unsubscribe.html", ok=True, company_name=lead.company_name)


@bp.post("/crm/leads/<int:lead_id>/consent-link")
@superadmin_required
def crm_lead_consent_link(lead_id):
    from app import SaaSLead, SaaSLeadConsent, db, utcnow, record_audit

    _require_superadmin()
    lead = SaaSLead.query.filter_by(id=lead_id).first_or_404()
    consent = lead.consent or SaaSLeadConsent(lead_id=lead.id)
    if not consent.consent_token:
        consent.consent_token = secrets.token_urlsafe(48)
    db.session.add(consent)
    record_audit(
        action="saas_lead_consent_link_generated",
        entity="saas_lead",
        entity_id=lead.id,
        detail="Enlace individual de consentimiento generado o recuperado.",
        user_id=current_user.id,
        company_id=lead.company_id,
    )
    db.session.commit()
    flash("Enlace de consentimiento generado. Podés copiarlo desde la ficha del prospecto.", "success")
    return _redirect_back("saas.crm_panel")


@bp.route("/crm/consent/<token>", methods=["GET", "POST"])
def crm_consent(token):
    from app import SaaSLeadConsent, db, utcnow, record_audit

    consent = SaaSLeadConsent.query.filter_by(consent_token=(token or "").strip()).first()
    if consent is None or not consent.lead:
        return render_template("saas/crm_consent.html", ok=False), 404

    lead = consent.lead
    if request.method == "GET":
        return render_template(
            "saas/crm_consent.html",
            ok=True,
            lead=lead,
            consent=consent,
            saved=False,
        )

    email_opted_in = request.form.get("email_opted_in") == "1"
    whatsapp_opted_in = request.form.get("whatsapp_opted_in") == "1"
    phone_opted_in = request.form.get("phone_opted_in") == "1"
    reject_all = request.form.get("reject_all") == "1"

    if reject_all:
        consent.email_status = "opted_out"
        consent.whatsapp_status = "opted_out"
        consent.phone_status = "opted_out"
        lead.email_consent_status = "opted_out"
        lead.whatsapp_consent_status = "opted_out"
        lead.phone_consent_status = "opted_out"
        lead.do_not_contact = True
        lead.do_not_contact_at = utcnow()
        consent.revoked_at = utcnow()
    else:
        # The form represents the prospect's complete current preferences:
        # checked means opted-in; unchecked means opted-out for that channel.
        consent.email_status = "opted_in" if email_opted_in else "opted_out"
        consent.whatsapp_status = "opted_in" if whatsapp_opted_in else "opted_out"
        consent.phone_status = "opted_in" if phone_opted_in else "opted_out"
        lead.email_consent_status = consent.email_status
        lead.whatsapp_consent_status = consent.whatsapp_status
        lead.phone_consent_status = consent.phone_status
        consent.email_source = "public_consent_link" if email_opted_in else consent.email_source
        consent.whatsapp_source = "public_consent_link" if whatsapp_opted_in else consent.whatsapp_source
        now = utcnow()
        if email_opted_in or whatsapp_opted_in or phone_opted_in:
            consent.granted_at = now
            consent.revoked_at = None
            lead.do_not_contact = False
            lead.do_not_contact_at = None
        else:
            consent.revoked_at = now
            lead.do_not_contact = True
            lead.do_not_contact_at = now

    record_audit(
        action="saas_lead_public_consent_update",
        entity="saas_lead",
        entity_id=lead.id,
        detail=(
            f"Consentimiento público actualizado: email={lead.email_consent_status}; "
            f"whatsapp={lead.whatsapp_consent_status}; llamada={lead.phone_consent_status}; "
            f"no_contactar={lead.do_not_contact}."
        ),
        company_id=lead.company_id,
        ip_address=request.remote_addr,
    )
    db.session.commit()
    return render_template("saas/crm_consent.html", ok=True, lead=lead, consent=consent, saved=True)


@bp.post("/crm/leads/<int:lead_id>/contact-preferences")
@superadmin_required
def crm_lead_contact_preferences(lead_id):
    from app import SaaSLead, SaaSLeadConsent, db, utcnow, record_audit

    _require_superadmin()
    lead = SaaSLead.query.filter_by(id=lead_id).first_or_404()
    consent = lead.consent or SaaSLeadConsent(lead_id=lead.id)
    consent.email_status = (request.form.get("email_consent_status") or "unknown").strip().lower()
    consent.whatsapp_status = (request.form.get("whatsapp_consent_status") or "unknown").strip().lower()
    consent.phone_status = (request.form.get("phone_consent_status") or "unknown").strip().lower()
    if consent.email_status not in {"opted_in", "opted_out", "unknown"}:
        consent.email_status = "unknown"
    if consent.whatsapp_status not in {"opted_in", "opted_out", "unknown"}:
        consent.whatsapp_status = "unknown"
    if consent.phone_status not in {"opted_in", "opted_out", "unknown"}:
        consent.phone_status = "unknown"
    lead.email_consent_status = consent.email_status
    lead.whatsapp_consent_status = consent.whatsapp_status
    lead.phone_consent_status = consent.phone_status
    if not consent.unsubscribe_token:
        consent.unsubscribe_token = __import__("secrets").token_urlsafe(48)

    manual_reason = (request.form.get("consent_reason") or "").strip()[:500]
    has_opt_in = "opted_in" in {
        consent.email_status,
        consent.whatsapp_status,
        consent.phone_status,
    }
    if has_opt_in and len(manual_reason) < 5:
        flash("Para registrar un permiso manual necesitás indicar el motivo o cómo fue obtenido.", "warning")
        return _redirect_back("saas.crm_panel")

    now = utcnow()
    if consent.email_status == "opted_out" or consent.whatsapp_status == "opted_out" or consent.phone_status == "opted_out":
        consent.revoked_at = now
    if has_opt_in:
        consent.granted_at = now
        consent.revoked_at = None
        if consent.email_status == "opted_in":
            consent.email_source = "superadmin_manual"
        if consent.whatsapp_status == "opted_in":
            consent.whatsapp_source = "superadmin_manual"
    if request.form.get("do_not_contact") == "1":
        lead.do_not_contact = True
        lead.do_not_contact_at = lead.do_not_contact_at or utcnow()
    else:
        lead.do_not_contact = False
        lead.do_not_contact_at = None
    db.session.add(consent)
    record_audit(
        action="saas_lead_contact_preferences_update",
        entity="saas_lead",
        entity_id=lead.id,
        detail=(
            f"Preferencias actualizadas manualmente: email={lead.email_consent_status}; "
            f"whatsapp={lead.whatsapp_consent_status}; llamada={lead.phone_consent_status}; "
            f"no_contactar={lead.do_not_contact}; motivo={manual_reason or 'sin motivo informado'}."
        ),
        user_id=current_user.id,
    )
    db.session.commit()
    flash("Preferencias de contacto actualizadas.", "success")
    return _redirect_back("saas.crm_panel")


@bp.post("/crm/leads/bulk-contact-preferences")
@superadmin_required
def crm_leads_bulk_contact_preferences():
    from app import SaaSLead, SaaSLeadConsent, db, record_audit, utcnow

    _require_superadmin()
    action = (request.form.get("action") or "").strip().lower()
    if action not in {"opt_in_email", "opt_in_whatsapp", "opt_in_both", "opt_out_all"}:
        flash("Acción de consentimiento no válida.", "danger")
        return _redirect_back("saas.crm_panel")

    raw_ids = request.form.getlist("lead_ids")
    ids = []
    for raw_id in raw_ids:
        try:
            value = int(raw_id)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    ids = ids[:100]
    if not ids:
        flash("Seleccioná al menos un prospecto.", "warning")
        return _redirect_back("saas.crm_panel")

    reason = (request.form.get("consent_reason") or "").strip()[:500]
    if len(reason) < 5:
        flash("El motivo es obligatorio para dejar trazabilidad del cambio manual.", "warning")
        return _redirect_back("saas.crm_panel")

    if not _require_superadmin_step_up():
        return _redirect_back("saas.crm_panel")

    leads = SaaSLead.query.filter(SaaSLead.id.in_(ids)).order_by(SaaSLead.id.asc()).all()
    now = utcnow()
    changed = 0
    for lead in leads:
        consent = lead.consent or SaaSLeadConsent(lead_id=lead.id)
        if not consent.unsubscribe_token:
            consent.unsubscribe_token = secrets.token_urlsafe(48)

        if action in {"opt_in_email", "opt_in_both"}:
            lead.email_consent_status = "opted_in"
            consent.email_status = "opted_in"
            consent.email_source = "superadmin_manual"
        if action in {"opt_in_whatsapp", "opt_in_both"}:
            lead.whatsapp_consent_status = "opted_in"
            consent.whatsapp_status = "opted_in"
            consent.whatsapp_source = "superadmin_manual"
        if action == "opt_out_all":
            lead.email_consent_status = "opted_out"
            lead.whatsapp_consent_status = "opted_out"
            lead.phone_consent_status = "opted_out"
            consent.email_status = "opted_out"
            consent.whatsapp_status = "opted_out"
            consent.phone_status = "opted_out"
            consent.revoked_at = now
        if action != "opt_out_all":
            consent.granted_at = now
            consent.revoked_at = None

        db.session.add(consent)
        changed += 1
        record_audit(
            action="saas_lead_manual_consent_bulk",
            entity="saas_lead",
            entity_id=lead.id,
            detail=f"Consentimiento manual: accion={action}; motivo={reason}.",
            user_id=current_user.id,
            company_id=lead.company_id,
        )

    db.session.commit()
    flash(f"Consentimiento manual actualizado en {changed} prospecto(s). Ahora podés preparar la audiencia.", "success")
    return _redirect_back("saas.crm_panel")


@bp.post("/crm/leads/<int:lead_id>/delete")
@superadmin_required
def crm_lead_delete(lead_id):
    from app import SaaSLead, db, record_audit
    from services.saas_commercial_service import delete_saas_lead

    if not _require_superadmin_step_up():
        return _redirect_back("saas.crm_panel")

    lead = SaaSLead.query.filter_by(id=lead_id).first_or_404()
    lead_company_id = lead.company_id
    try:
        result = delete_saas_lead(db.session, lead_id, suppressing_user_id=current_user.id)
        record_audit(
            action="saas_lead_delete",
            entity="saas_lead",
            entity_id=lead_id,
            detail=(
                f"Prospecto CRM {lead_id} eliminado permanentemente. "
                f"tareas={result['tasks_deleted']}; alertas={result['alerts_deleted']}; "
                f"destinatarios={result['campaign_recipients_deleted']}; "
                f"consentimientos={result['consents_deleted']}; "
                f"eventos_campaña_eliminados={result.get('campaign_events_deleted', 0)}; "
                f"checkouts_desvinculados={result['checkouts_detached']}."
            ),
            user_id=current_user.id,
            company_id=lead_company_id,
        )
        db.session.commit()
        flash(
            "Prospecto eliminado. El historial de cobros queda conservado y desvinculado del CRM."
            if result["checkouts_detached"]
            else "Prospecto eliminado permanentemente.",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("No se pudo eliminar el prospecto CRM id=%s", lead_id)
        flash("No se pudo eliminar el prospecto. No se realizaron cambios.", "danger")
    return _redirect_back("saas.crm_panel")


@bp.route("/crm/leads/<int:lead_id>/status", methods=["POST"])
@superadmin_required
def crm_lead_status(lead_id):
    from app import SaaSLead, db, record_audit

    _require_superadmin()
    lead = SaaSLead.query.filter_by(id=lead_id).first_or_404()
    status = _normalize_crm_value(request.form.get("status"), CRM_LEAD_STATUSES, lead.status)
    lead.status = status
    lead.converted_at = utcnow() if status == "ganado" else None
    record_audit(
        action="saas_lead_status_update",
        entity="saas_lead",
        entity_id=lead.id,
        detail=f"Estado actualizado a {status}.",
        user_id=current_user.id,
        company_id=lead.company_id,
    )
    db.session.commit()
    flash("Estado del prospecto actualizado.", "success")
    return _redirect_back("saas.crm_panel")


@bp.route("/crm/tasks/<int:task_id>/status", methods=["POST"])
@superadmin_required
def crm_task_status(task_id):
    from app import SaaSTask, db, record_audit

    _require_superadmin()
    task = SaaSTask.query.filter_by(id=task_id).first_or_404()
    status = _normalize_crm_value(request.form.get("status"), CRM_TASK_STATUSES, task.status)
    task.status = status
    task.completed_at = utcnow() if status == "hecha" else None
    record_audit(
        action="saas_task_status_update",
        entity="saas_task",
        entity_id=task.id,
        detail=f"Estado actualizado a {status}.",
        user_id=current_user.id,
        company_id=task.company_id,
    )
    db.session.commit()
    flash("Estado de la tarea actualizado.", "success")
    return _redirect_back("saas.crm_panel")


@bp.route("/crm/alerts/<int:alert_id>/status", methods=["POST"])
@superadmin_required
def crm_alert_status(alert_id):
    from app import SaaSAlert, db, record_audit

    _require_superadmin()
    alert = SaaSAlert.query.filter_by(id=alert_id).first_or_404()
    status = _normalize_crm_value(request.form.get("status"), CRM_ALERT_STATUSES, alert.status)
    alert.status = status
    if status in {"revisada", "resuelta"}:
        alert.acknowledged_at = alert.acknowledged_at or utcnow()
    if status == "resuelta":
        alert.resolved_at = utcnow()
    else:
        alert.resolved_at = None if status == "abierta" else alert.resolved_at
    record_audit(
        action="saas_alert_status_update",
        entity="saas_alert",
        entity_id=alert.id,
        detail=f"Estado actualizado a {status}.",
        user_id=current_user.id,
        company_id=alert.company_id,
    )
    db.session.commit()
    flash("Estado de la alerta actualizado.", "success")
    return _redirect_back("saas.crm_panel")


@bp.route("/mercado-pago")
@superadmin_required
def mercado_pago_connections():
    from app import Company
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    service = MercadoPagoOAuthService()
    companies = Company.query.order_by(Company.created_at.desc()).all()
    rows = []
    for company in companies:
        rows.append({
            "company": company,
            "connection": service.summarize_connection(getattr(company, "mercadopago_connection", None)),
        })
    return render_template("saas/mercado_pago_connections.html", rows=rows)


@bp.route("/companies/<int:company_id>/toggle", methods=["POST"])
@superadmin_required
def toggle_company(company_id):
    from app import AuditLog, Company, User, db

    _require_superadmin()
    if not _require_superadmin_step_up():
        return _redirect_back("saas.companies_panel" )
    company = db.session.get(Company, company_id)
    if company is None:
        abort(404)
    company.active = not company.active
    User.query.filter_by(company_id=company.id).update({User.active: company.active}, synchronize_session=False)
    db.session.add(
        AuditLog(
            user_id=current_user.id,
            action="toggle_company",
            entity="company",
            entity_id=company.id,
            detail=f"Empresa {'reactivada' if company.active else 'suspendida'} desde Superadmin",
        )
    )
    db.session.commit()
    flash(f"Empresa {company.name} {'reactivada' if company.active else 'suspendida'}.", "success")
    return _redirect_back("saas.companies_panel")


@bp.route("/ai-subscriptions")
@superadmin_required
def ai_subscriptions_panel():
    from app import Company
    from services.ai_agent.subscription_service import AISubscriptionService
    from services.ai_agent.profitability_service import profitability_snapshot
    from services.ai_agent.usage_service import AI_PLANS

    _require_superadmin()
    q = (request.args.get("q") or "").strip()
    company_id = request.args.get("company_id", type=int)
    query = Company.query.order_by(Company.name.asc())
    if company_id:
        # Navegacion directa desde el Gestor de empresas: el id siempre viene del backend (url_for), nunca de un input libre.
        query = query.filter(Company.id == company_id)
    elif q:
        query = query.filter(Company.name.ilike(f"%{q}%"))

    rows = []
    profitability_totals = {"companies": 0, "plan_list_price_ars": 0.0, "estimated_cost_usd": 0.0, "estimated_cost_ars": 0.0, "estimated_gross_contribution_ars": 0.0, "cost_ars_available": True, "pricing_available": True}
    for company in query.all():
        status = AISubscriptionService.get_status(company)
        profitability = profitability_snapshot(company)
        rows.append({"company": company, "status": status, "profitability": profitability})
        if profitability.get("plan_code"):
            profitability_totals["companies"] += 1
            profitability_totals["plan_list_price_ars"] += float(profitability.get("plan_list_price_ars") or 0)
            profitability_totals["estimated_cost_usd"] += float(profitability.get("estimated_cost_usd") or 0)
            if profitability.get("status") == "missing_provider_pricing":
                profitability_totals["pricing_available"] = False
            cost_ars = profitability.get("estimated_cost_ars")
            contribution_ars = profitability.get("estimated_gross_contribution_ars")
            if cost_ars is None or contribution_ars is None:
                profitability_totals["cost_ars_available"] = False
            else:
                profitability_totals["estimated_cost_ars"] += float(cost_ars)
                profitability_totals["estimated_gross_contribution_ars"] += float(contribution_ars)

    total_revenue = profitability_totals["plan_list_price_ars"]
    total_cost = profitability_totals["estimated_cost_ars"]
    profitability_totals["estimated_margin_percent"] = round(((total_revenue - total_cost) / total_revenue) * 100, 2) if profitability_totals["cost_ars_available"] and total_revenue > 0 else None
    return render_template(
        "saas/ai_subscriptions.html",
        rows=rows,
        ai_plans=AI_PLANS,
        profitability_totals=profitability_totals,
        filters={"q": q, "company_id": company_id},
    )

@bp.route("/ai-profitability")
@superadmin_required
def ai_profitability_panel():
    """Global current-period economics and measured AI cost center for Super Admin."""
    from app import Company
    from services.ai_agent.profitability_service import configured_usd_to_ars, profitability_snapshot
    from services.ai_agent.usage_service import AGENT_LABELS

    _require_superadmin()
    companies = Company.query.order_by(Company.name.asc()).all()
    fx = configured_usd_to_ars()
    period = None
    totals = {
        "companies_with_ai": 0,
        "companies_with_usage": 0,
        "plan_list_price_ars": 0.0,
        "estimated_cost_usd": 0.0,
        "estimated_cost_ars": 0.0,
        "estimated_gross_contribution_ars": 0.0,
        "pricing_available": True,
        "cost_ars_available": True,
        "priced_interactions": 0,
        "unpriced_interactions": 0,
    }
    by_provider = {}
    by_provider_model = {}
    by_model = {}
    by_agent = {}
    company_rows = []

    for company in companies:
        profitability = profitability_snapshot(company)
        usage = profitability.get("usage") or {}
        period = period or usage.get("period") or profitability.get("period")
        interactions = int(usage.get("priced_interactions") or 0) + int(usage.get("unpriced_interactions") or 0)
        if profitability.get("plan_code"):
            totals["companies_with_ai"] += 1
            totals["plan_list_price_ars"] += float(profitability.get("plan_list_price_ars") or 0)
        if interactions:
            totals["companies_with_usage"] += 1

        totals["estimated_cost_usd"] += float(usage.get("estimated_cost_usd") or 0)
        totals["priced_interactions"] += int(usage.get("priced_interactions") or 0)
        totals["unpriced_interactions"] += int(usage.get("unpriced_interactions") or 0)
        if profitability.get("status") == "missing_provider_pricing":
            totals["pricing_available"] = False
        if profitability.get("estimated_cost_ars") is None and profitability.get("plan_code"):
            totals["cost_ars_available"] = False
        else:
            totals["estimated_cost_ars"] += float(profitability.get("estimated_cost_ars") or 0)
            totals["estimated_gross_contribution_ars"] += float(profitability.get("estimated_gross_contribution_ars") or 0)

        for key, value in (usage.get("by_provider") or {}).items():
            by_provider[key] = by_provider.get(key, 0.0) + float(value or 0)
        for key, value in (usage.get("by_provider_model") or {}).items():
            by_provider_model[key] = by_provider_model.get(key, 0.0) + float(value or 0)
        for key, value in (usage.get("by_model") or {}).items():
            by_model[key] = by_model.get(key, 0.0) + float(value or 0)
        for key, value in (usage.get("by_agent") or {}).items():
            by_agent[key] = by_agent.get(key, 0.0) + float(value or 0)

        if profitability.get("plan_code") or interactions:
            company_rows.append({
                "company": company,
                "plan_name": profitability.get("plan_name"),
                "plan_code": profitability.get("plan_code"),
                "revenue_ars": float(profitability.get("plan_list_price_ars") or 0),
                "cost_usd": float(usage.get("estimated_cost_usd") or 0),
                "cost_ars": profitability.get("estimated_cost_ars"),
                "margin_percent": profitability.get("estimated_margin_percent"),
                "status": profitability.get("status"),
                "priced_interactions": int(usage.get("priced_interactions") or 0),
                "unpriced_interactions": int(usage.get("unpriced_interactions") or 0),
                "tokens": int(usage.get("total_tokens") or 0),
            })

    total_interactions = totals["priced_interactions"] + totals["unpriced_interactions"]
    totals["pricing_coverage_percent"] = round((totals["priced_interactions"] / total_interactions) * 100, 2) if total_interactions else 100.0
    totals["estimated_margin_percent"] = (
        round((totals["estimated_gross_contribution_ars"] / totals["plan_list_price_ars"]) * 100, 2)
        if totals["cost_ars_available"] and totals["plan_list_price_ars"] > 0
        else None
    )
    totals["usd_to_ars"] = float(fx) if fx > 0 else None

    def _sorted_rows(values):
        return sorted(
            [{"name": key, "cost_usd": round(float(value or 0), 8)} for key, value in values.items()],
            key=lambda item: (-item["cost_usd"], item["name"]),
        )

    provider_rows = _sorted_rows(by_provider)
    model_rows = _sorted_rows(by_model)
    provider_model_rows = _sorted_rows(by_provider_model)
    agent_rows = []
    for key, value in sorted(by_agent.items(), key=lambda item: (-float(item[1] or 0), item[0])):
        agent_rows.append({
            "name": AGENT_LABELS.get(key, key.replace("_", " ").title()),
            "key": key,
            "cost_usd": round(float(value or 0), 8),
        })
    company_rows.sort(key=lambda item: (-item["cost_usd"], item["company"].name.lower()))

    warnings = []
    if totals["unpriced_interactions"]:
        warnings.append(f'{totals["unpriced_interactions"]} interacciones no tienen pricing de proveedor completo.')
    if totals["plan_list_price_ars"] and not totals["cost_ars_available"]:
        warnings.append("No se puede calcular el margen ARS de todas las empresas hasta completar pricing y/o cotización USD→ARS.")
    if not fx:
        warnings.append("AI_USD_TO_ARS no está configurada; los costos en ARS y el margen quedan sin calcular.")

    return render_template(
        "saas/ai_profitability.html",
        period=period or "sin datos",
        totals=totals,
        provider_rows=provider_rows,
        model_rows=model_rows,
        provider_model_rows=provider_model_rows,
        agent_rows=agent_rows,
        company_rows=company_rows,
        warnings=warnings,
    )


@bp.route("/ai-subscriptions/<int:company_id>")
@superadmin_required
def ai_subscription_detail(company_id):
    """Centro de control IA de una empresa puntual: unico lugar con las acciones de AISubscriptionService."""
    from app import Company
    from services.ai_agent.subscription_service import AISubscriptionService
    from services.ai_agent.usage_service import AI_PLANS

    _require_superadmin()
    company = Company.query.get(company_id)
    if company is None:
        abort(404)
    status = AISubscriptionService.get_status(company)
    return render_template("saas/ai_subscription_detail.html", company=company, status=status, ai_plans=AI_PLANS)


@bp.route("/ai-subscriptions/<int:company_id>/action", methods=["POST"])
@superadmin_required
def ai_subscriptions_action(company_id):
    from app import Company
    from services.ai_agent.subscription_service import AISubscriptionError, AISubscriptionService

    _require_superadmin()
    company = Company.query.get(company_id)
    if company is None:
        abort(404)
    if not _require_superadmin_step_up():
        return redirect(url_for("saas.ai_subscription_detail", company_id=company.id))
    action = (request.form.get("action") or "").strip().lower()
    try:
        if action == "assign_plan":
            AISubscriptionService.assign_plan(company, plan_code=request.form.get("plan_code"), admin_user_id=current_user.id)
        elif action == "activate":
            AISubscriptionService.activate(company, admin_user_id=current_user.id)
        elif action == "suspend":
            AISubscriptionService.suspend(company, admin_user_id=current_user.id, reason=request.form.get("reason"))
        elif action == "reactivate":
            AISubscriptionService.reactivate(company, admin_user_id=current_user.id)
        elif action == "cancel":
            AISubscriptionService.cancel(company, admin_user_id=current_user.id, reason=request.form.get("reason"))
        elif action == "grant_trial":
            AISubscriptionService.grant_trial(company, plan_code=request.form.get("plan_code"), days=request.form.get("days"), admin_user_id=current_user.id, reason=request.form.get("reason"))
        elif action == "renew":
            AISubscriptionService.renew(company, admin_user_id=current_user.id, days=request.form.get("days"))
        elif action == "set_expiry":
            AISubscriptionService.set_expiry(company, ends_at=request.form.get("ends_at"), admin_user_id=current_user.id)
        else:
            flash("Acción inválida.", "danger")
            return _redirect_back("saas.ai_subscriptions_panel")
        flash("Suscripción IA actualizada.", "success")
    except AISubscriptionError as exc:
        flash(str(exc), "danger")
    return _redirect_back("saas.ai_subscriptions_panel")


@bp.route("/companies")
@superadmin_required
def companies_panel():
    from app import Client, Company, Plan, Product, Sale, Subscription, User, db
    from services.subscription_service import SubscriptionService
    from services.ai_agent.subscription_service import AISubscriptionService

    _require_superadmin()
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").strip().lower()
    plan_code = (request.args.get("plan") or "all").strip().lower()
    page = request.args.get("page", default=1, type=int)
    per_page = request.args.get("per_page", default=12, type=int)
    per_page = min(max(per_page, 5), 100)

    query = Company.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Company.name.ilike(like),
                Company.contact_email.ilike(like),
                Company.tax_id.ilike(like),
            )
        )

    if status == "active":
        query = query.filter(Company.active.is_(True))
    elif status in {"inactive", "suspended"}:
        query = query.filter(Company.active.is_(False))
    elif status in {"trial", "expired"}:
        query = query.join(Subscription, Subscription.company_id == Company.id).filter(Subscription.status == status).distinct()

    if plan_code != "all":
        query = (
            query.join(Subscription, Subscription.company_id == Company.id)
            .join(Plan, Plan.id == Subscription.plan_id)
            .filter(Plan.code == plan_code)
            .distinct()
        )

    pagination = query.order_by(Company.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    companies = pagination.items
    company_ids = [company.id for company in companies]

    user_counts = {}
    product_counts = {}
    client_counts = {}
    sale_counts = {}
    latest_subscriptions = {}
    effective_states = {}

    if company_ids:
        user_counts = {row[0]: int(row[1] or 0) for row in db.session.query(User.company_id, db.func.count(User.id)).filter(User.company_id.in_(company_ids)).group_by(User.company_id).all()}
        product_counts = {row[0]: int(row[1] or 0) for row in db.session.query(Product.company_id, db.func.count(Product.id)).filter(Product.company_id.in_(company_ids), Product.active.is_(True)).group_by(Product.company_id).all()}
        client_counts = {row[0]: int(row[1] or 0) for row in db.session.query(Client.company_id, db.func.count(Client.id)).filter(Client.company_id.in_(company_ids), Client.active.is_(True)).group_by(Client.company_id).all()}
        sale_counts = {row[0]: int(row[1] or 0) for row in db.session.query(Sale.company_id, db.func.count(Sale.id)).filter(Sale.company_id.in_(company_ids)).group_by(Sale.company_id).all()}

        for subscription in (
            Subscription.query.filter(Subscription.company_id.in_(company_ids))
            .order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc())
            .all()
        ):
            if subscription.company_id not in latest_subscriptions:
                latest_subscriptions[subscription.company_id] = subscription

        for company in companies:
            sub = latest_subscriptions.get(company.id)
            effective_states[company.id] = SubscriptionService.resolve_company_access_state(company, subscription=sub)

    # Suscripcion IA: proyeccion de solo lectura via AISubscriptionService, sin duplicar AI_PLANS/can_use_ai.
    ai_statuses = {company.id: AISubscriptionService.get_status(company) for company in companies}

    return render_template(
        "saas/companies.html",
        companies=companies,
        pagination=pagination,
        user_counts=user_counts,
        product_counts=product_counts,
        client_counts=client_counts,
        sale_counts=sale_counts,
        latest_subscriptions=latest_subscriptions,
        effective_states=effective_states,
        ai_statuses=ai_statuses,
        plans=Plan.query.filter(Plan.active.is_(True)).order_by(Plan.price.asc()).all(),
        filters={"q": q, "status": status, "plan": plan_code, "per_page": per_page},
    )


@bp.route("/companies/<int:company_id>")
@superadmin_required
def company_detail(company_id):
    from app import AuditLog, Client, Company, Payment, Product, Sale, Subscription, User, db
    from services.subscription_service import SubscriptionService

    _require_superadmin()
    company = Company.query.filter_by(id=company_id).first_or_404()
    subscription = (
        Subscription.query.filter_by(company_id=company.id)
        .order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc())
        .first()
    )
    effective_state = SubscriptionService.resolve_company_access_state(company, subscription=subscription)
    payments_standard_amount = float(db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(Payment.company_id == company.id, standard_subscription_payment_filter(Payment), Payment.status == "approved").scalar() or 0)
    payments_ai_amount = float(db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(Payment.company_id == company.id, ai_subscription_payment_filter(Payment), Payment.status == "approved").scalar() or 0)
    stats = {
        "users": User.query.filter_by(company_id=company.id).count(),
        "active_users": User.query.filter_by(company_id=company.id, active=True).count(),
        "products": Product.query.filter_by(company_id=company.id, active=True).count(),
        "clients": Client.query.filter_by(company_id=company.id, active=True).count(),
        "sales": Sale.query.filter_by(company_id=company.id).count(),
        "sales_amount": float(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).filter(Sale.company_id == company.id).scalar() or 0),
        "payments_approved": payments_standard_amount + payments_ai_amount,
        "payments_standard_amount": payments_standard_amount,
        "payments_ai_amount": payments_ai_amount,
    }
    from services.one_time_secret_service import OneTimeSecretService

    pin_revealed_once = OneTimeSecretService.consume(
        db.session,
        user_id=current_user.id,
        purpose="company_pin_reveal",
        subject_type="company",
        subject_id=company.id,
        access_token=session.pop(f"company_pin_reveal_{company.id}", None),
    )
    if pin_revealed_once is not None:
        db.session.commit()
    last_payments = Payment.query.filter(Payment.company_id == company.id, subscription_revenue_payment_filter(Payment)).order_by(Payment.created_at.desc()).limit(10).all()
    audit = AuditLog.query.filter_by(company_id=company.id).order_by(AuditLog.created_at.desc()).limit(20).all()
    return render_template(
        "saas/company_detail.html",
        company=company,
        subscription=subscription,
        effective_state=effective_state,
        stats=stats,
        last_payments=last_payments,
        payment_flow_label=payment_flow_label,
        audit=audit,
        pin_revealed_once=pin_revealed_once,
    )


@bp.route("/companies/<int:company_id>/360")
@superadmin_required
def company_360(company_id):
    """Read-only operational 360 view; preserves the existing company detail route."""
    from app import AuditLog, BackupLog, Client, Company, Payment, Product, Sale, Subscription, SupportTicket, User, db, model_table_exists

    company = Company.query.filter_by(id=company_id).first_or_404()
    subscription = (
        Subscription.query.filter_by(company_id=company.id)
        .order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc())
        .first()
    )

    approved_payments = float(
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(Payment.company_id == company.id, subscription_revenue_payment_filter(Payment), Payment.status == "approved")
        .scalar() or 0
    )
    open_tickets = (
        SupportTicket.query.filter(
            SupportTicket.company_id == company.id,
            SupportTicket.status == "pendiente",
        ).count()
        if model_table_exists(SupportTicket)
        else 0
    )
    latest_sale = (
        Sale.query.filter(Sale.company_id == company.id)
        .order_by(Sale.date.desc())
        .first()
    )
    latest_backup = (
        BackupLog.query.filter(BackupLog.company_id == company.id)
        .order_by(BackupLog.created_at.desc())
        .first()
        if model_table_exists(BackupLog)
        else None
    )
    audit = AuditLog.query.filter_by(company_id=company.id).order_by(AuditLog.created_at.desc()).limit(12).all()

    stats = {
        "users": User.query.filter_by(company_id=company.id).count(),
        "active_users": User.query.filter_by(company_id=company.id, active=True).count(),
        "products": Product.query.filter_by(company_id=company.id, active=True).count(),
        "clients": Client.query.filter_by(company_id=company.id, active=True).count(),
        "sales": Sale.query.filter_by(company_id=company.id).count(),
        "sales_amount": float(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).filter(Sale.company_id == company.id).scalar() or 0),
        "approved_payments": approved_payments,
        "open_tickets": open_tickets,
    }
    return render_template(
        "saas/company_360.html",
        company=company,
        subscription=subscription,
        stats=stats,
        latest_sale=latest_sale,
        latest_backup=latest_backup,
        audit=audit,
    )


@bp.route("/companies/<int:company_id>/update", methods=["POST"])
@superadmin_required
def company_update(company_id):
    from app import AuditLog, Company, db

    _require_superadmin()
    company = Company.query.filter_by(id=company_id).first_or_404()
    old_name = company.name
    company.name = (request.form.get("name") or company.name).strip()[:160] or company.name
    company.contact_email = (request.form.get("contact_email") or "").strip()[:160] or None
    company.logo = (request.form.get("logo") or "").strip()[:255] or None

    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=company.id,
            action="company_update",
            entity="company",
            entity_id=company.id,
            detail=f"Empresa actualizada {old_name} -> {company.name}. ip={request.remote_addr or 'unknown'} resultado=ok",
        )
    )
    db.session.commit()
    flash("Empresa actualizada correctamente.", "success")
    return _redirect_back("saas.companies_panel")


@bp.route("/companies/<int:company_id>/pin/assign", methods=["POST"])
@superadmin_required
def company_assign_pin(company_id):
    from app import AuditLog, Company, db
    from services.company_security_service import CompanySecurityService

    _require_superadmin()
    company = Company.query.filter_by(id=company_id).first_or_404()
    if not _require_superadmin_step_up():
        return redirect(url_for("saas.company_detail", company_id=company.id))
    raw_pin = (request.form.get("admin_pin") or "").strip()
    if len(raw_pin) != 4 or not raw_pin.isdigit():
        flash("El PIN debe ser numerico y de 4 digitos.", "danger")
        return redirect(url_for("saas.company_detail", company_id=company.id))

    from services.one_time_secret_service import OneTimeSecretService

    OneTimeSecretService.revoke(
        db.session,
        user_id=current_user.id,
        purpose="company_pin_reveal",
        subject_type="company",
        subject_id=company.id,
    )
    _secret_row, access_token = OneTimeSecretService.issue(
        db.session,
        user_id=current_user.id,
        purpose="company_pin_reveal",
        subject_type="company",
        subject_id=company.id,
        secret_value=raw_pin,
    )
    CompanySecurityService.set_pin(company, raw_pin)
    session[f"company_pin_reveal_{company.id}"] = access_token
    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=company.id,
            action="company_pin_assigned",
            entity="company",
            entity_id=company.id,
            detail=f"PIN Mi Empresa asignado/actualizado por superadmin. ip={request.remote_addr or 'unknown'} resultado=ok",
        )
    )
    db.session.commit()
    flash("PIN asignado correctamente.", "success")
    return redirect(url_for("saas.company_detail", company_id=company.id))


@bp.route("/companies/<int:company_id>/pin/generate", methods=["POST"])
@superadmin_required
def company_generate_pin(company_id):
    from app import AuditLog, Company, db
    from services.company_security_service import CompanySecurityService

    _require_superadmin()
    company = Company.query.filter_by(id=company_id).first_or_404()
    if not _require_superadmin_step_up():
        return redirect(url_for("saas.company_detail", company_id=company.id))
    had_pin = bool(company.business_pin_hash)

    raw_pin = f"{secrets.randbelow(10000):04d}"
    from services.one_time_secret_service import OneTimeSecretService

    OneTimeSecretService.revoke(
        db.session,
        user_id=current_user.id,
        purpose="company_pin_reveal",
        subject_type="company",
        subject_id=company.id,
    )
    _secret_row, access_token = OneTimeSecretService.issue(
        db.session,
        user_id=current_user.id,
        purpose="company_pin_reveal",
        subject_type="company",
        subject_id=company.id,
        secret_value=raw_pin,
    )
    CompanySecurityService.set_pin(company, raw_pin)
    session[f"company_pin_reveal_{company.id}"] = access_token

    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=company.id,
            action="company_pin_regenerated" if had_pin else "company_pin_generated",
            entity="company",
            entity_id=company.id,
            detail=f"PIN Mi Empresa {'regenerado' if had_pin else 'generado'} automaticamente por superadmin. ip={request.remote_addr or 'unknown'} resultado=ok",
        )
    )
    db.session.commit()
    flash("PIN generado correctamente. Se mostrara una sola vez.", "success")
    return redirect(url_for("saas.company_detail", company_id=company.id))


@bp.route("/companies/<int:company_id>/delete", methods=["POST"])
@superadmin_required
def company_delete(company_id):
    from app import AuditLog, Client, Company, Product, User, db

    _require_superadmin()
    company = Company.query.filter_by(id=company_id).first_or_404()
    if not _require_superadmin_step_up():
        return _redirect_back("saas.companies_panel")
    confirm_company_name = (request.form.get("confirm_company_name") or "").strip()
    if confirm_company_name != (company.name or ""):
        flash("Para eliminar definitivamente, escribí el nombre exacto de la empresa.", "warning")
        return _redirect_back("saas.companies_panel")

    # SuperAdmin tiene autoridad total: la eliminación definitiva no se restringe por
    # el estado de la suscripción (ya está protegida por rol + confirmación exacta del nombre).
    company_name = company.name
    try:
        db.session.add(
            AuditLog(
                user_id=current_user.id,
                company_id=None,
                action="company_hard_delete",
                entity="company",
                entity_id=company.id,
                detail=f"Empresa eliminada definitivamente: {company_name}. ip={request.remote_addr or 'unknown'} resultado=ok",
            )
        )
        _hard_delete_company(company)
        db.session.add(
            AuditLog(
                user_id=current_user.id,
                company_id=None,
                action="company_hard_delete_complete",
                entity="company",
                entity_id=company.id,
                detail=f"Empresa purgada definitivamente: {company_name}. ip={request.remote_addr or 'unknown'} resultado=ok",
            )
        )
        # Defensive final pass for primary tenant entities in case of ORM/session edge cases.
        db.session.query(User).filter(User.company_id == company_id).delete(synchronize_session=False)
        db.session.query(Product).filter(Product.company_id == company_id).delete(synchronize_session=False)
        db.session.query(Client).filter(Client.company_id == company_id).delete(synchronize_session=False)
        db.session.commit()
        flash("Empresa eliminada definitivamente.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Error al eliminar definitivamente company_id=%s (%s): %s", company_id, company_name, exc)
        flash("No se pudo eliminar definitivamente la empresa. Revisá los logs del servidor para el detalle técnico.", "danger")
    return _redirect_back("saas.companies_panel")


@bp.route("/companies/<int:company_id>/impersonate", methods=["POST"])
@superadmin_required
def company_impersonate(company_id):
    from app import AuditLog, Company, db

    _require_superadmin()
    company = Company.query.filter_by(id=company_id).first_or_404()
    if not _require_superadmin_step_up():
        return _redirect_back("saas.companies_panel")
    session["impersonator_user_id"] = current_user.id
    session["impersonated_company_id"] = company.id

    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=company.id,
            action="impersonation_start",
            entity="company",
            entity_id=company.id,
            detail=f"Impersonacion iniciada hacia empresa {company.name}. ip={request.remote_addr or 'unknown'} resultado=ok",
        )
    )
    db.session.commit()
    flash(f"Modo auditoría de empresa activado para: {company.name}", "info")
    return redirect(url_for("saas.company_detail", company_id=company.id))


@bp.route("/impersonation/exit", methods=["POST"])
@superadmin_required
def impersonation_exit():
    from app import AuditLog, db

    _require_superadmin()
    previous_company_id = session.get("impersonated_company_id")
    restore_company_id = getattr(current_user, "company_id", None)
    session.pop("impersonated_company_id", None)
    session.pop("impersonator_user_id", None)

    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=restore_company_id,
            action="impersonation_end",
            entity="company",
            entity_id=previous_company_id,
            detail=f"Impersonacion finalizada. ip={request.remote_addr or 'unknown'} resultado=ok",
        )
    )
    db.session.commit()
    flash("Impersonación finalizada.", "success")
    return _redirect_back("saas.companies_panel")


@bp.route("/billing")
@superadmin_required
def billing():
    from app import Company, Invoice, Payment, PaymentHistory, Subscription, db

    _require_superadmin()
    invoices = Invoice.query.order_by(Invoice.issued_at.desc()).limit(40).all()
    payments = Payment.query.filter(subscription_revenue_payment_filter(Payment)).order_by(Payment.created_at.desc()).limit(40).all()
    history = PaymentHistory.query.order_by(PaymentHistory.created_at.desc()).limit(30).all()
    companies = Company.query.order_by(Company.created_at.desc()).all()
    subscriptions = Subscription.query.order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc()).limit(40).all()

    totals = {
        "total_invoiced": float(db.session.query(db.func.coalesce(db.func.sum(Invoice.amount), 0)).scalar() or 0),
        "total_paid_standard": float(db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(standard_subscription_payment_filter(Payment), Payment.status == "approved").scalar() or 0),
        "total_paid_ai": float(db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(ai_subscription_payment_filter(Payment), Payment.status == "approved").scalar() or 0),
        "pending_invoices": Invoice.query.filter(Invoice.status.in_(["pending", "draft", "issued"])).count(),
        "pending_payments_standard": Payment.query.filter(standard_subscription_payment_filter(Payment), Payment.status.in_(["pending", "authorized", "in_process"])).count(),
        "pending_payments_ai": Payment.query.filter(ai_subscription_payment_filter(Payment), Payment.status.in_(["pending", "authorized", "in_process"])).count(),
        "rejected_payments_standard": Payment.query.filter(standard_subscription_payment_filter(Payment), Payment.status.in_(["rejected", "cancelled", "expired", "charged_back"])).count(),
        "rejected_payments_ai": Payment.query.filter(ai_subscription_payment_filter(Payment), Payment.status.in_(["rejected", "cancelled", "expired", "charged_back"])).count(),
        "trial_companies": Subscription.query.filter(Subscription.status == "trial").count(),
    }
    totals["total_paid"] = totals["total_paid_standard"] + totals["total_paid_ai"]
    totals["pending_payments"] = totals["pending_payments_standard"] + totals["pending_payments_ai"]
    totals["rejected_payments"] = totals["rejected_payments_standard"] + totals["rejected_payments_ai"]
    return render_template(
        "saas/billing.html",
        invoices=invoices,
        payments=payments,
        history=history,
        companies=companies,
        subscriptions=subscriptions,
        totals=totals,
        payment_flow=payment_flow,
        payment_flow_label=payment_flow_label,
        mp_config=load_billing_config(),
    )


@bp.route("/subscriptions")
@superadmin_required
def subscriptions_panel():
    from app import Company, Payment, Plan, Subscription, db
    from services.subscription_service import SubscriptionService

    _require_superadmin()
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").strip().lower()
    plan_code = (request.args.get("plan") or "all").strip().lower()
    company_id_filter = request.args.get("company_id", type=int)
    page = request.args.get("page", default=1, type=int)
    per_page = request.args.get("per_page", default=12, type=int)
    per_page = min(max(per_page, 5), 100)

    query = Subscription.query.join(Company, Company.id == Subscription.company_id, isouter=True).join(Plan, Plan.id == Subscription.plan_id, isouter=True)
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Company.name.ilike(like),
                Plan.name.ilike(like),
                Subscription.status.ilike(like),
            )
        )
    if plan_code != "all":
        query = query.filter(Plan.code == plan_code)
    if company_id_filter:
        query = query.filter(Subscription.company_id == company_id_filter)

    now_ref = utcnow()
    rows = query.order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc()).all()
    effective_state_by_id = {}
    effective_status_by_id = {}
    filtered_rows = []
    for sub in rows:
        company = getattr(sub, "company", None) or db.session.get(Company, sub.company_id)
        effective_state = SubscriptionService.resolve_company_access_state(company, subscription=sub, now=now_ref)
        effective_status = SubscriptionService.get_effective_subscription_status(sub, company=company, now=now_ref)
        effective_state_by_id[sub.id] = effective_state
        effective_status_by_id[sub.id] = effective_status
        if status != "all" and effective_status != status:
            continue
        filtered_rows.append(sub)

    total = len(filtered_rows)
    current_subscription_id_by_company = {}
    for company_id in {sub.company_id for sub in rows}:
        current = SubscriptionService.active_subscription_for_company(company_id)
        if current is None:
            continue
        current_company = db.session.get(Company, company_id)
        current_effective_status = SubscriptionService.get_effective_subscription_status(
            current,
            company=current_company,
            now=now_ref,
        )
        if current_effective_status not in {
            SubscriptionService.STATE_EXPIRED,
            SubscriptionService.STATE_TRIAL_EXPIRED,
            SubscriptionService.STATE_CANCELLED,
            SubscriptionService.STATE_SUSPENDED,
        }:
            current_subscription_id_by_company[company_id] = current.id

    filtered_rows.sort(
        key=lambda sub: (
            0 if current_subscription_id_by_company.get(sub.company_id) == sub.id else 1,
            (getattr(sub.company, "name", "") or "").lower(),
            sub.start_date or datetime.min,
            sub.id,
        ),
        reverse=False,
    )
    pagination = _SimplePagination(page=page, per_page=per_page, total=total)
    start = (pagination.page - 1) * pagination.per_page
    end = start + pagination.per_page
    subscriptions = filtered_rows[start:end]

    confirmed_payment_statuses = ["approved", "paid", "active", "refunded"]
    last_payment_by_subscription_id = {}
    last_payment_by_company_id = {}
    sub_ids = [sub.id for sub in subscriptions]
    company_ids = [sub.company_id for sub in subscriptions]
    if sub_ids:
        for row in (
            db.session.query(Payment.subscription_id, db.func.max(Payment.paid_at).label("last_paid_at"))
            .filter(Payment.subscription_id.in_(sub_ids), Payment.status.in_(confirmed_payment_statuses), Payment.paid_at.isnot(None))
            .group_by(Payment.subscription_id)
            .all()
        ):
            last_payment_by_subscription_id[int(row.subscription_id)] = row.last_paid_at
    if company_ids:
        for row in (
            db.session.query(Payment.company_id, db.func.max(Payment.paid_at).label("last_paid_at"))
            .filter(Payment.company_id.in_(company_ids), standard_subscription_payment_filter(Payment), Payment.status.in_(confirmed_payment_statuses), Payment.paid_at.isnot(None))
            .group_by(Payment.company_id)
            .all()
        ):
            last_payment_by_company_id[int(row.company_id)] = row.last_paid_at

    start_display_by_id = {}
    start_input_by_id = {}
    next_due_display_by_id = {}
    next_due_input_by_id = {}
    last_payment_display_by_id = {}
    last_payment_input_by_id = {}
    for sub in subscriptions:
        effective_state = effective_state_by_id.get(sub.id, {})
        effective_next_due = effective_state.get("next_billing_date") or sub.next_billing_date
        real_last_payment = last_payment_by_subscription_id.get(sub.id) or last_payment_by_company_id.get(sub.company_id) or sub.last_payment_date

        start_display_by_id[sub.id] = _format_admin_datetime_local(sub.start_date, "%Y-%m-%d %H:%M") if sub.start_date else "—"
        start_input_by_id[sub.id] = _format_admin_datetime_local(sub.start_date, "%Y-%m-%dT%H:%M") if sub.start_date else ""
        next_due_display_by_id[sub.id] = _format_admin_datetime_local(effective_next_due, "%Y-%m-%d %H:%M") if effective_next_due else "—"
        next_due_input_by_id[sub.id] = _format_admin_datetime_local(sub.next_billing_date, "%Y-%m-%dT%H:%M") if sub.next_billing_date else ""
        last_payment_display_by_id[sub.id] = _format_admin_datetime_local(real_last_payment, "%Y-%m-%d %H:%M") if real_last_payment else "—"
        last_payment_input_by_id[sub.id] = _format_admin_datetime_local(sub.last_payment_date, "%Y-%m-%dT%H:%M") if sub.last_payment_date else ""

    companies = Company.query.order_by(Company.name.asc()).all()
    selected_company = next((company for company in companies if company.id == company_id_filter), None) if company_id_filter else None
    selected_company_has_subscription = False
    if selected_company is not None:
        selected_company_has_subscription = Subscription.query.filter_by(company_id=selected_company.id).first() is not None
    plans = Plan.query.filter(Plan.active.is_(True)).order_by(Plan.price.asc()).all()
    subscription_actions = {
        sub.id: (
            _allowed_ui_actions_for_status(effective_status_by_id.get(sub.id, sub.status))
            if current_subscription_id_by_company.get(sub.company_id) in {None, sub.id}
            else set()
        )
        for sub in subscriptions
    }
    return render_template(
        "saas/subscriptions.html",
        subscriptions=subscriptions,
        subscription_actions=subscription_actions,
        effective_status_by_id=effective_status_by_id,
        current_subscription_id_by_company=current_subscription_id_by_company,
        start_display_by_id=start_display_by_id,
        start_input_by_id=start_input_by_id,
        next_due_display_by_id=next_due_display_by_id,
        next_due_input_by_id=next_due_input_by_id,
        last_payment_display_by_id=last_payment_display_by_id,
        last_payment_input_by_id=last_payment_input_by_id,
        pagination=pagination,
        companies=companies,
        selected_company=selected_company,
        selected_company_has_subscription=selected_company_has_subscription,
        plans=plans,
        filters={"q": q, "status": status, "plan": plan_code, "per_page": per_page, "company_id": company_id_filter},
        status_options=SUBSCRIPTION_STATUS_OPTIONS,
    )


@bp.route("/subscriptions/quick-renew-company", methods=["POST"])
@superadmin_required
def subscriptions_quick_renew_company():
    from app import Company, Plan, Subscription, db
    from services.subscription_service import SubscriptionCommandError, SubscriptionService

    _require_superadmin()
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        flash("Empresa inválida.", "danger")
        return _redirect_back("saas.subscriptions_panel")

    company = Company.query.filter_by(id=company_id).first()
    if company is None:
        flash("Empresa inválida.", "danger")
        return _redirect_back("saas.subscriptions_panel")
    if not _require_superadmin_step_up():
        return _redirect_back("saas.subscriptions_panel")

    try:
        latest_subscription = SubscriptionService.active_subscription_for_company(company.id)
        if latest_subscription is not None:
            renew_snapshot = f"{latest_subscription.status}:{latest_subscription.next_billing_date.isoformat() if latest_subscription.next_billing_date else 'none'}"
            SubscriptionService.run_command(
                db.session,
                SubscriptionService.RenewSubscriptionCommand(
                    company_id=company.id,
                    subscription_id=latest_subscription.id,
                    actor_user_id=current_user.id,
                    actor_role=current_user.role,
                    origin="superadmin",
                    ip_address=request.remote_addr,
                    idempotency_key=f"saas-quick-renew:{company.id}:{latest_subscription.id}:{renew_snapshot}",
                ),
            )
            db.session.commit()
            flash("Suscripción renovada correctamente.", "success")
            return redirect(url_for("saas.subscriptions_panel", company_id=company.id))

        plan = (
            Plan.query.filter(Plan.active.is_(True), Plan.code == "emprendedor")
            .order_by(Plan.price.asc(), Plan.id.asc())
            .first()
        )
        if plan is None:
            plan = Plan.query.filter(Plan.active.is_(True)).order_by(Plan.price.asc(), Plan.id.asc()).first()
        if plan is None:
            flash("No hay planes activos para crear una suscripción.", "danger")
            return redirect(url_for("saas.subscriptions_panel", company_id=company.id))

        SubscriptionService.run_command(
            db.session,
            SubscriptionService.AssignManualSubscriptionCommand(
                company_id=company.id,
                plan_id=plan.id,
                manual_reason="Renovación rápida desde SuperAdmin",
                created_by_admin=current_user.id,
                actor_user_id=current_user.id,
                actor_role=current_user.role,
                origin="superadmin",
                ip_address=request.remote_addr,
                idempotency_key=f"saas-quick-create:{company.id}:{plan.id}",
            ),
        )
        db.session.commit()
        flash("Suscripción creada y renovada correctamente.", "success")
    except SubscriptionCommandError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Error en renovación rápida company_id=%s: %s", company_id, exc)
        flash("No se pudo procesar la renovación rápida.", "danger")

    return redirect(url_for("saas.subscriptions_panel", company_id=company.id))


@bp.route("/subscriptions/create", methods=["POST"])
@superadmin_required
def subscriptions_create():
    from app import Company, Plan, db
    from services.subscription_service import SubscriptionCommandError, SubscriptionService

    _require_superadmin()
    company_id = request.form.get("company_id", type=int)
    plan_id = request.form.get("plan_id", type=int)
    status = _normalized_subscription_status(request.form.get("status"))
    start_date = _parse_dt(request.form.get("start_date")) or utcnow()
    next_billing_date = _parse_dt(request.form.get("next_billing_date"))
    renewal_enabled = (request.form.get("renewal_enabled") or "1") == "1"

    company = Company.query.filter_by(id=company_id).first()
    plan = Plan.query.filter_by(id=plan_id).first()
    if company is None or plan is None:
        flash("Empresa o plan inválido.", "danger")
        return _redirect_back("saas.subscriptions_panel")
    if not _require_superadmin_step_up():
        return _redirect_back("saas.subscriptions_panel")

    try:
        SubscriptionService.run_command(
            db.session,
            SubscriptionService.CreateSubscriptionCommand(
                company_id=company.id,
                plan_id=plan.id,
                status=status,
                start_date=start_date,
                next_billing_date=next_billing_date,
                renewal_enabled=renewal_enabled,
                actor_user_id=current_user.id,
                actor_role=current_user.role,
                origin="superadmin",
                ip_address=request.remote_addr,
                idempotency_key=(
                    request.form.get("idempotency_key")
                    or f"saas-create:{company.id}:{plan.id}:{status}:{int(bool(renewal_enabled))}"
                ),
            ),
        )
        db.session.commit()
        flash("Suscripción creada correctamente.", "success")
    except SubscriptionCommandError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Error al crear suscripción company_id=%s plan_id=%s: %s", company_id, plan_id, exc)
        flash("No se pudo crear la suscripción. Revisá los datos e intentá nuevamente.", "danger")
    return _redirect_back("saas.subscriptions_panel")


@bp.route("/subscriptions/<int:subscription_id>/update", methods=["POST"])
@superadmin_required
def subscriptions_update(subscription_id):
    from app import AuditLog, PaymentHistory, Plan, Subscription, db
    from services.subscription_service import SubscriptionCommandError, SubscriptionService

    _require_superadmin()
    subscription = Subscription.query.filter_by(id=subscription_id).first_or_404()
    plan_id = request.form.get("plan_id", type=int)
    plan = Plan.query.filter_by(id=plan_id).first() if plan_id else None
    if plan_id and plan is None:
        flash("Plan inválido.", "danger")
        return _redirect_back("saas.subscriptions_panel")

    # SuperAdmin puede corregir cualquier registro existente (incluidos vencidos/cancelados)
    # desde la edición. Las transiciones sensibles de plan/estado siguen protegidas por step-up abajo.
    start_date = _parse_dt(request.form.get("start_date"))
    next_billing_date = _parse_dt(request.form.get("next_billing_date"))
    last_payment_date = _parse_dt(request.form.get("last_payment_date"))
    renewal_enabled = (request.form.get("renewal_enabled") or "1") == "1"
    target_status = _normalized_subscription_status(request.form.get("status") or subscription.status)
    requires_sensitive_step_up = (
        (plan is not None and plan.id != subscription.plan_id)
        or target_status != _normalized_subscription_status(subscription.status)
    )
    if requires_sensitive_step_up and not _require_superadmin_step_up():
        return _redirect_back("saas.subscriptions_panel")

    try:
        # IMPORTANTE: "Modificar" siempre debe hacer UPDATE sobre esta misma fila de Subscription
        # (identificada por subscription_id). NUNCA reutilizar ChangePlanCommand/CreateSubscriptionCommand
        # aquí: esos comandos son para el flujo de autogestión del tenant y crean una fila NUEVA de
        # Subscription (cerrando la anterior), lo cual duplicaba suscripciones cuando el SuperAdmin
        # sólo quería editar el plan de un registro existente.
        target_subscription = subscription
        plan_before_id = target_subscription.plan_id
        current_app.logger.info(
            "subscriptions_update ANTES: company_id=%s subscription_id=%s plan_id=%s status=%s",
            target_subscription.company_id,
            target_subscription.id,
            plan_before_id,
            target_subscription.status,
        )
        if plan is not None and plan.id != target_subscription.plan_id:
            target_subscription.plan_id = plan.id

        if start_date is not None:
            target_subscription.start_date = start_date
            target_subscription.starts_at = start_date
        if next_billing_date is not None:
            target_subscription.next_billing_date = next_billing_date
            target_subscription.ends_at = next_billing_date
        if last_payment_date is not None:
            target_subscription.last_payment_date = last_payment_date

        target_subscription.renewal_enabled = renewal_enabled
        target_subscription.auto_renew = renewal_enabled
        if renewal_enabled:
            target_subscription.cancel_at_period_end = False

        if target_subscription.start_date and target_subscription.next_billing_date and target_subscription.next_billing_date < target_subscription.start_date:
            raise SubscriptionCommandError("Fechas inválidas: la fecha de vencimiento no puede ser menor a la de inicio.")
        if target_subscription.starts_at and target_subscription.ends_at and target_subscription.ends_at < target_subscription.starts_at:
            raise SubscriptionCommandError("Fechas inválidas: el vencimiento no puede ser menor al inicio.")

        if target_status in {"cancelled", "suspended", "expired"}:
            if target_status == "cancelled":
                SubscriptionService.run_command(
                    db.session,
                    SubscriptionService.CancelSubscriptionCommand(
                        company_id=target_subscription.company_id,
                        subscription_id=target_subscription.id,
                        actor_user_id=current_user.id,
                        actor_role=current_user.role,
                        origin="superadmin",
                        ip_address=request.remote_addr,
                        idempotency_key=f"saas-update-cancel:{target_subscription.company_id}:{target_subscription.id}",
                        cancel_at_period_end=False,
                    ),
                )
            elif target_status == "suspended":
                SubscriptionService.run_command(
                    db.session,
                    SubscriptionService.ExpireSubscriptionCommand(
                        company_id=target_subscription.company_id,
                        subscription_id=target_subscription.id,
                        actor_user_id=current_user.id,
                        actor_role=current_user.role,
                        origin="superadmin",
                        ip_address=request.remote_addr,
                        idempotency_key=f"saas-update-suspend:{target_subscription.company_id}:{target_subscription.id}",
                        reason="superadmin_suspend",
                    ),
                )
            elif target_status == "expired":
                SubscriptionService.run_command(
                    db.session,
                    SubscriptionService.ExpireSubscriptionCommand(
                        company_id=target_subscription.company_id,
                        subscription_id=target_subscription.id,
                        actor_user_id=current_user.id,
                        actor_role=current_user.role,
                        origin="superadmin",
                        ip_address=request.remote_addr,
                        idempotency_key=f"saas-update-expire:{target_subscription.company_id}:{target_subscription.id}",
                        reason="superadmin_update",
                    ),
                )
        elif target_status == "active":
            SubscriptionService.run_command(
                db.session,
                SubscriptionService.ReactivateSubscriptionCommand(
                    company_id=target_subscription.company_id,
                    subscription_id=target_subscription.id,
                    actor_user_id=current_user.id,
                    actor_role=current_user.role,
                    origin="superadmin",
                    ip_address=request.remote_addr,
                    idempotency_key=f"saas-update-reactivate:{target_subscription.company_id}:{target_subscription.id}",
                ),
            )
        else:
            target_subscription.status = target_status

        # Protección contra duplicados: "Modificar" nunca debe crear una segunda
        # Subscription para la misma empresa. Verificamos que el id siga siendo el mismo
        # y que la fila continúe existiendo antes de confirmar los cambios.
        if target_subscription.id != subscription_id:
            raise SubscriptionCommandError("Operación inválida: el id de la suscripción cambió durante la modificación.")

        db.session.add(
            AuditLog(
                user_id=current_user.id,
                company_id=target_subscription.company_id,
                action="subscription_admin_update",
                entity="subscription",
                entity_id=target_subscription.id,
                detail=(
                    f"Update superadmin status={target_status} start_date={target_subscription.start_date} "
                    f"next_billing_date={target_subscription.next_billing_date} last_payment_date={target_subscription.last_payment_date} "
                    f"renewal_enabled={target_subscription.renewal_enabled} ip={request.remote_addr or 'unknown'}"
                ),
            )
        )
        db.session.add(
            PaymentHistory(
                company_id=target_subscription.company_id,
                subscription_id=target_subscription.id,
                event="subscription_admin_update",
                detail="Actualización administrativa de suscripción",
                source="superadmin",
                status=target_subscription.status,
                payload_json=json.dumps(
                    {
                        "subscription_id": target_subscription.id,
                        "status": target_subscription.status,
                        "start_date": target_subscription.start_date.isoformat() if target_subscription.start_date else None,
                        "next_billing_date": target_subscription.next_billing_date.isoformat() if target_subscription.next_billing_date else None,
                        "last_payment_date": target_subscription.last_payment_date.isoformat() if target_subscription.last_payment_date else None,
                        "renewal_enabled": bool(target_subscription.renewal_enabled),
                        "actor_user_id": current_user.id,
                    },
                    ensure_ascii=False,
                ),
            )
        )

        db.session.commit()
        current_app.logger.info(
            "subscriptions_update DESPUÉS: company_id=%s subscription_id=%s plan_id=%s status=%s",
            target_subscription.company_id,
            target_subscription.id,
            target_subscription.plan_id,
            target_subscription.status,
        )
        flash("Suscripción modificada correctamente.", "success")
    except SubscriptionCommandError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Error al modificar suscripción id=%s: %s", subscription_id, exc)
        flash("No se pudo modificar la suscripción.", "danger")
    return _redirect_back("saas.subscriptions_panel")


@bp.route("/subscriptions/<int:subscription_id>/action", methods=["POST"])
@superadmin_required
def subscriptions_action(subscription_id):
    from app import Subscription, db
    from services.subscription_service import SubscriptionCommandError, SubscriptionService

    _require_superadmin()
    subscription = Subscription.query.filter_by(id=subscription_id).first_or_404()
    if not _require_superadmin_step_up():
        return _redirect_back("saas.subscriptions_panel")
    action = (request.form.get("action") or "").strip().lower()
    status_before = SubscriptionService.get_effective_subscription_status(subscription, company=subscription.company)

    if not _action_allowed_for_status(status_before, action):
        flash("La acción no está permitida para el estado actual de la suscripción.", "warning")
        return _redirect_back("saas.subscriptions_panel")

    try:
        if action == "cancel":
            SubscriptionService.run_command(
                db.session,
                SubscriptionService.CancelSubscriptionCommand(
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    actor_user_id=current_user.id,
                    actor_role=current_user.role,
                    origin="superadmin",
                    ip_address=request.remote_addr,
                    idempotency_key=(
                        f"saas-action-cancel:{subscription.company_id}:{subscription.id}:"
                        f"{subscription.status}:{subscription.next_billing_date.isoformat() if subscription.next_billing_date else 'none'}"
                    ),
                    cancel_at_period_end=False,
                ),
            )
        elif action == "reactivate":
            SubscriptionService.run_command(
                db.session,
                SubscriptionService.ReactivateSubscriptionCommand(
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    actor_user_id=current_user.id,
                    actor_role=current_user.role,
                    origin="superadmin",
                    ip_address=request.remote_addr,
                    idempotency_key=(
                        f"saas-action-reactivate:{subscription.company_id}:{subscription.id}:"
                        f"{subscription.status}:{subscription.next_billing_date.isoformat() if subscription.next_billing_date else 'none'}"
                    ),
                ),
            )
        elif action == "suspend":
            SubscriptionService.run_command(
                db.session,
                SubscriptionService.ExpireSubscriptionCommand(
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    actor_user_id=current_user.id,
                    actor_role=current_user.role,
                    origin="superadmin",
                    ip_address=request.remote_addr,
                    idempotency_key=(
                        f"saas-action-suspend:{subscription.company_id}:{subscription.id}:"
                        f"{subscription.status}:{subscription.next_billing_date.isoformat() if subscription.next_billing_date else 'none'}"
                    ),
                    reason="superadmin_suspend",
                ),
            )
        elif action == "extend":
            days = request.form.get("days", type=int) or 7
            SubscriptionService.run_command(
                db.session,
                SubscriptionService.ExtendSubscriptionCommand(
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    actor_user_id=current_user.id,
                    actor_role=current_user.role,
                    origin="superadmin",
                    ip_address=request.remote_addr,
                    idempotency_key=(
                        f"saas-action-extend:{subscription.company_id}:{subscription.id}:{days}:"
                        f"{subscription.next_billing_date.isoformat() if subscription.next_billing_date else 'none'}"
                    ),
                    days=days,
                ),
            )
        elif action == "renew_now":
            SubscriptionService.run_command(
                db.session,
                SubscriptionService.RenewSubscriptionCommand(
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    actor_user_id=current_user.id,
                    actor_role=current_user.role,
                    origin="superadmin",
                    ip_address=request.remote_addr,
                    idempotency_key=(
                        f"saas-action-renew:{subscription.company_id}:{subscription.id}:"
                        f"{subscription.status}:{subscription.next_billing_date.isoformat() if subscription.next_billing_date else 'none'}"
                    ),
                ),
            )
        elif action == "delete":
            SubscriptionService.run_command(
                db.session,
                SubscriptionService.CancelSubscriptionCommand(
                    company_id=subscription.company_id,
                    subscription_id=subscription.id,
                    actor_user_id=current_user.id,
                    actor_role=current_user.role,
                    origin="superadmin",
                    ip_address=request.remote_addr,
                    idempotency_key=(
                        f"saas-action-delete:{subscription.company_id}:{subscription.id}:"
                        f"{subscription.status}:{subscription.next_billing_date.isoformat() if subscription.next_billing_date else 'none'}"
                    ),
                    cancel_at_period_end=False,
                ),
            )
        else:
            flash("Acción de suscripción inválida.", "danger")
            return _redirect_back("saas.subscriptions_panel")

        db.session.commit()
    except SubscriptionCommandError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return _redirect_back("saas.subscriptions_panel")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "Error en acción de suscripción action=%s subscription_id=%s status_before=%s: %s",
            action,
            subscription_id,
            status_before,
            exc,
        )
        flash("No se pudo ejecutar la acción de suscripción.", "danger")
        return _redirect_back("saas.subscriptions_panel")

    if action == "cancel":
        flash("Suscripción cancelada.", "success")
    elif action == "suspend":
        flash("Suscripción suspendida.", "success")
    elif action == "reactivate":
        flash("Suscripción reactivada.", "success")
    elif action == "renew_now":
        flash("Suscripción renovada.", "success")
    else:
        flash("Acción ejecutada correctamente.", "success")
    return _redirect_back("saas.subscriptions_panel")


@bp.route("/users")
@superadmin_required
def users_panel():
    from app import Company, User, db

    _require_superadmin()
    q = (request.args.get("q") or "").strip()
    role = (request.args.get("role") or "all").strip().lower()
    company_id = request.args.get("company_id", type=int)

    query = User.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                User.username.ilike(like),
                User.email.ilike(like),
                User.first_name.ilike(like),
                User.last_name.ilike(like),
            )
        )
    if role in {"admin", "user", "superadmin"}:
        query = query.filter(User.role == role)
    if company_id:
        query = query.filter(User.company_id == company_id)

    users = query.order_by(User.created_at.desc(), User.id.desc()).limit(300).all()
    company_ids = sorted({item.company_id for item in users if item.company_id})
    companies = (
        Company.query.filter(Company.id.in_(company_ids)).order_by(Company.name.asc()).all() if company_ids else []
    )
    companies_by_id = {item.id: item for item in companies}
    all_companies = Company.query.order_by(Company.name.asc()).all()

    return render_template(
        "saas/users.html",
        users=users,
        companies_by_id=companies_by_id,
        all_companies=all_companies,
        filters={"q": q, "role": role, "company_id": company_id},
    )


@bp.route("/users/<int:user_id>/role", methods=["POST"])
@superadmin_required
def users_update_role(user_id):
    from app import AuditLog, User, db

    _require_superadmin()
    if not _require_superadmin_step_up():
        return _redirect_back("saas.users_panel" )
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)

    new_role = (request.form.get("role") or "").strip().lower()
    if new_role not in {"admin", "user"}:
        flash("Rol inválido. Solo se permite admin o user.", "danger")
        return _redirect_back("saas.users_panel")

    if user.role == "superadmin":
        flash("No se puede modificar el rol de un superadmin.", "warning")
        return _redirect_back("saas.users_panel")

    if user.company_id is None:
        flash("Solo se pueden modificar roles de empleados de empresas.", "warning")
        return _redirect_back("saas.users_panel")

    previous_role = (user.role or "").strip().lower()
    if previous_role == new_role:
        flash("El empleado ya tiene ese rol.", "info")
        return _redirect_back("saas.users_panel")

    user.role = new_role
    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=user.company_id,
            action="superadmin_user_role_update",
            entity="user",
            entity_id=user.id,
            detail=f"Rol actualizado de {previous_role or '-'} a {new_role} por superadmin",
        )
    )
    db.session.commit()
    flash(f"Rol actualizado correctamente para {user.username}: {new_role}.", "success")
    return _redirect_back("saas.users_panel")


@bp.route("/users/<int:user_id>/status", methods=["POST"])
@superadmin_required
def users_update_status(user_id):
    from app import AuditLog, User, db

    _require_superadmin()
    if not _require_superadmin_step_up():
        return _redirect_back("saas.users_panel" )
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    if user.role == "superadmin":
        flash("No se puede cambiar el estado de un superadmin desde este panel.", "warning")
        return _redirect_back("saas.users_panel")
    if user.company_id is None:
        flash("Solo se puede cambiar el estado de usuarios asociados a una empresa.", "warning")
        return _redirect_back("saas.users_panel")

    desired = (request.form.get("active") or "").strip().lower()
    if desired not in {"0", "1"}:
        flash("Estado inválido.", "danger")
        return _redirect_back("saas.users_panel")

    new_active = desired == "1"
    if user.active == new_active:
        flash("El usuario ya tiene ese estado.", "info")
        return _redirect_back("saas.users_panel")

    if not new_active and user.role == "admin":
        remaining_admins = User.query.filter(
            User.company_id == user.company_id,
            User.role == "admin",
            User.active.is_(True),
            User.id != user.id,
        ).count()
        if remaining_admins == 0:
            flash("No se puede desactivar al único administrador activo de la empresa.", "warning")
            return _redirect_back("saas.users_panel")

    previous = bool(user.active)
    user.active = new_active
    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=user.company_id,
            action="superadmin_user_status_update",
            entity="user",
            entity_id=user.id,
            detail=f"Estado actualizado de {'activo' if previous else 'inactivo'} a {'activo' if new_active else 'inactivo'} por superadmin",
        )
    )
    db.session.commit()
    flash(f"Usuario {'activado' if new_active else 'desactivado'} correctamente.", "success")
    return _redirect_back("saas.users_panel")


@bp.route("/password-recovery")
@superadmin_required
def password_recovery_panel():
    from app import Company, PasswordRecoveryRequest, User, db

    _require_superadmin()
    status = (request.args.get("status") or "all").strip().lower()
    query = PasswordRecoveryRequest.query
    if status in {"pendiente", "atendida", "cerrada"}:
        query = query.filter(PasswordRecoveryRequest.status == status)
    items = query.order_by(PasswordRecoveryRequest.requested_at.desc()).all()
    company_users = (
        db.session.query(User, Company.name)
        .join(Company, Company.id == User.company_id)
        .filter(
            User.company_id.isnot(None),
            User.role != "superadmin",
            User.active.is_(True),
        )
        .order_by(User.company_id.asc(), User.username.asc())
        .all()
    )
    from services.one_time_secret_service import OneTimeSecretService

    reveal_user_id = session.pop("password_recovery_temp_password_user_id", None)
    temp_password = OneTimeSecretService.consume(
        db.session,
        user_id=current_user.id,
        purpose="password_recovery_temp_password",
        subject_type="user",
        subject_id=reveal_user_id,
        access_token=session.pop("password_recovery_temp_password", None),
    )
    if temp_password is not None:
        db.session.commit()
    temp_password_user = session.pop("password_recovery_temp_password_user", None)
    return render_template(
        "saas/password_recovery.html",
        items=items,
        company_users=company_users,
        current_status=status,
        temp_password=temp_password,
        temp_password_user=temp_password_user,
    )


@bp.route("/password-recovery/company-user/reset", methods=["POST"])
@superadmin_required
def password_recovery_company_user_reset():
    from app import User, db, record_audit

    _require_superadmin()
    if not _require_superadmin_step_up():
        return _redirect_back("saas.password_recovery_panel")
    raw_user_id = request.form.get("user_id")
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        flash("Selecciona un usuario de empresa válido.", "danger")
        return _redirect_back("saas.password_recovery_panel")

    user = (
        User.query.filter(
            User.id == user_id,
            User.company_id.isnot(None),
            User.role != "superadmin",
            User.active.is_(True),
        )
        .first()
    )
    if user is None:
        flash("El usuario de empresa no está disponible para restablecer su contraseña.", "danger")
        return _redirect_back("saas.password_recovery_panel")

    temp_password = _temporary_password()
    from services.one_time_secret_service import OneTimeSecretService

    OneTimeSecretService.revoke(
        db.session,
        user_id=current_user.id,
        purpose="password_recovery_temp_password",
        subject_type="user",
        subject_id=user.id,
    )
    _secret_row, access_token = OneTimeSecretService.issue(
        db.session,
        user_id=current_user.id,
        purpose="password_recovery_temp_password",
        subject_type="user",
        subject_id=user.id,
        secret_value=temp_password,
    )
    user.set_password(temp_password)
    user.must_change_password = True
    record_audit(
        action="superadmin_company_user_password_reset",
        entity="user",
        entity_id=user.id,
        user_id=current_user.id,
        company_id=user.company_id,
        detail="Contraseña temporal generada desde recuperación de contraseñas.",
    )
    db.session.commit()

    session["password_recovery_temp_password"] = access_token
    session["password_recovery_temp_password_user_id"] = user.id
    session["password_recovery_temp_password_user"] = user.username
    flash("Contraseña temporal generada. Copiala ahora; se mostrará una sola vez.", "warning")
    return _redirect_back("saas.password_recovery_panel")


@bp.route("/password-recovery/<int:request_id>/status", methods=["POST"])
@superadmin_required
def password_recovery_update_status(request_id):
    from app import PasswordRecoveryRequest, db

    _require_superadmin()
    item = PasswordRecoveryRequest.query.filter_by(id=request_id).first_or_404()
    status = (request.form.get("status") or "").strip().lower()
    if status not in {"pendiente", "atendida", "cerrada"}:
        flash("Estado invalido.", "danger")
        return _redirect_back("saas.password_recovery_panel")

    item.status = status
    if status in {"atendida", "cerrada"}:
        item.processed_at = utcnow()
        item.processed_by_user_id = current_user.id
    else:
        item.processed_at = None
        item.processed_by_user_id = None
    db.session.commit()
    flash("Estado actualizado.", "success")
    return _redirect_back("saas.password_recovery_panel")


@bp.route("/password-recovery/<int:request_id>/reset", methods=["POST"])
@superadmin_required
def password_recovery_reset(request_id):
    from app import PasswordRecoveryRequest, User, db, record_audit

    _require_superadmin()
    if not _require_superadmin_step_up():
        return _redirect_back("saas.password_recovery_panel" )
    item = PasswordRecoveryRequest.query.filter_by(id=request_id).first_or_404()
    user = db.session.get(User, item.user_id)
    if user is None:
        flash("Usuario no encontrado.", "danger")
        return _redirect_back("saas.password_recovery_panel")

    temp_password = _temporary_password()
    from services.one_time_secret_service import OneTimeSecretService

    OneTimeSecretService.revoke(
        db.session,
        user_id=current_user.id,
        purpose="password_recovery_temp_password",
        subject_type="user",
        subject_id=user.id,
    )
    _secret_row, access_token = OneTimeSecretService.issue(
        db.session,
        user_id=current_user.id,
        purpose="password_recovery_temp_password",
        subject_type="user",
        subject_id=user.id,
        secret_value=temp_password,
    )
    user.set_password(temp_password)
    user.must_change_password = True

    item.status = "atendida"
    item.processed_at = utcnow()
    item.processed_by_user_id = current_user.id

    record_audit(
        action="password_recovery_reset",
        entity="password_recovery_request",
        entity_id=item.id,
        detail=f"Password temporal generada para user_id={user.id}",
        user_id=current_user.id,
        company_id=item.company_id,
    )
    db.session.commit()

    # La sesión conserva solo un token opaco; el secreto permanece cifrado del lado servidor.
    session["password_recovery_temp_password"] = access_token
    session["password_recovery_temp_password_user_id"] = user.id
    session["password_recovery_temp_password_user"] = user.username
    flash("Contrasena temporal generada. Se mostrara una sola vez.", "warning")
    return _redirect_back("saas.password_recovery_panel")


@bp.route("/plans")
@superadmin_required
def plans_panel():
    from app import Plan

    _require_superadmin()
    plans = Plan.query.order_by(Plan.price.asc()).all()
    return render_template("saas/plans.html", plans=plans)


@bp.route("/payments")
@superadmin_required
def payments_panel():
    from app import Payment

    _require_superadmin()
    payments = Payment.query.order_by(Payment.created_at.desc()).limit(200).all()
    return render_template("saas/payments.html", payments=payments, payment_flow_label=payment_flow_label)


@bp.route("/trials")
@superadmin_required
def trials_panel():
    from app import Company, Subscription

    _require_superadmin()
    trials = (
        Subscription.query.filter(Subscription.status == "trial")
        .order_by(Subscription.starts_at.desc().nullslast(), Subscription.id.desc())
        .all()
    )
    companies = {company.id: company for company in Company.query.filter(Company.id.in_([sub.company_id for sub in trials])).all()} if trials else {}
    return render_template("saas/trials.html", trials=trials, companies=companies)


@bp.route("/renewals")
@superadmin_required
def renewals_panel():
    from app import Subscription, utcnow

    _require_superadmin()
    upcoming = (
        Subscription.query.filter(
            Subscription.renewal_enabled.is_(True),
            Subscription.next_billing_date.isnot(None),
            Subscription.next_billing_date >= utcnow(),
        )
        .order_by(Subscription.next_billing_date.asc())
        .limit(200)
        .all()
    )
    return render_template("saas/renewals.html", renewals=upcoming)


@bp.route("/logs")
@superadmin_required
def logs_panel():
    from app import AuditLog, Company, db

    _require_superadmin()
    action = (request.args.get("action") or "").strip()[:120]
    company_id = request.args.get("company_id", type=int)
    q = (request.args.get("q") or "").strip()[:180]
    days = request.args.get("days", type=int) or 30
    days = max(1, min(days, 365))

    query = AuditLog.query
    if action:
        query = query.filter(AuditLog.action == action)
    if company_id:
        query = query.filter(AuditLog.company_id == company_id)
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                AuditLog.action.ilike(like),
                AuditLog.entity.ilike(like),
                AuditLog.detail.ilike(like),
            )
        )
    cutoff = utcnow() - timedelta(days=days)
    query = query.filter(AuditLog.created_at >= cutoff)
    logs = query.order_by(AuditLog.created_at.desc()).limit(500).all()
    companies = Company.query.order_by(Company.name.asc()).all()
    actions = [row[0] for row in db.session.query(AuditLog.action).filter(AuditLog.action.isnot(None)).distinct().order_by(AuditLog.action.asc()).limit(200).all()]
    return render_template(
        "saas/logs.html",
        logs=logs,
        companies=companies,
        actions=actions,
        filters={"action": action, "company_id": company_id, "q": q, "days": days},
    )


@bp.route("/server-status")
@superadmin_required
def server_status():
    from app import db

    _require_superadmin()
    db_ok = True
    db_error = None
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_error = str(exc)

    redis_state = _redis_service_status()
    context = {
        "db_ok": db_ok,
        "db_error": db_error,
        "redis_status": redis_state["label"],
        "redis_detail": redis_state["detail"],
        "redis_color": redis_state["color"],
        "flask_env": os.environ.get("FLASK_ENV", "development"),
        "render": bool(os.environ.get("RENDER")),
        "database_url_configured": bool(os.environ.get("DATABASE_URL")),
        "redis_url_configured": bool(os.environ.get("REDIS_URL")),
    }
    return render_template("saas/server_status.html", status=context)


@bp.route("/backups")
@superadmin_required
def backups_panel():
    from app import Company

    _require_superadmin()
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").strip().lower()
    plan_code = (request.args.get("plan") or "all").strip().lower()
    company_id = request.args.get("company_id", type=int)
    preview_id = request.args.get("preview_id", type=int)

    backups = BackupService.superadmin_backups(q=q, company_id=company_id, status=status, plan_code=plan_code)
    companies = Company.query.order_by(Company.name.asc()).all()
    backup_summaries = {}
    selected_backup = None
    selected_backup_summary = None
    for backup in backups:
        try:
            backup_summaries[backup.id] = BackupService.summarize_backup(backup)
        except Exception:
            backup_summaries[backup.id] = {"schema_version": "-", "system_version": "-", "company_id": backup.company_id, "generated_at": None, "products": 0, "inventory": 0, "categories": 0, "clients": 0, "sales": 0, "employees": 0, "schedules": 0}
    if preview_id:
        selected_backup = next((item for item in backups if item.id == preview_id), None)
        if selected_backup is not None:
            selected_backup_summary = backup_summaries.get(selected_backup.id)
    return render_template(
        "saas/backups.html",
        backups=backups,
        companies=companies,
        filters={"q": q, "status": status, "plan": plan_code, "company_id": company_id},
        backup_summaries=backup_summaries,
        selected_backup=selected_backup,
        selected_backup_summary=selected_backup_summary,
        backup_section_options=BackupService.restore_section_options(),
        format_size=_format_size,
    )


@bp.route("/backups/create", methods=["POST"])
@superadmin_required
def backups_create():
    from app import db, record_audit

    _require_superadmin()
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        flash("Seleccioná una empresa para crear el backup.", "warning")
        return _redirect_back("saas.backups_panel")

    backup, plan = BackupService.create_manual_backup(company_id, user_id=current_user.id, trigger_type="manual_superadmin")
    record_audit(
        action="backup_create_superadmin",
        entity="backup",
        entity_id=backup.id,
        company_id=company_id,
        detail=f"Backup creado por superadmin. plan={plan['code']}",
        user_id=current_user.id,
    )
    db.session.commit()
    flash("Backup creado correctamente.", "success")
    return _redirect_back("saas.backups_panel")


@bp.route("/backups/import", methods=["POST"])
@superadmin_required
def backups_import():
    from app import db, record_audit

    _require_superadmin()
    company_id = request.form.get("company_id", type=int)
    backup_file = request.files.get("backup_file")
    if not company_id:
        flash("Seleccioná una empresa para importar el backup.", "warning")
        return _redirect_back("saas.backups_panel")
    if not backup_file or not getattr(backup_file, "filename", "").strip():
        flash("Seleccioná un archivo de backup válido.", "warning")
        return _redirect_back("saas.backups_panel")

    try:
        backup, plan, payload = BackupService.import_backup_file(company_id=company_id, file_storage=backup_file, created_by_user_id=current_user.id, trigger_type="manual_superadmin_import")
        record_audit(
            action="backup_import_superadmin",
            entity="backup",
            entity_id=backup.id,
            company_id=company_id,
            detail=f"Backup importado por superadmin. plan={plan['code']} version={payload.get('schema_version')}",
            user_id=current_user.id,
        )
        db.session.commit()
        flash("Backup importado correctamente.", "success")
        return redirect(url_for("saas.backups_panel", preview_id=backup.id, company_id=company_id))
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("No se pudo importar el backup global: %s", exc)
        flash("No se pudo importar el backup.", "danger")
        return _redirect_back("saas.backups_panel")


@bp.route("/backups/<int:backup_id>/download")
@superadmin_required
def backups_download(backup_id):
    from app import BackupLog

    _require_superadmin()
    backup = BackupLog.query.filter_by(id=backup_id).first_or_404()
    backup_path = BackupService.backup_download_path(backup)
    return send_file(
        backup_path,
        mimetype="application/gzip",
        as_attachment=True,
        download_name=backup.file_name or backup_path.name,
    )


@bp.route("/backups/<int:backup_id>/verify", methods=["POST"])
@superadmin_required
def backups_verify(backup_id):
    from app import BackupLog, record_audit

    backup = BackupLog.query.filter_by(id=backup_id).first_or_404()
    try:
        result = BackupService.verify_backup(backup, expected_company_id=backup.company_id)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        record_audit(
            action="superadmin_backup_verification_failed",
            entity="backup",
            entity_id=backup.id,
            detail=f"Verificación fallida para company={backup.company_id}: {exc}",
        )
        flash(f"Verificación fallida: {exc}", "danger")
    else:
        record_audit(
            action="superadmin_backup_verified",
            entity="backup",
            entity_id=backup.id,
            detail=(
                f"Backup verificado company={backup.company_id} "
                f"schema={result['schema_version']} counts={result['counts']}"
            ),
        )
        flash(
            f"Backup #{backup.id} verificado correctamente: "
            f"{result['counts']['products']} productos, "
            f"{result['counts']['clients']} clientes y "
            f"{result['counts']['sales']} ventas.",
            "success",
        )

    return redirect(url_for("saas.backups_panel", preview_id=backup.id))


@bp.route("/backups/<int:backup_id>/restore", methods=["POST"])
@superadmin_required
def backups_restore(backup_id):
    from app import BackupLog, db, record_audit

    _require_superadmin()
    backup = BackupLog.query.filter_by(id=backup_id).first_or_404()
    sections = request.form.getlist("sections")
    confirm_restore = (request.form.get("confirm_restore") or "").strip() == "1"
    if not confirm_restore:
        return redirect(url_for("saas.backups_panel", preview_id=backup.id, company_id=backup.company_id))
    if not _require_superadmin_step_up():
        return _redirect_back("saas.backups_panel")

    try:
        BackupService.restore_backup(backup, expected_company_id=backup.company_id, restored_by_user_id=current_user.id, sections=sections)
        record_audit(
            action="backup_restore_superadmin",
            entity="backup",
            entity_id=backup.id,
            company_id=backup.company_id,
            detail=f"Backup restaurado por superadmin. sections={','.join(sections or ['full'])}",
            user_id=current_user.id,
        )
        db.session.commit()
        flash("Backup restaurado correctamente.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("No se pudo restaurar el backup global: %s", exc)
        flash("No se pudo restaurar el backup.", "danger")
    return _redirect_back("saas.backups_panel")


@bp.route("/backups/<int:backup_id>/delete", methods=["POST"])
@superadmin_required
def backups_delete(backup_id):
    from app import BackupLog, db, record_audit

    _require_superadmin()
    backup = BackupLog.query.filter_by(id=backup_id).first_or_404()
    confirm_delete = (request.form.get("confirm_delete") or "").strip() == "1"
    if not confirm_delete:
        flash("Confirmá la eliminación del backup para continuar.", "warning")
        return _redirect_back("saas.backups_panel")
    if not _require_superadmin_step_up():
        return _redirect_back("saas.backups_panel")
    try:
        company_id = backup.company_id
        BackupService.delete_backup(backup)
        record_audit(
            action="backup_delete_superadmin",
            entity="backup",
            entity_id=backup_id,
            company_id=company_id,
            detail="Backup eliminado por superadmin.",
            user_id=current_user.id,
        )
        db.session.commit()
        flash("Backup eliminado correctamente.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("No se pudo eliminar el backup global id=%s: %s", backup_id, exc)
        flash("No se pudo eliminar el backup.", "danger")
    return _redirect_back("saas.backups_panel")




def _commercial_inbox_rows(company, *, q: str = "", state: str = "pending"):
    from app import SaaSLead, db
    from stockarmobile.models.conversations import Conversation, ConversationMessage
    from services.saas_commercial_whatsapp import commercial_conversation_attention

    conversations = (
        Conversation.query
        .filter(
            Conversation.company_id == int(company.id),
            Conversation.channel == "whatsapp_commercial",
        )
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(200)
        .all()
    )
    query_text = (q or "").strip().lower()
    rows = []
    for conversation in conversations:
        phone = str(conversation.external_conversation_id or "").strip()
        attention = commercial_conversation_attention(conversation)
        attention_state = attention["status"]
        if state == "pending" and not attention["pending"]:
            continue
        if state == "human" and attention_state != "human":
            continue
        if state == "ai" and attention_state not in {"", "ai"}:
            continue
        if state == "resolved" and attention_state != "resolved":
            continue
        if query_text and query_text not in phone.lower():
            continue

        normalized_phone = "".join(ch for ch in phone if ch.isdigit())[:40]
        lead = (
            SaaSLead.query
            .filter(db.or_(SaaSLead.whatsapp == normalized_phone, SaaSLead.phone == normalized_phone))
            .order_by(SaaSLead.id.desc())
            .first()
        )
        latest = (
            ConversationMessage.query
            .filter(ConversationMessage.company_id == int(company.id), ConversationMessage.conversation_id == int(conversation.id))
            .order_by(ConversationMessage.id.desc())
            .first()
        )
        rows.append({
            "conversation_id": conversation.id,
            "phone": phone or "Sin número",
            "contact_name": (getattr(lead, "contact_name", None) or getattr(lead, "company_name", None) or "Prospecto"),
            "company_name": getattr(lead, "company_name", None) or "Prospecto de WhatsApp",
            "lead_id": getattr(lead, "id", None),
            "attention": attention,
            "latest_text": (latest.content if latest is not None else ""),
            "latest_sender_type": (latest.sender_type if latest is not None else ""),
            "latest_at": _format_admin_datetime_local(getattr(latest, "created_at", None), "%d/%m %H:%M") if latest is not None else "",
            "updated_at": _format_admin_datetime_local(getattr(conversation, "updated_at", None), "%d/%m %H:%M"),
        })
    return rows


@bp.route("/whatsapp-comercial/atencion", methods=["GET"])
@superadmin_required
def whatsapp_commercial_attention():
    from app import SaaSLead, db
    from stockarmobile.models.conversations import Conversation, ConversationMessage
    from services.saas_commercial_whatsapp import (
        COMMERCIAL_CHANNEL,
        commercial_conversation_attention,
        get_commercial_company,
    )

    _require_superadmin()
    company = get_commercial_company()
    if company is None:
        flash("La empresa interna StockArMobile Comercial no está inicializada.", "danger")
        return redirect(url_for("saas.whatsapp_commercial_settings"))

    q = (request.args.get("q") or "").strip()
    state = (request.args.get("state") or "pending").strip().lower()
    if state not in {"all", "pending", "human", "ai", "resolved"}:
        state = "pending"

    # The default queue is human-pending. Other tabs are available without
    # changing any conversation state.
    rows = _commercial_inbox_rows(company, q=q, state=state)

    total_rows = _commercial_inbox_rows(company, q="", state="all")
    pending_count = sum(1 for row in total_rows if row["attention"]["pending"])
    human_count = sum(1 for row in total_rows if row["attention"]["status"] == "human")
    ai_count = sum(1 for row in total_rows if row["attention"]["status"] in {"", "ai"})
    resolved_count = sum(1 for row in total_rows if row["attention"]["status"] == "resolved")

    selected_id = request.args.get("conversation_id", type=int)
    selected = None
    selected_messages = []
    if selected_id:
        selected = (
            Conversation.query
            .filter_by(
                id=int(selected_id),
                company_id=int(company.id),
                channel=COMMERCIAL_CHANNEL,
            )
            .first()
        )
    if selected is None:
        preferred = next((row for row in rows if row["attention"]["pending"]), None) or (rows[0] if rows else None)
        if preferred:
            selected = (
                Conversation.query
                .filter_by(
                    id=int(preferred["conversation_id"]),
                    company_id=int(company.id),
                    channel=COMMERCIAL_CHANNEL,
                )
                .first()
            )

    selected_view = None
    if selected is not None:
        normalized_phone = "".join(ch for ch in str(selected.external_conversation_id or "") if ch.isdigit())[:40]
        lead = (
            SaaSLead.query
            .filter(db.or_(SaaSLead.whatsapp == normalized_phone, SaaSLead.phone == normalized_phone))
            .order_by(SaaSLead.id.desc())
            .first()
        )
        selected_messages = (
            ConversationMessage.query
            .filter(
                ConversationMessage.company_id == int(company.id),
                ConversationMessage.conversation_id == int(selected.id),
            )
            .order_by(ConversationMessage.id.desc())
            .limit(200)
            .all()
        )
        selected_messages.reverse()
        selected_view = {
            "conversation_id": selected.id,
            "phone": str(selected.external_conversation_id or ""),
            "contact_name": getattr(lead, "contact_name", None) or "Prospecto",
            "company_name": getattr(lead, "company_name", None) or "Prospecto de WhatsApp",
            "email": getattr(lead, "email", None) or "",
            "attention": commercial_conversation_attention(selected),
            "created_at": _format_admin_datetime_local(selected.created_at, "%d/%m/%Y %H:%M"),
            "messages": [
                {
                    "id": msg.id,
                    "sender_type": msg.sender_type,
                    "content": msg.content,
                    "created_at": _format_admin_datetime_local(msg.created_at, "%d/%m/%Y %H:%M"),
                }
                for msg in selected_messages
            ],
        }

    return render_template(
        "saas/whatsapp_commercial_attention.html",
        company=company,
        rows=rows,
        selected=selected_view,
        q=q,
        state=state,
        counts={
            "pending": pending_count,
            "human": human_count,
            "ai": ai_count,
            "resolved": resolved_count,
        },
    )


@bp.post("/whatsapp-comercial/atencion/<int:conversation_id>/action")
@superadmin_required
def whatsapp_commercial_attention_action(conversation_id: int):
    from app import db, record_audit
    from stockarmobile.models.conversations import Conversation
    from services.saas_commercial_whatsapp import (
        COMMERCIAL_CHANNEL,
        _set_commercial_attention,
        clear_commercial_attention,
        commercial_conversation_attention,
        get_commercial_company,
        resume_commercial_ai,
    )

    _require_superadmin()
    company = get_commercial_company()
    conversation = (
        Conversation.query
        .filter_by(
            id=int(conversation_id),
            company_id=int(company.id) if company is not None else -1,
            channel=COMMERCIAL_CHANNEL,
        )
        .first()
        if company is not None
        else None
    )
    if company is None or conversation is None:
        abort(404)

    action = (request.form.get("action") or "").strip().lower()
    attention = commercial_conversation_attention(conversation)
    reason = (request.form.get("reason") or "").strip()[:500]

    if action == "take":
        taken_by = attention.get("taken_by_user_id")
        if taken_by and int(taken_by) != int(current_user.id):
            flash("La conversación ya fue tomada por otro operador.", "warning")
            return redirect(url_for("saas.whatsapp_commercial_attention", conversation_id=conversation.id))
        _set_commercial_attention(
            conversation,
            status="human",
            user_id=int(current_user.id),
            reason=reason or attention.get("reason") or "Tomada desde Atención Comercial.",
        )
        message = "Conversación tomada. Comercial IA queda pausado para este prospecto."
        audit_action = "commercial_whatsapp_take_human"
    elif action == "resolve":
        resume_commercial_ai(conversation, user_id=int(current_user.id))
        message = "Atención humana marcada como resuelta. La próxima consulta del prospecto podrá retomar Comercial IA."
        audit_action = "commercial_whatsapp_resolve_human"
    elif action == "resume_ai":
        clear_commercial_attention(conversation)
        message = "Comercial IA reanudado para esta conversación."
        audit_action = "commercial_whatsapp_resume_ai"
    else:
        abort(400)

    record_audit(
        action=audit_action,
        entity="conversation",
        entity_id=conversation.id,
        company_id=company.id,
        detail=message,
        user_id=current_user.id,
    )
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("saas.whatsapp_commercial_attention", conversation_id=conversation.id))


@bp.post("/whatsapp-comercial/atencion/<int:conversation_id>/delete")
@superadmin_required
def whatsapp_commercial_attention_delete(conversation_id: int):
    from app import db, record_audit
    from services.saas_commercial_whatsapp import delete_commercial_conversation

    if not _require_superadmin_step_up():
        return redirect(url_for("saas.whatsapp_commercial_attention", conversation_id=conversation_id))

    try:
        result = delete_commercial_conversation(conversation_id)
        record_audit(
            action="commercial_whatsapp_conversation_delete",
            entity="conversation",
            entity_id=conversation_id,
            detail=(
                f"Conversación comercial eliminada. "
                f"messages_deleted={result['messages_deleted']} "
                f"participants_deleted={result['participants_deleted']}."
            ),
            user_id=current_user.id,
            company_id=result["company_id"],
            ip_address=request.remote_addr,
        )
        db.session.commit()
        flash("Conversación eliminada. El prospecto de CRM se conservó.", "warning")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "No se pudo eliminar conversación de WhatsApp comercial conversation_id=%s",
            conversation_id,
        )
        flash("No se pudo eliminar la conversación. No se aplicaron cambios.", "danger")

    return redirect(url_for("saas.whatsapp_commercial_attention"))


@bp.post("/whatsapp-comercial/atencion/<int:conversation_id>/mensaje")
@superadmin_required
def whatsapp_commercial_attention_message(conversation_id: int):
    from app import db, record_audit
    from stockarmobile.models.conversations import Conversation
    from services.saas_commercial_whatsapp import (
        COMMERCIAL_CHANNEL,
        _set_commercial_attention,
        commercial_conversation_attention,
        get_commercial_company,
        send_commercial_human_message,
    )

    _require_superadmin()
    company = get_commercial_company()
    if company is None:
        abort(404)
    conversation = (
        Conversation.query
        .filter_by(
            id=int(conversation_id),
            company_id=int(company.id),
            channel=COMMERCIAL_CHANNEL,
        )
        .first()
    )
    if conversation is None:
        abort(404)

    if commercial_conversation_attention(conversation)["status"] != "human":
        _set_commercial_attention(
            conversation,
            status="human",
            user_id=int(current_user.id),
            reason="Tomada al enviar respuesta desde Atención Comercial.",
        )
        db.session.flush()

    body = (request.form.get("body") or "").strip()
    try:
        result = send_commercial_human_message(
            company=company,
            conversation=conversation,
            body=body,
            user_id=int(current_user.id),
        )
        record_audit(
            action="commercial_whatsapp_human_message",
            entity="conversation",
            entity_id=conversation.id,
            company_id=company.id,
            detail=f"Mensaje humano enviado por SuperAdmin. message_id={result.get('message_id')}.",
            user_id=current_user.id,
        )
        flash("Mensaje enviado por WhatsApp.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
    except PermissionError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("No se pudo enviar mensaje humano de WhatsApp comercial.")
        flash("No se pudo enviar el mensaje. Revisá la conexión de WhatsApp.", "danger")
    return redirect(url_for("saas.whatsapp_commercial_attention", conversation_id=conversation.id))



@bp.route("/stats")
@superadmin_required
def global_stats():
    return redirect(url_for("saas.index"))


@bp.route("/mercadopago")
@superadmin_required
def mercadopago_settings():
    _require_superadmin()
    return render_template("saas/mercadopago.html", mp_config=load_billing_config())


@bp.route("/settings")
@superadmin_required
def global_settings():
    _require_superadmin()
    settings_snapshot = {
        "app_url": os.environ.get("APP_URL") or "",
        "secret_key_configured": bool(os.environ.get("SECRET_KEY")),
        "mp_access_token_configured": bool(os.environ.get("MP_ACCESS_TOKEN")),
        "mp_public_key_configured": bool(os.environ.get("MP_PUBLIC_KEY")),
        "mp_webhook_secret_configured": bool(os.environ.get("MP_WEBHOOK_SECRET")),
    }
    return render_template("saas/settings.html", settings_snapshot=settings_snapshot)


@bp.route("/whatsapp-comercial", methods=["GET", "POST"])
@superadmin_required
def whatsapp_commercial_settings():
    """Configure the isolated StockArMobile commercial WhatsApp channel."""
    from app import db, record_audit
    from services.ai_agent.config_service import (
        configure_whatsapp_connection,
        get_ai_preferences,
        get_whatsapp_connection,
        update_ai_preferences,
    )
    from services.saas_commercial_whatsapp import get_commercial_company

    _require_superadmin()
    company = get_commercial_company()

    if request.method == "POST":
        if company is None:
            flash("La empresa interna StockArMobile Comercial todavía no está creada. Aplicá la migración de WhatsApp comercial antes de configurar el canal.", "danger")
            return redirect(url_for("saas.whatsapp_commercial_settings"))

        phone_number_id = (request.form.get("phone_number_id") or "").strip()
        waba_id = (request.form.get("waba_id") or "").strip()
        display_phone_number = (request.form.get("display_phone_number") or "").strip()
        access_token = (request.form.get("access_token") or "").strip()
        template_name = (request.form.get("template_name") or "").strip()
        template_language = (request.form.get("template_language") or "es_AR").strip() or "es_AR"
        enabled = (request.form.get("enabled") or "") == "1"
        business_id = (request.form.get("business_id") or "").strip()

        if enabled and (not phone_number_id or not waba_id):
            flash("Para activar WhatsApp Comercial necesitás informar el Phone Number ID y el WABA ID.", "danger")
            return redirect(url_for("saas.whatsapp_commercial_settings"))

        try:
            configure_whatsapp_connection(
                company,
                phone_number_id=phone_number_id,
                access_token=access_token or None,
                business_account_id=waba_id,
                display_phone_number=display_phone_number,
                enabled=enabled,
                template_name=template_name,
                template_language=template_language,
            )
            whatsapp_updates = {"waba_id": waba_id, "business_id": business_id}
            update_ai_preferences(company, whatsapp_updates=whatsapp_updates)
            record_audit(
                action="superadmin_whatsapp_commercial_configured",
                entity="whatsapp_commercial",
                entity_id=company.id,
                company_id=company.id,
                detail=f"WhatsApp Comercial configurado. enabled={enabled}, phone_number_id={phone_number_id}, waba_id={waba_id}.",
                user_id=current_user.id,
            )
            db.session.commit()
            flash("Configuración de WhatsApp Comercial guardada.", "success")
        except Exception:
            db.session.rollback()
            current_app.logger.exception("No se pudo guardar la configuración de WhatsApp Comercial.")
            flash("No se pudo guardar la configuración de WhatsApp Comercial.", "danger")
        return redirect(url_for("saas.whatsapp_commercial_settings"))

    connection = get_whatsapp_connection(company) if company is not None else {
        "enabled": False,
        "phone_number_id": "",
        "business_account_id": "",
        "display_phone_number": "",
        "template_name": "",
        "template_language": "es_AR",
        "access_token": "",
    }
    stored = get_ai_preferences(company)["whatsapp"] if company is not None else {}
    env_enabled = str(os.getenv("WHATSAPP_COMMERCIAL_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}
    env_phone = str(os.getenv("WHATSAPP_COMMERCIAL_PHONE_NUMBER_ID") or "").strip()
    env_waba = str(os.getenv("WHATSAPP_COMMERCIAL_WABA_ID") or "").strip()
    token_configured = bool(str(connection.get("access_token") or "").strip())
    return render_template(
        "saas/whatsapp_commercial.html",
        company=company,
        connection=connection,
        stored=stored,
        env_enabled=env_enabled,
        env_phone=env_phone,
        env_waba=env_waba,
        token_configured=token_configured,
        webhook_url=url_for("whatsapp_agent.webhook", _external=True),
    )


@bp.post("/whatsapp-comercial/migrar-conexion")
@superadmin_required
def whatsapp_commercial_migrate_connection():
    """Move an existing tenant WhatsApp connection into SuperAdmin safely."""
    from app import Company, db, record_audit
    from services.ai_agent.config_service import get_ai_preferences, save_company_preferences
    from services.saas_commercial_whatsapp import get_commercial_company

    _require_superadmin()
    destination = get_commercial_company()
    phone_number_id = (request.form.get("phone_number_id") or "").strip()
    confirm = (request.form.get("confirm_migration") or "") == "1"

    if destination is None:
        flash("La empresa interna StockArMobile Comercial no está inicializada.", "danger")
        return redirect(url_for("saas.whatsapp_commercial_settings"))
    if not phone_number_id:
        flash("Informá el Phone Number ID del número que hoy usa el Vendedor IA.", "danger")
        return redirect(url_for("saas.whatsapp_commercial_settings"))
    if not confirm:
        flash("Confirmá la migración de la conexión para continuar.", "warning")
        return redirect(url_for("saas.whatsapp_commercial_settings"))

    candidates = []
    for source in Company.query.filter(Company.active.is_(True)).order_by(Company.id.asc()).all():
        if int(source.id) == int(destination.id):
            continue
        prefs = get_ai_preferences(source)
        wa = prefs.get("whatsapp") if isinstance(prefs.get("whatsapp"), dict) else {}
        if str(wa.get("phone_number_id") or "").strip() != phone_number_id:
            continue
        if not str(wa.get("access_token_encrypted") or "").strip():
            continue
        candidates.append((source, prefs, wa))

    if len(candidates) != 1:
        if not candidates:
            flash("No encontré una conexión activa de Vendedor IA con ese Phone Number ID.", "danger")
        else:
            flash("Encontré más de una conexión con ese Phone Number ID. No migré nada por seguridad.", "danger")
        return redirect(url_for("saas.whatsapp_commercial_settings"))

    source, source_prefs, source_wa = candidates[0]
    try:
        destination_full = json.loads(destination.preferences_json or "{}") if isinstance(destination.preferences_json, str) else {}
        if not isinstance(destination_full, dict):
            destination_full = {}
        destination_ai = destination_full.get("ai_agent") if isinstance(destination_full.get("ai_agent"), dict) else {}
        destination_whatsapp = dict(source_wa)
        destination_whatsapp["enabled"] = True
        destination_ai["whatsapp"] = destination_whatsapp
        destination_full["internal_channel"] = COMMERCIAL_COMPANY_MARKER
        destination_full["ai_agent"] = destination_ai
        save_company_preferences(destination, destination_full)

        source_full = json.loads(source.preferences_json or "{}") if isinstance(source.preferences_json, str) else {}
        if not isinstance(source_full, dict):
            source_full = dict(source_prefs or {})
        source_ai = source_full.get("ai_agent") if isinstance(source_full.get("ai_agent"), dict) else {}
        source_whatsapp = dict(source_wa)
        source_whatsapp["enabled"] = False
        source_whatsapp["phone_number_id"] = ""
        source_whatsapp["business_account_id"] = ""
        source_whatsapp["display_phone_number"] = ""
        source_whatsapp.pop("access_token_encrypted", None)
        source_ai["whatsapp"] = source_whatsapp
        source_ai["whatsapp_enabled"] = False
        source_full["ai_agent"] = source_ai
        save_company_preferences(source, source_full)

        record_audit(
            action="superadmin_whatsapp_commercial_migrated",
            entity="whatsapp_commercial",
            entity_id=destination.id,
            company_id=destination.id,
            detail=f"Conexión WhatsApp migrada desde company_id={source.id}. phone_number_id={phone_number_id}.",
            user_id=current_user.id,
        )
        record_audit(
            action="tenant_whatsapp_connection_moved_to_superadmin",
            entity="whatsapp_connection",
            entity_id=source.id,
            company_id=source.id,
            detail=f"Conexión WhatsApp movida a SuperAdmin. destination_company_id={destination.id}.",
            user_id=current_user.id,
        )
        db.session.commit()
        flash(f"Conexión migrada desde '{source.name}' a WhatsApp Comercial de Super Admin.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("No se pudo migrar la conexión WhatsApp al SuperAdmin.")
        flash("No se pudo completar la migración. No se aplicaron cambios.", "danger")
    return redirect(url_for("saas.whatsapp_commercial_settings"))


@bp.route("/landing/testimonials", methods=["GET", "POST"])
@superadmin_required
def landing_testimonials_panel():
    from app import LandingTestimonial, db

    _require_superadmin()
    if request.method == "POST":
        author_name = (request.form.get("author_name") or "").strip()
        company_name = (request.form.get("company_name") or "").strip()
        quote = (request.form.get("quote") or "").strip()
        active = (request.form.get("active") or "1") == "1"

        if not author_name or not quote:
            flash("Autor y testimonio son obligatorios.", "danger")
            return redirect(url_for("saas.landing_testimonials_panel"))

        row = LandingTestimonial(
            author_name=author_name[:120],
            company_name=company_name[:160] or None,
            quote=quote,
            active=active,
        )
        db.session.add(row)
        db.session.commit()
        flash("Testimonio guardado correctamente.", "success")
        return redirect(url_for("saas.landing_testimonials_panel"))

    testimonials = LandingTestimonial.query.order_by(LandingTestimonial.created_at.desc()).all()
    return render_template("saas/landing_testimonials.html", testimonials=testimonials)


@bp.route("/landing/testimonials/<int:testimonial_id>/toggle", methods=["POST"])
@superadmin_required
def landing_testimonials_toggle(testimonial_id):
    from app import LandingTestimonial, db

    _require_superadmin()
    row = LandingTestimonial.query.filter_by(id=testimonial_id).first_or_404()
    row.active = not row.active
    db.session.commit()
    flash("Estado del testimonio actualizado.", "success")
    return redirect(url_for("saas.landing_testimonials_panel"))


@bp.route("/landing/testimonials/<int:testimonial_id>/update", methods=["POST"])
@superadmin_required
def landing_testimonials_update(testimonial_id):
    from app import LandingTestimonial, db

    _require_superadmin()
    row = LandingTestimonial.query.filter_by(id=testimonial_id).first_or_404()

    author_name = (request.form.get("author_name") or "").strip()
    company_name = (request.form.get("company_name") or "").strip()
    quote = (request.form.get("quote") or "").strip()
    active = (request.form.get("active") or "1") == "1"

    if not author_name or not quote:
        flash("Autor y testimonio son obligatorios para actualizar.", "danger")
        return redirect(url_for("saas.landing_testimonials_panel"))

    row.author_name = author_name[:120]
    row.company_name = company_name[:160] or None
    row.quote = quote
    row.active = active
    db.session.commit()
    flash("Testimonio actualizado correctamente.", "success")
    return redirect(url_for("saas.landing_testimonials_panel"))


@bp.route("/landing/testimonials/<int:testimonial_id>/delete", methods=["POST"])
@superadmin_required
def landing_testimonials_delete(testimonial_id):
    from app import LandingTestimonial, db

    _require_superadmin()
    row = LandingTestimonial.query.filter_by(id=testimonial_id).first_or_404()
    db.session.delete(row)
    db.session.commit()
    flash("Testimonio eliminado.", "warning")
    return redirect(url_for("saas.landing_testimonials_panel"))


@bp.route("/metrics.xlsx")
@superadmin_required
def export_metrics():
    from app import Company, Payment, Plan, Subscription, User

    _require_superadmin()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Metricas SaaS"

    mrr = (
        Plan.query.with_entities(Plan.price, Subscription.status)
        .join(Subscription, Subscription.plan_id == Plan.id)
        .filter(Subscription.status.in_(["active", "approved"]))
        .all()
    )
    mrr_total = sum(float(row.price or 0) for row in mrr)

    rows = [
        ("Empresas", Company.query.count()),
        ("Empresas activas", Company.query.filter_by(active=True).count()),
        ("Usuarios", User.query.count()),
        ("Suscripciones", Subscription.query.count()),
        ("Empresas trial", Subscription.query.filter(Subscription.status == "trial").count()),
        ("Empresas suspendidas", Subscription.query.filter(Subscription.status.in_(["suspended", "expired", "cancelled", "rejected", "charged_back"])).count()),
        ("Pagos pendientes Standard", Payment.query.filter(standard_subscription_payment_filter(Payment), Payment.status.in_(["pending", "authorized", "in_process"])).count()),
        ("Pagos pendientes IA", Payment.query.filter(ai_subscription_payment_filter(Payment), Payment.status.in_(["pending", "authorized", "in_process"])).count()),
        ("Pagos rechazados Standard", Payment.query.filter(standard_subscription_payment_filter(Payment), Payment.status.in_(["rejected", "cancelled", "expired", "charged_back"])).count()),
        ("Pagos rechazados IA", Payment.query.filter(ai_subscription_payment_filter(Payment), Payment.status.in_(["rejected", "cancelled", "expired", "charged_back"])).count()),
        ("MRR", float(mrr_total)),
        ("ARR", float(mrr_total) * 12),
    ]
    sheet.append(["Metrica", "Valor"])
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"metricas_saas_{utcnow():%Y%m%d}.xlsx",
    )

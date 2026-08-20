"""SaaS y billing: planes, suscripciones, checkout y webhooks Mercado Pago."""

from __future__ import annotations

import json
import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from io import BytesIO
from math import ceil
from time import monotonic
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

from app import model_table_exists, superadmin_required, utcnow
from config.billing_config import load_billing_config
from services.backup_service import BackupService
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

        db.session.execute(sa.text("DELETE FROM companies WHERE id = :company_id"), {"company_id": company_id})


def _action_allowed_for_status(status: str | None, action: str) -> bool:
    if action == "extend":
        return True
    return action in _allowed_ui_actions_for_status(status)


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


def _health_check_snapshot(db_session, now):
    from app import BackupLog, Company, MercadoPagoConnection, Subscription, User, WebhookEvent, db, model_table_exists

    db_ok = True
    db_detail = "Conectada"
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        db_detail = str(exc)

    smtp_ok = bool(os.environ.get("SUPPORT_EMAIL"))
    smtp_warning = not bool(os.environ.get("SUPPORT_EMAIL"))

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
            "data": _service_status(smtp_ok, smtp_warning, "SUPPORT_EMAIL configurado" if smtp_ok else "Falta SUPPORT_EMAIL"),
        },
        {
            "name": "Backups",
            "key": "backups",
            "data": _service_status(backup_total > 0 and backup_failed == 0, backup_total > 0 and backup_failed > 0, f"{backup_total} backups / {backup_failed} con error"),
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
        .filter(Payment.status.in_(["rejected", "charged_back", "cancelled"]), Payment.created_at >= now - timedelta(days=15))
        .order_by(Payment.created_at.desc())
        .limit(20)
        .all()
    )
    for payment in rejected_payments:
        queue.append({
            "company_id": payment.company_id,
            "company_name": payment.company.name if payment.company else f"Empresa #{payment.company_id}",
            "reason": "Pago rechazado",
            "severity": "danger",
            "detail": f"{payment.status} · ${float(payment.amount or 0):.2f}",
        })

    pending_payments = (
        Payment.query.join(Company, Company.id == Payment.company_id)
        .filter(Payment.status.in_(["pending", "authorized", "in_process"]), Payment.created_at >= now - timedelta(days=10))
        .order_by(Payment.created_at.desc())
        .limit(20)
        .all()
    )
    for payment in pending_payments:
        queue.append({
            "company_id": payment.company_id,
            "company_name": payment.company.name if payment.company else f"Empresa #{payment.company_id}",
            "reason": "Pago pendiente",
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

    pending_payments = Payment.query.filter(Payment.status.in_(["pending", "authorized", "in_process"])).count()
    rejected_payments = Payment.query.filter(Payment.status.in_(["rejected", "cancelled", "charged_back", "expired"])).count()
    pending_payments_previous = Payment.query.filter(
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
    income_month = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(Payment.status == "approved", Payment.created_at >= month_start)
        .scalar()
        or 0
    )
    income_month_previous = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(
            Payment.status == "approved",
            Payment.created_at >= previous_month_start,
            Payment.created_at < previous_month_end,
        )
        .scalar()
        or 0
    )
    income_year = (
        db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0))
        .filter(Payment.status == "approved", Payment.created_at >= year_start)
        .scalar()
        or 0
    )

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
    last_payments = Payment.query.order_by(Payment.created_at.desc()).limit(10).all()
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
            Payment.query.filter(Payment.status == "approved", Payment.created_at >= start, Payment.created_at < end).count()
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
        "rejected_payments": rejected_payments,
        "mrr": float(mrr),
        "arr": float(mrr) * 12,
        "monthly_billing": float(monthly_billing),
        "annual_billing": float(annual_billing),
        "income_month": float(income_month),
        "income_year": float(income_year),
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
        .filter(Payment.status == "approved", Payment.created_at >= datetime(now.year, now.month, now.day))
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
        renewals_buckets=renewals_buckets,
        support_metrics=support_metrics,
        activity_timeline=activity_timeline,
        executive_cards=executive_cards,
        quick_actions=quick_actions,
        operations_charts=operations_charts,
    )


@bp.route("/crm", methods=["GET", "POST"])
@superadmin_required
def crm_panel():
    from app import Company, SaaSAlert, SaaSLead, SaaSTask, User, db, record_audit

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

            lead = SaaSLead(
                company_name=company_name[:160],
                contact_name=contact_name[:160],
                email=(request.form.get("email") or "").strip().lower()[:160] or None,
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
    task_status = (request.args.get("task_status") or "all").strip().lower()
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

    if lead_status in CRM_LEAD_STATUSES:
        lead_query = lead_query.filter(SaaSLead.status == lead_status)
    if task_status in CRM_TASK_STATUSES:
        task_query = task_query.filter(SaaSTask.status == task_status)
    if alert_status in CRM_ALERT_STATUSES:
        alert_query = alert_query.filter(SaaSAlert.status == alert_status)

    leads = lead_query.order_by(SaaSLead.updated_at.desc(), SaaSLead.id.desc()).limit(20).all()
    tasks = task_query.order_by(SaaSTask.updated_at.desc(), SaaSTask.id.desc()).limit(20).all()
    alerts = alert_query.order_by(SaaSAlert.updated_at.desc(), SaaSAlert.id.desc()).limit(20).all()

    lead_counts = {status: SaaSLead.query.filter(SaaSLead.status == status).count() for status in CRM_LEAD_STATUSES}
    task_counts = {status: SaaSTask.query.filter(SaaSTask.status == status).count() for status in CRM_TASK_STATUSES}
    alert_counts = {status: SaaSAlert.query.filter(SaaSAlert.status == status).count() for status in CRM_ALERT_STATUSES}

    return render_template(
        "saas/crm.html",
        leads=leads,
        tasks=tasks,
        alerts=alerts,
        companies=companies,
        users=users,
        filters={"q": q, "lead_status": lead_status, "task_status": task_status, "alert_status": alert_status},
        lead_counts=lead_counts,
        task_counts=task_counts,
        alert_counts=alert_counts,
        CRM_LEAD_STATUSES=sorted(CRM_LEAD_STATUSES),
        CRM_TASK_STATUSES=sorted(CRM_TASK_STATUSES),
        CRM_ALERT_STATUSES=sorted(CRM_ALERT_STATUSES),
        CRM_PRIORITIES=sorted(CRM_PRIORITIES),
    )


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


@bp.route("/companies")
@superadmin_required
def companies_panel():
    from app import Client, Company, Plan, Product, Sale, Subscription, User, db
    from services.subscription_service import SubscriptionService

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
    stats = {
        "users": User.query.filter_by(company_id=company.id).count(),
        "active_users": User.query.filter_by(company_id=company.id, active=True).count(),
        "products": Product.query.filter_by(company_id=company.id, active=True).count(),
        "clients": Client.query.filter_by(company_id=company.id, active=True).count(),
        "sales": Sale.query.filter_by(company_id=company.id).count(),
        "sales_amount": float(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).filter(Sale.company_id == company.id).scalar() or 0),
        "payments_approved": float(db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(Payment.company_id == company.id, Payment.status == "approved").scalar() or 0),
    }
    pin_revealed_once = session.pop(f"company_pin_reveal_{company.id}", None)
    last_payments = Payment.query.filter_by(company_id=company.id).order_by(Payment.created_at.desc()).limit(10).all()
    audit = AuditLog.query.filter_by(company_id=company.id).order_by(AuditLog.created_at.desc()).limit(20).all()
    return render_template(
        "saas/company_detail.html",
        company=company,
        subscription=subscription,
        effective_state=effective_state,
        stats=stats,
        last_payments=last_payments,
        audit=audit,
        pin_revealed_once=pin_revealed_once,
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
    raw_pin = (request.form.get("admin_pin") or "").strip()
    if len(raw_pin) != 4 or not raw_pin.isdigit():
        flash("El PIN debe ser numerico y de 4 digitos.", "danger")
        return redirect(url_for("saas.company_detail", company_id=company.id))

    CompanySecurityService.set_pin(company, raw_pin)
    session[f"company_pin_reveal_{company.id}"] = raw_pin
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
    had_pin = bool(company.business_pin_hash)

    raw_pin = f"{secrets.randbelow(10000):04d}"
    CompanySecurityService.set_pin(company, raw_pin)
    session[f"company_pin_reveal_{company.id}"] = raw_pin

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
    payments = Payment.query.order_by(Payment.created_at.desc()).limit(40).all()
    history = PaymentHistory.query.order_by(PaymentHistory.created_at.desc()).limit(30).all()
    companies = Company.query.order_by(Company.created_at.desc()).all()
    subscriptions = Subscription.query.order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc()).limit(40).all()

    totals = {
        "total_invoiced": float(db.session.query(db.func.coalesce(db.func.sum(Invoice.amount), 0)).scalar() or 0),
        "total_paid": float(db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(Payment.status == "approved").scalar() or 0),
        "pending_payments": Payment.query.filter(Payment.status.in_(["pending", "authorized", "in_process"])).count(),
        "pending_invoices": Invoice.query.filter(Invoice.status.in_(["pending", "draft", "issued"])).count(),
        "rejected_payments": Payment.query.filter(Payment.status.in_(["rejected", "cancelled", "expired", "charged_back"])).count(),
        "trial_companies": Subscription.query.filter(Subscription.status == "trial").count(),
    }
    return render_template(
        "saas/billing.html",
        invoices=invoices,
        payments=payments,
        history=history,
        companies=companies,
        subscriptions=subscriptions,
        totals=totals,
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
            .filter(Payment.company_id.in_(company_ids), Payment.status.in_(confirmed_payment_statuses), Payment.paid_at.isnot(None))
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
        sub.id: _allowed_ui_actions_for_status(effective_status_by_id.get(sub.id, sub.status))
        for sub in subscriptions
    }
    return render_template(
        "saas/subscriptions.html",
        subscriptions=subscriptions,
        subscription_actions=subscription_actions,
        effective_status_by_id=effective_status_by_id,
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

    effective_status = SubscriptionService.get_effective_subscription_status(subscription, company=subscription.company)
    if not _action_allowed_for_status(effective_status, "modify"):
        flash("No se puede modificar esta suscripción en su estado actual.", "warning")
        return _redirect_back("saas.subscriptions_panel")

    start_date = _parse_dt(request.form.get("start_date"))
    next_billing_date = _parse_dt(request.form.get("next_billing_date"))
    last_payment_date = _parse_dt(request.form.get("last_payment_date"))
    renewal_enabled = (request.form.get("renewal_enabled") or "1") == "1"

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

        target_status = _normalized_subscription_status(request.form.get("status") or target_subscription.status)
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
    temp_password = session.pop("password_recovery_temp_password", None)
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

    session["password_recovery_temp_password"] = temp_password
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
    item = PasswordRecoveryRequest.query.filter_by(id=request_id).first_or_404()
    user = db.session.get(User, item.user_id)
    if user is None:
        flash("Usuario no encontrado.", "danger")
        return _redirect_back("saas.password_recovery_panel")

    temp_password = _temporary_password()
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

    # Mostrar una sola vez en la pantalla de recuperacion.
    session["password_recovery_temp_password"] = temp_password
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
    return render_template("saas/payments.html", payments=payments)


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
    from app import AuditLog

    _require_superadmin()
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(400).all()
    return render_template("saas/logs.html", logs=logs)


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
    return _redirect_back("saas.backups_panel")


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
        ("Pagos pendientes", Payment.query.filter(Payment.status.in_(["pending", "authorized", "in_process"])).count()),
        ("Pagos rechazados", Payment.query.filter(Payment.status.in_(["rejected", "cancelled", "expired", "charged_back"])).count()),
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

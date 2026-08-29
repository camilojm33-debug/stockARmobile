"""Centro de notificaciones operativo separado por rol."""

from datetime import datetime, time, timedelta
from hashlib import sha256
import json

from flask import url_for
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError, ProgrammingError
from services.sales_calculation_service import CONFIRMED_SALE_STATUSES


def build_notifications():
    if not getattr(current_user, "is_authenticated", False):
        return []
    try:
        if getattr(current_user, "role", None) == "superadmin":
            return _build_superadmin_notifications()
        if getattr(current_user, "role", None) == "seller":
            return _build_seller_notifications()
        return _build_user_notifications()
    except (OperationalError, ProgrammingError):
        return []


def get_notification_payload():
    """Return notifications and unseen badge count for current user."""
    items = build_notifications()
    if not getattr(current_user, "is_authenticated", False):
        return {"items": [], "count": 0, "signature": None}

    signature = _signature_for_items(items)
    if not items:
        return {"items": [], "count": 0, "signature": signature}

    try:
        from app import NotificationReadState

        state = NotificationReadState.query.filter_by(user_id=current_user.id).first()
        is_seen = bool(state and state.last_seen_signature == signature)
    except (OperationalError, ProgrammingError):
        is_seen = False
    return {
        "items": items,
        "count": 0 if is_seen else len(items),
        "signature": signature,
    }


def mark_notifications_seen():
    """Persist current notifications signature as seen for current user."""
    if not getattr(current_user, "is_authenticated", False):
        return {"ok": False, "count": 0}

    from app import NotificationReadState, db, utcnow

    items = build_notifications()
    signature = _signature_for_items(items)
    state = NotificationReadState.query.filter_by(user_id=current_user.id).first()
    now = utcnow()

    if state is None:
        state = NotificationReadState(
            user_id=current_user.id,
            last_seen_signature=signature,
            last_seen_at=now,
        )
        db.session.add(state)
    else:
        state.last_seen_signature = signature
        state.last_seen_at = now
        state.updated_at = now

    db.session.commit()
    return {"ok": True, "count": len(items), "signature": signature}


def _signature_for_items(items):
    normalized = []
    for item in items:
        normalized.append(
            {
                "type": item.get("type"),
                "title": item.get("title"),
                "body": item.get("body"),
                "href": item.get("href"),
            }
        )
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def _subscription_notification_target():
    """Build the canonical subscription portal URL instead of the legacy /admin path."""
    try:
        return url_for("company_billing.subscription_portal")
    except Exception:
        return "/admin/portal"


def _build_recent_quote_acceptance_notifications():
    """Notify company users about quotes recently accepted by a customer."""
    from app import Quote, scope_query_to_company, utcnow

    cutoff = utcnow() - timedelta(days=7)
    quotes = (
        scope_query_to_company(Quote.query, Quote)
        .filter(
            Quote.status == "APROBADO",
            Quote.converted_sale_id.is_(None),
            Quote.updated_at.isnot(None),
            Quote.updated_at >= cutoff,
        )
        .order_by(Quote.updated_at.desc(), Quote.id.desc())
        .limit(6)
        .all()
    )

    items = []
    for quote in quotes:
        customer_name = (
            getattr(getattr(quote, "client", None), "name", None)
            or getattr(quote, "consumer_name", None)
            or "Consumidor final"
        )
        number = getattr(quote, "number", None) or f"P-{quote.id:06d}"
        currency = getattr(quote, "currency", None) or "ARS"
        total = float(getattr(quote, "total_amount", 0) or 0)
        items.append(
            {
                "type": "success",
                "title": "Presupuesto aceptado",
                "body": f"{customer_name} aceptó el {number} · {currency} {total:.2f}. Listo para convertir en venta.",
                "href": url_for("quotes.view_quote", quote_id=quote.id),
            }
        )
    return items


def _build_superadmin_notifications():
    from app import BackupLog, Company, Payment, PaymentHistory, ReferralCommission, WebhookEvent, utcnow
    from app import SupportTicket

    now = utcnow()
    today_start = datetime.combine(now.date(), time.min)
    last_24h = now - timedelta(hours=24)

    new_companies = Company.query.filter(Company.created_at >= today_start).count()
    pending_payments = Payment.query.filter(Payment.status.in_(["pending", "in_process", "authorized"])).count()
    approved_payments_today = Payment.query.filter(Payment.status == "approved", Payment.created_at >= today_start).count()
    pending_referrals = ReferralCommission.query.filter(ReferralCommission.status == "pendiente").count()
    admin_alerts = PaymentHistory.query.filter(PaymentHistory.created_at >= last_24h, PaymentHistory.status == "rejected").count()
    backup_alerts = BackupLog.query.filter(BackupLog.status.in_(["error", "failed", "fallido"])).count()
    system_events = WebhookEvent.query.filter(WebhookEvent.created_at >= last_24h).count()

    items = []
    if new_companies:
        items.append({"type": "primary", "title": "Empresas nuevas", "body": f"{new_companies} alta(s) registradas hoy.", "href": "/superadmin"})
    if pending_payments or approved_payments_today:
        items.append(
            {
                "type": "success",
                "title": "Pagos",
                "body": f"{pending_payments} pendientes · {approved_payments_today} aprobados hoy.",
                "href": "/superadmin",
            }
        )
    if pending_referrals:
        items.append({"type": "info", "title": "Referidos", "body": f"{pending_referrals} comision(es) pendientes de gestion.", "href": "/superadmin"})
    if admin_alerts:
        items.append({"type": "danger", "title": "Alertas administrativas", "body": f"{admin_alerts} evento(s) de pago rechazado en las ultimas 24h.", "href": "/superadmin"})
    if backup_alerts:
        items.append({"type": "warning", "title": "Backups", "body": f"{backup_alerts} respaldo(s) con error requieren revision.", "href": "/superadmin"})
    if system_events:
        items.append({"type": "secondary", "title": "Eventos del sistema", "body": f"{system_events} webhook(s) procesados en las ultimas 24h.", "href": "/superadmin"})
    try:
        support_pending = SupportTicket.query.filter(SupportTicket.status == "pendiente").count()
    except Exception:
        support_pending = 0
    if support_pending:
        items.append(
            {
                "type": "info",
                "title": "Pedidos de ayuda",
                "body": f"{support_pending} pendiente(s)",
                "href": "/soporte/admin",
            }
        )
    return items


def _build_seller_notifications():
    from app import ReferralCommission, ReferralSeller

    profile = ReferralSeller.query.filter_by(user_id=current_user.id, active=True).first()
    if profile is None:
        return _build_recent_quote_acceptance_notifications() + [
            {
                "type": "warning",
                "title": "Referidos",
                "body": "Completa la activacion para acceder a tu portal de vendedor.",
                "href": "/referidos/activar",
            }
        ]

    commissions = (
        ReferralCommission.query.filter_by(seller_id=profile.id)
        .order_by(ReferralCommission.created_at.desc(), ReferralCommission.id.desc())
        .limit(20)
        .all()
    )

    pending_count = sum(1 for row in commissions if row.status in {"pendiente", "disponible"})
    available_total = sum(float(row.commission_amount or 0) for row in commissions if row.status == "disponible")
    paid_count = sum(1 for row in commissions if row.status == "pagada")

    items = _build_recent_quote_acceptance_notifications()
    if pending_count:
        items.append(
            {
                "type": "primary",
                "title": "Referidos",
                "body": f"{pending_count} comision(es) pendientes o disponibles.",
                "href": "/referidos/comisiones",
            }
        )
    if available_total:
        items.append(
            {
                "type": "success",
                "title": "Comisiones",
                "body": f"Tenes ARS {available_total:.2f} disponibles para cobrar.",
                "href": "/referidos/comisiones",
            }
        )
    if paid_count:
        items.append(
            {
                "type": "info",
                "title": "Pagos",
                "body": f"{paid_count} comision(es) ya figuran como pagadas.",
                "href": "/referidos/comisiones",
            }
        )

    if not items:
        items.append(
            {
                "type": "secondary",
                "title": "Referidos",
                "body": "Tu portal esta listo para compartir tu enlace y seguir tus comisiones.",
                "href": "/referidos",
            }
        )
    return items[:6]


def _build_user_notifications():
    from app import (
        BackupLog,
        CashSession,
        Client,
        Product,
        PurchaseOrder,
        Sale,
        Subscription,
        db,
        get_company_access_state,
        get_current_company_id,
        scope_query_to_company,
        utcnow,
    )

    now = utcnow()
    today_start = datetime.combine(now.date(), time.min)
    confirmed_sale_filter = or_(Sale.status.is_(None), db.func.lower(Sale.status).in_(list(CONFIRMED_SALE_STATUSES)))

    sales_today = scope_query_to_company(Sale.query.filter(Sale.date >= today_start, confirmed_sale_filter), Sale).count()
    sales_amount = (
        scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).filter(Sale.date >= today_start, confirmed_sale_filter), Sale).scalar()
        or 0
    )
    low_stock = scope_query_to_company(
        Product.query.filter(Product.active.is_(True), Product.stock <= Product.min_stock, Product.stock > 0),
        Product,
    ).count()
    out_stock = scope_query_to_company(Product.query.filter(Product.active.is_(True), Product.stock <= 0), Product).count()
    new_clients = scope_query_to_company(Client.query.filter(Client.created_at >= today_start), Client).count()
    open_cash = scope_query_to_company(
        CashSession.query.filter_by(status="abierta", user_id=current_user.id),
        CashSession,
    ).count()
    pending_purchases = scope_query_to_company(
        PurchaseOrder.query.filter(PurchaseOrder.status.in_(["pendiente", "ordenada"])),
        PurchaseOrder,
    ).count()
    latest_subscription = scope_query_to_company(
        Subscription.query.order_by(Subscription.starts_at.desc()),
        Subscription,
    ).first()
    latest_backup = scope_query_to_company(
        BackupLog.query.order_by(BackupLog.created_at.desc()),
        BackupLog,
    ).first()

    company_id = get_current_company_id()
    company_state = get_company_access_state(company_id) if company_id else {"status": "missing", "can_access": False}

    items = _build_recent_quote_acceptance_notifications()
    items.append({"type": "success", "title": "Ventas", "body": f"{sales_today} venta(s) hoy · ${float(sales_amount):.2f}", "href": "/ventas/"})

    if low_stock or out_stock:
        items.append(
            {
                "type": "warning" if low_stock else "danger",
                "title": "Stock",
                "body": f"{low_stock} producto(s) en minimo · {out_stock} agotado(s).",
                "href": "/productos/",
            }
        )

    if new_clients:
        items.append({"type": "primary", "title": "Clientes", "body": f"{new_clients} cliente(s) nuevo(s) hoy.", "href": "/clientes/"})

    if latest_subscription is not None:
        sub_status = (latest_subscription.status or "sin estado").replace("_", " ")
        items.append({"type": "info", "title": "Suscripcion", "body": f"Estado actual: {sub_status}.", "href": _subscription_notification_target()})

    if latest_backup is not None:
        backup_status = (latest_backup.status or "pendiente").lower()
        badge = "danger" if backup_status in {"error", "failed", "fallido"} else "secondary"
        items.append(
            {
                "type": badge,
                "title": "Backups",
                "body": f"Ultimo respaldo: {backup_status}.",
                "href": "/admin?panel=backups",
            }
        )

    if open_cash or pending_purchases:
        items.append(
            {
                "type": "secondary",
                "title": "Recordatorios",
                "body": f"Tu usuario tiene {open_cash} caja(s) abierta(s) · {pending_purchases} compra(s) pendiente(s).",
                "href": "/caja/",
            }
        )

    if not company_state.get("can_access", True):
        items.append(
            {
                "type": "danger",
                "title": "Empresa",
                "body": company_state.get("reason", "Revisa el estado de tu empresa."),
                "href": _subscription_notification_target(),
            }
        )

    return items

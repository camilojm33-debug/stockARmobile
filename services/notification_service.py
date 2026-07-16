"""Centro de notificaciones operativo separado por rol."""

from datetime import datetime, time, timedelta

from flask_login import current_user


def build_notifications():
    if not getattr(current_user, "is_authenticated", False):
        return []
    if getattr(current_user, "role", None) == "superadmin":
        return _build_superadmin_notifications()
    return _build_user_notifications()


def _build_superadmin_notifications():
    from app import BackupLog, Company, Payment, PaymentHistory, ReferralCommission, WebhookEvent, utcnow

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
    return items


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

    sales_today = scope_query_to_company(Sale.query.filter(Sale.date >= today_start), Sale).count()
    sales_amount = (
        scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).filter(Sale.date >= today_start), Sale).scalar()
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

    items = []
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
        items.append({"type": "info", "title": "Suscripcion", "body": f"Estado actual: {sub_status}.", "href": "/admin"})

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
                "href": "/admin",
            }
        )

    return items

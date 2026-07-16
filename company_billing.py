"""Portal de suscripcion para empresas (tenant) y webhook publico de Mercado Pago."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
import json
import secrets
import string

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import case, func
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app import company_admin_required, csrf, tenant_required
from config.billing_config import load_billing_config
from services.billing_service import BillingService
from services.backup_service import BackupService
from services.company_security_service import CompanySecurityService
from services.plan_service import PlanService
from services.plan_usage_service import PlanUsageService
from services.referral_service import ReferralService
from services.subscription_service import SubscriptionService
from services.webhook_service import WebhookService

bp = Blueprint("company_billing", __name__)

EMPLOYEE_PERMISSIONS = [
    ("inventory", "Inventario"),
    ("sales", "Ventas"),
    ("clients", "Clientes"),
    ("reports", "Reportes"),
    ("cash", "Caja"),
    ("economic_stats", "Puede visualizar estadísticas económicas"),
]


def company_member_required(func):
    @wraps(func)
    @login_required
    def decorated(*args, **kwargs):
        if getattr(current_user, "role", None) not in {"admin", "user"}:
            abort(403)
        if getattr(current_user, "company_id", None) is None:
            abort(403)
        return func(*args, **kwargs)

    return decorated


def _parse_date(value):
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def _to_float(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _pin_session_key(company_id):
    return f"company_pin_verified_{company_id}"


def _pin_reveal_session_key(company_id):
    return f"company_pin_reveal_{company_id}"


def _is_pin_verified(company_id):
    from flask import session

    value = session.get(_pin_session_key(company_id))
    if not value:
        return False

    # Legacy sessions stored a boolean. Force re-validation for safer behavior.
    if isinstance(value, bool):
        session.pop(_pin_session_key(company_id), None)
        return False

    ttl_minutes = int(current_app.config.get("COMPANY_PIN_SESSION_TTL_MINUTES", 30) or 30)
    expires_at = float(value) + (ttl_minutes * 60)
    if datetime.now(timezone.utc).timestamp() > expires_at:
        session.pop(_pin_session_key(company_id), None)
        return False
    return True


def _mark_pin_verified(company_id, verified=True):
    from flask import session

    key = _pin_session_key(company_id)
    if verified:
        session[key] = datetime.now(timezone.utc).timestamp()
    else:
        session.pop(key, None)


def _load_company(company_id):
    from app import Company

    return Company.query.filter_by(id=company_id).first_or_404()


def _normalize_company_role(raw_role):
    role = (raw_role or "user").strip().lower()
    return role if role in {"admin", "user"} else "user"


def _temporary_password(length=10):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _user_permissions(user):
    raw = (getattr(user, "permissions_json", None) or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        data = []
    permissions = [str(item).strip() for item in data if str(item).strip()]
    if getattr(user, "role", None) in {"admin", "superadmin"} and "economic_stats" not in permissions:
        permissions.append("economic_stats")
    return sorted(set(permissions))


def _set_user_permissions(user, permission_keys):
    valid_keys = {key for key, _label in EMPLOYEE_PERMISSIONS}
    cleaned = sorted({key for key in permission_keys if key in valid_keys})
    if getattr(user, "role", None) in {"admin", "superadmin"} and "economic_stats" not in cleaned:
        cleaned.append("economic_stats")
        cleaned.sort()
    user.permissions_json = json.dumps(cleaned)


def _json_company_dict(value):
    raw = (value or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _company_schedules_payload(company):
    payload = _json_company_dict(company.schedules_json)
    if not isinstance(payload.get("weekly"), dict):
        payload["weekly"] = {}
    assignments = payload.get("employee_assignments")
    if not isinstance(assignments, list):
        assignments = []
    cleaned = []
    for row in assignments:
        if not isinstance(row, dict):
            continue
        assignment_id = str(row.get("id") or "").strip()
        user_id = int(row.get("user_id") or 0)
        day = str(row.get("day") or "").strip().lower()
        start = str(row.get("start") or "").strip()[:5]
        end = str(row.get("end") or "").strip()[:5]
        if not assignment_id or user_id <= 0 or not day or not start or not end:
            continue
        cleaned.append({
            "id": assignment_id,
            "user_id": user_id,
            "day": day,
            "start": start,
            "end": end,
        })
    payload["employee_assignments"] = cleaned
    return payload


def _mercadopago_connection_summary(company):
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    return MercadoPagoOAuthService().summarize_connection(getattr(company, "mercadopago_connection", None))


def _pdf_from_lines(title, lines, filename):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 52
    pdf.setTitle(title)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(42, y, title)
    y -= 26
    pdf.setFont("Helvetica", 10)
    for line in lines:
        if y < 52:
            pdf.showPage()
            y = height - 52
            pdf.setFont("Helvetica", 10)
        pdf.drawString(42, y, str(line)[:180])
        y -= 14
    pdf.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)


def _plan_limit_context(company_id):
    usage_snapshot = PlanUsageService.usage_snapshot(company_id)
    users_metric = next(
        (item for item in usage_snapshot["metrics"] if item.key == PlanUsageService.RESOURCE_USERS),
        None,
    )
    return usage_snapshot, users_metric


def _format_size(size_bytes):
    value = float(size_bytes or 0)
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


def _subscription_expiration(subscription, company):
    if subscription is None:
        return getattr(company, "trial_ends_at", None)
    return (
        subscription.next_billing_date
        or subscription.ends_at
        or subscription.trial_end
        or getattr(company, "trial_ends_at", None)
    )


def _pin_metadata(company):
    from app import AuditLog

    creation_log = (
        AuditLog.query.filter(
            AuditLog.company_id == company.id,
            AuditLog.action.in_(["company_pin_assigned", "company_pin_regenerated"]),
        )
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .first()
    )
    last_use_log = (
        AuditLog.query.filter_by(company_id=company.id, action="company_settings_pin_ok")
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .first()
    )
    created_at = getattr(creation_log, "created_at", None) or getattr(company, "business_pin_updated_at", None)
    last_used_at = getattr(last_use_log, "created_at", None)
    return created_at, last_used_at


def _user_access_map(company_id, user_ids):
    from app import AuditLog

    if not user_ids:
        return {}

    access_logs = (
        AuditLog.query.filter(
            AuditLog.company_id == company_id,
            AuditLog.action == "login_success",
            AuditLog.user_id.in_(user_ids),
        )
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .all()
    )
    result = {}
    for row in access_logs:
        if row.user_id not in result:
            result[row.user_id] = row.created_at
    return result


def _cash_summary(cash_rows):
    total_sold = sum(row["total_sold"] for row in cash_rows)
    total_sales = sum(row["sales_count"] for row in cash_rows)
    average_ticket = (total_sold / total_sales) if total_sales else 0.0
    return {
        "total_sold": total_sold,
        "total_sales": total_sales,
        "average_ticket": average_ticket,
    }


def _days_remaining(target_date):
    if target_date is None:
        return None
    delta = target_date - datetime.now(timezone.utc).replace(tzinfo=None)
    if delta.total_seconds() <= 0:
        return int(delta.days)
    return int((delta.total_seconds() + 86399) // 86400)


def _plan_features_label(plan):
    if plan is None:
        return []
    raw = (getattr(plan, "features_json", None) or "").strip().lower()
    if not raw:
        return []
    if raw == "all":
        return ["Inventario", "Ventas", "Clientes", "Compras", "Caja", "Reportes", "Excel", "Kardex", "QR", "Etiquetas"]
    mapping = {
        "inventario": "Inventario",
        "ventas": "Ventas",
        "clientes": "Clientes",
        "compras": "Compras",
        "caja": "Caja",
        "reportes": "Reportes",
        "reportes_basicos": "Reportes básicos",
        "excel": "Excel",
        "kardex": "Kardex",
        "qr": "QR",
        "etiquetas": "Etiquetas",
    }
    return [mapping.get(item.strip(), item.strip().title()) for item in raw.split(",") if item.strip()]


def _subscription_state_badge(subscription, days_remaining):
    status = (getattr(subscription, "status", None) or "trial").lower()
    label_map = {
        "trial": "Trial",
        "pending": "Pendiente",
        "authorized": "Pendiente",
        "in_process": "Pendiente",
        "active": "Activa",
        "approved": "Activa",
        "cancelled": "Cancelada",
        "expired": "Vencida",
        "suspended": "Suspendida",
        "rejected": "Rechazada",
    }
    if status in {"cancelled", "expired", "suspended", "rejected"}:
        return {"label": label_map.get(status, status.title()), "class": "text-bg-danger", "indicator": "danger", "text": "Vencida"}
    if status == "trial":
        if days_remaining is not None and days_remaining <= 1:
            return {"label": "Trial", "class": "text-bg-danger", "indicator": "danger", "text": "Vencida"}
        if days_remaining is not None and days_remaining <= 3:
            return {"label": "Trial", "class": "text-bg-warning", "indicator": "warning", "text": "Quedan pocos días"}
        if days_remaining is not None and days_remaining <= 7:
            return {"label": "Trial", "class": "text-bg-warning", "indicator": "warning", "text": "Próxima a vencer"}
        return {"label": "Trial", "class": "text-bg-success", "indicator": "success", "text": "Activa"}
    if days_remaining is None:
        return {"label": label_map.get(status, status.title()), "class": "text-bg-info", "indicator": "success", "text": "Activa"}
    if days_remaining <= 0:
        return {"label": label_map.get(status, status.title()), "class": "text-bg-danger", "indicator": "danger", "text": "Vencida"}
    if days_remaining <= 3:
        return {"label": label_map.get(status, status.title()), "class": "text-bg-danger", "indicator": "warning", "text": "Quedan pocos días"}
    if days_remaining <= 15:
        return {"label": label_map.get(status, status.title()), "class": "text-bg-warning", "indicator": "warning", "text": "Próxima a vencer"}
    return {"label": label_map.get(status, status.title()), "class": "text-bg-success", "indicator": "success", "text": "Activa"}


def _pin_guard(company):
    if _is_pin_verified(company.id):
        return None
    flash("Debes validar PIN para gestionar Mi Empresa.", "warning")
    return redirect(url_for("company_billing.company_settings"))


def _build_user_and_cash_rows(company_id, date_from=None, date_to=None, search_text="", role_filter="", status_filter=""):
    from app import CashMovement, Sale, User, db

    users_query = User.query.filter_by(company_id=company_id)
    normalized_search = (search_text or "").strip().lower()
    normalized_role = (role_filter or "").strip().lower()
    normalized_status = (status_filter or "").strip().lower()

    if normalized_search:
        search_like = f"%{normalized_search}%"
        users_query = users_query.filter(
            func.lower(func.coalesce(User.username, "")).like(search_like)
            | func.lower(func.coalesce(User.email, "")).like(search_like)
            | func.lower(func.coalesce(User.first_name, "")).like(search_like)
            | func.lower(func.coalesce(User.last_name, "")).like(search_like)
        )
    if normalized_role in {"admin", "user"}:
        users_query = users_query.filter(User.role == normalized_role)
    if normalized_status == "active":
        users_query = users_query.filter(User.active.is_(True))
    elif normalized_status == "inactive":
        users_query = users_query.filter(User.active.is_(False))

    users = users_query.order_by(User.created_at.asc(), User.id.asc()).all()

    sales_query = db.session.query(
        Sale.seller_id.label("user_id"),
        func.count(Sale.id).label("sales_count"),
        func.coalesce(func.sum(Sale.total_amount), 0).label("total_sold"),
    ).filter(Sale.company_id == company_id)
    movement_query = db.session.query(
        CashMovement.user_id.label("user_id"),
        func.coalesce(func.sum(case((CashMovement.movement_type == "ingreso", CashMovement.amount), else_=0)), 0).label("ingresos"),
        func.coalesce(func.sum(case((CashMovement.movement_type == "egreso", CashMovement.amount), else_=0)), 0).label("egresos"),
    ).filter(CashMovement.company_id == company_id)

    if date_from:
        sales_query = sales_query.filter(Sale.date >= date_from)
        movement_query = movement_query.filter(CashMovement.created_at >= date_from)
    if date_to:
        until = date_to + timedelta(days=1)
        sales_query = sales_query.filter(Sale.date < until)
        movement_query = movement_query.filter(CashMovement.created_at < until)

    sales_rows = {
        int(row.user_id): row
        for row in sales_query.group_by(Sale.seller_id).all()
        if row.user_id is not None
    }
    movement_rows = {
        int(row.user_id): row
        for row in movement_query.group_by(CashMovement.user_id).all()
        if row.user_id is not None
    }
    access_rows = _user_access_map(company_id, [user.id for user in users])

    result_rows = []
    for user in users:
        sales_data = sales_rows.get(user.id)
        movement_data = movement_rows.get(user.id)
        total_sold = float(getattr(sales_data, "total_sold", 0) or 0)
        sales_count = int(getattr(sales_data, "sales_count", 0) or 0)
        ingresos = float(getattr(movement_data, "ingresos", 0) or 0)
        egresos = float(getattr(movement_data, "egresos", 0) or 0)
        saldo = ingresos - egresos
        average_ticket = (total_sold / sales_count) if sales_count else 0.0
        result_rows.append(
            {
                "user": user,
                "total_sold": total_sold,
                "sales_count": sales_count,
                "average_ticket": average_ticket,
                "ingresos": ingresos,
                "egresos": egresos,
                "saldo": saldo,
                "last_access": access_rows.get(user.id),
                "permissions": _user_permissions(user),
            }
        )
    result_rows.sort(key=lambda item: (-item["total_sold"], item["user"].created_at or datetime.min, item["user"].id))
    for index, row in enumerate(result_rows, start=1):
        row["rank"] = index
    return users, result_rows


@bp.route("/portal")
@company_member_required
def subscription_portal():
    from flask import session

    from app import Company, Invoice, Payment, ReferralAttribution, db

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()

    PlanService.ensure_defaults(db.session)
    plans = PlanService.all_commercial_plans()
    subscription = SubscriptionService.active_subscription_for_company(company.id)

    if subscription is None:
        trial_plan = PlanService.get_plan(code="trial")
        subscription = SubscriptionService.ensure_company_trial(db.session, company=company, trial_plan=trial_plan)
        db.session.commit()

    usage_snapshot = PlanUsageService.usage_snapshot(company.id)
    recent_payments = (
        Payment.query.filter_by(company_id=company.id)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .limit(20)
        .all()
    )
    recent_invoices = (
        Invoice.query.filter_by(company_id=company.id)
        .order_by(Invoice.issued_at.desc(), Invoice.id.desc())
        .limit(20)
        .all()
    )
    referral_attribution = ReferralAttribution.query.filter_by(company_id=company.id).first()
    managed_by_seller = referral_attribution.seller.user.username if referral_attribution and referral_attribution.seller and referral_attribution.seller.user else None
    reference_date = _subscription_expiration(subscription, company)
    days_remaining = _days_remaining(reference_date)
    status_badge = _subscription_state_badge(subscription, days_remaining)
    plan_features = _plan_features_label(subscription.plan if subscription else None)
    checkout_preview = session.pop("mp_checkout_preview", None)
    checkout_status = (request.args.get("checkout") or "").strip().lower()
    return render_template(
        "company_billing/portal.html",
        company=company,
        plans=plans,
        subscription=subscription,
        usage_snapshot=usage_snapshot,
        recent_payments=recent_payments,
        recent_invoices=recent_invoices,
        checkout_preview=checkout_preview,
        checkout_status=checkout_status,
        days_remaining=days_remaining,
        reference_date=reference_date,
        status_badge=status_badge,
        plan_features=plan_features,
        managed_by_seller=managed_by_seller,
        mp_config=load_billing_config(),
    )


@bp.route("/subscription/invoices/<int:invoice_id>/pdf")
@company_member_required
def subscription_invoice_pdf(invoice_id):
    from app import Company, Invoice

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    invoice = Invoice.query.filter_by(id=invoice_id, company_id=company.id).first_or_404()
    lines = [
        f"Empresa: {company.name}",
        f"Factura: {invoice.invoice_number or ('#' + str(invoice.id))}",
        f"Estado: {invoice.status or '-'}",
        f"Importe: {float(invoice.amount or 0):.2f} {invoice.currency or ''}",
        f"Vencimiento: {invoice.due_at.strftime('%Y-%m-%d') if invoice.due_at else '-'}",
        f"Emision: {invoice.issued_at.strftime('%Y-%m-%d %H:%M') if invoice.issued_at else '-'}",
        f"Detalle: {(invoice.detail or '-').strip()[:500]}",
    ]
    return _pdf_from_lines("Factura SaaS - StockArmobile", lines, f"factura_{invoice.id}.pdf")


@bp.route("/subscription/payments/<int:payment_id>/pdf")
@company_member_required
def subscription_payment_pdf(payment_id):
    from app import Company, Payment

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    payment = Payment.query.filter_by(id=payment_id, company_id=company.id).first_or_404()
    lines = [
        f"Empresa: {company.name}",
        f"Pago: {payment.payment_id or ('#' + str(payment.id))}",
        f"Estado: {payment.status or '-'}",
        f"Importe: {float(payment.amount or 0):.2f} {payment.currency or ''}",
        f"Metodo: {payment.payment_method or '-'}",
        f"Referencia: {payment.reference or '-'}",
        f"Fecha de registro: {payment.created_at.strftime('%Y-%m-%d %H:%M') if payment.created_at else '-'}",
    ]
    return _pdf_from_lines("Comprobante de Pago - StockArmobile", lines, f"pago_{payment.id}.pdf")


@bp.route("/checkout", methods=["POST"])
@company_member_required
def create_checkout():
    from flask import session

    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()

    plan_id = request.form.get("plan_id", type=int)
    plan = PlanService.get_plan(plan_id=plan_id)
    if plan is None:
        flash("Plan no encontrado.", "danger")
        return redirect(url_for("company_billing.subscription_portal"))

    if float(plan.price or 0) <= 0:
        subscription = SubscriptionService.start_or_change_plan(db.session, company=company, plan=plan, user_id=current_user.id)
        ReferralService.create_commission_for_sale(
            db.session,
            company_id=company.id,
            subscription=subscription,
            payment=None,
            plan=plan,
        )
        record_audit(action="subscription_change", entity="subscription", detail=f"Plan actualizado a {plan.code or plan.name}")
        db.session.commit()
        flash("Plan actualizado correctamente.", "success")
        return redirect(url_for("company_billing.subscription_portal"))

    try:
        payload = BillingService().create_checkout_for_plan(db_session=db.session, company=company, plan=plan, user=current_user)
        preference = payload["preference"]
    except Exception as exc:
        current_app.logger.exception("Error creando checkout Mercado Pago: %s", exc)
        flash("No se pudo iniciar el checkout. Revisá la configuración de Mercado Pago.", "danger")
        return redirect(url_for("company_billing.subscription_portal"))

    checkout_url = preference.get("init_point") or preference.get("sandbox_init_point")
    if not checkout_url:
        flash("Mercado Pago no devolvió URL de checkout.", "danger")
        return redirect(url_for("company_billing.subscription_portal"))

    session["mp_checkout_preview"] = BillingService.checkout_preview_payload(
        preference=preference,
        plan=plan,
        company=company,
    )
    flash("Checkout generado correctamente. Escaneá el QR o continuá con el botón de pago.", "info")
    return redirect(url_for("company_billing.subscription_portal", checkout="created"))


@bp.route("/subscription/cancel", methods=["POST"])
@company_member_required
def cancel_subscription():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    subscription = SubscriptionService.active_subscription_for_company(company_id)
    if subscription is None:
        flash("No hay suscripción activa.", "warning")
        return redirect(url_for("company_billing.subscription_portal"))
    BillingService.cancel_subscription(db.session, subscription=subscription, user_id=current_user.id)
    record_audit(action="subscription_cancel", entity="subscription", entity_id=subscription.id, detail="Cancelacion de suscripcion solicitada")
    db.session.commit()
    flash("La suscripción se cancelará al finalizar el período actual.", "success")
    return redirect(url_for("company_billing.subscription_portal"))


@bp.route("/subscription/reactivate", methods=["POST"])
@company_member_required
def reactivate_subscription():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    subscription = SubscriptionService.active_subscription_for_company(company_id)
    if subscription is None:
        flash("No hay suscripción para reactivar.", "warning")
        return redirect(url_for("company_billing.subscription_portal"))
    BillingService.reactivate_subscription(db.session, subscription=subscription, user_id=current_user.id)
    record_audit(action="subscription_reactivate", entity="subscription", entity_id=subscription.id, detail="Renovacion automatica reactivada")
    db.session.commit()
    flash("Renovación automática reactivada.", "success")
    return redirect(url_for("company_billing.subscription_portal"))


@bp.route("/payment-qr-settings", methods=["POST"])
@company_member_required
def payment_qr_settings():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)

    if getattr(current_user, "role", None) != "admin":
        flash("Solo el administrador puede modificar datos de la empresa.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="company"))

    company.name = (request.form.get("name") or company.name or "").strip()[:160] or company.name
    company.legal_name = (request.form.get("legal_name") or "").strip()[:160] or None
    company.address = (request.form.get("address") or "").strip()[:255] or None
    company.province = (request.form.get("province") or "").strip()[:120] or None
    company.city = (request.form.get("city") or "").strip()[:120] or None
    company.postal_code = (request.form.get("postal_code") or "").strip()[:20] or None
    company.phone = (request.form.get("phone") or "").strip()[:40] or None
    company.whatsapp = (request.form.get("whatsapp") or "").strip()[:40] or None
    company.contact_email = (request.form.get("contact_email") or "").strip()[:160] or None
    company.website = (request.form.get("website") or "").strip()[:255] or None
    company.social_facebook = (request.form.get("social_facebook") or "").strip()[:255] or None
    company.social_instagram = (request.form.get("social_instagram") or "").strip()[:255] or None
    company.social_tiktok = (request.form.get("social_tiktok") or "").strip()[:255] or None
    company.social_youtube = (request.form.get("social_youtube") or "").strip()[:255] or None
    company.social_linkedin = (request.form.get("social_linkedin") or "").strip()[:255] or None
    company.logo = (request.form.get("logo") or "").strip()[:255] or None
    company.tax_id = (request.form.get("tax_id") or "").strip()[:50] or None
    company.payment_alias = (request.form.get("payment_alias") or "").strip() or None
    company.payment_cbu = (request.form.get("payment_cbu") or "").strip() or None
    company.payment_cvu = (request.form.get("payment_cvu") or "").strip() or None
    company.payment_qr_text = (request.form.get("payment_qr_text") or "").strip() or None
    company.payment_qr_url = (request.form.get("payment_qr_url") or "").strip() or None

    record_audit(action="company_settings_update", entity="company", entity_id=company.id, detail="Datos de Mi Empresa actualizados")

    if not any([company.payment_alias, company.payment_cbu, company.payment_cvu, company.payment_qr_text, company.payment_qr_url]):
        flash("Guardado. Agrega al menos un dato para generar el QR de cobro.", "warning")
    else:
        flash("Datos de cobro QR guardados correctamente.", "success")
    db.session.commit()
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/mercado-pago", methods=["POST"])
@company_admin_required
def mercado_pago_connect():
    from app import db, record_audit
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    service = MercadoPagoOAuthService()
    oauth_ready, missing_vars = service.oauth_config_status()
    if not oauth_ready:
        detail = ", ".join(missing_vars) if missing_vars else "credenciales OAuth"
        flash(f"Faltan credenciales OAuth de Mercado Pago en el servidor: {detail}.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="mercado-pago"))

    state = service.oauth_state()
    session_key = f"mp_oauth_state_{company.id}"
    session[session_key] = state
    session.modified = True
    redirect_uri = service.oauth_redirect_uri(url_for("company_billing.mercado_pago_callback", _external=True))
    auth_url = service.build_authorization_url(state=state, redirect_uri=redirect_uri)
    record_audit(action="mercadopago_oauth_start", entity="company", entity_id=company.id, detail="Inicio de conexión OAuth con Mercado Pago")
    db.session.commit()
    return redirect(auth_url)


@bp.route("/mercado-pago/callback")
@company_admin_required
def mercado_pago_callback():
    from app import db, record_audit
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    service = MercadoPagoOAuthService()

    error = request.args.get("error")
    if error:
        flash(f"Mercado Pago rechazó la conexión: {error}", "danger")
        return redirect(url_for("company_billing.company_settings", panel="mercado-pago"))

    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()
    session_key = f"mp_oauth_state_{company.id}"
    expected_state = session.get(session_key)
    if not code or not state or not expected_state or state != expected_state:
        flash("La devolución OAuth no pudo validarse.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="mercado-pago"))

    session.pop(session_key, None)
    redirect_uri = service.oauth_redirect_uri(url_for("company_billing.mercado_pago_callback", _external=True))
    try:
        token_payload = service.exchange_code(code=code, redirect_uri=redirect_uri)
        access_token = token_payload.get("access_token") or ""
        if not access_token:
            raise RuntimeError("Mercado Pago no devolvió access_token")
        profile = service.fetch_user_profile(access_token=access_token)
        connection = service.save_connection(company_id=company.id, token_payload=token_payload, profile=profile)
        record_audit(action="mercadopago_oauth_connected", entity="company", entity_id=company.id, detail=f"Cuenta Mercado Pago conectada: {connection.account_email or connection.mp_user_id or 'sin email'}")
        db.session.commit()
        flash("Mercado Pago conectado correctamente.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Error completando OAuth de Mercado Pago: %s", exc)
        flash("No se pudo completar la conexión con Mercado Pago.", "danger")
    return redirect(url_for("company_billing.company_settings", panel="mercado-pago"))


@bp.route("/mercado-pago/refresh", methods=["POST"])
@company_admin_required
def mercado_pago_refresh():
    from app import db, record_audit
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    service = MercadoPagoOAuthService()
    try:
        connection = service.refresh_connection(company_id=company.id)
        record_audit(action="mercadopago_oauth_refresh", entity="company", entity_id=company.id, detail="Conexión Mercado Pago actualizada")
        db.session.commit()
        flash("Conexión de Mercado Pago actualizada correctamente.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("No se pudo actualizar Mercado Pago: %s", exc)
        flash("No se pudo actualizar la conexión de Mercado Pago.", "danger")
    return redirect(url_for("company_billing.company_settings", panel="mercado-pago"))


@bp.route("/mercado-pago/test", methods=["POST"])
@company_admin_required
def mercado_pago_test():
    from app import db, record_audit
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    service = MercadoPagoOAuthService()
    try:
        profile = service.test_connection(company_id=company.id)
        record_audit(action="mercadopago_oauth_test", entity="company", entity_id=company.id, detail=f"Prueba de conexión Mercado Pago OK: {profile.get('id')}")
        db.session.commit()
        flash("La conexión con Mercado Pago funciona correctamente.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Error probando Mercado Pago: %s", exc)
        flash("La conexión con Mercado Pago falló.", "danger")
    return redirect(url_for("company_billing.company_settings", panel="mercado-pago"))


@bp.route("/mercado-pago/disconnect", methods=["POST"])
@company_admin_required
def mercado_pago_disconnect():
    from app import db, record_audit
    from services.mercadopago_oauth_service import MercadoPagoOAuthService

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    service = MercadoPagoOAuthService()
    try:
        service.disconnect(company_id=company.id)
        record_audit(action="mercadopago_oauth_disconnect", entity="company", entity_id=company.id, detail="Cuenta Mercado Pago desconectada")
        db.session.commit()
        flash("Cuenta de Mercado Pago desconectada.", "info")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("No se pudo desconectar Mercado Pago: %s", exc)
        flash("No se pudo desconectar la cuenta de Mercado Pago.", "danger")
    return redirect(url_for("company_billing.company_settings", panel="mercado-pago"))


@bp.route("/mercado-pago/status")
@company_admin_required
def mercado_pago_status():
    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    summary = _mercadopago_connection_summary(company)
    for key in ["connected_at", "last_synced_at", "token_expires_at"]:
        value = summary.get(key)
        summary[key] = value.isoformat() if value else None
    return jsonify(summary)


@bp.route("/company-settings/cash/open", methods=["POST"])
@company_admin_required
def company_settings_cash_open():
    from app import CashSession, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    open_session = (
        CashSession.query.filter_by(company_id=company.id, status="abierta")
        .order_by(CashSession.opened_at.desc(), CashSession.id.desc())
        .first()
    )
    if open_session is not None:
        flash("Ya existe una caja abierta para la empresa.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="stats"))

    opening_amount = _to_float(request.form.get("opening_amount"), default=0.0)
    note = (request.form.get("note") or "").strip() or None
    session_row = CashSession(
        user_id=current_user.id,
        company_id=company.id,
        opening_amount=opening_amount,
        note=note,
    )
    db.session.add(session_row)
    db.session.flush()
    record_audit(
        action="company_cash_open",
        entity="cash_session",
        entity_id=session_row.id,
        company_id=company.id,
        detail=f"Apertura de caja desde Mi Empresa por admin {current_user.id}",
    )
    db.session.commit()
    flash("Caja abierta correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="stats"))


@bp.route("/company-settings/cash/close/<int:session_id>", methods=["POST"])
@company_admin_required
def company_settings_cash_close(session_id):
    from app import CashSession, db, record_audit, utcnow

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    session_row = CashSession.query.filter_by(id=session_id, company_id=company.id).first_or_404()
    if session_row.status != "abierta":
        flash("La caja seleccionada ya está cerrada.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="stats"))

    session_row.closing_amount = _to_float(request.form.get("closing_amount"), default=float(session_row.closing_amount or 0))
    session_row.closed_at = utcnow()
    session_row.status = "cerrada"
    note = (request.form.get("note") or "").strip()
    if note:
        session_row.note = note

    record_audit(
        action="company_cash_close",
        entity="cash_session",
        entity_id=session_row.id,
        company_id=company.id,
        detail=f"Cierre de caja desde Mi Empresa por admin {current_user.id}",
    )
    db.session.commit()
    flash("Caja cerrada correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="stats"))


@bp.route("/company-settings/cash/update/<int:session_id>", methods=["POST"])
@company_admin_required
def company_settings_cash_update(session_id):
    from app import CashSession, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    session_row = CashSession.query.filter_by(id=session_id, company_id=company.id).first_or_404()
    if request.form.get("opening_amount") not in (None, ""):
        session_row.opening_amount = _to_float(request.form.get("opening_amount"), default=float(session_row.opening_amount or 0))
    if request.form.get("closing_amount") not in (None, ""):
        session_row.closing_amount = _to_float(request.form.get("closing_amount"), default=float(session_row.closing_amount or 0))
    note = request.form.get("note")
    if note is not None:
        session_row.note = (note or "").strip() or None

    record_audit(
        action="company_cash_update",
        entity="cash_session",
        entity_id=session_row.id,
        company_id=company.id,
        detail=f"Edición de caja desde Mi Empresa por admin {current_user.id}",
    )
    db.session.commit()
    flash("Caja editada correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="stats"))


@bp.route("/company-settings/pin/verify", methods=["POST"])
@company_member_required
def company_settings_pin_verify():
    from app import db, record_audit, utcnow

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)

    if not company.business_pin_hash:
        flash("El PIN no esta configurado. Solicita al Super Administrador que lo asigne.", "warning")
        return redirect(url_for("company_billing.company_settings"))

    remaining = CompanySecurityService.remaining_block_seconds(company, now=utcnow())
    if remaining > 0:
        flash(f"Acceso bloqueado temporalmente. Intenta en {remaining} segundos.", "danger")
        return redirect(url_for("company_billing.company_settings"))

    pin = request.form.get("access_pin")
    if CompanySecurityService.verify_pin(company, pin):
        CompanySecurityService.reset_attempts(company)
        _mark_pin_verified(company.id, True)
        record_audit(action="company_settings_pin_ok", entity="company", entity_id=company.id, detail="PIN Mi Empresa validado")
        db.session.commit()
        flash("PIN correcto. Acceso concedido a Mi Empresa.", "success")
        return redirect(url_for("company_billing.company_settings"))

    attempts, blocked = CompanySecurityService.register_failed_attempt(company)
    record_audit(action="company_settings_pin_failed", entity="company", entity_id=company.id, detail=f"Intento PIN fallido #{attempts}")
    db.session.commit()
    if blocked:
        flash("Demasiados intentos fallidos. Acceso bloqueado temporalmente.", "danger")
    else:
        flash("PIN incorrecto.", "danger")
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/company-settings/pin/change", methods=["POST"])
@company_admin_required
def company_settings_pin_change():
    flash("Solo el Super Administrador puede asignar o cambiar el PIN.", "warning")
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/company-settings/pin/bootstrap", methods=["POST"])
@company_member_required
def company_settings_pin_bootstrap():
    from app import db, record_audit
    from flask import session

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)

    if company.business_pin_hash:
        flash("El PIN ya esta configurado para esta empresa.", "warning")
        return redirect(url_for("company_billing.company_settings"))

    raw_pin = f"{secrets.randbelow(10000):04d}"
    CompanySecurityService.set_pin(company, raw_pin)
    _mark_pin_verified(company.id, True)
    session[_pin_reveal_session_key(company.id)] = raw_pin
    record_audit(
        action="company_pin_bootstrap",
        entity="company",
        entity_id=company.id,
        detail="PIN inicial de Mi Empresa generado por usuario de la empresa.",
    )
    db.session.commit()
    flash("PIN inicial generado. Guardalo ahora: se muestra una sola vez.", "success")
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/company-settings/pin/regenerate", methods=["POST"])
@company_admin_required
def company_settings_pin_regenerate():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    raw_pin = f"{secrets.randbelow(10000):04d}"
    CompanySecurityService.set_pin(company, raw_pin)
    record_audit(
        action="company_pin_regenerated",
        entity="company",
        entity_id=company.id,
        detail="PIN Mi Empresa regenerado por administrador de empresa.",
    )
    db.session.commit()
    flash(f"PIN regenerado correctamente. Nuevo PIN temporal: {raw_pin}", "success")
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/company-settings/pin/logout", methods=["POST"])
@company_member_required
def company_settings_pin_logout():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    access_pin = (request.form.get("access_pin") or "").strip()
    if not CompanySecurityService.verify_pin(company, access_pin):
        flash("PIN inválido. No se pudo bloquear Mi Empresa.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="security"))

    _mark_pin_verified(company_id, False)
    record_audit(action="company_settings_pin_logout", entity="company", entity_id=company.id, detail="Sesion de Mi Empresa bloqueada manualmente")
    db.session.commit()
    flash("Se cerro la sesion de seguridad de Mi Empresa.", "info")
    return redirect(url_for("dashboard.index"))


@bp.route("/company-settings/users/<int:user_id>/update", methods=["POST"])
@company_admin_required
def company_settings_user_update(user_id):
    from app import User, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    user = User.query.filter_by(id=user_id, company_id=company.id).first_or_404()
    full_name = (request.form.get("full_name") or "").strip()[:160]
    email = (request.form.get("email") or "").strip().lower()[:120]
    role = _normalize_company_role(request.form.get("role"))
    if full_name:
        parts = full_name.split(" ", 1)
        user.first_name = parts[0][:80]
        user.last_name = (parts[1] if len(parts) > 1 else "")[:80] or None
    if email and email != user.email:
        existing_email = User.query.filter(User.email == email, User.id != user.id).first()
        if existing_email is not None:
            flash("Ese email ya esta en uso por otro usuario.", "danger")
            return redirect(url_for("company_billing.company_settings"))
        user.email = email
    if user.id != current_user.id:
        user.role = role
    record_audit(action="company_user_update", entity="user", entity_id=user.id, detail=f"Usuario actualizado por administrador: {user.username}")
    db.session.commit()
    flash("Usuario actualizado correctamente.", "success")
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/company-settings/users/<int:user_id>/role", methods=["POST"])
@company_admin_required
def company_settings_user_role_update(user_id):
    from app import User, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    user = User.query.filter_by(id=user_id, company_id=company.id).first_or_404()
    role = _normalize_company_role(request.form.get("role"))
    if user.id == current_user.id and role != "admin":
        flash("No puedes quitarte el rol administrador desde tu propia sesión.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="employees"))

    user.role = role
    record_audit(action="company_user_role_update", entity="user", entity_id=user.id, detail=f"Rol actualizado a {role} para {user.username}")
    db.session.commit()
    flash("Rol del empleado actualizado correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="employees"))


@bp.route("/company-settings/users/<int:user_id>/permissions", methods=["POST"])
@company_admin_required
def company_settings_user_permissions(user_id):
    from app import User, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    user = User.query.filter_by(id=user_id, company_id=company.id).first_or_404()
    selected = request.form.getlist("permissions")
    _set_user_permissions(user, selected)
    record_audit(action="company_user_permissions", entity="user", entity_id=user.id, detail=f"Permisos actualizados para {user.username}")
    db.session.commit()
    flash("Permisos del empleado actualizados.", "success")
    return redirect(url_for("company_billing.company_settings", panel="employees"))


@bp.route("/company-settings/users/<int:user_id>/delete", methods=["POST"])
@company_admin_required
def company_settings_user_delete(user_id):
    from app import CashMovement, Expense, Sale, User, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    user = User.query.filter_by(id=user_id, company_id=company.id).first_or_404()
    if user.id == current_user.id:
        flash("No puedes eliminar tu propio usuario administrador.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="employees"))

    linked_sales = Sale.query.filter_by(company_id=company.id, seller_id=user.id).count()
    linked_cash = CashMovement.query.filter_by(company_id=company.id, user_id=user.id).count()
    linked_expenses = Expense.query.filter_by(company_id=company.id, user_id=user.id).count()
    has_history = (linked_sales + linked_cash + linked_expenses) > 0

    if has_history:
        user.active = False
        record_audit(
            action="company_user_soft_delete",
            entity="user",
            entity_id=user.id,
            detail=f"Usuario desactivado por historial vinculado ({linked_sales} ventas, {linked_cash} movimientos, {linked_expenses} gastos)",
        )
        db.session.commit()
        flash("El empleado tenía historial y fue desactivado en lugar de eliminarse.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="employees"))

    username = user.username
    db.session.delete(user)
    record_audit(action="company_user_delete", entity="user", entity_id=user_id, detail=f"Empleado eliminado: {username}")
    db.session.commit()
    flash("Empleado eliminado correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="employees"))


@bp.route("/company-settings/users/create", methods=["POST"])
@company_admin_required
def company_settings_user_create():
    from app import User, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    allowed, message = PlanUsageService.can_create(company.id, PlanUsageService.RESOURCE_USERS)
    if not allowed:
        flash(message, "danger")
        return redirect(url_for("company_billing.company_settings"))

    username = (request.form.get("username") or "").strip()[:80]
    email = (request.form.get("email") or "").strip().lower()[:120]
    full_name = (request.form.get("full_name") or "").strip()[:160]
    role = _normalize_company_role(request.form.get("role"))

    if not username or not email:
        flash("Debes completar usuario y email.", "danger")
        return redirect(url_for("company_billing.company_settings"))
    if User.query.filter_by(username=username).first() is not None:
        flash("Ese nombre de usuario ya existe.", "danger")
        return redirect(url_for("company_billing.company_settings"))
    if User.query.filter_by(email=email).first() is not None:
        flash("Ese email ya existe.", "danger")
        return redirect(url_for("company_billing.company_settings"))

    temp_password = _temporary_password()
    user = User(username=username, email=email, company_id=company.id, role=role, active=True, auth_provider="local")
    if full_name:
        parts = full_name.split(" ", 1)
        user.first_name = parts[0][:80]
        user.last_name = (parts[1] if len(parts) > 1 else "")[:80] or None
    user.set_password(temp_password)
    user.must_change_password = True
    db.session.add(user)
    db.session.flush()
    record_audit(action="company_user_create", entity="user", entity_id=user.id, detail=f"Alta de usuario {user.username}")
    db.session.commit()
    flash(f"Empleado creado correctamente. Contrasena temporal: {temp_password}", "success")
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/company-settings/users/<int:user_id>/toggle", methods=["POST"])
@company_admin_required
def company_settings_user_toggle(user_id):
    from app import User, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    user = User.query.filter_by(id=user_id, company_id=company.id).first_or_404()
    if user.id == current_user.id and user.active:
        flash("No puedes desactivar tu propio usuario administrador.", "warning")
        return redirect(url_for("company_billing.company_settings"))

    if not user.active:
        allowed, message = PlanUsageService.can_create(company.id, PlanUsageService.RESOURCE_USERS)
        if not allowed:
            flash(message, "danger")
            return redirect(url_for("company_billing.company_settings"))

    user.active = not user.active
    record_audit(action="company_user_toggle", entity="user", entity_id=user.id, detail=f"Usuario {'activado' if user.active else 'desactivado'}")
    db.session.commit()
    flash(f"Usuario {'activado' if user.active else 'desactivado'} correctamente.", "success")
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/company-settings/users/<int:user_id>/reset-password", methods=["POST"])
@company_admin_required
def company_settings_user_reset_password(user_id):
    from app import User, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    user = User.query.filter_by(id=user_id, company_id=company.id).first_or_404()
    temp_password = _temporary_password()
    user.set_password(temp_password)
    user.must_change_password = True
    record_audit(action="company_user_reset_password", entity="user", entity_id=user.id, detail=f"Contrasena restablecida para {user.username}")
    db.session.commit()
    flash(f"Contrasena restablecida. Temporal para {user.username}: {temp_password}", "success")
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/company-settings/password", methods=["POST"])
@company_member_required
def company_settings_change_password():
    from app import db, record_audit

    current_password = (request.form.get("current_password") or "").strip()
    new_password = (request.form.get("new_password") or "").strip()
    confirm_password = (request.form.get("confirm_password") or "").strip()

    if not current_user.check_password(current_password):
        flash("La contrasena actual es incorrecta.", "danger")
        return redirect(url_for("company_billing.company_settings"))
    if len(new_password) < 6:
        flash("La nueva contrasena debe tener al menos 6 caracteres.", "danger")
        return redirect(url_for("company_billing.company_settings"))
    if new_password != confirm_password:
        flash("Las contrasenas no coinciden.", "danger")
        return redirect(url_for("company_billing.company_settings"))

    current_user.set_password(new_password)
    current_user.must_change_password = False
    record_audit(action="company_admin_password_change", entity="user", entity_id=current_user.id, detail="Contrasena de administrador actualizada desde Mi Empresa")
    db.session.commit()
    flash("Contrasena actualizada correctamente.", "success")
    return redirect(url_for("company_billing.company_settings"))


@bp.route("/company-settings/general", methods=["POST"])
@company_admin_required
def company_settings_general_save():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    company.language = (request.form.get("language") or "es").strip()[:20] or "es"
    company.timezone = (request.form.get("timezone") or "America/Argentina/Buenos_Aires").strip()[:80] or "America/Argentina/Buenos_Aires"
    company.currency = (request.form.get("currency") or "ARS").strip()[:10] or "ARS"
    company.date_format = (request.form.get("date_format") or "%Y-%m-%d").strip()[:20] or "%Y-%m-%d"
    company.numbering_format = (request.form.get("numbering_format") or "es_AR").strip()[:20] or "es_AR"

    preferences = {
        "allow_negative_stock": bool(request.form.get("allow_negative_stock")),
        "show_costs": bool(request.form.get("show_costs")),
        "compact_print": bool(request.form.get("compact_print")),
    }
    printer_settings = {
        "printer_name": (request.form.get("printer_name") or "").strip()[:160],
        "paper_size": (request.form.get("paper_size") or "A4").strip()[:20] or "A4",
    }

    company.preferences_json = json.dumps(preferences)
    company.printer_settings_json = json.dumps(printer_settings)
    record_audit(action="company_general_settings", entity="company", entity_id=company.id, detail="Configuracion general actualizada")
    db.session.commit()
    flash("Configuración general guardada correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="general"))


@bp.route("/company-settings/schedules", methods=["POST"])
@company_admin_required
def company_settings_schedules_save():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    weekdays = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
    weekly = {}
    for day in weekdays:
        weekly[day] = {
            "open": (request.form.get(f"{day}_open") or "").strip()[:5],
            "close": (request.form.get(f"{day}_close") or "").strip()[:5],
        }

    schedules_payload = {
        "weekly": weekly,
        "special_shifts": (request.form.get("special_shifts") or "").strip()[:2000],
        "vacations": (request.form.get("vacations") or "").strip()[:2000],
        "licenses": (request.form.get("licenses") or "").strip()[:2000],
    }
    existing = _company_schedules_payload(company)
    schedules_payload["employee_assignments"] = existing.get("employee_assignments", [])
    company.schedules_json = json.dumps(schedules_payload)
    record_audit(action="company_schedules_update", entity="company", entity_id=company.id, detail="Horarios de atencion actualizados")
    db.session.commit()
    flash("Horarios de atención guardados correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="schedules"))


@bp.route("/company-settings/schedules/assign", methods=["POST"])
@company_admin_required
def company_settings_schedules_assign_add():
    from app import User, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    user_id = request.form.get("user_id", type=int)
    day = (request.form.get("day") or "").strip().lower()
    start = (request.form.get("start") or "").strip()[:5]
    end = (request.form.get("end") or "").strip()[:5]
    valid_days = {"lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"}

    user = User.query.filter_by(id=user_id, company_id=company.id, active=True).first()
    if user is None:
        flash("Debes seleccionar un empleado activo válido.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="schedules"))
    if day not in valid_days or not start or not end:
        flash("Completa día y rango horario para asignar actividad.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="schedules"))
    if start >= end:
        flash("El horario de inicio debe ser menor al de cierre.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="schedules"))

    schedules_payload = _company_schedules_payload(company)
    assignments = schedules_payload.get("employee_assignments", [])
    assignments.append({
        "id": secrets.token_hex(6),
        "user_id": user.id,
        "day": day,
        "start": start,
        "end": end,
    })
    schedules_payload["employee_assignments"] = assignments
    company.schedules_json = json.dumps(schedules_payload)
    record_audit(action="company_schedule_assignment_add", entity="company", entity_id=company.id, detail=f"Asignación de horario para {user.username} {day} {start}-{end}")
    db.session.commit()
    flash("Asignación de horario guardada correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="schedules"))


@bp.route("/company-settings/schedules/assign/<string:assignment_id>/delete", methods=["POST"])
@company_admin_required
def company_settings_schedules_assign_delete(assignment_id):
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    schedules_payload = _company_schedules_payload(company)
    assignments = schedules_payload.get("employee_assignments", [])
    filtered = [row for row in assignments if row.get("id") != assignment_id]
    if len(filtered) == len(assignments):
        flash("No se encontró la asignación solicitada.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="schedules"))

    schedules_payload["employee_assignments"] = filtered
    company.schedules_json = json.dumps(schedules_payload)
    record_audit(action="company_schedule_assignment_delete", entity="company", entity_id=company.id, detail=f"Asignación eliminada {assignment_id}")
    db.session.commit()
    flash("Asignación de horario eliminada correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="schedules"))


@bp.route("/company-settings/security/logout-current", methods=["POST"])
@company_member_required
def company_settings_security_logout_current():
    from app import db, record_audit
    from flask_login import logout_user

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    _mark_pin_verified(company.id, False)
    record_audit(action="company_security_logout_current", entity="company", entity_id=company.id, detail="Usuario cerro sesion actual desde Mi Empresa")
    db.session.commit()
    logout_user()
    flash("Sesión cerrada correctamente.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/company-settings/billing/invoice/<int:invoice_id>/pdf")
@company_member_required
def company_settings_billing_invoice_pdf(invoice_id):
    from app import Invoice

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    invoice = Invoice.query.filter_by(id=invoice_id, company_id=company.id).first_or_404()
    lines = [
        f"Empresa: {company.name}",
        f"Factura: {invoice.invoice_number or ('#' + str(invoice.id))}",
        f"Estado: {invoice.status or '-'}",
        f"Importe: {float(invoice.amount or 0):.2f} {invoice.currency or ''}",
        f"Vencimiento: {invoice.due_at.strftime('%Y-%m-%d') if invoice.due_at else '-'}",
        f"Emision: {invoice.issued_at.strftime('%Y-%m-%d %H:%M') if invoice.issued_at else '-'}",
        f"Detalle: {(invoice.detail or '-').strip()[:500]}",
    ]
    return _pdf_from_lines("Factura SaaS - StockArmobile", lines, f"factura_{invoice.id}.pdf")


@bp.route("/company-settings/billing/payment/<int:payment_id>/pdf")
@company_member_required
def company_settings_billing_payment_pdf(payment_id):
    from app import Payment

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    payment = Payment.query.filter_by(id=payment_id, company_id=company.id).first_or_404()
    lines = [
        f"Empresa: {company.name}",
        f"Pago: {payment.payment_id or ('#' + str(payment.id))}",
        f"Estado: {payment.status or '-'}",
        f"Importe: {float(payment.amount or 0):.2f} {payment.currency or ''}",
        f"Metodo: {payment.payment_method or '-'}",
        f"Referencia: {payment.reference or '-'}",
        f"Fecha de registro: {payment.created_at.strftime('%Y-%m-%d %H:%M') if payment.created_at else '-'}",
    ]
    return _pdf_from_lines("Comprobante de Pago - StockArmobile", lines, f"pago_{payment.id}.pdf")


@bp.route("/company-settings/backups/create", methods=["POST"])
@company_member_required
def company_settings_backups_create():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    backup, plan = BackupService.create_manual_backup(company_id, user_id=current_user.id, trigger_type="manual")
    record_audit(
        action="backup_create",
        entity="backup",
        entity_id=backup.id,
        company_id=company_id,
        detail=f"Backup manual creado por usuario empresa. plan={plan['code']}",
    )
    db.session.commit()
    flash("Backup creado correctamente.", "success")
    if BackupService.plan_limit_status(company_id)["count"] >= plan["limit"]:
        flash("Límite de Backups alcanzado. Se eliminó automáticamente el backup más antiguo.", "warning")
    return redirect(url_for("company_billing.company_settings", panel="backups"))


@bp.route("/company-settings/backups/import", methods=["POST"])
@company_member_required
def company_settings_backups_import():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    backup_file = request.files.get("backup_file")
    if not backup_file or not getattr(backup_file, "filename", "").strip():
        flash("Seleccioná un archivo de backup válido.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="backups"))

    try:
        backup, plan, payload = BackupService.import_backup_file(company_id=company.id, file_storage=backup_file, created_by_user_id=current_user.id)
        record_audit(
            action="backup_import",
            entity="backup",
            entity_id=backup.id,
            company_id=company.id,
            detail=f"Backup importado por usuario empresa. plan={plan['code']} version={payload.get('schema_version')}",
        )
        db.session.commit()
        flash("Backup importado correctamente. Revisá el resumen antes de restaurar.", "success")
        return redirect(url_for("company_billing.company_settings", panel="backups", preview_id=backup.id))
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("No se pudo importar el backup de empresa: %s", exc)
        flash("No se pudo importar el backup.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="backups"))


@bp.route("/company-settings/backups/<int:backup_id>/download")
@company_member_required
def company_settings_backups_download(backup_id):
    from app import BackupLog

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    backup = BackupLog.query.filter_by(id=backup_id, company_id=company_id).first_or_404()
    backup_path = BackupService.backup_download_path(backup)
    return send_file(
        backup_path,
        mimetype="application/gzip",
        as_attachment=True,
        download_name=backup.file_name or backup_path.name,
    )


@bp.route("/company-settings/backups/<int:backup_id>/restore", methods=["POST"])
@company_member_required
def company_settings_backups_restore(backup_id):
    from app import BackupLog, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    backup = BackupLog.query.filter_by(id=backup_id, company_id=company_id).first_or_404()
    sections = request.form.getlist("sections")
    confirm_restore = (request.form.get("confirm_restore") or "").strip() == "1"
    if not confirm_restore:
        return redirect(url_for("company_billing.company_settings", panel="backups", preview_id=backup.id))

    try:
        BackupService.restore_backup(backup, expected_company_id=company_id, restored_by_user_id=current_user.id, sections=sections)
        record_audit(
            action="backup_restore",
            entity="backup",
            entity_id=backup.id,
            company_id=company_id,
            detail=f"Backup restaurado desde Mi Empresa. sections={','.join(sections or ['full'])}",
        )
        db.session.commit()
        flash("Backup restaurado correctamente.", "success")
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("No se pudo restaurar el backup de empresa: %s", exc)
        flash("No se pudo restaurar el backup.", "danger")
    return redirect(url_for("company_billing.company_settings", panel="backups"))


@bp.route("/company-settings/backups/<int:backup_id>/delete", methods=["POST"])
@company_member_required
def company_settings_backups_delete(backup_id):
    from app import BackupLog, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    backup = BackupLog.query.filter_by(id=backup_id, company_id=company_id).first_or_404()
    BackupService.delete_backup(backup)
    record_audit(
        action="backup_delete",
        entity="backup",
        entity_id=backup_id,
        company_id=company_id,
        detail="Backup eliminado desde Mi Empresa.",
    )
    db.session.commit()
    flash("Backup eliminado correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="backups"))


@bp.route("/company-settings")
@company_member_required
def company_settings():
    from flask import session
    from sqlalchemy.orm import selectinload

    from app import AuditLog, CashSession, Invoice, Payment, PaymentHistory, Sale, SaleItem, User

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    settings_panel = (request.args.get("panel") or "").strip().lower()

    pin_verified = _is_pin_verified(company.id)
    date_from_raw = request.args.get("from") or ""
    date_to_raw = request.args.get("to") or ""
    if not settings_panel and (date_from_raw or date_to_raw):
        settings_panel = "stats"
    date_from = _parse_date(date_from_raw)
    date_to = _parse_date(date_to_raw)
    employee_search = (request.args.get("q") or "").strip()
    employee_role = (request.args.get("role") or "").strip().lower()
    employee_status = (request.args.get("status") or "").strip().lower()

    users = []
    cash_rows = []
    recent_sales = []
    usage_snapshot, users_metric = _plan_limit_context(company.id)
    subscription = usage_snapshot["subscription"]
    plan = usage_snapshot["plan"]
    pin_created_at, pin_last_used_at = _pin_metadata(company)
    pin_bootstrap_reveal = session.pop(_pin_reveal_session_key(company.id), None)
    cash_summary = {"total_sold": 0.0, "total_sales": 0, "average_ticket": 0.0}
    cash_sessions_recent = []
    open_cash_session = None
    billing_invoices = []
    billing_payments = []
    billing_history = []
    company_preferences = _json_company_dict(company.preferences_json)
    printer_settings = _json_company_dict(company.printer_settings_json)
    schedules_settings = _company_schedules_payload(company)
    schedule_assignments = schedules_settings.get("employee_assignments", [])
    mercado_pago_connection_summary = _mercadopago_connection_summary(company)
    active_employees = []
    device_rows = []
    backups = []
    backup_plan = {}
    backup_storage_used = "0.00 B"
    backup_automation = BackupService.automation_scaffold()
    backup_summaries = {}
    selected_backup = None
    selected_backup_summary = None
    preview_backup_id = request.args.get("preview_id", type=int)
    if pin_verified:
        users, cash_rows = _build_user_and_cash_rows(
            company.id,
            date_from=date_from,
            date_to=date_to,
            search_text=employee_search if settings_panel == "employees" else "",
            role_filter=employee_role if settings_panel == "employees" else "",
            status_filter=employee_status if settings_panel == "employees" else "",
        )
        cash_summary = _cash_summary(cash_rows)
        recent_sales = (
            Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product))
            .filter_by(company_id=company.id)
            .order_by(Sale.date.desc())
            .limit(12)
            .all()
        )
        billing_invoices = (
            Invoice.query.filter_by(company_id=company.id)
            .order_by(Invoice.issued_at.desc(), Invoice.id.desc())
            .limit(30)
            .all()
        )
        billing_payments = (
            Payment.query.filter_by(company_id=company.id)
            .order_by(Payment.created_at.desc(), Payment.id.desc())
            .limit(30)
            .all()
        )
        billing_history = (
            PaymentHistory.query.filter_by(company_id=company.id)
            .order_by(PaymentHistory.created_at.desc(), PaymentHistory.id.desc())
            .limit(30)
            .all()
        )
        active_employees = (
            User.query.filter_by(company_id=company.id, active=True)
            .order_by(User.first_name.asc(), User.last_name.asc(), User.username.asc())
            .all()
        )
        device_rows = (
            AuditLog.query.filter_by(company_id=company.id, action="login_success")
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(12)
            .all()
        )
        cash_sessions_recent = (
            CashSession.query.filter_by(company_id=company.id)
            .order_by(CashSession.opened_at.desc(), CashSession.id.desc())
            .limit(12)
            .all()
        )
        open_cash_session = next((item for item in cash_sessions_recent if (item.status or "").lower() == "abierta"), None)
        backups = BackupService.company_backups(company.id)
        backup_plan = BackupService.plan_limit_status(company.id)
        backup_storage_used = _format_size(sum(int(item.file_size_bytes or 0) for item in backups))
        for backup in backups:
            try:
                backup_summaries[backup.id] = BackupService.summarize_backup(backup)
            except Exception:
                backup_summaries[backup.id] = {"schema_version": "-", "system_version": "-", "company_id": backup.company_id, "generated_at": None, "products": 0, "inventory": 0, "categories": 0, "clients": 0, "sales": 0, "employees": 0, "schedules": 0}
        if preview_backup_id:
            selected_backup = next((item for item in backups if item.id == preview_backup_id), None)
            if selected_backup is not None:
                selected_backup_summary = backup_summaries.get(selected_backup.id)

    return render_template(
        "company_billing/settings.html",
        company=company,
        users=users,
        cash_rows=cash_rows,
        recent_sales=recent_sales,
        cash_summary=cash_summary,
        subscription=subscription,
        current_plan=plan,
        plan_expiration=_subscription_expiration(subscription, company),
        users_metric=users_metric,
        usage_snapshot=usage_snapshot,
        pin_verified=pin_verified,
        settings_panel=settings_panel,
        pin_created_at=pin_created_at,
        pin_last_used_at=pin_last_used_at,
        pin_bootstrap_reveal=pin_bootstrap_reveal,
        date_from=date_from_raw,
        date_to=date_to_raw,
        pin_block_seconds=CompanySecurityService.remaining_block_seconds(company),
        billing_invoices=billing_invoices,
        billing_payments=billing_payments,
        billing_history=billing_history,
        employee_search=employee_search,
        employee_role=employee_role,
        employee_status=employee_status,
        employee_permissions=EMPLOYEE_PERMISSIONS,
        company_preferences=company_preferences,
        printer_settings=printer_settings,
        schedules_settings=schedules_settings,
        schedule_assignments=schedule_assignments,
        mercado_pago_connection_summary=mercado_pago_connection_summary,
        active_employees=active_employees,
        device_rows=device_rows,
        cash_sessions_recent=cash_sessions_recent,
        open_cash_session=open_cash_session,
        backups=backups,
        backup_plan=backup_plan,
        backup_storage_used=backup_storage_used,
        backup_automation=backup_automation,
        backup_summaries=backup_summaries,
        selected_backup=selected_backup,
        selected_backup_summary=selected_backup_summary,
        backup_section_options=BackupService.restore_section_options(),
        preview_backup_id=preview_backup_id,
        format_size=_format_size,
    )


@bp.route("/webhooks/mercadopago", methods=["POST"])
@csrf.exempt
def webhook_mercadopago():
    from app import db, record_audit

    payload = request.get_json(silent=True) or {}
    try:
        result = WebhookService().process(db_session=db.session, headers=dict(request.headers), payload=payload)
        record_audit(action="webhook_mercadopago", entity="webhook", detail=f"Webhook procesado: {result.get('status')}")
        db.session.commit()
    except Exception as exc:
        current_app.logger.exception("Webhook Mercado Pago rechazado: %s", exc)
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "result": result}), 200

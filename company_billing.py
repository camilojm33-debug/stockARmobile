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
from sqlalchemy.exc import IntegrityError
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app import company_admin_required, csrf, tenant_required
from config.billing_config import load_billing_config
from services.billing_service import BillingService
from services.backup_service import BackupService
from services.business_billing_service import BusinessBillingService
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
    ("quotes_view", "Ver presupuestos"),
    ("quotes_create", "Crear presupuestos"),
    ("quotes_edit", "Editar presupuestos"),
    ("quotes_delete", "Eliminar presupuestos"),
    ("quotes_duplicate", "Duplicar presupuestos"),
    ("quotes_share_whatsapp", "Compartir presupuestos por WhatsApp"),
    ("quotes_email", "Enviar presupuestos por email"),
    ("quotes_convert", "Convertir presupuestos"),
    ("quotes_print", "Imprimir presupuestos"),
    ("quotes_download_pdf", "Descargar PDF de presupuestos"),
    ("quotes_view_other_sellers", "Ver presupuestos de otros vendedores"),
    ("quotes_modify_prices", "Modificar precios en presupuestos"),
    ("quotes_apply_discounts", "Aplicar descuentos en presupuestos"),
    ("quotes_anulate", "Anular presupuestos"),
    ("clients", "Clientes"),
    ("reports", "Reportes"),
    ("cash", "Caja"),
    ("economic_stats", "Puede visualizar estadísticas económicas"),
]

BILLING_DOCUMENT_TYPES = [
    ("factura_a", "Factura A"),
    ("factura_b", "Factura B"),
    ("factura_c", "Factura C"),
    ("nota_credito", "Nota de Credito"),
    ("nota_debito", "Nota de Debito"),
    ("remito", "Remito"),
    ("presupuesto", "Presupuesto"),
    ("recibo", "Recibo"),
]


def _normalize_pos_number(raw_value):
    digits = "".join(ch for ch in str(raw_value or "") if ch.isdigit())
    if not digits:
        return "00001"
    return digits[-5:].zfill(5)


def _default_billing_config(company):
    enabled_docs = {key: key in {"factura_b", "factura_c", "presupuesto", "recibo"} for key, _ in BILLING_DOCUMENT_TYPES}
    return {
        "fiscal": {
            "tax_id": (getattr(company, "tax_id", None) or "").strip(),
            "legal_name": (getattr(company, "legal_name", None) or getattr(company, "name", None) or "").strip(),
            "iva_condition": "Consumidor Final",
            "activity_start": "",
            "tax_profile": "Monotributo",
            "gross_income": "",
            "jurisdiction": "",
        },
        "documents_enabled": enabled_docs,
        "points_of_sale": [
            {
                "number": "00001",
                "description": "Casa central",
                "active": True,
            }
        ],
        "active_pos": "00001",
        "numbering": {
            "factura_a": "00001-00000001",
            "factura_b": "00001-00000001",
            "factura_c": "00001-00000001",
            "nota_credito": "00001-00000001",
            "nota_debito": "00001-00000001",
            "remito": "00001-00000001",
            "presupuesto": "00001-00000001",
            "recibo": "00001-00000001",
        },
        "print_template": {
            "logo": (getattr(company, "logo", None) or "").strip(),
            "footer": "Gracias por su compra.",
            "commercial_terms": "Pago contado.",
            "observations": "",
            "show_qr": True,
            "show_barcode": False,
            "format_a4": True,
            "format_ticket_58": False,
            "format_ticket_80": True,
        },
        "electronic": {
            "status": "coming_soon",
            "connected": False,
            "certificate": "",
            "certificate_expires_at": "",
            "environment": "homologacion",
        },
    }


def _load_company_billing_config(company, company_preferences):
    defaults = _default_billing_config(company)
    stored = company_preferences.get("billing_business") if isinstance(company_preferences, dict) else None
    if not isinstance(stored, dict):
        stored = {}

    fiscal = defaults["fiscal"].copy()
    fiscal.update(stored.get("fiscal") or {})

    docs = defaults["documents_enabled"].copy()
    docs.update(stored.get("documents_enabled") or {})
    docs = {key: bool(docs.get(key)) for key, _ in BILLING_DOCUMENT_TYPES}

    points_raw = stored.get("points_of_sale")
    points_of_sale = []
    if isinstance(points_raw, list):
        for row in points_raw:
            if not isinstance(row, dict):
                continue
            number = _normalize_pos_number(row.get("number"))
            description = (str(row.get("description") or "").strip() or f"Punto de venta {number}")[:80]
            points_of_sale.append({"number": number, "description": description, "active": bool(row.get("active", True))})
    if not points_of_sale:
        points_of_sale = defaults["points_of_sale"]

    active_pos = _normalize_pos_number(stored.get("active_pos") or points_of_sale[0].get("number"))
    if not any(item.get("number") == active_pos for item in points_of_sale):
        active_pos = points_of_sale[0].get("number")

    numbering = defaults["numbering"].copy()
    numbering.update(stored.get("numbering") or {})
    for key, _ in BILLING_DOCUMENT_TYPES:
        current = str(numbering.get(key) or "").strip()
        numbering[key] = current if current else f"{active_pos}-00000001"

    print_template = defaults["print_template"].copy()
    print_template.update(stored.get("print_template") or {})
    print_template["show_qr"] = bool(print_template.get("show_qr"))
    print_template["show_barcode"] = bool(print_template.get("show_barcode"))
    print_template["format_a4"] = bool(print_template.get("format_a4", True))
    print_template["format_ticket_58"] = bool(print_template.get("format_ticket_58"))
    print_template["format_ticket_80"] = bool(print_template.get("format_ticket_80", True))

    electronic = defaults["electronic"].copy()
    electronic.update(stored.get("electronic") or {})
    electronic["connected"] = bool(electronic.get("connected"))
    electronic["status"] = str(electronic.get("status") or "coming_soon")
    electronic["environment"] = str(electronic.get("environment") or "homologacion")

    return {
        "fiscal": fiscal,
        "documents_enabled": docs,
        "points_of_sale": points_of_sale,
        "active_pos": active_pos,
        "numbering": numbering,
        "print_template": print_template,
        "electronic": electronic,
    }


def _business_billing_status(config):
    fiscal = config.get("fiscal") or {}
    required_fields = [
        (fiscal.get("tax_id") or "").strip(),
        (fiscal.get("legal_name") or "").strip(),
        (fiscal.get("iva_condition") or "").strip(),
    ]
    filled = sum(1 for item in required_fields if item)
    if filled == len(required_fields):
        return {"label": "Configurada", "class": "text-bg-success"}
    if filled == 0:
        return {"label": "Requiere configuración", "class": "text-bg-danger"}
    return {"label": "Pendiente", "class": "text-bg-warning"}


def _format_document_type_label(raw_value):
    normalized = (raw_value or "").strip().lower()
    mapping = {key: label for key, label in BILLING_DOCUMENT_TYPES}
    if normalized in mapping:
        return mapping[normalized]
    if normalized.startswith("factura"):
        suffix = normalized.replace("factura", "").replace("_", " ").strip().upper()
        return f"Factura {suffix}" if suffix else "Factura"
    return normalized.replace("_", " ").title() or "Comprobante"


def _sale_business_status_badge(sale):
    if bool(getattr(sale, "comprobante_emitido", False)):
        return {"label": "Emitido", "class": "text-bg-success"}
    status = (getattr(sale, "status", "") or "").strip().lower()
    if status in {"anulada", "cancelada", "rechazada"}:
        return {"label": "Anulado", "class": "text-bg-danger"}
    if status in {"borrador", "pendiente"}:
        return {"label": "Pendiente", "class": "text-bg-warning"}
    return {"label": "En proceso", "class": "text-bg-primary"}


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


def _can_view_business_billing(user):
    role = (getattr(user, "role", None) or "").strip().lower()
    if role == "admin":
        return True
    if role != "user":
        return False
    user_permissions = set(_user_permissions(user))
    return bool(user_permissions.intersection({"reports", "sales", "quotes_view", "cash", "economic_stats"}))


def business_billing_view_required(func):
    @wraps(func)
    @tenant_required
    def decorated(*args, **kwargs):
        if not _can_view_business_billing(current_user):
            abort(403)
        return func(*args, **kwargs)

    return decorated


def business_billing_admin_required(func):
    @wraps(func)
    @tenant_required
    def decorated(*args, **kwargs):
        if (getattr(current_user, "role", None) or "").strip().lower() != "admin":
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


def _subscription_state_badge(status, days_remaining):
    status = (status or "trial").lower()
    label_map = {
        "trial": "Prueba gratuita",
        "trial_expired": "Prueba vencida",
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
    if status == "trial_expired":
        return {"label": "Prueba vencida", "class": "text-bg-danger", "indicator": "danger", "text": "Vencida"}
    if status == "trial":
        if days_remaining is not None and days_remaining <= 1:
            return {"label": "Prueba gratuita", "class": "text-bg-danger", "indicator": "danger", "text": "Vence hoy"}
        if days_remaining is not None and days_remaining <= 3:
            return {"label": "Prueba gratuita", "class": "text-bg-warning", "indicator": "warning", "text": "Quedan pocos días"}
        if days_remaining is not None and days_remaining <= 7:
            return {"label": "Prueba gratuita", "class": "text-bg-warning", "indicator": "warning", "text": "Próxima a vencer"}
        return {"label": "Prueba gratuita", "class": "text-bg-success", "indicator": "success", "text": "Activa"}
    if days_remaining is None:
        return {"label": label_map.get(status, status.title()), "class": "text-bg-info", "indicator": "success", "text": "Activa"}
    if days_remaining <= 0:
        return {"label": label_map.get(status, status.title()), "class": "text-bg-danger", "indicator": "danger", "text": "Vencida"}
    if days_remaining <= 3:
        return {"label": label_map.get(status, status.title()), "class": "text-bg-danger", "indicator": "warning", "text": "Quedan pocos días"}
    if days_remaining <= 15:
        return {"label": label_map.get(status, status.title()), "class": "text-bg-warning", "indicator": "warning", "text": "Próxima a vencer"}
    return {"label": label_map.get(status, status.title()), "class": "text-bg-success", "indicator": "success", "text": "Activa"}


def _payment_status_badge(status):
    normalized = (status or "pending").strip().lower()
    mapping = {
        "approved": {"label": "Pagado", "class": "text-bg-success"},
        "active": {"label": "Pagado", "class": "text-bg-success"},
        "paid": {"label": "Pagado", "class": "text-bg-success"},
        "pending": {"label": "Pendiente", "class": "text-bg-warning"},
        "authorized": {"label": "Pendiente", "class": "text-bg-warning"},
        "in_process": {"label": "En proceso", "class": "text-bg-primary"},
        "processing": {"label": "En proceso", "class": "text-bg-primary"},
        "rejected": {"label": "Rechazado", "class": "text-bg-danger"},
        "cancelled": {"label": "Rechazado", "class": "text-bg-danger"},
        "failed": {"label": "Rechazado", "class": "text-bg-danger"},
    }
    return mapping.get(normalized, {"label": normalized.replace("_", " ").title(), "class": "text-bg-secondary"})


def _subscription_frequency_label(subscription):
    duration_days = int(getattr(getattr(subscription, "plan", None), "duration_days", 30) or 30)
    return "Anual" if duration_days >= 365 else "Mensual"


def _human_payment_method(value):
    raw = (value or "").strip().lower()
    if not raw:
        return "Sin método registrado"
    aliases = {
        "credit_card": "Tarjeta de crédito",
        "debit_card": "Tarjeta de débito",
        "account_money": "Dinero en cuenta",
        "pix": "PIX",
        "ticket": "Pago en efectivo",
        "bank_transfer": "Transferencia bancaria",
    }
    return aliases.get(raw, raw.replace("_", " ").title())


def _timeline_event_label(event_name):
    normalized = (event_name or "").strip().lower()
    label_map = {
        "subscription_created": "Suscripción creada",
        "subscription_change": "Cambio de plan",
        "subscription_plan_changed": "Cambio de plan",
        "subscription_renewed": "Renovación automática",
        "subscription_reactivated": "Reactivación",
        "subscription_cancel": "Cancelación",
        "subscription_cancel_requested": "Cancelación",
        "payment_approved": "Pago aprobado",
        "payment_rejected": "Pago rechazado",
        "payment_pending": "Pago en proceso",
        "payment_in_process": "Pago en proceso",
        "payment_method_updated": "Método de pago actualizado",
    }
    if normalized in label_map:
        return label_map[normalized]
    if "approved" in normalized:
        return "Pago aprobado"
    if "rejected" in normalized:
        return "Pago rechazado"
    if "cancel" in normalized:
        return "Cancelación"
    if "reactivat" in normalized:
        return "Reactivación"
    if "renew" in normalized:
        return "Renovación automática"
    if "plan" in normalized:
        return "Cambio de plan"
    return "Actualización de suscripción"


def _timeline_event_icon(label):
    icon_map = {
        "Suscripción creada": "bi-rocket-takeoff",
        "Cambio de plan": "bi-arrow-repeat",
        "Renovación automática": "bi-arrow-clockwise",
        "Pago aprobado": "bi-check2-circle",
        "Pago rechazado": "bi-x-circle",
        "Pago en proceso": "bi-hourglass-split",
        "Método de pago actualizado": "bi-credit-card-2-front",
        "Cancelación": "bi-slash-circle",
        "Reactivación": "bi-play-circle",
        "Actualización de suscripción": "bi-dot",
    }
    return icon_map.get(label, "bi-dot")


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

    from app import Company, Invoice, Payment, PaymentHistory, ReferralAttribution

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()

    plans = PlanService.all_commercial_plans()
    subscription = SubscriptionService.active_subscription_for_company(company.id)
    effective_state = SubscriptionService.resolve_company_access_state(company, subscription=subscription)

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
    recent_events = (
        PaymentHistory.query.filter_by(company_id=company.id)
        .order_by(PaymentHistory.created_at.desc(), PaymentHistory.id.desc())
        .limit(40)
        .all()
    )
    referral_attribution = ReferralAttribution.query.filter_by(company_id=company.id).first()
    managed_by_seller = referral_attribution.seller.user.username if referral_attribution and referral_attribution.seller and referral_attribution.seller.user else None
    reference_date = (
        effective_state.get("reference_date")
        or effective_state.get("next_billing_date")
        or effective_state.get("trial_ends_at")
    )
    days_remaining = _days_remaining(reference_date)
    status_badge = _subscription_state_badge(effective_state.get("status"), days_remaining)
    plan_features = _plan_features_label(subscription.plan if subscription else None)
    next_amount = float(getattr(getattr(subscription, "plan", None), "price", 0) or 0)
    currency = getattr(getattr(subscription, "plan", None), "currency", None) or "ARS"
    frequency_label = _subscription_frequency_label(subscription)
    last_payment_with_method = next((item for item in recent_payments if (item.payment_method or "").strip()), None)
    payment_method_label = _human_payment_method(last_payment_with_method.payment_method if last_payment_with_method else None)
    payment_rows = []
    for payment in recent_payments:
        status = _payment_status_badge(payment.status)
        concept = f"Plan {payment.subscription.plan.name}" if payment.subscription and payment.subscription.plan else "Suscripción StockArMobile"
        payment_rows.append(
            {
                "date": payment.paid_at or payment.created_at,
                "concept": concept,
                "status": status,
                "amount": float(payment.amount or 0),
                "currency": payment.currency or currency,
                "method": _human_payment_method(payment.payment_method),
                "receipt": payment.payment_id or f"#{payment.id}",
                "payment_id": payment.id,
            }
        )
    invoice_rows = []
    for invoice in recent_invoices:
        status = _payment_status_badge(invoice.status)
        invoice_rows.append(
            {
                "id": invoice.id,
                "number": invoice.invoice_number or f"#{invoice.id}",
                "issued_at": invoice.issued_at,
                "due_at": invoice.due_at,
                "amount": float(invoice.amount or 0),
                "currency": invoice.currency or currency,
                "status": status,
                "detail": (invoice.detail or "").strip(),
            }
        )

    timeline_items = []
    if subscription and subscription.created_at:
        timeline_items.append(
            {
                "created_at": subscription.created_at,
                "label": "Suscripción creada",
                "icon": _timeline_event_icon("Suscripción creada"),
                "detail": "Se inició la relación comercial con StockArMobile.",
            }
        )
    for event in recent_events:
        label = _timeline_event_label(event.event)
        timeline_items.append(
            {
                "created_at": event.created_at,
                "label": label,
                "icon": _timeline_event_icon(label),
                "detail": (event.detail or "").strip() or "Actualización registrada automáticamente.",
            }
        )
    timeline_items.sort(key=lambda item: item.get("created_at") or datetime.min, reverse=True)

    checkout_preview = session.pop("mp_checkout_preview", None)
    checkout_status = (request.args.get("checkout") or "").strip().lower()
    selected_plan_id = request.args.get("selected_plan_id", type=int)
    selected_plan = next((p for p in plans if p.id == selected_plan_id), None)
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
        effective_state=effective_state,
        plan_features=plan_features,
        managed_by_seller=managed_by_seller,
        mp_config=load_billing_config(),
        next_amount=next_amount,
        currency=currency,
        frequency_label=frequency_label,
        payment_method_label=payment_method_label,
        payment_rows=payment_rows,
        invoice_rows=invoice_rows,
        timeline_items=timeline_items,
        selected_plan=selected_plan,
    )


@bp.route("/subscription/invoices/<int:invoice_id>")
@company_member_required
def subscription_invoice_detail(invoice_id):
    from app import Company, Invoice

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    invoice = Invoice.query.filter_by(id=invoice_id, company_id=company.id).first_or_404()
    return render_template("company_billing/subscription_invoice_detail.html", company=company, invoice=invoice)


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
    from app import Company

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()

    plan_id = request.form.get("plan_id", type=int)
    plan = PlanService.get_plan(plan_id=plan_id)
    if plan is None:
        flash("Plan no encontrado.", "danger")
        return redirect(url_for("company_billing.subscription_portal"))
    _ = company  # explicitamente mantenemos validación de empresa/tenant antes del redirect
    return redirect(url_for("company_billing.subscription_portal", selected_plan_id=plan.id))


@bp.route("/subscription/change", methods=["POST"])
@company_member_required
def subscription_change_confirm():
    from flask import session

    from app import Company, Subscription, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()

    plan_id = request.form.get("plan_id", type=int)
    plan = PlanService.get_plan(plan_id=plan_id)
    if plan is None:
        flash("Plan no encontrado.", "danger")
        return redirect(url_for("company_billing.subscription_portal"))

    try:
        with db.session.begin_nested():
            current_subscription = SubscriptionService.active_subscription_for_company(company.id)
            command_result = SubscriptionService.run_command(
                db.session,
                SubscriptionService.ChangePlanCommand(
                    company_id=company.id,
                    plan_id=plan.id,
                    actor_user_id=current_user.id,
                    actor_role=getattr(current_user, "role", None),
                    origin="portal_confirm",
                    ip_address=request.remote_addr,
                    idempotency_key=(
                        request.form.get("idempotency_key")
                        or f"portal-change:{company.id}:{getattr(current_subscription, 'id', 0)}:{plan.id}:{current_user.id}"
                    ),
                ),
            )
            subscription = Subscription.query.filter_by(id=command_result.subscription_id).first()
            if subscription is None:
                raise RuntimeError("No se pudo recuperar la suscripción creada por el comando.")

            if float(plan.price or 0) <= 0:
                ReferralService.create_commission_for_sale(
                    db.session,
                    company_id=company.id,
                    subscription=subscription,
                    payment=None,
                    plan=plan,
                )
            else:
                payload = BillingService().create_checkout_for_plan(
                    db_session=db.session,
                    company=company,
                    plan=plan,
                    user=current_user,
                    subscription=subscription,
                )
                preference = payload["preference"]
                checkout_url = preference.get("init_point") or preference.get("sandbox_init_point")
                if not checkout_url:
                    raise RuntimeError("Mercado Pago no devolvió URL de checkout.")
                session["mp_checkout_preview"] = BillingService.checkout_preview_payload(
                    preference=preference,
                    plan=plan,
                    company=company,
                )

            record_audit(
                action="subscription_change_confirmed",
                entity="subscription",
                entity_id=subscription.id,
                detail=f"Cambio confirmado a plan {plan.code or plan.name}",
            )

        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "warning")
        return redirect(url_for("company_billing.subscription_portal"))
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("Error confirmando cambio de plan: %s", exc)
        flash("No se pudo confirmar el cambio de plan. No se aplicaron cambios.", "danger")
        return redirect(url_for("company_billing.subscription_portal"))

    if float(plan.price or 0) <= 0:
        flash("Plan actualizado correctamente.", "success")
        return redirect(url_for("company_billing.subscription_portal"))

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
    redirect_uri = service.oauth_redirect_uri(service.default_oauth_redirect_uri())
    session[f"mp_oauth_redirect_uri_{company.id}"] = redirect_uri
    session.modified = True
    auth_url = service.build_authorization_url(state=state, redirect_uri=redirect_uri)
    current_app.logger.info(
        "Mercado Pago OAuth start: company_id=%s client_id=%s redirect_uri=%s state=%s auth_url=%s",
        company.id,
        service._client_id(),
        redirect_uri,
        state,
        auth_url,
    )
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
        error_description = request.args.get("error_description") or ""
        current_app.logger.error(
            "Mercado Pago OAuth callback error: company_id=%s error=%s error_description=%s args=%s",
            company.id,
            error,
            error_description,
            dict(request.args),
        )
        flash(f"Mercado Pago rechazó la conexión: {error}", "danger")
        return redirect(url_for("company_billing.company_settings", panel="mercado-pago"))

    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()
    session_key = f"mp_oauth_state_{company.id}"
    expected_state = session.get(session_key)
    redirect_uri_session_key = f"mp_oauth_redirect_uri_{company.id}"
    redirect_uri = session.get(redirect_uri_session_key) or service.oauth_redirect_uri(service.default_oauth_redirect_uri())
    current_app.logger.info(
        "Mercado Pago OAuth callback received: company_id=%s code_present=%s state=%s expected_state=%s redirect_uri=%s args=%s",
        company.id,
        bool(code),
        state,
        expected_state,
        redirect_uri,
        dict(request.args),
    )
    if not code or not state or not expected_state or state != expected_state:
        current_app.logger.warning(
            "Mercado Pago OAuth state validation failed: company_id=%s code_present=%s received_state=%s expected_state=%s session_key=%s",
            company.id,
            bool(code),
            state,
            expected_state,
            session_key,
        )
        flash("La devolución OAuth no pudo validarse.", "danger")
        return redirect(url_for("company_billing.company_settings", panel="mercado-pago"))

    session.pop(session_key, None)
    session.pop(redirect_uri_session_key, None)
    try:
        token_payload = service.exchange_code(code=code, redirect_uri=redirect_uri)
        access_token = token_payload.get("access_token") or ""
        if not access_token:
            raise RuntimeError("Mercado Pago no devolvió access_token")
        profile = service.fetch_user_profile(access_token=access_token)
        connection = service.save_connection(company_id=company.id, token_payload=token_payload, profile=profile)
        current_app.logger.info(
            "Mercado Pago OAuth connected: company_id=%s mp_user_id=%s account_email=%s country=%s",
            company.id,
            connection.mp_user_id,
            connection.account_email,
            connection.country,
        )
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
        flash("Debes completar empresa/negocio y email.", "danger")
        return redirect(url_for("company_billing.company_settings"))
    if User.query.filter_by(username=username).first() is not None:
        flash("Esa empresa/negocio ya existe.", "danger")
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
    new_password = (request.form.get("new_password") or "").strip()
    confirm_password = (request.form.get("confirm_password") or "").strip()
    force_change = (request.form.get("force_change") or "").strip().lower() in {"1", "true", "yes", "on"}

    if new_password or confirm_password:
        if len(new_password) < 6:
            flash("La nueva contrasena debe tener al menos 6 caracteres.", "danger")
            return redirect(url_for("company_billing.company_settings", panel="employees"))
        if new_password != confirm_password:
            flash("Las contrasenas no coinciden.", "danger")
            return redirect(url_for("company_billing.company_settings", panel="employees"))
        user.set_password(new_password)
        user.must_change_password = force_change
        record_audit(
            action="company_user_set_password",
            entity="user",
            entity_id=user.id,
            detail=f"Contrasena configurada manualmente para {user.username}. force_change={force_change}",
        )
        db.session.commit()
        flash(f"Contrasena actualizada correctamente para {user.username}.", "success")
        return redirect(url_for("company_billing.company_settings", panel="employees"))

    temp_password = _temporary_password()
    user.set_password(temp_password)
    user.must_change_password = True
    record_audit(action="company_user_reset_password", entity="user", entity_id=user.id, detail=f"Contrasena restablecida para {user.username}")
    db.session.commit()
    flash(f"Contrasena restablecida. Temporal para {user.username}: {temp_password}", "success")
    return redirect(url_for("company_billing.company_settings", panel="employees"))


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
        "quick_mode_default": bool(request.form.get("quick_mode_default")),
        "quotes_module_enabled": bool(request.form.get("quotes_module_enabled")),
    }
    try:
        printer_port = int(request.form.get("printer_port") or 9100)
    except (TypeError, ValueError):
        printer_port = 9100

    printer_settings = {
        "printer_name": (request.form.get("printer_name") or "").strip()[:160],
        "ticket_name": (request.form.get("ticket_name") or "").strip()[:120],
        "paper_size": (request.form.get("paper_size") or "A4").strip()[:20] or "A4",
        "printer_type": (request.form.get("printer_type") or "browser").strip()[:20] or "browser",
        "printer_host": (request.form.get("printer_host") or "").strip()[:120],
        "printer_port": printer_port,
        "cashdrawer_enabled": bool(request.form.get("cashdrawer_enabled")),
    }

    company.preferences_json = json.dumps(preferences)
    company.printer_settings_json = json.dumps(printer_settings)
    record_audit(action="company_general_settings", entity="company", entity_id=company.id, detail="Configuracion general actualizada")
    db.session.commit()
    flash("Configuración general guardada correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="general"))


@bp.route("/company-settings/billing", methods=["POST"])
@company_admin_required
def company_settings_billing_save():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    company.tax_id = (request.form.get("tax_id") or "").strip()[:50] or None
    company.legal_name = (request.form.get("legal_name") or "").strip()[:160] or None

    company_preferences = _json_company_dict(company.preferences_json)
    billing = _load_company_billing_config(company, company_preferences)

    fiscal = billing.get("fiscal") or {}
    fiscal["tax_id"] = (request.form.get("tax_id") or "").strip()[:50]
    fiscal["legal_name"] = (request.form.get("legal_name") or "").strip()[:160]
    fiscal["iva_condition"] = (request.form.get("iva_condition") or "Consumidor Final").strip()[:80]
    fiscal["activity_start"] = (request.form.get("activity_start") or "").strip()[:20]
    fiscal["tax_profile"] = (request.form.get("tax_profile") or "Monotributo").strip()[:60]
    fiscal["gross_income"] = (request.form.get("gross_income") or "").strip()[:80]
    fiscal["jurisdiction"] = (request.form.get("jurisdiction") or "").strip()[:120]
    billing["fiscal"] = fiscal

    documents_enabled = billing.get("documents_enabled") or {}
    for key, _ in BILLING_DOCUMENT_TYPES:
        documents_enabled[key] = bool(request.form.get(f"doc_{key}"))
    billing["documents_enabled"] = documents_enabled

    points = billing.get("points_of_sale") or []
    active_pos = _normalize_pos_number(request.form.get("active_pos") or billing.get("active_pos") or "00001")
    for row in points:
        row["active"] = row.get("number") == active_pos
    if not any(row.get("number") == active_pos for row in points):
        points.append({"number": active_pos, "description": f"Punto de venta {active_pos}", "active": True})
    billing["points_of_sale"] = points
    billing["active_pos"] = active_pos

    numbering = billing.get("numbering") or {}
    for key, _ in BILLING_DOCUMENT_TYPES:
        raw = (request.form.get(f"numbering_{key}") or "").strip()[:20]
        if raw:
            numbering[key] = raw
    billing["numbering"] = numbering

    print_template = billing.get("print_template") or {}
    print_template["logo"] = (request.form.get("tpl_logo") or company.logo or "").strip()[:255]
    print_template["footer"] = (request.form.get("tpl_footer") or "").strip()[:400]
    print_template["commercial_terms"] = (request.form.get("tpl_commercial_terms") or "").strip()[:600]
    print_template["observations"] = (request.form.get("tpl_observations") or "").strip()[:600]
    print_template["show_qr"] = bool(request.form.get("tpl_show_qr"))
    print_template["show_barcode"] = bool(request.form.get("tpl_show_barcode"))
    print_template["format_a4"] = bool(request.form.get("tpl_format_a4"))
    print_template["format_ticket_58"] = bool(request.form.get("tpl_format_ticket_58"))
    print_template["format_ticket_80"] = bool(request.form.get("tpl_format_ticket_80"))
    billing["print_template"] = print_template

    electronic = billing.get("electronic") or {}
    electronic["environment"] = (request.form.get("electronic_environment") or "homologacion").strip().lower()[:30]
    billing["electronic"] = electronic

    company_preferences["billing_business"] = billing
    company.preferences_json = json.dumps(company_preferences)

    record_audit(action="company_billing_business_update", entity="company", entity_id=company.id, detail="Configuración de facturación del negocio actualizada")
    db.session.commit()
    flash("Facturación del negocio guardada correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="billing"))


@bp.route("/company-settings/billing/pos/add", methods=["POST"])
@company_admin_required
def company_settings_billing_pos_add():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    company_preferences = _json_company_dict(company.preferences_json)
    billing = _load_company_billing_config(company, company_preferences)

    number = _normalize_pos_number(request.form.get("pos_number") or "")
    description = (request.form.get("pos_description") or "").strip()[:80] or f"Punto de venta {number}"

    points = billing.get("points_of_sale") or []
    if any(row.get("number") == number for row in points):
        flash("Ese punto de venta ya existe.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="billing"))

    points.append({"number": number, "description": description, "active": False})
    billing["points_of_sale"] = sorted(points, key=lambda item: item.get("number") or "00000")

    company_preferences["billing_business"] = billing
    company.preferences_json = json.dumps(company_preferences)
    record_audit(action="company_billing_pos_add", entity="company", entity_id=company.id, detail=f"Punto de venta agregado {number}")
    db.session.commit()
    flash("Punto de venta agregado correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="billing"))


@bp.route("/company-settings/billing/numbering/reset", methods=["POST"])
@company_admin_required
def company_settings_billing_numbering_reset():
    from app import db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = _load_company(company_id)
    blocked = _pin_guard(company)
    if blocked is not None:
        return blocked

    if getattr(current_user, "role", None) != "admin":
        flash("Solo el administrador puede reiniciar la numeración.", "warning")
        return redirect(url_for("company_billing.company_settings", panel="billing"))

    company_preferences = _json_company_dict(company.preferences_json)
    billing = _load_company_billing_config(company, company_preferences)
    active_pos = billing.get("active_pos") or "00001"
    numbering = billing.get("numbering") or {}
    for key, _ in BILLING_DOCUMENT_TYPES:
        numbering[key] = f"{active_pos}-00000001"
    billing["numbering"] = numbering
    company_preferences["billing_business"] = billing
    company.preferences_json = json.dumps(company_preferences)

    record_audit(action="company_billing_numbering_reset", entity="company", entity_id=company.id, detail="Numeración de comprobantes reiniciada")
    db.session.commit()
    flash("Numeración reiniciada correctamente.", "success")
    return redirect(url_for("company_billing.company_settings", panel="billing"))


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

    from app import AuditLog, CashSession, Quote, Sale, SaleItem, User

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
    company_preferences = _json_company_dict(company.preferences_json)
    billing_config = _load_company_billing_config(company, company_preferences)
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
    company_dashboard = {}
    billing_status = _business_billing_status(billing_config)
    billing_document_labels = {key: label for key, label in BILLING_DOCUMENT_TYPES}
    billing_enabled_documents = [
        billing_document_labels[key]
        for key, _label in BILLING_DOCUMENT_TYPES
        if (billing_config.get("documents_enabled") or {}).get(key)
    ]
    billing_last_receipt = None
    billing_history_rows = []
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

        if not settings_panel:
            from services.dashboard_service import build_dashboard_context

            company_dashboard = build_dashboard_context()
        if settings_panel == "billing":
            recent_sales_docs = (
                Sale.query.filter_by(company_id=company.id)
                .filter((Sale.requiere_comprobante.is_(True)) | (Sale.comprobante_emitido.is_(True)))
                .order_by(Sale.date.desc(), Sale.id.desc())
                .limit(50)
                .all()
            )
            recent_quotes_docs = (
                Quote.query.options(selectinload(Quote.client))
                .filter_by(company_id=company.id)
                .order_by(Quote.date.desc(), Quote.id.desc())
                .limit(30)
                .all()
            )

            for sale in recent_sales_docs:
                doc_label = _format_document_type_label(sale.tipo_comprobante or "factura_b")
                number = f"{billing_config.get('active_pos', '00001')}-{sale.id:08d}"
                row = {
                    "origin": "sale",
                    "date": sale.date,
                    "number": number,
                    "client": sale.customer or "Consumidor final",
                    "doc_type": doc_label,
                    "amount": float(sale.total_amount or 0),
                    "status": _sale_business_status_badge(sale),
                    "sale_id": sale.id,
                    "quote_id": None,
                    "can_email": False,
                    "can_duplicate": False,
                    "can_annul": False,
                }
                billing_history_rows.append(row)

            for quote in recent_quotes_docs:
                quote_status = (quote.status or "BORRADOR").strip().upper()
                if quote_status in {"APROBADO", "CONVERTIDO"}:
                    badge = {"label": "Emitido", "class": "text-bg-success"}
                elif quote_status in {"ANULADO", "RECHAZADO", "VENCIDO"}:
                    badge = {"label": "Anulado", "class": "text-bg-danger"}
                elif quote_status in {"PENDIENTE", "ENVIADO"}:
                    badge = {"label": "Pendiente", "class": "text-bg-warning"}
                else:
                    badge = {"label": "En proceso", "class": "text-bg-primary"}
                billing_history_rows.append(
                    {
                        "origin": "quote",
                        "date": quote.date,
                        "number": quote.number or f"P-{quote.id:06d}",
                        "client": quote.client.name if quote.client else (quote.consumer_name or "Consumidor final"),
                        "doc_type": "Presupuesto",
                        "amount": float(quote.total_amount or 0),
                        "status": badge,
                        "sale_id": None,
                        "quote_id": quote.id,
                        "can_email": True,
                        "can_duplicate": True,
                        "can_annul": quote_status != "CONVERTIDO",
                    }
                )

            billing_history_rows.sort(key=lambda item: item.get("date") or datetime.min, reverse=True)
            billing_last_receipt = next((item for item in billing_history_rows if item.get("status", {}).get("label") == "Emitido"), None)
            if billing_last_receipt is None:
                billing_last_receipt = billing_history_rows[0] if billing_history_rows else None

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
        company_dashboard=company_dashboard,
        billing_config=billing_config,
        billing_status=billing_status,
        billing_document_types=BILLING_DOCUMENT_TYPES,
        billing_enabled_documents=billing_enabled_documents,
        billing_last_receipt=billing_last_receipt,
        billing_history_rows=billing_history_rows,
    )


def _billing_business_config(company):
    return BusinessBillingService.load_config(company)


def _billing_collect_filters():
    return BusinessBillingService.parse_filter_values(request.args)


@bp.route("/facturacion")
@business_billing_view_required
def business_billing_hub():
    from app import Company, User

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)
    filters = _billing_collect_filters()
    rows = BusinessBillingService.list_documents(company.id, config, filters)
    dashboard = BusinessBillingService.build_dashboard(company.id, config)
    reports = BusinessBillingService.report_summary(rows)

    users = (
        User.query.filter_by(company_id=company.id, active=True)
        .order_by(User.username.asc())
        .all()
    )
    selected_tab = (request.args.get("tab") or "dashboard").strip().lower()
    if selected_tab not in {
        "dashboard",
        "fiscal",
        "comprobantes",
        "numeracion",
        "puntos-venta",
        "plantillas",
        "emision",
        "electronica",
        "reportes",
    }:
        selected_tab = "dashboard"

    return render_template(
        "company_billing/business_billing.html",
        company=company,
        config=config,
        dashboard=dashboard,
        rows=rows,
        reports=reports,
        filters=filters,
        users=users,
        selected_tab=selected_tab,
        document_types=BusinessBillingService.DOCUMENT_TYPES,
        is_admin=((getattr(current_user, "role", None) or "").strip().lower() == "admin"),
        has_view_access=_can_view_business_billing(current_user),
        next_number_preview=BusinessBillingService.next_number_preview,
    )


@bp.route("/facturacion/fiscal", methods=["POST"])
@business_billing_admin_required
def business_billing_save_fiscal():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)

    fiscal = config.get("fiscal") or {}
    fiscal["tax_id"] = (request.form.get("tax_id") or "").strip()[:50]
    fiscal["legal_name"] = (request.form.get("legal_name") or "").strip()[:160]
    fiscal["iva_condition"] = (request.form.get("iva_condition") or "").strip()[:80]
    fiscal["gross_income"] = (request.form.get("gross_income") or "").strip()[:80]
    fiscal["jurisdiction"] = (request.form.get("jurisdiction") or "").strip()[:120]
    fiscal["activity_start"] = (request.form.get("activity_start") or "").strip()[:20]
    fiscal["fiscal_address"] = (request.form.get("fiscal_address") or "").strip()[:255]
    fiscal["branch_name"] = (request.form.get("branch_name") or "").strip()[:80] or "Casa central"
    config["fiscal"] = fiscal

    company.tax_id = fiscal["tax_id"] or None
    company.legal_name = fiscal["legal_name"] or None
    if fiscal["fiscal_address"]:
        company.address = fiscal["fiscal_address"]

    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_fiscal_update",
        entity="company",
        entity_id=company.id,
        detail="Configuracion fiscal del negocio actualizada.",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Configuración fiscal actualizada.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="fiscal"))


@bp.route("/facturacion/documentos", methods=["POST"])
@business_billing_admin_required
def business_billing_save_documents():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)

    docs = config.get("documents_enabled") or {}
    for key, _label in BusinessBillingService.DOCUMENT_TYPES:
        docs[key] = bool(request.form.get(f"doc_{key}"))
    config["documents_enabled"] = docs

    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_documents_update",
        entity="company",
        entity_id=company.id,
        detail="Tipos de comprobantes actualizados.",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Tipos de comprobantes guardados.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="comprobantes"))


@bp.route("/facturacion/numeracion", methods=["POST"])
@business_billing_admin_required
def business_billing_save_numbering():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)

    active_pos = BusinessBillingService._normalize_pos_number(request.form.get("active_pos") or config.get("active_pos"))
    config["active_pos"] = active_pos
    points = config.get("points_of_sale") or []
    for point in points:
        point["active"] = point.get("number") == active_pos
    if not any(point.get("number") == active_pos for point in points):
        points.append({
            "number": active_pos,
            "description": f"Punto de venta {active_pos}",
            "branch": (config.get("fiscal") or {}).get("branch_name") or "Casa central",
            "active": True,
        })
    config["points_of_sale"] = points

    numbering = config.get("numbering") or {}
    for key, _label in BusinessBillingService.DOCUMENT_TYPES:
        current = (request.form.get(f"numbering_{key}") or "").strip()[:20]
        if current:
            numbering[key] = current
    config["numbering"] = numbering

    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_numbering_update",
        entity="company",
        entity_id=company.id,
        detail="Numeracion de comprobantes actualizada.",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Numeración actualizada.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="numeracion"))


@bp.route("/facturacion/numeracion/reset", methods=["POST"])
@business_billing_admin_required
def business_billing_reset_numbering():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)
    active_pos = config.get("active_pos") or "00001"

    target_doc_type = (request.form.get("doc_type") or "").strip().lower()
    numbering = config.get("numbering") or {}
    all_types = {key for key, _label in BusinessBillingService.DOCUMENT_TYPES}
    if target_doc_type and target_doc_type in all_types:
        numbering[target_doc_type] = f"{active_pos}-00000001"
        detail = f"Numeracion reiniciada para {target_doc_type}."
    else:
        for key in all_types:
            numbering[key] = f"{active_pos}-00000001"
        detail = "Numeracion reiniciada para todos los comprobantes."
    config["numbering"] = numbering

    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_numbering_reset",
        entity="company",
        entity_id=company.id,
        detail=detail,
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Numeración reiniciada correctamente.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="numeracion"))


@bp.route("/facturacion/puntos-venta/add", methods=["POST"])
@business_billing_admin_required
def business_billing_pos_add():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)
    points = config.get("points_of_sale") or []

    number = BusinessBillingService._normalize_pos_number(request.form.get("number") or "")
    if any(item.get("number") == number for item in points):
        flash("Ese punto de venta ya existe.", "warning")
        return redirect(url_for("company_billing.business_billing_hub", tab="puntos-venta"))

    points.append(
        {
            "number": number,
            "description": (request.form.get("description") or "").strip()[:80] or f"Punto de venta {number}",
            "branch": (request.form.get("branch") or "").strip()[:80] or "Casa central",
            "active": False,
        }
    )
    points.sort(key=lambda item: item.get("number") or "00000")
    config["points_of_sale"] = points

    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_pos_add",
        entity="company",
        entity_id=company.id,
        detail=f"Punto de venta agregado {number}.",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Punto de venta agregado.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="puntos-venta"))


@bp.route("/facturacion/puntos-venta/update", methods=["POST"])
@business_billing_admin_required
def business_billing_pos_update():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)
    points = config.get("points_of_sale") or []

    number = BusinessBillingService._normalize_pos_number(request.form.get("number") or "")
    target = next((item for item in points if item.get("number") == number), None)
    if target is None:
        flash("No se encontró el punto de venta.", "warning")
        return redirect(url_for("company_billing.business_billing_hub", tab="puntos-venta"))

    target["description"] = (request.form.get("description") or target.get("description") or "").strip()[:80]
    target["branch"] = (request.form.get("branch") or target.get("branch") or "Casa central").strip()[:80]
    target["active"] = bool(request.form.get("active"))
    if target["active"]:
        config["active_pos"] = number
        for item in points:
            item["active"] = item.get("number") == number

    config["points_of_sale"] = points
    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_pos_update",
        entity="company",
        entity_id=company.id,
        detail=f"Punto de venta actualizado {number}.",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Punto de venta actualizado.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="puntos-venta"))


@bp.route("/facturacion/puntos-venta/toggle", methods=["POST"])
@business_billing_admin_required
def business_billing_pos_toggle():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)
    points = config.get("points_of_sale") or []

    number = BusinessBillingService._normalize_pos_number(request.form.get("number") or "")
    target = next((item for item in points if item.get("number") == number), None)
    if target is None:
        flash("No se encontró el punto de venta.", "warning")
        return redirect(url_for("company_billing.business_billing_hub", tab="puntos-venta"))

    target["active"] = not bool(target.get("active"))
    if target["active"]:
        config["active_pos"] = number
        for item in points:
            item["active"] = item.get("number") == number
    elif config.get("active_pos") == number:
        target["active"] = True
        flash("Debe quedar al menos un punto de venta activo.", "warning")
        return redirect(url_for("company_billing.business_billing_hub", tab="puntos-venta"))

    config["points_of_sale"] = points
    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_pos_toggle",
        entity="company",
        entity_id=company.id,
        detail=f"Punto de venta {'activado' if target['active'] else 'desactivado'} {number}.",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Estado del punto de venta actualizado.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="puntos-venta"))


@bp.route("/facturacion/plantillas", methods=["POST"])
@business_billing_admin_required
def business_billing_save_templates():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)

    tpl = config.get("template") or {}
    tpl["logo"] = (request.form.get("logo") or "").strip()[:255]
    tpl["footer"] = (request.form.get("footer") or "").strip()[:500]
    tpl["commercial_terms"] = (request.form.get("commercial_terms") or "").strip()[:1000]
    tpl["observations"] = (request.form.get("observations") or "").strip()[:1000]
    tpl["show_qr"] = bool(request.form.get("show_qr"))
    tpl["show_barcode"] = bool(request.form.get("show_barcode"))
    tpl["format_a4"] = bool(request.form.get("format_a4"))
    tpl["format_ticket_58"] = bool(request.form.get("format_ticket_58"))
    tpl["format_ticket_80"] = bool(request.form.get("format_ticket_80"))
    config["template"] = tpl

    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_template_update",
        entity="company",
        entity_id=company.id,
        detail="Plantillas de comprobante actualizadas.",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Plantilla guardada correctamente.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="plantillas"))


@bp.route("/facturacion/emision", methods=["POST"])
@business_billing_admin_required
def business_billing_save_emission():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)

    emission = config.get("emission") or {}
    emission["auto_numbering"] = bool(request.form.get("auto_numbering"))
    emission["auto_print"] = bool(request.form.get("auto_print"))
    emission["send_pdf_email"] = bool(request.form.get("send_pdf_email"))
    emission["send_whatsapp_prepared"] = bool(request.form.get("send_whatsapp_prepared"))
    emission["copies"] = max(1, int(request.form.get("copies", 1) or 1))
    emission["currency"] = (request.form.get("currency") or "ARS").strip()[:10]
    emission["decimals"] = max(0, min(4, int(request.form.get("decimals", 2) or 2)))
    emission["default_format"] = (request.form.get("default_format") or "a4").strip()[:20]
    emission["default_template"] = (request.form.get("default_template") or "estandar").strip()[:60]
    config["emission"] = emission

    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_emission_update",
        entity="company",
        entity_id=company.id,
        detail="Configuracion de emision actualizada.",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Configuración de emisión actualizada.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="emision"))


@bp.route("/facturacion/electronica", methods=["POST"])
@business_billing_admin_required
def business_billing_save_electronic():
    from app import Company, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)

    electronic = config.get("electronic") or {}
    electronic["environment"] = (request.form.get("environment") or "homologacion").strip().lower()[:20]
    electronic["certificate"] = (request.form.get("certificate") or "").strip()[:160]
    electronic["certificate_expires_at"] = (request.form.get("certificate_expires_at") or "").strip()[:20]
    electronic["cae"] = (request.form.get("cae") or "").strip()[:60]
    electronic["caea"] = (request.form.get("caea") or "").strip()[:60]
    raw_enabled_points = (request.form.get("enabled_points") or "").strip()
    electronic["enabled_points"] = [
        point.strip() for point in raw_enabled_points.split(",") if point.strip()
    ]
    electronic["status"] = "coming_soon"
    electronic["connected"] = False
    config["electronic"] = electronic

    BusinessBillingService.save_config(company, config)
    record_audit(
        action="business_billing_electronic_update",
        entity="company",
        entity_id=company.id,
        detail="Configuracion de facturacion electronica actualizada (modo preparado).",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Configuración electrónica guardada. Integración ARCA próximamente disponible.", "info")
    return redirect(url_for("company_billing.business_billing_hub", tab="electronica"))


@bp.route("/facturacion/reportes/export")
@business_billing_view_required
def business_billing_export_reports():
    from app import Company

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    config = _billing_business_config(company)
    filters = _billing_collect_filters()
    rows = BusinessBillingService.list_documents(company.id, config, filters)
    export_format = (request.args.get("format") or "csv").strip().lower()
    base_name = f"comprobantes_{company.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if export_format == "excel":
        buffer = BusinessBillingService.export_excel(rows)
        return send_file(
            buffer,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"{base_name}.xlsx",
        )
    if export_format == "pdf":
        buffer = BusinessBillingService.export_pdf(rows)
        return send_file(
            buffer,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{base_name}.pdf",
        )

    buffer = BusinessBillingService.export_csv(rows)
    return send_file(
        buffer,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{base_name}.csv",
    )


@bp.route("/facturacion/comprobantes/venta/<int:sale_id>/anular", methods=["POST"])
@business_billing_admin_required
def business_billing_annul_sale(sale_id):
    from app import Company, Sale, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    sale = Sale.query.filter_by(id=sale_id, company_id=company_id).first_or_404()

    if (sale.status or "").strip().lower() in {"anulada", "cancelada", "rechazada"}:
        flash("La venta ya estaba anulada.", "warning")
        return redirect(url_for("company_billing.business_billing_hub", tab="dashboard"))

    sale.status = "anulada"
    BusinessBillingService.mark_sale_document_annulled(db.session, company_id=company.id, sale_id=sale.id)
    record_audit(
        action="business_billing_sale_annul",
        entity="sale",
        entity_id=sale.id,
        detail=f"Comprobante de venta anulado desde Facturacion. venta_id={sale.id}",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Comprobante anulado correctamente.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="dashboard"))


@bp.route("/facturacion/comprobantes/venta/<int:sale_id>/emitir", methods=["POST"])
@business_billing_admin_required
def business_billing_issue_sale_document(sale_id):
    from app import Company, Sale, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()
    sale = Sale.query.filter_by(id=sale_id, company_id=company.id).first_or_404()

    config = _billing_business_config(company)
    doc_type = ((getattr(sale, "tipo_comprobante", None) or "factura_b") or "factura_b").strip().lower()
    enabled = bool((config.get("documents_enabled") or {}).get(doc_type, False))
    if not enabled:
        flash("El tipo de comprobante no está habilitado en configuración.", "warning")
        return redirect(url_for("company_billing.business_billing_hub", tab="dashboard"))

    try:
        BusinessBillingService.issue_sale_document(
            db.session,
            company=company,
            sale=sale,
            config=config,
            emitted_by_user_id=current_user.id,
            metadata={"origin": "business_billing_issue_sale_document", "ip": request.remote_addr},
        )
        BusinessBillingService.save_config(company, config)
        record_audit(
            action="business_billing_sale_issue",
            entity="sale",
            entity_id=sale.id,
            detail=f"Comprobante emitido para venta {sale.id}.",
            ip_address=request.remote_addr,
        )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("No se pudo emitir el comprobante por conflicto de numeración. Reintenta.", "danger")
        return redirect(url_for("company_billing.business_billing_hub", tab="dashboard"))

    flash("Comprobante emitido correctamente.", "success")
    return redirect(url_for("company_billing.business_billing_hub", tab="dashboard"))


@bp.route("/facturacion/comprobantes/venta/<int:sale_id>/email", methods=["POST"])
@business_billing_view_required
def business_billing_sale_email_ready(sale_id):
    from app import Sale, db, record_audit

    company_id = getattr(current_user, "company_id", None)
    sale = Sale.query.filter_by(id=sale_id, company_id=company_id).first_or_404()

    client_email = getattr(getattr(sale, "client", None), "email", None)
    if not client_email:
        flash("La venta no tiene cliente con email registrado.", "warning")
        return redirect(url_for("company_billing.business_billing_hub", tab="dashboard"))

    record_audit(
        action="business_billing_sale_email_ready",
        entity="sale",
        entity_id=sale.id,
        detail=f"Envio por email preparado para venta {sale.id}.",
        ip_address=request.remote_addr,
    )
    db.session.commit()
    flash("Estructura de envío por email preparada para este comprobante.", "info")
    return redirect(url_for("company_billing.business_billing_hub", tab="dashboard"))


@bp.route("/facturacion/comprobantes/venta/<int:sale_id>/whatsapp", methods=["POST"])
@business_billing_view_required
def business_billing_sale_whatsapp(sale_id):
    return redirect(url_for("sales.share_whatsapp", sale_id=sale_id))


@bp.route("/facturacion/comprobantes/presupuesto/<int:quote_id>/whatsapp", methods=["POST"])
@business_billing_view_required
def business_billing_quote_whatsapp(quote_id):
    return redirect(url_for("quotes.share_whatsapp", quote_id=quote_id))


@bp.route("/facturacion/comprobantes/presupuesto/<int:quote_id>/email", methods=["POST"])
@business_billing_view_required
def business_billing_quote_email(quote_id):
    return redirect(url_for("quotes.email_quote", quote_id=quote_id))


@bp.route("/webhooks/mercadopago", methods=["POST"])
@csrf.exempt
def webhook_mercadopago():
    from app import db, record_audit
    from app import WebhookEvent

    payload = request.get_json(silent=True) or {}
    try:
        result = WebhookService().process(db_session=db.session, headers=dict(request.headers), payload=payload)
        record_audit(action="webhook_mercadopago", entity="webhook", detail=f"Webhook procesado: {result.get('status')}")
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            event_key = result.get("event_key")
            if event_key and WebhookEvent.query.filter_by(event_key=event_key).first():
                current_app.logger.info("Webhook Mercado Pago duplicado ignorado: %s", event_key)
                return jsonify({"ok": True, "result": {"status": "duplicate", "event_key": event_key}}), 200
            raise
        return jsonify({"ok": True, "result": result}), 200
    except IntegrityError as exc:
        current_app.logger.exception("Webhook Mercado Pago rechazado por integridad: %s", exc)
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        current_app.logger.exception("Webhook Mercado Pago rechazado: %s", exc)
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400

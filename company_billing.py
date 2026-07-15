"""Portal de suscripcion para empresas (tenant) y webhook publico de Mercado Pago."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
import secrets
import string

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import case, func

from app import company_admin_required, csrf, tenant_required
from config.billing_config import load_billing_config
from services.billing_service import BillingService
from services.company_security_service import CompanySecurityService
from services.plan_service import PlanService
from services.plan_usage_service import PlanUsageService
from services.referral_service import ReferralService
from services.subscription_service import SubscriptionService
from services.webhook_service import WebhookService

bp = Blueprint("company_billing", __name__)


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


def _plan_limit_context(company_id):
    usage_snapshot = PlanUsageService.usage_snapshot(company_id)
    users_metric = next(
        (item for item in usage_snapshot["metrics"] if item.key == PlanUsageService.RESOURCE_USERS),
        None,
    )
    return usage_snapshot, users_metric


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


def _pin_guard(company):
    if _is_pin_verified(company.id):
        return None
    flash("Debes validar PIN para gestionar Mi Empresa.", "warning")
    return redirect(url_for("company_billing.company_settings"))


def _build_user_and_cash_rows(company_id, date_from=None, date_to=None):
    from app import CashMovement, Sale, User, db

    users = (
        User.query.filter_by(company_id=company_id)
        .order_by(User.created_at.asc(), User.id.asc())
        .all()
    )

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
            }
        )
    result_rows.sort(key=lambda item: (-item["total_sold"], item["user"].created_at or datetime.min, item["user"].id))
    for index, row in enumerate(result_rows, start=1):
        row["rank"] = index
    return users, result_rows


@bp.route("/portal")
@tenant_required
def subscription_portal():
    from app import Company, db

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
    return render_template(
        "company_billing/portal.html",
        company=company,
        plans=plans,
        subscription=subscription,
        usage_snapshot=usage_snapshot,
        mp_config=load_billing_config(),
    )


@bp.route("/checkout", methods=["POST"])
@tenant_required
def create_checkout():
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
    return redirect(checkout_url)


@bp.route("/subscription/cancel", methods=["POST"])
@tenant_required
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
@tenant_required
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

    company.name = (request.form.get("name") or company.name or "").strip()[:160] or company.name
    company.legal_name = (request.form.get("legal_name") or "").strip()[:160] or None
    company.address = (request.form.get("address") or "").strip()[:255] or None
    company.phone = (request.form.get("phone") or "").strip()[:40] or None
    company.contact_email = (request.form.get("contact_email") or "").strip()[:160] or None
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
    company_id = getattr(current_user, "company_id", None)
    _mark_pin_verified(company_id, False)
    flash("Se cerro la sesion de seguridad de Mi Empresa.", "info")
    return redirect(url_for("company_billing.company_settings"))


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


@bp.route("/company-settings")
@company_member_required
def company_settings():
    from flask import session
    from sqlalchemy.orm import selectinload

    from app import Sale, SaleItem

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

    users = []
    cash_rows = []
    recent_sales = []
    usage_snapshot, users_metric = _plan_limit_context(company.id)
    subscription = usage_snapshot["subscription"]
    plan = usage_snapshot["plan"]
    pin_created_at, pin_last_used_at = _pin_metadata(company)
    pin_bootstrap_reveal = session.pop(_pin_reveal_session_key(company.id), None)
    cash_summary = {"total_sold": 0.0, "total_sales": 0, "average_ticket": 0.0}
    if pin_verified:
        users, cash_rows = _build_user_and_cash_rows(company.id, date_from=date_from, date_to=date_to)
        cash_summary = _cash_summary(cash_rows)
        recent_sales = (
            Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product))
            .filter_by(company_id=company.id)
            .order_by(Sale.date.desc())
            .limit(12)
            .all()
        )

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

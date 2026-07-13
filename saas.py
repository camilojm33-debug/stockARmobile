"""SaaS y billing: planes, suscripciones, checkout y webhooks Mercado Pago."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from io import BytesIO

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask import send_file
from flask_login import current_user, login_required
from openpyxl import Workbook
from sqlalchemy import text

from app import superadmin_required, utcnow
from config.billing_config import load_billing_config
from services.plan_service import PlanService

bp = Blueprint("saas", __name__)


def _require_superadmin():
    if current_user.role != "superadmin":
        abort(403)


def _redirect_back(default_endpoint: str = "saas.companies_panel"):
    next_url = (request.form.get("next") or request.args.get("next") or "").strip()
    if next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for(default_endpoint))


def _parse_dt(value: str | None):
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@bp.route("/", methods=["GET", "POST"])
@superadmin_required
def index():
    from app import AuditLog, BackupLog, Client, Company, Invoice, Payment, Plan, Product, Sale, Subscription, User, db

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
    }

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
        last_payments=last_payments,
        last_errors=last_errors,
    )


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

    return render_template(
        "saas/companies.html",
        companies=companies,
        pagination=pagination,
        user_counts=user_counts,
        product_counts=product_counts,
        client_counts=client_counts,
        sale_counts=sale_counts,
        latest_subscriptions=latest_subscriptions,
        plans=Plan.query.filter(Plan.active.is_(True)).order_by(Plan.price.asc()).all(),
        filters={"q": q, "status": status, "plan": plan_code, "per_page": per_page},
    )


@bp.route("/companies/<int:company_id>")
@superadmin_required
def company_detail(company_id):
    from app import AuditLog, Client, Company, Payment, Product, Sale, Subscription, User, db

    _require_superadmin()
    company = Company.query.filter_by(id=company_id).first_or_404()
    subscription = (
        Subscription.query.filter_by(company_id=company.id)
        .order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc())
        .first()
    )
    stats = {
        "users": User.query.filter_by(company_id=company.id).count(),
        "active_users": User.query.filter_by(company_id=company.id, active=True).count(),
        "products": Product.query.filter_by(company_id=company.id, active=True).count(),
        "clients": Client.query.filter_by(company_id=company.id, active=True).count(),
        "sales": Sale.query.filter_by(company_id=company.id).count(),
        "sales_amount": float(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)).filter(Sale.company_id == company.id).scalar() or 0),
        "payments_approved": float(db.session.query(db.func.coalesce(db.func.sum(Payment.amount), 0)).filter(Payment.company_id == company.id, Payment.status == "approved").scalar() or 0),
    }
    last_payments = Payment.query.filter_by(company_id=company.id).order_by(Payment.created_at.desc()).limit(10).all()
    audit = AuditLog.query.filter_by(company_id=company.id).order_by(AuditLog.created_at.desc()).limit(20).all()
    return render_template("saas/company_detail.html", company=company, subscription=subscription, stats=stats, last_payments=last_payments, audit=audit)


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


@bp.route("/companies/<int:company_id>/delete", methods=["POST"])
@superadmin_required
def company_delete(company_id):
    from app import AuditLog, Company, Subscription, User, db

    _require_superadmin()
    company = Company.query.filter_by(id=company_id).first_or_404()
    company.active = False
    User.query.filter_by(company_id=company.id).update({User.active: False}, synchronize_session=False)
    Subscription.query.filter_by(company_id=company.id).update(
        {
            Subscription.status: "cancelled",
            Subscription.renewal_enabled: False,
            Subscription.auto_renew: False,
        },
        synchronize_session=False,
    )
    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=company.id,
            action="company_soft_delete",
            entity="company",
            entity_id=company.id,
            detail=f"Empresa marcada como eliminada logicamente. ip={request.remote_addr or 'unknown'} resultado=ok",
        )
    )
    db.session.commit()
    flash("Empresa eliminada de forma lógica (desactivada).", "warning")
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
    from app import Company, Plan, Subscription

    _require_superadmin()
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").strip().lower()
    plan_code = (request.args.get("plan") or "all").strip().lower()
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
    if status != "all":
        query = query.filter(Subscription.status == status)
    if plan_code != "all":
        query = query.filter(Plan.code == plan_code)

    pagination = query.order_by(Subscription.start_date.desc().nullslast(), Subscription.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
    subscriptions = pagination.items
    companies = Company.query.order_by(Company.name.asc()).all()
    plans = Plan.query.filter(Plan.active.is_(True)).order_by(Plan.price.asc()).all()
    return render_template(
        "saas/subscriptions.html",
        subscriptions=subscriptions,
        pagination=pagination,
        companies=companies,
        plans=plans,
        filters={"q": q, "status": status, "plan": plan_code, "per_page": per_page},
        status_options=["trial", "pending", "active", "approved", "cancelled", "suspended", "expired", "rejected"],
    )


@bp.route("/subscriptions/create", methods=["POST"])
@superadmin_required
def subscriptions_create():
    from app import AuditLog, Company, Plan, Subscription

    _require_superadmin()
    company_id = request.form.get("company_id", type=int)
    plan_id = request.form.get("plan_id", type=int)
    status = (request.form.get("status") or "pending").strip().lower()
    start_date = _parse_dt(request.form.get("start_date")) or utcnow()
    next_billing_date = _parse_dt(request.form.get("next_billing_date"))
    renewal_enabled = (request.form.get("renewal_enabled") or "1") == "1"

    company = Company.query.filter_by(id=company_id).first()
    plan = Plan.query.filter_by(id=plan_id).first()
    if company is None or plan is None:
        flash("Empresa o plan inválido.", "danger")
        return _redirect_back("saas.subscriptions_panel")

    duration_days = int(plan.duration_days or 30)
    next_due = next_billing_date or (start_date + timedelta(days=duration_days))
    subscription = Subscription(
        company_id=company.id,
        plan_id=plan.id,
        status=status,
        start_date=start_date,
        starts_at=start_date,
        ends_at=next_due,
        next_billing_date=next_due,
        renewal_enabled=renewal_enabled,
        auto_renew=renewal_enabled,
        cancel_at_period_end=not renewal_enabled,
    )
    db.session.add(subscription)
    db.session.flush()
    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=company.id,
            action="subscription_create",
            entity="subscription",
            entity_id=subscription.id,
            detail=f"Suscripción creada plan={plan.code} status={status}. ip={request.remote_addr or 'unknown'} resultado=ok",
        )
    )
    db.session.commit()
    flash("Suscripción creada correctamente.", "success")
    return _redirect_back("saas.subscriptions_panel")


@bp.route("/subscriptions/<int:subscription_id>/update", methods=["POST"])
@superadmin_required
def subscriptions_update(subscription_id):
    from app import AuditLog, Plan, Subscription

    _require_superadmin()
    subscription = Subscription.query.filter_by(id=subscription_id).first_or_404()
    plan_id = request.form.get("plan_id", type=int)
    plan = Plan.query.filter_by(id=plan_id).first() if plan_id else None
    if plan_id and plan is None:
        flash("Plan inválido.", "danger")
        return _redirect_back("saas.subscriptions_panel")

    if plan:
        subscription.plan_id = plan.id
    subscription.status = (request.form.get("status") or subscription.status or "pending").strip().lower()
    start_date = _parse_dt(request.form.get("start_date"))
    next_billing_date = _parse_dt(request.form.get("next_billing_date"))
    last_payment_date = _parse_dt(request.form.get("last_payment_date"))
    if start_date:
        subscription.start_date = start_date
        subscription.starts_at = start_date
    if next_billing_date:
        subscription.next_billing_date = next_billing_date
        subscription.ends_at = next_billing_date
    if last_payment_date:
        subscription.last_payment_date = last_payment_date

    renewal_enabled = (request.form.get("renewal_enabled") or "1") == "1"
    subscription.renewal_enabled = renewal_enabled
    subscription.auto_renew = renewal_enabled
    subscription.cancel_at_period_end = not renewal_enabled

    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=subscription.company_id,
            action="subscription_update",
            entity="subscription",
            entity_id=subscription.id,
            detail=f"Suscripción actualizada status={subscription.status}. ip={request.remote_addr or 'unknown'} resultado=ok",
        )
    )
    db.session.commit()
    flash("Suscripción actualizada.", "success")
    return _redirect_back("saas.subscriptions_panel")


@bp.route("/subscriptions/<int:subscription_id>/action", methods=["POST"])
@superadmin_required
def subscriptions_action(subscription_id):
    from app import AuditLog, PaymentHistory, Subscription

    _require_superadmin()
    subscription = Subscription.query.filter_by(id=subscription_id).first_or_404()
    action = (request.form.get("action") or "").strip().lower()
    detail = ""

    if action == "cancel":
        subscription.status = "cancelled"
        subscription.renewal_enabled = False
        subscription.auto_renew = False
        subscription.cancel_at_period_end = True
        detail = "Suscripción cancelada"
    elif action == "reactivate":
        subscription.status = "active"
        subscription.renewal_enabled = True
        subscription.auto_renew = True
        subscription.cancel_at_period_end = False
        detail = "Suscripción reactivada"
    elif action == "suspend":
        subscription.status = "suspended"
        subscription.renewal_enabled = False
        subscription.auto_renew = False
        detail = "Suscripción suspendida"
    elif action == "extend":
        days = request.form.get("days", type=int) or 7
        days = min(max(days, 1), 365)
        base = subscription.next_billing_date or utcnow()
        subscription.next_billing_date = base + timedelta(days=days)
        subscription.ends_at = subscription.next_billing_date
        detail = f"Suscripción extendida {days} días"
    elif action == "renew_now":
        duration_days = int(subscription.plan.duration_days if subscription.plan else 30)
        base = subscription.next_billing_date or utcnow()
        subscription.last_payment_date = utcnow()
        subscription.next_billing_date = base + timedelta(days=duration_days)
        subscription.ends_at = subscription.next_billing_date
        subscription.status = "active"
        subscription.renewal_enabled = True
        subscription.auto_renew = True
        detail = "Renovación manual aplicada"
    elif action == "delete":
        subscription.status = "cancelled"
        subscription.renewal_enabled = False
        subscription.auto_renew = False
        subscription.cancel_at_period_end = True
        metadata = json.loads(subscription.metadata_json) if subscription.metadata_json else {}
        metadata["deleted_by_superadmin"] = True
        metadata["deleted_at"] = utcnow().isoformat()
        subscription.metadata_json = json.dumps(metadata, ensure_ascii=False)
        detail = "Suscripción eliminada lógicamente"
    else:
        flash("Acción de suscripción inválida.", "danger")
        return _redirect_back("saas.subscriptions_panel")

    db.session.add(
        AuditLog(
            user_id=current_user.id,
            company_id=subscription.company_id,
            action=f"subscription_{action}",
            entity="subscription",
            entity_id=subscription.id,
            detail=f"{detail}. ip={request.remote_addr or 'unknown'} resultado=ok",
        )
    )
    db.session.add(
        PaymentHistory(
            company_id=subscription.company_id,
            subscription_id=subscription.id,
            event=f"subscription_{action}",
            detail=detail,
            source="superadmin",
            status=subscription.status,
            payload_json=json.dumps({"action": action, "user_id": current_user.id}, ensure_ascii=False),
        )
    )
    db.session.commit()
    flash(f"{detail}.", "success")
    return _redirect_back("saas.subscriptions_panel")


@bp.route("/users")
@superadmin_required
def users_panel():
    from app import User

    _require_superadmin()
    users = User.query.order_by(User.created_at.desc()).limit(200).all()
    return render_template("saas/users.html", users=users)


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
    context = {
        "db_ok": db_ok,
        "db_error": db_error,
        "flask_env": os.environ.get("FLASK_ENV", "development"),
        "render": bool(os.environ.get("RENDER")),
        "database_url_configured": bool(os.environ.get("DATABASE_URL")),
    }
    return render_template("saas/server_status.html", status=context)


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
        "google_client_id_configured": bool(os.environ.get("GOOGLE_CLIENT_ID")),
        "google_client_secret_configured": bool(os.environ.get("GOOGLE_CLIENT_SECRET")),
        "mp_access_token_configured": bool(os.environ.get("MP_ACCESS_TOKEN")),
        "mp_public_key_configured": bool(os.environ.get("MP_PUBLIC_KEY")),
        "mp_webhook_secret_configured": bool(os.environ.get("MP_WEBHOOK_SECRET")),
    }
    return render_template("saas/settings.html", settings_snapshot=settings_snapshot)


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

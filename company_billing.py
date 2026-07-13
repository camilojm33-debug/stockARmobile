"""Portal de suscripcion para empresas (tenant) y webhook publico de Mercado Pago."""

from __future__ import annotations

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app import company_admin_required, csrf, tenant_required
from config.billing_config import load_billing_config
from services.billing_service import BillingService
from services.plan_service import PlanService
from services.subscription_service import SubscriptionService
from services.webhook_service import WebhookService

bp = Blueprint("company_billing", __name__)


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

    return render_template(
        "company_billing/portal.html",
        company=company,
        plans=plans,
        subscription=subscription,
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
        SubscriptionService.start_or_change_plan(db.session, company=company, plan=plan, user_id=current_user.id)
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
@company_admin_required
def payment_qr_settings():
    from app import Company, db

    company_id = getattr(current_user, "company_id", None)
    company = Company.query.filter_by(id=company_id).first_or_404()

    company.payment_alias = (request.form.get("payment_alias") or "").strip() or None
    company.payment_cbu = (request.form.get("payment_cbu") or "").strip() or None
    company.payment_cvu = (request.form.get("payment_cvu") or "").strip() or None
    company.payment_qr_text = (request.form.get("payment_qr_text") or "").strip() or None
    company.payment_qr_url = (request.form.get("payment_qr_url") or "").strip() or None

    if not any([company.payment_alias, company.payment_cbu, company.payment_cvu, company.payment_qr_text, company.payment_qr_url]):
        flash("Guardado. Agrega al menos un dato para generar el QR de cobro.", "warning")
    else:
        flash("Datos de cobro QR guardados correctamente.", "success")
    db.session.commit()
    return redirect(url_for("company_billing.subscription_portal"))


@bp.route("/company-settings")
@company_admin_required
def company_settings():
    return render_template("company_billing/settings.html")


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

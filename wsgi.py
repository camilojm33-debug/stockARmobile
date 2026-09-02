from flask import flash, redirect, request, url_for
from flask_login import current_user, login_required

from app import app, AuditLog, Invoice, Payment, PaymentHistory, Subscription, SubscriptionCommandExecution, csrf, db
from services.subscription_service import SubscriptionService
from services.ai_agent.admin import bp as ai_admin_bp
from whatsapp_agent import bp as whatsapp_agent_bp


if "ai_admin" not in app.blueprints:
    app.register_blueprint(ai_admin_bp)
if "whatsapp_agent" not in app.blueprints:
    app.register_blueprint(whatsapp_agent_bp)
    csrf.exempt(whatsapp_agent_bp)


@app.route("/superadmin/subscriptions/<int:subscription_id>/delete-historical", methods=["POST"])
@login_required
def superadmin_delete_historical_subscription(subscription_id):
    if getattr(current_user, "role", None) != "superadmin":
        return ("Forbidden", 403)

    subscription = Subscription.query.filter_by(id=subscription_id).first_or_404()
    current_subscription = SubscriptionService.active_subscription_for_company(subscription.company_id)

    # Never allow this endpoint to delete the company's current subscription.
    if current_subscription is not None and current_subscription.id == subscription.id:
        flash("No se puede eliminar la suscripción actual de la empresa.", "warning")
        return redirect(url_for("saas.subscriptions_panel"))

    company_id = subscription.company_id
    company_name = subscription.company.name if subscription.company else str(company_id)

    try:
        # Financial records remain intact; only their optional subscription link is cleared.
        for model in (Payment, Invoice, PaymentHistory, SubscriptionCommandExecution):
            if hasattr(model, "subscription_id"):
                db.session.query(model).filter(model.subscription_id == subscription.id).update(
                    {model.subscription_id: None}, synchronize_session=False
                )

        db.session.add(AuditLog(
            user_id=current_user.id,
            company_id=company_id,
            action="subscription_historical_hard_delete",
            entity="subscription",
            entity_id=subscription.id,
            detail=f"Suscripción histórica eliminada definitivamente por SuperAdmin: {company_name}. ip={request.remote_addr or 'unknown'} resultado=ok",
        ))
        db.session.delete(subscription)
        db.session.commit()
        flash("Suscripción histórica eliminada definitivamente.", "success")
    except Exception:
        db.session.rollback()
        app.logger.exception("Error eliminando suscripción histórica id=%s", subscription_id)
        flash("No se pudo eliminar la suscripción histórica.", "danger")

    return redirect(url_for("saas.subscriptions_panel"))


application = app

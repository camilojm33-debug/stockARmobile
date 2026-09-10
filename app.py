from flask import flash, redirect, request, url_for, jsonify
from flask_login import current_user, login_required
from sqlalchemy import text

from app import app, AuditLog, Invoice, Payment, PaymentHistory, Subscription, SubscriptionCommandExecution, db
from services.subscription_service import SubscriptionService
from services.referral_network_service import network_bp, install_commission_hook

if "referral_network.dashboard" not in app.view_functions:
    app.register_blueprint(network_bp)
install_commission_hook()

def health():
    """Lightweight liveness/readiness endpoint for Render and uptime checks."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok"}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Health check database probe failed")
        return jsonify({"status": "error"}), 503

if "health" not in app.view_functions:
    app.add_url_rule("/health", endpoint="health", view_func=health, methods=["GET"])

@app.route("/superadmin/subscriptions/<int:subscription_id>/delete-historical", methods=["POST"])
@login_required
def superadmin_delete_historical_subscription(subscription_id):
    if getattr(current_user, "role", None) != "superadmin":
        return ("Forbidden", 403)
    subscription = Subscription.query.filter_by(id=subscription_id).first_or_404()
    current_subscription = SubscriptionService.active_subscription_for_company(subscription.company_id)
    if current_subscription is not None and current_subscription.id == subscription.id:
        flash("No se puede eliminar la suscripción actual de la empresa.", "warning")
        return redirect(url_for("saas.subscriptions_panel"))
    company_id = subscription.company_id
    company_name = subscription.company.name if subscription.company else str(company_id)
    try:
        for model in (Payment, Invoice, PaymentHistory, SubscriptionCommandExecution):
            if hasattr(model, "subscription_id"):
                db.session.query(model).filter(model.subscription_id == subscription.id).update({model.subscription_id: None}, synchronize_session=False)
        db.session.add(AuditLog(user_id=current_user.id, company_id=company_id, action="subscription_historical_hard_delete", entity="subscription", entity_id=subscription.id, detail=f"Suscripción histórica eliminada definitivamente por SuperAdmin: {company_name}. ip={request.remote_addr or 'unknown'} resultado=ok"))
        db.session.delete(subscription)
        db.session.commit()
        flash("Suscripción histórica eliminada definitivamente.", "success")
    except Exception:
        db.session.rollback()
        app.logger.exception("Error eliminando suscripción histórica id=%s", subscription_id)
        flash("No se pudo eliminar la suscripción histórica.", "danger")
    return redirect(url_for("saas.subscriptions_panel"))


class ReferralNetworkPayout(db.Model):
    __tablename__ = "referral_network_payouts"

    id = db.Column(db.Integer, primary_key=True)
    parent_seller_id = db.Column(db.Integer, db.ForeignKey("referral_sellers.id"), nullable=False, index=True)
    processed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    amount = db.Column(MONEY, default=Decimal("0.00"), nullable=False)
    transfer_date = db.Column(db.DateTime, nullable=False)
    payment_method = db.Column(db.String(80))
    receipt = db.Column(db.String(255))
    transfer_number = db.Column(db.String(120))
    observations = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    parent_seller = db.relationship("ReferralSeller")
    processed_by = db.relationship("User")


application = app
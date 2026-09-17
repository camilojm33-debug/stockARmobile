from flask import flash, redirect, request, url_for, jsonify, render_template, g
from flask_login import current_user, login_required
from sqlalchemy import text

from app import app, AuditLog, Company, Invoice, Payment, PaymentHistory, Subscription, SubscriptionCommandExecution, Supplier, db
from services.subscription_service import SubscriptionService
from services.ai_agent.usage_service import can_use_ai
from stockarmobile.models.conversations import Conversation
from pricing_controller import bp as pricing_controller_bp


def stockarmobile_health():
    """Lightweight liveness/readiness endpoint for Render and uptime checks."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok"}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Health check database probe failed")
        return jsonify({"status": "error"}), 503


if not any(rule.rule == "/health" for rule in app.url_map.iter_rules()):
    app.add_url_rule("/health", endpoint="stockarmobile_health", view_func=stockarmobile_health, methods=["GET"])


@app.before_request
def enforce_registration_legal_acceptance():
    """Enforce legal acceptance server-side and keep a durable audit record."""
    if request.endpoint != "auth.register" or request.method != "POST":
        return None
    if (request.form.get("mode") or "").strip().lower() == "seller":
        return None
    if request.form.get("accept_legal_terms") != "1":
        flash("Debes aceptar los Términos y Condiciones y la Política de Privacidad para crear la cuenta.", "warning")
        return redirect(url_for("auth.register", selected_plan=(request.form.get("selected_plan") or "trial").strip().lower(), mode=(request.form.get("mode") or "").strip().lower()))
    email = (request.form.get("email") or "").strip().lower()
    if email:
        db.session.add(AuditLog(action="legal_acceptance_submitted", entity="registration", detail=f"Aceptación de términos enviada en registro. email={email}; terms_version=2026-09-16; privacy_version=2026-09-14", ip_address=(request.remote_addr or "unknown")))
        db.session.commit()
    return None


@app.before_request
def trace_mp_qr_requests():
    path = request.path or ""
    if not path.startswith("/ventas/api/mp-qr/"):
        return None
    g.mp_qr_trace_started = True
    g.mp_qr_endpoint_entered = False
    g.mp_qr_csrf_header_present = bool(request.headers.get("X-CSRFToken"))
    g.mp_qr_request_method = request.method
    g.mp_qr_request_path = path
    g.mp_qr_request_endpoint = request.endpoint or ""
    g.mp_qr_request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Correlation-ID") or ""
    g.mp_qr_user_id = None
    g.mp_qr_company_id = None
    try:
        if current_user.is_authenticated:
            g.mp_qr_user_id = getattr(current_user, "id", None)
            g.mp_qr_company_id = getattr(current_user, "company_id", None)
    except Exception as exc:
        app.logger.warning("MP QR trace incoming user-context capture failed: %s", exc)
    app.logger.info("MP QR trace incoming: method=%s path=%s endpoint=%s csrf_header_present=%s company_id=%s user_id=%s request_id=%s", request.method, path, request.endpoint or "", g.mp_qr_csrf_header_present, getattr(g, "mp_qr_company_id", None), getattr(g, "mp_qr_user_id", None), g.mp_qr_request_id)


@app.route("/dashboard/ai-agent/facturas", methods=["GET"])
@login_required
def ai_invoices_workspace():
    """Tenant-scoped workspace for supplier invoices processed with AI."""
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        return redirect(url_for("auth.login"))
    company = Company.query.filter_by(id=company_id).first()
    invoice_access = can_use_ai(company, "facturas") if company is not None else type("Access", (), {"allowed": False, "reason": "No hay una empresa activa."})()
    invoices = []
    suppliers = []
    if invoice_access.allowed:
        conversations = Conversation.query.filter_by(company_id=company_id).order_by(Conversation.id.desc()).limit(100).all()
        for conversation in conversations:
            metadata = conversation.metadata_json or {}
            for upload in metadata.get("invoice_uploads") or []:
                if not isinstance(upload, dict) or not upload.get("upload_id"):
                    continue
                invoices.append({"upload_id": str(upload.get("upload_id")), "original_name": str(upload.get("original_name") or "Factura"), "status": str(upload.get("status") or "PENDIENTE_PROCESAMIENTO"), "invoice": upload.get("invoice") or {}, "conversation_id": conversation.id})
        invoices = invoices[:50]
        suppliers = Supplier.query.filter_by(company_id=company_id, active=True).order_by(Supplier.name.asc()).all()
    can_confirm = getattr(current_user, "role", None) in {"admin", "superadmin"}
    try:
        plan_url = url_for("ai_agents.agent", agent="planes")
    except Exception:
        plan_url = "/dashboard/ai-agent/planes"
    return render_template("ai_agents/invoices_smart.html", invoice_access=invoice_access, invoices=invoices, can_confirm=can_confirm, plan_url=plan_url, suppliers=suppliers)


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


app.register_blueprint(pricing_controller_bp)


@app.route("/productos/precios")
@login_required
def products_pricing_shortcut():
    return redirect(url_for("pricing_controller.index"))


application = app

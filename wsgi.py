from flask import flash, redirect, request, url_for, jsonify, render_template
from flask_login import current_user, login_required
from sqlalchemy import text

from app import app, AuditLog, Company, Invoice, Payment, PaymentHistory, Subscription, SubscriptionCommandExecution, db
from services.subscription_service import SubscriptionService
from services.ai_agent.usage_service import can_use_ai
from stockarmobile.models.conversations import Conversation


def stockarmobile_health():
    """Lightweight liveness/readiness endpoint for Render and uptime checks."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok"}), 200
    except Exception:
        db.session.rollback()
        app.logger.exception("Health check database probe failed")
        return jsonify({"status": "error"}), 503


# The application factory or another bootstrap path may already expose /health.
# Detect the URL rule itself (not only the endpoint name) before adding ours.
if not any(rule.rule == "/health" for rule in app.url_map.iter_rules()):
    app.add_url_rule(
        "/health",
        endpoint="stockarmobile_health",
        view_func=stockarmobile_health,
        methods=["GET"],
    )


@app.route("/dashboard/ai-agent/facturas", methods=["GET"])
@login_required
def ai_invoices_workspace():
    """Tenant-scoped workspace for supplier invoices processed with AI."""
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        return redirect(url_for("auth.login"))

    company = Company.query.filter_by(id=company_id).first()
    invoice_access = can_use_ai(company, "facturas") if company is not None else type(
        "Access", (), {"allowed": False, "reason": "No hay una empresa activa."}
    )()
    invoices = []

    if invoice_access.allowed:
        conversations = Conversation.query.filter_by(company_id=company_id).order_by(Conversation.id.desc()).limit(100).all()
        for conversation in conversations:
            metadata = conversation.metadata_json or {}
            for upload in metadata.get("invoice_uploads") or []:
                if not isinstance(upload, dict) or not upload.get("upload_id"):
                    continue
                invoices.append(
                    {
                        "upload_id": str(upload.get("upload_id")),
                        "original_name": str(upload.get("original_name") or "Factura"),
                        "status": str(upload.get("status") or "PENDIENTE_PROCESAMIENTO"),
                        "invoice": upload.get("invoice") or {},
                        "conversation_id": conversation.id,
                    }
                )
        invoices = invoices[:50]

    can_confirm = getattr(current_user, "role", None) in {"admin", "superadmin"}
    try:
        plan_url = url_for("ai_agents.agent", agent="planes")
    except Exception:
        plan_url = "/dashboard/ai-agent/planes"

    return render_template(
        "ai_agents/invoices_smart.html",
        invoice_access=invoice_access,
        invoices=invoices,
        can_confirm=can_confirm,
        plan_url=plan_url,
    )


@app.route("/dashboard/ai-agent/invoices/<upload_id>/review", methods=["GET"])
@login_required
def invoice_review_page(upload_id):
    """Direct HTML review page for a previously uploaded invoice."""
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        return redirect(url_for("auth.login"))
    company = Company.query.filter_by(id=company_id).first()
    invoice_access = can_use_ai(company, "facturas") if company is not None else type(
        "Access", (), {"allowed": False, "reason": "No hay una empresa activa."}
    )()
    if not invoice_access.allowed:
        return redirect(url_for("ai_invoices_workspace"))
    from dashboard import _invoice_record
    found = _invoice_record(upload_id, company_id)
    if found is None:
        return redirect(url_for("ai_invoices_workspace"))
    _conversation, _metadata, _uploads, _index, upload = found
    return render_template(
        "ai_agents/invoice_review.html",
        upload_id=str(upload_id),
        original_name=str(upload.get("original_name") or "Factura"),
        status=str(upload.get("status") or "PENDIENTE_PROCESAMIENTO"),
        preview=upload.get("invoice") or {},
        can_confirm=getattr(current_user, "role", None) in {"admin", "superadmin"},
    )


@app.route("/dashboard/ai-agent/invoices/<upload_id>/resolve-product", methods=["POST"])
@login_required
def resolve_invoice_product_v2(upload_id):
    """Tenant-scoped product selector used by the direct historical invoice review page."""
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        return jsonify({"success": False, "error": "No hay una empresa activa."}), 403
    company = Company.query.filter_by(id=company_id).first()
    access = can_use_ai(company, "facturas") if company is not None else type("Access", (), {"allowed": False, "reason": "No hay una empresa activa."})()
    if not access.allowed:
        return jsonify({"success": False, "error": access.reason}), 403

    from dashboard import _invoice_record, _save_invoice
    from app import Product
    from services.invoice_matching_service import normalize

    found = _invoice_record(upload_id, company_id)
    if found is None:
        return jsonify({"success": False, "error": "Factura no encontrada."}), 404
    conversation, metadata, uploads, index, upload = found
    invoice = upload.get("invoice") or {}
    line_number = request.json.get("line_number") if request.is_json else None
    payload = request.get_json(silent=True) or {}
    line_number = payload.get("line_number")
    line = next((item for item in invoice.get("matches") or [] if str(item.get("line_number")) == str(line_number)), None)
    if line is None:
        return jsonify({"success": False, "error": "Línea no encontrada."}), 400

    products = Product.query.filter_by(company_id=company_id, active=True).all()
    product_id = payload.get("product_id")
    if product_id not in (None, ""):
        product = Product.query.filter_by(id=product_id, company_id=company_id, active=True).first()
        if product is None:
            return jsonify({"success": False, "error": "Producto inválido para esta empresa."}), 409
        line.update({"matching_status": "MATCH_EXACTO", "matching_reason": "SELECCION_MANUAL", "product_id": product.id, "product_name": product.name, "product_code": product.barcode, "confidence_level": "ALTA", "auto_matched": False})
    else:
        query = str(payload.get("query") or "").strip()
        if not query or query == "__SUGGEST__":
            query = str(line.get("description") or "").strip()
        key = normalize(query)
        exact = [p for p in products if key and (normalize(p.name) == key or normalize(p.barcode) == key)]
        candidates = exact or [p for p in products if key and (key in normalize(p.name) or key in normalize(p.barcode))]
        if len(candidates) != 1:
            return jsonify({"success": False, "error": "Elegí uno de los productos sugeridos o refiná la búsqueda.", "candidates": [{"id": p.id, "name": p.name, "code": p.barcode, "category": getattr(p, "category", None)} for p in candidates[:8]]}), 409
        product = candidates[0]
        line.update({"matching_status": "MATCH_EXACTO", "matching_reason": "SELECCION_MANUAL", "product_id": product.id, "product_name": product.name, "product_code": product.barcode, "confidence_level": "ALTA", "auto_matched": False})

    invoice["status"] = "LISTA_PARA_CONFIRMAR" if not invoice.get("requires_review") and not any(item.get("matching_status") in {"AMBIGUO", "MATCH_PROPUESTO"} for item in invoice.get("matches") or []) else "REQUIERE_REVISION"
    upload["invoice"] = invoice
    upload["status"] = invoice["status"]
    _save_invoice(conversation, metadata, uploads, index, upload)
    return jsonify({"success": True, "status": upload["status"], "preview": invoice})


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


application = app

from decimal import Decimal, InvalidOperation

from flask import flash, redirect, request, url_for, jsonify, render_template, g
from flask_login import current_user, login_required
from sqlalchemy import text

from app import app, AuditLog, Company, Invoice, Payment, PaymentHistory, Subscription, SubscriptionCommandExecution, Supplier, Product, db
from services.subscription_service import SubscriptionService
from services.ai_agent.usage_service import can_use_ai, can_use_ai_feature
from services.invoice_matching_service import normalize
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


@app.after_request
def inject_invoice_manual_code_fix(response):
    """Load invoice barcode/manual-code fixes only in the invoice workspace."""
    if request.path != "/dashboard/ai-agent/facturas" or not response.content_type.startswith("text/html"):
        return response
    try:
        html = response.get_data(as_text=True)
        scripts = [
            '<script src="/static/assets/js/barcode-scanner.js?v=20260917-scan-3"></script>',
            '<script src="/static/js/invoice_manual_code_fix.js?v=20260917-manual-code-4" defer></script>',
            '<script src="/static/js/invoice_scanner_fix.js?v=20260917-scanner-fix-1"></script>',
        ]
        for script in scripts:
            marker = script.split(' src="', 1)[1].split('"', 1)[0] if ' src="' in script else script
            if marker not in html:
                if "</body>" in html:
                    html = html.replace("</body>", script + "</body>", 1)
                else:
                    html += script
        response.set_data(html)
    except Exception:
        app.logger.exception("Could not inject invoice scanner/manual-code fixes")
    return response


@app.route("/dashboard/ai-agent/facturas", methods=["GET"])
@login_required
def ai_invoices_workspace():
    """Tenant-scoped workspace for supplier invoices processed with AI."""
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        return redirect(url_for("auth.login"))
    company = Company.query.filter_by(id=company_id).first()
    invoice_access = can_use_ai_feature(company, "facturas") if company is not None else type("Access", (), {"allowed": False, "reason": "No hay una empresa activa."})()
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


@app.route("/dashboard/ai-agent/invoices/<upload_id>/resolve-code", methods=["POST"])
@login_required
def resolve_invoice_code_direct(upload_id):
    """Assign an invoice line to a tenant-scoped product by exact SKU/barcode.

    A missing code creates/uses a draft product with the invoice unit cost but
    never adds stock. If the invoice was already applied, the endpoint reuses
    the product recorded by that purchase instead of creating a zero-cost
    duplicate.
    """
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        return jsonify({"success": False, "error": "No hay una empresa activa."}), 403

    company = Company.query.filter_by(id=company_id).first()
    invoice_access = can_use_ai_feature(company, "facturas") if company is not None else None
    if invoice_access is None or not invoice_access.allowed:
        return jsonify({"success": False, "error": invoice_access.reason if invoice_access else "No hay una empresa activa."}), 403

    payload = request.get_json(silent=True) or {}
    raw_code = str(payload.get("code") or "").strip()
    line_number = payload.get("line_number")
    if not raw_code:
        return jsonify({"success": False, "error": "Ingresá o escaneá un código de producto."}), 400

    def code_key(value):
        normalized = normalize(value).replace(" ", "")
        if normalized.isdigit():
            normalized = normalized.lstrip("0") or "0"
        return normalized

    query_key = code_key(raw_code)
    found = None
    for conversation in Conversation.query.filter_by(company_id=company_id).all():
        metadata = dict(conversation.metadata_json or {})
        uploads = list(metadata.get("invoice_uploads") or [])
        for index, upload in enumerate(uploads):
            if str(upload.get("upload_id")) == str(upload_id):
                found = (conversation, metadata, uploads, index, upload)
                break
        if found:
            break
    if found is None:
        return jsonify({"success": False, "error": "Factura no encontrada."}), 404

    conversation, metadata, uploads, upload_index, upload = found
    invoice = upload.get("invoice") or {}
    matches = invoice.get("matches") or []
    line = next((item for item in matches if str(item.get("line_number")) == str(line_number)), None)
    if line is None:
        return jsonify({"success": False, "error": "Línea de factura no encontrada."}), 400

    products = Product.query.filter_by(company_id=company_id).all()
    active_candidates = [product for product in products if getattr(product, "active", True) and code_key(getattr(product, "barcode", None)) == query_key]
    created = False
    reactivated = False
    reassigned_existing = False

    result_items = (invoice.get("result") or {}).get("items") or []
    result_item = next((item for item in result_items if str(item.get("line_number")) == str(line_number)), None)
    linked_product_id = line.get("product_id") or (result_item or {}).get("product_id")
    linked_product = None
    if linked_product_id:
        linked_product = Product.query.filter_by(id=linked_product_id, company_id=company_id).first()
        if linked_product is not None and not getattr(linked_product, "active", True):
            linked_product.active = True
            reactivated = True

    if len(active_candidates) > 1:
        return jsonify({"success": False, "error": "Hay más de un producto activo con ese código/SKU en StockAR."}), 409

    if len(active_candidates) == 1:
        product = active_candidates[0]
        if linked_product is not None and product.id != linked_product.id:
            duplicate_like = (
                Decimal(str(product.stock or 0)) == Decimal("0")
                and Decimal(str(product.cost_price or 0)) == Decimal("0")
                and normalize(product.name) == normalize(line.get("description"))
            )
            if not duplicate_like:
                return jsonify({"success": False, "error": "Ese código ya está asignado a otro producto activo."}), 409
            product.active = False
            product = linked_product
            reassigned_existing = True
        elif linked_product is not None and product.id == linked_product.id:
            reassigned_existing = True
    elif linked_product is not None:
        product = linked_product
        reassigned_existing = True
    else:
        inactive_candidates = [product for product in products if not getattr(product, "active", True) and code_key(getattr(product, "barcode", None)) == query_key]
        if len(inactive_candidates) > 1:
            return jsonify({"success": False, "error": "Hay varios productos inactivos con ese código. Revisalos antes de continuar."}), 409
        if len(inactive_candidates) == 1:
            product = inactive_candidates[0]
            product.active = True
            reactivated = True
        else:
            try:
                unit_cost = Decimal(str(line.get("unit_cost") or "0"))
            except (TypeError, ValueError, ArithmeticError):
                unit_cost = Decimal("0")
            product = Product(
                company_id=company_id,
                barcode=raw_code,
                name=str(line.get("description") or "Producto de factura")[:200],
                stock=0,
                cost_price=unit_cost,
                price=0,
                active=True,
            )
            db.session.add(product)
            db.session.flush()
            created = True

    try:
        unit_cost = Decimal(str(line.get("unit_cost") or "0"))
    except (TypeError, ValueError, ArithmeticError):
        unit_cost = Decimal(str(product.cost_price or "0"))

    # Manual resolution must show the invoice cost immediately, but stock only
    # changes when the final confirmation applies the purchase.
    if Decimal(str(product.stock or 0)) == Decimal("0") and unit_cost >= 0:
        product.cost_price = unit_cost
    product.barcode = raw_code
    product.margin = Decimal(str(product.price or 0)) - Decimal(str(product.cost_price or 0))
    product.profit_percent = (product.margin / Decimal(str(product.cost_price)) * 100) if Decimal(str(product.cost_price or 0)) else 0

    line.update({
        "matching_status": "MATCH_EXACTO",
        "matching_reason": "CODIGO_ESCANEADO_O_MANUAL",
        "product_id": product.id,
        "product_name": product.name,
        "product_code": product.barcode,
        "confidence_level": "ALTA",
        "proposal_score": 1.0,
        "auto_matched": False,
        "created_from_manual_code": created,
        "reassigned_existing_product": reassigned_existing,
        "unit_cost": str(unit_cost),
    })
    if result_item is not None:
        result_item["product_id"] = product.id
        result_item["product_code"] = product.barcode
        invoice["result"]["items"] = result_items

    supplier_status = (invoice.get("supplier_match") or {}).get("status")
    active_lines = [item for item in matches if item.get("matching_status") != "EXCLUIDA"]
    blocking_warnings = any(str(warning).startswith(("La línea ", "Los totales de la factura")) for warning in (invoice.get("warnings") or []))
    ready = (
        supplier_status in {"MATCH_EXACTO", "NUEVO_PROVEEDOR_CONFIRMADO"}
        and bool(active_lines)
        and not any(item.get("matching_status") in {"AMBIGUO", "MATCH_PROPUESTO"} for item in active_lines)
        and not any(item.get("quantity") in (None, "") or item.get("unit_cost") in (None, "") for item in active_lines)
        and not blocking_warnings
    )
    invoice["status"] = "LISTA_PARA_CONFIRMAR" if ready else "REQUIERE_REVISION"
    upload["invoice"] = invoice
    upload["status"] = invoice["status"]
    uploads[upload_index] = upload
    metadata["invoice_uploads"] = uploads
    conversation.metadata_json = metadata
    db.session.commit()

    return jsonify({
        "success": True,
        "status": invoice["status"],
        "preview": invoice,
        "product": {
            "id": product.id,
            "name": product.name,
            "code": product.barcode,
            "created": created,
            "reactivated": reactivated,
            "reassigned_existing": reassigned_existing,
            "unit_cost": str(unit_cost),
        },
    })


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

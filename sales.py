"""Blueprint de ventas: carrito, checkout, historial y tickets."""

import csv
import base64
import hashlib
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from datetime import datetime
from io import BytesIO, StringIO
from urllib.parse import quote

from flask import Blueprint, abort, current_app, flash, jsonify, make_response, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from app import tenant_required, utcnow
from services.mercadopago_oauth_service import MercadoPagoOAuthService
from services.mercadopago_service import MercadoPagoService
from services.sales_calculation_service import calculate_sale_totals, normalize_payment_split, sale_payment_breakdown_from_values, to_decimal
import qrcode

bp = Blueprint("sales", __name__)


def _to_float(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _to_decimal(value, default="0.00"):
    try:
        if value in (None, ""):
            return Decimal(default)
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _is_truthy(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "si", "on"}


def _clean_comprobante_type(raw_value):
    allowed = {
        "factura_a",
        "factura_b",
        "factura_c",
        "ticket_fiscal",
        "remito",
        "otro",
    }
    value = (raw_value or "").strip().lower()
    return value if value in allowed else None


def _resolve_comprobante_payload(data):
    document_type = (data.get("document_type") or "").strip().lower()
    explicit_requires = _is_truthy(data.get("requiere_comprobante"))
    explicit_tipo = _clean_comprobante_type(data.get("tipo_comprobante"))
    inferred_tipo = _clean_comprobante_type(document_type)

    requiere_comprobante = explicit_requires or bool(explicit_tipo) or bool(inferred_tipo)
    tipo_comprobante = explicit_tipo or inferred_tipo
    observacion_comprobante = (data.get("observacion_comprobante") or "").strip()[:255] if requiere_comprobante else None
    return requiere_comprobante, tipo_comprobante, observacion_comprobante


def _requires_identified_client(data, requiere_comprobante, tipo_comprobante):
    document_type = (data.get("document_type") or "").strip().lower()
    if document_type in {"factura_a", "factura_b", "factura_c"}:
        return True
    if requiere_comprobante and tipo_comprobante in {"factura_a", "factura_b", "factura_c"}:
        return True
    return False


def _sanitize_checkout_token(raw_value):
    token = (raw_value or "").strip()
    if not token:
        return None
    token = token[:64]
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", token):
        raise ValueError("Token de checkout inválido.")
    return token


def _new_checkout_token():
    return f"chk_{uuid.uuid4()}"


def _cart_key():
    company_id = getattr(current_user, "company_id", None) or "global"
    return f"cart_{company_id}_{current_user.id}"


def _cart_tenant_key():
    company_id = getattr(current_user, "company_id", None) or "global"
    return f"{company_id}:{current_user.id}"


def _current_open_cash_session():
    from app import CashSession, scope_query_to_company

    session_query = scope_query_to_company(CashSession.query.filter_by(status="abierta", user_id=current_user.id), CashSession)
    return session_query.order_by(CashSession.opened_at.desc()).first()


def _require_open_cash_session(json_response=False):
    open_session = _current_open_cash_session()
    if open_session is not None:
        return open_session
    message = "Debes abrir una caja antes de comenzar a vender."
    if json_response:
        return jsonify({"error": message}), 409
    flash(message, "warning")
    return None


def _pos_qr_draft_session_key():
    return f"pos_qr_draft_{_cart_tenant_key()}"


def _pos_qr_snapshot(items, payload, *, total_amount, currency):
    normalized_items = []
    requiere_comprobante, tipo_comprobante, observacion_comprobante = _resolve_comprobante_payload(payload)
    for item in items:
        normalized_items.append(
            {
                "productId": int(item.get("productId") or item.get("product_id") or 0),
                "name": item.get("name") or "",
                "price": float(item.get("price") or 0),
                "quantity": float(item.get("quantity") or 0),
                "barcode": item.get("barcode") or "",
            }
        )
    cart_hash = hashlib.sha256(json.dumps(normalized_items, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "items": normalized_items,
        "client_id": payload.get("client_id") or payload.get("cliente_id") or "",
        "checkout_token": payload.get("checkout_token") or payload.get("checkoutToken") or "",
        "note": payload.get("note") or "",
        "document_type": payload.get("document_type") or payload.get("tipo_comprobante") or "venta",
        "requiere_comprobante": requiere_comprobante,
        "tipo_comprobante": tipo_comprobante,
        "observacion_comprobante": observacion_comprobante,
        "descuento_general": float(payload.get("descuento_general") or payload.get("general_discount") or 0),
        "recargo": float(payload.get("recargo") or payload.get("surcharge") or 0),
        "total_amount": float(total_amount),
        "currency": currency,
        "cart_hash": cart_hash,
    }


def _qr_data_uri(content: str) -> str:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _get_pos_qr_draft():
    return session.get(_pos_qr_draft_session_key())


def _set_pos_qr_draft(value):
    if value is None:
        session.pop(_pos_qr_draft_session_key(), None)
    else:
        session[_pos_qr_draft_session_key()] = value
    session.modified = True


def _get_cart():
    cart = session.get(_cart_key(), {"items": {}})
    cart.setdefault("items", {})
    return cart


def _save_cart(cart):
    session[_cart_key()] = cart
    session.modified = True


def _calculate_lines(items, *, lock_for_update=False):
    from app import Product, db, scope_query_to_company

    lines = []
    product_ids = sorted(int(prod_id) for prod_id in items.keys())
    product_query = scope_query_to_company(db.session.query(Product), Product).filter(Product.id.in_(product_ids), Product.active.is_(True)).order_by(Product.id.asc())
    if lock_for_update:
        product_query = product_query.with_for_update()
    products = {product.id: product for product in product_query.all()}
    current_app.logger.info("[sales] productos recibidos para calcular lineas: product_ids=%s encontrados=%s", product_ids, len(products))
    for prod_id, qty in items.items():
        product = products.get(int(prod_id))
        if not product:
            raise ValueError("Producto no encontrado.")
        qty = _to_float(qty)
        if qty <= 0:
            raise ValueError("La cantidad debe ser mayor a cero.")
        if float(product.stock or 0) < qty:
            raise ValueError(f"Stock insuficiente para {product.name}. Disponible: {product.stock:g} {product.unit_measure or ''}.")
        quantity_dec = _to_decimal(qty)
        unit_price = _to_decimal(product.price)
        unit_discount = _to_decimal(product.discount)
        line_subtotal = unit_price * quantity_dec
        line_discount = min(unit_discount * quantity_dec, line_subtotal)
        lines.append({"product": product, "quantity": qty, "price": unit_price, "discount": line_discount})
    return lines


@bp.route("/")
@tenant_required
def index():
    from app import Client, Company, Product, Sale, db, scope_query_to_company

    low_stock = request.args.get("low_stock")
    search = request.args.get("q") or request.args.get("search")
    category = request.args.get("category") or request.args.get("categoria")
    products_query = scope_query_to_company(Product.query.filter_by(active=True), Product)
    if search:
        like = f"%{search}%"
        products_query = products_query.filter((Product.name.ilike(like)) | (Product.barcode.ilike(like)) | (Product.category.ilike(like)))
    if category:
        products_query = products_query.filter(Product.category == category)
    if low_stock:
        products_query = products_query.filter(Product.stock <= Product.min_stock)
    products = products_query.order_by(Product.favorite.desc(), Product.name).all()
    categories = [c[0] for c in scope_query_to_company(Product.query.with_entities(Product.category), Product).filter(Product.active.is_(True)).distinct().order_by(Product.category).all() if c[0]]
    clients = scope_query_to_company(Client.query.filter_by(active=True), Client).order_by(Client.name).all()
    company = Company.query.filter_by(id=getattr(current_user, "company_id", None)).first()
    has_qr_data = bool(
        company
        and (
            company.payment_alias
            or company.payment_cbu
            or company.payment_cvu
            or company.payment_qr_text
            or company.payment_qr_url
        )
    )
    qr_payment_image_url = None
    if has_qr_data:
        qr_payment_image_url = url_for(
            "qr_labels.payment_qr",
            alias=company.payment_alias or "",
            cbu=company.payment_cbu or "",
            cvu=company.payment_cvu or "",
            text=company.payment_qr_text or company.name or "",
            url=company.payment_qr_url or "",
        )
    mp_connection_summary = MercadoPagoOAuthService().summarize_connection(getattr(company, "mercadopago_connection", None)) if company else MercadoPagoOAuthService().summarize_connection(None)
    cash_session_open = _current_open_cash_session() is not None
    total_sales_amount = scope_query_to_company(db.session.query(db.func.coalesce(db.func.sum(Sale.total_amount), 0)), Sale).scalar() or 0
    sales = scope_query_to_company(Sale.query.options(selectinload(Sale.client)), Sale).order_by(Sale.date.desc()).limit(20).all()
    return render_template(
        "ventas/index.html",
        products=products,
        categorias=categories,
        clientes=clients,
        sales=sales,
        total_sales=total_sales_amount,
        company_name=(company.name if company else "Mi comercio"),
        qr_payment_image_url=qr_payment_image_url,
        has_qr_payment_data=has_qr_data,
        mp_connection_summary=mp_connection_summary,
        cash_session_open=cash_session_open,
    )


@bp.route("/nueva-venta")
@tenant_required
def new_sale():
    from app import Company, Product, db, scope_query_to_company

    products_query = scope_query_to_company(Product.query.filter_by(active=True), Product)
    search = request.args.get("q") or request.args.get("search")
    if search:
        like = f"%{search}%"
        products_query = products_query.filter((Product.name.ilike(like)) | (Product.barcode.ilike(like)) | (Product.category.ilike(like)))
    products = products_query.order_by(Product.name).all()
    cart = _get_cart()
    cart_items = []
    product_ids = [int(prod_id) for prod_id in cart["items"].keys()]
    products_by_id = {}
    if product_ids:
        products_by_id = {p.id: p for p in scope_query_to_company(db.session.query(Product), Product).filter(Product.id.in_(product_ids), Product.active.is_(True)).all()}
    for prod_id, qty in cart["items"].items():
        product = products_by_id.get(int(prod_id))
        if product:
            cart_items.append({"product": product, "qty": qty})
    company = Company.query.filter_by(id=getattr(current_user, "company_id", None)).first()
    return render_template(
        "ventas/new.html",
        products=products,
        cart=cart_items,
        checkout_url=url_for("sales.checkout"),
        company_name=(company.name if company else "Mi comercio"),
    )


@bp.route("/carrito")
@tenant_required
def view_cart():
    return redirect(url_for("sales.checkout"))


@bp.route("/carrito/<int:product_id>", methods=["POST"])
@tenant_required
def add_to_cart(product_id):
    from app import Product, scope_query_to_company

    product = scope_query_to_company(Product.query.filter_by(id=product_id, active=True), Product).first_or_404()
    if not product.active:
        flash("No se pueden vender productos desactivados.", "warning")
        return redirect(url_for("sales.new_sale"))
    qty = _to_float(request.form.get("qty"), 1)
    if qty <= 0:
        flash("La cantidad debe ser mayor a cero.", "danger")
        return redirect(url_for("sales.new_sale"))
    if float(product.stock or 0) < qty:
        flash(f"Stock insuficiente para {product.name}. Disponible: {product.stock:g}.", "danger")
        return redirect(url_for("sales.new_sale"))
    cart = _get_cart()
    current_qty = _to_float(cart["items"].get(str(product_id), 0))
    cart["items"][str(product_id)] = min(current_qty + qty, float(product.stock or 0))
    _save_cart(cart)
    flash(f"{product.name} agregado al carrito.", "success")
    return redirect(request.referrer or url_for("sales.new_sale"))


@bp.route("/checkout", methods=["GET", "POST"])
@tenant_required
def checkout():
    from app import Client, scope_query_to_company

    if request.method == "POST":
        if _require_open_cash_session() is None:
            return redirect(url_for("cash.index"))
        return _create_sale_from_items(_get_cart().get("items", {}), request.form)

    if _require_open_cash_session() is None:
        return redirect(url_for("cash.index"))

    cart = _get_cart()
    if not cart.get("items"):
        flash("Carrito vacio.", "warning")
        return redirect(url_for("sales.new_sale"))
    try:
        products = _calculate_lines(cart["items"])
        sale_totals = calculate_sale_totals(
            [{"price": line["price"], "quantity": line["quantity"], "line_discount": line["discount"]} for line in products]
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("sales.new_sale"))
    return render_template(
        "ventas/checkout.html",
        products=products,
        clientes=scope_query_to_company(Client.query.filter_by(active=True), Client).order_by(Client.name).all(),
        subtotal=sale_totals["subtotal"],
        discount=sale_totals["line_discount_total"],
        tax_total=sale_totals["tax"],
        total=sale_totals["total"],
        checkout_token=_new_checkout_token(),
        checkout_url=url_for("sales.checkout"),
    )


@bp.route("/api/checkout", methods=["POST"])
@tenant_required
def api_checkout():
    payload = request.get_json(silent=True) or {}
    current_app.logger.info("[sales] carrito recibido (api_checkout): payload=%s", payload)
    incoming_tenant = (request.headers.get("X-Cart-Tenant") or "").strip()
    expected_tenant = _cart_tenant_key()
    if incoming_tenant != expected_tenant:
        current_app.logger.warning("[sales] tenant key invalido en checkout: incoming=%s expected=%s", incoming_tenant, expected_tenant)
        return jsonify({"error": "Carrito fuera de contexto de empresa o usuario."}), 409
    raw_items = payload.get("items", [])
    items = {}
    try:
        for item in raw_items:
            product_id = int(item.get("productId") or item.get("product_id") or 0)
            quantity = _to_float(item.get("quantity") or 0)
            if product_id > 0 and quantity > 0:
                items[str(product_id)] = quantity
    except (TypeError, ValueError):
        return jsonify({"error": "Datos de carrito invalidos."}), 400
    if not items:
        return jsonify({"error": "El carrito esta vacio."}), 400
    open_session = _require_open_cash_session(json_response=True)
    if isinstance(open_session, tuple):
        return open_session
    result = _create_sale_from_items(items, payload, json_response=True)
    return result


@bp.route("/api/mp-qr/create", methods=["POST"])
@tenant_required
def api_mp_qr_create():
    from app import Payment, db, scope_query_to_company

    payload = request.get_json(silent=True) or {}
    incoming_tenant = (request.headers.get("X-Cart-Tenant") or "").strip()
    expected_tenant = _cart_tenant_key()
    if incoming_tenant != expected_tenant:
        return jsonify({"error": "Carrito fuera de contexto de empresa o usuario."}), 409

    raw_items = payload.get("items", [])
    items = {}
    try:
        for item in raw_items:
            product_id = int(item.get("productId") or item.get("product_id") or 0)
            quantity = _to_float(item.get("quantity") or 0)
            if product_id > 0 and quantity > 0:
                items[str(product_id)] = quantity
    except (TypeError, ValueError):
        return jsonify({"error": "Datos de carrito invalidos."}), 400

    if not items:
        return jsonify({"error": "El carrito esta vacio."}), 400

    open_session = _require_open_cash_session(json_response=True)
    if isinstance(open_session, tuple):
        return open_session

    try:
        lines = _calculate_lines(items, lock_for_update=True)
        general_discount = _to_decimal(payload.get("descuento_general") or payload.get("general_discount"))
        surcharge = _to_decimal(payload.get("recargo") or payload.get("surcharge"))
        sale_totals = calculate_sale_totals(
            [{"price": line["price"], "quantity": line["quantity"], "line_discount": line["discount"]} for line in lines],
            general_discount=general_discount,
            surcharge=surcharge,
        )
        final_total = sale_totals["total"]
        currency = "ARS"
        company_id = getattr(current_user, "company_id", None)
        snapshot = _pos_qr_snapshot(
            payload.get("items", []),
            payload,
            total_amount=final_total,
            currency=currency,
        )

        existing_draft = _get_pos_qr_draft()
        if existing_draft and existing_draft.get("cart_hash") == snapshot["cart_hash"]:
            draft_payment = Payment.query.filter_by(id=int(existing_draft.get("payment_db_id") or 0), company_id=company_id).first()
            if draft_payment and (draft_payment.status or "").lower() == "pending":
                return jsonify({
                    "status": "reused",
                    "payment_id": draft_payment.id,
                    "status_url": url_for("sales.api_mp_qr_status", draft_id=draft_payment.id),
                    "finalize_url": url_for("sales.api_mp_qr_finalize", draft_id=draft_payment.id),
                    "total": float(draft_payment.amount or final_total),
                    "currency": draft_payment.currency or currency,
                    "checkout_url": existing_draft.get("checkout_url") or "",
                    "qr_data_uri": existing_draft.get("qr_data_uri") or "",
                    "status_label": "Pendiente de aprobación",
                })

        draft_payment = Payment(
            payment_id=None,
            preference_id="",
            external_reference="",
            company_id=company_id,
            user_id=current_user.id,
            amount=final_total,
            currency=currency,
            status="pending",
            payment_method="QR Mercado Pago",
            provider="mercadopago_pos",
            reference="pos_draft",
            payload_json=json.dumps({
                "flow": "pos_sale",
                "snapshot": snapshot,
            }, ensure_ascii=False),
        )
        db.session.add(draft_payment)
        db.session.flush()
        external_reference = (
            f"flow:pos_sale|draft_payment_id:{draft_payment.id}|company_id:{company_id}|user_id:{current_user.id}|"
            f"cart_hash:{snapshot['cart_hash']}"
        )
        oauth_service = MercadoPagoOAuthService()
        try:
            access_token = oauth_service.ensure_access_token(company_id=company_id)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        mp_service = MercadoPagoService()
        preference = mp_service.create_pos_checkout_preference(
            title=f"StockArmobile POS - {company_id}",
            amount=float(final_total),
            currency=currency,
            external_reference=external_reference,
            company_id=company_id,
            user_id=current_user.id,
            access_token=access_token,
            metadata={
                "draft_payment_id": draft_payment.id,
                "cart_hash": snapshot["cart_hash"],
                "total_amount": float(final_total),
            },
        )
        draft_payment.preference_id = preference.get("id")
        draft_payment.external_reference = external_reference
        draft_payment.reference = f"pos_draft:{draft_payment.id}"
        db.session.commit()

        checkout_url = preference.get("init_point") or preference.get("sandbox_init_point") or ""
        qr_preview = {
            "qr_data_uri": _qr_data_uri(checkout_url) if checkout_url else "",
        }
        _set_pos_qr_draft({
            "payment_db_id": draft_payment.id,
            "preference_id": preference.get("id"),
            "checkout_url": checkout_url,
            "qr_data_uri": qr_preview.get("qr_data_uri") or "",
            "cart_hash": snapshot["cart_hash"],
        })
        return jsonify({
            "status": "created",
            "payment_id": draft_payment.id,
            "status_url": url_for("sales.api_mp_qr_status", draft_id=draft_payment.id),
            "finalize_url": url_for("sales.api_mp_qr_finalize", draft_id=draft_payment.id),
            "checkout_url": checkout_url,
            "qr_data_uri": qr_preview.get("qr_data_uri") or "",
            "total": float(final_total),
            "currency": currency,
            "status_label": "Pendiente de aprobación",
        })
    except Exception as exc:
        current_app.logger.exception("Error creando QR Mercado Pago POS: %s", exc)
        return jsonify({"error": "No se pudo generar el QR de Mercado Pago."}), 400


@bp.route("/api/mp-qr/status", methods=["GET"])
@tenant_required
def api_mp_qr_status():
    from app import Payment

    draft_id = request.args.get("draft_id", type=int)
    if not draft_id:
        return jsonify({"error": "draft_id requerido"}), 400
    payment = Payment.query.filter_by(id=draft_id, company_id=getattr(current_user, "company_id", None)).first_or_404()
    payload = json.loads(payment.payload_json) if payment.payload_json else {}
    approved_at = payment.paid_at or payment.updated_at
    status = (payment.status or "pending").lower()
    can_process = status == "approved"
    return jsonify({
        "payment_id": payment.id,
        "status": status,
        "status_label": "Pago recibido" if status == "approved" else "Esperando pago" if status == "pending" else status.title(),
        "amount": float(payment.amount or 0),
        "currency": payment.currency or "ARS",
        "payment_method": payment.payment_method or "QR Mercado Pago",
        "operation_number": payment.payment_id or payment.preference_id or f"pos-{payment.id}",
        "approved_at": approved_at.strftime("%Y-%m-%d %H:%M") if approved_at else None,
        "can_process_sale": can_process,
        "sale_id": payload.get("sale_id"),
        "finalize_url": url_for("sales.api_mp_qr_finalize", draft_id=payment.id),
    })


@bp.route("/api/mp-qr/finalize", methods=["POST"])
@tenant_required
def api_mp_qr_finalize():
    from app import Payment, Sale, db, record_audit, scope_query_to_company

    payload = request.get_json(silent=True) or {}
    draft_id = request.args.get("draft_id", type=int) or _to_float(payload.get("draft_id"), 0)
    draft_id = int(draft_id) if draft_id else None
    if not draft_id:
        return jsonify({"error": "draft_id requerido"}), 400

    open_session = _require_open_cash_session(json_response=True)
    if isinstance(open_session, tuple):
        return open_session

    payment = Payment.query.filter_by(id=draft_id, company_id=getattr(current_user, "company_id", None)).first_or_404()
    if (payment.status or "").lower() != "approved":
        return jsonify({"error": "El pago aun no fue aprobado."}), 409

    draft_payload = json.loads(payment.payload_json) if payment.payload_json else {}
    existing_sale_id = draft_payload.get("sale_id")
    if existing_sale_id:
        existing_sale = scope_query_to_company(Sale.query, Sale).filter(Sale.id == int(existing_sale_id)).first()
        if existing_sale:
            return jsonify({"sale_id": existing_sale.id, "redirect_url": url_for("sales.success", sale_id=existing_sale.id)})

    snapshot = draft_payload.get("snapshot") or {}
    items = snapshot.get("items") or []
    if not items:
        return jsonify({"error": "No hay items para procesar."}), 400

    sale_payload = {
        "client_id": snapshot.get("client_id") or "",
        "checkout_token": snapshot.get("checkout_token") or "",
        "metodo_pago": "QR Mercado Pago",
        "payment_method": "QR Mercado Pago",
        "monto_pago": payment.amount,
        "document_type": snapshot.get("document_type") or "venta",
        "requiere_comprobante": snapshot.get("requiere_comprobante"),
        "tipo_comprobante": snapshot.get("tipo_comprobante"),
        "observacion_comprobante": snapshot.get("observacion_comprobante"),
        "note": snapshot.get("note") or "",
        "qr_reference": payment.payment_id or payment.preference_id or f"pos-{payment.id}",
        "status": "confirmada",
        "general_discount": snapshot.get("descuento_general") or 0,
        "surcharge": snapshot.get("recargo") or 0,
    }

    result = _create_sale_from_items({str(item["productId"]): item["quantity"] for item in items}, sale_payload, json_response=True)
    if hasattr(result, "json"):
        data = result.get_json() if hasattr(result, "get_json") else None
    else:
        data = result
    if not isinstance(data, dict) or not data.get("sale_id"):
        return result

    sale_id = data["sale_id"]
    payment.reference = f"sale_id:{sale_id}"
    payment.payload_json = json.dumps({**draft_payload, "sale_id": sale_id}, ensure_ascii=False)
    record_audit(action="pos_qr_finalize", entity="sale", entity_id=sale_id, detail=f"POS QR finalizado con pago {payment.payment_id or payment.preference_id}")
    db.session.commit()
    _set_pos_qr_draft(None)
    return jsonify({"sale_id": sale_id, "redirect_url": data.get("redirect_url")})


def _create_sale_from_items(items, data, json_response=False):
    from app import CashMovement, Client, Sale, SaleItem, db, record_audit, scope_query_to_company

    sale = None
    final_total = Decimal("0.00")
    try:
        current_app.logger.info("[sales] carrito recibido (_create_sale_from_items): items=%s json_response=%s", items, json_response)
        cash_session = _require_open_cash_session(json_response=json_response)
        if cash_session is None:
            return redirect(url_for("cash.index"))
        if isinstance(cash_session, tuple):
            return cash_session

        checkout_token = _sanitize_checkout_token(data.get("checkout_token") or data.get("checkoutToken"))
        company_id = getattr(current_user, "company_id", None)
        if checkout_token:
            existing_sale = scope_query_to_company(Sale.query, Sale).filter(Sale.client_txn_id == checkout_token).first()
            if existing_sale is not None:
                if json_response:
                    return jsonify({"sale_id": existing_sale.id, "redirect_url": url_for("sales.success", sale_id=existing_sale.id)})
                return redirect(url_for("sales.success", sale_id=existing_sale.id))

        lines = _calculate_lines(items, lock_for_update=True)
        general_discount = _to_decimal(data.get("descuento_general") or data.get("general_discount"))
        surcharge = _to_decimal(data.get("recargo") or data.get("surcharge"))
        sale_totals = calculate_sale_totals(
            [{"price": line["price"], "quantity": line["quantity"], "line_discount": line["discount"]} for line in lines],
            general_discount=general_discount,
            surcharge=surcharge,
        )
        general_discount = sale_totals["general_discount"]
        surcharge = sale_totals["surcharge"]
        subtotal = sale_totals["subtotal"]
        discount = sale_totals["line_discount_total"]
        tax_total = sale_totals["tax"]
        final_total = sale_totals["total"]

        payment_primary_method = data.get("metodo_pago") or data.get("payment_method") or "EFECTIVO"
        payment_secondary_method = data.get("metodo_pago_2") or data.get("secondary_payment_method") or ""
        payment_split = normalize_payment_split(
            total_amount=final_total,
            primary_method=payment_primary_method,
            secondary_method=payment_secondary_method,
            primary_amount=(data.get("monto_pago") or data.get("paid_amount")),
            secondary_amount=(data.get("monto_pago_2") or data.get("secondary_paid_amount")),
        )

        client_id = data.get("client_id") or data.get("cliente_id") or None
        client = scope_query_to_company(Client.query.filter_by(id=int(client_id), active=True), Client).first() if client_id else None
        current_app.logger.info("[sales] cliente recibido: client_id=%s resolved_client=%s", client_id, getattr(client, "id", None))
        current_app.logger.info(
            "[sales] total calculado: subtotal=%s descuento_lineas=%s descuento_general=%s recargo=%s total=%s",
            str(subtotal),
            str(discount),
            str(general_discount),
            str(surcharge),
            str(final_total),
        )
        current_app.logger.info("[sales] creando Sale")
        requiere_comprobante, tipo_comprobante, observacion_comprobante = _resolve_comprobante_payload(data)
        if _requires_identified_client(data, requiere_comprobante, tipo_comprobante) and client is None:
            raise ValueError("Para ese comprobante debés seleccionar un cliente.")
        sale = Sale(
            customer=client.name if client else current_user.username,
            subtotal=subtotal,
            discount=discount + general_discount,
            tax=tax_total,
            total_amount=final_total,
            payment_method=payment_split["primary_method"],
            secondary_payment_method=payment_split["secondary_method"],
            paid_amount=payment_split["primary_amount"],
            secondary_paid_amount=payment_split["secondary_amount"],
            surcharge=surcharge,
            client_txn_id=checkout_token,
            document_type=data.get("document_type") or data.get("tipo_comprobante") or "venta",
            requiere_comprobante=requiere_comprobante,
            tipo_comprobante=tipo_comprobante,
            observacion_comprobante=observacion_comprobante or None,
            comprobante_emitido=False,
            status=data.get("status") or "confirmada",
            qr_reference=data.get("qr_reference"),
            note=data.get("note"),
            client_id=client.id if client else None,
            seller_id=current_user.id,
            company_id=company_id,
            cash_session_id=cash_session.id,
            date=utcnow(),
        )
        payment_breakdown = sale_payment_breakdown_from_values(
            total_amount=final_total,
            primary_method=payment_split["primary_method"],
            secondary_method=payment_split["secondary_method"],
            primary_amount=payment_split["primary_amount"],
            secondary_amount=payment_split["secondary_amount"],
        )
        db.session.add(sale)
        db.session.flush()
        current_app.logger.info("[sales] Sale creada en flush: sale_id=%s", sale.id)

        cash_amount = to_decimal(payment_breakdown.get("efectivo", 0))
        if cash_amount > 0:
            db.session.add(
                CashMovement(
                    session_id=cash_session.id,
                    user_id=current_user.id,
                    company_id=company_id,
                    sale_id=sale.id,
                    movement_type="ingreso",
                    category="venta",
                    amount=cash_amount,
                    description=f"Venta #{sale.id}",
                )
            )

        for idx, line in enumerate(lines):
            product = line["product"]
            calculated_line = sale_totals["lines"][idx]
            current_app.logger.info(
                "[sales] actualizando stock: product_id=%s stock_actual=%s cantidad=%s",
                product.id,
                str(product.stock),
                str(line["quantity"]),
            )
            product.stock -= line["quantity"]
            current_app.logger.info("[sales] creando SaleItem: sale_id=%s product_id=%s", sale.id, product.id)
            db.session.add(
                SaleItem(
                    sale_id=sale.id,
                    product_id=product.id,
                    quantity=line["quantity"],
                    price=line["price"],
                    cost_price=_to_decimal(product.cost_price),
                    discount=calculated_line["final_discount"],
                )
            )

        current_app.logger.info("[sales] commit de transaccion de venta: sale_id=%s", sale.id)
        db.session.commit()
        current_app.logger.info("[sales] commit exitoso: sale_id=%s", sale.id)
        try:
            record_audit(action="sale_create", entity="sale", entity_id=sale.id, detail=f"Venta registrada total={final_total}")
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("[sales] no se pudo persistir auditoria post-venta: sale_id=%s", sale.id)
        session.pop(_cart_key(), None)
    except IntegrityError as exc:
        db.session.rollback()
        current_app.logger.exception("[sales] integridad al crear venta")
        token = _sanitize_checkout_token(data.get("checkout_token") or data.get("checkoutToken"))
        if token:
            from app import Sale, scope_query_to_company

            existing_sale = scope_query_to_company(Sale.query, Sale).filter(Sale.client_txn_id == token).first()
            if existing_sale is not None:
                if json_response:
                    return jsonify({"sale_id": existing_sale.id, "redirect_url": url_for("sales.success", sale_id=existing_sale.id)})
                return redirect(url_for("sales.success", sale_id=existing_sale.id))
        if json_response:
            return jsonify({"error": "No se pudo completar la venta. Revisa los datos e intenta nuevamente."}), 400
        flash("No se pudo completar la venta por un conflicto de concurrencia.", "danger")
        return redirect(url_for("sales.new_sale"))
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception("[sales] error creando venta")
        try:
            record_audit(action="sale_error", entity="sale", detail=f"Error al crear venta: {exc}")
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("[sales] no se pudo persistir auditoria de error")
        if json_response:
            message = str(exc)
            safe_message = message if isinstance(exc, ValueError) else "No se pudo completar la venta. Revisa los datos e intenta nuevamente."
            return jsonify({"error": safe_message}), 400
        flash(f"No se pudo completar la venta: {exc}", "danger")
        return redirect(url_for("sales.new_sale"))

    if json_response:
        return jsonify({"sale_id": sale.id, "redirect_url": url_for("sales.success", sale_id=sale.id)})
    flash(f"Venta #{sale.id} realizada con exito. Total: ${final_total:.2f}", "success")
    return redirect(url_for("sales.success", sale_id=sale.id))


@bp.route("/success/<int:sale_id>")
@tenant_required
def success(sale_id):
    from app import Sale, SaleItem, scope_query_to_company

    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)), Sale).filter(Sale.id == sale_id).first_or_404()
    ticket_html = _ticket_text(sale)
    return render_template("ventas/success.html", sale=sale, ticket_html=ticket_html, pdf_url=url_for("qr_labels.generate_pdf_ticket", sale_id=sale.id))


@bp.route("/<int:sale_id>")
@tenant_required
def view_sale(sale_id):
    from app import Sale, SaleItem, scope_query_to_company

    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)), Sale).filter(Sale.id == sale_id).first_or_404()
    return render_template("ventas/view.html", sale=sale, items=sale.items)


@bp.route("/<int:sale_id>/edit", methods=["GET", "POST"])
@tenant_required
def edit(sale_id):
    from app import Client, Sale, db, scope_query_to_company

    sale = scope_query_to_company(Sale.query, Sale).filter(Sale.id == sale_id).first_or_404()
    clients = scope_query_to_company(Client.query.filter_by(active=True), Client).order_by(Client.name).all()
    if request.method == "POST":
        client_id = request.form.get("client_id") or None
        client = scope_query_to_company(Client.query.filter_by(id=int(client_id), active=True), Client).first() if client_id else None
        sale.client_id = client.id if client else None
        sale.customer = client.name if client else (request.form.get("customer") or sale.customer)
        sale.payment_method = request.form.get("payment_method") or sale.payment_method
        sale.note = request.form.get("note") or None
        db.session.commit()
        flash("Venta actualizada correctamente.", "success")
        return redirect(url_for("sales.view_sale", sale_id=sale.id))
    return render_template("ventas/edit_sale.html", sale=sale, clients=clients)


@bp.route("/<int:sale_id>/comprobante-emitido", methods=["POST"])
@tenant_required
def mark_comprobante_issued(sale_id):
    from app import Sale, db, scope_query_to_company

    sale = scope_query_to_company(Sale.query, Sale).filter(Sale.id == sale_id).first_or_404()
    if not bool(sale.requiere_comprobante):
        flash("La venta no tiene comprobante solicitado.", "warning")
        return redirect(url_for("sales.view_sale", sale_id=sale.id))
    sale.comprobante_emitido = True
    db.session.commit()
    flash("Comprobante marcado como emitido.", "success")
    return redirect(url_for("sales.view_sale", sale_id=sale.id))


@bp.route("/<int:sale_id>/delete", methods=["POST"])
@tenant_required
def delete_sale(sale_id):
    from app import CashMovement, Product, Sale, SaleItem, db, record_audit, scope_query_to_company

    if getattr(current_user, "role", None) != "admin":
        abort(403)

    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)), Sale).filter(Sale.id == sale_id).first_or_404()

    CashMovement.query.filter_by(sale_id=sale.id).delete(synchronize_session=False)
    for item in sale.items:
        product = item.product or scope_query_to_company(db.session.query(Product), Product).filter(Product.id == item.product_id).first()
        if product is not None:
            product.stock = (product.stock or 0) + (item.quantity or 0)

    record_audit(
        action="sale_delete",
        entity="sale",
        entity_id=sale.id,
        detail=f"Venta eliminada y stock restituido. Total={sale.total_amount}",
    )
    db.session.delete(sale)
    db.session.commit()
    flash("Venta eliminada correctamente y stock restaurado.", "success")
    return redirect(request.referrer or url_for("company_billing.company_settings", panel="stats"))


@bp.route("/<int:sale_id>/imprimir-ticket")
@tenant_required
def print_ticket(sale_id):
    from app import Sale, SaleItem, scope_query_to_company

    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)), Sale).filter(Sale.id == sale_id).first_or_404()
    response = make_response(_ticket_text(sale))
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    return response


@bp.route("/<int:sale_id>/ticket")
@tenant_required
def thermal_ticket(sale_id):
    from app import Sale, SaleItem, scope_query_to_company

    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)), Sale).filter(Sale.id == sale_id).first_or_404()
    ticket_brand = _ticket_brand_name()
    return render_template("ventas/ticket.html", sale=sale, rows=_ticket_rows(sale), ticket_text=_ticket_text(sale, ticket_brand=ticket_brand), ticket_brand=ticket_brand)


@bp.route("/<int:sale_id>/print-thermal", methods=["POST"])
@tenant_required
def print_thermal_ticket(sale_id):
    from app import Company, Sale, SaleItem, scope_query_to_company
    from services.thermal_printer_service import ThermalPrinterService

    company = Company.query.filter_by(id=getattr(current_user, "company_id", None)).first_or_404()
    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)), Sale).filter(Sale.id == sale_id).first_or_404()
    result = ThermalPrinterService().print_sale_ticket(company, sale)
    if result.printed:
        flash("Ticket enviado a la impresora térmica.", "success")
    else:
        flash("No hay impresora térmica configurada o no se pudo imprimir. Se abrió el ticket web.", "warning")
    return redirect(url_for("sales.thermal_ticket", sale_id=sale.id))


@bp.route("/exportar-ventas/csv")
@tenant_required
def export_sales_csv():
    from app import Sale, scope_query_to_company

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Cliente", "Subtotal", "Descuento", "Total", "Iva", "Fecha"])
    for sale in scope_query_to_company(Sale.query, Sale).order_by(Sale.date.desc()).all():
        writer.writerow([sale.id, sale.customer or "", f"{sale.subtotal:.2f}", f"{sale.discount:.2f}", f"{sale.total_amount:.2f}", f"{sale.tax:.2f}", f"{sale.date:%Y-%m-%d}"])
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="ventas_{utcnow():%Y%m%d}.csv"'
    return response


@bp.route("/api/ventas/<int:sale_id>")
@tenant_required
def api_sale(sale_id):
    from app import Sale, SaleItem, scope_query_to_company

    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)), Sale).filter(Sale.id == sale_id).first_or_404()
    return jsonify(
        {
            "id": sale.id,
            "customer": sale.customer,
            "date": sale.date.isoformat() if sale.date else None,
            "subtotal": sale.subtotal,
            "discount": sale.discount,
            "tax": sale.tax,
            "total_amount": sale.total_amount,
            "items": [
                {"id": i.id, "product_name": i.product.name if i.product else "", "quantity": i.quantity, "price": i.price, "subtotal": i.total_amount}
                for i in sale.items
            ],
        }
    )


@bp.route("/api/recent")
@tenant_required
def api_recent_sales():
    from app import Sale, scope_query_to_company

    sales = scope_query_to_company(Sale.query, Sale).order_by(Sale.date.desc()).limit(20).all()
    return jsonify(
        {
            "sales": [
                {
                    "id": sale.id,
                    "customer": sale.customer or "",
                    "date": sale.date.isoformat() if sale.date else None,
                    "total_amount": float(sale.total_amount or 0),
                    "requiere_comprobante": bool(getattr(sale, "requiere_comprobante", False)),
                    "tipo_comprobante": getattr(sale, "tipo_comprobante", None),
                    "comprobante_emitido": bool(getattr(sale, "comprobante_emitido", False)),
                }
                for sale in sales
            ]
        }
    )


def _ticket_brand_name():
    from app import Company

    fallback = "STOCK ARMOBILE"
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        return fallback

    company = Company.query.filter_by(id=company_id).first()
    if company is None:
        return fallback

    settings = {}
    raw = getattr(company, "printer_settings_json", None)
    if raw:
        try:
            settings = json.loads(raw)
        except Exception:
            settings = {}

    name = (settings.get("ticket_name") or settings.get("printer_name") or getattr(company, "name", "") or fallback).strip()
    return name[:120] or fallback


def _ticket_text(sale, ticket_brand=None):
    brand = (ticket_brand or _ticket_brand_name() or "STOCK ARMOBILE").strip()
    lines = [f"{brand} - TICKET DE VENTA", "-" * 32, f"Venta: #{sale.id}", f"Fecha: {sale.date:%Y-%m-%d %H:%M}"]
    if sale.customer:
        lines.append(f"Cliente: {sale.customer}")
    lines.append("-" * 32)
    for item in sale.items:
        name = item.product.name if item.product else f"Producto {item.product_id}"
        lines.append(f"{name}: ${item.price:.2f} x {item.quantity} = ${item.total_amount:.2f}")
    if sale.note:
        lines.extend(["-" * 32, f"Obs.: {sale.note}"])
    lines.extend(["-" * 32, f"Subtotal: ${sale.subtotal:.2f}", f"Descuento: -${sale.discount:.2f}", f"Impuestos: ${sale.tax:.2f}", "=" * 32, f"TOTAL: ${sale.total_amount:.2f}", "Gracias por su compra!"])
    return "\n".join(lines)


def _ticket_rows(sale):
    return [
        {
            "name": item.product.name if item.product else f"Producto {item.product_id}",
            "quantity": item.quantity,
            "price": item.price,
            "total": item.total_amount,
        }
        for item in sale.items
    ]


def _normalize_phone(value):
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())


def _is_valid_whatsapp_phone(value):
    normalized = _normalize_phone(value)
    return 8 <= len(normalized) <= 15


def _build_whatsapp_link(sale, phone):
    text = quote(_ticket_text_for_whatsapp(sale))
    return f"https://wa.me/{phone}?text={text}"


def _ticket_text_for_whatsapp(sale):
    brand = _ticket_brand_name()
    lines = [f"{brand} - Ticket de compra", f"Venta #{sale.id}", f"Fecha: {sale.date:%Y-%m-%d %H:%M}"]
    if sale.customer:
        lines.append(f"Cliente: {sale.customer}")
    lines.append("------------------------------")
    for item in sale.items:
        name = item.product.name if item.product else f"Producto {item.product_id}"
        lines.append(f"{name}: ${item.price:.2f} x {item.quantity} = ${item.total_amount:.2f}")
    lines.extend([
        "------------------------------",
        f"Subtotal: ${sale.subtotal:.2f}",
        f"Descuento: -${sale.discount:.2f}",
        f"Impuestos: ${sale.tax:.2f}",
        f"Total: ${sale.total_amount:.2f}",
        "Gracias por su compra!",
    ])
    return "\n".join(lines)


@bp.route("/<int:sale_id>/share-whatsapp", methods=["GET", "POST"])
@tenant_required
def share_whatsapp(sale_id):
    from app import Sale, SaleItem, db, scope_query_to_company

    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product), selectinload(Sale.client)), Sale).filter(Sale.id == sale_id).first_or_404()
    raw_phone = (sale.client.whatsapp if getattr(sale, "client", None) else "") or (sale.client.phone if getattr(sale, "client", None) else "")
    phone = _normalize_phone(raw_phone)

    if request.method == "POST":
        phone_input = (request.form.get("whatsapp_phone") or "").strip()
        action = (request.form.get("phone_action") or "send_once").strip()
        normalized_phone = _normalize_phone(phone_input)

        if not _is_valid_whatsapp_phone(normalized_phone):
            flash("Numero de WhatsApp invalido. Ingresa entre 8 y 15 digitos.", "danger")
            return render_template(
                "ventas/whatsapp_dialog.html",
                sale=sale,
                entered_phone=phone_input,
                selected_action=action,
                can_save_phone=bool(getattr(sale, "client", None)),
            )

        if action == "save_and_send":
            if not getattr(sale, "client", None):
                flash("No se puede guardar el numero porque esta venta no tiene cliente asociado.", "warning")
                return render_template(
                    "ventas/whatsapp_dialog.html",
                    sale=sale,
                    entered_phone=phone_input,
                    selected_action=action,
                    can_save_phone=False,
                )
            sale.client.whatsapp = normalized_phone
            db.session.commit()

        return redirect(_build_whatsapp_link(sale, normalized_phone))

    if _is_valid_whatsapp_phone(phone):
        return redirect(_build_whatsapp_link(sale, phone))

    return render_template(
        "ventas/whatsapp_dialog.html",
        sale=sale,
        entered_phone="",
        selected_action="send_once",
        can_save_phone=bool(getattr(sale, "client", None)),
    )

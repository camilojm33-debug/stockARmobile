"""Blueprint de ventas: carrito, checkout, historial y tickets."""

import csv
from decimal import Decimal, InvalidOperation
from datetime import datetime
from io import StringIO
from urllib.parse import quote

from flask import Blueprint, abort, current_app, flash, jsonify, make_response, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload
from app import tenant_required, utcnow

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


def _cart_key():
    company_id = getattr(current_user, "company_id", None) or "global"
    return f"cart_{company_id}_{current_user.id}"


def _cart_tenant_key():
    company_id = getattr(current_user, "company_id", None) or "global"
    return f"{company_id}:{current_user.id}"


def _get_cart():
    cart = session.get(_cart_key(), {"items": {}})
    cart.setdefault("items", {})
    return cart


def _save_cart(cart):
    session[_cart_key()] = cart
    session.modified = True


def _calculate_lines(items):
    from app import Product, db, scope_query_to_company

    lines = []
    subtotal = Decimal("0.00")
    discount_total = Decimal("0.00")
    product_ids = [int(prod_id) for prod_id in items.keys()]
    products = {
        product.id: product
        for product in scope_query_to_company(db.session.query(Product), Product).filter(Product.id.in_(product_ids), Product.active.is_(True)).all()
    }
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
        subtotal += line_subtotal
        discount_total += line_discount
        lines.append({"product": product, "quantity": qty, "price": unit_price, "discount": line_discount})
    taxable = max(subtotal - discount_total, Decimal("0.00"))
    tax = Decimal("0.00")
    total = taxable
    current_app.logger.info(
        "[sales] cantidades y totales de lineas calculados: cantidades=%s subtotal=%s descuento=%s tax=%s total=%s",
        [{"product_id": line["product"].id, "quantity": line["quantity"]} for line in lines],
        str(subtotal),
        str(discount_total),
        str(tax),
        str(total),
    )
    return lines, subtotal, discount_total, tax, total


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
        return _create_sale_from_items(_get_cart().get("items", {}), request.form)

    cart = _get_cart()
    if not cart.get("items"):
        flash("Carrito vacio.", "warning")
        return redirect(url_for("sales.new_sale"))
    try:
        products, subtotal, discount, tax_total, total = _calculate_lines(cart["items"])
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("sales.new_sale"))
    return render_template(
        "ventas/checkout.html",
        products=products,
        clientes=scope_query_to_company(Client.query.filter_by(active=True), Client).order_by(Client.name).all(),
        subtotal=subtotal,
        discount=discount,
        tax_total=tax_total,
        total=total,
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
    result = _create_sale_from_items(items, payload, json_response=True)
    return result


def _create_sale_from_items(items, data, json_response=False):
    from app import Client, Sale, SaleItem, db, record_audit, scope_query_to_company

    sale = None
    try:
        current_app.logger.info("[sales] carrito recibido (_create_sale_from_items): items=%s json_response=%s", items, json_response)
        lines, subtotal, discount, tax_total, final_total = _calculate_lines(items)
        general_discount = _to_decimal(data.get("descuento_general") or data.get("general_discount"))
        surcharge = _to_decimal(data.get("recargo") or data.get("surcharge"))
        taxable = max(subtotal - discount - general_discount, Decimal("0.00"))
        tax_total = Decimal("0.00")
        final_total = taxable + surcharge
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
        sale = Sale(
            customer=client.name if client else current_user.username,
            subtotal=subtotal,
            discount=discount + general_discount,
            tax=tax_total,
            total_amount=final_total,
            payment_method=data.get("metodo_pago") or data.get("payment_method"),
            secondary_payment_method=data.get("metodo_pago_2") or data.get("secondary_payment_method"),
            paid_amount=_to_decimal(data.get("monto_pago") or data.get("paid_amount") or final_total),
            secondary_paid_amount=_to_decimal(data.get("monto_pago_2") or data.get("secondary_paid_amount")),
            surcharge=surcharge,
            document_type=data.get("document_type") or data.get("tipo_comprobante") or "venta",
            status=data.get("status") or "confirmada",
            qr_reference=data.get("qr_reference"),
            note=data.get("note"),
            client_id=client.id if client else None,
            seller_id=current_user.id,
            company_id=getattr(current_user, "company_id", None),
            date=utcnow(),
        )
        db.session.add(sale)
        db.session.flush()
        current_app.logger.info("[sales] Sale creada en flush: sale_id=%s", sale.id)

        for line in lines:
            product = line["product"]
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
                    discount=line["discount"],
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


@bp.route("/<int:sale_id>/delete", methods=["POST"])
@tenant_required
def delete_sale(sale_id):
    from app import Product, Sale, SaleItem, db, record_audit, scope_query_to_company

    if getattr(current_user, "role", None) != "admin":
        abort(403)

    sale = scope_query_to_company(Sale.query.options(selectinload(Sale.items).selectinload(SaleItem.product)), Sale).filter(Sale.id == sale_id).first_or_404()

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
                }
                for sale in sales
            ]
        }
    )


def _ticket_text(sale):
    lines = ["STOCK ARMOBILE - TICKET DE VENTA", "-" * 32, f"Venta: #{sale.id}", f"Fecha: {sale.date:%Y-%m-%d %H:%M}"]
    if sale.customer:
        lines.append(f"Cliente: {sale.customer}")
    lines.append("-" * 32)
    for item in sale.items:
        name = item.product.name if item.product else f"Producto {item.product_id}"
        lines.append(f"{name}: ${item.price:.2f} x {item.quantity} = ${item.total_amount:.2f}")
    lines.extend(["-" * 32, f"Subtotal: ${sale.subtotal:.2f}", f"Descuento: -${sale.discount:.2f}", f"Impuestos: ${sale.tax:.2f}", "=" * 32, f"TOTAL: ${sale.total_amount:.2f}", "Gracias por su compra!"])
    return "\n".join(lines)


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
    lines = [f"Ticket de compra - Venta #{sale.id}", f"Fecha: {sale.date:%Y-%m-%d %H:%M}"]
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

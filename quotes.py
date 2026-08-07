"""Blueprint de presupuestos integrado al flujo de ventas."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from reportlab.lib.utils import ImageReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy.orm import selectinload
import qrcode

from app import tenant_required, utcnow
from stockarmobile.enums import QuoteStatus
from stockarmobile.helpers.dates import parse_date_yyyy_mm_dd
from services.sales_calculation_service import calculate_sale_totals, to_decimal
from services.whatsapp_share_service import build_whatsapp_share_url

bp = Blueprint("quotes", __name__)

QUOTE_STATUS_OPTIONS = [
    QuoteStatus.BORRADOR.value,
    QuoteStatus.ENVIADO.value,
    QuoteStatus.PENDIENTE.value,
    QuoteStatus.APROBADO.value,
    QuoteStatus.RECHAZADO.value,
    QuoteStatus.VENCIDO.value,
    QuoteStatus.CONVERTIDO.value,
    QuoteStatus.ANULADO.value,
]
QUOTE_PUBLIC_TOKEN_SALT = "quotes-public-share-v1"
QUOTE_PUBLIC_TOKEN_MAX_AGE_SECONDS = 60 * 60 * 24 * 30


def _json_dict(raw_value):
    raw = (raw_value or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _company_quotes_enabled(company):
    if company is None:
        return False
    preferences = _json_dict(getattr(company, "preferences_json", None))
    return bool(preferences.get("quotes_module_enabled"))


def _next_quote_number(company_id):
    from app import Quote, db, scope_query_to_company

    query = scope_query_to_company(db.session.query(Quote.number), Quote).filter(Quote.company_id == company_id)
    max_sequence = 0
    for (number,) in query.all():
        raw = str(number or "").strip().upper()
        if raw.startswith("P-"):
            raw = raw[2:]
        if raw.isdigit():
            max_sequence = max(max_sequence, int(raw))
    return f"P-{max_sequence + 1:08d}"


def _quote_permission(permission_key: str):
    role = (getattr(current_user, "role", None) or "").strip().lower()
    if role in {"admin", "superadmin"}:
        return True
    raw_permissions = (getattr(current_user, "permissions_json", None) or "").strip()
    if not raw_permissions:
        return False
    try:
        payload = json.loads(raw_permissions)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, list):
        return False
    normalized = {str(item).strip().lower() for item in payload if str(item).strip()}
    return permission_key in normalized


def _require_quotes_module():
    from app import Company

    company = Company.query.filter_by(id=getattr(current_user, "company_id", None)).first()
    return company


def _require_quote_permission(permission_key: str):
    # Presupuestos queda habilitado para todos los usuarios autenticados de empresa.
    return None


def _can_view_other_quotes():
    return _quote_permission("quotes_view_other_sellers")


def _can_modify_prices():
    return _quote_permission("quotes_modify_prices")


def _can_apply_discounts():
    return _quote_permission("quotes_apply_discounts")


def _can_anulate():
    return _quote_permission("quotes_anulate")


def _can_share_whatsapp():
    return _quote_permission("quotes_share_whatsapp")


def _can_send_email():
    return _quote_permission("quotes_email")


def _can_duplicate():
    return _quote_permission("quotes_duplicate")


def _can_download_pdf():
    return _quote_permission("quotes_download_pdf") or _quote_permission("quotes_print")


def _normalize_status(raw_status):
    status = (raw_status or "BORRADOR").strip().upper()
    return status if status in QUOTE_STATUS_OPTIONS else "BORRADOR"


def _parse_date(value):
    return parse_date_yyyy_mm_dd(value)


def _parse_items(payload):
    from app import Product, QuoteItem, db, scope_query_to_company

    items = []
    total_lines = payload.get("items", [])
    if not isinstance(total_lines, list):
        return items

    product_ids = []
    for raw_item in total_lines:
        if not isinstance(raw_item, dict):
            continue
        try:
            product_id = int(raw_item.get("product_id") or raw_item.get("productId") or 0)
        except (TypeError, ValueError):
            product_id = 0
        if product_id > 0:
            product_ids.append(product_id)

    products = {}
    if product_ids:
        products = {
            product.id: product
            for product in scope_query_to_company(db.session.query(Product), Product)
            .filter(Product.id.in_(sorted(set(product_ids))))
            .all()
        }

    for raw_item in total_lines:
        if not isinstance(raw_item, dict):
            continue
        try:
            quantity = to_decimal(raw_item.get("quantity"))
            unit_price = to_decimal(raw_item.get("unit_price") or raw_item.get("price"))
            discount = to_decimal(raw_item.get("discount"))
            product_id = int(raw_item.get("product_id") or raw_item.get("productId") or 0)
        except (TypeError, ValueError):
            raise ValueError("Hay una línea de presupuesto inválida.")
        if quantity <= 0:
            raise ValueError("La cantidad debe ser mayor a cero.")
        if unit_price < 0 or discount < 0:
            raise ValueError("Los importes del presupuesto no pueden ser negativos.")

        product = products.get(product_id)
        description = (raw_item.get("description") or raw_item.get("name") or (product.name if product else "")).strip()
        if not description:
            raise ValueError("Cada línea debe tener una descripción.")
        if product is not None:
            if not _can_modify_prices() and unit_price != to_decimal(product.price):
                raise ValueError("No tenés permiso para modificar precios.")
        elif not _can_modify_prices():
            raise ValueError("No tenés permiso para cargar precios manuales.")
        gross = unit_price * quantity
        net = max(gross - discount, Decimal("0.00"))
        items.append(
            {
                "product": product,
                "product_id": product.id if product else None,
                "description": description,
                "quantity": float(quantity),
                "unit_price": unit_price,
                "discount": discount,
                "subtotal": net,
            }
        )

    return items


def _quote_totals(items, *, general_discount=0, surcharge=0):
    line_payload = [
        {"price": item["unit_price"], "quantity": item["quantity"], "line_discount": item["discount"]}
        for item in items
    ]
    totals = calculate_sale_totals(line_payload, general_discount=general_discount, surcharge=surcharge)
    return totals


def _quote_customer_name(quote):
    if getattr(quote, "client", None) is not None:
        return quote.client.name
    raw_name = (getattr(quote, "consumer_name", None) or "").strip()
    return raw_name or "Consumidor final"


def _public_share_serializer():
    return URLSafeTimedSerializer(current_app.config.get("SECRET_KEY", "stockarmobile-dev-secret"))


def _build_public_quote_pdf_url(quote_id: int) -> str:
    token = _public_share_serializer().dumps({"quote_id": int(quote_id)}, salt=QUOTE_PUBLIC_TOKEN_SALT)
    return url_for("quotes.quote_public_pdf", token=token, _external=True)


def _quote_id_from_public_token(token: str) -> int | None:
    try:
        payload = _public_share_serializer().loads(
            token,
            salt=QUOTE_PUBLIC_TOKEN_SALT,
            max_age=QUOTE_PUBLIC_TOKEN_MAX_AGE_SECONDS,
        )
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None
    quote_id = payload.get("quote_id")
    try:
        return int(quote_id)
    except (TypeError, ValueError):
        return None


def _quote_from_form(*, quote=None):
    from app import Company, Quote, QuoteItem, Client, db, scope_query_to_company

    payload = request.form.to_dict(flat=True)
    items_raw = payload.get("items_json") or request.form.get("items_json") or "[]"
    try:
        parsed = json.loads(items_raw)
    except json.JSONDecodeError:
        raise ValueError("Los productos del presupuesto son inválidos.")
    parsed_items = _parse_items({"items": parsed})
    if not parsed_items:
        raise ValueError("Debes agregar al menos un producto al presupuesto.")

    client_id = payload.get("client_id") or None
    consumer_name = (payload.get("consumer_name") or "").strip()
    selected_client = None
    if client_id not in (None, ""):
        try:
            parsed_client_id = int(client_id)
        except (TypeError, ValueError):
            raise ValueError("Cliente inválido.")
        selected_client = scope_query_to_company(db.session.query(Client), Client).filter(Client.id == parsed_client_id, Client.active.is_(True)).first()
        if selected_client is None:
            raise ValueError("El cliente seleccionado no pertenece a tu empresa.")

    if selected_client is None:
        consumer_name = consumer_name[:160]
    else:
        consumer_name = None

    company = Company.query.filter_by(id=getattr(current_user, "company_id", None)).first()
    requested_seller_id = payload.get("seller_id") or getattr(current_user, "id", None)
    if _can_view_other_quotes() or getattr(current_user, "role", None) in {"admin", "superadmin"}:
        seller_id = requested_seller_id
    else:
        seller_id = getattr(current_user, "id", None)
    branch_id = payload.get("branch_id") or None
    expires_at = _parse_date(payload.get("expires_at") or payload.get("fecha_vencimiento"))
    if expires_at is None:
        expires_at = (utcnow() + timedelta(days=5)).replace(hour=23, minute=59, second=59, microsecond=0)

    general_discount = to_decimal(payload.get("discount") or payload.get("descuento") or 0)
    surcharge = to_decimal(payload.get("surcharge") or payload.get("recargo") or 0)
    totals = _quote_totals(parsed_items, general_discount=general_discount, surcharge=surcharge)
    status = _normalize_status(payload.get("status") or (quote.status if quote else "BORRADOR"))
    if payload.get("submit_action") == "send":
        status = "ENVIADO"

    if quote is None:
        quote = Quote(
            client_id=selected_client.id if selected_client else None,
            consumer_name=consumer_name,
            seller_id=int(seller_id) if seller_id not in (None, "") else getattr(current_user, "id", None),
            created_by_user_id=getattr(current_user, "id", None),
            company_id=getattr(current_user, "company_id", None),
            branch_id=int(branch_id) if branch_id not in (None, "") else None,
            currency=getattr(company, "currency", "ARS") or "ARS",
            date=utcnow(),
        )
        db.session.add(quote)
        db.session.flush()
        quote.number = quote.number or _next_quote_number(quote.company_id)

    quote.client_id = selected_client.id if selected_client else None
    quote.consumer_name = consumer_name
    quote.seller_id = int(seller_id) if seller_id not in (None, "") else getattr(current_user, "id", None)
    quote.branch_id = int(branch_id) if branch_id not in (None, "") else quote.branch_id
    quote.expires_at = expires_at
    quote.subtotal = totals["subtotal"]
    quote.discount = totals["line_discount_total"] + totals["general_discount"]
    quote.surcharge = totals["surcharge"]
    quote.tax = totals["tax"]
    quote.total_amount = totals["total"]
    quote.observations = (payload.get("observations") or payload.get("note") or "").strip() or None
    quote.commercial_conditions = (payload.get("commercial_conditions") or payload.get("condiciones_comerciales") or "").strip() or None
    quote.currency = (payload.get("currency") or getattr(company, "currency", None) or quote.currency or "ARS")[:10]
    quote.status = status

    QuoteItem.query.filter_by(quote_id=quote.id).delete()
    for position, (parsed_item, line) in enumerate(zip(parsed_items, totals["lines"]), start=1):
        product = parsed_item["product"]
        product_id = product.id if product else None
        quote_item = QuoteItem(
            quote_id=quote.id,
            product_id=product_id,
            description=parsed_item["description"],
            quantity=float(line["quantity"]),
            unit_price=line["price"],
            discount=line["final_discount"],
            subtotal=line["line_total"],
            sort_order=position,
        )
        db.session.add(quote_item)

    db.session.commit()
    return quote


def _quote_snapshot(quote):
    return {
        "quote": {
            "id": quote.id,
            "number": quote.number or f"P-{quote.id:06d}",
            "date": quote.date,
            "expires_at": quote.expires_at,
            "subtotal": float(quote.subtotal or 0),
            "discount": float(quote.discount or 0),
            "surcharge": float(quote.surcharge or 0),
            "tax": float(quote.tax or 0),
            "total_amount": float(quote.total_amount or 0),
            "observations": quote.observations,
            "status": quote.status,
            "converted_sale_id": quote.converted_sale_id,
            "consumer_name": quote.consumer_name,
        },
        "items": [
            {
                "product_id": item.product_id,
                "description": item.description,
                "quantity": float(item.quantity or 0),
                "unit_price": float(item.unit_price or 0),
                "discount": float(item.discount or 0),
                "subtotal": float(item.subtotal or 0),
            }
            for item in quote.items
        ],
    }


def _quote_rows(quote):
    rows = []
    for item in quote.items:
        rows.append(
            {
                "name": item.description,
                "quantity": float(item.quantity or 0),
                "price": float(item.unit_price or 0),
                "discount": float(item.discount or 0),
                "total": float(item.subtotal or 0),
            }
        )
    return rows


def _quote_form_items(quote):
    return [
        {
            "product_id": item.product_id,
            "description": item.description,
            "quantity": float(item.quantity or 0),
            "unit_price": float(item.unit_price or 0),
            "discount": float(item.discount or 0),
        }
        for item in quote.items
    ]


def _product_image_url(product):
    candidate = (
        getattr(product, "image", None)
        or getattr(product, "image_url", None)
        or getattr(product, "photo", None)
        or getattr(product, "photo_url", None)
        or ""
    )
    raw = str(candidate or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("/"):
        return raw
    return url_for("static", filename=raw.lstrip("/"))


def _product_category_name(product):
    category_value = getattr(product, "category", None)
    if isinstance(category_value, str):
        return category_value
    return getattr(category_value, "name", None) or "Sin categoría"


def _build_product_catalog(products):
    catalog = []
    for product in products:
        category = _product_category_name(product)
        brand = str(getattr(product, "brand", None) or getattr(product, "brand_name", None) or "").strip()
        supplier = str(
            getattr(product, "supplier_name", None)
            or getattr(getattr(product, "supplier", None), "name", None)
            or ""
        ).strip()
        description = str(getattr(product, "description", None) or "").strip()
        code = str(getattr(product, "barcode", None) or getattr(product, "sku", None) or "").strip()
        name = str(getattr(product, "name", None) or "Producto").strip()

        search_blob = " ".join(
            [
                name,
                code,
                category,
                brand,
                supplier,
                description,
            ]
        ).lower()

        catalog.append(
            {
                "id": int(product.id),
                "name": name,
                "category": category,
                "barcode": code,
                "stock": float(getattr(product, "stock", 0) or 0),
                "price": float(getattr(product, "price", 0) or 0),
                "discount": float(getattr(product, "discount", 0) or 0),
                "brand": brand,
                "supplier": supplier,
                "description": description,
                "image": _product_image_url(product),
                "cost": float(getattr(product, "cost_price", 0) or getattr(product, "cost", 0) or 0),
                "search": search_blob,
            }
        )
    return catalog


def _build_client_catalog(clients, *, sales_history):
    payload = []
    for client in clients:
        history = sales_history.get(int(client.id), {"count": 0, "total": 0.0, "last": ""})
        payload.append(
            {
                "id": int(client.id),
                "name": str(getattr(client, "name", None) or "").strip(),
                "commercial_condition": str(
                    getattr(client, "commercial_condition", None)
                    or getattr(client, "payment_terms", None)
                    or ""
                ).strip(),
                "price_list": str(getattr(client, "price_list", None) or "").strip(),
                "usual_discount": float(getattr(client, "usual_discount", 0) or 0),
                "address": str(getattr(client, "address", None) or "").strip(),
                "observations": str(getattr(client, "notes", None) or getattr(client, "observations", None) or "").strip(),
                "purchase_count": int(history.get("count", 0) or 0),
                "purchase_total": float(history.get("total", 0) or 0),
                "last_purchase": str(history.get("last", "") or ""),
            }
        )
    return payload


def _quote_pdf_response(quote, *, as_attachment=False):
    from app import Company

    company = Company.query.filter_by(id=quote.company_id).first()
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    top_margin = height - 36
    pdf.setTitle(f"Presupuesto {quote.number or quote.id}")
    logo_path = None
    if company and company.logo:
        candidate = company.logo
        if not os.path.isabs(candidate):
            candidate = os.path.join(current_app.static_folder or "", candidate)
        if os.path.exists(candidate):
            logo_path = candidate
    if not logo_path:
        fallback_logo = os.path.join(current_app.static_folder or "", "images", "branding", "logo.png")
        if os.path.exists(fallback_logo):
            logo_path = fallback_logo
    if logo_path:
        try:
            # Keep logo near the top edge so it does not look sunken in PDF previews.
            pdf.drawImage(ImageReader(logo_path), 40, top_margin - 24, width=64, height=40, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(118, top_margin, (company.name if company else "StockArmobile") + " - Presupuesto")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(40, top_margin - 26, f"Numero: {quote.number or f'P-{quote.id:06d}'}")
    pdf.drawString(40, top_margin - 40, f"Fecha: {quote.date.strftime('%d/%m/%Y %H:%M') if quote.date else ''}")
    pdf.drawString(40, top_margin - 54, f"Validez: {quote.expires_at.strftime('%d/%m/%Y') if quote.expires_at else ''}")
    pdf.drawString(40, top_margin - 68, f"Estado: {quote.status}")
    pdf.drawString(40, top_margin - 82, f"Cliente: {_quote_customer_name(quote)}")
    if quote.commercial_conditions:
        pdf.drawString(40, top_margin - 96, f"Condiciones: {(quote.commercial_conditions or '')[:90]}")

    pdf.line(40, top_margin - 104, width - 40, top_margin - 104)
    y = top_margin - 122
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(40, y, "Producto")
    pdf.drawString(260, y, "Cant.")
    pdf.drawString(310, y, "Precio")
    pdf.drawString(380, y, "Subtotal")
    y -= 12
    pdf.setFont("Helvetica", 10)
    for item in quote.items:
        if y < 100:
            pdf.showPage()
            y = height - 45
            pdf.setFont("Helvetica", 9)
        pdf.drawString(40, y, (item.description or "")[:48])
        pdf.drawRightString(295, y, f"{float(item.quantity or 0):.3g}")
        pdf.drawRightString(360, y, f"${float(item.unit_price or 0):.2f}")
        pdf.drawRightString(470, y, f"${float(item.subtotal or 0):.2f}")
        y -= 14

    y -= 8
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawRightString(470, y, f"Subtotal: ${float(quote.subtotal or 0):.2f}")
    y -= 14
    pdf.drawRightString(470, y, f"Descuento: -${float(quote.discount or 0):.2f}")
    y -= 14
    pdf.drawRightString(470, y, f"Recargo: ${float(quote.surcharge or 0):.2f}")
    y -= 14
    pdf.drawRightString(470, y, f"Impuestos: ${float(quote.tax or 0):.2f}")
    y -= 18
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawRightString(470, y, f"Total: ${float(quote.total_amount or 0):.2f}")
    y -= 22
    pdf.setFont("Helvetica", 10)
    pdf.drawString(40, y, f"Moneda: {quote.currency or getattr(company, 'currency', 'ARS')}")
    y -= 18
    pdf.drawString(40, y, f"Creado por: {quote.created_by_user.name if quote.created_by_user else ''}")
    y -= 18
    pdf.drawString(40, y, f"Vendedor: {quote.seller.name if quote.seller else ''}")
    if quote.observations:
        y -= 28
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, y, "Observaciones:")
        text = pdf.beginText(40, y - 12)
        for line in (quote.observations or "").splitlines():
            text.textLine(line[:95])
        pdf.drawText(text)
    if quote.commercial_conditions:
        y -= 52
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, y, "Condiciones comerciales:")
        text = pdf.beginText(40, y - 12)
        for line in (quote.commercial_conditions or "").splitlines():
            text.textLine(line[:95])
        pdf.drawText(text)
    qr_value = url_for("quotes.view_quote", quote_id=quote.id, _external=True)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
    qr.add_data(qr_value)
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color="black", back_color="white")
    qr_buffer = BytesIO()
    qr_image.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)
    try:
        pdf.drawImage(ImageReader(qr_buffer), width - 110, 35, width=70, height=70, preserveAspectRatio=True, mask='auto')
        pdf.setFont("Helvetica", 7)
        pdf.drawString(width - 110, 28, "Escaneá para ver el presupuesto")
    except Exception:
        pass
    pdf.setFont("Helvetica", 10)
    pdf.drawString(40, 35, "Firma: ______________________________")
    pdf.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=as_attachment, download_name=f"presupuesto_{quote.id}.pdf")


def _quote_lookup(quote_id):
    from app import Quote, QuoteItem, scope_query_to_company

    return (
        scope_query_to_company(
            Quote.query.options(
                selectinload(Quote.client),
                selectinload(Quote.seller),
                selectinload(Quote.items).selectinload(QuoteItem.product),
                selectinload(Quote.converted_sale),
            ),
            Quote,
        )
        .filter(Quote.id == quote_id)
        .first_or_404()
    )


def _expire_quotes(company_id):
    from app import Quote, db, scope_query_to_company

    now = utcnow()
    active_statuses = {"BORRADOR", "ENVIADO", "PENDIENTE", "APROBADO"}
    quotes_to_expire = scope_query_to_company(
        Quote.query.filter(Quote.company_id == company_id, Quote.expires_at.isnot(None)),
        Quote,
    ).filter(Quote.expires_at < now, Quote.status.in_(active_statuses)).all()
    changed = False
    for quote in quotes_to_expire:
        quote.status = "VENCIDO"
        changed = True
    if changed:
        db.session.commit()


@bp.before_request
def _guard_module():
    if not current_user.is_authenticated:
        return None
    if current_user.role == "superadmin":
        return None
    company = _require_quotes_module()
    _expire_quotes(company.id)
    return None


def _require_owned_or_authorized(quote):
    if getattr(current_user, "role", None) in {"admin", "superadmin"}:
        return
    if quote.seller_id == getattr(current_user, "id", None):
        return
    if _can_view_other_quotes():
        return
    abort(403)


@bp.route("/")
@bp.route("/historial")
@tenant_required
@login_required
def index():
    from app import Client, Quote, QuoteItem, Product, User, db, scope_query_to_company

    _require_quote_permission("quotes_view")
    search = (request.args.get("q") or request.args.get("search") or "").strip()
    product_search = (request.args.get("product") or "").strip()
    status = (request.args.get("status") or "").strip().upper()
    quick = (request.args.get("quick") or "").strip().lower()
    client_id = request.args.get("client_id", type=int)
    seller_id = request.args.get("seller_id", type=int)
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()

    if quick in {"today", "week", "month", "year"}:
        today = utcnow().date()
        if quick == "today":
            date_from = date_to = today.isoformat()
        elif quick == "week":
            date_from = (today - timedelta(days=today.weekday())).isoformat()
            date_to = today.isoformat()
        elif quick == "month":
            date_from = today.replace(day=1).isoformat()
            date_to = today.isoformat()
        elif quick == "year":
            date_from = today.replace(month=1, day=1).isoformat()
            date_to = today.isoformat()

    if quick == "pending" and not status:
        status = "PENDIENTE"
    elif quick == "sent" and not status:
        status = "ENVIADO"
    elif quick == "approved" and not status:
        status = "APROBADO"
    elif quick == "rejected" and not status:
        status = "RECHAZADO"
    elif quick == "converted" and not status:
        status = "CONVERTIDO"
    elif quick == "expired" and not status:
        status = "VENCIDO"

    query = scope_query_to_company(
        Quote.query.options(selectinload(Quote.client), selectinload(Quote.seller), selectinload(Quote.items)),
        Quote,
    )
    if getattr(current_user, "role", None) not in {"admin", "superadmin"} and not _can_view_other_quotes():
        query = query.filter(Quote.seller_id == getattr(current_user, "id", None))
    if search:
        like = f"%{search}%"
        query = query.filter((Quote.number.ilike(like)) | (Quote.observations.ilike(like)) | (Quote.status.ilike(like)))
    if status and status in QUOTE_STATUS_OPTIONS:
        query = query.filter(Quote.status == status)
    if client_id:
        query = query.filter(Quote.client_id == client_id)
    if seller_id:
        query = query.filter(Quote.seller_id == seller_id)
    if product_search:
        like = f"%{product_search}%"
        query = query.join(QuoteItem, QuoteItem.quote_id == Quote.id).outerjoin(Product, Product.id == QuoteItem.product_id).filter((QuoteItem.description.ilike(like)) | (Product.name.ilike(like)) | (Product.barcode.ilike(like))).distinct()
    if date_from:
        query = query.filter(Quote.date >= date_from)
    if date_to:
        query = query.filter(Quote.date <= f"{date_to} 23:59:59")

    quotes_rows = query.order_by(Quote.date.desc(), Quote.id.desc()).limit(100).all()
    clients = scope_query_to_company(Client.query.filter_by(active=True), Client).order_by(Client.name).all()
    sellers = scope_query_to_company(User.query.filter(User.active.is_(True)), User).order_by(User.first_name.asc(), User.last_name.asc(), User.username.asc()).all()
    return render_template(
        "presupuestos/index.html",
        quotes=quotes_rows,
        clients=clients,
        sellers=sellers,
        statuses=QUOTE_STATUS_OPTIONS,
        search=search,
        selected_status=status,
        selected_quick=quick,
        selected_client_id=client_id,
        selected_seller_id=seller_id,
        date_from=date_from,
        date_to=date_to,
        product_search=product_search,
    )


@bp.route("/nuevo", methods=["GET", "POST"])
@tenant_required
@login_required
def new_quote():
    from app import Client, Product, Sale, User, db, scope_query_to_company

    _require_quote_permission("quotes_create")
    if request.method == "POST":
        quote = _quote_from_form()
        from app import record_audit
        record_audit(action="quote_create", entity="quote", entity_id=quote.id, detail=f"Presupuesto creado {quote.number or quote.id}", ip_address=request.remote_addr)
        flash("Presupuesto guardado correctamente.", "success")
        current_app.logger.info("[quotes] presupuesto creado: quote_id=%s", quote.id)
        return redirect(url_for("quotes.view_quote", quote_id=quote.id))

    products = scope_query_to_company(Product.query.filter_by(active=True), Product).order_by(Product.favorite.desc(), Product.name).all()
    clients = scope_query_to_company(Client.query.filter_by(active=True), Client).order_by(Client.name).all()
    sellers = scope_query_to_company(User.query.filter(User.active.is_(True)), User).order_by(User.first_name.asc(), User.last_name.asc(), User.username.asc()).all()
    client_ids = [client.id for client in clients]
    sales_history = {}
    if client_ids:
        rows = (
            scope_query_to_company(db.session.query(Sale.client_id, db.func.count(Sale.id), db.func.coalesce(db.func.sum(Sale.total_amount), 0), db.func.max(Sale.date)), Sale)
            .filter(Sale.client_id.in_(client_ids))
            .group_by(Sale.client_id)
            .all()
        )
        for client_id, count, total_amount, last_date in rows:
            sales_history[int(client_id)] = {
                "count": int(count or 0),
                "total": float(total_amount or 0),
                "last": last_date.strftime("%Y-%m-%d") if last_date else "",
            }

    product_catalog = _build_product_catalog(products)
    client_catalog = _build_client_catalog(clients, sales_history=sales_history)
    default_expires_at = (utcnow() + timedelta(days=5)).strftime("%Y-%m-%d")
    return render_template(
        "presupuestos/form.html",
        quote=None,
        products=products,
        clients=clients,
        sellers=sellers,
        statuses=QUOTE_STATUS_OPTIONS,
        mode="new",
        initial_items=[],
        general_discount_value=0,
        default_expires_at=default_expires_at,
        product_catalog=product_catalog,
        client_catalog=client_catalog,
        can_modify_prices=_can_modify_prices(),
        can_apply_discounts=_can_apply_discounts(),
    )


@bp.route("/<int:quote_id>")
@tenant_required
@login_required
def view_quote(quote_id):
    _require_quote_permission("quotes_view")
    quote = _quote_lookup(quote_id)
    _require_owned_or_authorized(quote)
    return render_template("presupuestos/view.html", quote=quote, rows=_quote_rows(quote), statuses=QUOTE_STATUS_OPTIONS)


@bp.route("/<int:quote_id>/editar", methods=["GET", "POST"])
@tenant_required
@login_required
def edit_quote(quote_id):
    from app import Client, Product, Sale, User, db, scope_query_to_company

    _require_quote_permission("quotes_edit")
    quote = _quote_lookup(quote_id)
    _require_owned_or_authorized(quote)
    if request.method == "POST":
        _quote_from_form(quote=quote)
        from app import record_audit
        record_audit(action="quote_update", entity="quote", entity_id=quote.id, detail=f"Presupuesto actualizado {quote.number or quote.id}", ip_address=request.remote_addr)
        flash("Presupuesto actualizado correctamente.", "success")
        return redirect(url_for("quotes.view_quote", quote_id=quote.id))

    products = scope_query_to_company(Product.query.filter_by(active=True), Product).order_by(Product.favorite.desc(), Product.name).all()
    clients = scope_query_to_company(Client.query.filter_by(active=True), Client).order_by(Client.name).all()
    sellers = scope_query_to_company(User.query.filter(User.active.is_(True)), User).order_by(User.first_name.asc(), User.last_name.asc(), User.username.asc()).all()
    client_ids = [client.id for client in clients]
    sales_history = {}
    if client_ids:
        rows = (
            scope_query_to_company(db.session.query(Sale.client_id, db.func.count(Sale.id), db.func.coalesce(db.func.sum(Sale.total_amount), 0), db.func.max(Sale.date)), Sale)
            .filter(Sale.client_id.in_(client_ids))
            .group_by(Sale.client_id)
            .all()
        )
        for client_id, count, total_amount, last_date in rows:
            sales_history[int(client_id)] = {
                "count": int(count or 0),
                "total": float(total_amount or 0),
                "last": last_date.strftime("%Y-%m-%d") if last_date else "",
            }

    product_catalog = _build_product_catalog(products)
    client_catalog = _build_client_catalog(clients, sales_history=sales_history)
    return render_template(
        "presupuestos/form.html",
        quote=quote,
        products=products,
        clients=clients,
        sellers=sellers,
        statuses=QUOTE_STATUS_OPTIONS,
        mode="edit",
        initial_items=_quote_form_items(quote),
        general_discount_value=max(float(quote.discount or 0) - sum(float(item.discount or 0) for item in quote.items), 0.0),
        default_expires_at="",
        product_catalog=product_catalog,
        client_catalog=client_catalog,
        can_modify_prices=_can_modify_prices(),
        can_apply_discounts=_can_apply_discounts(),
    )


@bp.route("/<int:quote_id>/duplicar", methods=["POST"])
@tenant_required
@login_required
def duplicate_quote(quote_id):
    _require_quote_permission("quotes_duplicate")
    original = _quote_lookup(quote_id)
    _require_owned_or_authorized(original)
    from app import Quote, QuoteItem, db

    duplicate = Quote(
        client_id=original.client_id,
        consumer_name=original.consumer_name,
        seller_id=original.seller_id,
        company_id=original.company_id,
        expires_at=original.expires_at,
        subtotal=original.subtotal,
        discount=original.discount,
        surcharge=original.surcharge,
        tax=original.tax,
        total_amount=original.total_amount,
        observations=original.observations,
        commercial_conditions=original.commercial_conditions,
        currency=original.currency,
        status="BORRADOR",
        date=utcnow(),
        created_by_user_id=getattr(current_user, "id", None),
    )
    db.session.add(duplicate)
    db.session.flush()
    duplicate.number = f"P-{duplicate.id:06d}"
    for item in original.items:
        db.session.add(
            QuoteItem(
                quote_id=duplicate.id,
                product_id=item.product_id,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                discount=item.discount,
                subtotal=item.subtotal,
                sort_order=item.sort_order,
            )
        )
    db.session.commit()
    from app import record_audit
    record_audit(action="quote_duplicate", entity="quote", entity_id=duplicate.id, detail=f"Duplicado desde {original.number or original.id}", ip_address=request.remote_addr)
    flash("Presupuesto duplicado.", "success")
    return redirect(url_for("quotes.edit_quote", quote_id=duplicate.id))


@bp.route("/<int:quote_id>/eliminar", methods=["POST"])
@tenant_required
@login_required
def delete_quote(quote_id):
    from app import Quote, db, scope_query_to_company

    _require_quote_permission("quotes_delete")
    quote = scope_query_to_company(db.session.query(Quote), Quote).filter(Quote.id == quote_id).first_or_404()
    _require_owned_or_authorized(quote)
    if quote.status == "CONVERTIDO":
        flash("No se puede eliminar un presupuesto convertido.", "warning")
        return redirect(url_for("quotes.view_quote", quote_id=quote.id))
    db.session.delete(quote)
    db.session.commit()
    from app import record_audit
    record_audit(action="quote_delete", entity="quote", entity_id=quote.id, detail=f"Presupuesto eliminado {quote.number or quote.id}", ip_address=request.remote_addr)
    flash("Presupuesto eliminado.", "success")
    return redirect(url_for("quotes.index"))


@bp.route("/<int:quote_id>/anular", methods=["POST"])
@tenant_required
@login_required
def annul_quote(quote_id):
    from app import Quote, db, record_audit, scope_query_to_company

    _require_quote_permission("quotes_anulate")
    quote = scope_query_to_company(db.session.query(Quote), Quote).filter(Quote.id == quote_id).first_or_404()
    _require_owned_or_authorized(quote)
    if quote.status == "CONVERTIDO":
        flash("No se puede anular un presupuesto convertido.", "warning")
        return redirect(url_for("quotes.view_quote", quote_id=quote.id))
    quote.status = "ANULADO"
    db.session.commit()
    record_audit(action="quote_annul", entity="quote", entity_id=quote.id, detail=f"Presupuesto anulado {quote.number or quote.id}", ip_address=request.remote_addr)
    flash("Presupuesto anulado.", "success")
    return redirect(url_for("quotes.view_quote", quote_id=quote.id))


@bp.route("/<int:quote_id>/pdf")
@tenant_required
@login_required
def quote_pdf(quote_id):
    if not _can_download_pdf():
        abort(403)
    quote = _quote_lookup(quote_id)
    _require_owned_or_authorized(quote)
    return _quote_pdf_response(quote, as_attachment=True)


@bp.route("/<int:quote_id>/imprimir")
@tenant_required
@login_required
def quote_print(quote_id):
    _require_quote_permission("quotes_print")
    quote = _quote_lookup(quote_id)
    _require_owned_or_authorized(quote)
    return _quote_pdf_response(quote, as_attachment=False)


@bp.route("/publico/<token>/pdf")
def quote_public_pdf(token):
    from app import Quote, QuoteItem

    quote_id = _quote_id_from_public_token(token)
    if not quote_id:
        abort(404)

    quote = (
        Quote.query.options(
            selectinload(Quote.client),
            selectinload(Quote.seller),
            selectinload(Quote.items).selectinload(QuoteItem.product),
            selectinload(Quote.converted_sale),
        )
        .filter(Quote.id == quote_id)
        .first()
    )
    if quote is None:
        abort(404)
    return _quote_pdf_response(quote, as_attachment=False)


@bp.route("/<int:quote_id>/whatsapp")
@tenant_required
@login_required
def share_whatsapp(quote_id):
    quote = _quote_lookup(quote_id)
    _require_owned_or_authorized(quote)
    _require_quote_permission("quotes_share_whatsapp")
    phone = (quote.client.whatsapp if quote.client else "") or (getattr(current_user, "company", None) and getattr(current_user.company, "whatsapp", None)) or ""
    message = (
        "Hola.\n\n"
        "Te enviamos el presupuesto solicitado.\n\n"
        "Quedamos a disposición para cualquier consulta.\n\n"
        "Muchas gracias."
    )
    pdf_url = _build_public_quote_pdf_url(quote.id)
    return render_template("presupuestos/whatsapp_dialog.html", quote=quote, entered_phone=phone, message=message, pdf_url=pdf_url)


@bp.route("/<int:quote_id>/whatsapp", methods=["POST"])
@tenant_required
@login_required
def share_whatsapp_post(quote_id):
    quote = _quote_lookup(quote_id)
    _require_owned_or_authorized(quote)
    _require_quote_permission("quotes_share_whatsapp")
    phone = (request.form.get("whatsapp_phone") or "").strip()
    message = (
        "Hola.\n\n"
        "Te enviamos el presupuesto solicitado.\n\n"
        "Quedamos a disposición para cualquier consulta.\n\n"
        "Muchas gracias."
    )
    pdf_url = _build_public_quote_pdf_url(quote.id)
    from app import record_audit
    record_audit(action="quote_share_whatsapp", entity="quote", entity_id=quote.id, detail=f"Compartido por WhatsApp {quote.number or quote.id}", ip_address=request.remote_addr)
    return redirect(build_whatsapp_share_url(phone=phone, message=message, document_url=pdf_url, document_label="PDF"))


@bp.route("/<int:quote_id>/convertir", methods=["POST"])
@tenant_required
@login_required
def convert_to_sale(quote_id):
    from app import Product, Quote, db, record_audit, scope_query_to_company

    _require_quote_permission("quotes_convert")
    quote = scope_query_to_company(db.session.query(Quote).options(selectinload(Quote.items)), Quote).filter(Quote.id == quote_id).first_or_404()
    _require_owned_or_authorized(quote)
    if quote.status == "CONVERTIDO" and quote.converted_sale_id:
        return redirect(url_for("sales.view_sale", sale_id=quote.converted_sale_id))

    payload_items = []
    product_ids = []
    for item in quote.items:
        product_id = int(item.product_id or 0)
        if product_id <= 0:
            raise ValueError("Solo se pueden convertir presupuestos con productos asociados.")
        product_ids.append(product_id)
        payload_items.append(
            {
                "productId": product_id,
                "name": item.description,
                "price": float(item.unit_price or 0),
                "quantity": float(item.quantity or 0),
            }
        )

    if not payload_items:
        flash("El presupuesto no tiene productos para convertir.", "warning")
        return redirect(url_for("quotes.view_quote", quote_id=quote.id))

    products = {
        product.id: product
        for product in scope_query_to_company(db.session.query(Product), Product)
        .filter(Product.id.in_(sorted(set(product_ids))))
        .all()
    }
    cart_items = []
    skipped_lines = 0
    for row in payload_items:
        product = products.get(int(row["productId"]))
        if product is None:
            skipped_lines += 1
            continue
        max_stock = max(float(product.stock or 0), 0.0)
        requested_qty = max(float(row["quantity"] or 0), 0.0)
        final_qty = min(requested_qty, max_stock)
        if final_qty <= 0:
            skipped_lines += 1
            continue
        cart_items.append(
            {
                "productId": int(product.id),
                "name": row["name"],
                "price": float(row["price"]),
                "quantity": final_qty,
                "stock": max_stock,
                "barcode": (product.barcode or "")[:80],
                "unitMeasure": (product.unit_measure or "u")[:20],
            }
        )

    if not cart_items:
        flash("No se pudo cargar el presupuesto al carrito porque no hay stock disponible.", "warning")
        return redirect(url_for("quotes.view_quote", quote_id=quote.id))

    import sales as sales_blueprint

    sales_blueprint._set_quote_cart_prefill(
        {
            "source": "quote",
            "quote_id": quote.id,
            "quote_number": quote.number or f"P-{quote.id:06d}",
            "checkout_token": f"quote-cart-{quote.id}",
            "client_id": quote.client_id or "",
            "note": (quote.observations or "")[:255],
            "document_type": "venta",
            "general_discount": float(quote.discount or 0),
            "surcharge": float(quote.surcharge or 0),
            "auto_open_cart": True,
            "items": cart_items,
        }
    )

    record_audit(
        action="quote_convert_prepare",
        entity="quote",
        entity_id=quote.id,
        detail=f"Presupuesto preparado para carrito POS ({len(cart_items)} líneas)",
        ip_address=request.remote_addr,
    )
    if skipped_lines:
        flash(
            f"Presupuesto cargado al carrito. {skipped_lines} línea(s) no se agregaron por falta de stock/producto.",
            "warning",
        )
    else:
        flash("Presupuesto cargado al carrito. Ahora puedes revisar y procesar la venta.", "success")
    return redirect(url_for("sales.index"))


@bp.route("/<int:quote_id>/email", methods=["POST"])
@tenant_required
@login_required
def email_quote(quote_id):
    quote = _quote_lookup(quote_id)
    _require_owned_or_authorized(quote)
    _require_quote_permission("quotes_email")
    if not quote.client or not quote.client.email:
        flash("El cliente no tiene email registrado.", "warning")
        return redirect(url_for("quotes.view_quote", quote_id=quote.id))
    from app import record_audit
    record_audit(action="quote_email_ready", entity="quote", entity_id=quote.id, detail=f"Estructura de email preparada {quote.number or quote.id}", ip_address=request.remote_addr)
    flash("Estructura de envío por email preparada. Integrar provider configurado para adjuntar el PDF.", "info")
    return redirect(url_for("quotes.view_quote", quote_id=quote.id))

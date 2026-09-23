"""Blueprint de productos: CRUD e inventario."""

import uuid
import unicodedata
from datetime import datetime
from io import BytesIO

from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from app import tenant_required, utcnow
from services.sales_calculation_service import calculate_product_pricing
from services.product_image_service import ProductImageError, delete_product_image, resolve_product_image, save_product_image

bp = Blueprint("products", __name__)

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024


def _product_to_dict(product):
    return {
        "id": product.id,
        "barcode": product.barcode,
        "codigo": product.barcode,
        "name": product.name,
        "nombre": product.name,
        "description": product.description or "",
        "category": product.category or "",
        "categoria": product.category or "",
        "sale_type": product.sale_type or "unidad",
        "tipo_venta": product.sale_type or "unidad",
        "unit_measure": product.unit_measure or "u",
        "unidad_medida": product.unit_measure or "u",
        "photo": product.photo or "",
        "brand": product.brand or "",
        "marca": product.brand or "",
        "supplier": product.supplier or "",
        "proveedor": product.supplier or "",
        "cost_price": float(product.cost_price or 0),
        "precio_costo": float(product.cost_price or 0),
        "price": float(product.price or 0),
        "precio_venta": float(product.price or 0),
        "margin": float(product.margin or 0),
        "profit_percent": float(product.profit_percent or 0),
        "tax": float(product.tax or 0),
        "iva": float(product.tax or 0),
        "stock": product.stock or 0,
        "min_stock": product.min_stock or 0,
        "discount": float(product.discount or 0),
        "favorite": bool(product.favorite),
    }


def _apply_product_form(product, form):
    product.barcode = (form.barcode.data or product.barcode or "").strip()
    product.name = form.name.data
    product.description = form.description.data
    product.category = form.category.data
    product.sale_type = form.sale_type.data or "unidad"
    product.unit_measure = (form.unit_measure.data or "").strip() or _default_unit(product.sale_type)
    product.brand = form.brand.data
    product.supplier = form.supplier.data
    cost_price = float(form.cost_price.data or 0)
    tax_percent = float(form.tax.data or 0)
    raw_price = (request.form.get("price") or "").strip()
    raw_profit_percent = (request.form.get("profit_percent") or "").strip()
    raw_margin = (request.form.get("margin") or "").strip()
    pricing_source = (request.form.get("pricing_source") or "").strip().lower()

    if pricing_source not in {"price", "profit_percent", "margin"}:
        if request.endpoint == "products.edit":
            posted_price = float(form.price.data or 0)
            posted_margin = float(form.margin.data or 0)
            posted_profit = float(form.profit_percent.data or 0)
            current_price = float(product.price or 0)
            current_margin = float(product.margin or 0)
            current_profit = float(product.profit_percent or 0)
            if raw_margin and posted_margin != current_margin:
                pricing_source = "margin"
            elif raw_profit_percent and posted_profit != current_profit:
                pricing_source = "profit_percent"
            elif raw_price and posted_price != current_price:
                pricing_source = "price"

    pricing = calculate_product_pricing(
        cost_price=cost_price,
        price=form.price.data if raw_price else product.price,
        margin=form.margin.data if raw_margin else product.margin,
        profit_percent=form.profit_percent.data if raw_profit_percent else product.profit_percent,
        pricing_source=pricing_source,
    )
    final_price = float(pricing["price"])
    gain_amount = float(pricing["margin"])
    margin_percent = float(pricing["profit_percent"])

    product.cost_price = cost_price
    product.price = final_price
    product.margin = gain_amount
    product.profit_percent = margin_percent
    product.tax = tax_percent
    product.stock = float(form.stock.data or 0)
    product.min_stock = float(form.min_stock.data or 0)
    product.discount = float(form.discount.data or 0)
    product.favorite = bool(form.favorite.data)


def _default_unit(sale_type):
    return {
        "unidad": "u",
        "kilogramo": "kg",
        "gramos": "g",
        "litros": "l",
        "mililitros": "ml",
        "metros": "m",
        "centimetros": "cm",
        "caja": "caja",
        "pack": "pack",
        "bolsa": "bolsa",
        "botella": "botella",
        "paquete": "paq",
        "docena": "doc",
        "media_docena": "1/2 doc",
    }.get(sale_type or "unidad", "u")


def _float_value(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _require_admin_product_management():
    if getattr(current_user, "role", None) != "admin":
        flash("Solo el administrador puede editar precios o eliminar productos.", "warning")
        return redirect(url_for("products.index"))
    return None


@bp.route("/imagen/<filename>")
def product_image(filename):
    resolved = resolve_product_image(filename)
    if resolved is None:
        from flask import abort
        abort(404)
    path, mime = resolved
    return send_file(path, mimetype=mime, max_age=86400, conditional=True)


@bp.route("/")
@tenant_required
def index():
    from app import Product, ProductForm, scope_query_to_company

    query = scope_query_to_company(Product.query.filter_by(active=True), Product)
    search = request.args.get("q") or request.args.get("search")
    category = request.args.get("categoria") or request.args.get("category")
    low_stock = request.args.get("low_stock")
    if search:
        like = f"%{search}%"
        query = query.filter((Product.name.ilike(like)) | (Product.barcode.ilike(like)) | (Product.category.ilike(like)))
    if category:
        query = query.filter(Product.category == category)
    if low_stock:
        query = query.filter(Product.stock <= Product.min_stock)

    productos = query.order_by(Product.name).all()
    categorias = [c[0] for c in scope_query_to_company(Product.query.with_entities(Product.category), Product).distinct().all() if c[0]]
    return render_template(
        "productos/index.html",
        products=productos,
        productos=productos,
        categorias=categorias,
        form=ProductForm(),
        edit=False,
    )


@bp.route("/add", methods=["GET", "POST"])
@tenant_required
def add():
    from app import Product, ProductForm, ProductModification, db, record_audit, scope_query_to_company
    from services.plan_usage_service import PlanUsageService

    form = ProductForm()
    if form.validate_on_submit():
        allowed, message = PlanUsageService.can_create(getattr(current_user, "company_id", None), PlanUsageService.RESOURCE_PRODUCTS)
        if not allowed:
            flash(message, "warning")
            return redirect(url_for("company_billing.subscription_portal"))

        next_id = (db.session.query(db.func.coalesce(db.func.max(Product.id), 0)).scalar() or 0) + 1
        barcode = (form.barcode.data or "").strip() or f"P{next_id:06d}"
        if scope_query_to_company(Product.query.filter_by(barcode=barcode), Product).first():
            flash("El codigo de barras ya existe.", "danger")
            return redirect(url_for("products.index"))

        product = Product(barcode=barcode, name=form.name.data, company_id=getattr(current_user, 'company_id', None))
        try:
            _apply_product_form(product, form)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("products.index"))
        upload = request.files.get("photo_file")
        if upload and (upload.filename or "").strip():
            try:
                product.photo = save_product_image(upload)
            except ProductImageError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("products.index"))
        try:
            db.session.add(product)
            db.session.flush()
            db.session.add(
                ProductModification(
                    product_id=product.id,
                    company_id=product.company_id,
                    user_id=current_user.id,
                    action="creacion",
                    detail="Producto creado",
                )
            )
            record_audit(action="product_create", entity="product", entity_id=product.id, detail=f"Producto creado: {product.name}")
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("No se pudo guardar: el codigo de barras ya existe.", "danger")
            return redirect(url_for("products.index"))
        flash("Producto creado exitosamente.", "success")
        return redirect(url_for("products.index"))

    return render_template("productos/index.html", productos=[], products=[], categorias=[], form=form, edit=True)


@bp.route("/edit/<int:product_id>", methods=["GET", "POST"])
@bp.route("/edit/<int:id>", methods=["GET", "POST"])
@tenant_required
def edit(product_id=None, id=None):
    from app import Product, ProductForm, ProductModification, ProductPriceHistory, db, record_audit, scope_query_to_company

    blocked = _require_admin_product_management()
    if blocked is not None:
        return blocked

    product = scope_query_to_company(db.session.query(Product), Product).filter(Product.id == (product_id or id)).first()
    if product is None:
        flash("Producto no encontrado.", "warning")
        return redirect(url_for("products.index"))

    form = ProductForm(obj=product)
    if form.validate_on_submit():
        barcode = (form.barcode.data or product.barcode or "").strip()
        duplicate = (
            scope_query_to_company(Product.query.filter_by(barcode=barcode), Product)
            .filter(Product.id != product.id)
            .first()
        )
        if duplicate is not None:
            flash("Este codigo de barras ya esta registrado.", "danger")
            return redirect(url_for("products.edit", product_id=product.id))
        old_price = float(product.price or 0)
        old_cost = float(product.cost_price or 0)
        try:
            _apply_product_form(product, form)
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("products.edit", product_id=product.id))
        upload = request.files.get("photo_file")
        if upload and (upload.filename or "").strip():
            try:
                product.photo = _save_product_image(upload)
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("products.edit", product_id=product.id))
        if old_price != float(product.price or 0) or old_cost != float(product.cost_price or 0):
            db.session.add(
                ProductPriceHistory(
                    product_id=product.id,
                    company_id=product.company_id,
                    user_id=current_user.id,
                    old_price=old_price,
                    new_price=float(product.price or 0),
                    old_cost=old_cost,
                    new_cost=float(product.cost_price or 0),
                )
            )
        db.session.add(
            ProductModification(
                product_id=product.id,
                company_id=product.company_id,
                user_id=current_user.id,
                action="edicion",
                detail="Producto actualizado",
            )
        )
        record_audit(action="product_update", entity="product", entity_id=product.id, detail=f"Producto actualizado: {product.name}")
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("No se pudo actualizar: el codigo de barras ya existe.", "danger")
            return redirect(url_for("products.index"))
        if upload and (upload.filename or "").strip():
            delete_product_image(old_photo)
        flash("Producto actualizado exitosamente.", "success")
        return redirect(url_for("products.index"))

    categorias = [c[0] for c in scope_query_to_company(Product.query.with_entities(Product.category), Product).distinct().all() if c[0]]
    return render_template("productos/index.html", productos=[product], products=[product], categorias=categorias, form=form, edit=True)


@bp.route("/delete/<int:product_id>", methods=["POST"])
@bp.route("/delete/<int:id>", methods=["POST"])
@tenant_required
def delete(product_id=None, id=None):
    from app import Product, db, record_audit, scope_query_to_company

    blocked = _require_admin_product_management()
    if blocked is not None:
        return blocked

    product = scope_query_to_company(db.session.query(Product), Product).filter(Product.id == (product_id or id)).first()
    if product:
        product.active = False
        record_audit(action="product_deactivate", entity="product", entity_id=product.id, detail=f"Producto desactivado: {product.name}")
        db.session.commit()
        flash("Producto desactivado exitosamente.", "success")
    return redirect(url_for("products.index"))


def _get_kardex_movements(product_id):
    from app import Product, ProductModification, PurchaseItem, PurchaseOrder, Sale, SaleItem, db, scope_query_to_company

    product = scope_query_to_company(db.session.query(Product), Product).filter(Product.id == product_id).first()
    if product is None:
        return None, []

    movements = []
    sale_rows = (
        scope_query_to_company(
            db.session.query(Sale, SaleItem).join(SaleItem, Sale.id == SaleItem.sale_id),
            Sale,
        )
        .filter(SaleItem.product_id == product.id)
        .order_by(Sale.date.desc(), Sale.id.desc())
        .all()
    )
    for sale, item in sale_rows:
        movements.append(
            {
                "date": sale.date,
                "type": "Venta",
                "detail": "Venta #{} - {}".format(sale.id, sale.customer or "Consumidor final"),
                "quantity": -float(item.quantity or 0),
                "unit_value": float(item.price or 0),
            }
        )

    purchase_rows = (
        scope_query_to_company(
            db.session.query(PurchaseOrder, PurchaseItem).join(PurchaseItem, PurchaseOrder.id == PurchaseItem.purchase_order_id),
            PurchaseOrder,
        )
        .filter(PurchaseItem.product_id == product.id)
        .order_by(PurchaseOrder.date.desc(), PurchaseOrder.id.desc())
        .all()
    )
    for purchase, item in purchase_rows:
        movements.append(
            {
                "date": purchase.date,
                "type": "Compra",
                "detail": "Compra #{}".format(purchase.id),
                "quantity": float(item.quantity or 0),
                "unit_value": float(item.unit_cost or 0),
            }
        )

    modifications = (
        scope_query_to_company(ProductModification.query.filter_by(product_id=product.id), ProductModification)
        .order_by(ProductModification.created_at.desc())
        .all()
    )
    for modification in modifications:
        movements.append(
            {
                "date": modification.created_at,
                "type": modification.action.capitalize(),
                "detail": modification.detail or "",
                "quantity": None,
                "unit_value": None,
            }
        )

    movements.sort(key=lambda item: item["date"] or datetime.min, reverse=True)
    return product, movements


@bp.route("/<int:product_id>/kardex")
@tenant_required
def kardex(product_id):
    product, movements = _get_kardex_movements(product_id)
    if product is None:
        flash("Producto no encontrado.", "warning")
        return redirect(url_for("products.index"))
    return render_template("productos/kardex.html", product=product, movements=movements, auto_print=False)


@bp.route("/<int:product_id>/kardex/export.xlsx")
@tenant_required
def kardex_export_excel(product_id):
    from openpyxl import Workbook

    product, movements = _get_kardex_movements(product_id)
    if product is None:
        flash("Producto no encontrado.", "warning")
        return redirect(url_for("products.index"))

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Kardex"
    sheet.append(["Fecha", "Tipo", "Detalle", "Cantidad", "Valor unitario", "Importe"])
    for movement in movements:
        quantity = movement["quantity"]
        unit_value = movement["unit_value"]
        sheet.append([
            movement["date"].strftime("%Y-%m-%d %H:%M:%S") if movement["date"] else "",
            movement["type"], movement["detail"], quantity, unit_value,
            (quantity * unit_value) if quantity is not None and unit_value is not None else None,
        ])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        width = min(max(max(len(str(cell.value or "")) for cell in column) + 2, 12), 48)
        sheet.column_dimensions[column[0].column_letter].width = width

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="kardex_{}_{}.xlsx".format(product.id, utcnow().strftime("%Y%m%d")),
    )


@bp.route("/<int:product_id>/kardex/export.pdf")
@tenant_required
def kardex_export_pdf(product_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

    product, movements = _get_kardex_movements(product_id)
    if product is None:
        flash("Producto no encontrado.", "warning")
        return redirect(url_for("products.index"))

    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=10 * mm, leftMargin=10 * mm, topMargin=10 * mm, bottomMargin=10 * mm)
    styles = getSampleStyleSheet()
    rows = [["Fecha", "Tipo", "Detalle", "Cantidad", "Valor unit.", "Importe"]]
    for movement in movements:
        quantity = movement["quantity"]
        unit_value = movement["unit_value"]
        rows.append([
            movement["date"].strftime("%Y-%m-%d %H:%M:%S") if movement["date"] else "",
            movement["type"], movement["detail"],
            "{:.3f}".format(quantity) if quantity is not None else "",
            "${:.2f}".format(unit_value) if unit_value is not None else "",
            "${:.2f}".format(quantity * unit_value) if quantity is not None and unit_value is not None else "",
        ])
    table = Table(rows, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9eef5")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story = [
        Paragraph("<b>Kardex — {}</b>".format(product.name), styles["Title"]),
        Paragraph("Código: {} · Stock actual: {:.3f} {}".format(product.barcode or "-", float(product.stock or 0), product.unit_measure or ""), styles["Normal"]),
        Spacer(1, 5 * mm), table,
    ]
    document.build(story)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="kardex_{}_{}.pdf".format(product.id, utcnow().strftime("%Y%m%d")))


@bp.route("/<int:product_id>/kardex/imprimir")
@tenant_required
def kardex_print(product_id):
    product, movements = _get_kardex_movements(product_id)
    if product is None:
        flash("Producto no encontrado.", "warning")
        return redirect(url_for("products.index"))
    return render_template("productos/kardex.html", product=product, movements=movements, auto_print=True)

@bp.route("/export.xlsx")
@tenant_required
def export_excel():
    from app import Product, scope_query_to_company
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Productos"
    headers = ["barcode", "name", "category", "brand", "supplier", "cost_price", "price", "stock", "min_stock", "sale_type", "unit_measure", "discount"]
    sheet.append(headers)
    for product in scope_query_to_company(Product.query.filter_by(active=True), Product).order_by(Product.name).all():
        sheet.append(
            [
                product.barcode,
                product.name,
                product.category or "",
                product.brand or "",
                product.supplier or "",
                float(product.cost_price or 0),
                float(product.price or 0),
                float(product.stock or 0),
                float(product.min_stock or 0),
                product.sale_type or "unidad",
                product.unit_measure or "u",
                float(product.discount or 0),
            ]
        )
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"productos_{utcnow():%Y%m%d}.xlsx",
    )


@bp.route("/import", methods=["POST"])
@tenant_required
def import_excel():
    from app import Product, ProductModification, ProductPriceHistory, db, scope_query_to_company
    from openpyxl import load_workbook

    blocked = _require_admin_product_management()
    if blocked is not None:
        return blocked

    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith(".xlsx"):
        flash("Subi un archivo .xlsx valido.", "danger")
        return redirect(url_for("products.index"))

    max_rows = 10000
    workbook = None
    try:
        workbook = load_workbook(upload, read_only=True, data_only=True)
        aliases = {
            "barcode": {
                "barcode", "codigo", "codigo de barras", "codigo producto",
                "codigo del producto", "codigo interno", "cod", "ean", "ean8",
                "ean13", "ean 13", "sku",
            },
            "name": {
                "name", "nombre", "nombre producto", "nombre del producto",
                "producto", "articulo", "articulo producto", "descripcion",
                "descripcion producto", "descripcion del producto", "detalle",
            },
            "category": {"category", "categoria", "rubro", "familia"},
            "brand": {"brand", "marca"},
            "supplier": {"supplier", "proveedor", "proveedor principal"},
            "cost_price": {
                "cost price", "cost_price", "precio costo", "precio de costo",
                "precio costo unitario", "costo", "costo unitario",
            },
            "price": {
                "price", "precio", "precio venta", "precio de venta",
                "precio unitario", "venta",
            },
            "stock": {"stock", "existencias", "cantidad", "stock actual", "existencia"},
            "min_stock": {"min stock", "min_stock", "stock minimo", "minimo", "minimo stock"},
            "sale_type": {"sale type", "sale_type", "tipo venta", "tipo de venta"},
            "unit_measure": {"unit measure", "unit_measure", "unidad medida", "unidad de medida", "unidad"},
            "discount": {"discount", "descuento"},
        }

        def normalize_header(value):
            text = str(value or "").strip().lower()
            text = unicodedata.normalize("NFKD", text)
            text = "".join(char for char in text if not unicodedata.combining(char))
            return " ".join(text.replace("_", " ").replace("-", " ").split())

        normalized_aliases = {
            field: {normalize_header(value) for value in accepted}
            for field, accepted in aliases.items()
        }

        def detect_positions(raw_headers):
            positions = {}
            for index, raw_header in enumerate(raw_headers):
                header = normalize_header(raw_header)
                if not header:
                    continue
                for field, accepted in normalized_aliases.items():
                    if header in accepted and field not in positions:
                        positions[field] = index
                        break
            return positions

        header_row_number = None
        header_sheet = None
        positions = {}

        # Algunos Excel tienen un titulo, filas vacias o informacion comercial antes
        # de la cabecera. Buscamos la primera fila que contenga Codigo/EAN/SKU + Nombre/Descripcion.
        for candidate_sheet in workbook.worksheets:
            for row_number, raw_row in enumerate(
                candidate_sheet.iter_rows(min_row=1, max_row=20, values_only=True),
                start=1,
            ):
                candidate_positions = detect_positions(raw_row)
                if "barcode" in candidate_positions and "name" in candidate_positions:
                    header_row_number = row_number
                    header_sheet = candidate_sheet
                    positions = candidate_positions
                    break
            if header_sheet is not None:
                break

        if header_sheet is None:
            flash(
                "No se encontraron encabezados validos. Se requiere una columna de Codigo/Barcode/EAN/SKU "
                "y otra de Nombre/Descripcion/Producto.",
                "danger",
            )
            return redirect(url_for("products.index"))

        sheet = header_sheet
        iterator = sheet.iter_rows(min_row=header_row_number + 1, values_only=True)

        def cell(row, field, default=None):
            index = positions.get(field)
            if index is None or index >= len(row):
                return default
            value = row[index]
            return default if value is None else value

        def numeric(value, field, default=0.0):
            if value in (None, ""):
                return default
            if isinstance(value, str):
                normalized = value.strip().replace(" ", "")
                if "," in normalized and "." in normalized:
                    if normalized.rfind(",") > normalized.rfind("."):
                        normalized = normalized.replace(".", "").replace(",", ".")
                    else:
                        normalized = normalized.replace(",", "")
                else:
                    normalized = normalized.replace(",", ".")
                value = normalized
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ValueError(f"Valor numerico invalido en {field}: {value!r}")

        records = []
        seen_barcodes = set()
        skipped = 0
        duplicate_rows = 0

        for row_number, row in enumerate(iterator, start=header_row_number + 1):
            if row_number > max_rows + 1:
                raise ValueError(f"El archivo supera el limite de {max_rows} productos por importacion.")

            barcode = str(cell(row, "barcode", "")).strip()
            name = str(cell(row, "name", "")).strip()
            if not barcode and not name:
                continue
            if not barcode or not name:
                skipped += 1
                continue
            if barcode in seen_barcodes:
                duplicate_rows += 1
                continue
            seen_barcodes.add(barcode)

            records.append(
                {
                    "row_number": row_number,
                    "barcode": barcode,
                    "name": name,
                    "category": str(cell(row, "category", "")).strip(),
                    "brand": str(cell(row, "brand", "")).strip(),
                    "supplier": str(cell(row, "supplier", "")).strip(),
                    "cost_price": numeric(cell(row, "cost_price"), "precio costo", None),
                    "price": numeric(cell(row, "price"), "precio venta", None),
                    "stock": numeric(cell(row, "stock"), "stock", None),
                    "min_stock": numeric(cell(row, "min_stock"), "stock minimo", None),
                    "sale_type": str(cell(row, "sale_type", "")).strip(),
                    "unit_measure": str(cell(row, "unit_measure", "")).strip(),
                    "discount": numeric(cell(row, "discount"), "descuento", None),
                }
            )

        if not records:
            flash("No se encontraron filas validas para importar.", "warning")
            return redirect(url_for("products.index"))

        created = 0
        updated = 0
        price_changes = 0
        stock_changes = 0

        try:
            for data in records:
                product = scope_query_to_company(
                    Product.query.filter_by(barcode=data["barcode"]), Product
                ).first()

                is_new = product is None
                if is_new:
                    product = Product(
                        barcode=data["barcode"],
                        name=data["name"],
                        active=True,
                        company_id=getattr(current_user, "company_id", None),
                    )
                    db.session.add(product)
                    db.session.flush()
                    created += 1
                    action = "importacion"
                else:
                    updated += 1
                    action = "actualizacion_importacion"

                old_price = float(product.price or 0)
                old_cost = float(product.cost_price or 0)
                old_stock = float(product.stock or 0)

                product.name = data["name"]
                product.active = True
                if data["category"]:
                    product.category = data["category"]
                if data["brand"]:
                    product.brand = data["brand"]
                if data["supplier"]:
                    product.supplier = data["supplier"]
                if data["cost_price"] is not None:
                    product.cost_price = data["cost_price"]
                if data["price"] is not None:
                    product.price = data["price"]
                if data["stock"] is not None:
                    product.stock = data["stock"]
                if data["min_stock"] is not None:
                    product.min_stock = data["min_stock"]
                if data["sale_type"]:
                    product.sale_type = data["sale_type"]
                if data["unit_measure"]:
                    product.unit_measure = data["unit_measure"]
                elif not product.unit_measure:
                    product.unit_measure = _default_unit(product.sale_type)
                if data["discount"] is not None:
                    product.discount = data["discount"]

                product.margin = float(product.price or 0) - float(product.cost_price or 0)
                product.profit_percent = (
                    product.margin / float(product.cost_price or 1) * 100
                    if product.cost_price
                    else 0
                )

                new_price = float(product.price or 0)
                new_cost = float(product.cost_price or 0)
                new_stock = float(product.stock or 0)
                if old_price != new_price or old_cost != new_cost:
                    price_changes += 1
                    db.session.add(
                        ProductPriceHistory(
                            product_id=product.id,
                            company_id=product.company_id,
                            user_id=current_user.id,
                            old_price=old_price,
                            new_price=new_price,
                            old_cost=old_cost,
                            new_cost=new_cost,
                        )
                    )
                if old_stock != new_stock:
                    stock_changes += 1

                detail = "Importacion Excel"
                if old_stock != new_stock:
                    detail += f" · Stock {old_stock:g} → {new_stock:g}"
                db.session.add(
                    ProductModification(
                        product_id=product.id,
                        company_id=product.company_id,
                        user_id=current_user.id,
                        action=action,
                        detail=detail,
                    )
                )

            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        summary = (
            f"Importacion completada: {created} creados, {updated} actualizados, "
            f"{price_changes} con cambios de precio/costo y {stock_changes} con cambios de stock."
        )
        extras = []
        if skipped:
            extras.append(f"{skipped} filas incompletas omitidas")
        if duplicate_rows:
            extras.append(f"{duplicate_rows} filas duplicadas omitidas")
        if extras:
            summary += " " + ", ".join(extras) + "."
        flash(summary, "success")
        return redirect(url_for("products.index"))
    except ValueError as exc:
        if workbook is not None:
            workbook.close()
        flash(f"No se importo el archivo: {exc}", "danger")
        return redirect(url_for("products.index"))
    except Exception:
        db.session.rollback()
        if workbook is not None:
            workbook.close()
        flash("No se importo el archivo. No se aplicaron cambios.", "danger")
        return redirect(url_for("products.index"))
    finally:
        if workbook is not None:
            workbook.close()


@bp.route("/api/products")
@tenant_required
def api_list():
    from app import Product, scope_query_to_company

    return jsonify({"products": [_product_to_dict(p) for p in scope_query_to_company(Product.query.filter_by(active=True), Product).order_by(Product.name).all()]})


@bp.route("/api/<barcode>")
@tenant_required
def api_get_by_barcode(barcode):
    from app import Product, scope_query_to_company

    product = scope_query_to_company(Product.query.filter_by(barcode=barcode, active=True), Product).first()
    if not product:
        return jsonify({"error": "Producto no encontrado"}), 404
    return jsonify(_product_to_dict(product))

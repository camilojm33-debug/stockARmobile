"""Administración tenant-scoped de listas de precios mayoristas."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps
from io import BytesIO

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import Index, UniqueConstraint

from app import Company, db, tenant_required
from services.ai_agent.usage_service import can_use_commercial_feature
from stockarmobile.helpers.dates import utcnow_naive

bp = Blueprint("wholesale_pricing", __name__, url_prefix="/precios-mayoristas")


class PriceList(db.Model):
    __tablename__ = "price_lists"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_price_lists_company_code"),
        Index("ix_price_lists_company_active", "company_id", "active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(80), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    effective_from = db.Column(db.Date)
    effective_until = db.Column(db.Date)
    # Wholesale tariffs do not stack retail promotions unless a future policy explicitly enables it.
    allow_promotions = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)

    items = db.relationship("PriceListItem", backref="price_list", cascade="all, delete-orphan", lazy="selectin")
    company = db.relationship("Company")
    created_by = db.relationship("User")


class PriceListItem(db.Model):
    __tablename__ = "price_list_items"
    __table_args__ = (
        UniqueConstraint("price_list_id", "product_id", "min_quantity", name="uq_price_list_item_tier"),
        Index("ix_price_list_items_company_list_product", "company_id", "price_list_id", "product_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    price_list_id = db.Column(db.Integer, db.ForeignKey("price_lists.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    min_quantity = db.Column(db.Numeric(18, 3), nullable=False, default=Decimal("1.000"), server_default="1.000")
    unit_price = db.Column(db.Numeric(18, 2), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)

    product = db.relationship("Product")
    company = db.relationship("Company")


def _company_id():
    return int(getattr(current_user, "company_id", 0) or 0)


def _require_admin():
    if not current_user.is_authenticated or getattr(current_user, "role", None) != "admin" or not _company_id():
        abort(403)


def _wholesale_plan_required(view):
    """Require an active commercial plan with wholesale-pricing entitlement."""
    @wraps(view)
    def decorated(*args, **kwargs):
        _require_admin()
        company = Company.query.filter_by(id=_company_id(), active=True).first()
        if company is None:
            flash("No se encontró la empresa activa.", "danger")
            return redirect(url_for("dashboard.index"))
        access = can_use_commercial_feature(company, "wholesale_pricing")
        if not access.allowed:
            flash(
                access.reason or "Los precios mayoristas requieren el plan Negocio o superior.",
                "warning",
            )
            return redirect(url_for("company_billing.subscription_portal"))
        return view(*args, **kwargs)
    return decorated


def _slug(value):
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:80]


def _date_value(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError("La fecha de vigencia no es válida.")


def active_price_lists(company_id: int, *, include_inactive: bool = False):
    query = PriceList.query.filter_by(company_id=int(company_id))
    if not include_inactive:
        query = query.filter(PriceList.active.is_(True))
    return query.order_by(PriceList.name.asc(), PriceList.id.asc()).all()


@bp.get("/")
@tenant_required
@login_required
@_wholesale_plan_required
def index():
    _require_admin()
    from app import Product

    company_id = _company_id()
    lists = active_price_lists(company_id, include_inactive=True)
    selected_id = request.args.get("list_id", type=int)
    selected = next((row for row in lists if row.id == selected_id), None)
    if selected is None:
        selected = next((row for row in lists if row.active), None)
    products = (
        Product.query.filter_by(company_id=company_id, active=True)
        .order_by(Product.name.asc())
        .limit(1000)
        .all()
    )
    rows = (
        PriceListItem.query.filter_by(company_id=company_id, price_list_id=selected.id)
        .join(Product, Product.id == PriceListItem.product_id)
        .order_by(Product.name.asc(), PriceListItem.min_quantity.asc())
        .all()
        if selected is not None
        else []
    )
    return render_template(
        "precios_mayoristas/index.html",
        lists=lists,
        selected_list=selected,
        products=products,
        price_rows=rows,
        today=date.today().isoformat(),
    )


@bp.post("/listas")
@tenant_required
@login_required
@_wholesale_plan_required
def create_list():
    _require_admin()
    company_id = _company_id()
    name = str(request.form.get("name") or "").strip()[:120]
    code = _slug(request.form.get("code") or name)
    try:
        effective_from = _date_value(request.form.get("effective_from"))
        effective_until = _date_value(request.form.get("effective_until"))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("wholesale_pricing.index"))
    if not name or not code:
        flash("Ingresá un nombre para la lista.", "danger")
        return redirect(url_for("wholesale_pricing.index"))
    if effective_from and effective_until and effective_until < effective_from:
        flash("La fecha final no puede ser anterior a la fecha inicial.", "danger")
        return redirect(url_for("wholesale_pricing.index"))
    if PriceList.query.filter_by(company_id=company_id, code=code).first():
        flash("Ya existe una lista con ese código en tu empresa.", "warning")
        return redirect(url_for("wholesale_pricing.index"))
    row = PriceList(
        company_id=company_id,
        name=name,
        code=code,
        effective_from=effective_from,
        effective_until=effective_until,
        allow_promotions=request.form.get("allow_promotions") == "1",
        created_by_user_id=int(current_user.id),
    )
    db.session.add(row)
    db.session.commit()
    flash("Lista de precios creada. Cargá los precios por producto y asignala a los clientes correspondientes.", "success")
    return redirect(url_for("wholesale_pricing.index", list_id=row.id))


@bp.post("/listas/<int:list_id>/items")
@tenant_required
@login_required
@_wholesale_plan_required
def save_item(list_id):
    _require_admin()
    from app import Product

    company_id = _company_id()
    price_list = PriceList.query.filter_by(id=list_id, company_id=company_id).first_or_404()
    product_id = request.form.get("product_id", type=int)
    item_id = request.form.get("item_id", type=int)
    try:
        quantity = Decimal(str(request.form.get("min_quantity") or "1").replace(",", "."))
        unit_price = Decimal(str(request.form.get("unit_price") or "").replace(",", "."))
        if not quantity.is_finite() or quantity <= 0:
            raise ValueError("La cantidad mínima debe ser mayor a cero.")
        if not unit_price.is_finite() or unit_price <= 0:
            raise ValueError("El precio mayorista debe ser mayor a cero.")
        quantity = quantity.quantize(Decimal("0.001"))
        unit_price = unit_price.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) and str(exc) else "Ingresá una cantidad y un precio válidos."
        flash(message, "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))
    product = Product.query.filter_by(id=product_id, company_id=company_id, active=True).first()
    if product is None:
        flash("El producto no pertenece a tu empresa o está inactivo.", "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))

    if item_id:
        item = PriceListItem.query.filter_by(
            id=item_id, company_id=company_id, price_list_id=price_list.id
        ).first()
        if item is None:
            abort(404)
        conflict = PriceListItem.query.filter(
            PriceListItem.price_list_id == price_list.id,
            PriceListItem.product_id == product_id,
            PriceListItem.min_quantity == quantity,
            PriceListItem.id != item.id,
        ).first()
        if conflict:
            flash("Ya existe un tramo para ese producto y cantidad mínima.", "warning")
            return redirect(url_for("wholesale_pricing.index", list_id=list_id))
        item.product_id = product_id
        item.min_quantity = quantity
        item.unit_price = unit_price
        item.active = True
    else:
        item = PriceListItem.query.filter_by(
            company_id=company_id,
            price_list_id=price_list.id,
            product_id=product_id,
            min_quantity=quantity,
        ).first()
        if item is None:
            item = PriceListItem(
                company_id=company_id,
                price_list_id=price_list.id,
                product_id=product_id,
                min_quantity=quantity,
                unit_price=unit_price,
                active=True,
            )
            db.session.add(item)
        else:
            item.unit_price = unit_price
            item.active = True
    db.session.commit()
    cost = Decimal(str(product.cost_price or 0))
    if cost > 0 and unit_price < cost:
        flash(
            f"Precio guardado, pero queda por debajo del costo ({cost:.2f}). Revisá la rentabilidad antes de vender.",
            "warning",
        )
    else:
        flash("Precio mayorista guardado.", "success")
    return redirect(url_for("wholesale_pricing.index", list_id=list_id))


@bp.get("/listas/<int:list_id>/plantilla.xlsx")
@tenant_required
@login_required
@_wholesale_plan_required
def download_template(list_id):
    """Download a full active-product catalog ready for wholesale price entry."""
    _require_admin()
    from openpyxl import Workbook
    from app import Product

    company_id = _company_id()
    price_list = PriceList.query.filter_by(id=list_id, company_id=company_id).first_or_404()
    products = (
        Product.query.filter_by(company_id=company_id, active=True)
        .order_by(Product.name.asc(), Product.id.asc())
        .all()
    )
    product_ids = {int(product.id) for product in products}
    existing_rows = (
        PriceListItem.query.filter_by(company_id=company_id, price_list_id=price_list.id)
        .order_by(PriceListItem.product_id.asc(), PriceListItem.min_quantity.asc(), PriceListItem.id.asc())
        .all()
    )
    tiers_by_product = {}
    for item in existing_rows:
        if int(item.product_id) in product_ids:
            tiers_by_product.setdefault(int(item.product_id), []).append(item)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Precios mayoristas"
    sheet.append(["barcode", "product_name", "min_quantity", "unit_price", "active"])
    for product in products:
        tiers = tiers_by_product.get(int(product.id), [])
        if tiers:
            for tier in tiers:
                sheet.append([
                    product.barcode or "",
                    product.name or "",
                    float(tier.min_quantity or 1),
                    float(tier.unit_price or 0) if tier.unit_price is not None else None,
                    bool(tier.active),
                ])
        else:
            # Blank price means "do not import this product until a price is entered".
            sheet.append([product.barcode or "", product.name or "", 1, None, True])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in (("A", 24), ("B", 36), ("C", 18), ("D", 18), ("E", 12)):
        sheet.column_dimensions[column].width = width
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"plantilla_precios_mayoristas_{price_list.code}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.get("/listas/<int:list_id>/exportar.xlsx")
@tenant_required
@login_required
@_wholesale_plan_required
def export_list(list_id):
    _require_admin()
    from openpyxl import Workbook
    from sqlalchemy.orm import joinedload

    company_id = _company_id()
    price_list = PriceList.query.filter_by(id=list_id, company_id=company_id).first_or_404()
    rows = (
        PriceListItem.query.options(joinedload(PriceListItem.product))
        .filter_by(company_id=company_id, price_list_id=price_list.id)
        .order_by(PriceListItem.product_id.asc(), PriceListItem.min_quantity.asc())
        .all()
    )
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Precios mayoristas"
    sheet.append(["barcode", "product_name", "min_quantity", "unit_price", "active"])
    for row in rows:
        product = row.product
        if product is None or int(product.company_id) != company_id:
            continue
        sheet.append([
            product.barcode or "",
            product.name or "",
            float(row.min_quantity or 1),
            float(row.unit_price or 0),
            bool(row.active),
        ])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column, width in (("A", 24), ("B", 36), ("C", 18), ("D", 18), ("E", 12)):
        sheet.column_dimensions[column].width = width
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"precios_mayoristas_{price_list.code}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.post("/listas/<int:list_id>/importar.xlsx")
@tenant_required
@login_required
@_wholesale_plan_required
def import_list(list_id):
    _require_admin()
    from openpyxl import load_workbook
    from app import Product

    company_id = _company_id()
    price_list = PriceList.query.filter_by(id=list_id, company_id=company_id).first_or_404()
    upload = request.files.get("file")
    if upload is None or not upload.filename or not upload.filename.lower().endswith(".xlsx"):
        flash("Seleccioná un archivo Excel .xlsx.", "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))
    raw = upload.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        flash("El archivo no puede superar los 5 MB.", "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))
    try:
        workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
        sheet = workbook.active
        values = list(sheet.iter_rows(values_only=True))
        workbook.close()
    except Exception:
        flash("No se pudo leer el Excel. Exportá primero la plantilla de la lista.", "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))
    if not values:
        flash("El archivo Excel está vacío.", "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))

    def normalize_header(value):
        raw_header = unicodedata.normalize("NFKD", str(value or "").strip().lower())
        raw_header = "".join(char for char in raw_header if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]+", "_", raw_header).strip("_")

    header = [normalize_header(value) for value in values[0]]
    aliases = {
        "barcode": {"barcode", "codigo", "sku", "ean", "product_code"},
        "min_quantity": {"min_quantity", "cantidad_minima", "desde_cantidad", "min_qty"},
        "unit_price": {"unit_price", "precio_mayorista", "precio", "price"},
        "active": {"active", "activo", "habilitado"},
    }
    positions = {}
    for index, value in enumerate(header):
        for canonical, options in aliases.items():
            if value in options:
                positions[canonical] = index
    missing = {"barcode", "min_quantity", "unit_price"} - set(positions)
    if missing:
        flash("Faltan columnas obligatorias: código/sku, cantidad mínima y precio mayorista.", "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))
    if len(values) > 10001:
        flash("El archivo admite hasta 10.000 filas de precios más el encabezado.", "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))

    parsed_rows = []
    seen = set()
    errors = []
    for line_number, row in enumerate(values[1:], start=2):
        if not row or not any(value not in (None, "") for value in row):
            continue
        try:
            barcode = str(row[positions["barcode"]] or "").strip()[:50]
            if not barcode:
                raise ValueError("falta código/sku")
            raw_quantity = row[positions["min_quantity"]] if positions["min_quantity"] < len(row) else None
            raw_price = row[positions["unit_price"]] if positions["unit_price"] < len(row) else None
            # A blank price is an explicit omission, not a $0 price. This lets the
            # full catalog template include products the user does not want to price yet.
            if raw_price is None or (isinstance(raw_price, str) and not raw_price.strip()):
                continue
            if raw_quantity is None or (isinstance(raw_quantity, str) and not raw_quantity.strip()):
                raise ValueError("falta cantidad mínima")
            quantity = Decimal(str(raw_quantity).replace(",", "."))
            unit_price = Decimal(str(raw_price).replace(",", "."))
            if not quantity.is_finite() or quantity <= 0:
                raise ValueError("la cantidad mínima debe ser mayor a cero")
            if not unit_price.is_finite() or unit_price <= 0:
                raise ValueError("el precio debe ser mayor a cero")
            quantity = quantity.quantize(Decimal("0.001"))
            unit_price = unit_price.quantize(Decimal("0.01"))
            key = (barcode, quantity)
            if key in seen:
                raise ValueError("fila duplicada para el mismo código y cantidad mínima")
            seen.add(key)
            raw_active = row[positions["active"]] if "active" in positions and positions["active"] < len(row) else True
            active_value = str(raw_active or "").strip().lower()
            active = True if raw_active in (None, "") else (
                raw_active is True or active_value in {"1", "true", "si", "sí", "yes", "activo", "on"}
            )
            parsed_rows.append({
                "barcode": barcode,
                "quantity": quantity,
                "unit_price": unit_price,
                "active": active,
            })
        except (InvalidOperation, TypeError, ValueError, IndexError) as exc:
            errors.append(f"Fila {line_number}: {exc}")
    if errors:
        flash("No se importó ningún precio. " + " · ".join(errors[:5]), "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))
    if not parsed_rows:
        flash("No se encontraron filas de precios para importar.", "warning")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))

    products = {
        product.barcode: product
        for product in Product.query.filter_by(company_id=company_id, active=True)
        .filter(Product.barcode.in_(sorted({row["barcode"] for row in parsed_rows})))
        .all()
    }
    missing_codes = sorted({row["barcode"] for row in parsed_rows} - set(products))
    if missing_codes:
        flash("No se importó ningún precio. Códigos inexistentes o inactivos: " + ", ".join(missing_codes[:8]), "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))
    below_cost_count = sum(
        1 for row in parsed_rows
        if Decimal(str(products[row["barcode"]].cost_price or 0)) > 0
        and row["unit_price"] < Decimal(str(products[row["barcode"]].cost_price or 0))
    )

    # Load existing tiers in one query instead of a database lookup for each Excel row.
    existing_items = PriceListItem.query.filter_by(
        company_id=company_id, price_list_id=price_list.id
    ).all()
    items_by_key = {
        (int(item.product_id), Decimal(str(item.min_quantity)).quantize(Decimal("0.001"))): item
        for item in existing_items
    }

    try:
        updated = 0
        created = 0
        for row in parsed_rows:
            product = products[row["barcode"]]
            key = (int(product.id), row["quantity"])
            item = items_by_key.get(key)
            if item is None:
                item = PriceListItem(
                    company_id=company_id,
                    price_list_id=price_list.id,
                    product_id=product.id,
                    min_quantity=row["quantity"],
                    unit_price=row["unit_price"],
                    active=row["active"],
                )
                db.session.add(item)
                items_by_key[key] = item
                created += 1
            else:
                item.unit_price = row["unit_price"]
                item.active = row["active"]
                updated += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Wholesale price-list import failed: company_id=%s list_id=%s", company_id, list_id)
        flash("No se aplicó la importación. No se guardaron cambios.", "danger")
        return redirect(url_for("wholesale_pricing.index", list_id=list_id))

    summary = f"Importación aplicada a '{price_list.name}': {created} tramos creados y {updated} actualizados. Las filas que no aparecen en el archivo se conservaron."
    if below_cost_count:
        flash(summary + f" Atención: {below_cost_count} tramo(s) quedan por debajo del costo de producto.", "warning")
    else:
        flash(summary, "success")
    return redirect(url_for("wholesale_pricing.index", list_id=list_id))


@bp.post("/listas/<int:list_id>/items/<int:item_id>/desactivar")
@tenant_required
@login_required
@_wholesale_plan_required
def deactivate_item(list_id, item_id):
    _require_admin()
    item = PriceListItem.query.filter_by(
        id=item_id, company_id=_company_id(), price_list_id=list_id
    ).first_or_404()
    item.active = False
    db.session.commit()
    flash("Tramo de precio desactivado.", "success")
    return redirect(url_for("wholesale_pricing.index", list_id=list_id))


@bp.post("/listas/<int:list_id>/estado")
@tenant_required
@login_required
@_wholesale_plan_required
def toggle_list(list_id):
    _require_admin()
    row = PriceList.query.filter_by(id=list_id, company_id=_company_id()).first_or_404()
    row.active = not bool(row.active)
    db.session.commit()
    flash("Lista activada." if row.active else "Lista desactivada.", "success")
    return redirect(url_for("wholesale_pricing.index", list_id=list_id))

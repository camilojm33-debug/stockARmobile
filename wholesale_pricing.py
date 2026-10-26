"""Administración tenant-scoped de listas de precios mayoristas."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import Index, UniqueConstraint

from app import db, tenant_required
from stockarmobile.helpers.dates import utcnow_naive

bp = Blueprint("wholesale_pricing", __name__, url_prefix="/precios-mayoristas")


class PriceList(db.Model):
    __tablename__ = "price_lists"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_price_lists_company_code"),
        Index("ix_price_lists_company_active", "company_id", "active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(80), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    effective_from = db.Column(db.Date)
    effective_until = db.Column(db.Date)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
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
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
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
    flash("Precio mayorista guardado.", "success")
    return redirect(url_for("wholesale_pricing.index", list_id=list_id))


@bp.post("/listas/<int:list_id>/items/<int:item_id>/desactivar")
@tenant_required
@login_required
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
def toggle_list(list_id):
    _require_admin()
    row = PriceList.query.filter_by(id=list_id, company_id=_company_id()).first_or_404()
    row.active = not bool(row.active)
    db.session.commit()
    flash("Lista activada." if row.active else "Lista desactivada.", "success")
    return redirect(url_for("wholesale_pricing.index", list_id=list_id))

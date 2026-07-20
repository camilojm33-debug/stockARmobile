"""Modulo de compras: proveedores, ordenes e ingreso de mercaderia."""

from datetime import datetime

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

bp = Blueprint("purchases", __name__)


def _to_float(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _can_manage_purchases():
    return getattr(current_user, "role", None) in {"admin", "superadmin"}


def _resolve_company_id(required=False):
    from app import Company

    role = getattr(current_user, "role", None)
    if role == "superadmin":
        company_id = request.values.get("company_id", type=int)
    else:
        company_id = getattr(current_user, "company_id", None)

    if required and not company_id:
        return None
    if company_id:
        company = Company.query.filter_by(id=company_id).first()
        if company is None:
            return None
    return company_id


def _company_scope(query, model, company_id):
    if company_id is None or not hasattr(model, "company_id"):
        return query
    return query.filter(model.company_id == company_id)


def _superadmin_companies():
    from app import Company

    return Company.query.filter(Company.active.is_(True)).order_by(Company.name.asc()).all()


def _purchase_access_guard():
    if not _can_manage_purchases():
        abort(403)
    return None


@bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    from app import Product, PurchaseItem, PurchaseOrder, Supplier, db, record_audit, utcnow

    blocked = _purchase_access_guard()
    if blocked is not None:
        return blocked

    company_id = _resolve_company_id(required=False)
    if getattr(current_user, "role", None) == "superadmin" and company_id is None:
        return render_template(
            "compras/index.html",
            suppliers=[],
            products=[],
            purchases=[],
            selected_company_id=None,
            companies=_superadmin_companies(),
            search="",
            status_filter="active",
            supplier_stats={},
            company_required=True,
        )

    if request.method == "POST":
        if company_id is None:
            flash("Selecciona una empresa para continuar.", "warning")
            return redirect(url_for("purchases.index"))
        product_id = request.form.get("product_id", type=int)
        supplier_id = request.form.get("supplier_id", type=int)
        quantity = _to_float(request.form.get("quantity"))
        unit_cost = _to_float(request.form.get("unit_cost"))
        if not product_id or quantity <= 0 or unit_cost < 0:
            flash("Completa producto, cantidad y costo para registrar la compra.", "danger")
            return redirect(url_for("purchases.index", company_id=company_id))

        product = _company_scope(db.session.query(Product), Product, company_id).filter(Product.id == product_id).first()
        if product is None:
            flash("Producto no encontrado.", "danger")
            return redirect(url_for("purchases.index", company_id=company_id))

        supplier = None
        if supplier_id:
            supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id, Supplier.active.is_(True)).first()
            if supplier is None:
                flash("Proveedor no encontrado para esta empresa.", "danger")
                return redirect(url_for("purchases.index", company_id=company_id))

        previous_stock = float(product.stock or 0)
        previous_cost = float(product.cost_price or 0)
        total_units = previous_stock + quantity
        average_cost = ((previous_stock * previous_cost) + (quantity * unit_cost)) / total_units if total_units else unit_cost

        order = PurchaseOrder(
            supplier_id=supplier.id if supplier else None,
            company_id=company_id,
            date=utcnow(),
            status="recibida",
            subtotal=quantity * unit_cost,
            total_amount=quantity * unit_cost,
            note=request.form.get("note"),
        )
        db.session.add(order)
        db.session.flush()
        db.session.add(PurchaseItem(purchase_order_id=order.id, product_id=product.id, quantity=quantity, unit_cost=unit_cost))
        product.stock = total_units
        product.cost_price = average_cost
        product.margin = float(product.price or 0) - average_cost
        product.profit_percent = (product.margin / average_cost * 100) if average_cost else 0
        record_audit(action="purchase_create", entity="purchase_order", entity_id=order.id, detail=f"Compra registrada producto={product.id} qty={quantity}")
        db.session.commit()
        flash("Compra registrada y stock actualizado.", "success")
        return redirect(url_for("purchases.index", company_id=company_id))

    suppliers = _company_scope(Supplier.query.filter_by(active=True), Supplier, company_id).order_by(Supplier.name).all()
    products = _company_scope(Product.query.filter_by(active=True), Product, company_id).order_by(Product.name).all()
    purchases = _company_scope(PurchaseOrder.query, PurchaseOrder, company_id).order_by(PurchaseOrder.date.desc()).limit(20).all()
    return render_template(
        "compras/index.html",
        suppliers=suppliers,
        products=products,
        purchases=purchases,
        selected_company_id=company_id,
        companies=_superadmin_companies() if getattr(current_user, "role", None) == "superadmin" else [],
        search="",
        status_filter="active",
        supplier_stats={},
        company_required=False,
    )


@bp.route("/proveedores", methods=["GET", "POST"])
@login_required
def suppliers_panel():
    from app import PurchaseOrder, Supplier, db, record_audit

    blocked = _purchase_access_guard()
    if blocked is not None:
        return blocked

    company_id = _resolve_company_id(required=False)
    if getattr(current_user, "role", None) == "superadmin" and company_id is None:
        return render_template(
            "compras/suppliers.html",
            suppliers=[],
            supplier_stats={},
            selected_company_id=None,
            companies=_superadmin_companies(),
            search="",
            status_filter="active",
            company_required=True,
        )

    if request.method == "POST":
        if company_id is None:
            flash("Selecciona una empresa para continuar.", "warning")
            return redirect(url_for("purchases.suppliers_panel"))

        name = (request.form.get("name") or "").strip()
        if not name:
            flash("El proveedor necesita nombre.", "danger")
            return redirect(url_for("purchases.suppliers_panel", company_id=company_id))

        supplier = Supplier(
            company_id=company_id,
            name=name,
            email=(request.form.get("email") or "").strip() or None,
            phone=(request.form.get("phone") or "").strip() or None,
            whatsapp=(request.form.get("whatsapp") or "").strip() or None,
            address=(request.form.get("address") or "").strip() or None,
            notes=(request.form.get("notes") or "").strip() or None,
            active=True,
        )
        db.session.add(supplier)
        db.session.flush()
        record_audit(action="supplier_create", entity="supplier", entity_id=supplier.id, detail=f"Proveedor creado: {supplier.name}")
        db.session.commit()
        flash("Proveedor creado.", "success")
        return redirect(url_for("purchases.suppliers_panel", company_id=company_id))

    search = (request.args.get("q") or "").strip()
    status_filter = (request.args.get("status") or "active").strip().lower()

    supplier_query = _company_scope(Supplier.query, Supplier, company_id)
    if search:
        like = f"%{search}%"
        supplier_query = supplier_query.filter(
            or_(
                Supplier.name.ilike(like),
                Supplier.email.ilike(like),
                Supplier.phone.ilike(like),
                Supplier.whatsapp.ilike(like),
                Supplier.address.ilike(like),
                Supplier.notes.ilike(like),
            )
        )

    if status_filter == "active":
        supplier_query = supplier_query.filter(Supplier.active.is_(True))
    elif status_filter == "inactive":
        supplier_query = supplier_query.filter(Supplier.active.is_(False))

    suppliers = supplier_query.order_by(Supplier.name.asc(), Supplier.id.desc()).all()

    stats_rows = (
        _company_scope(
            db.session.query(
                PurchaseOrder.supplier_id,
                db.func.count(PurchaseOrder.id).label("purchase_count"),
                db.func.coalesce(db.func.sum(PurchaseOrder.total_amount), 0).label("total_amount"),
            ),
            PurchaseOrder,
            company_id,
        )
        .filter(PurchaseOrder.supplier_id.isnot(None))
        .group_by(PurchaseOrder.supplier_id)
        .all()
    )
    supplier_stats = {
        int(row.supplier_id): {
            "purchase_count": int(row.purchase_count or 0),
            "total_amount": float(row.total_amount or 0),
        }
        for row in stats_rows
        if row.supplier_id is not None
    }

    return render_template(
        "compras/suppliers.html",
        suppliers=suppliers,
        supplier_stats=supplier_stats,
        selected_company_id=company_id,
        companies=_superadmin_companies() if getattr(current_user, "role", None) == "superadmin" else [],
        search=search,
        status_filter=status_filter,
        company_required=False,
    )


@bp.route("/proveedores/<int:supplier_id>/update", methods=["POST"])
@login_required
def update_supplier(supplier_id):
    from app import Supplier, db, record_audit

    blocked = _purchase_access_guard()
    if blocked is not None:
        return blocked

    company_id = _resolve_company_id(required=True)
    if company_id is None:
        flash("Empresa inválida.", "danger")
        return redirect(url_for("purchases.suppliers_panel"))

    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("El proveedor necesita nombre.", "danger")
        return redirect(url_for("purchases.suppliers_panel", company_id=company_id))

    supplier.name = name
    supplier.email = (request.form.get("email") or "").strip() or None
    supplier.phone = (request.form.get("phone") or "").strip() or None
    supplier.whatsapp = (request.form.get("whatsapp") or "").strip() or None
    supplier.address = (request.form.get("address") or "").strip() or None
    supplier.notes = (request.form.get("notes") or "").strip() or None
    db.session.add(supplier)
    record_audit(action="supplier_update", entity="supplier", entity_id=supplier.id, detail=f"Proveedor actualizado: {supplier.name}")
    db.session.commit()
    flash("Proveedor actualizado.", "success")
    return redirect(url_for("purchases.suppliers_panel", company_id=company_id))


@bp.route("/proveedores/<int:supplier_id>/toggle", methods=["POST"])
@login_required
def toggle_supplier(supplier_id):
    from app import Supplier, db, record_audit

    blocked = _purchase_access_guard()
    if blocked is not None:
        return blocked

    company_id = _resolve_company_id(required=True)
    if company_id is None:
        flash("Empresa inválida.", "danger")
        return redirect(url_for("purchases.suppliers_panel"))

    supplier = _company_scope(db.session.query(Supplier), Supplier, company_id).filter(Supplier.id == supplier_id).first_or_404()
    supplier.active = not bool(supplier.active)
    db.session.add(supplier)
    action = "supplier_activate" if supplier.active else "supplier_deactivate"
    detail = "Proveedor reactivado" if supplier.active else "Proveedor desactivado"
    record_audit(action=action, entity="supplier", entity_id=supplier.id, detail=f"{detail}: {supplier.name}")
    db.session.commit()
    flash("Proveedor actualizado.", "success")
    return redirect(url_for("purchases.suppliers_panel", company_id=company_id))

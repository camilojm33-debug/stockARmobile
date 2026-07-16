"""Modulo de compras: proveedores, ordenes e ingreso de mercaderia."""

from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from app import company_admin_required

bp = Blueprint("purchases", __name__)


def _to_float(value, default=0.0):
    try:
        return float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        return default


@bp.route("/", methods=["GET", "POST"])
@company_admin_required
def index():
    from app import Product, PurchaseItem, PurchaseOrder, Supplier, db, record_audit, scope_query_to_company, utcnow

    if request.method == "POST":
        product_id = request.form.get("product_id", type=int)
        supplier_id = request.form.get("supplier_id", type=int)
        quantity = _to_float(request.form.get("quantity"))
        unit_cost = _to_float(request.form.get("unit_cost"))
        if not product_id or quantity <= 0 or unit_cost < 0:
            flash("Completa producto, cantidad y costo para registrar la compra.", "danger")
            return redirect(url_for("purchases.index"))

        product = scope_query_to_company(db.session.query(Product), Product).filter(Product.id == product_id).first()
        if product is None:
            flash("Producto no encontrado.", "danger")
            return redirect(url_for("purchases.index"))

        previous_stock = float(product.stock or 0)
        previous_cost = float(product.cost_price or 0)
        total_units = previous_stock + quantity
        average_cost = ((previous_stock * previous_cost) + (quantity * unit_cost)) / total_units if total_units else unit_cost

        order = PurchaseOrder(
            supplier_id=supplier_id or None,
            company_id=getattr(current_user, "company_id", None),
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
        return redirect(url_for("purchases.index"))

    suppliers = scope_query_to_company(Supplier.query.filter_by(active=True), Supplier).order_by(Supplier.name).all()
    products = scope_query_to_company(Product.query.filter_by(active=True), Product).order_by(Product.name).all()
    purchases = scope_query_to_company(PurchaseOrder.query, PurchaseOrder).order_by(PurchaseOrder.date.desc()).limit(20).all()
    return render_template("compras/index.html", suppliers=suppliers, products=products, purchases=purchases)


@bp.route("/proveedores", methods=["POST"])
@company_admin_required
def add_supplier():
    from app import Supplier, db, record_audit, scope_query_to_company

    name = (request.form.get("name") or "").strip()
    if not name:
        flash("El proveedor necesita nombre.", "danger")
        return redirect(url_for("purchases.index"))
    supplier = Supplier(
        company_id=getattr(current_user, "company_id", None),
        name=name,
        email=request.form.get("email") or None,
        phone=request.form.get("phone") or None,
        whatsapp=request.form.get("whatsapp") or None,
        address=request.form.get("address") or None,
        notes=request.form.get("notes") or None,
    )
    db.session.add(supplier)
    db.session.flush()
    record_audit(action="supplier_create", entity="supplier", entity_id=supplier.id, detail=f"Proveedor creado: {supplier.name}")
    db.session.commit()
    flash("Proveedor creado.", "success")
    return redirect(url_for("purchases.index"))

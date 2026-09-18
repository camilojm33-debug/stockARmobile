"""Controlador global de precios, seguro y tenant-scoped."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import Numeric

from app import Company, Product, Supplier, db, record_audit, scope_query_to_company, utcnow
from stockarmobile.decorators import company_admin_required
from stockarmobile.tenant import get_current_company_id
from services.ai_agent.usage_service import can_use_ai_feature


bp = Blueprint("pricing_controller", __name__, url_prefix="/precios")

TWOPLACES = Decimal("0.01")
FOURPLACES = Decimal("0.0001")
ROUNDING_UNITS = {
    "none": None,
    "10": Decimal("10"),
    "100": Decimal("100"),
    "1000": Decimal("1000"),
}


class PriceControllerBatch(db.Model):
    __tablename__ = "price_controller_batches"
    __table_args__ = (
        db.Index("ix_price_controller_batches_company_created", "company_id", "created_at"),
        db.Index("ix_price_controller_batches_company_status", "company_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False, default="Ajuste global de precios")
    adjustment_type = db.Column(db.String(20), nullable=False)
    direction = db.Column(db.String(12), nullable=False, default="increase")
    adjustment_value = db.Column(Numeric(18, 4), nullable=False, default=0)
    category = db.Column(db.String(100))
    brand = db.Column(db.String(120))
    supplier = db.Column(db.String(160))
    only_in_stock = db.Column(db.Boolean, nullable=False, default=False)
    rounding = db.Column(db.String(20), nullable=False, default="none")
    min_price = db.Column(Numeric(18, 2))
    max_price = db.Column(Numeric(18, 2))
    min_margin_percent = db.Column(Numeric(10, 4))
    max_change_percent = db.Column(Numeric(10, 4))
    status = db.Column(db.String(24), nullable=False, default="preview", index=True)
    product_count = db.Column(db.Integer, nullable=False, default=0)
    blocked_count = db.Column(db.Integer, nullable=False, default=0)
    total_delta = db.Column(Numeric(18, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    applied_at = db.Column(db.DateTime)
    rolled_back_at = db.Column(db.DateTime)

    company = db.relationship("Company")
    user = db.relationship("User")


class PriceControllerItem(db.Model):
    __tablename__ = "price_controller_items"
    __table_args__ = (
        db.Index("ix_price_controller_items_batch", "batch_id"),
        db.Index("ix_price_controller_items_product", "product_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    batch_id = db.Column(db.Integer, db.ForeignKey("price_controller_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False, index=True)
    old_price = db.Column(Numeric(18, 2), nullable=False)
    new_price = db.Column(Numeric(18, 2), nullable=False)
    old_margin = db.Column(Numeric(18, 2), nullable=False, default=0)
    new_margin = db.Column(Numeric(18, 2), nullable=False, default=0)
    old_profit_percent = db.Column(Numeric(10, 4), nullable=False, default=0)
    new_profit_percent = db.Column(Numeric(10, 4), nullable=False, default=0)
    status = db.Column(db.String(24), nullable=False, default="ready", index=True)
    reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    rolled_back_at = db.Column(db.DateTime)
    product = db.relationship("Product")


def _decimal(value: object, default: Optional[Decimal] = None) -> Optional[Decimal]:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value).replace(",", ".")).quantize(FOURPLACES)
    except (InvalidOperation, TypeError, ValueError):
        return default


def _money(value: object) -> Decimal:
    result = _decimal(value, Decimal("0")) or Decimal("0")
    return result.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _pct(old_price: Decimal, new_price: Decimal) -> Decimal:
    if old_price == 0:
        return Decimal("0.0000")
    return ((new_price - old_price) / old_price * Decimal("100")).quantize(FOURPLACES, rounding=ROUND_HALF_UP)


def _profit_percent(price: Decimal, cost: Decimal) -> Decimal:
    if cost <= 0:
        return Decimal("0.0000")
    return (((price - cost) / cost) * Decimal("100")).quantize(FOURPLACES, rounding=ROUND_HALF_UP)


def _rounded(price: Decimal, rounding: str) -> Decimal:
    unit = ROUNDING_UNITS.get(rounding)
    if not unit:
        return price.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    return ((price / unit).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * unit).quantize(TWOPLACES)


def _parse_rules(form):
    adjustment_type = (form.get("adjustment_type") or "percent").strip().lower()
    direction = (form.get("direction") or "increase").strip().lower()
    if adjustment_type not in {"percent", "fixed", "set"}:
        raise ValueError("Tipo de ajuste inválido.")
    if direction not in {"increase", "decrease"}:
        direction = "increase"

    value = _decimal(form.get("adjustment_value"))
    if value is None or value < 0:
        raise ValueError("El valor del ajuste debe ser un número positivo.")
    if adjustment_type == "percent" and value > Decimal("100") and direction == "decrease":
        raise ValueError("Una disminución porcentual no puede superar el 100%.")
    if adjustment_type == "set":
        direction = "increase"

    min_price = _decimal(form.get("min_price"))
    max_price = _decimal(form.get("max_price"))
    min_margin = _decimal(form.get("min_margin_percent"))
    max_change = _decimal(form.get("max_change_percent"))

    if min_price is not None and min_price < 0:
        raise ValueError("El precio mínimo no puede ser negativo.")
    if max_price is not None and max_price < 0:
        raise ValueError("El precio máximo no puede ser negativo.")
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValueError("El precio mínimo no puede ser mayor que el máximo.")
    if min_margin is not None and min_margin < 0:
        raise ValueError("El margen mínimo no puede ser negativo.")
    if max_change is not None and max_change < 0:
        raise ValueError("El cambio máximo permitido no puede ser negativo.")

    return {
        "name": (form.get("name") or "Ajuste global de precios").strip()[:120] or "Ajuste global de precios",
        "adjustment_type": adjustment_type,
        "direction": direction,
        "adjustment_value": value.quantize(FOURPLACES),
        "category": (form.get("category") or "").strip()[:100] or None,
        "brand": (form.get("brand") or "").strip()[:120] or None,
        "supplier": (form.get("supplier") or "").strip()[:160] or None,
        "only_in_stock": form.get("only_in_stock") in {"1", "true", "on", "yes"},
        "rounding": (form.get("rounding") or "none").strip().lower() if (form.get("rounding") or "none").strip().lower() in ROUNDING_UNITS else "none",
        "min_price": min_price.quantize(TWOPLACES) if min_price is not None else None,
        "max_price": max_price.quantize(TWOPLACES) if max_price is not None else None,
        "min_margin_percent": min_margin.quantize(FOURPLACES) if min_margin is not None else None,
        "max_change_percent": max_change.quantize(FOURPLACES) if max_change is not None else None,
    }


def _calculate_new_price(old_price: Decimal, rules: dict) -> Decimal:
    value = rules["adjustment_value"]
    if rules["adjustment_type"] == "percent":
        factor = value / Decimal("100")
        new_price = old_price * (Decimal("1") + factor if rules["direction"] == "increase" else Decimal("1") - factor)
    elif rules["adjustment_type"] == "fixed":
        new_price = old_price + value if rules["direction"] == "increase" else old_price - value
    else:
        new_price = value

    if rules["min_price"] is not None:
        new_price = max(new_price, rules["min_price"])
    if rules["max_price"] is not None:
        new_price = min(new_price, rules["max_price"])
    return _rounded(new_price, rules["rounding"])


def _products_for_rules(rules):
    query = scope_query_to_company(Product.query.filter(Product.active.is_(True)), Product)
    if rules["category"]:
        query = query.filter(Product.category == rules["category"])
    if rules["brand"]:
        query = query.filter(Product.brand == rules["brand"])
    if rules["supplier"]:
        # The selector is populated only from the tenant's Supplier master.
        # Keep compatibility with legacy products that only have the supplier
        # name in Product.supplier and no supplier_id.
        company_id = current_user.company_id
        supplier_row = Supplier.query.filter_by(
            company_id=company_id,
            name=rules["supplier"],
            active=True,
        ).first()
        if supplier_row is None:
            # Never accept a supplier value that is not part of the current tenant.
            return query.filter(db.false())
        query = query.filter(
            db.or_(
                Product.supplier_id == supplier_row.id,
                Product.supplier == supplier_row.name,
            )
        )
    if rules["only_in_stock"]:
        query = query.filter(Product.stock > 0)
    return query.order_by(Product.id)




def _ai_pricing_entitlement():
    company_id = get_current_company_id(current_user)
    company = Company.query.filter_by(id=company_id, active=True).first() if company_id else None
    if company is None:
        flash("No se encontró la empresa activa.", "danger")
        return redirect(url_for("dashboard.index"))
    access = can_use_ai_feature(company, "pricing_controller")
    if not access.allowed:
        flash(access.reason or "Tu plan IA no incluye el Controlador global de precios.", "warning")
        return redirect(url_for("ai_agents.agent", agent="planes"))
    return None

def _form_context(preview=None):
    query = scope_query_to_company(Product.query.filter(Product.active.is_(True)), Product)
    categories = [row[0] for row in query.with_entities(Product.category).distinct().order_by(Product.category).all() if row[0]]
    brands = [row[0] for row in query.with_entities(Product.brand).distinct().order_by(Product.brand).all() if row[0]]
    suppliers = [
        row[0]
        for row in scope_query_to_company(
            Supplier.query.filter(Supplier.active.is_(True)),
            Supplier,
        )
        .with_entities(Supplier.name)
        .distinct()
        .order_by(Supplier.name)
        .all()
        if row[0]
    ]
    history = PriceControllerBatch.query.filter_by(company_id=current_user.company_id).order_by(PriceControllerBatch.id.desc()).limit(12).all()
    preview_rows = []
    if preview is not None:
        preview_rows = (
            PriceControllerItem.query.filter_by(batch_id=preview.id)
            .join(Product, Product.id == PriceControllerItem.product_id)
            .order_by(PriceControllerItem.id)
            .limit(100)
            .all()
        )
    return {
        "categories": categories,
        "brands": brands,
        "suppliers": suppliers,
        "history": history,
        "preview": preview,
        "preview_rows": preview_rows,
    }


@bp.get("/")
@company_admin_required
def index():
    blocked = _ai_pricing_entitlement()
    if blocked is not None:
        return blocked
    return render_template("precios/controlador.html", **_form_context())


@bp.post("/preview")
@company_admin_required
def preview():
    blocked = _ai_pricing_entitlement()
    if blocked is not None:
        return blocked
    try:
        rules = _parse_rules(request.form)
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("pricing_controller.index"))

    batch = PriceControllerBatch(
        company_id=current_user.company_id,
        user_id=current_user.id,
        **rules,
        status="preview",
    )
    db.session.add(batch)
    db.session.flush()

    changed = blocked = 0
    total_delta = Decimal("0")
    for product in _products_for_rules(rules).yield_per(1000):
        old_price = _money(product.price)
        new_price = _calculate_new_price(old_price, rules)
        if new_price == old_price:
            continue

        cost = _money(product.cost_price)
        new_margin = (new_price - cost).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
        new_profit = _profit_percent(new_price, cost)
        item_status = "ready"
        reason = None
        change_pct = abs(_pct(old_price, new_price))

        if new_price < 0:
            item_status = "blocked"
            reason = "El precio resultante sería negativo."
        elif rules["max_change_percent"] is not None and change_pct > rules["max_change_percent"]:
            item_status = "blocked"
            reason = f"Supera el cambio máximo permitido ({rules['max_change_percent']}%)."
        elif rules["min_margin_percent"] is not None and cost > 0 and new_profit < rules["min_margin_percent"]:
            item_status = "blocked"
            reason = f"Queda por debajo del margen mínimo ({rules['min_margin_percent']}%)."

        item = PriceControllerItem(
            batch_id=batch.id,
            product_id=product.id,
            old_price=old_price,
            new_price=new_price,
            old_margin=(old_price - cost).quantize(TWOPLACES, rounding=ROUND_HALF_UP),
            new_margin=new_margin,
            old_profit_percent=_profit_percent(old_price, cost),
            new_profit_percent=new_profit,
            status=item_status,
            reason=reason,
        )
        db.session.add(item)
        changed += 1
        if item_status == "blocked":
            blocked += 1
        else:
            total_delta += new_price - old_price

        if changed % 1000 == 0:
            db.session.flush()

    batch.product_count = changed
    batch.blocked_count = blocked
    batch.total_delta = total_delta.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    db.session.commit()
    return render_template("precios/controlador.html", **_form_context(preview=batch))


@bp.post("/apply/<int:batch_id>")
@company_admin_required
def apply(batch_id: int):
    blocked = _ai_pricing_entitlement()
    if blocked is not None:
        return blocked
    batch = PriceControllerBatch.query.filter_by(id=batch_id, company_id=current_user.company_id).first_or_404()
    if batch.status != "preview":
        flash("Ese ajuste ya fue aplicado o no está disponible para aplicar.", "warning")
        return redirect(url_for("pricing_controller.index"))

    applied = 0
    skipped = 0
    try:
        from app import ProductModification, ProductPriceHistory

        items = PriceControllerItem.query.filter_by(batch_id=batch.id, status="ready").order_by(PriceControllerItem.id).yield_per(1000)
        for item in items:
            product = scope_query_to_company(Product.query.filter_by(id=item.product_id), Product).first()
            if product is None or _money(product.price) != _money(item.old_price):
                item.status = "conflict"
                item.reason = "El precio cambió después de la vista previa."
                skipped += 1
                continue

            old_price = _money(product.price)
            old_cost = _money(product.cost_price)
            product.price = item.new_price
            product.margin = item.new_margin
            product.profit_percent = item.new_profit_percent
            db.session.add(ProductPriceHistory(
                product_id=product.id,
                company_id=product.company_id,
                user_id=current_user.id,
                old_price=old_price,
                new_price=_money(item.new_price),
                old_cost=old_cost,
                new_cost=old_cost,
            ))
            db.session.add(ProductModification(
                product_id=product.id,
                company_id=product.company_id,
                user_id=current_user.id,
                action="cambio_precio_global",
                detail=f"Ajuste global #{batch.id}: {batch.name}",
            ))
            item.status = "applied"
            applied += 1

            if applied % 1000 == 0:
                db.session.flush()

        batch.status = "applied" if applied else "apply_conflict"
        batch.product_count = applied
        batch.applied_at = utcnow()
        record_audit(
            action="price_controller_apply",
            entity="price_controller_batch",
            entity_id=batch.id,
            detail=f"Aplicado={applied} omitidos={skipped} batch={batch.id}",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    if applied:
        flash(f"Precios actualizados: {applied} productos. {skipped} se omitieron por cambios posteriores.", "success")
    else:
        flash("No se aplicó ningún precio porque los productos habían cambiado desde la vista previa.", "warning")
    return redirect(url_for("pricing_controller.index"))


@bp.post("/rollback/<int:batch_id>")
@company_admin_required
def rollback(batch_id: int):
    blocked = _ai_pricing_entitlement()
    if blocked is not None:
        return blocked
    company_id = get_current_company_id(current_user)
    company = Company.query.filter_by(id=company_id, active=True).first() if company_id else None
    rollback_access = can_use_ai_feature(company, "pricing_rollback") if company is not None else None
    if rollback_access is None or not rollback_access.allowed:
        flash(rollback_access.reason or "El rollback de precios requiere IA PRO.", "warning")
        return redirect(url_for("pricing_controller.index"))
    batch = PriceControllerBatch.query.filter_by(id=batch_id, company_id=current_user.company_id).first_or_404()
    if batch.status not in {"applied", "partial_rollback"}:
        flash("Ese ajuste no puede revertirse desde su estado actual.", "warning")
        return redirect(url_for("pricing_controller.index"))

    restored = conflicts = 0
    try:
        from app import ProductModification, ProductPriceHistory

        items = PriceControllerItem.query.filter_by(batch_id=batch.id, status="applied").order_by(PriceControllerItem.id).yield_per(1000)
        for item in items:
            product = scope_query_to_company(Product.query.filter_by(id=item.product_id), Product).first()
            if product is None or _money(product.price) != _money(item.new_price):
                item.status = "rollback_conflict"
                item.reason = "No se revirtió porque el precio actual fue modificado posteriormente."
                conflicts += 1
                continue

            current_cost = _money(product.cost_price)
            product.price = item.old_price
            product.margin = item.old_margin
            product.profit_percent = item.old_profit_percent
            db.session.add(ProductPriceHistory(
                product_id=product.id,
                company_id=product.company_id,
                user_id=current_user.id,
                old_price=_money(item.new_price),
                new_price=_money(item.old_price),
                old_cost=current_cost,
                new_cost=current_cost,
            ))
            db.session.add(ProductModification(
                product_id=product.id,
                company_id=product.company_id,
                user_id=current_user.id,
                action="reversion_precio_global",
                detail=f"Reversión ajuste global #{batch.id}: {batch.name}",
            ))
            item.status = "reverted"
            item.rolled_back_at = utcnow()
            restored += 1

            if restored % 1000 == 0:
                db.session.flush()

        batch.status = "rolled_back" if conflicts == 0 else "partial_rollback"
        batch.rolled_back_at = utcnow()
        record_audit(
            action="price_controller_rollback",
            entity="price_controller_batch",
            entity_id=batch.id,
            detail=f"Revertidos={restored} conflictos={conflicts} batch={batch.id}",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    if conflicts:
        flash(f"Reversión parcial: {restored} restaurados y {conflicts} protegidos porque cambiaron después.", "warning")
    else:
        flash(f"Ajuste revertido correctamente: {restored} productos.", "success")
    return redirect(url_for("pricing_controller.index"))

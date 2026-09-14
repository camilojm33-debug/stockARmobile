"""Service layer for safe AI access to the global price controller.

The AI never mutates Product.price directly. It creates a persisted preview batch
through the same pricing controller models/calculations and can apply/rollback a
batch only when an authenticated tenant administrator explicitly invokes the
confirmation tool.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func

from app import Product, Sale, SaleItem, User, db, record_audit, utcnow
from pricing_controller import (
    PriceControllerBatch,
    PriceControllerItem,
    _calculate_new_price,
    _money,
    _parse_rules,
    _profit_percent,
    _products_for_rules,
)


def _admin_user(company_id: int, user_id: int | None):
    if not user_id:
        return None, "La confirmación de precios requiere un usuario administrador autenticado."
    user = db.session.get(User, int(user_id))
    if user is None or getattr(user, "company_id", None) != company_id:
        return None, "El usuario no pertenece a la empresa activa."
    if getattr(user, "role", None) not in {"admin", "superadmin"}:
        return None, "Solo un administrador puede confirmar o revertir cambios globales de precios."
    return user, None


def _rules_form(rules: dict):
    return {
        "name": rules.get("name") or "Propuesta IA de precios",
        "adjustment_type": rules.get("adjustment_type") or "percent",
        "direction": rules.get("direction") or "increase",
        "adjustment_value": rules.get("adjustment_value"),
        "category": rules.get("category") or "",
        "brand": rules.get("brand") or "",
        "supplier": rules.get("supplier") or "",
        "only_in_stock": "1" if rules.get("only_in_stock") else "0",
        "rounding": rules.get("rounding") or "none",
        "min_price": rules.get("min_price"),
        "max_price": rules.get("max_price"),
        "min_margin_percent": rules.get("min_margin_percent"),
        "max_change_percent": rules.get("max_change_percent"),
    }


def create_preview(*, company_id: int, user_id: int | None, rules: dict) -> dict:
    normalized = _parse_rules(_rules_form(rules))
    batch = PriceControllerBatch(
        company_id=company_id,
        user_id=int(user_id) if user_id else int(rules.get("actor_user_id") or 0),
        **normalized,
        status="preview",
    )
    if not batch.user_id:
        raise ValueError("No se pudo asociar la propuesta de precios con un usuario.")
    db.session.add(batch)
    db.session.flush()

    changed = blocked = 0
    total_delta = Decimal("0")
    sample = []

    for product in _products_for_rules(normalized).yield_per(1000):
        old_price = _money(product.price)
        new_price = _calculate_new_price(old_price, normalized)
        if new_price == old_price:
            continue

        cost = _money(product.cost_price)
        new_margin = (new_price - cost).quantize(Decimal("0.01"))
        new_profit = _profit_percent(new_price, cost)
        status = "ready"
        reason = None
        change_pct = abs(((new_price - old_price) / old_price * Decimal("100"))) if old_price else Decimal("0")

        if new_price < 0:
            status = "blocked"
            reason = "El precio resultante sería negativo."
        elif normalized["max_change_percent"] is not None and change_pct > normalized["max_change_percent"]:
            status = "blocked"
            reason = f"Supera el cambio máximo permitido ({normalized['max_change_percent']}%)."
        elif normalized["min_margin_percent"] is not None and cost > 0 and new_profit < normalized["min_margin_percent"]:
            status = "blocked"
            reason = f"Queda por debajo del margen mínimo ({normalized['min_margin_percent']}%)."

        db.session.add(
            PriceControllerItem(
                batch_id=batch.id,
                product_id=product.id,
                old_price=old_price,
                new_price=new_price,
                old_margin=(old_price - cost).quantize(Decimal("0.01")),
                new_margin=new_margin,
                old_profit_percent=_profit_percent(old_price, cost),
                new_profit_percent=new_profit,
                status=status,
                reason=reason,
            )
        )
        changed += 1
        if status == "blocked":
            blocked += 1
        else:
            total_delta += new_price - old_price

        if len(sample) < 10:
            sample.append(
                {
                    "product_id": product.id,
                    "name": product.name,
                    "old_price": float(old_price),
                    "new_price": float(new_price),
                    "old_profit_percent": float(_profit_percent(old_price, cost)),
                    "new_profit_percent": float(new_profit),
                    "status": status,
                    "reason": reason,
                }
            )

    batch.product_count = changed
    batch.blocked_count = blocked
    batch.total_delta = total_delta.quantize(Decimal("0.01"))
    db.session.commit()

    return {
        "success": True,
        "batch_id": batch.id,
        "status": batch.status,
        "product_count": changed,
        "blocked_count": blocked,
        "total_delta": float(batch.total_delta or 0),
        "sample": sample,
        "requires_confirmation": bool(changed and (changed - blocked) > 0),
        "message": "Vista previa creada. Ningún precio fue modificado todavía.",
    }


def apply_batch(*, company_id: int, user_id: int | None, batch_id: int) -> dict:
    actor, error = _admin_user(company_id, user_id)
    if error:
        return {"success": False, "error": error}

    batch = PriceControllerBatch.query.filter_by(id=int(batch_id), company_id=company_id).first()
    if batch is None:
        return {"success": False, "error": "No existe esa propuesta de precios."}
    if batch.status != "preview":
        return {"success": False, "error": f"La propuesta no está pendiente de confirmación (estado={batch.status})."}

    applied = 0
    conflicts = 0
    try:
        from app import ProductModification, ProductPriceHistory

        items = PriceControllerItem.query.filter_by(batch_id=batch.id, status="ready").order_by(PriceControllerItem.id).yield_per(1000)
        for item in items:
            product = Product.query.filter_by(id=item.product_id, company_id=company_id, active=True).first()
            if product is None or _money(product.price) != _money(item.old_price):
                item.status = "conflict"
                item.reason = "El precio cambió después de la vista previa."
                conflicts += 1
                continue

            old_price = _money(product.price)
            old_cost = _money(product.cost_price)
            product.price = item.new_price
            product.margin = item.new_margin
            product.profit_percent = item.new_profit_percent
            db.session.add(ProductPriceHistory(
                product_id=product.id,
                company_id=company_id,
                user_id=actor.id,
                old_price=old_price,
                new_price=_money(item.new_price),
                old_cost=old_cost,
                new_cost=old_cost,
            ))
            db.session.add(ProductModification(
                product_id=product.id,
                company_id=company_id,
                user_id=actor.id,
                action="cambio_precio_global_ia",
                detail=f"Confirmación IA lote #{batch.id}: {batch.name}",
            ))
            item.status = "applied"
            applied += 1

        batch.status = "applied" if applied else "apply_conflict"
        batch.product_count = applied
        batch.applied_at = utcnow()
        record_audit(
            action="price_controller_apply_ai",
            entity="price_controller_batch",
            entity_id=batch.id,
            detail=f"IA confirmó lote={batch.id} aplicado={applied} conflictos={conflicts}",
            user_id=actor.id,
            company_id=company_id,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return {
        "success": True,
        "batch_id": batch.id,
        "status": batch.status,
        "applied": applied,
        "conflicts": conflicts,
        "message": "Cambios de precios aplicados con protección contra modificaciones concurrentes." if applied else "No se aplicaron precios porque todos los productos tenían conflictos.",
    }


def rollback_batch(*, company_id: int, user_id: int | None, batch_id: int) -> dict:
    actor, error = _admin_user(company_id, user_id)
    if error:
        return {"success": False, "error": error}

    batch = PriceControllerBatch.query.filter_by(id=int(batch_id), company_id=company_id).first()
    if batch is None:
        return {"success": False, "error": "No existe ese lote de precios."}
    if batch.status not in {"applied", "partial_rollback"}:
        return {"success": False, "error": f"El lote no puede revertirse desde el estado {batch.status}."}

    restored = conflicts = 0
    try:
        from app import ProductModification, ProductPriceHistory

        items = PriceControllerItem.query.filter_by(batch_id=batch.id, status="applied").order_by(PriceControllerItem.id).yield_per(1000)
        for item in items:
            product = Product.query.filter_by(id=item.product_id, company_id=company_id, active=True).first()
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
                company_id=company_id,
                user_id=actor.id,
                old_price=_money(item.new_price),
                new_price=_money(item.old_price),
                old_cost=current_cost,
                new_cost=current_cost,
            ))
            db.session.add(ProductModification(
                product_id=product.id,
                company_id=company_id,
                user_id=actor.id,
                action="reversion_precio_global_ia",
                detail=f"Reversión IA lote #{batch.id}: {batch.name}",
            ))
            item.status = "reverted"
            item.rolled_back_at = utcnow()
            restored += 1

        batch.status = "rolled_back" if conflicts == 0 else "partial_rollback"
        batch.rolled_back_at = utcnow()
        record_audit(
            action="price_controller_rollback_ai",
            entity="price_controller_batch",
            entity_id=batch.id,
            detail=f"IA revirtió lote={batch.id} restaurados={restored} conflictos={conflicts}",
            user_id=actor.id,
            company_id=company_id,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return {
        "success": True,
        "batch_id": batch.id,
        "status": batch.status,
        "restored": restored,
        "conflicts": conflicts,
    }


def consult_prices(*, company_id: int, query: str = "", category: str = "", brand: str = "", supplier: str = "", limit: int = 20) -> dict:
    limit = max(1, min(int(limit or 20), 40))
    products = Product.query.filter_by(company_id=company_id, active=True)
    if query:
        like = f"%{query.strip()}%"
        products = products.filter((Product.name.ilike(like)) | (Product.barcode.ilike(like)) | (Product.brand.ilike(like)))
    if category:
        products = products.filter(Product.category == category)
    if brand:
        products = products.filter(Product.brand == brand)
    if supplier:
        products = products.filter(Product.supplier == supplier)

    rows = []
    for product in products.order_by(Product.name).limit(limit).all():
        rows.append(
            {
                "product_id": product.id,
                "name": product.name,
                "barcode": product.barcode,
                "category": product.category or "",
                "brand": product.brand or "",
                "supplier": product.supplier or "",
                "cost_price": float(product.cost_price or 0),
                "price": float(product.price or 0),
                "margin": float(product.margin or 0),
                "profit_percent": float(product.profit_percent or 0),
                "stock": float(product.stock or 0),
            }
        )
    return {"success": True, "count": len(rows), "products": rows}


def pricing_opportunities(*, company_id: int, objective: str = "margin", days: int = 30, target_margin_percent: float = 20.0, limit: int = 20) -> dict:
    """Return evidence-based candidates; it does not change any price."""
    days = max(1, min(int(days or 30), 365))
    limit = max(1, min(int(limit or 20), 40))
    target = Decimal(str(target_margin_percent or 20.0))
    cutoff = utcnow() - timedelta(days=days)

    sales_rows = (
        db.session.query(SaleItem.product_id, func.coalesce(func.sum(SaleItem.quantity), 0))
        .join(Sale, Sale.id == SaleItem.sale_id)
        .filter(Sale.company_id == company_id, Sale.date >= cutoff)
        .group_by(SaleItem.product_id)
        .all()
    )
    units_by_product = {int(product_id): float(units or 0) for product_id, units in sales_rows}

    candidates = []
    for product in Product.query.filter_by(company_id=company_id, active=True).order_by(Product.name).all():
        price = _money(product.price)
        cost = _money(product.cost_price)
        margin_pct = _profit_percent(price, cost)
        units = units_by_product.get(product.id, 0.0)
        stock = float(product.stock or 0)
        min_stock = float(product.min_stock or 0)
        reason = None
        action = None

        if objective == "margin":
            if cost > 0 and margin_pct < target and units > 0:
                reason = f"Margen actual {margin_pct}% por debajo del objetivo {target}% con ventas reales en {days} días."
                action = "revisar_aumento"
        elif objective in {"liquidar_stock", "stock"}:
            if stock > max(min_stock * 2, 1) and units == 0:
                reason = f"Stock elevado ({stock:g}) y 0 unidades vendidas en {days} días."
                action = "revisar_baja"
        elif objective == "sin_ventas":
            if units == 0 and stock > 0:
                reason = f"No registra ventas en los últimos {days} días y conserva stock."
                action = "revisar_baja"
        else:
            if cost > 0 and margin_pct < target and units > 0:
                reason = f"Margen {margin_pct}% < objetivo {target}% con {units:g} unidades vendidas."
                action = "revisar_aumento"

        if reason:
            candidates.append(
                {
                    "product_id": product.id,
                    "name": product.name,
                    "category": product.category or "",
                    "price": float(price),
                    "cost_price": float(cost),
                    "profit_percent": float(margin_pct),
                    "stock": stock,
                    "units_sold_period": units,
                    "suggested_action": action,
                    "reason": reason,
                }
            )
            if len(candidates) >= limit:
                break

    return {
        "success": True,
        "objective": objective,
        "days": days,
        "target_margin_percent": float(target),
        "count": len(candidates),
        "candidates": candidates,
        "note": "Son candidatos basados en datos reales; la herramienta no predice el impacto de ventas y no modifica precios.",
    }

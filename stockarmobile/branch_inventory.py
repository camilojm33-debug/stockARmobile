"""Branch-aware stock primitives."""
from decimal import Decimal

from app import BranchStock, BranchStockMovement, db


def get_branch_stock(product, branch_id, *, lock=False, create=False):
    query = BranchStock.query.filter_by(company_id=int(product.company_id), branch_id=int(branch_id), product_id=int(product.id))
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None and create:
        row = BranchStock(company_id=int(product.company_id), branch_id=int(branch_id), product_id=int(product.id),
                          stock=Decimal("0"), min_stock=Decimal(str(getattr(product, "min_stock", 0) or 0)), reserved_stock=Decimal("0"))
        db.session.add(row)
        db.session.flush()
    return row


def available_stock(product, branch_id):
    row = get_branch_stock(product, branch_id)
    return float(row.stock or 0) if row is not None else 0.0


def set_branch_stock(product, branch_id, quantity, *, movement_type="initial", user_id=None, note=None):
    row = get_branch_stock(product, branch_id, lock=True, create=True)
    old = Decimal(str(row.stock or 0))
    new = Decimal(str(quantity or 0))
    row.stock = new
    row.min_stock = Decimal(str(getattr(product, "min_stock", 0) or 0))
    if new != old:
        db.session.add(BranchStockMovement(company_id=int(product.company_id), branch_id=int(branch_id), product_id=int(product.id),
            movement_type=movement_type, quantity_delta=new-old, stock_after=new, user_id=user_id, note=note))
    return row


def adjust_branch_stock(product, branch_id, delta, *, movement_type, source_type=None, source_id=None,
                        user_id=None, note=None, update_company_total=True):
    row = get_branch_stock(product, branch_id, lock=True, create=True)
    delta = Decimal(str(delta))
    current = Decimal(str(row.stock or 0))
    updated = current + delta
    if updated < 0:
        raise ValueError(f"Stock insuficiente en la sucursal para {product.name}. Disponible: {current}.")
    row.stock = updated
    row.min_stock = Decimal(str(getattr(product, "min_stock", 0) or 0))
    if update_company_total:
        aggregate = Decimal(str(product.stock or 0)) + delta
        if aggregate < 0:
            raise ValueError(f"El stock consolidado no puede quedar negativo para {product.name}.")
        product.stock = float(aggregate)
    db.session.add(BranchStockMovement(company_id=int(product.company_id), branch_id=int(branch_id), product_id=int(product.id),
        movement_type=movement_type, quantity_delta=delta, stock_after=updated,
        source_type=source_type, source_id=source_id, user_id=user_id, note=note))
    return row

"""Branch-aware stock primitives."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import func

from app import Branch, BranchStock, BranchStockMovement, db


STOCK_QUANTUM = Decimal("0.001")


def validate_stock_quantity(quantity, *, label="La cantidad"):
    """Return a finite stock quantity with at most three meaningful decimals."""
    try:
        amount = Decimal(str(quantity))
        normalized = amount.quantize(STOCK_QUANTUM, rounding=ROUND_HALF_UP)
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError(f"{label} no es válida.") from None
    if not amount.is_finite():
        raise ValueError(f"{label} debe ser un número finito.")
    if amount != normalized:
        raise ValueError(f"{label} debe tener como máximo tres decimales.")
    return normalized


def _normalize_legacy_stock_quantity(quantity, *, label="El movimiento de stock"):
    """Normalize historical values written before the three-decimal rule existed."""
    try:
        amount = Decimal(str(quantity))
        if not amount.is_finite():
            raise InvalidOperation
        return amount.quantize(STOCK_QUANTUM, rounding=ROUND_HALF_UP)
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError(f"{label} no es válida.") from None


def reconcile_company_stock_total(product):
    """Rebuild Product.stock from the persisted per-branch balances."""
    total = (
        db.session.query(func.coalesce(func.sum(BranchStock.stock), 0))
        .filter_by(company_id=int(product.company_id), product_id=int(product.id))
        .scalar()
    )
    product.stock = float(total or 0)
    return Decimal(str(total or 0))


def lock_product_stock_row(product):
    """Acquire the product row lock before any branch stock row lock."""
    from app import Product
    product_id = db.session.query(Product.id).filter_by(
        id=int(product.id), company_id=int(product.company_id)
    ).with_for_update().scalar()
    if product_id is None:
        raise ValueError("El producto no pertenece a la empresa activa.")
    return product_id


def get_branch_stock(product, branch_id, *, lock=False, create=False):
    branch = Branch.query.filter_by(id=int(branch_id), company_id=int(product.company_id), active=True).first()
    if branch is None:
        raise ValueError("La sucursal no pertenece a la empresa del producto o está inactiva.")
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


def transfer_branch_stock(product, source_branch_id, target_branch_id, quantity, *, user_id=None, note=None):
    """Move units between branches atomically; company aggregate stock is unchanged."""
    from decimal import Decimal, InvalidOperation
    from app import Branch, BranchStockTransfer, db

    amount = validate_stock_quantity(quantity, label="La cantidad de transferencia")
    if amount <= 0:
        raise ValueError("La cantidad de transferencia debe ser mayor que cero.")

    company_id = int(getattr(product, "company_id", 0) or 0)
    lock_product_stock_row(product)
    source = Branch.query.filter_by(id=int(source_branch_id), company_id=company_id, active=True).first()
    target = Branch.query.filter_by(id=int(target_branch_id), company_id=company_id, active=True).first()
    if source is None or target is None or source.id == target.id:
        raise ValueError("El origen y destino deben ser sucursales activas de la misma empresa.")

    # Lock rows in stable order so concurrent inverse transfers do not deadlock.
    locked_rows = {}
    for branch_id in sorted((int(source.id), int(target.id))):
        locked_rows[branch_id] = get_branch_stock(product, branch_id, lock=True, create=True)
    source_stock = locked_rows[int(source.id)]
    if Decimal(str(source_stock.stock or 0)) < amount:
        raise ValueError(f"Stock insuficiente en {source.name}. Disponible: {source_stock.stock}.")

    transfer = BranchStockTransfer(
        company_id=company_id,
        source_branch_id=source.id,
        target_branch_id=target.id,
        product_id=product.id,
        quantity=amount,
        note=(str(note or "").strip()[:255] or None),
        created_by_user_id=user_id,
    )
    db.session.add(transfer)
    db.session.flush()
    adjust_branch_stock(
        product, source.id, -amount, movement_type="transfer_out",
        source_type="branch_transfer", source_id=transfer.id, user_id=user_id,
        update_company_total=False, note=f"Transferencia hacia {target.name}",
    )
    adjust_branch_stock(
        product, target.id, amount, movement_type="transfer_in",
        source_type="branch_transfer", source_id=transfer.id, user_id=user_id,
        update_company_total=False, note=f"Transferencia desde {source.name}",
    )
    return transfer


def set_branch_stock(product, branch_id, quantity, *, movement_type="initial", user_id=None, note=None):
    lock_product_stock_row(product)
    row = get_branch_stock(product, branch_id, lock=True, create=True)
    old = Decimal(str(row.stock or 0))
    new = validate_stock_quantity(quantity or 0, label="El stock de sucursal")
    if new < 0:
        raise ValueError("El stock de sucursal debe ser un número finito no negativo.")
    row.stock = new
    row.min_stock = Decimal(str(getattr(product, "min_stock", 0) or 0))
    if new != old:
        db.session.add(BranchStockMovement(company_id=int(product.company_id), branch_id=int(branch_id), product_id=int(product.id),
            movement_type=movement_type, quantity_delta=new-old, stock_after=new, user_id=user_id, note=note))
    reconcile_company_stock_total(product)
    return row


def adjust_branch_stock(product, branch_id, delta, *, movement_type, source_type=None, source_id=None,
                        user_id=None, note=None, update_company_total=True, allow_legacy_precision=False):
    delta = (
        _normalize_legacy_stock_quantity(delta)
        if allow_legacy_precision
        else validate_stock_quantity(delta, label="El movimiento de stock")
    )
    # All aggregate-changing writes lock Product before BranchStock. This
    # serializes sales in different branches and prevents lost aggregate updates.
    if update_company_total:
        from app import Product
        locked_product = (
            Product.query.filter_by(id=int(product.id), company_id=int(product.company_id))
            .with_for_update().populate_existing().first()
        )
        if locked_product is None:
            raise ValueError("El producto no pertenece a la empresa activa.")
        product = locked_product
    row = get_branch_stock(product, branch_id, lock=True, create=True)
    current = Decimal(str(row.stock or 0))
    updated = current + delta
    if updated < 0:
        raise ValueError(f"Stock insuficiente en la sucursal para {product.name}. Disponible: {current}.")
    row.stock = updated
    row.min_stock = Decimal(str(getattr(product, "min_stock", 0) or 0))
    if update_company_total:
        aggregate = reconcile_company_stock_total(product)
        if aggregate < 0:
            raise ValueError(f"El stock consolidado no puede quedar negativo para {product.name}.")
    db.session.add(BranchStockMovement(company_id=int(product.company_id), branch_id=int(branch_id), product_id=int(product.id),
        movement_type=movement_type, quantity_delta=delta, stock_after=updated,
        source_type=source_type, source_id=source_id, user_id=user_id, note=note))
    return row

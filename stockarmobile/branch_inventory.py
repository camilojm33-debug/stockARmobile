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


def get_branch_stock(product, branch_id, *, lock=False, create=False, allow_inactive=False):
    branch_query = Branch.query.filter_by(id=int(branch_id), company_id=int(product.company_id))
    if not allow_inactive:
        branch_query = branch_query.filter_by(active=True)
    branch = branch_query.first()
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


def available_stock(product, branch_id, *, allow_inactive=False):
    """Return physical stock minus units already reserved for pending POS payments."""
    row = get_branch_stock(product, branch_id, allow_inactive=allow_inactive)
    if row is None:
        return 0.0
    stock = Decimal(str(row.stock or 0))
    reserved = Decimal(str(row.reserved_stock or 0))
    return float(max(stock - reserved, Decimal("0")))


def transfer_branch_stock(product, source_branch_id, target_branch_id, quantity, *, user_id=None, note=None, request_key=None):
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
    available = Decimal(str(source_stock.stock or 0)) - Decimal(str(source_stock.reserved_stock or 0))
    if available < amount:
        raise ValueError(f"Stock disponible insuficiente en {source.name}. Disponible: {max(available, Decimal('0'))}.")

    transfer = BranchStockTransfer(
        company_id=company_id,
        source_branch_id=source.id,
        target_branch_id=target.id,
        product_id=product.id,
        quantity=amount,
        request_key=(str(request_key or "").strip()[:80] or None),
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


def transfer_branch_stock_batch(items, source_branch_id, target_branch_id, *, user_id=None, note=None, request_key=None):
    """Transfer several products in one caller-controlled database transaction.

    Products are locked in ascending ID order so concurrent multi-product
    transfers acquire product locks consistently. The caller must commit only
    after this function returns and roll back if any line raises ValueError.
    """
    normalized = []
    seen_product_ids = set()
    for item in items or ():
        try:
            product, quantity = item
        except (TypeError, ValueError):
            raise ValueError("La lista de productos de la transferencia no es válida.") from None
        product_id = int(getattr(product, "id", 0) or 0)
        if product_id <= 0:
            raise ValueError("Hay un producto inválido en la transferencia.")
        if product_id in seen_product_ids:
            raise ValueError("No repitas un producto en la misma transferencia.")
        seen_product_ids.add(product_id)
        normalized.append((product_id, product, quantity))

    if not normalized:
        raise ValueError("Agregá al menos un producto a la transferencia.")
    if len(normalized) > 100:
        raise ValueError("Una transferencia puede incluir hasta 100 productos por operación.")

    normalized.sort(key=lambda row: row[0])
    transfers = []
    for _product_id, product, quantity in normalized:
        transfers.append(transfer_branch_stock(
            product,
            source_branch_id,
            target_branch_id,
            quantity,
            user_id=user_id,
            note=note,
            request_key=request_key,
        ))
    return transfers


def set_branch_stock(product, branch_id, quantity, *, movement_type="initial", user_id=None, note=None):
    lock_product_stock_row(product)
    row = get_branch_stock(product, branch_id, lock=True, create=True)
    old = Decimal(str(row.stock or 0))
    new = validate_stock_quantity(quantity or 0, label="El stock de sucursal")
    if new < 0:
        raise ValueError("El stock de sucursal debe ser un número finito no negativo.")
    reserved = Decimal(str(row.reserved_stock or 0))
    if new < reserved:
        raise ValueError("El ajuste dejaría por debajo del stock reservado para cobros QR pendientes.")
    row.stock = new
    row.min_stock = Decimal(str(getattr(product, "min_stock", 0) or 0))
    if new != old:
        db.session.add(BranchStockMovement(company_id=int(product.company_id), branch_id=int(branch_id), product_id=int(product.id),
            movement_type=movement_type, quantity_delta=new-old, stock_after=new, user_id=user_id, note=note))
    return row


def set_branch_stock_and_reconcile(product, branch_id, quantity, *, movement_type="manual_adjustment", user_id=None, note=None):
    """Set one branch balance and safely rebuild the company aggregate.

    Lock the product before reading or changing branch rows. Refresh only the
    aggregate column so a caller's other pending product edits are preserved.
    """
    with db.session.no_autoflush:
        lock_product_stock_row(product)
        db.session.refresh(product, attribute_names=["stock"])
    row = set_branch_stock(
        product, branch_id, quantity, movement_type=movement_type,
        user_id=user_id, note=note,
    )
    reconcile_company_stock_total(product)
    return row



def reserve_branch_stock_for_payment(payment, items, branch_id, *, user_id=None):
    """Atomically reserve branch stock for a pending POS QR payment."""
    from app import BranchStockReservation

    if branch_id is None:
        return []
    normalized = []
    for product, quantity in items or ():
        qty = validate_stock_quantity(quantity, label="La cantidad reservada")
        if product is None or qty <= 0:
            raise ValueError("No se puede reservar una línea de producto inválida.")
        normalized.append((int(product.id), product, qty))
    if not normalized:
        raise ValueError("No hay productos para reservar.")
    if len({row[0] for row in normalized}) != len(normalized):
        raise ValueError("No se puede reservar el mismo producto más de una vez.")

    reservations = []
    for product_id, product, qty in sorted(normalized, key=lambda row: row[0]):
        lock_product_stock_row(product)
        if int(product.company_id) != int(payment.company_id):
            raise ValueError("El producto de la reserva pertenece a otra empresa.")
        row = get_branch_stock(product, branch_id, lock=True, create=True)
        existing = BranchStockReservation.query.filter_by(
            payment_id=int(payment.id), product_id=product_id
        ).with_for_update().first()
        if existing is not None:
            if existing.status == "reserved" and Decimal(str(existing.quantity)) == qty:
                reservations.append(existing)
                continue
            raise ValueError("El QR ya tiene una reserva de stock incompatible.")
        stock = Decimal(str(row.stock or 0))
        reserved = Decimal(str(row.reserved_stock or 0))
        available = stock - reserved
        if qty > available:
            raise ValueError(f"Stock disponible insuficiente para reservar {product.name}. Disponible: {max(available, Decimal('0'))}.")
        row.reserved_stock = reserved + qty
        reservation = BranchStockReservation(
            payment_id=int(payment.id),
            company_id=int(payment.company_id),
            branch_id=int(branch_id),
            product_id=product_id,
            quantity=qty,
            status="reserved",
        )
        db.session.add(reservation)
        reservations.append(reservation)
    db.session.flush()
    return reservations


def release_branch_stock_reservations(payment_id, *, user_id=None):
    """Release every active reservation belonging to one POS payment exactly once."""
    from app import BranchStockReservation, Product, utcnow

    rows = BranchStockReservation.query.filter_by(
        payment_id=int(payment_id), status="reserved"
    ).order_by(BranchStockReservation.product_id.asc()).with_for_update().all()
    released = 0
    for reservation in rows:
        product = Product.query.filter_by(
            id=int(reservation.product_id), company_id=int(reservation.company_id)
        ).first()
        if product is not None:
            lock_product_stock_row(product)
            stock_row = get_branch_stock(
                product, reservation.branch_id, lock=True, create=False, allow_inactive=True
            )
            if stock_row is not None:
                current_reserved = Decimal(str(stock_row.reserved_stock or 0))
                quantity = Decimal(str(reservation.quantity or 0))
                stock_row.reserved_stock = max(current_reserved - quantity, Decimal("0"))
        reservation.status = "released"
        reservation.released_at = utcnow()
        released += 1
    db.session.flush()
    return released


def adjust_branch_stock(product, branch_id, delta, *, movement_type, source_type=None, source_id=None,
                        user_id=None, note=None, update_company_total=True, allow_legacy_precision=False,
                        allow_inactive=False):
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
    row = get_branch_stock(product, branch_id, lock=True, create=True, allow_inactive=allow_inactive)
    current = Decimal(str(row.stock or 0))
    updated = current + delta
    if updated < 0:
        raise ValueError(f"Stock insuficiente en la sucursal para {product.name}. Disponible: {current}.")
    reserved = Decimal(str(row.reserved_stock or 0))
    if delta < 0 and updated < reserved:
        raise ValueError(f"Stock reservado para cobros pendientes en {product.name}. Disponible para operar: {max(current - reserved, Decimal('0'))}.")
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

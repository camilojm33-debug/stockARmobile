"""Per-line physical pickup tracking for standard and AI quotes."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func

from app import db


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Ingresá una cantidad válida.")
    if not result.is_finite():
        raise ValueError("Ingresá una cantidad válida.")
    return result


def pickup_rows_for_quote(quote):
    """Return item-level picked/remaining quantities and latest event metadata."""
    from app import QuoteItemPickupEvent

    items = list(getattr(quote, "items", []) or [])
    item_ids = [int(item.id) for item in items if getattr(item, "id", None)]
    events = []
    if item_ids:
        events = (
            QuoteItemPickupEvent.query.filter_by(
                quote_id=int(quote.id), company_id=int(quote.company_id)
            )
            .filter(QuoteItemPickupEvent.quote_item_id.in_(item_ids))
            .order_by(QuoteItemPickupEvent.picked_up_at.desc(), QuoteItemPickupEvent.id.desc())
            .all()
        )
    totals = {}
    latest = {}
    for event in events:
        item_id = int(event.quote_item_id)
        totals[item_id] = totals.get(item_id, Decimal("0")) + _decimal(event.quantity)
        latest.setdefault(item_id, event)
    rows = []
    for item in items:
        quoted = max(_decimal(item.quantity), Decimal("0"))
        picked = min(totals.get(int(item.id), Decimal("0")), quoted) if item.id else Decimal("0")
        remaining = max(quoted - picked, Decimal("0"))
        event = latest.get(int(item.id)) if item.id else None
        rows.append({
            "item_id": int(item.id),
            "name": item.description,
            "quantity": float(quoted),
            "picked_quantity": float(picked),
            "remaining_quantity": float(remaining),
            "status": "picked_up" if remaining <= 0 and quoted > 0 else ("partial" if picked > 0 else "pending"),
            "last_picked_at": event.picked_up_at_local if event else "",
            "last_picked_by": (event.picked_up_by.name or event.picked_up_by.username) if event and event.picked_up_by else "",
        })
    total = sum((Decimal(str(row["quantity"])) for row in rows), Decimal("0"))
    picked_total = sum((Decimal(str(row["picked_quantity"])) for row in rows), Decimal("0"))
    return {
        "rows": rows,
        "total_quantity": float(total),
        "picked_quantity": float(picked_total),
        "remaining_quantity": float(max(total - picked_total, Decimal("0"))),
        "status": "picked_up" if total > 0 and picked_total >= total else ("partial" if picked_total > 0 else "pending"),
    }


def _locked_quote_and_delivery(company_id: int, quote_id: int):
    from app import Quote, QuoteDelivery

    quote = Quote.query.filter_by(id=int(quote_id), company_id=int(company_id)).with_for_update().first()
    if quote is None:
        raise LookupError("No encontramos este presupuesto en tu comercio.")
    delivery = QuoteDelivery.query.filter_by(
        quote_id=int(quote.id), company_id=int(company_id)
    ).with_for_update().first()
    if delivery is None:
        raise ValueError("Este presupuesto no tiene configurado el retiro en el comercio.")
    if str(delivery.method or "retiro").strip().lower() != "retiro":
        raise ValueError("Este presupuesto está configurado para envío; el seguimiento de retiro no aplica.")
    if not getattr(quote, "converted_sale_id", None) and str(quote.status or "").upper() != "CONVERTIDO":
        raise ValueError("Primero confirmá la venta por el flujo habitual antes de registrar el retiro.")
    return quote, delivery


def _append_event(*, company_id, quote, delivery, item, quantity, actor_user_id, note=""):
    from app import QuoteItemPickupEvent

    picked_before = db.session.query(func.coalesce(func.sum(QuoteItemPickupEvent.quantity), 0)).filter_by(
        company_id=int(company_id), quote_id=int(quote.id), quote_item_id=int(item.id)
    ).scalar()
    quoted = max(_decimal(item.quantity), Decimal("0"))
    remaining = max(quoted - _decimal(picked_before), Decimal("0"))
    amount = _decimal(quantity)
    if amount <= 0:
        raise ValueError("La cantidad retirada debe ser mayor que cero.")
    if amount > remaining:
        raise ValueError(f"Solo quedan {remaining:g} unidades pendientes de {item.description}.")
    event = QuoteItemPickupEvent(
        company_id=int(company_id), quote_id=int(quote.id), quote_item_id=int(item.id),
        quantity=amount, picked_up_by_user_id=int(actor_user_id), note=str(note or "").strip()[:500] or None,
    )
    db.session.add(event)
    db.session.flush()
    return event


def _refresh_delivery_status(company_id, quote, delivery, actor_user_id):
    from app import QuoteItemPickupEvent

    items = list(getattr(quote, "items", []) or [])
    if not items:
        delivery.pickup_status = "pending"
        delivery.picked_up_at = None
        delivery.picked_up_by_user_id = None
        return "pending"
    picked = {}
    rows = (
        db.session.query(QuoteItemPickupEvent.quote_item_id, func.sum(QuoteItemPickupEvent.quantity))
        .filter_by(company_id=int(company_id), quote_id=int(quote.id))
        .group_by(QuoteItemPickupEvent.quote_item_id).all()
    )
    picked = {int(item_id): _decimal(qty) for item_id, qty in rows}
    any_picked = False
    all_picked = True
    for item in items:
        quantity = max(_decimal(item.quantity), Decimal("0"))
        done = min(picked.get(int(item.id), Decimal("0")), quantity)
        any_picked = any_picked or done > 0
        if quantity > 0 and done < quantity:
            all_picked = False
    if all_picked and any_picked:
        from stockarmobile.helpers.dates import utcnow_naive
        delivery.pickup_status = "picked_up"
        delivery.picked_up_at = utcnow_naive()
        delivery.picked_up_by_user_id = int(actor_user_id)
        return "picked_up"
    delivery.pickup_status = "partial" if any_picked else "pending"
    delivery.picked_up_at = None
    delivery.picked_up_by_user_id = None
    return delivery.pickup_status


def register_item_pickup(*, company_id, quote_id, quote_item_id, quantity, actor_user_id, note=""):
    from app import QuoteItem

    quote, delivery = _locked_quote_and_delivery(company_id, quote_id)
    item = QuoteItem.query.filter_by(id=int(quote_item_id), quote_id=int(quote.id)).with_for_update().first()
    if item is None:
        raise LookupError("El producto no pertenece a este presupuesto.")
    event = _append_event(
        company_id=company_id, quote=quote, delivery=delivery, item=item,
        quantity=quantity, actor_user_id=actor_user_id, note=note,
    )
    status = _refresh_delivery_status(company_id, quote, delivery, actor_user_id)
    db.session.commit()
    return event, status


def register_all_remaining_pickup(*, company_id, quote_id, actor_user_id, note=""):
    """Bulk quick action used by the AI order screen; commits all line events atomically."""
    from app import QuoteItem, QuoteItemPickupEvent

    quote, delivery = _locked_quote_and_delivery(company_id, quote_id)
    items = QuoteItem.query.filter_by(quote_id=int(quote.id)).with_for_update().all()
    changed = 0
    for item in items:
        picked = db.session.query(func.coalesce(func.sum(QuoteItemPickupEvent.quantity), 0)).filter_by(
            company_id=int(company_id), quote_id=int(quote.id), quote_item_id=int(item.id)
        ).scalar()
        remaining = max(_decimal(item.quantity) - _decimal(picked), Decimal("0"))
        if remaining > 0:
            _append_event(
                company_id=company_id, quote=quote, delivery=delivery, item=item,
                quantity=remaining, actor_user_id=actor_user_id, note=note,
            )
            changed += 1
    status = _refresh_delivery_status(company_id, quote, delivery, actor_user_id)
    db.session.commit()
    return changed, status

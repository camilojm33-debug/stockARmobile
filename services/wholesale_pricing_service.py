"""Central price resolution for registered wholesale customers.

The existing Product.price remains the retail price. Wholesale prices are selected
only for an active client in the same company with an active, current price list.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


CENT = Decimal("0.01")


def _decimal(value, default="0"):
    try:
        result = Decimal(str(value if value is not None else default))
        return result if result.is_finite() else Decimal(default)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _active_today(price_list, today=None):
    today = today or date.today()
    if not price_list or not bool(price_list.active):
        return False
    starts = getattr(price_list, "effective_from", None)
    ends = getattr(price_list, "effective_until", None)
    if starts is not None and starts > today:
        return False
    if ends is not None and ends < today:
        return False
    return True


def resolve_product_price(*, company_id: int, product, quantity=1, client=None) -> dict:
    """Resolve price for one line without modifying Product or customer records."""
    retail_price = _decimal(getattr(product, "price", 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    fallback = {
        "price": retail_price,
        "source": "minorista",
        "price_list_id": None,
        "price_list_name": None,
        "min_quantity": None,
        "reason": None,
    }
    if client is None:
        return fallback
    try:
        effective_company_id = int(company_id)
        client_company_id = int(getattr(client, "company_id", 0) or 0)
        product_company_id = int(getattr(product, "company_id", 0) or 0)
    except (TypeError, ValueError):
        return fallback
    if effective_company_id <= 0 or client_company_id != effective_company_id or product_company_id != effective_company_id:
        return fallback
    if str(getattr(client, "customer_type", "minorista") or "minorista").strip().lower() != "mayorista":
        return fallback
    try:
        price_list_id = int(getattr(client, "price_list_id", 0) or 0)
    except (TypeError, ValueError):
        price_list_id = 0
    if price_list_id <= 0:
        return {**fallback, "reason": "mayorista_sin_lista"}

    # Deferred imports avoid coupling model import order to the resolver.
    from wholesale_pricing import PriceList, PriceListItem

    price_list = PriceList.query.filter_by(id=price_list_id, company_id=effective_company_id).first()
    if not _active_today(price_list):
        return {**fallback, "reason": "lista_no_vigente"}

    qty = _decimal(quantity, "0")
    if qty <= 0:
        return {**fallback, "reason": "cantidad_invalida"}

    row = (
        PriceListItem.query.filter(
            PriceListItem.company_id == effective_company_id,
            PriceListItem.price_list_id == price_list.id,
            PriceListItem.product_id == int(product.id),
            PriceListItem.active.is_(True),
            PriceListItem.min_quantity <= qty,
        )
        .order_by(PriceListItem.min_quantity.desc(), PriceListItem.id.desc())
        .first()
    )
    if row is None or _decimal(row.unit_price) <= 0:
        return {**fallback, "reason": "producto_sin_tarifa_mayorista"}
    return {
        "price": _decimal(row.unit_price).quantize(CENT, rounding=ROUND_HALF_UP),
        "source": "mayorista",
        "price_list_id": int(price_list.id),
        "price_list_name": str(price_list.name),
        "min_quantity": _decimal(row.min_quantity),
        "reason": None,
    }


def apply_wholesale_prices(lines: list[dict], *, company_id: int, client) -> list[dict]:
    """Annotate calculated lines with resolved prices; wholesale list prices are final.

    Product-level legacy discounts and promotions are intentionally not stacked on
    top of a wholesale list price in this first version.
    """
    for line in lines:
        product = line.get("product")
        if product is None:
            line.setdefault("price_source", "minorista")
            line.setdefault("price_list_id", None)
            continue
        resolution = resolve_product_price(
            company_id=company_id,
            product=product,
            quantity=line.get("quantity", 1),
            client=client,
        )
        line["price_source"] = resolution["source"]
        line["price_list_id"] = resolution["price_list_id"]
        line["price_list_name"] = resolution["price_list_name"]
        line["price_resolution_reason"] = resolution["reason"]
        if resolution["source"] == "mayorista":
            line["price"] = resolution["price"]
            line["discount"] = Decimal("0.00")
    return lines


def product_price_catalog(*, company_id: int, product_ids: list[int]) -> dict:
    """Return active list tiers for quote UI; never returns data from another tenant."""
    ids = sorted({int(value) for value in (product_ids or []) if int(value) > 0})
    if not ids or not company_id:
        return {}
    from wholesale_pricing import PriceList, PriceListItem

    today = date.today()
    lists = PriceList.query.filter(
        PriceList.company_id == int(company_id),
        PriceList.active.is_(True),
    ).all()
    eligible = {
        int(row.id): row
        for row in lists
        if _active_today(row, today=today)
    }
    if not eligible:
        return {}
    rows = (
        PriceListItem.query.filter(
            PriceListItem.company_id == int(company_id),
            PriceListItem.price_list_id.in_(sorted(eligible)),
            PriceListItem.product_id.in_(ids),
            PriceListItem.active.is_(True),
        )
        .order_by(PriceListItem.product_id.asc(), PriceListItem.price_list_id.asc(), PriceListItem.min_quantity.asc())
        .all()
    )
    result = {}
    for row in rows:
        price_list = eligible.get(int(row.price_list_id))
        if price_list is None or _decimal(row.unit_price) <= 0:
            continue
        product_rows = result.setdefault(int(row.product_id), {})
        tiers = product_rows.setdefault(str(row.price_list_id), [])
        tiers.append({
            "min_quantity": float(_decimal(row.min_quantity)),
            "price": float(_decimal(row.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)),
        })
    return result

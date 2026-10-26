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


def _has_wholesale_pricing_access(company_id: int) -> bool:
    """Fail closed when a company plan does not include wholesale pricing."""
    try:
        normalized_company_id = int(company_id)
    except (TypeError, ValueError):
        return False
    if normalized_company_id <= 0:
        return False

    from app import Company
    from services.ai_agent.usage_service import can_use_commercial_feature

    company = Company.query.filter_by(id=normalized_company_id, active=True).first()
    return bool(company is not None and can_use_commercial_feature(company, "wholesale_pricing").allowed)


def resolve_product_price(
    *,
    company_id: int,
    product,
    quantity=1,
    client=None,
    _plan_access_allowed: bool | None = None,
) -> dict:
    """Resolve price for one line without modifying Product or customer records."""
    retail_price = _decimal(getattr(product, "price", 0)).quantize(CENT, rounding=ROUND_HALF_UP)
    fallback = {
        "price": retail_price,
        "source": "minorista",
        "price_list_id": None,
        "price_list_name": None,
        "min_quantity": None,
        "discount_percent": None,
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

    plan_access_allowed = _plan_access_allowed
    if plan_access_allowed is None:
        plan_access_allowed = _has_wholesale_pricing_access(effective_company_id)
    if not plan_access_allowed:
        return {**fallback, "reason": "plan_sin_mayoristas"}

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
    if row is None:
        return {**fallback, "reason": "producto_sin_tarifa_mayorista"}

    price = _decimal(row.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
    discount_percent = None
    if getattr(row, "discount_percent", None) is not None:
        candidate_percent = _decimal(row.discount_percent, "0")
        if Decimal("0") <= candidate_percent < Decimal("100") and retail_price > 0:
            discounted = (
                retail_price * (Decimal("100") - candidate_percent) / Decimal("100")
            ).quantize(CENT, rounding=ROUND_HALF_UP)
            if discounted > 0:
                price = discounted
                discount_percent = candidate_percent
    if price <= 0:
        return {**fallback, "reason": "producto_sin_tarifa_mayorista"}
    return {
        "price": price,
        "source": "mayorista",
        "price_list_id": int(price_list.id),
        "price_list_name": str(price_list.name),
        "min_quantity": _decimal(row.min_quantity),
        "discount_percent": discount_percent,
        "reason": None,
    }


def apply_wholesale_prices(lines: list[dict], *, company_id: int, client) -> list[dict]:
    """Annotate calculated lines with resolved prices; wholesale list prices are final.

    Product-level legacy discounts and promotions are intentionally not stacked on
    top of a wholesale list price in this first version.
    """
    wholesale_client = (
        str(getattr(client, "customer_type", "minorista") or "minorista").strip().lower() == "mayorista"
    )
    plan_access_allowed = _has_wholesale_pricing_access(company_id) if wholesale_client else None
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
            _plan_access_allowed=plan_access_allowed,
        )
        line["price_source"] = resolution["source"]
        line["price_list_id"] = resolution["price_list_id"]
        line["price_list_name"] = resolution["price_list_name"]
        line["price_resolution_reason"] = resolution["reason"]
        if resolution["source"] == "mayorista":
            line["price"] = resolution["price"]
            line["discount"] = Decimal("0.00")
        elif resolution["reason"] == "plan_sin_mayoristas":
            # A previously assigned list must not keep applying after a downgrade.
            line["price"] = resolution["price"]
    return lines


def apply_price_snapshot(lines: list[dict], snapshots: list[dict]) -> list[dict]:
    """Apply a server-persisted POS QR line snapshot without re-resolving today's prices.

    This is intentionally for internal payment finalization only; the public request
    payload must never be passed directly as a trusted snapshot.
    """
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("El snapshot de precios del QR está vacío o no es válido.")

    parsed = {}
    for raw in snapshots:
        if not isinstance(raw, dict):
            raise ValueError("El snapshot de precios del QR contiene una línea inválida.")
        try:
            product_id = int(raw.get("productId") or raw.get("product_id") or 0)
            quantity = Decimal(str(raw.get("quantity")))
            price = Decimal(str(raw.get("price")))
            discount = Decimal(str(raw.get("discount") or 0))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("El snapshot de precios del QR contiene importes inválidos.")
        if product_id <= 0 or product_id in parsed:
            raise ValueError("El snapshot de precios del QR contiene productos duplicados o inválidos.")
        if (
            not quantity.is_finite()
            or quantity <= 0
            or not price.is_finite()
            or price < 0
            or not discount.is_finite()
            or discount < 0
            or discount > price * quantity + CENT
        ):
            raise ValueError("El snapshot de precios del QR contiene cantidades o importes fuera de rango.")
        source = str(raw.get("price_source") or "minorista").strip().lower()
        if source not in {"minorista", "mayorista", "manual"}:
            raise ValueError("El origen del precio guardado en el QR no es válido.")
        raw_list_id = raw.get("price_list_id")
        try:
            price_list_id = int(raw_list_id) if raw_list_id not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError("La lista de precios guardada en el QR no es válida.")
        if source != "mayorista":
            price_list_id = None
        parsed[product_id] = {
            "quantity": quantity,
            "price": price.quantize(CENT, rounding=ROUND_HALF_UP),
            "discount": discount.quantize(CENT, rounding=ROUND_HALF_UP),
            "price_source": source,
            "price_list_id": price_list_id,
        }

    line_ids = {int(line["product"].id) for line in lines}
    if line_ids != set(parsed):
        raise ValueError("Los productos del carrito ya no coinciden con el snapshot de precios del QR.")

    for line in lines:
        product_id = int(line["product"].id)
        saved = parsed[product_id]
        quantity = Decimal(str(line.get("quantity") or 0))
        if quantity != saved["quantity"]:
            raise ValueError("La cantidad del carrito ya no coincide con el snapshot de precios del QR.")
        line["price"] = saved["price"]
        line["discount"] = saved["discount"]
        line["price_source"] = saved["price_source"]
        line["price_list_id"] = saved["price_list_id"]
    return lines


def product_price_catalog(*, company_id: int, product_ids: list[int]) -> dict:
    """Return active list tiers for quote UI; never returns data from another tenant."""
    ids = sorted({int(value) for value in (product_ids or []) if int(value) > 0})
    if not ids or not company_id or not _has_wholesale_pricing_access(company_id):
        return {}
    from app import Product
    from wholesale_pricing import PriceList, PriceListItem

    products = {
        int(product.id): product
        for product in Product.query.filter(
            Product.company_id == int(company_id),
            Product.id.in_(ids),
        ).all()
    }
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
        product = products.get(int(row.product_id))
        price = _decimal(row.unit_price).quantize(CENT, rounding=ROUND_HALF_UP)
        discount_percent = None
        if product is not None and getattr(row, "discount_percent", None) is not None:
            candidate_percent = _decimal(row.discount_percent, "0")
            retail_price = _decimal(getattr(product, "price", 0)).quantize(CENT, rounding=ROUND_HALF_UP)
            if Decimal("0") <= candidate_percent < Decimal("100") and retail_price > 0:
                discounted = (
                    retail_price * (Decimal("100") - candidate_percent) / Decimal("100")
                ).quantize(CENT, rounding=ROUND_HALF_UP)
                if discounted > 0:
                    price = discounted
                    discount_percent = float(candidate_percent)
        tier = {
            "min_quantity": float(_decimal(row.min_quantity)),
            "price": float(price),
        }
        if discount_percent is not None:
            tier["discount_percent"] = discount_percent
        tiers.append(tier)
    return result

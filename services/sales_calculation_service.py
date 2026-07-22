"""Single source of truth for sale totals and payment breakdowns."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MONEY_STEP = Decimal("0.01")
CONFIRMED_SALE_STATUSES = {"confirmada", "confirmed", "aprobada", "approved", "completada", "complete"}


def to_decimal(value, default="0.00"):
    try:
        if value in (None, ""):
            return Decimal(default)
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def quantize_money(value):
    return to_decimal(value).quantize(MONEY_STEP, rounding=ROUND_HALF_UP)


def clamp_non_negative_money(value):
    amount = quantize_money(value)
    return amount if amount > Decimal("0.00") else Decimal("0.00")


def calculate_product_pricing(*, cost_price, price=None, margin=None, profit_percent=None, pricing_source=None):
    cost = clamp_non_negative_money(cost_price)
    source = (pricing_source or "").strip().lower()

    price_value = to_decimal(price)
    margin_value = to_decimal(margin)
    profit_value = to_decimal(profit_percent)

    if source == "profit_percent":
        if profit_value < 0:
            raise ValueError("El margen % no puede ser negativo.")
        final_price = quantize_money(cost * (Decimal("1.00") + (profit_value / Decimal("100.00"))))
        gain_amount = quantize_money(final_price - cost)
        margin_percent = profit_value
    elif source == "margin":
        if margin_value < 0:
            raise ValueError("La ganancia no puede ser negativa.")
        gain_amount = quantize_money(margin_value)
        final_price = quantize_money(cost + gain_amount)
        margin_percent = quantize_money((gain_amount / cost * Decimal("100.00")) if cost > 0 else Decimal("0.00"))
    elif source == "price":
        if price_value < 0:
            raise ValueError("El precio de venta no puede ser negativo.")
        final_price = quantize_money(price_value)
        if final_price < cost:
            raise ValueError("El precio de venta no puede ser menor al costo.")
        gain_amount = quantize_money(final_price - cost)
        margin_percent = quantize_money((gain_amount / cost * Decimal("100.00")) if cost > 0 else Decimal("0.00"))
    elif profit_value not in (None, Decimal("0"), Decimal("0.0"), Decimal("0.00")):
        if profit_value < 0:
            raise ValueError("El margen % no puede ser negativo.")
        final_price = quantize_money(cost * (Decimal("1.00") + (profit_value / Decimal("100.00"))))
        gain_amount = quantize_money(final_price - cost)
        margin_percent = profit_value
    elif margin_value not in (None, Decimal("0"), Decimal("0.0"), Decimal("0.00")):
        if margin_value < 0:
            raise ValueError("La ganancia no puede ser negativa.")
        gain_amount = quantize_money(margin_value)
        final_price = quantize_money(cost + gain_amount)
        margin_percent = quantize_money((gain_amount / cost * Decimal("100.00")) if cost > 0 else Decimal("0.00"))
    else:
        if price_value < 0:
            raise ValueError("El precio de venta no puede ser negativo.")
        final_price = quantize_money(price_value if price_value > 0 else cost)
        if final_price < cost:
            raise ValueError("El precio de venta no puede ser menor al costo.")
        gain_amount = quantize_money(final_price - cost)
        margin_percent = quantize_money((gain_amount / cost * Decimal("100.00")) if cost > 0 else Decimal("0.00"))

    return {
        "cost_price": cost,
        "price": final_price,
        "margin": gain_amount,
        "profit_percent": margin_percent,
    }


def is_confirmed_sale_status(status_value):
    normalized = (status_value or "confirmada").strip().lower()
    if not normalized:
        normalized = "confirmada"
    return normalized in CONFIRMED_SALE_STATUSES


def normalize_payment_method(value):
    raw = (value or "").strip().lower()
    if not raw:
        return "otros"
    if "efect" in raw:
        return "efectivo"
    if "mercado" in raw or raw == "mp" or "qr" in raw:
        return "mercado_pago"
    if "tarj" in raw:
        return "debito"
    if "deb" in raw:
        return "debito"
    if "cred" in raw or "credito" in raw or "crédito" in raw:
        return "credito"
    if "transfer" in raw:
        return "transferencia"
    return "otros"


def normalize_payment_split(*, total_amount, primary_method, secondary_method=None, primary_amount=None, secondary_amount=None):
    total = clamp_non_negative_money(total_amount)
    has_secondary = bool((secondary_method or "").strip())

    normalized_primary = primary_method or "EFECTIVO"
    normalized_secondary = secondary_method or ""

    primary = clamp_non_negative_money(primary_amount)
    secondary = clamp_non_negative_money(secondary_amount)

    if has_secondary:
        if secondary <= 0:
            secondary = Decimal("0.00")
        if primary <= 0:
            primary = max(total - secondary, Decimal("0.00"))
        combined = primary + secondary
        if combined <= Decimal("0.00"):
            primary = total
            secondary = Decimal("0.00")
        elif combined < total:
            primary += total - combined
        elif combined > total:
            primary = quantize_money(primary * total / combined)
            secondary = total - primary
    else:
        primary = total if primary <= 0 else min(primary, total)
        secondary = Decimal("0.00")

    primary = quantize_money(primary)
    secondary = quantize_money(secondary)
    # Keep exact total after rounding.
    rounded_total = quantize_money(total)
    if has_secondary:
        secondary = rounded_total - primary
    else:
        primary = rounded_total

    return {
        "primary_method": normalized_primary,
        "secondary_method": normalized_secondary,
        "primary_amount": primary,
        "secondary_amount": secondary,
        "total": rounded_total,
    }


def sale_payment_breakdown_from_values(*, total_amount, primary_method, secondary_method=None, primary_amount=None, secondary_amount=None):
    split = normalize_payment_split(
        total_amount=total_amount,
        primary_method=primary_method,
        secondary_method=secondary_method,
        primary_amount=primary_amount,
        secondary_amount=secondary_amount,
    )

    primary_key = normalize_payment_method(split["primary_method"])
    secondary_key = normalize_payment_method(split["secondary_method"])
    has_secondary = bool((split["secondary_method"] or "").strip())

    result = {
        "efectivo": Decimal("0.00"),
        "mercado_pago": Decimal("0.00"),
        "debito": Decimal("0.00"),
        "credito": Decimal("0.00"),
        "transferencia": Decimal("0.00"),
        "otros": Decimal("0.00"),
    }
    result[primary_key] = result.get(primary_key, Decimal("0.00")) + split["primary_amount"]
    if has_secondary:
        result[secondary_key] = result.get(secondary_key, Decimal("0.00")) + split["secondary_amount"]
    return result


def sale_payment_breakdown(sale):
    return sale_payment_breakdown_from_values(
        total_amount=getattr(sale, "total_amount", 0),
        primary_method=getattr(sale, "payment_method", None),
        secondary_method=getattr(sale, "secondary_payment_method", None),
        primary_amount=getattr(sale, "paid_amount", None),
        secondary_amount=getattr(sale, "secondary_paid_amount", None),
    )


def calculate_sale_totals(line_items, *, general_discount=0, surcharge=0):
    normalized_lines = []
    subtotal = Decimal("0.00")
    line_discount_total = Decimal("0.00")

    for line in line_items:
        quantity = to_decimal(line.get("quantity"))
        unit_price = quantize_money(line.get("price"))
        gross = quantize_money(unit_price * quantity)
        base_discount = clamp_non_negative_money(line.get("line_discount"))
        if base_discount > gross:
            base_discount = gross
        net = gross - base_discount
        subtotal += gross
        line_discount_total += base_discount
        normalized_lines.append(
            {
                "gross": gross,
                "base_discount": base_discount,
                "net": net,
                "quantity": quantity,
                "price": unit_price,
            }
        )

    subtotal = quantize_money(subtotal)
    line_discount_total = quantize_money(line_discount_total)
    safe_general_discount = clamp_non_negative_money(general_discount)
    safe_surcharge = clamp_non_negative_money(surcharge)

    taxable = subtotal - line_discount_total - safe_general_discount
    if taxable < Decimal("0.00"):
        taxable = Decimal("0.00")
    taxable = quantize_money(taxable)
    final_total = quantize_money(taxable + safe_surcharge)

    order_discount_adjustment = quantize_money(safe_general_discount - safe_surcharge)

    sum_net = sum((line["net"] for line in normalized_lines), Decimal("0.00"))
    if sum_net <= Decimal("0.00"):
        sum_net = sum((line["gross"] for line in normalized_lines), Decimal("0.00"))

    allocated_sum = Decimal("0.00")
    for index, line in enumerate(normalized_lines):
        if index == len(normalized_lines) - 1:
            allocation = order_discount_adjustment - allocated_sum
        else:
            weight_base = line["net"] if sum_net > Decimal("0.00") else (Decimal("1.00") if normalized_lines else Decimal("0.00"))
            allocation = quantize_money(order_discount_adjustment * weight_base / (sum_net or Decimal("1.00")))
            allocated_sum += allocation

        final_discount = quantize_money(line["base_discount"] + allocation)
        line_total = quantize_money(line["gross"] - final_discount)
        line["order_allocation"] = allocation
        line["final_discount"] = final_discount
        line["line_total"] = line_total

    rounded_sum = quantize_money(sum((line["line_total"] for line in normalized_lines), Decimal("0.00")))
    diff = quantize_money(final_total - rounded_sum)
    if normalized_lines and diff != Decimal("0.00"):
        normalized_lines[-1]["line_total"] = quantize_money(normalized_lines[-1]["line_total"] + diff)
        normalized_lines[-1]["final_discount"] = quantize_money(normalized_lines[-1]["gross"] - normalized_lines[-1]["line_total"])

    return {
        "subtotal": subtotal,
        "line_discount_total": line_discount_total,
        "general_discount": safe_general_discount,
        "surcharge": safe_surcharge,
        "tax": Decimal("0.00"),
        "total": final_total,
        "lines": normalized_lines,
    }

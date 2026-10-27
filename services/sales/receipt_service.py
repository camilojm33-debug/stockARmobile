"""Ticket and receipt rendering helpers for sales."""

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from stockarmobile.helpers.dates import format_local_datetime


class ReceiptService:
    @staticmethod
    def _sale_datetime(sale):
        company = getattr(sale, "company", None)
        tz_name = getattr(company, "timezone", None) or "America/Argentina/Buenos_Aires"
        return format_local_datetime(sale.date, tz_name, "%Y-%m-%d %H:%M") if sale.date else ""

    @staticmethod
    def _line_values(item):
        quantity = Decimal(str(getattr(item, "quantity", 0) or 0))
        price = Decimal(str(getattr(item, "price", 0) or 0))
        discount = Decimal(str(getattr(item, "discount", 0) or 0))
        gross = max(price * quantity, Decimal("0.00")).quantize(Decimal("0.01"))
        discount = min(max(discount, Decimal("0.00")), gross).quantize(Decimal("0.01"))
        total = (gross - discount).quantize(Decimal("0.01"))
        return quantity, price, discount, gross, total

    @staticmethod
    def _line_discount_total(sale):
        total = Decimal("0.00")
        for item in getattr(sale, "items", []) or []:
            total += ReceiptService._line_values(item)[2]
        return total.quantize(Decimal("0.01"))

    @staticmethod
    def _effective_discount(sale):
        stored = Decimal(str(getattr(sale, "discount", 0) or 0))
        line_total = ReceiptService._line_discount_total(sale)
        # Las promociones son descuentos de línea. Esto mantiene el ticket
        # correcto incluso para ventas antiguas que no persistieron el total.
        return max(stored, line_total).quantize(Decimal("0.01"))

    @staticmethod
    def wholesale_discount_info(item):
        """Return stored savings; infer legacy rows only when the current rule matches the charged price."""
        try:
            quantity = Decimal(str(getattr(item, "quantity", 0) or 0))
            unit_price = Decimal(str(getattr(item, "price", 0) or 0)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            percent_raw = getattr(item, "wholesale_discount_percent", None)
            reference_raw = getattr(item, "wholesale_reference_price", None)
            percent = Decimal(str(percent_raw)) if percent_raw not in (None, "") else None
            reference_price = Decimal(str(reference_raw)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ) if reference_raw not in (None, "") else None
        except (InvalidOperation, TypeError, ValueError):
            return None

        if percent is None or reference_price is None:
            if (
                str(getattr(item, "price_source", "") or "").strip().lower() != "mayorista"
                or not getattr(item, "price_list_id", None)
                or not getattr(item, "product_id", None)
                or quantity <= 0
            ):
                return None
            product = getattr(item, "product", None)
            if product is None:
                return None
            try:
                from wholesale_pricing import PriceListItem

                query = PriceListItem.query.filter(
                    PriceListItem.price_list_id == int(item.price_list_id),
                    PriceListItem.product_id == int(item.product_id),
                    PriceListItem.active.is_(True),
                    PriceListItem.min_quantity <= quantity,
                )
                sale = getattr(item, "sale", None)
                if getattr(sale, "company_id", None) is not None:
                    query = query.filter(PriceListItem.company_id == int(sale.company_id))
                rule = query.order_by(
                    PriceListItem.min_quantity.desc(), PriceListItem.id.desc()
                ).first()
                if rule is None or getattr(rule, "discount_percent", None) is None:
                    return None
                percent = Decimal(str(rule.discount_percent))
                reference_price = Decimal(str(getattr(product, "price", 0) or 0)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                expected_price = (
                    reference_price * (Decimal("100") - percent) / Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                # Safe fallback for tickets created before snapshots existed: never
                # infer a discount unless it exactly reproduces the saved charged price.
                if expected_price != unit_price:
                    return None
            except Exception:
                return None

        if (
            percent is None or not percent.is_finite()
            or percent <= 0 or percent >= Decimal("100")
            or reference_price is None or reference_price <= unit_price
            or quantity <= 0
        ):
            return None
        unit_saving = (reference_price - unit_price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        amount = (unit_saving * quantity).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if amount <= 0:
            return None
        return {
            "percent": percent,
            "reference_price": reference_price,
            "unit_saving": unit_saving,
            "amount": amount,
        }

    @staticmethod
    def wholesale_discount_total(sale):
        total = sum(
            (
                (ReceiptService.wholesale_discount_info(item) or {}).get("amount", Decimal("0.00"))
                for item in (getattr(sale, "items", []) or [])
            ),
            Decimal("0.00"),
        )
        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _ticket_lines(sale):
        lines = []
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            unit_measure = item.product.unit_measure if item.product else "u"
            quantity, price, discount, gross, total = ReceiptService._line_values(item)
            lines.append(f"{name}: $" + f"{price:.2f} x {quantity:g} {unit_measure} = $" + f"{gross:.2f}")
            wholesale_info = ReceiptService.wholesale_discount_info(item)
            if wholesale_info:
                lines.append(
                    "  Descuento mayorista (" + f"{wholesale_info['percent']:g}%): -$"
                    + f"{wholesale_info['amount']:.2f} (ya aplicado en el precio)"
                )
            if discount > 0:
                lines.append("  Descuento promoción: -$" + f"{discount:.2f}")
            if discount > 0 or wholesale_info:
                lines.append("  Total línea: $" + f"{total:.2f}")
        return lines

    @staticmethod
    def charge_rows(sale):
        """Return the persisted commercial charge snapshot for ticket rendering."""
        raw = getattr(sale, "charges_json", None)
        if not raw:
            return []
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []

        rows = []
        for charge in payload:
            if not isinstance(charge, dict):
                continue
            try:
                amount = Decimal(str(charge.get("amount") or 0)).quantize(Decimal("0.01"))
            except Exception:
                continue
            if amount <= Decimal("0.00"):
                continue
            charge_type = str(charge.get("type") or "fixed").strip().lower()
            try:
                value = Decimal(str(charge.get("value"))) if charge.get("value") is not None else None
            except Exception:
                value = None
            rows.append({
                "name": str(charge.get("name") or "Cargo").strip()[:120] or "Cargo",
                "type": charge_type,
                "value": value.quantize(Decimal("0.01")) if value is not None else None,
                "amount": amount,
                "base": str(charge.get("base") or "").strip(),
            })
        return rows

    @staticmethod
    def _snapshot_charge_amount(sale):
        return sum((row["amount"] for row in ReceiptService.charge_rows(sale)), Decimal("0.00")).quantize(Decimal("0.01"))

    @staticmethod
    def _adjustment_lines(label, adjustment_type, value, amount, reason, sign=""):
        if adjustment_type == "percentage" and value is not None:
            lines = [f"{label}: {value:.2f}%", f"{label} aplicado: {sign}${amount:.2f}"]
        elif adjustment_type == "fixed" and value is not None:
            lines = [f"{label}: {sign}${value:.2f}"]
        else:
            lines = [f"{label}: {sign}${amount:.2f}"]
        if reason:
            lines.append(f"Motivo {label.lower()}: {reason}")
        return lines

    @staticmethod
    def ticket_brand_name(*, company):
        fallback = "STOCK ARMOBILE"
        if company is None:
            return fallback
        settings = {}
        raw = getattr(company, "printer_settings_json", None)
        if raw:
            try:
                settings = json.loads(raw)
            except Exception:
                settings = {}
        name = (settings.get("ticket_name") or settings.get("printer_name") or getattr(company, "name", "") or fallback).strip()
        return name[:120] or fallback

    @staticmethod
    def ticket_note(sale, ticket_brand=None):
        """Translate an internal AI workflow marker into a merchant-facing note."""
        note = str(getattr(sale, "note", None) or "").strip()
        if note != "Pedido generado por el Vendedor 24 hs de StockARmobile.":
            return note
        company = getattr(sale, "company", None)
        business_name = str(
            getattr(company, "name", None)
            or ticket_brand
            or "el comercio"
        ).strip()
        return f"Pedido generado por el Vendedor IA de {business_name}."

    @staticmethod
    def ticket_text(sale, ticket_brand):
        brand = (ticket_brand or "STOCK ARMOBILE").strip()
        sale_datetime = ReceiptService._sale_datetime(sale)
        lines = [f"{brand} - TICKET DE VENTA", "-" * 32, f"Venta: #{sale.id}", f"Fecha: {sale_datetime}"]
        if sale.customer:
            lines.append(f"Cliente: {sale.customer}")
        lines.append("-" * 32)
        lines.extend(ReceiptService._ticket_lines(sale))
        ticket_note = ReceiptService.ticket_note(sale, ticket_brand=brand)
        if ticket_note:
            lines.extend(["-" * 32, f"Obs.: {ticket_note}"])
        effective_discount = ReceiptService._effective_discount(sale)
        wholesale_savings = ReceiptService.wholesale_discount_total(sale)
        lines.extend(["-" * 32, f"Subtotal: ${sale.subtotal:.2f}"])
        if wholesale_savings > 0:
            lines.append(f"Ahorro mayorista ya incluido en precios unitarios: ${wholesale_savings:.2f}")
        discount_label = "Descuento adicional" if wholesale_savings > 0 else "Descuento"
        lines.extend(ReceiptService._adjustment_lines(discount_label, sale.discount_type, sale.discount_value, effective_discount, sale.discount_reason, "-"))
        charge_rows = ReceiptService.charge_rows(sale)
        if charge_rows:
            for row in charge_rows:
                if row["type"] == "percentage" and row["value"] is not None:
                    lines.append(f'{row["name"]}: {row["value"]:.2f}%')
                    lines.append(f'{row["name"]} aplicado: ${row["amount"]:.2f}')
                else:
                    lines.append(f'{row["name"]}: +${row["amount"]:.2f}')
            expected_order_charges = (Decimal(str(getattr(sale, "surcharge", 0) or 0)) + Decimal(str(getattr(sale, "tax", 0) or 0))).quantize(Decimal("0.01"))
            covered = ReceiptService._snapshot_charge_amount(sale)
            if covered < expected_order_charges:
                remainder = expected_order_charges - covered
                surcharge = Decimal(str(getattr(sale, "surcharge", 0) or 0)).quantize(Decimal("0.01"))
                extra_surcharge = min(remainder, surcharge)
                if extra_surcharge > 0:
                    lines.append(f"Recargo adicional: +${extra_surcharge:.2f}")
                    remainder -= extra_surcharge
                if remainder > 0 and getattr(sale, "tax", 0):
                    lines.append(f"Impuestos adicionales: +${remainder:.2f}")
        else:
            if sale.surcharge:
                lines.extend(ReceiptService._adjustment_lines("Recargo", sale.surcharge_type, sale.surcharge_value, sale.surcharge, sale.surcharge_reason))
            if sale.tax:
                lines.append(f"Impuestos: ${sale.tax:.2f}")
        lines.extend(["=" * 32, f"TOTAL: ${sale.total_amount:.2f}", "Gracias por su compra!"])
        return "\n".join(lines)

    @staticmethod
    def ticket_rows(sale):
        rows = []
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            unit_measure = item.product.unit_measure if item.product else "u"
            quantity, price, discount, gross, total = ReceiptService._line_values(item)
            wholesale_info = ReceiptService.wholesale_discount_info(item)
            rows.append({
                "name": name,
                "unit_measure": unit_measure,
                "quantity": quantity,
                "price": price,
                "discount": discount,
                "gross": gross,
                "total": total,
                "wholesale_discount_percent": wholesale_info["percent"] if wholesale_info else None,
                "wholesale_reference_price": wholesale_info["reference_price"] if wholesale_info else None,
                "wholesale_unit_saving": wholesale_info["unit_saving"] if wholesale_info else Decimal("0.00"),
                "wholesale_discount_amount": wholesale_info["amount"] if wholesale_info else Decimal("0.00"),
            })
        return rows

    @staticmethod
    def whatsapp_text(sale, ticket_brand):
        sale_datetime = ReceiptService._sale_datetime(sale)
        lines = [f"{ticket_brand} - Ticket de compra", f"Venta #{sale.id}", f"Fecha: {sale_datetime}"]
        if sale.customer:
            lines.append(f"Cliente: {sale.customer}")
        lines.append("------------------------------")
        lines.extend(ReceiptService._ticket_lines(sale))
        effective_discount = ReceiptService._effective_discount(sale)
        wholesale_savings = ReceiptService.wholesale_discount_total(sale)
        lines.extend(["------------------------------", f"Subtotal: ${sale.subtotal:.2f}"])
        if wholesale_savings > 0:
            lines.append(f"Ahorro mayorista ya incluido en precios unitarios: ${wholesale_savings:.2f}")
        discount_label = "Descuento adicional" if wholesale_savings > 0 else "Descuento"
        lines.extend(ReceiptService._adjustment_lines(discount_label, sale.discount_type, sale.discount_value, effective_discount, sale.discount_reason, "-"))
        charge_rows = ReceiptService.charge_rows(sale)
        if charge_rows:
            for row in charge_rows:
                if row["type"] == "percentage" and row["value"] is not None:
                    lines.append(f'{row["name"]}: {row["value"]:.2f}%')
                    lines.append(f'{row["name"]} aplicado: ${row["amount"]:.2f}')
                else:
                    lines.append(f'{row["name"]}: +${row["amount"]:.2f}')
        else:
            if sale.surcharge:
                lines.extend(ReceiptService._adjustment_lines("Recargo", sale.surcharge_type, sale.surcharge_value, sale.surcharge, sale.surcharge_reason))
            if sale.tax:
                lines.append(f"Impuestos: ${sale.tax:.2f}")
        lines.extend([f"Total: ${sale.total_amount:.2f}", "Gracias por su compra!"])
        return "\n".join(lines)

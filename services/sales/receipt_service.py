"""Ticket and receipt rendering helpers for sales."""

import json
from decimal import Decimal

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
    def _ticket_lines(sale):
        lines = []
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            unit_measure = item.product.unit_measure if item.product else "u"
            quantity, price, discount, gross, total = ReceiptService._line_values(item)
            lines.append(f"{name}: $" + f"{price:.2f} x {quantity:g} {unit_measure} = $" + f"{gross:.2f}")
            if discount > 0:
                lines.append("  Descuento promoción: -$" + f"{discount:.2f}")
                lines.append("  Total línea: $" + f"{total:.2f}")
        return lines

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
    def ticket_text(sale, ticket_brand):
        brand = (ticket_brand or "STOCK ARMOBILE").strip()
        sale_datetime = ReceiptService._sale_datetime(sale)
        lines = [f"{brand} - TICKET DE VENTA", "-" * 32, f"Venta: #{sale.id}", f"Fecha: {sale_datetime}"]
        if sale.customer:
            lines.append(f"Cliente: {sale.customer}")
        lines.append("-" * 32)
        lines.extend(ReceiptService._ticket_lines(sale))
        if sale.note:
            lines.extend(["-" * 32, f"Obs.: {sale.note}"])
        effective_discount = ReceiptService._effective_discount(sale)
        lines.extend(["-" * 32, f"Subtotal: ${sale.subtotal:.2f}"])
        lines.extend(ReceiptService._adjustment_lines("Descuento", sale.discount_type, sale.discount_value, effective_discount, sale.discount_reason, "-"))
        if sale.surcharge:
            lines.extend(ReceiptService._adjustment_lines("Recargo", sale.surcharge_type, sale.surcharge_value, sale.surcharge, sale.surcharge_reason))
        lines.extend([f"Impuestos: ${sale.tax:.2f}", "=" * 32, f"TOTAL: ${sale.total_amount:.2f}", "Gracias por su compra!"])
        return "\n".join(lines)

    @staticmethod
    def ticket_rows(sale):
        rows = []
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            unit_measure = item.product.unit_measure if item.product else "u"
            quantity, price, discount, gross, total = ReceiptService._line_values(item)
            rows.append({
                "name": name,
                "unit_measure": unit_measure,
                "quantity": quantity,
                "price": price,
                "discount": discount,
                "gross": gross,
                "total": total,
            })
        return rows

    @staticmethod
    def whatsapp_text(sale, ticket_brand):
        sale_datetime = ReceiptService._sale_datetime(sale)
        lines = [f"{ticket_brand} - Ticket de compra", f"Venta #{sale.id}", f"Fecha: {sale_datetime}"]
        if sale.customer:
            lines.append(f"Cliente: {sale.customer}")
        lines.append("------------------------------")
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            unit_measure = item.product.unit_measure if item.product else "u"
            lines.append(f"{name}: ${item.price:.2f} x {item.quantity:g} {unit_measure} = ${item.total_amount:.2f}")
        lines.extend(["------------------------------", f"Subtotal: ${sale.subtotal:.2f}", *ReceiptService._adjustment_lines("Descuento", sale.discount_type, sale.discount_value, sale.discount, sale.discount_reason, "-"), *(ReceiptService._adjustment_lines("Recargo", sale.surcharge_type, sale.surcharge_value, sale.surcharge, sale.surcharge_reason) if sale.surcharge else []), f"Impuestos: ${sale.tax:.2f}", f"Total: ${sale.total_amount:.2f}", "Gracias por su compra!"])
        return "\n".join(lines)

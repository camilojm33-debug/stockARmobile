"""Ticket and receipt rendering helpers for sales."""

import json


class ReceiptService:
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
        lines = [f"{brand} - TICKET DE VENTA", "-" * 32, f"Venta: #{sale.id}", f"Fecha: {sale.date:%Y-%m-%d %H:%M}"]
        if sale.customer:
            lines.append(f"Cliente: {sale.customer}")
        lines.append("-" * 32)
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            unit_measure = item.product.unit_measure if item.product else "u"
            lines.append(f"{name}: ${item.price:.2f} x {item.quantity:g} {unit_measure} = ${item.total_amount:.2f}")
        if sale.note:
            lines.extend(["-" * 32, f"Obs.: {sale.note}"])
        lines.extend(["-" * 32, f"Subtotal: ${sale.subtotal:.2f}", f"Descuento: -${sale.discount:.2f}", f"Impuestos: ${sale.tax:.2f}", "=" * 32, f"TOTAL: ${sale.total_amount:.2f}", "Gracias por su compra!"])
        return "\n".join(lines)

    @staticmethod
    def ticket_rows(sale):
        return [
            {
                "name": item.product.name if item.product else f"Producto {item.product_id}",
                "unit_measure": item.product.unit_measure if item.product else "u",
                "quantity": item.quantity,
                "price": item.price,
                "total": item.total_amount,
            }
            for item in sale.items
        ]

    @staticmethod
    def whatsapp_text(sale, ticket_brand):
        lines = [f"{ticket_brand} - Ticket de compra", f"Venta #{sale.id}", f"Fecha: {sale.date:%Y-%m-%d %H:%M}"]
        if sale.customer:
            lines.append(f"Cliente: {sale.customer}")
        lines.append("------------------------------")
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            unit_measure = item.product.unit_measure if item.product else "u"
            lines.append(f"{name}: ${item.price:.2f} x {item.quantity:g} {unit_measure} = ${item.total_amount:.2f}")
        lines.extend([
            "------------------------------",
            f"Subtotal: ${sale.subtotal:.2f}",
            f"Descuento: -${sale.discount:.2f}",
            f"Impuestos: ${sale.tax:.2f}",
            f"Total: ${sale.total_amount:.2f}",
            "Gracias por su compra!",
        ])
        return "\n".join(lines)

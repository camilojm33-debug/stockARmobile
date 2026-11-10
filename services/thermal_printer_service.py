"""Impresion termica opcional para tickets de venta."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from flask import current_app, has_app_context

try:
    from escpos.printer import Network, Usb, Serial
except Exception:  # pragma: no cover - optional dependency
    Network = None
    Usb = None
    Serial = None


@dataclass
class ThermalPrintResult:
    attempted: bool
    printed: bool
    backend: str | None = None
    message: str | None = None


class ThermalPrinterService:
    def _logger(self):
        if has_app_context():
            return current_app.logger
        return logging.getLogger(__name__)

    def _json_loads(self, value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            payload = json.loads(value)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _company_printer_settings(self, company) -> dict[str, Any]:
        return self._json_loads(getattr(company, "printer_settings_json", None))

    def _company_preferences(self, company) -> dict[str, Any]:
        return self._json_loads(getattr(company, "preferences_json", None))

    def _build_printer(self, settings: dict[str, Any]):
        printer_type = (settings.get("printer_type") or "browser").strip().lower()
        if printer_type == "thermal":
            host = (settings.get("printer_host") or "").strip()
            port = int(settings.get("printer_port") or 9100)
            if host and Network is not None:
                return Network(host, port=port), "network"
        return None, None

    def print_sale_ticket(self, company, sale) -> ThermalPrintResult:
        from services.sales.receipt_service import ReceiptService

        ticket_context = ReceiptService.ticket_context(company=company, sale=sale)
        settings = ticket_context["printer_settings"]
        preferences = self._company_preferences(company)
        printer, backend = self._build_printer(settings)
        if printer is None:
            return ThermalPrintResult(attempted=False, printed=False, message="Impresión térmica no configurada")

        try:
            self._send_ticket(
                printer,
                sale,
                company=company,
                settings=settings,
                preferences=preferences,
                ticket_context=ticket_context,
            )
            if settings.get("cashdrawer_enabled"):
                try:
                    printer.cashdraw()
                except Exception:
                    self._logger().warning("No se pudo abrir el cajón en la impresora termica.")
            try:
                printer.cut()
            except Exception:
                pass
            return ThermalPrintResult(attempted=True, printed=True, backend=backend, message="Ticket enviado a impresora térmica")
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._logger().exception("Error imprimiendo ticket termico: %s", exc)
            return ThermalPrintResult(attempted=True, printed=False, backend=backend, message=str(exc))
        finally:
            try:
                printer.close()
            except Exception:
                pass

    def _send_ticket(
        self,
        printer,
        sale,
        *,
        company,
        settings: dict[str, Any],
        preferences: dict[str, Any],
        ticket_context: dict[str, Any] | None = None,
    ):
        from services.sales.receipt_service import ReceiptService

        compact = bool(preferences.get("compact_print"))
        ticket_context = ticket_context or ReceiptService.ticket_context(company=company, sale=sale)
        ticket_name = (ticket_context.get("ticket_name") or getattr(company, "name", None) or "STOCK ARMOBILE").strip()[:120]
        # Thermal logo support mirrors the receipt identity selected for the sale's branch.
        logo_payload = getattr(company, "logo_data", None)
        logo_url = str(ticket_context.get("logo_url") or "").strip()
        if not logo_payload and logo_url.startswith("/static/") and has_app_context():
            from pathlib import Path
            logo_path = Path(current_app.static_folder or "") / logo_url.removeprefix("/static/")
            try:
                if logo_path.is_file():
                    logo_payload = logo_path.read_bytes()
            except OSError:
                logo_payload = None
        if logo_payload:
            try:
                from io import BytesIO
                from PIL import Image
                image = Image.open(BytesIO(bytes(logo_payload))).convert("RGB")
                image.thumbnail((512, 220))
                printer.set(align="center")
                printer.image(image)
                printer.text("\n")
            except Exception:
                self._logger().warning("No se pudo imprimir el logo del ticket de venta %s.", getattr(sale, "id", None))
        printer.set(align="center", bold=True, width=2 if not compact else 1, height=2 if not compact else 1)
        printer.text(f"{ticket_name or 'STOCK ARMOBILE'}\n")
        printer.set(align="center", bold=False)
        printer.text("Ticket de venta\n")
        printer.text("-" * 32 + "\n")
        printer.set(align="left")
        if ticket_context.get("branch_name"):
            printer.text(f"Sucursal: {ticket_context['branch_name']}\n")
        if ticket_context.get("location_line"):
            printer.text(f"Dirección: {ticket_context['location_line']}\n")
        if ticket_context.get("phone"):
            printer.text(f"Teléfono: {ticket_context['phone']}\n")
        if ticket_context.get("whatsapp"):
            printer.text(f"WhatsApp: {ticket_context['whatsapp']}\n")
        if ticket_context.get("website"):
            printer.text(f"Web: {ticket_context['website']}\n")
        printer.text(f"Venta: #{sale.id}\n")
        printer.text(f"Fecha: {sale.date:%Y-%m-%d %H:%M}\n")
        printer.text(f"Cliente: {sale.customer or 'Consumidor final'}\n")
        if getattr(sale, "payment_method", None):
            payment_line = sale.payment_method
            if getattr(sale, "secondary_payment_method", None):
                payment_line += f" + {sale.secondary_payment_method}"
            printer.text(f"Pago: {payment_line}\n")
        from services.sales.receipt_service import ReceiptService

        printer.text("-" * 32 + "\n")
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            qty = float(item.quantity or 0)
            line = f"{name[:18]:18}\n{qty:g} x ${float(item.price or 0):.2f} = ${float(item.total_amount or 0):.2f}\n"
            printer.text(line)
            wholesale_info = ReceiptService.wholesale_discount_info(item)
            if wholesale_info:
                printer.text(
                    f"  Mayorista {float(wholesale_info['percent']):.2f}%: "
                    f"ahorro ${float(wholesale_info['amount']):.2f} (incluido)\n"
                )
        printer.text("-" * 32 + "\n")
        printer.text(f"Subtotal: ${float(sale.subtotal or 0):.2f}\n")
        wholesale_savings = ReceiptService.wholesale_discount_total(sale)
        if wholesale_savings > 0:
            printer.text(f"Ahorro mayorista ya incluido: ${float(wholesale_savings):.2f}\n")
            printer.text(f"Descuento adicional: -${float(sale.discount or 0):.2f}\n")
        else:
            printer.text(f"Descuento: -${float(sale.discount or 0):.2f}\n")
        printer.text(f"Impuestos: ${float(sale.tax or 0):.2f}\n")
        printer.set(align="left", bold=True)
        printer.text(f"TOTAL: ${float(sale.total_amount or 0):.2f}\n")
        if ticket_context.get("footer"):
            printer.set(align="center", bold=False)
            printer.text(f"{ticket_context['footer']}\n")
        printer.set(align="center", bold=False)
        printer.text("Gracias por su compra\n\n")

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
        settings = self._company_printer_settings(company)
        preferences = self._company_preferences(company)
        printer, backend = self._build_printer(settings)
        if printer is None:
            return ThermalPrintResult(attempted=False, printed=False, message="Impresión térmica no configurada")

        try:
            self._send_ticket(printer, sale, preferences=preferences)
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

    def _send_ticket(self, printer, sale, *, preferences: dict[str, Any]):
        compact = bool(preferences.get("compact_print"))
        printer.set(align="center", bold=True, width=2 if not compact else 1, height=2 if not compact else 1)
        printer.text("STOCK ARMOBILE\n")
        printer.set(align="center", bold=False)
        printer.text("Ticket de venta\n")
        printer.text("-" * 32 + "\n")
        printer.set(align="left")
        printer.text(f"Venta: #{sale.id}\n")
        printer.text(f"Fecha: {sale.date:%Y-%m-%d %H:%M}\n")
        printer.text(f"Cliente: {sale.customer or 'Consumidor final'}\n")
        if getattr(sale, "payment_method", None):
            payment_line = sale.payment_method
            if getattr(sale, "secondary_payment_method", None):
                payment_line += f" + {sale.secondary_payment_method}"
            printer.text(f"Pago: {payment_line}\n")
        printer.text("-" * 32 + "\n")
        for item in sale.items:
            name = item.product.name if item.product else f"Producto {item.product_id}"
            qty = float(item.quantity or 0)
            line = f"{name[:18]:18}\n{qty:g} x ${float(item.price or 0):.2f} = ${float(item.total_amount or 0):.2f}\n"
            printer.text(line)
        printer.text("-" * 32 + "\n")
        printer.text(f"Subtotal: ${float(sale.subtotal or 0):.2f}\n")
        printer.text(f"Descuento: -${float(sale.discount or 0):.2f}\n")
        printer.text(f"Impuestos: ${float(sale.tax or 0):.2f}\n")
        printer.set(align="left", bold=True)
        printer.text(f"TOTAL: ${float(sale.total_amount or 0):.2f}\n")
        printer.set(align="center", bold=False)
        printer.text("Gracias por su compra\n\n")

"""Local StockArMobile print bridge for Windows PCs.

Runs on the cashier PC and exposes a small localhost API so the browser-based
StockArMobile POS can print to USB/Windows-installed or Ethernet ESC/POS
thermal printers without changing the cloud application's existing behavior.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import socket
import sys
from io import BytesIO
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

try:
    import win32print
except ImportError:  # pragma: no cover - optional on non-Windows development hosts
    win32print = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover - installed by desktop_agent/requirements.txt
    Image = None

HOST = os.getenv("STOCKAR_PRINT_HOST", "127.0.0.1")
PORT = int(os.getenv("STOCKAR_PRINT_PORT", "8765"))
DEFAULT_PRINTER = os.getenv("STOCKAR_PRINTER_NAME", "").strip()
DEFAULT_NETWORK_HOST = os.getenv("STOCKAR_PRINTER_HOST", "").strip()
DEFAULT_NETWORK_PORT = int(os.getenv("STOCKAR_PRINTER_PORT", "9100"))
MAX_BODY_BYTES = 256 * 1024


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(raw)


def _escpos_text(text: str) -> bytes:
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    # Windows-1252 is broadly supported by inexpensive ESC/POS printers.
    return cleaned.encode("cp1252", errors="replace")


def _money(value: Any) -> str:
    """Format monetary values using Argentine-style separators (1.234,56)."""
    amount = float(value or 0)
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _escpos_logo(data_url: Any) -> bytes:
    """Render a bounded PNG/JPEG data URL as ESC/POS raster graphics."""
    if Image is None or not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        return b""
    try:
        _header, encoded = data_url.split(",", 1)
        if len(encoded) > 180_000:
            return b""
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > 135_000:
            return b""
        with Image.open(BytesIO(raw)) as source:
            image = source.convert("L")
            image.thumbnail((384, 180))
            width, height = image.size
            width_bytes = (width + 7) // 8
            pixels = image.load()
            raster = bytearray()
            for y in range(height):
                for byte_index in range(width_bytes):
                    value = 0
                    for bit in range(8):
                        x = byte_index * 8 + bit
                        if x < width and pixels[x, y] < 190:
                            value |= 1 << (7 - bit)
                    raster.append(value)
            header = b"\x1dv0\x00" + bytes((
                width_bytes & 0xFF, (width_bytes >> 8) & 0xFF,
                height & 0xFF, (height >> 8) & 0xFF,
            ))
            return b"\x1ba\x01" + header + bytes(raster) + b"\n"
    except (ValueError, TypeError, binascii.Error, OSError):
        return b""
    except Exception:
        return b""


def _build_ticket_bytes(ticket: dict[str, Any]) -> bytes:
    lines = []
    brand = str(ticket.get("brand") or "STOCK ARMOBILE").strip()
    sale_id = ticket.get("sale_id")
    date = str(ticket.get("date") or "")
    customer = str(ticket.get("customer") or "Consumidor final")
    branch = str(ticket.get("branch") or "")
    address = str(ticket.get("address") or "")
    phone = str(ticket.get("phone") or "")
    whatsapp = str(ticket.get("whatsapp") or "")
    website = str(ticket.get("website") or "")
    footer = str(ticket.get("footer") or "")
    payment = str(ticket.get("payment_method") or "")

    paper_size = str(ticket.get("paper_size") or "Ticket80").strip().lower()
    separator = "-" * (48 if paper_size in {"ticket80", "80mm", "80 mm"} else 32)

    data = bytearray()
    data += b"\x1b@"  # initialize
    logo = _escpos_logo(ticket.get("logo_data_url"))
    if logo:
        data += logo
    data += b"\x1ba\x01"  # center
    data += b"\x1bE\x01"  # bold
    data += b"\x1d!\x11"  # double width/height
    data += _escpos_text(f"{brand}\n")
    data += b"\x1d!\x00"
    data += b"\x1bE\x00"
    data += _escpos_text("Ticket de venta\n")
    data += _escpos_text(f"{separator}\n")
    data += b"\x1ba\x00"  # left
    if branch:
        data += _escpos_text(f"Sucursal: {branch}\n")
    if address:
        data += _escpos_text(f"Dirección: {address}\n")
    if phone:
        data += _escpos_text(f"Teléfono: {phone}\n")
    if whatsapp:
        data += _escpos_text(f"WhatsApp: {whatsapp}\n")
    if website:
        data += _escpos_text(f"Web: {website}\n")
    data += _escpos_text(f"Venta: #{sale_id}\n")
    if date:
        data += _escpos_text(f"Fecha: {date}\n")
    data += _escpos_text(f"Cliente: {customer}\n")
    if payment:
        data += _escpos_text(f"Pago: {payment}\n")
    data += _escpos_text(f"{separator}\n")

    for item in ticket.get("items") or []:
        name = str(item.get("name") or "Producto")
        qty = float(item.get("quantity") or 0)
        unit = float(item.get("unit_price") or 0)
        total = float(item.get("total") or 0)
        if qty == 1:
            data += _escpos_text(f"{name[:30]}: ${_money(unit)}\n")
        else:
            data += _escpos_text(
                f"{name[:30]}\n{qty:g} x ${_money(unit)} = ${_money(total)}\n"
            )

    data += _escpos_text(f"{separator}\n")
    subtotal = float(ticket.get("subtotal") or 0)
    discount = float(ticket.get("discount") or 0)
    surcharge = float(ticket.get("surcharge") or 0)
    tax = float(ticket.get("tax") or 0)
    grand_total = float(ticket.get("total") or 0)
    data += _escpos_text(f"Subtotal: ${_money(subtotal)}\n")
    if discount:
        data += _escpos_text(f"Descuento: -${_money(discount)}\n")
    if surcharge:
        data += _escpos_text(f"Recargo: ${_money(surcharge)}\n")
    if tax:
        data += _escpos_text(f"Impuestos: ${_money(tax)}\n")
    data += b"\x1bE\x01"
    data += _escpos_text(f"TOTAL: ${_money(grand_total)}\n")
    data += b"\x1bE\x00"
    note = str(ticket.get("note") or "").strip()
    if note:
        data += _escpos_text(f"Obs.: {note}\n")
    if footer:
        data += b"\x1ba\x01"
        data += _escpos_text(f"{footer}\n")
    data += b"\x1ba\x01"
    data += _escpos_text("Gracias por su compra\n\n\n")
    data += b"\x1dV\x00"  # cut
    return bytes(data)


def _list_windows_printers() -> list[dict[str, str]]:
    if win32print is None:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    rows = []
    for printer in win32print.EnumPrinters(flags):
        name = printer[2]
        rows.append({"name": name, "type": "windows"})
    return rows


def _raw_print_windows(printer_name: str, payload: bytes) -> None:
    if win32print is None:
        raise RuntimeError("win32print no está disponible. Ejecutá el agente en Windows.")
    handle = win32print.OpenPrinter(printer_name)
    try:
        job = win32print.StartDocPrinter(handle, 1, ("StockArMobile", None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, payload)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
    finally:
        win32print.ClosePrinter(handle)


def _raw_print_network(host: str, port: int, payload: bytes) -> None:
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(payload)


class Handler(BaseHTTPRequestHandler):
    server_version = "StockArMobilePrintAgent/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stdout.write((format % args) + "\n")
        sys.stdout.flush()

    def do_OPTIONS(self) -> None:  # noqa: N802
        _json_response(self, 204, {})

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            _json_response(self, 200, {"ok": True, "service": "stockarmobile-print-agent", "version": "1.0"})
            return
        if self.path.rstrip("/") == "/printers":
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "default_printer": DEFAULT_PRINTER,
                    "default_network": {"host": DEFAULT_NETWORK_HOST, "port": DEFAULT_NETWORK_PORT},
                    "printers": _list_windows_printers(),
                },
            )
            return
        _json_response(self, 404, {"ok": False, "error": "Ruta no encontrada"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/print":
            _json_response(self, 404, {"ok": False, "error": "Ruta no encontrada"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("Payload inválido")
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Payload inválido")

            ticket = payload.get("ticket") or {}
            printer_type = str(payload.get("printer_type") or "").strip().lower()
            printer_name = str(payload.get("printer_name") or DEFAULT_PRINTER).strip()
            network_host = str(payload.get("printer_host") or DEFAULT_NETWORK_HOST).strip()
            network_port = int(payload.get("printer_port") or DEFAULT_NETWORK_PORT)

            # "thermal" is the value stored by StockArMobile company settings.
            # With a host we use direct TCP/9100; without it we use the Windows
            # installed printer. Explicit "network"/"ethernet"/"tcp" and
            # "windows" remain supported for backwards compatibility.
            raw_ticket = _build_ticket_bytes(ticket)

            use_network = printer_type in {"network", "ethernet", "tcp"} or (
                printer_type == "thermal" and bool(network_host)
            )
            if use_network:
                if not network_host:
                    raise ValueError("Falta printer_host para impresora de red")
                _raw_print_network(network_host, network_port, raw_ticket)
                backend = "network"
            else:
                if not printer_name:
                    raise ValueError("Falta printer_name para impresora USB/Windows")
                _raw_print_windows(printer_name, raw_ticket)
                backend = "windows"

            _json_response(self, 200, {"ok": True, "printed": True, "backend": backend})
        except Exception as exc:
            _json_response(self, 400, {"ok": False, "printed": False, "error": str(exc)})


if __name__ == "__main__":
    print(f"StockArMobile Print Agent escuchando en http://{HOST}:{PORT}")
    print("GET /health   GET /printers   POST /print")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()

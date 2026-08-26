"""Utilities to build consistent WhatsApp share links across modules."""

from __future__ import annotations

import re
from urllib.parse import quote


# WhatsApp messages are customer-facing documents. Keep each product on one
# clear commercial line instead of repeating unit price and subtotal when the
# quantity is one.
_QUOTE_LINE_RE = re.compile(
    r"^(?P<prefix>-\s*)(?P<name>.+?)\s*\|\s*Cant:\s*(?P<qty>[0-9]+(?:\.[0-9]+)?)\s*\|\s*"
    r"Precio:\s*(?P<currency>[A-Z]{3})\s*(?P<unit>[0-9]+(?:\.[0-9]+)?)\s*\|\s*"
    r"Subtotal:\s*(?P=subcurrency>[A-Z]{3})\s*(?P<total>[0-9]+(?:\.[0-9]+)?)$"
)

_TICKET_LINE_RE = re.compile(
    r"^(?P<name>.+?):\s*\$(?P<unit>[0-9]+(?:\.[0-9]+)?)\s*x\s*(?P<qty>[0-9]+(?:\.[0-9]+)?)"
    r"(?:\s+(?P<measure>[^=]+?))?\s*=\s*\$(?P<total>[0-9]+(?:\.[0-9]+)?)$"
)


def _remove_argentina_mobile_prefix(digits: str) -> str:
    for area_length in (2, 3, 4):
        if len(digits) > area_length + 2 and digits[area_length : area_length + 2] == "15":
            candidate = digits[:area_length] + digits[area_length + 2 :]
            if len(candidate) == 10:
                return candidate
    return digits


def normalize_whatsapp_number(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    digits = digits.lstrip("0")
    if not digits:
        return ""

    if digits.startswith("54"):
        national_number = digits[2:].lstrip("0")
        if national_number.startswith("9"):
            national_number = national_number[1:]
        national_number = _remove_argentina_mobile_prefix(national_number)
        if len(national_number) == 10:
            return f"549{national_number}"
        return f"54{national_number}"

    if len(digits) == 10 and digits.startswith("15"):
        digits = f"11{digits[2:]}"
    digits = _remove_argentina_mobile_prefix(digits)
    if len(digits) == 10:
        return f"549{digits}"
    return digits


def _format_quantity(value: str) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.3f}".rstrip("0").rstrip(".")


def _professionalize_item_line(line: str) -> str:
    """Collapse duplicated customer-facing price/subtotal information."""
    match = _QUOTE_LINE_RE.match(line.strip())
    if match:
        qty = float(match.group("qty"))
        name = match.group("name").strip()
        currency = match.group("currency")
        unit = float(match.group("unit"))
        total = float(match.group("total"))
        if qty == 1:
            return f"{match.group('prefix')}{name}: {currency} {unit:.2f}"
        return f"{match.group('prefix')}{name}: {_format_quantity(match.group('qty'))} x {currency} {unit:.2f} = {currency} {total:.2f}"

    match = _TICKET_LINE_RE.match(line.strip())
    if match:
        qty = float(match.group("qty"))
        name = match.group("name").strip()
        unit = float(match.group("unit"))
        total = float(match.group("total"))
        if qty == 1:
            return f"{name}: ${unit:.2f}"
        measure = f" {match.group('measure').strip()}" if match.group("measure") else ""
        return f"{name}: {_format_quantity(match.group('qty'))} x ${unit:.2f}{measure} = ${total:.2f}"

    return line


def _professionalize_whatsapp_message(message: str) -> str:
    return "\n".join(_professionalize_item_line(line) for line in (message or "").splitlines())


def build_whatsapp_share_url(*, phone: str | None, message: str, document_url: str | None = None, document_label: str = "PDF") -> str:
    normalized_phone = normalize_whatsapp_number(phone)
    base_message = _professionalize_whatsapp_message((message or "").strip())
    if document_url:
        link_line = f"{document_label}: {document_url}"
        full_message = f"{base_message}\n\n{link_line}" if base_message else link_line
    else:
        full_message = base_message

    text = quote(full_message, safe="")
    if normalized_phone:
        return f"https://wa.me/{normalized_phone}?text={text}"
    return f"https://wa.me/?text={text}"

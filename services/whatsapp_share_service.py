"""Utilities to build consistent WhatsApp share links across modules."""

from __future__ import annotations

from urllib.parse import quote


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


def build_whatsapp_share_url(*, phone: str | None, message: str, document_url: str | None = None, document_label: str = "PDF") -> str:
    normalized_phone = normalize_whatsapp_number(phone)
    base_message = (message or "").strip()
    if document_url:
        link_line = f"{document_label}: {document_url}"
        full_message = f"{base_message}\n\n{link_line}" if base_message else link_line
    else:
        full_message = base_message

    text = quote(full_message, safe="")
    if normalized_phone:
        return f"https://wa.me/{normalized_phone}?text={text}"
    return f"https://wa.me/?text={text}"

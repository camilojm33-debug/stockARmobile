"""Utilities to build consistent WhatsApp share links across modules."""

from __future__ import annotations

from urllib.parse import quote


def normalize_whatsapp_number(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())


def build_whatsapp_share_url(*, phone: str | None, message: str, document_url: str | None = None, document_label: str = "PDF") -> str:
    normalized_phone = normalize_whatsapp_number(phone)
    base_message = (message or "").strip()
    if document_url:
        link_line = f"{document_label}: {document_url}"
        full_message = f"{base_message}\n\n{link_line}" if base_message else link_line
    else:
        full_message = base_message

    text = quote(full_message)
    if normalized_phone:
        return f"https://wa.me/{normalized_phone}?text={text}"
    return f"https://wa.me/?text={text}"

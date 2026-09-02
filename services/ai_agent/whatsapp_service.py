"""WhatsApp Cloud API transport for the StockARmobile vendor agent."""

from __future__ import annotations

import os
from typing import Any, Dict

import requests

from .config_service import get_whatsapp_connection


class WhatsAppService:
    API_VERSION = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v23.0")

    @classmethod
    def send_text(cls, company, *, to: str, body: str) -> Dict[str, Any]:
        connection = get_whatsapp_connection(company)
        phone_number_id = connection["phone_number_id"]
        access_token = connection["access_token"]
        if not phone_number_id or not access_token:
            raise RuntimeError("WhatsApp no está configurado para esta empresa.")
        recipient = "".join(ch for ch in str(to or "") if ch.isdigit())
        if not recipient:
            raise ValueError("El número de WhatsApp del destinatario es inválido.")

        url = f"https://graph.facebook.com/{cls.API_VERSION}/{phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": True, "body": str(body or "")[:4096]},
        }
        response = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            timeout=float(os.getenv("WHATSAPP_API_TIMEOUT", "20")),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"WhatsApp API HTTP {response.status_code}: {response.text[:500]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("WhatsApp API devolvió JSON inválido.") from exc
        return data
